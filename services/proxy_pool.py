import logging
import os
import time
from pathlib import Path
import socket
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

from core.config import (
    IS_VERCEL,
    PROXY_LIST_FILE,
    WARP_POOL,
    WARP_POOL_STATE_FILE,
    WARP_PROXY_URL,
)

log = logging.getLogger("services.proxy_pool")
_proxy_pool: List[str] = []
_proxy_index = 0
_proxy_lock = threading.Lock()
_last_proxy_mtime: float = 0.0
_warp_index: Optional[int] = None

# --- warp-minimal exit pinning (round-robin over the pool's exits) ----------
# warp-minimal hands every flow the first healthy exit unless the request pins
# one via ``Proxy-Authorization: Basic <exit-id>:``. Spreading requests across
# all exits multiplies the usable free quota instead of sharing one window.
_EXIT_ROUTING = os.environ.get("WARP_EXIT_ROUTING", "auto").strip().lower()
_EXIT_CACHE_TTL = float(os.environ.get("WARP_EXIT_CACHE_TTL", "10"))
_EXIT_FAILURE_TTL = float(os.environ.get("WARP_EXIT_FAILURE_TTL", "20"))
_EXIT_COOLDOWN_SECONDS = float(os.environ.get("WARP_EXIT_COOLDOWN", "900"))
_exit_index = 0
_exit_cache: List[Dict[str, Any]] = []
_exit_cache_at = 0.0
_exit_refreshing = False
_exit_cooldowns: Dict[str, float] = {}


class ExitProxyDict(Dict[str, str]):
    """Proxy mapping that remembers which WARP exit it is pinned to."""

    def __init__(self, mapping: Dict[str, str], exit_id: Optional[str] = None):
        super().__init__(mapping)
        self.exit_id = exit_id


def exit_id_for(proxies: Optional[Dict[str, str]]) -> Optional[str]:
    """The WARP exit a proxy mapping is pinned to, if any."""
    return getattr(proxies, "exit_id", None)


def mark_exit_rate_limited(exit_id: Optional[str], seconds: Optional[float] = None) -> None:
    """Stop routing new requests to an exit that just exhausted its quota."""
    if not exit_id:
        return
    now = time.time()
    window = _EXIT_COOLDOWN_SECONDS if seconds is None else max(0.0, seconds)
    with _proxy_lock:
        _exit_cooldowns[exit_id] = now + window
        for stale in [key for key, until in _exit_cooldowns.items() if until <= now]:
            _exit_cooldowns.pop(stale, None)
    log.info(
        "WARP exit %s rate-limited; skipping it for %ds", exit_id, int(window)
    )


def _refresh_exit_cache(now: float) -> None:
    """Fetch exit facts off the event loop; failures back off via the TTL."""
    global _exit_cache, _exit_cache_at, _exit_refreshing
    try:
        from services import warp_admin

        exits = warp_admin.pool_exits(timeout=2.0)
    except Exception as exc:  # noqa: BLE001 - routing must never break a request
        log.debug("Could not read warp-minimal exits: %s", exc)
        exits = []
    with _proxy_lock:
        _exit_cache = [item for item in exits if isinstance(item, dict)]
        _exit_cache_at = now
        _exit_refreshing = False


def _warp_exit_list() -> List[Dict[str, Any]]:
    """Cached warp-minimal exit facts, refreshed in the background.

    Never blocks a request on the admin API: a stale list is returned while a
    refresh thread runs, and a failed refresh backs off for a longer TTL.
    """
    global _exit_refreshing
    if _EXIT_ROUTING == "off" or not WARP_PROXY_URL:
        return []
    now = time.time()
    with _proxy_lock:
        cached = list(_exit_cache)
        ttl = _EXIT_CACHE_TTL if cached else _EXIT_FAILURE_TTL
        fresh = bool(_exit_cache_at) and now - _exit_cache_at < ttl
        if fresh:
            return cached
        if _exit_refreshing:
            return cached
        _exit_refreshing = True
    threading.Thread(target=_refresh_exit_cache, args=(now,), daemon=True).start()
    return cached


def _eligible_exits() -> List[Dict[str, Any]]:
    now = time.time()
    with _proxy_lock:
        cooled = {key for key, until in _exit_cooldowns.items() if until > now}
    eligible = []
    for item in _warp_exit_list():
        exit_id = str(item.get("id") or "")
        if not exit_id or item.get("state") not in ("healthy", "in_use"):
            continue
        if exit_id in cooled:
            continue
        eligible.append(item)
    return eligible


