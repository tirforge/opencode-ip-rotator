"""Hardening headers for every response.

A strict, nonce-based Content-Security-Policy is the important one: even if an
attacker manages to inject markup, the browser refuses to execute inline
scripts that do not carry the per-request nonce. The remaining headers are the
standard browser-side hardening set (clickjacking, MIME sniffing, referrer
leakage, feature permissions, HSTS on HTTPS).
"""

from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

CSP_TEMPLATE = (
    "default-src 'self'; "
    "base-uri 'none'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "script-src 'self' 'nonce-{nonce}'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "worker-src 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        nonce = secrets.token_urlsafe(18)
        request.state.csp_nonce = nonce
        response = await call_next(request)

        headers = response.headers
        headers.setdefault("Content-Security-Policy", CSP_TEMPLATE.format(nonce=nonce))
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault(
            "Permissions-Policy",
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
            "microphone=(), payment=(), usb=()",
        )
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        headers.setdefault("X-XSS-Protection", "0")

        if request.url.scheme == "https":
            headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )

        # Never let a browser cache an authenticated page.
        if request.url.path in ("/", "/dashboard", "/login"):
            headers.setdefault("Cache-Control", "no-store, max-age=0")
            headers.setdefault("Pragma", "no-cache")

        return response
