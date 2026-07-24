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
_sessions_seen = set()
_session_counter = None


def init(port=None, client="mh-mcp", addr="127.0.0.1"):
    """Create the instruments (and optionally start a loopback /metrics exposition).
    Call once at process start; everything stays a no-op without it."""
    global _instruments, _client_label, _session_counter
    if not _PROM_OK:
        log.warning("prometheus-client unavailable — mh-grounding metrics off")
        return False
    _client_label = client
    if _instruments is None:
        _instruments = (
            Counter("mh_tool_calls_total",
                    "mh-tool invocations, tagged tool/class/status",
                    ["tool", "class", "status", "client", "user"]),
            Histogram("mh_tool_duration_seconds",
                      "mh-tool wall-clock duration",
                      ["tool", "class", "client", "user"]),
            Counter("mh_governor_events_total",
                    "over-search governor actions (dedup / read-nudge)",
                    ["kind", "tool", "client", "user"]),
        )
        _session_counter = Counter(
            "mh_mcp_sessions_total",
            "distinct MCP client sessions seen since process start", ["client"])
        _session_counter.labels(client=client)  # pre-create the 0 series (dashboards)
        # Process self-health on darwin (prometheus_client's ProcessCollector needs /proc,
        # absent on macOS) — CPU seconds + peak RSS via os/resource, cheap and dependency-free.
        try:
            import os as _os
            import resource as _resource
            from prometheus_client import Gauge
            Gauge("mh_process_start_time_seconds",
                  "process start time (unix)", ["client"]).labels(client=client).set(
                __import__("time").time())
            Gauge("mh_process_cpu_seconds",
                  "user+system CPU seconds (os.times)", ["client"]).labels(
                client=client).set_function(lambda: sum(_os.times()[:2]))
            Gauge("mh_process_peak_rss_bytes",
                  "peak resident set size (getrusage ru_maxrss)", ["client"]).labels(
                client=client).set_function(
                lambda: _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss)
        except Exception as e:
            log.warning("process self-metrics unavailable: %s", e)
    if port:
        start_http_server(int(port), addr=addr)
        log.info("mh-grounding metrics exposition on %s:%s", addr, port)
    return True


def session_seen(session_key):
    """Count a distinct MCP session the first time it appears (call from the server's
    per-session plumbing; cheap set-dedup, cleared on restart like everything else)."""
    if _session_counter is None or session_key in _sessions_seen:
        return
    _sessions_seen.add(session_key)
    _session_counter.labels(client=_client_label).inc()


# Per-call USER attribution (household-scale cardinality). The serving layer registers a
# resolver mapping a tool's Context -> a user label ("local" for loopback; the caller's
# Tailscale-User-Login for Serve-proxied requests). Unresolvable -> "unknown".
_user_resolver = None


def set_user_resolver(fn):
    global _user_resolver
    _user_resolver = fn


def _resolve_user(kwargs):
    if _user_resolver is None:
        return ""
    try:
        ctx = kwargs.get("ctx")
        return _user_resolver(ctx) if ctx is not None else "unknown"
    except Exception:
        return "unknown"


def record_call(tool, cls, status, seconds, user=""):
    if _instruments is None:
        return
    counter, hist, _ = _instruments
    hist.labels(tool=tool, **{"class": cls}, client=_client_label, user=user).observe(seconds)
    counter.labels(tool=tool, **{"class": cls}, status=status, client=_client_label,
                   user=user).inc()


def governor_event(kind, tool, user=""):
    if _instruments is None:
        return
    _instruments[2].labels(kind=kind, tool=tool, client=_client_label, user=user).inc()


def instrument(tool, cls):
    """Decorator for an async tool entry point: counts + times the call, mirroring the
    OWUI-side semantics (errors degrade into model-readable strings, so status="error"
    captures only true exceptions). If the tool takes a Context kwarg and a user resolver
    is registered, calls are attributed per user."""
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
                record_call(tool, cls, status, time.perf_counter() - start,
                            user=_resolve_user(kwargs))
        return wrapper
    return deco