def _pinned_warp_proxy() -> Optional[ExitProxyDict]:
    """Round-robin one warp-minimal exit so requests spread across the pool."""
    global _exit_index
    if _EXIT_ROUTING == "off":
        return None
    exits = _eligible_exits()
    if len(exits) < 2:
        # A single usable exit is exactly what sticky acquisition gives us.
        return None
    parsed = urlparse(
        WARP_PROXY_URL if "://" in WARP_PROXY_URL else f"http://{WARP_PROXY_URL}"
    )
    if not parsed.hostname:
        return None
    with _proxy_lock:
        chosen = exits[_exit_index % len(exits)]
        _exit_index = (_exit_index + 1) % len(exits)
    exit_id = str(chosen.get("id"))
    netloc = f"{exit_id}:@{parsed.hostname}:{parsed.port or 80}"
    url = urlunparse((parsed.scheme or "http", netloc, "", "", "", ""))
    return ExitProxyDict({"http": url, "https": url}, exit_id=exit_id)



def _normalize_proxy(line: str) -> str:
    line = line.strip()
    if line and "://" not in line:
        return f"http://{line}"
    return line


def _proxy_reachable(proxy_url: str, timeout: float = 0.4) -> bool:
    """Cheap liveness probe for a local proxy listener."""
    try:
        parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, port)) == 0
    except Exception:
        return False


def load_proxy_list(force: bool = False) -> None:
    global _proxy_pool, _last_proxy_mtime
    with _proxy_lock:
        proxies = []
        files_to_check = [PROXY_LIST_FILE]
        root_file = Path(__file__).resolve().parent.parent / "proxies.txt"
        if root_file != PROXY_LIST_FILE and root_file.exists():
            files_to_check.append(root_file)

        for p_file in files_to_check:
            if p_file.exists():
                try:
                    mtime = p_file.stat().st_mtime
                    if not force and mtime == _last_proxy_mtime and _proxy_pool:
                        return
                    _last_proxy_mtime = mtime
                    with open(p_file, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#"):
                                proxies.append(_normalize_proxy(line))
                except Exception as e:
                    log.error("Error reading proxy file %s: %s", p_file, e)

        env_proxies = os.environ.get("PROXY_LIST", "").strip()
        if env_proxies:
            for p in env_proxies.split(","):
                p = p.strip()
                if p:
                    proxies.append(_normalize_proxy(p))

        _proxy_pool = list(dict.fromkeys(proxies))
        if _proxy_pool:
            log.info("Loaded %d proxies into outbound pool.", len(_proxy_pool))


def _warp_proxy_available() -> bool:
    """True when `warp-cli mode proxy` is listening (default 127.0.0.1:40000)."""
    if not WARP_PROXY_URL:
        return False
    return _proxy_reachable(WARP_PROXY_URL, timeout=0.3)


def _read_warp_index() -> int:
    global _warp_index
    if _warp_index is not None:
        return _warp_index
    _warp_index = 0
    try:
        raw = WARP_POOL_STATE_FILE.read_text(encoding="utf-8").strip()
        if raw:
            _warp_index = int(raw) % len(WARP_POOL)
    except Exception:
        _warp_index = 0
    return _warp_index


def _write_warp_index(index: int) -> None:
    try:
        WARP_POOL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        WARP_POOL_STATE_FILE.write_text(str(index), encoding="utf-8")
    except Exception as exc:
        log.warning("Could not persist WARP pool index: %s", exc)


def current_warp_proxy() -> Optional[str]:
    """The WARP exit currently used for upstream traffic."""
    if not WARP_POOL:
        return None
    with _proxy_lock:
        return WARP_POOL[_read_warp_index() % len(WARP_POOL)]


def rotate_warp_pool() -> Optional[str]:
    """Advance to the next healthy WARP exit; returns its proxy URL.

    Every member is a separate WARP registration, so the new exit has a
    different egress IP by construction (no dependence on Cloudflare
    handing back a different address on reconnect).
    """
    global _warp_index
    if not WARP_POOL:
        return None
    with _proxy_lock:
        start = _read_warp_index()
        for offset in range(1, len(WARP_POOL) + 1):
            candidate = (start + offset) % len(WARP_POOL)
            if _proxy_reachable(WARP_POOL[candidate]):
                _warp_index = candidate
                _write_warp_index(candidate)
                return WARP_POOL[candidate]
        log.warning("No healthy WARP pool exit is reachable.")
        return None


def get_next_outbound_proxy() -> Optional[Dict[str, str]]:
    global _proxy_index
    load_proxy_list()
    with _proxy_lock:
        if _proxy_pool:
            proxy_url = _proxy_pool[_proxy_index % len(_proxy_pool)]
            _proxy_index += 1
            return {"http": proxy_url, "https": proxy_url}

    if WARP_POOL:
        # Sticky exit: the pool only advances when the upstream rate-limits us.
        proxy_url = current_warp_proxy()
        if proxy_url:
            return {"http": proxy_url, "https": proxy_url}

    custom_proxy = os.environ.get("CUSTOM_OUTBOUND_PROXY", "").strip()
    if custom_proxy:
        return {"http": custom_proxy, "https": custom_proxy}

    pinned = _pinned_warp_proxy()
    if pinned:
        return pinned

    if not IS_VERCEL and _warp_proxy_available():
        return {"http": WARP_PROXY_URL, "https": WARP_PROXY_URL}

    return None
