"""In-process metrics for conversation-security events.

Single-instance, thread-safe counters + a coarse processing-time aggregate. Exposed via
GET /api/metrics. For multi-instance/HA replace with a shared store or OpenTelemetry (see DEV.md).
"""
import threading
import time

_lock = threading.Lock()
_counters: dict[str, int] = {}
_proc_count = 0
_proc_total_ms = 0.0
_started_at = time.time()

# Canonical metric names (kept stable for dashboards).
SESSIONS_STARTED = "sessions_started"
SESSIONS_COMPLETED = "sessions_completed"
SESSIONS_EXPIRED = "sessions_expired"
DUPLICATE_CLICKS = "duplicate_clicks_ignored"
RETRIES_IGNORED = "retries_ignored"
BLOCKED_MESSAGES = "blocked_messages"
EXCEPTIONS = "exceptions"


def increment(name: str, by: int = 1) -> None:
    with _lock:
        _counters[name] = _counters.get(name, 0) + by


def observe_processing_ms(ms: float) -> None:
    global _proc_count, _proc_total_ms
    with _lock:
        _proc_count += 1
        _proc_total_ms += ms


def snapshot() -> dict:
    with _lock:
        avg = (_proc_total_ms / _proc_count) if _proc_count else 0.0
        return {
            "uptime_seconds": round(time.time() - _started_at, 1),
            "counters": dict(_counters),
            "processing": {
                "count": _proc_count,
                "total_ms": round(_proc_total_ms, 1),
                "avg_ms": round(avg, 1),
            },
        }


def reset() -> None:
    """Test helper — clears all counters."""
    global _proc_count, _proc_total_ms
    with _lock:
        _counters.clear()
        _proc_count = 0
        _proc_total_ms = 0.0
