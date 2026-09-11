"""Small, auditable access checks for owner-only operator surfaces."""

from __future__ import annotations

from typing import Any, Mapping


def owner_access_allowed(identity: Mapping[str, Any] | None, owner_email: str) -> bool:
    """Require both an authenticated identity and an exact configured email match."""
    identity = identity if isinstance(identity, Mapping) else {}
    user_id = str(identity.get("user_id") or "").strip()
    signed_in_email = str(identity.get("email") or "").strip().casefold()
    configured_email = str(owner_email or "").strip().casefold()
    return bool(user_id and configured_email and signed_in_email == configured_email)
