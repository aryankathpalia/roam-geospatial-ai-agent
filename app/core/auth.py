"""
Sign-in with Google, and ROAM's own session tokens.

The browser gets a Google ID token from Google Identity Services and posts it once; the backend verifies it
against GOOGLE_CLIENT_ID and returns a ROAM session token (a signed, expiring payload) that the browser sends
as `Authorization: Bearer <token>` -- a header, not a cookie, so it works with the site and the API on
different domains.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from fastapi import Request

from app.core.config import settings

SESSION_TTL_S = 14 * 24 * 3600
_SECRET_FILE = Path("data/.session_secret")


def _secret() -> bytes:
    if settings.SESSION_SECRET:
        return settings.SESSION_SECRET.encode()
    if not _SECRET_FILE.exists():
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECRET_FILE.write_text(secrets.token_hex(32), encoding="utf-8")
    return _SECRET_FILE.read_text(encoding="utf-8").strip().encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(user: dict) -> str:
    payload = _b64(json.dumps({**user, "exp": int(time.time()) + SESSION_TTL_S}).encode())
    sig = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def read_token(token: str) -> dict | None:
    try:
        payload, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        user = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    return user if user.get("exp", 0) > time.time() else None


def verify_google(credential: str) -> dict:
    """The Google account behind an ID token from Google Identity Services (raises ValueError if invalid)."""

    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    if not settings.GOOGLE_CLIENT_ID:
        raise ValueError("Google sign-in is not configured (GOOGLE_CLIENT_ID)")
    info = id_token.verify_oauth2_token(credential, google_requests.Request(), settings.GOOGLE_CLIENT_ID)
    if not info.get("email_verified"):
        raise ValueError("the Google account's email is not verified")
    return {"email": info["email"].lower(), "name": info.get("name") or info["email"], "picture": info.get("picture")}


def current_user(request: Request) -> dict | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return read_token(header[7:].strip())
    return None


def is_admin(user: dict | None) -> bool:
    admins = {e.strip().lower() for e in settings.ADMIN_EMAILS.split(",") if e.strip()}
    return bool(user) and user.get("email") in admins
