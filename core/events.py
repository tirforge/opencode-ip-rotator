"""Lightweight in-memory request telemetry for the dashboard.

Kept deliberately small: a ring buffer of recent proxied requests plus a
per-minute series for the traffic chart. Nothing here is durable — durable
totals live in ``model_usage`` / ``rotation_log`` in SQLite.
"""

import collections
import threading
import time
from typing import Any, Deque, Dict, List

from starlette.middleware.base import BaseHTTPMiddleware

_MAX_EVENTS = 400
_events: Deque[Dict[str, Any]] = collections.deque(maxlen=_MAX_EVENTS)
_lock = threading.Lock()

_ENDPOINT_LABELS = {
    "/v1/chat/completions": "chat",
    "/v1/messages": "messages",
    "/v1/responses": "responses",
}


def record_event(
    path: str,
    status: int,
    duration_ms: float,
    client: str = "",
) -> None:
    event = {
        "at": time.time(),
        "endpoint": _ENDPOINT_LABELS.get(path, path),
        "path": path,
        "status": int(status),
        "duration_ms": round(float(duration_ms), 1),
        "client": client,
        "ok": 200 <= int(status) < 400,
    }
    with _lock:
        _events.append(event)


def recent_events(limit: int = 40) -> List[Dict[str, Any]]:
    with _lock:
        return list(_events)[-max(1, limit):][::-1]


def request_series(minutes: int = 30) -> List[Dict[str, Any]]:
    """Requests per minute for the last ``minutes`` minutes."""
    minutes = max(1, min(180, minutes))
    now = time.time()
    start = int(now // 60) - (minutes - 1)
    buckets = {start + offset: {"ok": 0, "error": 0} for offset in range(minutes)}
    with _lock:
        snapshot = list(_events)
    for event in snapshot:
        minute = int(event["at"] // 60)
        bucket = buckets.get(minute)
        if bucket is None:
            continue
        bucket["ok" if event["ok"] else "error"] += 1
    series = []
    for minute in sorted(buckets):
        entry = buckets[minute]
        series.append(
            {
                "minute": minute * 60,
                "ok": entry["ok"],
                "error": entry["error"],
                "total": entry["ok"] + entry["error"],
            }
        )
    return series


def window_totals(minutes: int = 60) -> Dict[str, int]:
    cutoff = time.time() - minutes * 60
    with _lock:
        snapshot = list(_events)
    recent = [event for event in snapshot if event["at"] >= cutoff]
    return {
        "requests": len(recent),
        "ok": sum(1 for event in recent if event["ok"]),
        "errors": sum(1 for event in recent if not event["ok"]),
    }


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Record proxied inference calls for the live dashboard feed."""

    async def dispatch(self, request, call_next):
        if not request.url.path.startswith("/v1/"):
            return await call_next(request)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            record_event(
                request.url.path,
                500,
                (time.perf_counter() - started) * 1000,
                request.client.host if request.client else "",
            )
            raise

        record_event(
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            request.client.host if request.client else "",
        )
        return response
