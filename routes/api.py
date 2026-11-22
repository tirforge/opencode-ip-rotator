"""JSON API powering the dashboard: live stats, IP rotation, API keys, tunnel."""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from core.auth import auth_summary
from core.db import (
    count_rotations,
    load_all_usage,
    recent_rotations,
    record_rotation,
)
from core.events import recent_events, request_series, window_totals
from core.keys import create_key, list_keys, revoke_key
from core.metrics import STARTUP_TIME, metrics, prom_active_flows
from core.tunnel import tunnel_info
from services import warp_admin
from services.discovery import get_discovered_models

log = logging.getLogger("routes.api")
router = APIRouter(prefix="/api")


def _active_flows() -> int:
    try:
        return int(prom_active_flows._value.get())  # noqa: SLF001 - gauge read
    except Exception:  # noqa: BLE001
        return 0


def _build_overview() -> Dict[str, Any]:
    now = time.time()
    models = get_discovered_models()
    pool = warp_admin.snapshot()
    usage = load_all_usage()
    totals = window_totals(60)

    total = int(metrics.get("total_requests", 0))
    success = int(metrics.get("successful_requests", 0))
    rate_limited = int(metrics.get("rate_limited_requests", 0))
    failed = int(metrics.get("failed_requests", 0))
    success_rate = round((success / total) * 100, 1) if total else 100.0

    return {
        "service": "opencode-proxy",
        "now": now,
        "started_at": STARTUP_TIME,
        "uptime_seconds": int(now - STARTUP_TIME),
        "totals": {
            "requests": total,
            "successful": success,
            "failed": failed,
            "rate_limited": rate_limited,
            "success_rate": success_rate,
            "active_flows": _active_flows(),
            "last_hour": totals,
        },
        "models": {
            "count": len(models),
            "items": models,
            "usage": usage,
        },
        "pool": pool,
        "egress": {
            "ips": pool.get("ips", []),
            "primary": (pool.get("ips") or [""])[0],
            "online": pool.get("online", False),
        },
        "rotations": {
            "count": count_rotations(),
            "recent": recent_rotations(8),
        },
        "series": request_series(30),
        "events": recent_events(25),
        "security": auth_summary(),
        "tunnel": tunnel_info(),
    }


@router.get("/overview")
async def overview() -> Dict[str, Any]:
    return await asyncio.to_thread(_build_overview)


@router.get("/requests")
async def requests_feed(limit: int = 40, minutes: int = 30) -> Dict[str, Any]:
    return {
        "events": recent_events(max(1, min(200, limit))),
        "series": request_series(max(5, min(180, minutes))),
        "window": window_totals(60),
    }


@router.post("/ip/rotate")
async def rotate_ip(request: Request) -> Dict[str, Any]:
    """Ask warp-minimal for a fresh egress exit (one exit, then whole pool)."""
    payload: Dict[str, Any] = {}
    try:
        raw = await request.json()
        if isinstance(raw, dict):
            payload = raw
    except Exception:  # noqa: BLE001 - empty body is fine
        pass

    exit_id: Optional[str] = payload.get("exit") if payload.get("exit") else None
    before = await asyncio.to_thread(warp_admin.snapshot)

    ok, reason = await asyncio.to_thread(warp_admin.rotate_exit, exit_id)
    if not ok and reason in {"no_standby", "http_409", "http_404"}:
        ok, reason = await asyncio.to_thread(warp_admin.recycle_pool)

    if ok:
        # Enrollment takes a couple of seconds; give the pool time to come back.
        await asyncio.sleep(3)

    after = await asyncio.to_thread(warp_admin.snapshot)
    old_ip = (before.get("ips") or [""])[0]
    new_ip = (after.get("ips") or [""])[0]
    if ok:
        record_rotation(old_ip, new_ip, "dashboard")

    return {
        "ok": ok,
        "reason": reason,
        "old_ip": old_ip,
        "new_ip": new_ip,
        "changed": bool(ok and old_ip != new_ip),
        "before": before,
        "after": after,
        "rotations": count_rotations(),
    }


@router.get("/keys")
async def get_keys() -> Dict[str, Any]:
    return {"keys": list_keys()}


@router.post("/keys", status_code=201)
async def generate_key(request: Request) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    try:
        raw = await request.json()
        if isinstance(raw, dict):
            payload = raw
    except Exception:  # noqa: BLE001
        pass
    label = str(payload.get("label") or "dashboard")
    try:
        created = create_key(label)
    except Exception as exc:  # noqa: BLE001
        log.exception("Could not create API key")
        raise HTTPException(status_code=500, detail=f"Could not create key: {exc}") from exc
    return {"key": created, "keys": list_keys()}


@router.delete("/keys/{key_id}")
async def delete_key(key_id: str) -> Dict[str, Any]:
    if not revoke_key(key_id):
        raise HTTPException(status_code=404, detail="Unknown key")
    return {"ok": True, "keys": list_keys()}


@router.get("/tunnel")
async def tunnel() -> Dict[str, Any]:
    return tunnel_info()
