from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from app.config import Settings


USER_SESSION_COOKIE_NAME = "ab_launch_session"
ADMIN_SESSION_COOKIE_NAME = "ab_admin_session"
SETUP_SESSION_COOKIE_NAME = "ab_setup_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
ADMIN_SESSION_MAX_AGE_SECONDS = 60 * 60 * 8
SETUP_SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(f"{data}{padding}".encode("utf-8"))


def _sign(payload: str, settings: Settings) -> str:
    return hmac.new(settings.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_session_token(subject: str, scope: str, settings: Settings, max_age: int) -> str:
    now = int(time.time())
    payload = {"sub": subject, "scope": scope, "iat": now, "exp": now + max_age}
    encoded = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _sign(encoded, settings)
    return f"{encoded}.{signature}"


def read_session_token(token: str | None, settings: Settings, required_scope: str) -> str | None:
    if not token or "." not in token:
        return None
    encoded, provided_signature = token.rsplit(".", 1)
    expected_signature = _sign(encoded, settings)
    if not hmac.compare_digest(provided_signature, expected_signature):
        return None
    try:
        payload = json.loads(_b64decode(encoded))
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("scope") != required_scope:
        return None
    expires_at = payload.get("exp")
    subject = payload.get("sub")
    if not isinstance(expires_at, int) or expires_at < int(time.time()):
        return None
    if not isinstance(subject, str) or not subject:
        return None
    return subject
