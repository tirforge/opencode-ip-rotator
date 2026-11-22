import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List

from core.config import METRICS_DB_PATH

log = logging.getLogger("core.db")
_db_lock = threading.Lock()

# Leases held for the lifetime of an upstream request/stream. The WARP rotator
# reads this table before rotating so an IP change never interrupts a live
# stream (which would break the upstream connection mid-response).
FLOW_LEASE_TTL = float(os.environ.get("FLOW_LEASE_TTL", "1800"))


def get_conn() -> sqlite3.Connection:
    METRICS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(METRICS_DB_PATH), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def db_execute(statement: str, params: tuple = ()) -> sqlite3.Cursor:
    for attempt in range(3):
        try:
            with _db_lock:
                conn = get_conn()
                try:
                    cursor = conn.cursor()
                    cursor.execute(statement, params)
                    conn.commit()
                    return cursor
                finally:
                    conn.close()
        except sqlite3.OperationalError as e:
            if "busy" in str(e).lower() and attempt < 2:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise


def db_fetchall(statement: str, params: tuple = ()) -> list:
    for attempt in range(3):
        try:
            with _db_lock:
                conn = get_conn()
                try:
                    cursor = conn.cursor()
                    cursor.execute(statement, params)
                    return cursor.fetchall()
                finally:
                    conn.close()
        except sqlite3.OperationalError as e:
            if "busy" in str(e).lower() and attempt < 2:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise


def init_db() -> None:
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS ip_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            ip TEXT NOT NULL,
            country TEXT,
            city TEXT,
            flag TEXT
        )
        """
    )
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS model_usage (
            model_name TEXT PRIMARY KEY,
            requests INTEGER DEFAULT 0,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            estimated_cost_usd REAL DEFAULT 0.0
        )
        """
    )
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS active_flow_leases (
            lease_id TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
        """
    )
    log.info("Database initialized at %s", METRICS_DB_PATH)


def acquire_flow_lease(lease_id: str, ttl_seconds: float = FLOW_LEASE_TTL) -> bool:
    """Mark an upstream flow as active so the rotator defers IP changes."""
    now = time.time()
    try:
        db_execute(
            "DELETE FROM active_flow_leases WHERE expires_at <= ?",
            (now,),
        )
        db_execute(
            "INSERT OR REPLACE INTO active_flow_leases (lease_id, created_at, expires_at) VALUES (?, ?, ?)",
            (lease_id, now, now + max(1.0, ttl_seconds)),
        )
        return True
    except Exception as exc:
        log.warning("Failed to acquire flow lease %s: %s", lease_id, exc)
        return False


def release_flow_lease(lease_id: str) -> None:
    try:
        db_execute(
            "DELETE FROM active_flow_leases WHERE lease_id = ?",
            (lease_id,),
        )
    except Exception as exc:
        log.warning("Failed to release flow lease %s: %s", lease_id, exc)


def has_active_flow_leases() -> bool:
    try:
        db_execute(
            "DELETE FROM active_flow_leases WHERE expires_at <= ?",
            (time.time(),),
        )
        rows = db_fetchall("SELECT 1 FROM active_flow_leases LIMIT 1")
        return bool(rows)
    except Exception as exc:
        log.warning("Failed to inspect flow leases: %s", exc)
        return False


def record_ip(ip: str, country: str = "", city: str = "", flag: str = "") -> None:
    try:
        db_execute(
            "INSERT INTO ip_history (ip, country, city, flag) VALUES (?, ?, ?, ?)",
            (ip, country, city, flag),
        )
    except Exception as exc:
        log.warning("Failed to record IP history: %s", exc)


def get_recent_ips(limit: int = 20) -> List[Dict[str, Any]]:
    try:
        rows = db_fetchall(
            "SELECT timestamp, ip, country, city, flag FROM ip_history ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "timestamp": r[0],
                "ip": r[1],
                "country": r[2],
                "city": r[3],
                "flag": r[4],
            }
            for r in rows
        ]
    except Exception as exc:
        log.warning("Failed to query IP history: %s", exc)
        return []


def record_usage(
    model_name: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost: float = 0.0,
) -> None:
    total = prompt_tokens + completion_tokens
    try:
        db_execute(
            """
            INSERT INTO model_usage (model_name, requests, prompt_tokens, completion_tokens, total_tokens, estimated_cost_usd)
            VALUES (?, 1, ?, ?, ?, ?)
            ON CONFLICT(model_name) DO UPDATE SET
                requests = requests + 1,
                prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                completion_tokens = completion_tokens + excluded.completion_tokens,
                total_tokens = total_tokens + excluded.total_tokens,
                estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd
            """,
            (model_name, prompt_tokens, completion_tokens, total, cost),
        )
    except Exception as exc:
        log.warning("Failed to record model usage: %s", exc)


def load_all_usage() -> Dict[str, Dict[str, Any]]:
    try:
        rows = db_fetchall(
            "SELECT model_name, requests, prompt_tokens, completion_tokens, total_tokens, estimated_cost_usd FROM model_usage"
        )
        return {
            r[0]: {
                "requests": r[1],
                "prompt_tokens": r[2],
                "completion_tokens": r[3],
                "total_tokens": r[4],
                "estimated_cost_usd": r[5],
            }
            for r in rows
        }
    except Exception as exc:
        log.warning("Failed to load model usage: %s", exc)
        return {}
