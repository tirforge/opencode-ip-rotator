import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import db


LEGACY_SCHEMA = """
CREATE TABLE ip_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    ip TEXT NOT NULL,
    country TEXT,
    city TEXT,
    flag TEXT
)
"""


class DatabaseMigrationTests(unittest.TestCase):
    def test_reason_column_added_to_legacy_table(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.db"
            connection = sqlite3.connect(path)
            connection.execute(LEGACY_SCHEMA)
            connection.commit()
            connection.close()

            with patch("core.db.METRICS_DB_PATH", path):
                db.init_db()

            connection = sqlite3.connect(path)
            columns = [row[1] for row in connection.execute("PRAGMA table_info(ip_history)")]
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )]
            connection.close()

            self.assertIn("reason", columns)
            self.assertIn("active_flow_leases", tables)


if __name__ == "__main__":
    unittest.main()
