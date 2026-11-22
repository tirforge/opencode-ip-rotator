import asyncio
import logging

from fastapi import APIRouter

from core.config import IS_VERCEL
from services.discovery import ensure_models_fresh, get_discovered_models

log = logging.getLogger("routes.models")
router = APIRouter()


@router.get("/v1/models")
async def list_models():
    if IS_VERCEL:
        # Keep the catalog reasonably fresh without a background thread. The
        # first call after startup is cheap; a refresh only happens when the
        # in-memory copy is older than MODEL_DISCOVERY_INTERVAL.
        await asyncio.to_thread(ensure_models_fresh)
    models = get_discovered_models()
    return {
        "object": "list",
        "data": models,
    }
