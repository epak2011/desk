"""Supabase bearer-token verification for the standalone Trading Desk API."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass


class TokenVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedIdentity:
    user_id: str
    email: str
    display_name: str


def _setting(name: str) -> str:
    return str(os.environ.get(name, "") or "").strip()


def verify_access_token(token: str) -> VerifiedIdentity:
    """Ask Supabase Auth to validate a JWT and return its trusted identity."""
    token = str(token or "").strip()
    supabase_url = _setting("SUPABASE_URL")
    anon_key = _setting("SUPABASE_ANON_KEY")
    if not token:
        raise TokenVerificationError("A bearer token is required.")
    if not supabase_url or not anon_key:
        raise TokenVerificationError("Authentication is not configured.")
    request = urllib.request.Request(
        f"{supabase_url.rstrip('/')}/auth/v1/user",
        method="GET",
        headers={"apikey": anon_key, "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise TokenVerificationError("The session is invalid or expired.") from None
        raise TokenVerificationError("Authentication is temporarily unavailable.") from None
    except Exception:
        raise TokenVerificationError("Authentication is temporarily unavailable.") from None
    user_id = str(payload.get("id") or "").strip()
    if not user_id:
        raise TokenVerificationError("The session is invalid or expired.")
    metadata = payload.get("user_metadata") if isinstance(payload.get("user_metadata"), dict) else {}
    return VerifiedIdentity(
        user_id=user_id,
        email=str(payload.get("email") or "").strip(),
        display_name=str(metadata.get("display_name") or metadata.get("full_name") or "").strip(),
    )
