import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import COOKIE_NAME, ApiKeyMiddleware
from server import app

TEST_KEY = "test-secret-key-123"


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.env = patch.dict(os.environ, {"PROXY_API_KEY": TEST_KEY}, clear=False)
        self.env.start()
        os.environ.pop("PROXY_IP_ALLOWLIST", None)

    def tearDown(self):
        self.env.stop()

    def test_health_is_public(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)

    def test_missing_key_is_rejected(self):
        resp = self.client.get("/v1/models")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["type"], "authentication_error")

    def test_wrong_key_is_rejected(self):
        resp = self.client.get("/v1/models", headers={"Authorization": "Bearer nope"})
        self.assertEqual(resp.status_code, 401)

    def test_bearer_key_is_accepted(self):
        with patch("routes.models.get_discovered_models", return_value=[]):
            resp = self.client.get(
                "/v1/models", headers={"Authorization": f"Bearer {TEST_KEY}"}
            )
        self.assertEqual(resp.status_code, 200)

    def test_x_api_key_is_accepted(self):
        with patch("routes.models.get_discovered_models", return_value=[]):
            resp = self.client.get("/v1/models", headers={"x-api-key": TEST_KEY})
        self.assertEqual(resp.status_code, 200)

    def test_dashboard_key_is_exchanged_for_cookie(self):
        resp = self.client.get(f"/dashboard?key={TEST_KEY}", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(COOKIE_NAME, resp.cookies)

    def test_dashboard_stats_accepts_cookie(self):
        self.client.get(f"/dashboard?key={TEST_KEY}", follow_redirects=False)
        resp = self.client.get("/dashboard/stats")
        self.assertEqual(resp.status_code, 200)

    def test_ip_allowlist_blocks_other_clients(self):
        os.environ["PROXY_IP_ALLOWLIST"] = "203.0.113.9"
        resp = self.client.get("/v1/models", headers={"Authorization": f"Bearer {TEST_KEY}"})
        self.assertEqual(resp.status_code, 403)
        os.environ.pop("PROXY_IP_ALLOWLIST", None)


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        os.environ["PROXY_API_KEY"] = TEST_KEY
        self.app = FastAPI()
        self.app.add_middleware(ApiKeyMiddleware, limit=2, window=60, allowlist=[])

        @self.app.get("/v1/models")
        async def _models():
            return {"object": "list", "data": []}

        self.client = TestClient(self.app)

    def tearDown(self):
        os.environ.pop("PROXY_API_KEY", None)

    def test_third_request_is_rate_limited(self):
        headers = {"Authorization": f"Bearer {TEST_KEY}"}
        self.assertEqual(self.client.get("/v1/models", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/v1/models", headers=headers).status_code, 200)
        resp = self.client.get("/v1/models", headers=headers)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["error"]["type"], "rate_limit_error")


if __name__ == "__main__":
    unittest.main()
