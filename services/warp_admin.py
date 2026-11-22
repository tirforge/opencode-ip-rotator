"""Client for the local warp-minimal pool API (127.0.0.1:8789).

warp-minimal owns the WARP exits; this module only reads its state and asks it
to rotate. Every call is short-timeout so a dead pool never stalls the
dashboard.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.config import WARP_ADMIN_URL

log = logging.getLogger("services.warp_admin")
TIMEOUT = 3.0


def _request(
    path: str, method: str = "GET", body: Optional[dict] = None, timeout: float = TIMEOUT
) -> Tuple[int, Any]:
    url = f"{(WARP_ADMIN_URL or '').rstrip('/')}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8") or "{}"
            try:
                return response.status, json.loads(raw)
            except ValueError:
                return response.status, {"raw": raw}
    except HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8") or ""
        except Exception:  # noqa: BLE001
            pass
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {"raw": raw}
        return exc.code, parsed
    except (URLError, OSError, TimeoutError) as exc:
        log.debug("warp-minimal request failed: %s", exc)
        return 0, {"error": str(exc)}


def pool_health() -> Dict[str, Any]:
    status, body = _request("/health")
    if status != 200 or not isinstance(body, dict):
        return {"ok": False, "active": 0, "healthy": 0, "cooling_down": 0}
    return body


def pool_exits(timeout: float = 6.0) -> List[Dict[str, Any]]:
    status, body = _request("/exits", timeout=timeout)
    if status == 200 and isinstance(body, list):
        return [exit_info for exit_info in body if isinstance(exit_info, dict)]
    return []


def snapshot() -> Dict[str, Any]:
    health = pool_health()
    exits = pool_exits()
    ips = [exit_info.get("ip") for exit_info in exits if exit_info.get("ip")]
    return {
        "online": bool(health.get("ok")),
        "active": int(health.get("active") or 0),
        "healthy": int(health.get("healthy") or 0),
        "cooling_down": int(health.get("cooling_down") or 0),
        "recycling": bool(health.get("recycling")),
        "pool_size": int(health.get("pool_size") or 0),
        "stack": health.get("stack") or "",
        "exits": exits,
        "ips": ips,
    }


def rotate_exit(exit_id: Optional[str] = None) -> Tuple[bool, str]:
    """Swap one exit for a fresh WARP registration."""
    body = {"exit": exit_id} if exit_id else {}
    status, payload = _request("/swap", method="POST", body=body, timeout=12.0)
    if status in (200, 202):
        return True, "swap"
    if status == 409:
        return False, "no_standby"
    return False, f"http_{status}"


def recycle_pool() -> Tuple[bool, str]:
    status, _ = _request("/recycle", method="POST", body={}, timeout=12.0)
    return status in (200, 202), f"http_{status}"


def wait_for_new_egress(
    old_ips: Set[str], timeout: float = 15.0, interval: float = 0.75
) -> Dict[str, Any]:
    """Poll until the pool serves from an IP that is not in ``old_ips``.

    Enrolment is asynchronous: right after a swap the drained exit may still be
    listed first and the replacement may still be starting. Retrying a request
    before the pool is ready would land on the same limited exit.
    """
    deadline = time.time() + max(0.0, timeout)
    snapshot_now = snapshot()
    while time.time() < deadline:
        serving = ""
        for exit_info in snapshot_now.get("exits") or []:
            if exit_info.get("state") in ("healthy", "in_use") and exit_info.get("ip"):
                serving = str(exit_info["ip"])
                break
        healthy = int(snapshot_now.get("healthy") or 0)
        if healthy > 0 and serving and serving not in old_ips:
            return snapshot_now
        time.sleep(interval)
        snapshot_now = snapshot()
    return snapshot_now
