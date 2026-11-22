import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from services import proxy_pool


class WarpPoolRotationTests(unittest.TestCase):
    def setUp(self):
        proxy_pool._warp_index = 0
        proxy_pool._proxy_pool = []

    def test_rotation_advances_through_pool(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "warp_pool_index"
            with (
                patch.object(proxy_pool, "WARP_POOL", ["http://127.0.0.1:18001", "http://127.0.0.1:18002"]),
                patch.object(proxy_pool, "WARP_POOL_STATE_FILE", state),
                patch.object(proxy_pool, "_proxy_reachable", return_value=True),
            ):
                self.assertEqual(proxy_pool.current_warp_proxy(), "http://127.0.0.1:18001")
                self.assertEqual(proxy_pool.rotate_warp_pool(), "http://127.0.0.1:18002")
                self.assertEqual(proxy_pool.rotate_warp_pool(), "http://127.0.0.1:18001")
                self.assertEqual(state.read_text(encoding="utf-8"), "0")

    def test_dead_exit_is_skipped(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "warp_pool_index"
            pool = ["http://127.0.0.1:18001", "http://127.0.0.1:18002", "http://127.0.0.1:18003"]

            def reachable(url):
                return not url.endswith("18002")

            with (
                patch.object(proxy_pool, "WARP_POOL", pool),
                patch.object(proxy_pool, "WARP_POOL_STATE_FILE", state),
                patch.object(proxy_pool, "_proxy_reachable", side_effect=reachable),
            ):
                self.assertEqual(proxy_pool.rotate_warp_pool(), "http://127.0.0.1:18003")

    def test_persisted_index_survives_restart(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "warp_pool_index"
            state.write_text("2", encoding="utf-8")
            proxy_pool._warp_index = None
            with (
                patch.object(proxy_pool, "WARP_POOL", ["a:1", "b:2", "c:3"]),
                patch.object(proxy_pool, "WARP_POOL_STATE_FILE", state),
            ):
                self.assertEqual(proxy_pool.current_warp_proxy(), "c:3")

    def test_outbound_proxy_is_sticky_between_requests(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "warp_pool_index"
            with (
                patch.object(proxy_pool, "WARP_POOL", ["http://127.0.0.1:18001", "http://127.0.0.1:18002"]),
                patch.object(proxy_pool, "WARP_POOL_STATE_FILE", state),
                patch.object(proxy_pool, "PROXY_LIST_FILE", Path(directory) / "none.txt"),
            ):
                first = proxy_pool.get_next_outbound_proxy()
                second = proxy_pool.get_next_outbound_proxy()
                self.assertEqual(first, {"http": "http://127.0.0.1:18001", "https": "http://127.0.0.1:18001"})
                self.assertEqual(second, first)

    def test_no_pool_returns_none(self):
        with patch.object(proxy_pool, "WARP_POOL", []):
            self.assertIsNone(proxy_pool.rotate_warp_pool())
            self.assertIsNone(proxy_pool.current_warp_proxy())


if __name__ == "__main__":
    unittest.main()
