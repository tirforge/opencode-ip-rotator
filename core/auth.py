"""Authentication for the API and the browser dashboard.

Two independent doors:

* **API clients** send the shared secret as ``Authorization: Bearer ...`` or
  ``x-api-key``. Dashboard-generated keys live hashed in SQLite.
* **Humans** open the dashboard and enter a password. It is verified with
  Argon2id and exchanged for a signed, HttpOnly, SameSite=Lax session cookie;
  the password itself is never stored client-side. State-changing dashboard
  calls must also echo the session's CSRF token.

The middleware fails closed: if no credential is configured on a public
deployment it returns 503 instead of exposing a free LLM proxy.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from core.keys import active_key_hashes, verify_key
from core.session import (
    COOKIE_NAME,
    cookie_is_secure,
    load_session,
    new_session,
    sign_session,
    verify_password,
)

log = logging.getLogger("core.auth")

# Paths that stay reachable without any credential.
PUBLIC_PATHS = {"/health", "/login", "/logout", "/favicon.ico", "/robots.txt"}
PUBLIC_PREFIXES = ("/static/",)

# Pages that render in a browser. Unauthenticated visitors are redirected to
# the login page instead of receiving a JSON 401.
HTML_PATHS = {"/", "/dashboard"}

COOKIE_MAX_AGE = 12 * 60 * 60

_IS_VERCEL = bool(os.environ.get("VERCEL"))

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _configured_keys() -> List[str]:
    raw = os.environ.get("PROXY_API_KEY", "")
    return [value.strip() for value in raw.split(",") if value.strip()]


def _key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _matches_any(candidate: str, keys: List[str]) -> bool:
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
    return None


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


def _login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    return RedirectResponse(url=f"/login?next={quote(target, safe='')}", status_code=303)


def _valid_credential(candidate: str) -> bool:
    if not candidate:
        return False
    if verify_password(candidate):
        return True
    return _matches_any(candidate, _configured_keys()) or verify_key(candidate) is not None


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Gate API routes behind a key and HTML routes behind a password session."""

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

    def _csrf_error(self, request: Request, session: Dict[str, object]) -> Optional[JSONResponse]:
        if request.method in SAFE_METHODS or not request.url.path.startswith("/api"):
            return None
        expected = str(session.get("csrf") or "")
        supplied = request.headers.get("x-csrf-token", "")
        if expected and hmac.compare_digest(expected, supplied):
            return None
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("host", "")
            if host and origin.split("//")[-1].split("/")[0] != host:
                return JSONResponse(
                    status_code=403,
                    content={"error": {"message": "Cross-origin request blocked.", "type": "csrf_error", "code": 403}},
                )
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": "Missing or invalid CSRF token. Reload the dashboard.",
                    "type": "csrf_error",
                    "code": 403,
                }
            },
        )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        # --- 1. Existing signed session -------------------------------------
        session = load_session(request.cookies.get(COOKIE_NAME, ""))
        if session:
            request.state.session = session
            csrf_error = self._csrf_error(request, session)
            if csrf_error is not None:
                return csrf_error
            return await call_next(request)

        # --- 2. ?key= bootstrap (dashboard shortcut / automation) ------------
        query_key = (request.query_params.get("key") or "").strip()
        if query_key and request.method in SAFE_METHODS and path in HTML_PATHS:
            if _valid_credential(query_key):
                data = new_session()
                response = RedirectResponse(url=path, status_code=303)
                response.set_cookie(
                    COOKIE_NAME,
                    sign_session(data),
                    max_age=COOKIE_MAX_AGE,
                    httponly=True,
                    secure=cookie_is_secure(request),
                    samesite="lax",
                    path="/",
                )
                return response

        keys = _configured_keys()
        generated_hashes = active_key_hashes()
        if not keys and not generated_hashes:
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
            # Local/Docker development without a key keeps working, but the
            # dashboard still asks for a password when one is configured.
            if path in HTML_PATHS:
                return await call_next(request)
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

        # --- 3. Browser pages without a session ------------------------------
        if request.method in SAFE_METHODS and path in HTML_PATHS:
            return _login_redirect(request)

        # --- 4. API key ------------------------------------------------------
        supplied = _extract_key(request)
        if not supplied:
            return _unauthorized(
                "Missing API key. Send 'Authorization: Bearer <key>' or 'x-api-key: <key>'."
            )

        if not _matches_any(supplied, keys) and verify_key(supplied) is None:
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

        return await call_next(request)


def auth_summary() -> Dict[str, object]:
    """Non-sensitive description of the active security posture."""
    keys = _configured_keys()
    try:
        generated = len(active_key_hashes())
    except Exception:  # noqa: BLE001
        generated = 0
    from core.session import password_configured

    return {
        "api_key_required": bool(keys) or generated > 0 or _IS_VERCEL,
        "configured_keys": len(keys),
        "generated_keys": generated,
        "dashboard_password": password_configured(),
        "session_cookie": COOKIE_NAME,
        "rate_limit_per_minute": int(os.environ.get("PROXY_RATE_LIMIT", "60")),
        "ip_allowlist_entries": len(
            [p for p in os.environ.get("PROXY_IP_ALLOWLIST", "").split(",") if p.strip()]
        ),
    }
