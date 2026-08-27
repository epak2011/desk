"""Signed, expiring one-click unsubscribe tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


def create_token(user_id: str, email: str, secret: str, *, expires_in_days: int = 90) -> str:
    if not secret:
        raise ValueError("Unsubscribe secret is required.")
    payload = {
        "user_id": str(user_id),
        "email": str(email).strip().lower(),
        "exp": int(time.time()) + max(1, int(expires_in_days)) * 86400,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + signature).decode("ascii").rstrip("=")


def verify_token(token: str, secret: str, *, now: int | None = None) -> dict:
    if not token or not secret:
        raise ValueError("Invalid unsubscribe link.")
    try:
        padded = token + "=" * (-len(token) % 4)
        packed = base64.urlsafe_b64decode(padded.encode("ascii"))
        raw, observed = packed[:-32], packed[-32:]
        expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(observed, expected):
            raise ValueError
        payload = json.loads(raw.decode("utf-8"))
        if int(payload.get("exp") or 0) < int(now or time.time()):
            raise ValueError
        if not payload.get("user_id") or "@" not in str(payload.get("email") or ""):
            raise ValueError
        return payload
    except Exception:
        raise ValueError("Invalid or expired unsubscribe link.") from None
