"""Regenerate the OpenCode ``oracle`` provider models from the official catalog.

The catalog publishes per-model reasoning efforts (``minimal``/``low``/
``medium``/``high``/``xhigh``/``max``/``none``), context limits and tool
support. OpenCode needs that metadata in its static config, so this script
rewrites only the ``oracle`` provider object and leaves every other provider
untouched. Run it every six hours (see the Task Scheduler entry) so new free
models and effort levels show up automatically.

Usage:
    python scripts/sync_oracle_models.py [--config PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

CATALOG_URL = "https://opencode.ai/inference/v1/models"
DEFAULT_CONFIG = Path.home() / ".config" / "opencode" / "opencode.jsonc"
PROVIDER_KEY = "oracle"
DEFAULT_NPM = "@ai-sdk/openai-compatible"
DEFAULT_PROVIDER_NAME = "Oracle VPS (free, Tailscale)"
MIN_EXPECTED_MODELS = 3


def _zero_price(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0


def is_free_chat_model(model: Dict[str, Any]) -> bool:
    if model.get("type") not in (None, "chat"):
        return False
    cost = model.get("cost")
    if not isinstance(cost, dict):
        return False
    text = cost.get("text")
    if not isinstance(text, dict):
        return False
    input_cost = text.get("input")
    output_cost = text.get("output")
    if not isinstance(input_cost, dict) or not isinstance(output_cost, dict):
        return False
    return _zero_price(input_cost.get("price")) and _zero_price(output_cost.get("price"))


def _efforts(model: Dict[str, Any]) -> List[str]:
    reasoning = (model.get("capabilities") or {}).get("reasoning") or {}
    raw = reasoning.get("effort") or []
    if not isinstance(raw, list):
        return []
    return [str(value).strip().lower() for value in raw if str(value).strip()]


def build_models(payload: Any) -> Dict[str, Dict[str, Any]]:
    """Turn a catalog payload into OpenCode ``provider.models`` entries."""
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, list):
        return {}

    models: Dict[str, Dict[str, Any]] = {}
    for model in data:
        if not isinstance(model, dict) or not is_free_chat_model(model):
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id or model_id in models:
            continue

        capabilities = model.get("capabilities") or {}
        reasoning = capabilities.get("reasoning") or {}
        tools = capabilities.get("tools") or {}
        limit = model.get("limit") or {}

        entry: Dict[str, Any] = {
            "name": model.get("name") or model_id,
            "tool_call": bool(tools.get("supported")),
            "reasoning": bool(reasoning.get("supported")),
            "limit": {
                "context": int(limit.get("context") or 0),
                "output": int(limit.get("output") or 0),
            },
        }
        efforts = _efforts(model)
        if efforts:
            # Preserve catalog order (minimal -> max) and drop duplicates.
            seen: set = set()
            variants: Dict[str, Dict[str, str]] = {}
            for effort in efforts:
                if effort in seen:
                    continue
                seen.add(effort)
                variants[effort] = {"reasoningEffort": effort}
            # The proxy defaults to ``medium``, so keep that tier selectable even
            # when the catalog only advertises low/high (a middle tier exists).
            if "medium" not in variants and {"low", "high"} <= set(variants):
                ordered: Dict[str, Dict[str, str]] = {}
                for effort, value in variants.items():
                    ordered[effort] = value
                    if effort == "low":
                        ordered["medium"] = {"reasoningEffort": "medium"}
                variants = ordered
            entry["variants"] = variants
        models[model_id] = entry

    return dict(sorted(models.items()))


def fetch_catalog(url: str = CATALOG_URL, timeout: float = 30.0) -> Dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "opencode-oracle-model-sync/1.0",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _object_span(text: str, key: str, start: int = 0) -> Optional[Tuple[int, int]]:
    """Return the ``[start, end)`` span of the JSON object stored under ``key``."""
    match = re.search(r'"%s"\s*:\s*\{' % re.escape(key), text[start:])
    if not match:
        return None
    begin = start + match.end() - 1
    depth = 0
    in_string = False
    escaped = False
    for index in range(begin, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return begin, index + 1
    return None


def _provider_defaults(block: str) -> Dict[str, Any]:
    def grab(field: str, fallback: str) -> str:
        match = re.search(r'"%s"\s*:\s*"([^"]*)"' % re.escape(field), block)
        return match.group(1) if match else fallback

    base_url = grab("baseURL", "")
    api_key = grab("apiKey", "")
    if not base_url or not api_key:
        raise ValueError(
            "existing oracle provider is missing options.baseURL/options.apiKey"
        )
    return {
        "npm": grab("npm", DEFAULT_NPM),
        "name": grab("name", DEFAULT_PROVIDER_NAME),
        "options": {"baseURL": base_url, "apiKey": api_key},
    }


def render_provider(block: str, models: Dict[str, Dict[str, Any]], indent: int) -> str:
    provider = _provider_defaults(block)
    provider["models"] = models
    rendered = json.dumps(provider, indent=4, ensure_ascii=False)
    pad = " " * indent
    return rendered.replace("\n", "\n" + pad)


def rewrite_config(text: str, models: Dict[str, Dict[str, Any]]) -> str:
    oracle = _object_span(text, PROVIDER_KEY)
    if not oracle:
        raise ValueError(f'provider "{PROVIDER_KEY}" not found in config')

    start, end = oracle
    line_start = text.rfind("\n", 0, start) + 1
    indent = start - line_start
    # The value starts at the opening brace; align its closing brace with the
    # key that owns it.
    indent = max(0, indent - len('"' + PROVIDER_KEY + '": '))
    block = text[start:end]
    replacement = render_provider(block, models, indent)
    return text[:start] + replacement + text[end:]


def sync(config_path: Path, catalog_url: str, dry_run: bool = False) -> int:
    payload = fetch_catalog(catalog_url)
    models = build_models(payload)
    if len(models) < MIN_EXPECTED_MODELS:
        print(
            f"Refusing to update: catalog produced only {len(models)} free chat models",
            file=sys.stderr,
        )
        return 2

    original = config_path.read_text(encoding="utf-8")
    updated = rewrite_config(original, models)
    # Guard against a malformed rewrite before touching the real file.
    json.loads(updated)

    if updated == original:
        print(f"Up to date: {len(models)} free models, no changes.")
        return 0

    if dry_run:
        print(f"Would update {config_path} with {len(models)} free models.")
        print(", ".join(models))
        return 0

    backup = config_path.with_name(
        f"{config_path.name}.bak-modelsync-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    shutil.copy2(config_path, backup)
    temp = config_path.with_name(config_path.name + ".tmp")
    temp.write_text(updated, encoding="utf-8")
    os.replace(temp, config_path)

    print(f"Updated {config_path} with {len(models)} free models.")
    print(f"Backup: {backup}")
    for model_id, entry in models.items():
        variants = ", ".join((entry.get("variants") or {}).keys()) or "default effort"
        print(f"  {model_id}: {variants}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--url", default=CATALOG_URL)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not args.config.exists():
        print(f"Config not found: {args.config}", file=sys.stderr)
        return 2
    try:
        return sync(args.config, args.url, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 - scheduled task logs the reason
        print(f"Model sync failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
