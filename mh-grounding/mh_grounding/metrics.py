"""mh_grounding.metrics — Prometheus instruments for OUT-OF-OWUI grounding processes.

Inside OWUI, mh-tools keep using the fork's OTel decorator
(open_webui/utils/telemetry/mh_tools.py -> the :9094 exposition); this module is the
equivalent for standalone processes (mh-mcp). Metric NAMES match the OWUI-side emitter
so the mh-ai-stack dashboard aggregates across both, with a `client` label to split
the series by consumer.

Safe no-op: without prometheus-client installed, or before init() is called, every
hook silently does nothing — a tool never needs to know whether metrics are enabled.
"""

import functools
import logging
import time

log = logging.getLogger("mh.grounding.metrics")

try:
    from prometheus_client import Counter, Histogram, start_http_server
    _PROM_OK = True
except ImportError:
    _PROM_OK = False

_instruments = None
_client_label = "unknown"


def init(port=None, client="mh-mcp", addr="127.0.0.1"):
    """Create the instruments (and optionally start a loopback /metrics exposition).
    Call once at process start; everything stays a no-op without it."""
    global _instruments, _client_label
    if not _PROM_OK:
        log.warning("prometheus-client unavailable — mh-grounding metrics off")
        return False
    _client_label = client
    if _instruments is None:
        _instruments = (
            Counter("mh_tool_calls_total",
                    "mh-tool invocations, tagged tool/class/status",
                    ["tool", "class", "status", "client"]),
            Histogram("mh_tool_duration_seconds",
                      "mh-tool wall-clock duration",
                      ["tool", "class", "client"]),
            Counter("mh_governor_events_total",
                    "over-search governor actions (dedup / read-nudge)",
                    ["kind", "tool", "client"]),
        )
    if port:
        start_http_server(int(port), addr=addr)
        log.info("mh-grounding metrics exposition on %s:%s", addr, port)
    return True


def record_call(tool, cls, status, seconds):
    if _instruments is None:
        return
    counter, hist, _ = _instruments
    hist.labels(tool=tool, **{"class": cls}, client=_client_label).observe(seconds)
    counter.labels(tool=tool, **{"class": cls}, status=status, client=_client_label).inc()


def governor_event(kind, tool):
    if _instruments is None:
        return
    _instruments[2].labels(kind=kind, tool=tool, client=_client_label).inc()


def instrument(tool, cls):
    """Decorator for an async tool entry point: counts + times the call, mirroring the
    OWUI-side semantics (errors degrade into model-readable strings, so status="error"
    captures only true exceptions)."""
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
