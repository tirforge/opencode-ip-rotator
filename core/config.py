import json
import os
from pathlib import Path
from typing import Dict

# Upstream target URLs
OPENCODE_ZEN_TARGET_BASE = os.environ.get(
    "OPENCODE_ZEN_TARGET_BASE", "https://opencode.ai/inference/openai/v1"
)
CHAT_COMPLETIONS_URL = f"{OPENCODE_ZEN_TARGET_BASE}/chat/completions"
RESPONSES_URL = f"{OPENCODE_ZEN_TARGET_BASE}/responses"
ANTHROPIC_MESSAGES_URL = os.environ.get(
    "OPENCODE_ANTHROPIC_URL", "https://opencode.ai/inference/anthropic/v1/messages"
)
MODELS_URL = os.environ.get(
    "OPENCODE_MODELS_URL", "https://opencode.ai/inference/v1/models"
)

# OpenSSL / Hyper compliant ciphers for OpenCode compatibility
TLS12_CIPHERS = (
    "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
    "ECDHE-ECDSA-AES128-SHA:ECDHE-RSA-AES128-SHA:"
    "ECDHE-ECDSA-AES256-SHA:ECDHE-RSA-AES256-SHA:"
    "AES128-GCM-SHA256:AES256-GCM-SHA384:AES128-SHA:AES256-SHA"
)

# Operational settings
# Vercel Functions have a read-only deployment filesystem; only /tmp is writable
# and it is ephemeral (per instance, wiped on cold start).
IS_VERCEL = bool(os.environ.get("VERCEL"))
DEFAULT_DATA_DIR = (
    Path("/app/data")
    if Path("/.dockerenv").exists()
    else Path("/tmp/opencode-proxy")
    if IS_VERCEL
    else Path(__file__).resolve().parent.parent / "data"
)
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "big-pickle")
DEFAULT_MODEL_OVERRIDE = os.environ.get("OPENCODE_DEFAULT_MODEL", "").strip()
MODEL_DISCOVERY_INTERVAL = int(os.environ.get("MODEL_DISCOVERY_INTERVAL", "300"))
STREAM_TIMEOUT = int(os.environ.get("STREAM_TIMEOUT", "120"))

# "Any model" mode: accept arbitrary model ids (gpt-*, claude-*, ...) and map
# unknown ones to a free target instead of failing. Unknown ids already fall
# back to the default free model; enabling this makes that explicit and lets
# you pick the target.
ALL_MODELS = os.environ.get("ALL_MODELS", "0").strip().lower() in {"1", "true", "yes", "on"}
ALL_MODELS_TARGET = os.environ.get("ALL_MODELS_TARGET", "").strip()
# Applied when the client does not send reasoning_effort and the model id has
# no "#effort" suffix. Empty string disables the default.
DEFAULT_REASONING_EFFORT = os.environ.get("DEFAULT_REASONING_EFFORT", "").strip().lower()
# Local Cloudflare WARP proxy (warp-cli mode proxy). Used for outbound rotation.
WARP_PROXY_URL = os.environ.get("WARP_PROXY_URL", "http://127.0.0.1:40000").strip()


def _load_model_aliases() -> Dict[str, str]:
    raw = os.environ.get("MODEL_ALIASES", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items() if str(v).strip()}


MODEL_ALIASES = _load_model_aliases()

WARP_ROTATOR_URL = os.environ.get("WARP_ROTATOR_URL", "http://warp-rotator:8001")
METRICS_DB_PATH = Path(
    os.environ.get("METRICS_DB_PATH", str(DEFAULT_DATA_DIR / "metrics.db"))
)
PROXY_LIST_FILE = Path(
    os.environ.get("PROXY_LIST_FILE", str(DEFAULT_DATA_DIR / "proxies.txt"))
)
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",")
    if origin.strip()
]

# Security / abuse protection
PROXY_RATE_LIMIT = int(os.environ.get("PROXY_RATE_LIMIT", "60"))
PROXY_RATE_WINDOW = int(os.environ.get("PROXY_RATE_WINDOW", "60"))
PROXY_IP_ALLOWLIST = [
    entry.strip()
    for entry in os.environ.get("PROXY_IP_ALLOWLIST", "").split(",")
    if entry.strip()
]

# Safe headers for upstream logging
SAFE_UPSTREAM_HEADERS = {
    "content-type",
    "retry-after",
    "x-request-id",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "cf-ray",
}
SENSITIVE_LOG_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "token",
    "password",
    "secret",
}
