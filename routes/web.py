"""Browser-facing routes: password login, dashboard shell and logout."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.auth import client_ip
from core.config import IS_VERCEL
from core.session import (
    COOKIE_NAME,
    COOKIE_MAX_AGE,
    cookie_is_secure,
    load_session,
    login_guard,
    new_session,
    password_configured,
    sign_session,
    verify_password,
)
from core.templating import render

log = logging.getLogger("routes.web")
router = APIRouter()


def _safe_next(value: str) -> str:
    """Only allow same-site relative redirects (no open redirect)."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\r" in value or "\n" in value:
        return "/"
    return value


def _session_from_request(request: Request):
    session = getattr(request.state, "session", None)
    if session:
        return session
    return load_session(request.cookies.get(COOKIE_NAME, ""))


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = ""):
    target = _safe_next(next)
    if _session_from_request(request):
        return RedirectResponse(url=target, status_code=303)
    return render(
        request,
        "login.html",
        {
            "next": target,
            "error": None,
            "password_configured": password_configured(),
            "locked_for": 0,
        },
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    form = await request.form()
    password = str(form.get("password") or "")
    target = _safe_next(str(form.get("next") or "/"))
    identity = client_ip(request)

    wait = login_guard.retry_after(identity)
    if wait:
        return render(
            request,
            "login.html",
            {
                "next": target,
                "error": f"Too many failed attempts. Try again in {wait} seconds.",
                "password_configured": password_configured(),
                "locked_for": wait,
            },
            status_code=429,
        )

    if not password_configured():
        return render(
            request,
            "login.html",
            {
                "next": target,
                "error": "No dashboard password is configured on this server.",
                "password_configured": False,
                "locked_for": 0,
            },
            status_code=503,
        )

    if not verify_password(password):
        login_guard.fail(identity)
        log.warning("Failed dashboard login from %s", identity)
        return render(
            request,
            "login.html",
            {
                "next": target,
                "error": "Incorrect password. Try again.",
                "password_configured": True,
                "locked_for": 0,
            },
            status_code=401,
        )

    login_guard.reset(identity)
    data = new_session()
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        sign_session(data),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=cookie_is_secure(request),
        samesite="lax",
        path="/",
    )
    log.info("Dashboard login succeeded for %s", identity)
    return response


@router.get("/logout")
@router.post("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    session = _session_from_request(request)
    if session is None:
        target = request.url.path
        return RedirectResponse(url=f"/login?next={target}", status_code=303)
    return render(
        request,
        "dashboard.html",
        {
            "csrf": session.get("csrf", ""),
            "serverless": IS_VERCEL,
        },
    )
