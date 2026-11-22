import logging
import time
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from core.auth import auth_summary
from core.config import IS_VERCEL
from core.db import get_recent_ips, load_all_usage
from core.metrics import STARTUP_TIME, metrics
from core.netinfo import get_ip_info
from services.discovery import get_discovered_models

log = logging.getLogger("routes.system")
router = APIRouter()

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


@router.get("/health")
@router.get("/status")
async def health_check():
    models = get_discovered_models()
    return {
        "status": "healthy",
        "timestamp": int(time.time()),
        "uptime_seconds": int(time.time() - STARTUP_TIME),
        "discovered_models_count": len(models),
        "serverless": IS_VERCEL,
    }


@router.get("/metrics")
async def get_metrics(request: Request):
    accept = request.headers.get("accept", "")
    if "text/plain" in accept or "openmetrics" in accept:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    info = get_ip_info()
    models = get_discovered_models()
    return {
        "total_requests": metrics["total_requests"],
        "successful_requests": metrics["successful_requests"],
        "rate_limited_requests": metrics["rate_limited_requests"],
        "failed_requests": metrics["failed_requests"],
        "rotation_count": 0,
        "verified_public_ip": info["ip"],
        "location": info["location"],
        "uptime_seconds": int(time.time() - STARTUP_TIME),
        "discovered_models": [m["id"] for m in models],
        "serverless": IS_VERCEL,
        "security": auth_summary(),
    }


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"metrics": metrics, "serverless": IS_VERCEL},
    )


@router.get("/dashboard/stats")
async def dashboard_stats():
    info = get_ip_info()
    models = get_discovered_models()
    return {
        "verified_ip": info["ip"],
        "location": info["location"],
        "total_requests": metrics["total_requests"],
        "successful_requests": metrics["successful_requests"],
        "rate_limited_requests": metrics["rate_limited_requests"],
        # WARP is not available on serverless; the value stays 0.
        "rotation_count": 0,
        "rotator_status": "unavailable" if IS_VERCEL else "online",
        "serverless": IS_VERCEL,
        "uptime_seconds": int(time.time() - STARTUP_TIME),
        "active_models_count": len(models),
        "models": models,
        "model_usage": load_all_usage(),
        "recent_ips": get_recent_ips(limit=10),
        "security": auth_summary(),
    }
