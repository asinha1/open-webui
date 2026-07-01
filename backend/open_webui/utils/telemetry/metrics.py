"""OpenTelemetry metrics bootstrap for Open WebUI.

This module initialises a MeterProvider that sends metrics to an OTLP
collector. The collector is responsible for exposing a Prometheus
`/metrics` endpoint – WebUI does **not** expose it directly.

Metrics collected:

* http.server.requests (counter)
* http.server.duration (histogram, milliseconds)

Attributes used: http.method, http.route, http.status_code

If you wish to add more attributes (e.g. user-agent) you can, but beware of
high-cardinality label sets.
"""

from __future__ import annotations

import datetime
import logging
import time
from base64 import b64encode
from typing import Dict, Iterable, List, Optional

from fastapi import FastAPI, Request
from open_webui.env import (
    OTEL_METRICS_BASIC_AUTH_PASSWORD,
    OTEL_METRICS_BASIC_AUTH_USERNAME,
    OTEL_METRICS_EXPORT_INTERVAL_MILLIS,
    OTEL_METRICS_EXPORTER_OTLP_ENDPOINT,
    OTEL_METRICS_EXPORTER_OTLP_INSECURE,
    OTEL_METRICS_OTLP_SPAN_EXPORTER,
    OTEL_METRICS_PROMETHEUS_EXPORT_PORT,
    OTEL_SERVICE_NAME,
)
from open_webui.models.users import User
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter as OTLPHttpMetricExporter,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.metrics.view import View
from opentelemetry.sdk.resources import Resource
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sync DB helpers for OTel gauge callbacks
#
# The OTel Python SDK calls observable-instrument callbacks *synchronously*
# from a background collection thread — async callbacks are NOT supported
# (the SDK does not ``await`` the return value).
#
# Rather than bridging into the async event loop, we run plain synchronous
# SQL queries using the sync engine that is already available at setup time.
# This avoids any cross-thread / cross-loop concerns entirely.
# ---------------------------------------------------------------------------


def _count_total_users(db_engine: Engine) -> Optional[int]:
    """Return the total number of registered users (sync)."""
    with Session(db_engine) as session:
        return session.execute(select(func.count()).select_from(User)).scalar()


def _count_active_users(db_engine: Engine) -> Optional[int]:
    """Return the number of users active within the last 3 minutes (sync)."""
    three_minutes_ago = int(time.time()) - 180
    with Session(db_engine) as session:
        return session.execute(
            select(func.count()).select_from(User).filter(User.last_active_at >= three_minutes_ago)
        ).scalar()


def _count_users_active_today(db_engine: Engine) -> Optional[int]:
    """Return the number of users active since midnight today (sync)."""
    now = int(datetime.datetime.now().timestamp())
    today_midnight = now - (now % 86400)
    with Session(db_engine) as session:
        return session.execute(
            select(func.count()).select_from(User).filter(User.last_active_at > today_midnight)
        ).scalar()


def _build_meter_provider(resource: Resource) -> MeterProvider:
    """Return a configured MeterProvider."""

    # Views limit attribute cardinality (drop user-agent etc.). Shared by both
    # the OTLP-push and the [mh] Prometheus-pull reader paths below.
    views: List[View] = [
        View(
            instrument_name='http.server.duration',
            attribute_keys=['http.method', 'http.route', 'http.status_code'],
        ),
        View(
            instrument_name='http.server.requests',
            attribute_keys=['http.method', 'http.route', 'http.status_code'],
        ),
        View(instrument_name='webui.users.total'),
        View(instrument_name='webui.users.active'),
        View(instrument_name='webui.users.active.today'),
    ]

    # [mh] Prometheus PULL mode: expose metrics on a dedicated loopback /metrics
    # scrape endpoint (the HTTP server is started in setup_metrics) via a
    # PrometheusMetricReader, instead of OTLP-pushing to a collector. Keeps OWUI
    # a normal scrape target alongside the rest of the fleet
    # (provisioning/observability/targets.yaml). Import is lazy so the OTLP path
    # carries no hard dep on opentelemetry-exporter-prometheus.
    if OTEL_METRICS_PROMETHEUS_EXPORT_PORT:
        from opentelemetry.exporter.prometheus import PrometheusMetricReader

        return MeterProvider(
            resource=resource,
            metric_readers=[PrometheusMetricReader()],
            views=views,
        )

    headers = []
    if OTEL_METRICS_BASIC_AUTH_USERNAME and OTEL_METRICS_BASIC_AUTH_PASSWORD:
        auth_string = f'{OTEL_METRICS_BASIC_AUTH_USERNAME}:{OTEL_METRICS_BASIC_AUTH_PASSWORD}'
        auth_header = b64encode(auth_string.encode()).decode()
        headers = [('authorization', f'Basic {auth_header}')]

    # Periodic reader pushes metrics over OTLP/gRPC (or HTTP) to a collector
    if OTEL_METRICS_OTLP_SPAN_EXPORTER == 'http':
        readers: List[PeriodicExportingMetricReader] = [
            PeriodicExportingMetricReader(
                OTLPHttpMetricExporter(endpoint=OTEL_METRICS_EXPORTER_OTLP_ENDPOINT, headers=headers),
                export_interval_millis=OTEL_METRICS_EXPORT_INTERVAL_MILLIS,
            )
        ]
    else:
        readers: List[PeriodicExportingMetricReader] = [
            PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=OTEL_METRICS_EXPORTER_OTLP_ENDPOINT,
                    insecure=OTEL_METRICS_EXPORTER_OTLP_INSECURE,
                    headers=headers,
                ),
                export_interval_millis=OTEL_METRICS_EXPORT_INTERVAL_MILLIS,
            )
        ]

    provider = MeterProvider(
        resource=resource,
        metric_readers=list(readers),
        views=views,
    )
    return provider


