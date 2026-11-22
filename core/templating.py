"""Shared Jinja environment for the dashboard and login pages."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def render(
    request: Request,
    template: str,
    context: Dict[str, Any] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a page, always supplying the CSP nonce."""
    payload: Dict[str, Any] = dict(context or {})
    payload.setdefault("nonce", getattr(request.state, "csp_nonce", ""))
    return templates.TemplateResponse(
        request, template, payload, status_code=status_code
    )
