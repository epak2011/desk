"""Small Supabase Auth client used by the Streamlit beta.

Only the public anon key is used here.  Database credentials and service-role
keys must never be sent to the browser or accepted by this module.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping


class AuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthSession:
    user_id: str
    email: str
    access_token: str
    refresh_token: str
    expires_in: int


def configured(supabase_url: str, anon_key: str) -> bool:
    return bool(str(supabase_url or "").strip() and str(anon_key or "").strip())


def _request(url: str, anon_key: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    body = json.dumps(dict(payload)).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"apikey": anon_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            message = detail.get("msg") or detail.get("error_description") or detail.get("message")
        except Exception:
            message = None
        raise AuthError(str(message or "Authentication failed.")) from None
    except Exception:
        raise AuthError("Authentication service is temporarily unavailable.") from None


def _session(payload: Mapping[str, Any]) -> AuthSession:
    user = payload.get("user") if isinstance(payload.get("user"), Mapping) else {}
    user_id = str(user.get("id") or "").strip()
    access_token = str(payload.get("access_token") or "").strip()
    if not user_id or not access_token:
        raise AuthError("Check your email to confirm the account, then sign in.")
    return AuthSession(
        user_id=user_id,
        email=str(user.get("email") or "").strip(),
        access_token=access_token,
        refresh_token=str(payload.get("refresh_token") or "").strip(),
        expires_in=int(payload.get("expires_in") or 3600),
    )


def sign_in(supabase_url: str, anon_key: str, email: str, password: str) -> AuthSession:
    payload = _request(
        f"{supabase_url.rstrip('/')}/auth/v1/token?grant_type=password",
        anon_key,
        {"email": email.strip(), "password": password},
    )
    return _session(payload)


def sign_up(supabase_url: str, anon_key: str, email: str, password: str) -> AuthSession | None:
    payload = _request(
        f"{supabase_url.rstrip('/')}/auth/v1/signup",
        anon_key,
        {"email": email.strip(), "password": password},
    )
    if payload.get("access_token"):
        return _session(payload)
    return None


def refresh(supabase_url: str, anon_key: str, refresh_token: str) -> AuthSession:
    payload = _request(
        f"{supabase_url.rstrip('/')}/auth/v1/token?grant_type=refresh_token",
        anon_key,
        {"refresh_token": refresh_token},
    )
    return _session(payload)


def public_identity(session: AuthSession) -> dict[str, str]:
    """Return only identity fields safe to retain in Streamlit session state."""
    return {"user_id": session.user_id, "email": session.email}