def setup_metrics(app: FastAPI, resource: Resource, db_engine: Engine) -> None:
    """Attach OTel metrics middleware to *app* and initialise provider."""

    metrics.set_meter_provider(_build_meter_provider(resource))

    # [mh] In Prometheus-pull mode, expose the registry on a dedicated loopback
    # scrape endpoint. PrometheusMetricReader (wired in _build_meter_provider)
    # registers with prometheus_client's global REGISTRY; start_http_server then
    # serves it. Loopback-only — never the tailnet — so it needs no auth; the
    # Prometheus hub on this same host scrapes 127.0.0.1:<port>/metrics.
    if OTEL_METRICS_PROMETHEUS_EXPORT_PORT:
        try:
            from prometheus_client import start_http_server

            start_http_server(OTEL_METRICS_PROMETHEUS_EXPORT_PORT, addr='127.0.0.1')
            logger.info(
                '[mh] Prometheus metrics endpoint live on 127.0.0.1:%s/metrics',
                OTEL_METRICS_PROMETHEUS_EXPORT_PORT,
            )
        except OSError:
            # Port already bound (e.g. re-entry within one process) — non-fatal.
            logger.warning(
                '[mh] Prometheus metrics port %s already in use; skipping bind',
                OTEL_METRICS_PROMETHEUS_EXPORT_PORT,
            )

    meter = metrics.get_meter(__name__)

    # Instruments
    request_counter = meter.create_counter(
        name='http.server.requests',
        description='Counts the total number of inbound HTTP requests.',
        unit='1',
    )
    duration_histogram = meter.create_histogram(
        name='http.server.duration',
        description='Measures the duration of inbound HTTP requests.',
        unit='ms',
    )

    # -- Observable gauge callbacks ----------------------------------------
    # These are called synchronously by the OTel SDK from a background
    # collection thread.  They use the sync DB engine directly — no async
    # bridging required.

    def observe_total_users(
        options: metrics.CallbackOptions,
    ) -> Iterable[metrics.Observation]:
        try:
            value = _count_total_users(db_engine)
            if value is not None:
                yield metrics.Observation(value=value)
        except Exception:
            logger.debug('Failed to observe total users', exc_info=True)

    def observe_active_users(
        options: metrics.CallbackOptions,
    ) -> Iterable[metrics.Observation]:
        try:
            value = _count_active_users(db_engine)
            if value is not None:
                yield metrics.Observation(value=value)
        except Exception:
            logger.debug('Failed to observe active users', exc_info=True)

    def observe_users_active_today(
        options: metrics.CallbackOptions,
    ) -> Iterable[metrics.Observation]:
        try:
            value = _count_users_active_today(db_engine)
            if value is not None:
                yield metrics.Observation(value=value)
        except Exception:
            logger.debug('Failed to observe users active today', exc_info=True)

    meter.create_observable_gauge(
        name='webui.users.total',
        description='Total number of registered users',
        unit='users',
        callbacks=[observe_total_users],
    )

    meter.create_observable_gauge(
        name='webui.users.active',
        description='Number of currently active users',
        unit='users',
        callbacks=[observe_active_users],
    )

    meter.create_observable_gauge(
        name='webui.users.active.today',
        description='Number of users active since midnight today',
        unit='users',
        callbacks=[observe_users_active_today],
    )

    # FastAPI middleware
    @app.middleware('http')
    async def _metrics_middleware(request: Request, call_next):
        start_time = time.perf_counter()

        status_code = None
        try:
            response = await call_next(request)
            status_code = getattr(response, 'status_code', 500)
            return response
        except Exception:
            status_code = 500
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0

            # Route template e.g. "/items/{item_id}" instead of real path.
            route = request.scope.get('route')
            route_path = getattr(route, 'path', request.url.path)

            attrs: Dict[str, str | int] = {
                'http.method': request.method,
                'http.route': route_path,
                'http.status_code': status_code,
            }

            request_counter.add(1, attrs)
            duration_histogram.record(elapsed_ms, attrs)
