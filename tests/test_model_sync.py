import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sync_oracle_models.py"
_spec = importlib.util.spec_from_file_location("sync_oracle_models", SCRIPT)
sync_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_mod)


def _model(model_id, efforts=None, price=0, model_type="chat", tools=True):
    return {
        "id": model_id,
        "type": model_type,
        "name": model_id.replace("-", " ").title(),
        "limit": {"context": 200000, "output": 32000},
        "capabilities": {
            "tools": {"supported": tools},
            "reasoning": {
                "supported": bool(efforts),
                "effort": efforts or [],
            },
        },
        "cost": {
            "text": {
                "input": {"price": price},
                "output": {"price": price},
            }
        },
    }


CATALOG = {
    "object": "list",
    "data": [
        _model("beam-free", ["low", "medium", "high", "xhigh", "max"]),
        _model("deepseek-v4.1-flash-free", ["low", "high", "max"]),
        _model("hy4-flash-free", ["none", "high"]),
        _model("big-pickle"),
        _model("paid-model", ["low", "high"], price=5),
        {"id": "embed-only", "type": "embedding", "cost": {"text": {"input": {"price": 0}, "output": {"price": 0}}}},
    ],
}

CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "model": "oracle/deepseek-v4.1-flash-free",
    "provider": {
        "openai": {
            "npm": "@ai-sdk/openai",
            "options": {"apiKey": "{env:OPENAI_API_KEY}"},
            "models": {"gpt-5": {"name": "GPT-5"}},
        },
        "oracle": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Oracle VPS (free, Tailscale)",
            "options": {"baseURL": "http://vps:8000/v1", "apiKey": "arsha"},
            "models": {"stale": {"name": "Stale"}},
        },
    },
}


class ModelSyncTests(unittest.TestCase):
    def test_only_free_chat_models_are_kept(self):
        models = sync_mod.build_models(CATALOG)
        self.assertEqual(
            set(models),
            {"beam-free", "deepseek-v4.1-flash-free", "hy4-flash-free", "big-pickle"},
        )
        self.assertNotIn("paid-model", models)
        self.assertNotIn("embed-only", models)

    def test_per_model_efforts_match_catalog(self):
        models = sync_mod.build_models(CATALOG)
        self.assertEqual(
            list(models["beam-free"]["variants"]), ["low", "medium", "high", "xhigh", "max"]
        )
        self.assertEqual(
            list(models["deepseek-v4.1-flash-free"]["variants"]), ["low", "high", "max"]
        )
        self.assertEqual(list(models["hy4-flash-free"]["variants"]), ["none", "high"])
        self.assertNotIn("variants", models["big-pickle"])
        self.assertTrue(models["beam-free"]["tool_call"])

    def test_rewrite_preserves_other_providers(self):
        text = json.dumps(CONFIG, indent=2)
        updated = sync_mod.rewrite_config(text, sync_mod.build_models(CATALOG))
        parsed = json.loads(updated)
        self.assertEqual(parsed["provider"]["openai"]["models"]["gpt-5"]["name"], "GPT-5")
        self.assertEqual(parsed["model"], "oracle/deepseek-v4.1-flash-free")
        self.assertEqual(
            parsed["provider"]["oracle"]["options"]["apiKey"], "arsha"
        )
        self.assertNotIn("stale", parsed["provider"]["oracle"]["models"])
        self.assertIn("xhigh", parsed["provider"]["oracle"]["models"]["beam-free"]["variants"])

    def test_sync_writes_config_and_backup(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "opencode.jsonc"
            config_path.write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
            with patch.object(sync_mod, "fetch_catalog", return_value=CATALOG):
                code = sync_mod.sync(config_path, "https://example.invalid", dry_run=False)
            self.assertEqual(code, 0)
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(len(parsed["provider"]["oracle"]["models"]), 4)
            backups = list(Path(directory).glob("opencode.jsonc.bak-modelsync-*"))
            self.assertEqual(len(backups), 1)

    def test_dry_run_does_not_write(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "opencode.jsonc"
            original = json.dumps(CONFIG, indent=2)
            config_path.write_text(original, encoding="utf-8")
            with patch.object(sync_mod, "fetch_catalog", return_value=CATALOG):
                code = sync_mod.sync(config_path, "https://example.invalid", dry_run=True)
            self.assertEqual(code, 0)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
