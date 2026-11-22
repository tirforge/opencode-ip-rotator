import json
import time
import unittest
from unittest.mock import MagicMock, patch

import services.upstream as upstream


class WarpRotationRequestTests(unittest.TestCase):
    def setUp(self):
        upstream._last_rotation_at = 0.0
        pool = patch.object(upstream, "rotate_warp_pool", return_value=None)
        pool.start()
        self.addCleanup(pool.stop)
        admin = patch.object(upstream, "WARP_ADMIN_URL", "")
        admin.start()
        self.addCleanup(admin.stop)
        settle = patch.object(upstream, "ROTATION_SETTLE_SECONDS", 0.0)
        settle.start()
        self.addCleanup(settle.stop)

    def test_disabled_without_rotator_url(self):
        with patch.object(upstream, "WARP_ROTATOR_URL", ""):
            self.assertFalse(upstream.request_warp_rotation())

    def test_success_response_is_parsed(self):
        response = MagicMock()
        response.status = 200
        response.read.return_value = json.dumps(
            {"status": "success", "verified_ip": "203.0.113.7"}
        ).encode()
        response.__enter__.return_value = response
        response.__exit__.return_value = False

        with (
            patch.object(upstream, "WARP_ROTATOR_URL", "http://127.0.0.1:8001"),
            patch("urllib.request.urlopen", return_value=response) as urlopen,
        ):
            self.assertTrue(upstream.request_warp_rotation("test"))
        urlopen.assert_called_once()

    def test_failed_status_is_false(self):
        response = MagicMock()
        response.status = 200
        response.read.return_value = json.dumps({"status": "failed"}).encode()
        response.__enter__.return_value = response
        response.__exit__.return_value = False

        with (
            patch.object(upstream, "WARP_ROTATOR_URL", "http://127.0.0.1:8001"),
            patch("urllib.request.urlopen", return_value=response),
        ):
            self.assertFalse(upstream.request_warp_rotation("test"))

    def test_cooldown_skips_second_request(self):
        upstream._last_rotation_at = time.time()
        with (
            patch.object(upstream, "WARP_ROTATOR_URL", "http://127.0.0.1:8001"),
            patch("urllib.request.urlopen") as urlopen,
        ):
            self.assertFalse(upstream.request_warp_rotation("test"))
        urlopen.assert_not_called()

    def test_warp_minimal_swap_is_used(self):
        with (
            patch.object(upstream, "WARP_ADMIN_URL", "http://127.0.0.1:8789"),
            patch.object(upstream.warp_admin, "rotate_exit", return_value=(True, "swap")) as swap,
            patch.object(upstream, "record_rotation") as record,
            patch.object(upstream, "WARP_ROTATOR_URL", ""),
        ):
            self.assertTrue(upstream.request_warp_rotation("upstream 429"))
        swap.assert_called_once()
        record.assert_called_once()

    def test_warp_minimal_falls_back_to_recycle(self):
        with (
            patch.object(upstream, "WARP_ADMIN_URL", "http://127.0.0.1:8789"),
            patch.object(upstream.warp_admin, "rotate_exit", return_value=(False, "no_standby")),
            patch.object(upstream.warp_admin, "recycle_pool", return_value=(True, "http_200")) as recycle,
            patch.object(upstream, "record_rotation"),
            patch.object(upstream, "WARP_ROTATOR_URL", ""),
        ):
            self.assertTrue(upstream.request_warp_rotation("stream 429"))
        recycle.assert_called_once()

    def test_warp_minimal_failure_falls_through(self):
        with (
            patch.object(upstream, "WARP_ADMIN_URL", "http://127.0.0.1:8789"),
            patch.object(upstream.warp_admin, "rotate_exit", return_value=(False, "http_500")),
            patch.object(upstream, "WARP_ROTATOR_URL", ""),
        ):
            self.assertFalse(upstream.request_warp_rotation("test"))


if __name__ == "__main__":
    unittest.main()
