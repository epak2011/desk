"""Strictly user-scoped private state persistence."""

from __future__ import annotations

import json
import copy
import hashlib
import threading
from collections import OrderedDict
from typing import Any


def normalize_user_id(user_id: str) -> str:
    value = str(user_id or "").strip()
    if not value:
        raise ValueError("A verified user ID is required for private storage.")
    return value


def owner_claim_allowed(identity_email: str, configured_owner_email: str, existing_state) -> bool:
    """Permit one idempotent legacy import for the explicitly configured owner."""
    identity = str(identity_email or "").strip().lower()
    configured = str(configured_owner_email or "").strip().lower()
    if not (identity and configured and identity == configured):
        return False
    return not isinstance(existing_state, dict) or not existing_state.get("legacy_owner_imported_at")


def merge_owner_legacy_state(legacy_state, existing_state) -> dict[str, Any]:
    """Restore the legacy desk without discarding newer owner-account activity."""
    legacy = dict(legacy_state or {})
    existing = dict(existing_state or {})
    merged = dict(legacy)
    merged.update(existing)

    for key in ("holdings", "pm_cache", "notes", "position_notes", "chat_history"):
        legacy_section = legacy.get(key) if isinstance(legacy.get(key), dict) else {}
        existing_section = existing.get(key) if isinstance(existing.get(key), dict) else {}
        if legacy_section or existing_section:
            merged[key] = {**legacy_section, **existing_section}

    legacy_watchlist = legacy.get("watchlist") if isinstance(legacy.get("watchlist"), list) else []
    existing_watchlist = existing.get("watchlist") if isinstance(existing.get("watchlist"), list) else []
    merged["watchlist"] = list(dict.fromkeys(
        str(ticker).upper().strip()
        for ticker in [*legacy_watchlist, *existing_watchlist]
        if str(ticker or "").strip()
    ))

    decisions_by_id = {}
    for index, entry in enumerate([*(legacy.get("decisions_log") or []), *(existing.get("decisions_log") or [])]):
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or entry.get("ts") or entry.get("created_at") or f"legacy-{index}")
        decisions_by_id[entry_id] = entry
    if decisions_by_id:
        merged["decisions_log"] = list(decisions_by_id.values())
    return merged


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
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_oauth_flows (
            state TEXT PRIMARY KEY,
            code_verifier TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    cur.execute("ALTER TABLE auth_oauth_flows ENABLE ROW LEVEL SECURITY")


def save_oauth_flow(cur, state: str, code_verifier: str, redirect_uri: str) -> None:
    cur.execute("DELETE FROM auth_oauth_flows WHERE expires_at <= NOW()")
    cur.execute(
        """
        INSERT INTO auth_oauth_flows (state, code_verifier, redirect_uri, expires_at)
        VALUES (%s, %s, %s, NOW() + INTERVAL '10 minutes')
        """,
        (str(state), str(code_verifier), str(redirect_uri)),
    )


def consume_oauth_flow(cur, state: str) -> dict[str, str] | None:
    cur.execute(
        """
        DELETE FROM auth_oauth_flows
        WHERE state = %s AND expires_at > NOW()
        RETURNING code_verifier, redirect_uri
        """,
        (str(state),),
    )
    row = cur.fetchone()
    return {"code_verifier": str(row[0]), "redirect_uri": str(row[1])} if row else None


# Cache only immutable copies. Every load still checks the database version,
# so writes from Streamlit, the API or another process are visible immediately.
_state_cache = OrderedDict()
_state_cache_lock = threading.Lock()
_STATE_CACHE_BYTES = 16 * 1024 * 1024
_STATE_CACHE_USERS = 16


def _cache_key(cur, user_id):
    connection = getattr(cur, "connection", None)
    info = getattr(connection, "info", None)
    dsn = str(getattr(info, "dsn", "test"))
    return hashlib.sha256(dsn.encode()).hexdigest(), user_id


def load(cur, user_id: str) -> dict[str, Any] | None:
    scoped_id = normalize_user_id(user_id)
    key = _cache_key(cur, scoped_id)
    with _state_cache_lock:
        cached = _state_cache.get(key)
        version = cached[0] if cached else None
        cur.execute(
            "SELECT updated_at, CASE WHEN updated_at = %s::timestamptz "
            "THEN NULL ELSE value END FROM user_app_state WHERE user_id = %s::uuid",
            (version, scoped_id),
        )
        row = cur.fetchone()
        if not row:
            _state_cache.pop(key, None)
            return None
        updated_at, value = row
        if cached and updated_at == version and value is None:
            _state_cache.move_to_end(key)
            return copy.deepcopy(cached[1])
        value = value or {}
        size = len(json.dumps(value, default=str).encode())
        _state_cache.pop(key, None)
        if updated_at is not None and size <= _STATE_CACHE_BYTES:
            while _state_cache and (len(_state_cache) >= _STATE_CACHE_USERS or
                    sum(item[2] for item in _state_cache.values()) + size > _STATE_CACHE_BYTES):
                _state_cache.popitem(last=False)
            _state_cache[key] = (updated_at, copy.deepcopy(value), size)
        return value


def save(cur, user_id: str, value: dict[str, Any]) -> None:
    scoped_id = normalize_user_id(user_id)
    with _state_cache_lock:
        _state_cache.pop(_cache_key(cur, scoped_id), None)
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
    with _state_cache_lock:
        _state_cache.pop(_cache_key(cur, scoped_id), None)
    cur.execute("DELETE FROM user_app_state WHERE user_id = %s::uuid", (scoped_id,))
