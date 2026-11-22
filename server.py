import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.auth import ApiKeyMiddleware
from core.config import (
    CORS_ALLOW_ORIGINS,
    IS_VERCEL,
    PROXY_IP_ALLOWLIST,
    PROXY_RATE_LIMIT,
    PROXY_RATE_WINDOW,
)
from core.db import init_db
from core.events import RequestLogMiddleware
from core.security import SecurityHeadersMiddleware
from routes.api import router as api_router
from routes.chat import router as chat_router
from routes.models import router as models_router
from routes.system import router as system_router
from routes.web import router as web_router
from services.discovery import refresh_models_once, start_discovery, stop_discovery
from services.proxy_pool import load_proxy_list

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
log = logging.getLogger("server")


@asynccontextmanager
async def lifespan(application: FastAPI):
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 - metrics must not block serving
        log.warning("Metrics database unavailable, continuing without it: %s", exc)
    load_proxy_list()

    if IS_VERCEL:
        # Serverless: no long-lived background thread. The catalog is fetched
        # once per instance at startup; `/v1/models` is served from memory.
        log.info("Running on Vercel; fetching model catalog once.")
        refresh_models_once(timeout=4)
    else:
        start_discovery()
    log.info("OpenCode proxy server started.")

    yield

    if not IS_VERCEL:
        stop_discovery()
    log.info("OpenCode proxy server stopped.")


app = FastAPI(title="OpenCode Vercel Proxy", lifespan=lifespan)

if CORS_ALLOW_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOW_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "x-api-key", "anthropic-version"],
    )

app.add_middleware(RequestLogMiddleware)
app.add_middleware(
    ApiKeyMiddleware,
    limit=PROXY_RATE_LIMIT,
    window=PROXY_RATE_WINDOW,
    allowlist=PROXY_IP_ALLOWLIST,
)
# Outermost: every response (including auth rejections) gets the hardening
# headers and a fresh CSP nonce.
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(web_router)
app.include_router(chat_router)
app.include_router(models_router)
app.include_router(system_router)
app.include_router(api_router)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
