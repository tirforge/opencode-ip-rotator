import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from core import db, tunnel
from server import app

TEST_KEY = "dashboard-test-key"


def _snapshot(ips):
    return {
        "online": True,
        "active": 3,
        "healthy": 3,
        "cooling_down": 0,
        "recycling": False,
        "pool_size": 3,
        "stack": "ipv6",
        "exits": [
            {"id": f"exit-{index}", "ip": ip, "state": "healthy", "served": index}
            for index, ip in enumerate(ips)
        ],
        "ips": list(ips),
    }


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_patch = patch(
            "core.db.METRICS_DB_PATH", Path(self.directory.name) / "metrics.db"
        )
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)

        self.env = patch.dict(
            os.environ, {"PROXY_API_KEY": TEST_KEY}, clear=False
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("PROXY_IP_ALLOWLIST", None)

        db.init_db()
        with tunnel._lock:  # noqa: SLF001 - reset the short-lived URL cache
            tunnel._cached.update({"url": "", "at": 0.0})  # noqa: SLF001
        self.client = TestClient(app)
        self.headers = {"Authorization": f"Bearer {TEST_KEY}"}

    # -- auth ---------------------------------------------------------
    def test_overview_requires_a_key(self):
        resp = self.client.get("/api/overview")
        self.assertEqual(resp.status_code, 401)

    def test_dashboard_key_query_sets_session_cookie(self):
        resp = self.client.get(
            f"/dashboard?key={TEST_KEY}", follow_redirects=False
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("oc_dash", resp.cookies)

    def test_dashboard_renders_with_cookie(self):
        self.client.get(f"/dashboard?key={TEST_KEY}", follow_redirects=False)
        resp = self.client.get("/dashboard")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Gateway overview", resp.text)

    # -- overview -----------------------------------------------------
    @patch("services.warp_admin.snapshot")
    @patch("services.warp_admin.pool_exits")
    def test_overview_shape(self, pool_exits, snapshot):
        snapshot.return_value = _snapshot(["2a09:bac5::1", "2a09:bac5::2"])
        pool_exits.return_value = []
        resp = self.client.get("/api/overview", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        for field in (
            "service",
            "uptime_seconds",
            "totals",
            "models",
            "pool",
            "egress",
            "rotations",
            "series",
            "events",
            "security",
            "tunnel",
        ):
            self.assertIn(field, data)
        self.assertEqual(data["egress"]["primary"], "2a09:bac5::1")
        self.assertEqual(len(data["series"]), 30)
        self.assertGreaterEqual(data["security"]["configured_keys"], 1)

    def test_requests_feed(self):
        resp = self.client.get("/api/requests?limit=5", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("events", data)
        self.assertIn("series", data)
        self.assertIn("window", data)

    # -- api keys -----------------------------------------------------
    def test_create_key_then_authenticate_with_it(self):
        resp = self.client.post(
            "/api/keys", headers=self.headers, json={"label": "laptop"}
        )
        self.assertEqual(resp.status_code, 201)
        created = resp.json()["key"]
        self.assertTrue(created["key"].startswith("sk-oc-"))

        listed = self.client.get("/api/keys", headers=self.headers).json()["keys"]
        self.assertEqual(len(listed), 1)
        self.assertNotIn("key", listed[0])

        generated = {"Authorization": f"Bearer {created['key']}"}
        self.assertEqual(
            self.client.get("/api/overview", headers=generated).status_code, 200
        )

        revoked = self.client.delete(
            f"/api/keys/{created['id']}", headers=self.headers
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(
            self.client.get("/api/overview", headers=generated).status_code, 401
        )

    def test_revoke_unknown_key_is_404(self):
        resp = self.client.delete("/api/keys/does-not-exist", headers=self.headers)
        self.assertEqual(resp.status_code, 404)

    def test_generated_key_can_open_dashboard(self):
        created = self.client.post(
            "/api/keys", headers=self.headers, json={"label": "phone"}
        ).json()["key"]
        resp = self.client.get(
            f"/dashboard?key={created['key']}", follow_redirects=False
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)

    # -- rotation -----------------------------------------------------
    @patch("routes.api.asyncio.sleep", new_callable=AsyncMock)
    @patch("services.warp_admin.snapshot")
    @patch("services.warp_admin.rotate_exit")
    def test_rotate_ip_swaps_exit(self, rotate_exit, snapshot, _sleep):
        snapshot.side_effect = [
            _snapshot(["2a09:bac5::1"]),
            _snapshot(["2a09:bac5::9"]),
        ]
        rotate_exit.return_value = (True, "swap")
        resp = self.client.post("/api/ip/rotate", headers=self.headers, json={})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["changed"])
        self.assertEqual(data["old_ip"], "2a09:bac5::1")
        self.assertEqual(data["new_ip"], "2a09:bac5::9")
        self.assertEqual(data["rotations"], 1)

    @patch("routes.api.asyncio.sleep", new_callable=AsyncMock)
    @patch("services.warp_admin.snapshot")
    @patch("services.warp_admin.recycle_pool")
    @patch("services.warp_admin.rotate_exit")
    def test_rotate_falls_back_to_pool_recycle(
        self, rotate_exit, recycle_pool, snapshot, _sleep
    ):
        snapshot.side_effect = [
            _snapshot(["2a09:bac5::1"]),
            _snapshot(["2a09:bac5::7"]),
        ]
        rotate_exit.return_value = (False, "no_standby")
        recycle_pool.return_value = (True, "http_200")
        data = self.client.post(
            "/api/ip/rotate", headers=self.headers, json={}
        ).json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["reason"], "http_200")
        recycle_pool.assert_called_once()

    @patch("routes.api.asyncio.sleep", new_callable=AsyncMock)
    @patch("services.warp_admin.snapshot")
    @patch("services.warp_admin.recycle_pool")
    @patch("services.warp_admin.rotate_exit")
    def test_rotate_reports_failure(
        self, rotate_exit, recycle_pool, snapshot, _sleep
    ):
        snapshot.return_value = _snapshot(["2a09:bac5::1"])
        rotate_exit.return_value = (False, "http_500")
        recycle_pool.return_value = (False, "http_500")
        data = self.client.post(
            "/api/ip/rotate", headers=self.headers, json={}
        ).json()
        self.assertFalse(data["ok"])
        self.assertFalse(data["changed"])

    # -- tunnel -------------------------------------------------------
    def test_tunnel_without_file(self):
        with patch(
            "core.tunnel.TUNNEL_URL_FILE", Path(self.directory.name) / "missing"
        ):
            data = self.client.get("/api/tunnel", headers=self.headers).json()
        self.assertFalse(data["configured"])
        self.assertEqual(data["url"], "")

    def test_tunnel_reads_written_url(self):
        tunnel_file = Path(self.directory.name) / "tunnel_url"
        tunnel_file.write_text("https://demo-test.trycloudflare.com\n", encoding="utf-8")
        with patch("core.tunnel.TUNNEL_URL_FILE", tunnel_file):
            data = self.client.get("/api/tunnel", headers=self.headers).json()
        self.assertTrue(data["configured"])
        self.assertEqual(data["url"], "https://demo-test.trycloudflare.com")


if __name__ == "__main__":
    unittest.main()
