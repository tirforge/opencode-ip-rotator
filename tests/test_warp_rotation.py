import json
import time
import unittest
from unittest.mock import MagicMock, patch

import services.upstream as upstream


class WarpRotationRequestTests(unittest.TestCase):
    def setUp(self):
        upstream._last_rotation_at = 0.0

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


if __name__ == "__main__":
    unittest.main()
