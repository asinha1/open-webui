"""[mh] OpenTelemetry instruments for the mh-tools layer.

mh-tools run inside the OWUI process, so they emit on the same global
MeterProvider that ``utils/telemetry/metrics.py`` wires to the Prometheus pull
endpoint (loopback :9094, gated by ``OTEL_METRICS_PROMETHEUS_EXPORT_PORT``). When
``ENABLE_OTEL_METRICS`` is off, ``metrics.get_meter`` yields a no-op meter, so all
of this is a safe no-op — a tool never needs to know whether metrics are enabled.

Usage in an mh-tool (signature-transparent — OWUI's ``inspect.signature``-based
spec parser follows ``__wrapped__``, so the model-facing spec is unchanged):

    from open_webui.utils.telemetry.mh_tools import instrument

    class Tools:
        @instrument("tavily_search", "web")
        async def tavily_search(self, query: str, ...) -> str:
            ...

``class`` is the usage axis the dashboards split on: ``web`` | ``rag`` | ``local``.
"""

from __future__ import annotations

import functools
import time

from opentelemetry import metrics

_instruments = None


def _get():
    """Lazily create the instruments on first use — by which point the global
    MeterProvider set in ``setup_metrics()`` at app startup is live."""
    global _instruments
    if _instruments is None:
        meter = metrics.get_meter("mh-tools")
        _instruments = (
            meter.create_counter(
                "mh_tool_calls_total",
                description="mh-tool invocations, tagged tool/class/status",
                unit="1",
            ),
            meter.create_histogram(
                "mh_tool_duration_seconds",
                description="mh-tool wall-clock duration",
                unit="s",
            ),
            meter.create_counter(
                "mh_governor_events_total",
                description="over-search governor actions (dedup / read-nudge)",
                unit="1",
            ),
        )
    return _instruments


def record_call(tool: str, cls: str, status: str, seconds: float) -> None:
    counter, hist, _ = _get()
    attrs = {"tool": tool, "class": cls}
    hist.record(seconds, attrs)
    counter.add(1, {**attrs, "status": status})


def instrument(tool: str, cls: str):
    """Decorator for an mh-tool's async entry method: counts + times the call,
    tagging tool / class (``web`` | ``rag`` | ``local``) / status (``ok`` |
    ``error``). ``functools.wraps`` preserves the signature + docstring so OWUI's
    spec parser is unaffected.

    mh-tools degrade errors into model-readable strings (they rarely raise), so
    ``status="error"`` captures only true exceptions; the call count + duration
    are the primary signal (tool popularity, web-vs-rag ratio, latency)."""

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            start = time.perf_counter()
            status = "ok"
            try:
                return await fn(*args, **kwargs)
            except Exception:
                status = "error"
                raise
            finally:
                record_call(tool, cls, status, time.perf_counter() - start)

        return wrapper

    return deco


def governor_event(kind: str, tool: str) -> None:
    """Record an over-search governor action (``kind`` = ``dedup`` |
    ``read_nudge``). Called from the governor logic in tavily_search /
    deep_research to measure governor effectiveness."""
    _, _, gov = _get()
    gov.add(1, {"kind": kind, "tool": tool})
