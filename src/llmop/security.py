"""Password hashing, API key and session token helpers (stdlib only).

* Passwords: scrypt, stored as ``scrypt$n$r$p$salt_b64$hash_b64``.
* API keys: ``sk-llop-`` + 43 url-safe chars; only ``sha256(key)`` is stored.
* Session ids: 32 random bytes; only ``sha256(id)`` is stored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

API_KEY_PREFIX = "sk-llop-"
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
_MAXMEM = 2**26


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str, *, n: int = SCRYPT_N) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=SCRYPT_R, p=SCRYPT_P, dklen=32,
        maxmem=_MAXMEM,
    )
    return f"scrypt${n}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p),
            dklen=len(expected), maxmem=_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


# Pre-computed hash used to equalise timing for unknown usernames.
_DUMMY_HASH: str | None = None


def dummy_verify(password: str) -> None:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("dummy-password-for-timing")
    verify_password(password, _DUMMY_HASH)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def looks_like_api_key(value: str) -> bool:
    return value.startswith(API_KEY_PREFIX) and 40 <= len(value) <= 80


def key_display(key: str) -> str:
    """What the UI shows after creation: prefix + last 4 characters."""
    return f"{API_KEY_PREFIX}...{key[-4:]}"


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


# Worker enrolment secrets (same format as B's gateway: prefix + base64url, no padding).
def new_reg_token() -> str:
    return "llmop_reg_" + secrets.token_urlsafe(24)


def new_worker_token() -> str:
    return "llmop_wk_" + secrets.token_urlsafe(32)
