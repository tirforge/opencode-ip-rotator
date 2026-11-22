import unittest

from core import events
from services.upstream import (
    finalize_stream_tokens,
    new_stream_tracker,
    observe_stream_payload,
)


class TokenAccountingTests(unittest.TestCase):
    def setUp(self):
        events._events.clear()  # noqa: SLF001 - isolate each test

    def test_event_tokens_and_throughput(self):
        event_id = events.begin_event("/v1/chat/completions", "1.2.3.4")
        events.finish_event(
            event_id,
            status=200,
            duration_ms=1250.0,
            ok=True,
            model="deepseek-v4.1-flash-free",
            tokens_in=120,
            tokens_out=250,
            tps=52.5,
        )
        stats = events.token_stats()
        self.assertEqual(stats["tokens_in"], 120)
        self.assertEqual(stats["tokens_out"], 250)
        self.assertEqual(stats["avg_tps"], 52.5)
        self.assertEqual(stats["last_hour"]["tokens_out"], 250)

        latest = events.recent_events(1)[0]
        self.assertEqual(latest["model"], "deepseek-v4.1-flash-free")
        self.assertFalse(latest["pending"])

    def test_series_counts_tokens(self):
        event_id = events.begin_event("/v1/chat/completions")
        events.finish_event(event_id, status=200, ok=True, tokens_out=42, tps=21.0)
        series = events.request_series(5)
        self.assertEqual(sum(point["tokens"] for point in series), 42)
        self.assertTrue(any(point["tps"] > 0 for point in series))

    def test_chat_stream_usage_is_captured(self):
        tracker = new_stream_tracker()
        observe_stream_payload(
            {
                "choices": [{"delta": {"content": "hello world"}}],
            },
            "chat",
            tracker,
        )
        observe_stream_payload(
            {
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 7},
            },
            "chat",
            tracker,
        )
        tokens_in, tokens_out, tps = finalize_stream_tokens(tracker, 0.0, 2.0)
        self.assertEqual(tokens_in, 10)
        self.assertEqual(tokens_out, 7)
        self.assertIsNotNone(tps)
        self.assertGreater(tps, 0)

    def test_missing_usage_falls_back_to_character_estimate(self):
        tracker = new_stream_tracker()
        for _ in range(10):
            observe_stream_payload(
                {"choices": [{"delta": {"content": "abcd"}}]}, "chat", tracker
            )
        _in, out, _tps = finalize_stream_tokens(tracker, 0.0, 1.0)
        self.assertEqual(out, 10)  # 40 chars / 4

    def test_anthropic_usage_is_merged(self):
        tracker = new_stream_tracker()
        observe_stream_payload(
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 99, "output_tokens": 1}},
            },
            "messages",
            tracker,
        )
        observe_stream_payload(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
            "messages",
            tracker,
        )
        observe_stream_payload(
            {"type": "message_delta", "usage": {"output_tokens": 12}},
            "messages",
            tracker,
        )
        tokens_in, tokens_out, _tps = finalize_stream_tokens(tracker, 0.0, 1.0)
        self.assertEqual(tokens_in, 99)
        self.assertEqual(tokens_out, 12)


if __name__ == "__main__":
    unittest.main()
