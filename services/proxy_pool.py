import logging
import os
from pathlib import Path
import socket
import threading
from typing import Dict, List, Optional
from urllib.parse import urlparse

from core.config import IS_VERCEL, PROXY_LIST_FILE, WARP_PROXY_URL

log = logging.getLogger("services.proxy_pool")
_proxy_pool: List[str] = []
_proxy_index = 0
_proxy_lock = threading.Lock()
_last_proxy_mtime: float = 0.0


def _normalize_proxy(line: str) -> str:
    line = line.strip()
    if line and "://" not in line:
        return f"http://{line}"
    return line


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
    try:
        parsed = urlparse(WARP_PROXY_URL)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 40000
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False


def get_next_outbound_proxy() -> Optional[Dict[str, str]]:
    global _proxy_index
    load_proxy_list()
    with _proxy_lock:
        if _proxy_pool:
            proxy_url = _proxy_pool[_proxy_index % len(_proxy_pool)]
            _proxy_index += 1
            return {"http": proxy_url, "https": proxy_url}

    custom_proxy = os.environ.get("CUSTOM_OUTBOUND_PROXY", "").strip()
    if custom_proxy:
        return {"http": custom_proxy, "https": custom_proxy}

    if not IS_VERCEL and _warp_proxy_available():
        return {"http": WARP_PROXY_URL, "https": WARP_PROXY_URL}

    return None
