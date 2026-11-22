"""API-key authentication and request throttling.

The public deployment sits on the open internet, so every proxy endpoint is
gated behind a shared secret. Clients send it as ``Authorization: Bearer ...``
(OpenAI style) or ``x-api-key`` (Anthropic style). Browsers can open the
dashboard with ``?key=<secret>`` once; the server then hands back a short-lived
HttpOnly cookie so the dashboard's own fetches keep working.

Design notes for Vercel:
- State is per function instance. Throttling is best-effort, not a distributed
  quota. That is still enough to stop casual abuse and runaway loops.
- If no key is configured on Vercel the service fails closed (503) instead of
  accidentally exposing a free LLM proxy to the internet.
"""

import hashlib
import hmac
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

# Paths that stay reachable without a key.
PUBLIC_PATHS = {"/health"}

# Routes served to the browser dashboard after a key has been exchanged for a
# cookie (the key itself is never stored in the cookie).
DASHBOARD_PATHS = ("/dashboard", "/metrics")

COOKIE_NAME = "oc_proxy_session"
COOKIE_MAX_AGE = 60 * 60 * 12  # 12 hours

_IS_VERCEL = bool(os.environ.get("VERCEL"))


def _configured_keys() -> List[str]:
    raw = os.environ.get("PROXY_API_KEY", "")
    return [value.strip() for value in raw.split(",") if value.strip()]


def _key_fingerprint(api_key: str) -> str:
    """Stable, non-reversible identifier used for the dashboard cookie."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _matches_any(candidate: str, keys: List[str]) -> bool:
    # Compare against every key with a constant-time check so timing does not
    # reveal how much of the secret was correct.
    ok = False
    for key in keys:
        ok |= hmac.compare_digest(candidate, key)
    return ok


def _extract_key(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        token = auth[7:].strip()
        if token:
            return token

    for header in ("x-api-key", "api-key"):
        value = request.headers.get(header)
        if value and value.strip():
            return value.strip()

    query_key = request.query_params.get("key")
    if query_key and request.method in ("GET", "HEAD"):
        return query_key.strip()

    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        # Cookie holds a fingerprint, not the key; report it as pre-verified.
        return f"cookie:{cookie}"
    return None


def _valid_session_cookie(cookie_value: str, keys: List[str]) -> bool:
    return any(hmac.compare_digest(cookie_value, _key_fingerprint(k)) for k in keys)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """Fixed-window counter keyed by API key + client IP."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = max(1, limit)
        self.window = max(1, window_seconds)
        self._hits: Dict[str, Tuple[int, float]] = {}
        self._lock = threading.Lock()

    def allow(self, identity: str) -> Tuple[bool, int]:
        now = time.time()
        window_start = now - (now % self.window)
        with self._lock:
            if len(self._hits) > 4096:
                self._hits = {
                    k: v for k, v in self._hits.items() if v[1] >= window_start
                }
            count, started = self._hits.get(identity, (0, window_start))
            if started != window_start:
                count, started = 0, window_start
            count += 1
            self._hits[identity] = (count, started)
        return count <= self.limit, max(1, int(started + self.window - now))


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": {"message": message, "type": "authentication_error", "code": 401}},
        headers={"WWW-Authenticate": 'Bearer realm="opencode-proxy"'},
    )


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Gate every non-public route behind a key and a per-client rate limit."""

    def __init__(self, app, limit: int, window: int, allowlist: List[str]) -> None:
        super().__init__(app)
        self.limiter = RateLimiter(limit, window)
        self.allowlist = allowlist

    def _active_allowlist(self) -> List[str]:
        configured = [
            entry.strip()
            for entry in os.environ.get("PROXY_IP_ALLOWLIST", "").split(",")
            if entry.strip()
        ]
        return configured or self.allowlist

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS:
            return await call_next(request)

        keys = _configured_keys()
        if not keys:
            if _IS_VERCEL:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "message": "Server is not configured: PROXY_API_KEY is missing.",
                            "type": "not_configured",
                            "code": 503,
                        }
                    },
                )
            # Local/Docker development without a key keeps working.
            return await call_next(request)

        if self._active_allowlist() and client_ip(request) not in self._active_allowlist():
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "message": "This client IP is not allowed.",
                        "type": "ip_not_allowed",
                        "code": 403,
                    }
                },
            )

        supplied = _extract_key(request)
        if not supplied:
            return _unauthorized(
                "Missing API key. Send 'Authorization: Bearer <key>' or 'x-api-key: <key>'."
            )

        if supplied.startswith("cookie:"):
            if not _valid_session_cookie(supplied[7:], keys):
                return _unauthorized("Dashboard session expired. Re-open it with ?key=<key>.")
            return await call_next(request)

        if not _matches_any(supplied, keys):
            return _unauthorized("Invalid API key.")

        allowed, retry_after = self.limiter.allow(
            f"{_key_fingerprint(supplied)}:{client_ip(request)}"
        )
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": "Rate limit exceeded. Slow down.",
                        "type": "rate_limit_error",
                        "code": 429,
                    }
                },
                headers={"Retry-After": str(retry_after)},
            )

        wants_cookie = bool(request.query_params.get("key")) and path.startswith(
            DASHBOARD_PATHS
        )

        if wants_cookie and path == "/dashboard" and request.method == "GET":
            # Strip the secret from the visible URL, but keep the session.
            response = RedirectResponse(url="/dashboard", status_code=302)
        else:
            response = await call_next(request)

        if wants_cookie:
            response.set_cookie(
                COOKIE_NAME,
                _key_fingerprint(supplied),
                max_age=COOKIE_MAX_AGE,
                httponly=True,
                secure=request.url.scheme == "https",
                samesite="strict",
                path="/",
            )
        return response


def auth_summary() -> Dict[str, object]:
    """Non-sensitive description of the active security posture."""
    keys = _configured_keys()
    return {
        "api_key_required": bool(keys) or _IS_VERCEL,
        "configured_keys": len(keys),
        "rate_limit_per_minute": int(os.environ.get("PROXY_RATE_LIMIT", "60")),
        "ip_allowlist_entries": len(
            [p for p in os.environ.get("PROXY_IP_ALLOWLIST", "").split(",") if p.strip()]
        ),
    }
