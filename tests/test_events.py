import time
import unittest

from core import events


class EventBufferTests(unittest.TestCase):
    def setUp(self):
        with events._lock:  # noqa: SLF001 - test isolation
            events._events.clear()  # noqa: SLF001

    def test_record_and_read_recent(self):
        events.record_event("/v1/chat/completions", 200, 42.5, "127.0.0.1")
        items = events.recent_events()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["endpoint"], "chat")
        self.assertEqual(item["status"], 200)
        self.assertTrue(item["ok"])
        self.assertEqual(item["duration_ms"], 42.5)

    def test_recent_is_newest_first(self):
        events.record_event("/v1/chat/completions", 200, 1, "a")
        events.record_event("/v1/messages", 500, 2, "b")
        items = events.recent_events()
        self.assertEqual(items[0]["endpoint"], "messages")
        self.assertFalse(items[0]["ok"])
        self.assertEqual(items[1]["endpoint"], "chat")

    def test_recent_limit(self):
        for _ in range(10):
            events.record_event("/v1/responses", 200, 1, "a")
        self.assertEqual(len(events.recent_events(limit=3)), 3)

    def test_series_buckets_by_minute(self):
        now = time.time()
        events.record_event("/v1/chat/completions", 200, 1, "a")
        series = events.request_series(5)
        self.assertEqual(len(series), 5)
        self.assertEqual(series[-1]["minute"], int(now // 60) * 60)
        self.assertEqual(sum(point["total"] for point in series), 1)
        self.assertEqual(series[-1]["ok"], 1)

    def test_series_records_errors(self):
        events.record_event("/v1/chat/completions", 502, 1, "a")
        series = events.request_series(3)
        self.assertEqual(series[-1]["error"], 1)
        self.assertEqual(series[-1]["ok"], 0)

    def test_window_totals(self):
        events.record_event("/v1/chat/completions", 200, 1, "a")
        events.record_event("/v1/chat/completions", 429, 1, "a")
        totals = events.window_totals(60)
        self.assertEqual(totals["requests"], 2)
        self.assertEqual(totals["ok"], 1)
        self.assertEqual(totals["errors"], 1)

    def test_window_totals_ignores_old_events(self):
        with events._lock:  # noqa: SLF001
            events._events.append(
                {
                    "at": time.time() - 7200,
                    "endpoint": "chat",
                    "path": "/v1/chat/completions",
                    "status": 200,
                    "duration_ms": 1,
                    "client": "old",
                    "ok": True,
                }
            )
        self.assertEqual(events.window_totals(60)["requests"], 0)


if __name__ == "__main__":
    unittest.main()
