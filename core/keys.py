"""Dashboard-generated API keys.

Keys are shown exactly once, at creation. Only a SHA-256 hash is stored, so a
database leak cannot be replayed. The environment key (``PROXY_API_KEY``) stays
valid alongside generated keys.
"""

import hashlib
import logging
import secrets
import threading
import time
import uuid
from typing import Dict, List, Optional

from core.db import db_execute, db_fetchall

log = logging.getLogger("core.keys")

KEY_PREFIX = "sk-oc-"
_TOUCH_INTERVAL = 15.0
_touch_lock = threading.Lock()
_last_touch: Dict[str, float] = {}


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_raw_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(24)


def create_key(label: str = "dashboard") -> Dict[str, object]:
    raw = _new_raw_key()
    key_id = uuid.uuid4().hex[:12]
    now = time.time()
    clean_label = (label or "dashboard").strip()[:64] or "dashboard"
    db_execute(
        """
        INSERT INTO api_keys
            (id, label, key_hash, prefix, created_at, last_used_at, request_count, revoked)
        VALUES (?, ?, ?, ?, ?, 0, 0, 0)
        """,
        (key_id, clean_label, hash_key(raw), raw[:12], now),
    )
    return {
        "id": key_id,
        "key": raw,
        "label": clean_label,
        "prefix": raw[:12],
        "created_at": now,
    }


def list_keys() -> List[Dict[str, object]]:
    rows = db_fetchall(
        """
        SELECT id, label, prefix, created_at, last_used_at, request_count, revoked
        FROM api_keys
        ORDER BY created_at DESC
        """
    )
    return [
        {
            "id": row[0],
            "label": row[1],
            "prefix": row[2],
            "created_at": row[3],
            "last_used_at": row[4],
            "request_count": row[5],
            "revoked": bool(row[6]),
        }
        for row in rows
    ]


def revoke_key(key_id: str) -> bool:
    cursor = db_execute("UPDATE api_keys SET revoked = 1 WHERE id = ?", (key_id,))
    return cursor.rowcount > 0


def active_key_hashes() -> List[str]:
    try:
        rows = db_fetchall("SELECT key_hash FROM api_keys WHERE revoked = 0")
        return [row[0] for row in rows]
    except Exception as exc:  # noqa: BLE001 - auth must never hard-fail
        log.warning("Could not read generated API keys: %s", exc)
        return []


def verify_key(raw: str) -> Optional[str]:
    """Return the key id when ``raw`` is a live generated key."""
    digest = hash_key(raw)
    try:
        rows = db_fetchall(
            "SELECT id, revoked FROM api_keys WHERE key_hash = ?", (digest,)
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not verify generated API key: %s", exc)
        return None
    if not rows:
        return None
    key_id, revoked = rows[0]
    if revoked:
        return None

    now = time.time()
    with _touch_lock:
        last = _last_touch.get(digest, 0.0)
        should_touch = now - last >= _TOUCH_INTERVAL
        if should_touch:
            _last_touch[digest] = now
    if should_touch:
        try:
            db_execute(
                "UPDATE api_keys SET last_used_at = ?, request_count = request_count + 1 WHERE id = ?",
                (now, key_id),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not update key usage: %s", exc)
    return key_id
