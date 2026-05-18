from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

_HASH_PREFIX = "pbkdf2_sha256"
_ITERATIONS = 390_000


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_HASH_PREFIX}${_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: Optional[str]) -> bool:
    if not password or not encoded:
        return False
    try:
        prefix, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if prefix != _HASH_PREFIX:
            return False
        salt = _unb64(salt_b64)
        expected = _unb64(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def sign_artifact_cookie(slug: str, secret: str, *, now: Optional[int] = None) -> str:
    if not secret:
        raise ValueError("cookie secret must not be empty")
    timestamp = int(now or time.time())
    nonce = secrets.token_urlsafe(12)
    payload = f"{slug}|{timestamp}|{nonce}"
    signature = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}|{signature}"


def verify_artifact_cookie(slug: str, cookie: Optional[str], secret: str, *, max_age_seconds: int = 60 * 60 * 24 * 30) -> bool:
    if not cookie or not secret:
        return False
    try:
        cookie_slug, timestamp_raw, nonce, signature = cookie.split("|", 3)
        if cookie_slug != slug:
            return False
        timestamp = int(timestamp_raw)
        if time.time() - timestamp > max_age_seconds:
            return False
        payload = f"{cookie_slug}|{timestamp}|{nonce}"
        expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except Exception:
        return False


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
