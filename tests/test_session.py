import os
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

import core.session as session
from core import db
from server import app

PASSWORD = "correct-horse-battery-staple"
SECRET = "test-session-secret-" + "x" * 32


class PasswordSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_patch = patch(
            "core.db.METRICS_DB_PATH", Path(self.directory.name) / "metrics.db"
        )
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)

        self.env = patch.dict(
            os.environ,
            {
                "PROXY_API_KEY": "api-test-key",
                "DASHBOARD_PASSWORD": PASSWORD,
                "SESSION_SECRET": SECRET,
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("DASHBOARD_PASSWORD_HASH", None)
        os.environ.pop("PROXY_IP_ALLOWLIST", None)

        session._password_hash = None
        session._password_loaded = False
        session._serializer = None
        session.login_guard.reset("testclient")
        db.init_db()
        self.client = TestClient(app)

    # -- hashing ------------------------------------------------------
    def test_password_verification(self):
        self.assertTrue(session.verify_password(PASSWORD))
        self.assertFalse(session.verify_password("wrong"))
        self.assertFalse(session.verify_password(""))

    def test_hash_is_argon2id(self):
        digest = session.hash_password(PASSWORD)
        self.assertTrue(digest.startswith("$argon2id$"))
        self.assertNotIn(PASSWORD, digest)

    # -- routes -------------------------------------------------------
    def test_login_page_renders(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Control room access", resp.text)
        self.assertIn('name="password"', resp.text)

    def test_root_redirects_to_login_when_signed_out(self):
        resp = self.client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/login", resp.headers["location"])

    def test_wrong_password_is_rejected(self):
        resp = self.client.post(
            "/login", data={"password": "nope", "next": "/"}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Incorrect password", resp.text)
        self.assertNotIn(session.COOKIE_NAME, resp.cookies)

    def test_correct_password_creates_session(self):
        resp = self.client.post(
            "/login", data={"password": PASSWORD, "next": "/"}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(session.COOKIE_NAME, resp.cookies)
        dashboard = self.client.get("/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Gateway overview", dashboard.text)

    def test_open_redirect_is_blocked(self):
        resp = self.client.post(
            "/login",
            data={"password": PASSWORD, "next": "//evil.example/steal"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/")

    def test_logout_clears_session(self):
        self.client.post("/login", data={"password": PASSWORD, "next": "/"})
        self.client.post("/logout")
        resp = self.client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/login", resp.headers["location"])

    def test_csrf_required_for_state_changing_api(self):
        self.client.post("/login", data={"password": PASSWORD, "next": "/"})
        blocked = self.client.post("/api/keys", json={"label": "x"})
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.json()["error"]["type"], "csrf_error")

        html = self.client.get("/").text
        token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        allowed = self.client.post(
            "/api/keys", json={"label": "x"}, headers={"X-CSRF-Token": token}
        )
        self.assertEqual(allowed.status_code, 201)

    def test_login_lockout_after_repeated_failures(self):
        for _ in range(6):
            self.client.post(
                "/login", data={"password": "bad", "next": "/"}, follow_redirects=False
            )
        resp = self.client.post(
            "/login", data={"password": PASSWORD, "next": "/"}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 429)
        self.assertIn("Too many failed attempts", resp.text)

    def test_security_headers_present(self):
        resp = self.client.get("/login")
        self.assertIn("content-security-policy", {k.lower() for k in resp.headers})
        self.assertEqual(resp.headers["x-frame-options"], "DENY")
        self.assertEqual(resp.headers["x-content-type-options"], "nosniff")
        self.assertIn("nonce-", resp.headers["content-security-policy"])


if __name__ == "__main__":
    unittest.main()
