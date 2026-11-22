"""Password-gated dashboard sessions.

Security posture:

* The dashboard password is hashed with **Argon2id** (``argon2-cffi``, the
  reference implementation) using 64 MiB / 3 passes / 2 lanes. The plaintext
  never leaves the process and is never written to disk.
* Sessions are **signed** with ``itsdangerous`` (HMAC-SHA1 via the standard
  ``URLSafeTimedSerializer``) and carry an expiry, so a stolen cookie cannot be
  forged or replayed forever.
* Every session embeds a random CSRF token that the dashboard must echo back
  on state-changing requests.
* The signing secret is generated once and stored with ``0600`` permissions.

The password itself comes from ``DASHBOARD_PASSWORD`` (plaintext env, hashed at
startup) or ``DASHBOARD_PASSWORD_HASH`` (a ready-made ``$argon2...`` string).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from core.config import DEFAULT_DATA_DIR

log = logging.getLogger("core.session")

COOKIE_NAME = "oc_dash"
SESSION_TTL = max(300, int(float(os.environ.get("DASHBOARD_SESSION_HOURS", "12")) * 3600))
COOKIE_MAX_AGE = SESSION_TTL
SESSION_SECRET_FILE = Path(
    os.environ.get("SESSION_SECRET_FILE", str(DEFAULT_DATA_DIR / "session.key"))
)

# Argon2id parameters. 64 MiB is the OWASP baseline for interactive logins and
# takes well under 100 ms on the VPS.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=2,
    hash_len=32,
    salt_len=16,
)

_lock = threading.Lock()
_serializer: Optional[URLSafeTimedSerializer] = None
_password_hash: Optional[str] = None
_password_loaded = False


# ---------------------------------------------------------------------------
# Signing secret
# ---------------------------------------------------------------------------


def _load_secret() -> bytes:
    env_secret = os.environ.get("SESSION_SECRET", "").strip()
    if env_secret:
        return env_secret.encode("utf-8")

    try:
        raw = SESSION_SECRET_FILE.read_text(encoding="utf-8").strip()
        if len(raw) >= 32:
            return raw.encode("utf-8")
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 - never block login on a bad file
        log.warning("Could not read session secret: %s", exc)

    generated = secrets.token_urlsafe(48)
    try:
        SESSION_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_SECRET_FILE.write_text(generated, encoding="utf-8")
        try:
            os.chmod(SESSION_SECRET_FILE, 0o600)
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001 - read-only filesystem (Vercel)
        log.warning("Could not persist session secret, using derived key: %s", exc)
        seed = os.environ.get("PROXY_API_KEY", "opencode-proxy") + os.uname().nodename
        return hashlib.sha256(seed.encode("utf-8")).hexdigest().encode("utf-8")
    return generated.encode("utf-8")


def _get_serializer() -> URLSafeTimedSerializer:
    global _serializer
    with _lock:
        if _serializer is None:
            _serializer = URLSafeTimedSerializer(
                _load_secret(), salt="opencode-dashboard-session"
            )
        return _serializer


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------


def _get_password_hash() -> Optional[str]:
    """Return the Argon2 hash for the configured password (hashed once)."""
    global _password_hash, _password_loaded
    with _lock:
        if _password_loaded:
            return _password_hash
        _password_loaded = True

        ready = os.environ.get("DASHBOARD_PASSWORD_HASH", "").strip()
        if ready.startswith("$argon2"):
            _password_hash = ready
            return _password_hash

        plaintext = os.environ.get("DASHBOARD_PASSWORD", "")
        if plaintext:
            started = time.perf_counter()
            _password_hash = _hasher.hash(plaintext)
            log.info(
                "Dashboard password hashed with Argon2id in %.0f ms",
                (time.perf_counter() - started) * 1000,
            )
        return _password_hash


def password_configured() -> bool:
    return _get_password_hash() is not None


def verify_password(candidate: str) -> bool:
    """Constant-time Argon2id verification. False on any failure."""
    if not candidate:
        return False
    stored = _get_password_hash()
    if not stored:
        return False
    try:
        return _hasher.verify(stored, candidate)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception as exc:  # noqa: BLE001 - fail closed
        log.warning("Password verification failed: %s", exc)
        return False


def hash_password(plaintext: str) -> str:
    """Used by ``scripts/set_dashboard_password.py`` and tests."""
    return _hasher.hash(plaintext)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def new_session() -> Dict[str, Any]:
    return {
        "v": 1,
        "iat": int(time.time()),
        "csrf": secrets.token_urlsafe(24),
        "jti": secrets.token_hex(8),
    }


def sign_session(data: Dict[str, Any]) -> str:
    return _get_serializer().dumps(data)


def load_session(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    try:
        payload = _get_serializer().loads(token, max_age=SESSION_TTL)
    except SignatureExpired:
        return None
    except BadSignature:
        return None
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not read session: %s", exc)
        return None
    if not isinstance(payload, dict) or not payload.get("csrf"):
        return None
    return payload


def csrf_matches(session: Dict[str, Any], supplied: str) -> bool:
    expected = str(session.get("csrf") or "")
    return bool(expected) and hmac.compare_digest(expected, supplied or "")


def cookie_is_secure(request: Any) -> bool:
    """True when the browser reached us over HTTPS (directly or via tunnel)."""
    try:
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        return request.url.scheme == "https" or proto == "https"
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Login throttling (per client IP, failures only)
# ---------------------------------------------------------------------------


class LoginGuard:
    """Block brute force: 6 failures per 10 min, then a 15 min lockout."""

    def __init__(self, limit: int = 6, window: int = 600, lockout: int = 900) -> None:
        self.limit = limit
        self.window = window
        self.lockout = lockout
        self._failures: Dict[str, list] = {}
        self._blocked_until: Dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, identity: str) -> int:
        now = time.time()
        with self._lock:
            until = self._blocked_until.get(identity, 0.0)
            if until > now:
                return int(until - now) + 1
            if until:
                self._blocked_until.pop(identity, None)
            return 0

    def fail(self, identity: str) -> None:
        now = time.time()
        with self._lock:
            if len(self._failures) > 2048:
                self._failures = {
                    key: [t for t in stamps if now - t < self.window]
                    for key, stamps in self._failures.items()
                }
            stamps = [t for t in self._failures.get(identity, []) if now - t < self.window]
            stamps.append(now)
            if len(stamps) >= self.limit:
                self._blocked_until[identity] = now + self.lockout
                self._failures.pop(identity, None)
            else:
                self._failures[identity] = stamps

    def reset(self, identity: str) -> None:
        with self._lock:
            self._failures.pop(identity, None)
            self._blocked_until.pop(identity, None)


# Shared between the middleware and the login route.
login_guard = LoginGuard()
