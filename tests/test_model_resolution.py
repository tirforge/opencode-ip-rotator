import unittest
from unittest.mock import patch

from services.upstream import apply_reasoning_effort, resolve_model

FREE = [{"id": "big-pickle"}, {"id": "deepseek-v4.1-flash-free"}]


class ModelResolutionTests(unittest.TestCase):
    def test_exact_free_model_is_kept(self):
        with patch("services.upstream.get_discovered_models", return_value=FREE):
            self.assertEqual(
                resolve_model("deepseek-v4.1-flash-free"),
                ("deepseek-v4.1-flash-free", None),
            )

    def test_effort_suffix_is_split(self):
        with patch("services.upstream.get_discovered_models", return_value=FREE):
            self.assertEqual(resolve_model("big-pickle#high"), ("big-pickle", "high"))

    def test_alias_is_resolved(self):
        with (
            patch("services.upstream.get_discovered_models", return_value=FREE),
            patch("services.upstream.MODEL_ALIASES", {"gpt-4o": "big-pickle"}),
        ):
            self.assertEqual(resolve_model("gpt-4o"), ("big-pickle", None))

    def test_all_models_maps_unknown_ids(self):
        with (
            patch("services.upstream.get_discovered_models", return_value=FREE),
            patch("services.upstream.ALL_MODELS", True),
            patch("services.upstream.ALL_MODELS_TARGET", "deepseek-v4.1-flash-free"),
        ):
            self.assertEqual(
                resolve_model("claude-sonnet-4#max"),
                ("deepseek-v4.1-flash-free", "max"),
            )

    def test_unknown_model_falls_back_to_default(self):
        with (
            patch("services.upstream.get_discovered_models", return_value=FREE),
            patch("services.upstream.ALL_MODELS", False),
            patch("services.upstream.get_default_model", return_value="big-pickle"),
        ):
            self.assertEqual(resolve_model("gpt-5"), ("big-pickle", None))


class ReasoningEffortTests(unittest.TestCase):
    def test_default_effort_is_applied(self):
        payload = {"model": "deepseek-v4.1-flash-free"}
        with patch("services.upstream.DEFAULT_REASONING_EFFORT", "medium"):
            apply_reasoning_effort(payload, "chat_completions")
        self.assertEqual(payload["reasoning_effort"], "medium")

    def test_suffix_overrides_default(self):
        payload = {"model": "deepseek-v4.1-flash-free"}
        with patch("services.upstream.DEFAULT_REASONING_EFFORT", "medium"):
            apply_reasoning_effort(payload, "chat_completions", "high")
        self.assertEqual(payload["reasoning_effort"], "high")

    def test_client_effort_wins(self):
        payload = {"model": "x", "reasoning_effort": "low"}
        with patch("services.upstream.DEFAULT_REASONING_EFFORT", "medium"):
            apply_reasoning_effort(payload, "chat_completions", "high")
        self.assertEqual(payload["reasoning_effort"], "low")

    def test_anthropic_endpoint_is_untouched(self):
        payload = {"model": "x"}
        with patch("services.upstream.DEFAULT_REASONING_EFFORT", "medium"):
            apply_reasoning_effort(payload, "anthropic_messages")
        self.assertNotIn("reasoning_effort", payload)


if __name__ == "__main__":
    unittest.main()
