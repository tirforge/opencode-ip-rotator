"""Quick Cloudflare tunnel URL published by the tunnel helper service."""

import logging
import threading
import time
from typing import Dict

from core.config import TUNNEL_URL_FILE

log = logging.getLogger("core.tunnel")

_lock = threading.Lock()
_cached: Dict[str, object] = {"url": "", "at": 0.0}
_TTL = 5.0


def tunnel_info() -> Dict[str, object]:
    """Return the current trycloudflare URL, if the helper wrote one."""
    now = time.time()
    with _lock:
        if now - float(_cached["at"]) < _TTL:
            return {"url": _cached["url"], "configured": bool(_cached["url"])}
        url = ""
        try:
            raw = TUNNEL_URL_FILE.read_text(encoding="utf-8").strip()
            if raw.startswith("https://") and "trycloudflare.com" in raw:
                url = raw.splitlines()[0].strip()
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not read tunnel URL: %s", exc)
        _cached["url"] = url
        _cached["at"] = now
        return {"url": url, "configured": bool(url)}
