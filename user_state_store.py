"""Strictly user-scoped private state persistence."""

from __future__ import annotations

import json
from typing import Any


def normalize_user_id(user_id: str) -> str:
    value = str(user_id or "").strip()
    if not value:
        raise ValueError("A verified user ID is required for private storage.")
    return value


def owner_claim_allowed(identity_email: str, configured_owner_email: str, existing_state) -> bool:
    """Permit one-time legacy import only for an explicitly configured owner."""
    identity = str(identity_email or "").strip().lower()
    configured = str(configured_owner_email or "").strip().lower()
    return existing_state is None and bool(identity and configured and identity == configured)


def ensure_schema(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_app_state (
            user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
            value JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    cur.execute("ALTER TABLE user_app_state ENABLE ROW LEVEL SECURITY")


def load(cur, user_id: str) -> dict[str, Any] | None:
    scoped_id = normalize_user_id(user_id)
    cur.execute("SELECT value FROM user_app_state WHERE user_id = %s::uuid", (scoped_id,))
    row = cur.fetchone()
    return (row[0] or {}) if row else None


def save(cur, user_id: str, value: dict[str, Any]) -> None:
    scoped_id = normalize_user_id(user_id)
    cur.execute(
        """
        INSERT INTO user_app_state (user_id, value, updated_at)
        VALUES (%s::uuid, %s::jsonb, NOW())
        ON CONFLICT (user_id) DO UPDATE
            SET value = EXCLUDED.value, updated_at = NOW()
        """,
        (scoped_id, json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))),
    )


def delete(cur, user_id: str) -> None:
    scoped_id = normalize_user_id(user_id)
    cur.execute("DELETE FROM user_app_state WHERE user_id = %s::uuid", (scoped_id,))
