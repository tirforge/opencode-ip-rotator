import time
import unittest
from unittest.mock import patch

from services import proxy_pool

EXITS = [
    {"id": "warp-aaa", "state": "healthy", "ip": "2a09:bac5:1::1"},
    {"id": "warp-bbb", "state": "in_use", "ip": "2a09:bac5:2::2"},
    {"id": "warp-ccc", "state": "draining", "ip": "2a09:bac5:3::3"},
    {"id": "warp-ddd", "state": "cooldown", "ip": "2a09:bac5:4::4"},
    {"id": "warp-eee", "state": "healthy", "ip": "2a09:bac5:5::5"},
]


class ExitRoutingTests(unittest.TestCase):
    def setUp(self):
        proxy_pool._exit_index = 0
        proxy_pool._exit_cooldowns.clear()
        proxy_pool._proxy_pool = []
        proxy_pool._exit_cache = []
        proxy_pool._exit_cache_at = 0.0
        proxy_pool._exit_refreshing = False

    def _next(self):
        with (
            patch.object(proxy_pool, "WARP_PROXY_URL", "http://127.0.0.1:8789"),
            patch.object(proxy_pool, "WARP_POOL", []),
            patch.object(proxy_pool, "_warp_exit_list", return_value=EXITS),
        ):
            return proxy_pool.get_next_outbound_proxy()

    def test_round_robin_pins_distinct_exits(self):
        first, second, third = self._next(), self._next(), self._next()
        self.assertEqual(proxy_pool.exit_id_for(first), "warp-aaa")
        self.assertEqual(proxy_pool.exit_id_for(second), "warp-bbb")
        self.assertEqual(proxy_pool.exit_id_for(third), "warp-eee")
        self.assertEqual(first["https"], "http://warp-aaa:@127.0.0.1:8789")
        self.assertEqual(second["https"], "http://warp-bbb:@127.0.0.1:8789")

    def test_unusable_exits_are_never_pinned(self):
        seen = set()
        for _ in range(9):
            seen.add(proxy_pool.exit_id_for(self._next()))
        self.assertEqual(seen, {"warp-aaa", "warp-bbb", "warp-eee"})

    def test_rate_limited_exit_is_skipped(self):
        proxy_pool.mark_exit_rate_limited("warp-aaa")
        seen = {proxy_pool.exit_id_for(self._next()) for _ in range(6)}
        self.assertEqual(seen, {"warp-bbb", "warp-eee"})

    def test_single_usable_exit_falls_back_to_sticky(self):
        proxy_pool.mark_exit_rate_limited("warp-aaa")
        with (
            patch.object(proxy_pool, "WARP_PROXY_URL", "http://127.0.0.1:8789"),
            patch.object(proxy_pool, "WARP_POOL", []),
            patch.object(proxy_pool, "_warp_exit_list", return_value=EXITS[:2]),
            patch.object(proxy_pool, "_warp_proxy_available", return_value=True),
        ):
            proxies = proxy_pool.get_next_outbound_proxy()
        self.assertEqual(proxies["https"], "http://127.0.0.1:8789")
        self.assertIsNone(proxy_pool.exit_id_for(proxies))

    def test_expired_cooldowns_are_pruned(self):
        proxy_pool.mark_exit_rate_limited("warp-aaa", seconds=-1)
        self.assertEqual(proxy_pool._exit_cooldowns, {})

    def test_no_exits_returns_nothing(self):
        with (
            patch.object(proxy_pool, "WARP_PROXY_URL", "http://127.0.0.1:8789"),
            patch.object(proxy_pool, "WARP_POOL", []),
            patch.object(proxy_pool, "_warp_exit_list", return_value=[]),
            patch.object(proxy_pool, "_warp_proxy_available", return_value=False),
        ):
            self.assertIsNone(proxy_pool.get_next_outbound_proxy())

    def test_routing_can_be_disabled(self):
        with (
            patch.object(proxy_pool, "_EXIT_ROUTING", "off"),
            patch.object(proxy_pool, "WARP_PROXY_URL", "http://127.0.0.1:8789"),
            patch.object(proxy_pool, "WARP_POOL", []),
            patch.object(proxy_pool, "_warp_proxy_available", return_value=True),
        ):
            proxies = proxy_pool.get_next_outbound_proxy()
        self.assertEqual(proxies["https"], "http://127.0.0.1:8789")
        self.assertIsNone(proxy_pool.exit_id_for(proxies))


if __name__ == "__main__":
    unittest.main()
