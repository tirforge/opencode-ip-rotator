import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import db, keys


class ApiKeyStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.patcher = patch(
            "core.db.METRICS_DB_PATH", Path(self.directory.name) / "metrics.db"
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        db.init_db()

    def test_create_and_verify(self):
        created = keys.create_key("laptop")
        self.assertTrue(created["key"].startswith(keys.KEY_PREFIX))
        self.assertEqual(keys.verify_key(created["key"]), created["id"])

    def test_wrong_key_is_rejected(self):
        keys.create_key("laptop")
        self.assertIsNone(keys.verify_key("sk-oc-not-a-real-key"))

    def test_revoked_key_is_rejected(self):
        created = keys.create_key("old")
        self.assertTrue(keys.revoke_key(created["id"]))
        self.assertIsNone(keys.verify_key(created["key"]))
        self.assertEqual(keys.active_key_hashes(), [])

    def test_list_never_returns_the_secret(self):
        created = keys.create_key("phone")
        items = keys.list_keys()
        self.assertEqual(len(items), 1)
        self.assertNotIn("key", items[0])
        self.assertNotIn("key_hash", items[0])
        self.assertEqual(items[0]["prefix"], created["key"][:12])

    def test_keys_are_unique(self):
        first = keys.create_key("a")
        second = keys.create_key("b")
        self.assertNotEqual(first["key"], second["key"])
        self.assertNotEqual(first["id"], second["id"])


if __name__ == "__main__":
    unittest.main()
