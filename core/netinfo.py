"""Best-effort egress IP lookup for the dashboard.

Cloudflare WARP is not available on Vercel, so this only reports the public IP
the function happens to egress from. Results are cached briefly to avoid an
outbound call on every dashboard refresh.
"""

import json
import logging
import threading
import time
from typing import Any, Dict
from urllib.request import Request, urlopen

log = logging.getLogger("core.netinfo")

IP_ENDPOINT = "https://api.ipify.org?format=json"
GEO_ENDPOINT = "https://ipinfo.io/{ip}/json"

_cache_lock = threading.Lock()
_cached: Dict[str, Any] = {"ip": "Unknown", "location": {}, "at": 0.0}
_CACHE_TTL = 60.0


def _fetch_json(url: str, timeout: float = 4.0) -> Dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "opencode-vercel-proxy/1.0"})
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def _lookup() -> Dict[str, Any]:
    try:
        ip = str(_fetch_json(IP_ENDPOINT).get("ip") or "Unknown")
    except Exception as exc:  # noqa: BLE001 - dashboard must never fail on this
        log.debug("Public IP lookup failed: %s", exc)
        return {"ip": "Unknown", "location": {}}

    location: Dict[str, Any] = {}
    if ip != "Unknown":
        try:
            data = _fetch_json(GEO_ENDPOINT.format(ip=ip))
            location = {
                "country": data.get("country", ""),
                "city": data.get("city", ""),
                "region": data.get("region", ""),
            }
        except Exception as exc:  # noqa: BLE001
            log.debug("IP geolocation failed: %s", exc)
    return {"ip": ip, "location": location}


def get_public_ip() -> str:
    return get_ip_info()["ip"]


def get_ip_location(ip: str) -> Dict[str, Any]:
    info = get_ip_info()
    return info["location"] if info["ip"] == ip else {}


def get_ip_info(force: bool = False) -> Dict[str, Any]:
    global _cached
    now = time.time()
    with _cache_lock:
        if not force and now - _cached["at"] < _CACHE_TTL:
            return _cached
        fresh = _lookup()
        _cached = {**fresh, "at": now}
        return _cached
