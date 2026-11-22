"""Live request telemetry: ring buffer, per-minute series and token throughput.

Every proxied inference call becomes one event. Streaming calls are finished
twice: once when the response headers arrive (status/TTFB) and once when the
last SSE chunk is written (full duration, tokens, tokens/second). Updates are
keyed by an event id, so the counters never double-count.
"""

from __future__ import annotations

import collections
import itertools
import threading
import time
from typing import Any, Deque, Dict, List, Optional

from starlette.middleware.base import BaseHTTPMiddleware

_MAX_EVENTS = 500
_events: Deque[Dict[str, Any]] = collections.deque(maxlen=_MAX_EVENTS)
_lock = threading.Lock()
_seq = itertools.count(1)

_ENDPOINT_LABELS = {
    "/v1/chat/completions": "chat",
    "/v1/messages": "messages",
    "/v1/responses": "responses",
}


def begin_event(path: str, client: str = "") -> str:
    event_id = f"e{next(_seq)}"
    event = {
        "id": event_id,
        "at": time.time(),
        "endpoint": _ENDPOINT_LABELS.get(path, path),
        "path": path,
        "status": 0,
        "duration_ms": 0.0,
        "client": client,
        "ok": True,
        "pending": True,
        "model": None,
        "tokens_in": 0,
        "tokens_out": 0,
        "tps": None,
        "stream": False,
    }
    with _lock:
        _events.append(event)
    return event_id


def _find(event_id: str) -> Optional[Dict[str, Any]]:
    for event in reversed(_events):
        if event.get("id") == event_id:
            return event
    return None


def finish_event(event_id: str, **fields: Any) -> None:
    with _lock:
        event = _find(event_id)
        if event is None:
            return
        event.update(fields)
        event["pending"] = False


def record_event(
    path: str,
    status: int,
    duration_ms: float,
    client: str = "",
    **extra: Any,
) -> None:
    """Compatibility helper for callers that have all data up front."""
    event_id = begin_event(path, client)
    payload: Dict[str, Any] = {
        "status": int(status),
        "duration_ms": round(float(duration_ms), 1),
        "ok": 200 <= int(status) < 400,
    }
    payload.update(extra)
    finish_event(event_id, **payload)


def recent_events(limit: int = 40) -> List[Dict[str, Any]]:
    with _lock:
        items = list(_events)
    return items[-max(1, limit):][::-1]


def request_series(minutes: int = 30) -> List[Dict[str, Any]]:
    """Requests + tokens per minute for the last ``minutes`` minutes."""
    minutes = max(1, min(180, minutes))
    now = time.time()
    start = int(now // 60) - (minutes - 1)
    buckets = {
        start + offset: {"ok": 0, "error": 0, "tokens": 0, "tps_sum": 0.0, "tps_n": 0}
        for offset in range(minutes)
    }
    with _lock:
        snapshot = list(_events)
    for event in snapshot:
        minute = int(event["at"] // 60)
        bucket = buckets.get(minute)
        if bucket is None:
            continue
        if event.get("ok"):
            bucket["ok"] += 1
        else:
            bucket["error"] += 1
        tokens = int(event.get("tokens_out") or 0)
        bucket["tokens"] += tokens
        if event.get("tps"):
            bucket["tps_sum"] += float(event["tps"])
            bucket["tps_n"] += 1
    series = []
    for minute in sorted(buckets):
        entry = buckets[minute]
        series.append(
            {
                "minute": minute * 60,
                "ok": entry["ok"],
                "error": entry["error"],
                "total": entry["ok"] + entry["error"],
                "tokens": entry["tokens"],
                "tps": round(entry["tps_sum"] / entry["tps_n"], 2) if entry["tps_n"] else 0,
            }
        )
    return series


def window_totals(minutes: int = 60) -> Dict[str, Any]:
    cutoff = time.time() - minutes * 60
    with _lock:
        snapshot = list(_events)
    recent = [event for event in snapshot if event["at"] >= cutoff]
    tokens_out = sum(int(event.get("tokens_out") or 0) for event in recent)
    tps_values = [float(event["tps"]) for event in recent if event.get("tps")]
    # Weighted average: throughput is per request, so weight by token count.
    weighted = sum(
        float(event["tps"]) * int(event.get("tokens_out") or 0)
        for event in recent
        if event.get("tps")
    )
    avg_tps = round(weighted / tokens_out, 1) if tokens_out else 0.0
    return {
        "requests": len(recent),
        "ok": sum(1 for event in recent if event.get("ok")),
        "errors": sum(1 for event in recent if not event.get("ok")),
        "tokens_out": tokens_out,
        "avg_tps": avg_tps,
        "tps_samples": len(tps_values),
    }


def token_stats() -> Dict[str, Any]:
    """All-time (in-memory) token totals plus the 60 minute window."""
    with _lock:
        snapshot = list(_events)
    tokens_in = sum(int(event.get("tokens_in") or 0) for event in snapshot)
    tokens_out = sum(int(event.get("tokens_out") or 0) for event in snapshot)
    weighted = sum(
        float(event["tps"]) * int(event.get("tokens_out") or 0)
        for event in snapshot
        if event.get("tps")
    )
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "avg_tps": round(weighted / tokens_out, 1) if tokens_out else 0.0,
        "last_hour": window_totals(60),
    }


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Record proxied inference calls for the live dashboard feed."""

    async def dispatch(self, request, call_next):
        if not request.url.path.startswith("/v1/"):
            return await call_next(request)

        started = time.perf_counter()
        event_id = begin_event(
            request.url.path, request.client.host if request.client else ""
        )
        request.state.event_id = event_id

        try:
            response = await call_next(request)
        except Exception:
            finish_event(
                event_id,
                status=500,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                ok=False,
            )
            raise

        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        # Streaming responses finish later inside the SSE generator; keep the
        # event open so token accounting can fill in the real numbers.
        streaming = response.headers.get(
            "content-type", ""
        ).startswith("text/event-stream")
        finish_event(
            event_id,
            status=response.status_code,
            duration_ms=elapsed_ms,
            ok=200 <= response.status_code < 400,
            stream=streaming,
            pending=streaming,
        )
        return response
