"""Durable backend primitives for Trading Desk.

This module is intentionally independent from Streamlit. The current app can
import it to enqueue work, and a future FastAPI service or React frontend can
use the same tables without inheriting Streamlit's rerun model.
"""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

try:
    from psycopg_pool import ConnectionPool
except Exception:  # pragma: no cover - lets local imports survive without deps
    ConnectionPool = None


JOB_TYPES = {
    "market_snapshot",
    "watchlist_market_scan",
    "full_report",
    "market_regime_daily",
    "repair_missing_data",
}

TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled"}

_POOL = None


def database_url() -> str:
    """Read DATABASE_URL from env, then Streamlit secrets when available."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return _clean_database_url(url)
    try:
        import streamlit as st  # type: ignore

        return _clean_database_url(str(st.secrets.get("DATABASE_URL", "")).strip())
    except Exception:
        return ""


def _clean_database_url(url: str) -> str:
    url = str(url or "").strip().strip('"').strip("'")
    # Clean URLs copied from docs/chat with markdown artifacts, e.g.
    # postgresql://...@[host:5432/postgres](http://host:5432/postgres)
    url = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", url)
    url = re.sub(r"(?<=:)\[([^\]@]*)\](?=@)", r"\1", url)
    url = re.sub(r"(?<=@)\[([^\]]+)\]", r"\1", url)
    if url.startswith(("postgres://", "postgresql://")) and "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return url


def has_database() -> bool:
    return bool(database_url())


def get_pool():
    global _POOL
    if _POOL is None:
        if ConnectionPool is None:
            raise RuntimeError("psycopg-pool is not installed.")
        url = database_url()
        if not url:
            raise RuntimeError("DATABASE_URL is not configured.")
        _POOL = ConnectionPool(
            url,
            min_size=1,
            max_size=5,
            max_idle=60,
            max_lifetime=300,
            reconnect_timeout=10,
            kwargs={"autocommit": True},
            check=ConnectionPool.check_connection,
        )
    return _POOL


def reset_pool() -> None:
    global _POOL
    pool = _POOL
    _POOL = None
    if pool is not None:
        try:
            pool.close()
        except Exception:
            pass


def _is_transient_db_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "ssl connection has been closed",
            "consuming input failed",
            "server closed the connection",
            "connection is closed",
            "terminating connection",
            "connection timeout",
            "could not receive data",
        )
    )


@contextmanager
def db_connection():
    last_error = None
    for attempt in range(2):
        try:
            with get_pool().connection() as conn:
                yield conn
            return
        except Exception as exc:
            last_error = exc
            if attempt == 0 and _is_transient_db_error(exc):
                reset_pool()
                continue
            raise
    if last_error:
        raise last_error


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, set):
        return [json_safe(v) for v in sorted(value, key=str)]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if hasattr(value, "item"):
        try:
            return json_safe(value.item())
        except Exception:
            return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def json_dumps(value: Any) -> str:
    return json.dumps(json_safe(value), allow_nan=False, sort_keys=True, separators=(",", ":"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_backend_schema() -> None:
    """Create backend tables. Safe to call on every app boot."""
    with db_connection() as conn:
        with conn.cursor() as cur:
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
                CREATE TABLE IF NOT EXISTS watchlist_assets (
                    ticker TEXT PRIMARY KEY,
                    asset_type TEXT DEFAULT 'stock',
                    name TEXT,
                    sector TEXT,
                    industry TEXT,
                    exchange TEXT,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    notes TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    ticker TEXT PRIMARY KEY,
                    payload JSONB NOT NULL,
                    source TEXT NOT NULL DEFAULT 'yahoo',
                    as_of TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS rule_outputs (
                    ticker TEXT PRIMARY KEY,
                    action TEXT,
                    trigger_text TEXT,
                    invalidation_text TEXT,
                    setup_type TEXT,
                    confidence NUMERIC,
                    payload JSONB NOT NULL,
                    market_updated_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pm_memos (
                    ticker TEXT PRIMARY KEY,
                    payload JSONB NOT NULL,
                    source TEXT,
                    generated_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS research_reports (
                    ticker TEXT PRIMARY KEY,
                    payload JSONB NOT NULL,
                    source TEXT,
                    generated_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS holdings (
                    ticker TEXT PRIMARY KEY,
                    payload JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS decisions_log (
                    id TEXT PRIMARY KEY,
                    entry JSONB NOT NULL,
                    entry_ts TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS engine_review_status (
                    key TEXT PRIMARY KEY,
                    payload JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS market_regime_daily (
                    day DATE PRIMARY KEY,
                    payload JSONB NOT NULL,
                    source TEXT,
                    generated_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS refresh_jobs (
                    id UUID PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    ticker TEXT,
                    status TEXT NOT NULL DEFAULT 'queued',
                    priority INTEGER NOT NULL DEFAULT 100,
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    result JSONB,
                    error TEXT,
                    requested_by TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    started_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS refresh_jobs_status_priority_idx
                ON refresh_jobs (status, priority, created_at)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS refresh_jobs_ticker_idx
                ON refresh_jobs (ticker, created_at DESC)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id UUID PRIMARY KEY,
                    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
                    digest_key TEXT NOT NULL UNIQUE,
                    recipient TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    html TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    provider_id TEXT,
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    started_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute("ALTER TABLE notification_outbox ENABLE ROW LEVEL SECURITY")
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS notification_outbox_status_idx
                ON notification_outbox (status, created_at)
                """
            )


def sync_watchlist_assets(tickers: Iterable[str]) -> None:
    clean = [str(t or "").upper().strip() for t in tickers]
    clean = [t for t in dict.fromkeys(clean) if t]
    if not clean:
        return
    ensure_backend_schema()
    with db_connection() as conn:
        with conn.cursor() as cur:
            for ticker in clean:
                cur.execute(
                    """
                    INSERT INTO watchlist_assets (ticker, enabled, updated_at)
                    VALUES (%s, TRUE, NOW())
                    ON CONFLICT (ticker) DO UPDATE
                        SET enabled = TRUE, updated_at = NOW()
                    """,
                    (ticker,),
                )


def enqueue_notification(
    *,
    user_id: str,
    digest_key: str,
    recipient: str,
    subject: str,
    html: str,
) -> str | None:
    """Queue one idempotent email. Existing digest keys are never duplicated."""
    row_id = str(uuid.uuid4())
    ensure_backend_schema()
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notification_outbox
                    (id, user_id, digest_key, recipient, subject, html, status, updated_at)
                VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, 'queued', NOW())
                ON CONFLICT (digest_key) DO NOTHING
                RETURNING id::text
                """,
                (row_id, str(user_id), str(digest_key), str(recipient), str(subject), str(html)),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None


def claim_notifications(*, limit: int = 10, max_attempts: int = 3) -> list[dict[str, Any]]:
    """Atomically claim queued delivery rows without returning data to logs."""
    ensure_backend_schema()
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH claimed AS (
                    SELECT id
                    FROM notification_outbox
                    WHERE status IN ('queued', 'retry')
                      AND attempts < %s
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE notification_outbox n
                SET status = 'sending', attempts = attempts + 1,
                    started_at = NOW(), updated_at = NOW(), error = NULL
                FROM claimed
                WHERE n.id = claimed.id
                RETURNING n.id::text, n.recipient, n.subject, n.html, n.attempts
                """,
                (max(1, int(max_attempts)), max(1, int(limit))),
            )
            return [
                {"id": row[0], "recipient": row[1], "subject": row[2], "html": row[3], "attempts": row[4]}
                for row in cur.fetchall()
            ]


def complete_notification(row_id: str, provider_id: str) -> None:
    ensure_backend_schema()
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE notification_outbox
                SET status = 'sent', provider_id = %s, completed_at = NOW(), updated_at = NOW(), error = NULL
                WHERE id = %s::uuid
                """,
                (str(provider_id), str(row_id)),
            )


def fail_notification(row_id: str, error: str, *, retry: bool = True) -> None:
    ensure_backend_schema()
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE notification_outbox
                SET status = %s, error = %s, updated_at = NOW()
                WHERE id = %s::uuid
                """,
                ("retry" if retry else "failed", str(error or "Delivery failed")[:300], str(row_id)),
            )


def notification_outbox_health() -> dict[str, int]:
    ensure_backend_schema()
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, COUNT(*)
                FROM notification_outbox
                WHERE created_at >= NOW() - INTERVAL '7 days'
                GROUP BY status
                """
            )
            counts = {str(status): int(count) for status, count in cur.fetchall()}
    return {
        "queued": counts.get("queued", 0) + counts.get("retry", 0),
        "sending": counts.get("sending", 0),
        "sent": counts.get("sent", 0),
        "failed": counts.get("failed", 0),
    }


def notification_users() -> list[dict[str, Any]]:
    """Return opted-in authenticated users for server-side digest generation."""
    ensure_backend_schema()
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id::text, value
                FROM user_app_state
                WHERE COALESCE((value->'notification_preferences'->>'daily_digest')::boolean, FALSE) = TRUE
                  AND COALESCE(value->'notification_preferences'->>'delivery_enabled', 'false')::boolean = TRUE
                  AND COALESCE(value->'notification_preferences'->>'email', '') <> ''
                """
            )
            return [
                {"user_id": str(user_id), "state": value or {}}
                for user_id, value in cur.fetchall()
                if isinstance(value, dict)
            ]


def unsubscribe_notifications(user_id: str, email: str) -> bool:
    """Disable every email channel only when the signed identity and address match."""
    ensure_backend_schema()
    clean_email = str(email or "").strip().lower()
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE user_app_state
                SET value = jsonb_set(
                        jsonb_set(
                            jsonb_set(value, '{notification_preferences,daily_digest}', 'false'::jsonb, TRUE),
                            '{notification_preferences,high_priority}', 'false'::jsonb, TRUE
                        ),
                        '{notification_preferences,delivery_enabled}', 'false'::jsonb, TRUE
                    ),
                    updated_at = NOW()
                WHERE user_id = %s::uuid
                  AND LOWER(COALESCE(value->'notification_preferences'->>'email', '')) = %s
                RETURNING user_id
                """,
                (str(user_id), clean_email),
            )
            return cur.fetchone() is not None


def auth_schema_health() -> dict[str, bool]:
    """Read the private-table RLS state without mutating the schema.

    Health checks must remain diagnostic. Running the full schema bootstrap here
    allowed an unrelated DDL failure to turn a correctly migrated auth schema
    into a false ``Needs migration`` result.
    """
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.relname, c.relrowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname IN ('user_app_state', 'notification_outbox')
                """
            )
            rows = {str(name): bool(rls) for name, rls in cur.fetchall()}
    return {
        "user_app_state": rows.get("user_app_state", False),
        "notification_outbox": rows.get("notification_outbox", False),
    }


def stale_watchlist_market_tickers(*, max_age_minutes: int = 10, limit: int = 100) -> list[str]:
    """Enabled watchlist tickers with missing/stale market snapshot rows."""
    if not has_database():
        return []
    ensure_backend_schema()
    age_minutes = max(1, int(max_age_minutes))
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT w.ticker
                FROM watchlist_assets w
                LEFT JOIN market_snapshots m ON m.ticker = w.ticker
                WHERE w.enabled = TRUE
                  AND (
                    m.ticker IS NULL
                    OR m.updated_at < NOW() - (%s::text || ' minutes')::interval
                  )
                ORDER BY COALESCE(m.updated_at, w.created_at) ASC
                LIMIT %s
                """,
                (str(age_minutes), max(1, int(limit))),
            )
            return [str(row[0]).upper().strip() for row in cur.fetchall() if str(row[0] or "").strip()]


def recent_auto_rule_decision_exists(ticker: str, signature: str, *, within_days: int = 7) -> bool:
    """True when the same open auto-logged rules call was recently persisted."""
    clean_ticker = str(ticker or "").upper().strip()
    clean_signature = str(signature or "").strip()
    if not clean_ticker or not clean_signature or not has_database():
        return False
    ensure_backend_schema()
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM decisions_log
                WHERE entry->>'ticker' = %s
                  AND entry->>'rule_signature' = %s
                  AND COALESCE((entry->>'auto_logged')::boolean, FALSE) = TRUE
                  AND (entry->'outcome' IS NULL OR entry->'outcome' = 'null'::jsonb)
                  AND COALESCE(entry_ts, updated_at) >= NOW() - (%s::text || ' days')::interval
                LIMIT 1
                """,
                (clean_ticker, clean_signature, str(max(1, int(within_days)))),
            )
            return cur.fetchone() is not None


def upsert_decision_log(entry: dict[str, Any]) -> None:
    """Persist one decision-log entry without touching the legacy store blob."""
    entry = entry if isinstance(entry, dict) else {}
    entry_id = str(entry.get("id") or "").strip()
    if not entry_id:
        raise ValueError("decision log entry requires id")
    entry_ts = entry.get("ts") or entry.get("created_at") or ""
    ensure_backend_schema()
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO decisions_log (id, entry, entry_ts, updated_at)
                VALUES (%s, %s::jsonb, NULLIF(%s, '')::timestamptz, NOW())
                ON CONFLICT (id) DO UPDATE
                    SET entry = EXCLUDED.entry,
                        entry_ts = EXCLUDED.entry_ts,
                        updated_at = NOW()
                """,
                (entry_id, json_dumps(entry), str(entry_ts or "")),
            )


def read_decision_logs(*, limit: int | None = None) -> list[dict[str, Any]]:
    """Read durable decision entries for background evaluation."""
    ensure_backend_schema()
    query = "SELECT entry FROM decisions_log ORDER BY COALESCE(entry_ts, updated_at) ASC"
    params: tuple[Any, ...] = ()
    if limit is not None:
        query += " LIMIT %s"
        params = (max(1, int(limit)),)
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return [row[0] for row in cur.fetchall() if row and isinstance(row[0], dict)]


def write_engine_review_status(payload: dict[str, Any]) -> None:
    """Persist the latest calibration alert snapshot for cheap UI reads."""
    ensure_backend_schema()
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO engine_review_status (key, payload, updated_at)
                VALUES ('current', %s::jsonb, NOW())
                ON CONFLICT (key) DO UPDATE
                    SET payload = EXCLUDED.payload,
                        updated_at = NOW()
                """,
                (json_dumps(payload or {}),),
            )


def read_engine_review_status() -> dict[str, Any]:
    """Read the worker-generated calibration alert snapshot."""
    ensure_backend_schema()
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM engine_review_status WHERE key = 'current'")
            row = cur.fetchone()
    return row[0] if row and isinstance(row[0], dict) else {}


def enqueue_job(
    job_type: str,
    *,
    ticker: str | None = None,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    requested_by: str = "streamlit",
    dedupe_active: bool = True,
) -> str | None:
    """Create a durable refresh job and return its id.

    If an equivalent queued/running job already exists, returns that job id.
    """
    job_type = str(job_type or "").strip()
    if job_type not in JOB_TYPES:
        raise ValueError(f"Unknown job_type: {job_type}")
    clean_ticker = str(ticker or "").upper().strip() or None
    ensure_backend_schema()
    with db_connection() as conn:
        with conn.cursor() as cur:
            if dedupe_active:
                cur.execute(
                    """
                    SELECT id
                    FROM refresh_jobs
                    WHERE job_type = %s
                      AND COALESCE(ticker, '') = COALESCE(%s, '')
                      AND status IN ('queued', 'running')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (job_type, clean_ticker),
                )
                row = cur.fetchone()
                if row:
                    return str(row[0])
            job_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO refresh_jobs
                    (id, job_type, ticker, status, priority, payload, requested_by)
                VALUES
                    (%s, %s, %s, 'queued', %s, %s::jsonb, %s)
                """,
                (job_id, job_type, clean_ticker, int(priority), json_dumps(payload or {}), requested_by),
            )
            return job_id


def latest_jobs(ticker: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
    if not has_database():
        return []
    ensure_backend_schema()
    clean_ticker = str(ticker or "").upper().strip()
    with db_connection() as conn:
        with conn.cursor() as cur:
            if clean_ticker:
                cur.execute(
                    """
                    SELECT id, job_type, ticker, status, result, error, created_at,
                           started_at, completed_at, updated_at
                    FROM refresh_jobs
                    WHERE ticker = %s OR ticker IS NULL
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (clean_ticker, int(limit)),
                )
            else:
                cur.execute(
                    """
                    SELECT id, job_type, ticker, status, result, error, created_at,
                           started_at, completed_at, updated_at
                    FROM refresh_jobs
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (int(limit),),
                )
            rows = cur.fetchall()
    jobs = []
    for row in rows:
        jobs.append(
            {
                "id": str(row[0]),
                "job_type": row[1],
                "ticker": row[2],
                "status": row[3],
                "result": row[4] or {},
                "error": row[5],
                "created_at": row[6],
                "started_at": row[7],
                "completed_at": row[8],
                "updated_at": row[9],
            }
        )
    return jobs


def retry_failed_jobs(
    ticker: str | None = None,
    job_type: str | None = None,
    *,
    error_contains: str | None = None,
    limit: int = 50,
) -> int:
    """Move recent failed jobs back to queued so the worker can retry them."""
    if not has_database():
        return 0
    clean_ticker = str(ticker or "").upper().strip()
    clean_job_type = str(job_type or "").strip()
    if clean_job_type and clean_job_type not in JOB_TYPES:
        raise ValueError(f"Unknown job_type: {clean_job_type}")
    ensure_backend_schema()
    clauses = ["status = 'failed'"]
    params: list[Any] = []
    if clean_ticker:
        clauses.append("ticker = %s")
        params.append(clean_ticker)
    if clean_job_type:
        clauses.append("job_type = %s")
        params.append(clean_job_type)
    clean_error = str(error_contains or "").strip()
    if clean_error:
        clauses.append("error ILIKE %s")
        params.append(f"%{clean_error}%")
    params.append(max(1, int(limit)))
    where_sql = " AND ".join(clauses)
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                WITH retry AS (
                    SELECT id
                    FROM refresh_jobs
                    WHERE {where_sql}
                    ORDER BY updated_at DESC
                    LIMIT %s
                )
                UPDATE refresh_jobs j
                SET status = 'queued',
                    error = NULL,
                    result = '{{}}'::jsonb,
                    started_at = NULL,
                    completed_at = NULL,
                    updated_at = NOW()
                FROM retry
                WHERE j.id = retry.id
                """,
                tuple(params),
            )
            return int(cur.rowcount or 0)


def retire_job_type(job_type: str, *, statuses: Iterable[str] = ("queued", "failed"), limit: int = 500) -> int:
    """Delete obsolete queued/failed jobs for a job type the worker no longer supports."""
    if not has_database():
        return 0
    clean_job_type = str(job_type or "").strip()
    if not clean_job_type:
        return 0
    clean_statuses = [str(status or "").strip() for status in statuses]
    clean_statuses = [status for status in dict.fromkeys(clean_statuses) if status]
    if not clean_statuses:
        return 0
    ensure_backend_schema()
    placeholders = ", ".join(["%s"] * len(clean_statuses))
    params: list[Any] = [clean_job_type, *clean_statuses, max(1, int(limit))]
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                WITH retired AS (
                    SELECT id
                    FROM refresh_jobs
                    WHERE job_type = %s
                      AND status IN ({placeholders})
                    ORDER BY updated_at DESC
                    LIMIT %s
                )
                DELETE FROM refresh_jobs j
                USING retired
                WHERE j.id = retired.id
                """,
                tuple(params),
            )
            return int(cur.rowcount or 0)


def recover_stale_running_jobs(*, max_age_minutes: int = 30, limit: int = 50) -> int:
    """Requeue running jobs whose worker disappeared before marking completion."""
    if not has_database():
        return 0
    ensure_backend_schema()
    age_minutes = max(5, int(max_age_minutes))
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH stale AS (
                    SELECT id
                    FROM refresh_jobs
                    WHERE status = 'running'
                      AND COALESCE(started_at, updated_at, created_at) < NOW() - (%s::text || ' minutes')::interval
                    ORDER BY COALESCE(started_at, updated_at, created_at) ASC
                    LIMIT %s
                )
                UPDATE refresh_jobs j
                SET status = 'queued',
                    error = NULL,
                    started_at = NULL,
                    completed_at = NULL,
                    updated_at = NOW()
                FROM stale
                WHERE j.id = stale.id
                """,
                (str(age_minutes), max(1, int(limit))),
            )
            return int(cur.rowcount or 0)


def read_json_table(table: str, key_value: str | None = None, *, limit: int | None = None) -> dict[str, dict[str, Any]]:
    """Read normalized JSON payload rows keyed by ticker/day.

    This gives the Streamlit UI a cheap way to hydrate from worker output
    without recomputing market data on every rerun.
    """
    allowed = {
        "market_snapshots": ("ticker", "updated_at"),
        "rule_outputs": ("ticker", "updated_at"),
        "pm_memos": ("ticker", "updated_at"),
        "research_reports": ("ticker", "updated_at"),
        "holdings": ("ticker", "updated_at"),
        "market_regime_daily": ("day", "updated_at"),
    }
    if table not in allowed:
        raise ValueError(f"Unsupported table: {table}")
    if not has_database():
        return {}
    ensure_backend_schema()
    key_column, order_column = allowed[table]
    source_tables = {"market_snapshots", "pm_memos", "research_reports", "market_regime_daily"}
    select_source = table in source_tables
    select_cols = (
        f"{key_column}, payload, source, {order_column}"
        if select_source
        else f"{key_column}, payload, {order_column}"
    )
    clean_key = str(key_value or "").upper().strip()
    rows: list[tuple[Any, ...]] = []
    with db_connection() as conn:
        with conn.cursor() as cur:
            if clean_key:
                cur.execute(
                    f"""
                    SELECT {select_cols}
                    FROM {table}
                    WHERE {key_column} = %s
                    """,
                    (clean_key,),
                )
            elif limit:
                cur.execute(
                    f"""
                    SELECT {select_cols}
                    FROM {table}
                    ORDER BY {order_column} DESC
                    LIMIT %s
                    """,
                    (int(limit),),
                )
            else:
                cur.execute(
                    f"""
                    SELECT {select_cols}
                    FROM {table}
                    ORDER BY {order_column} DESC
                    """
                )
            rows = cur.fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if select_source:
            key, payload, source, updated_at = row
        else:
            key, payload, updated_at = row
            source = None
        normalized_key = str(key or "").upper().strip()
        if not normalized_key:
            continue
        value = payload or {}
        if isinstance(value, dict):
            value = dict(value)
        else:
            value = {"value": value}
        if source is not None and not value.get("_source") and not value.get("source"):
            value["_source"] = source
        if updated_at is not None and not value.get("updated_at"):
            try:
                value["updated_at"] = updated_at.isoformat()
            except Exception:
                value["updated_at"] = str(updated_at)
        out[normalized_key] = value
    return out


def _pm_memo_text(payload: dict[str, Any] | None) -> str:
    """Return the first real memo thesis across supported payload shapes."""
    if not isinstance(payload, dict):
        return ""
    nested_pm = payload.get("pm") if isinstance(payload.get("pm"), dict) else {}
    bullets = payload.get("bullets") if isinstance(payload.get("bullets"), dict) else {}
    nested_bullets = nested_pm.get("bullets") if isinstance(nested_pm.get("bullets"), dict) else {}
    candidates = (
        payload.get("thesis"),
        nested_pm.get("thesis"),
        bullets.get("thesis"),
        nested_bullets.get("thesis"),
        payload.get("pm_narrative"),
        nested_pm.get("pm_narrative"),
        payload.get("dossier"),
    )
    placeholders = (
        "no generated pm thesis yet",
        "no thesis on file",
        "pm research has not produced",
    )
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text and not text.lower().startswith(placeholders):
            return text
    return ""


def validate_pm_memo_payload(payload: dict[str, Any] | None, ticker: str = "") -> dict[str, Any]:
    """Reject blank/placeholder PM rows before they can replace durable content."""
    clean_ticker = str(ticker or "").upper().strip()
    if not isinstance(payload, dict):
        raise ValueError(f"PM memo payload for {clean_ticker or 'ticker'} must be an object")
    safe_payload = json_safe(payload)
    if not isinstance(safe_payload, dict) or not _pm_memo_text(safe_payload):
        raise ValueError(f"PM memo for {clean_ticker or 'ticker'} has no durable thesis")
    return safe_payload


def read_pm_memo(ticker: str) -> dict[str, Any]:
    """Read the durable PM memo row. Database errors intentionally propagate."""
    clean_ticker = str(ticker or "").upper().strip()
    if not clean_ticker or not has_database():
        return {}
    return (read_json_table("pm_memos", clean_ticker) or {}).get(clean_ticker, {}) or {}


def upsert_pm_memo(
    ticker: str,
    payload: dict[str, Any],
    *,
    source: str | None = None,
) -> dict[str, Any]:
    """Persist and read back one PM memo, returning only a verified DB revision.

    A generated memo is not considered saved until PostgreSQL returns the row and
    a second SELECT observes the exact revision id. This prevents session state
    from masquerading as durable storage and prevents blank initialization state
    from overwriting a previously valid memo.
    """
    clean_ticker = str(ticker or "").upper().strip()
    if not clean_ticker:
        raise ValueError("PM memo ticker is required")
    stored_payload = validate_pm_memo_payload(payload, clean_ticker)
    revision_id = uuid.uuid4().hex
    generated_at = str(stored_payload.get("_worker_generated_at") or utc_now_iso())
    stored_source = str(source or stored_payload.get("_source") or stored_payload.get("source") or "claude")
    stored_payload = {
        **stored_payload,
        "_revision_id": revision_id,
        "_worker_generated_at": generated_at,
        "_source": stored_source,
    }

    ensure_backend_schema()
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pm_memos (ticker, payload, source, generated_at, updated_at)
                VALUES (%s, %s::jsonb, %s, %s::timestamptz, NOW())
                ON CONFLICT (ticker) DO UPDATE
                    SET payload = EXCLUDED.payload,
                        source = EXCLUDED.source,
                        generated_at = EXCLUDED.generated_at,
                        updated_at = NOW()
                RETURNING payload, source, updated_at
                """,
                (clean_ticker, json_dumps(stored_payload), stored_source, generated_at),
            )
            returned = cur.fetchone()
            if not returned or not isinstance(returned[0], dict):
                raise RuntimeError(f"PostgreSQL did not acknowledge PM memo {clean_ticker}")
            if returned[0].get("_revision_id") != revision_id:
                raise RuntimeError(f"PM memo write acknowledgement mismatch for {clean_ticker}")
            cur.execute(
                """
                SELECT payload, source, updated_at
                FROM pm_memos
                WHERE ticker = %s
                """,
                (clean_ticker,),
            )
            verified = cur.fetchone()

    if not verified or not isinstance(verified[0], dict):
        raise RuntimeError(f"PM memo read-back failed for {clean_ticker}")
    verified_payload = dict(verified[0])
    if verified_payload.get("_revision_id") != revision_id:
        raise RuntimeError(f"PM memo read-back revision mismatch for {clean_ticker}")
    validate_pm_memo_payload(verified_payload, clean_ticker)
    verified_payload["_source"] = verified_payload.get("_source") or verified[1] or stored_source
    if verified[2] is not None:
        verified_payload["updated_at"] = (
            verified[2].isoformat() if hasattr(verified[2], "isoformat") else str(verified[2])
        )
    print(f"[pm-persistence] write_verified ticker={clean_ticker} revision={revision_id[:10]}")
    return verified_payload


def read_json_table_many(table: str, key_values: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Read normalized JSON payload rows for a specific key set."""
    allowed = {
        "market_snapshots": ("ticker", "updated_at"),
        "rule_outputs": ("ticker", "updated_at"),
        "pm_memos": ("ticker", "updated_at"),
        "research_reports": ("ticker", "updated_at"),
        "holdings": ("ticker", "updated_at"),
        "market_regime_daily": ("day", "updated_at"),
    }
    if table not in allowed:
        raise ValueError(f"Unsupported table: {table}")
    clean_keys = [
        str(key or "").upper().strip()
        for key in (key_values or [])
        if str(key or "").strip()
    ]
    clean_keys = list(dict.fromkeys(clean_keys))
    if not clean_keys or not has_database():
        return {}
    ensure_backend_schema()
    key_column, order_column = allowed[table]
    source_tables = {"market_snapshots", "pm_memos", "research_reports", "market_regime_daily"}
    select_source = table in source_tables
    select_cols = (
        f"{key_column}, payload, source, {order_column}"
        if select_source
        else f"{key_column}, payload, {order_column}"
    )
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {select_cols}
                FROM {table}
                WHERE {key_column} = ANY(%s)
                """,
                (clean_keys,),
            )
            rows = cur.fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if select_source:
            key, payload, source, updated_at = row
        else:
            key, payload, updated_at = row
            source = None
        normalized_key = str(key or "").upper().strip()
        if not normalized_key:
            continue
        value = payload or {}
        if isinstance(value, dict):
            value = dict(value)
        else:
            value = {"value": value}
        if source is not None and not value.get("_source") and not value.get("source"):
            value["_source"] = source
        if updated_at is not None and not value.get("updated_at"):
            try:
                value["updated_at"] = updated_at.isoformat()
            except Exception:
                value["updated_at"] = str(updated_at)
        out[normalized_key] = value
    return out


def read_json_tables(tables: Iterable[str], key_value: str | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    """Read several normalized payload tables with one small helper call."""
    return {str(table): read_json_table(str(table), key_value) for table in tables}


def claim_next_job(worker_name: str = "worker", job_types: Iterable[str] | None = None) -> dict[str, Any] | None:
    allowed_job_types = []
    for raw_type in job_types or []:
        clean_type = str(raw_type or "").strip()
        if not clean_type:
            continue
        if clean_type not in JOB_TYPES:
            raise ValueError(f"Unknown job_type: {clean_type}")
        allowed_job_types.append(clean_type)
    type_filter = "AND job_type = ANY(%s)" if allowed_job_types else ""
    params: list[Any] = []
    if allowed_job_types:
        params.append(allowed_job_types)
    params.append(worker_name)
    ensure_backend_schema()
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                WITH next_job AS (
                    SELECT id
                    FROM refresh_jobs
                    WHERE status = 'queued'
                      {type_filter}
                    ORDER BY priority ASC, created_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE refresh_jobs j
                SET status = 'running',
                    attempts = attempts + 1,
                    started_at = COALESCE(started_at, NOW()),
                    updated_at = NOW(),
                    requested_by = COALESCE(requested_by, %s)
                FROM next_job
                WHERE j.id = next_job.id
                RETURNING j.id, j.job_type, j.ticker, j.payload, j.attempts
                """,
                tuple(params),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "id": str(row[0]),
        "job_type": row[1],
        "ticker": row[2],
        "payload": row[3] or {},
        "attempts": row[4],
    }


def complete_job(job_id: str, result: dict[str, Any] | None = None) -> None:
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE refresh_jobs
                SET status = 'succeeded',
                    result = %s::jsonb,
                    error = NULL,
                    completed_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (json_dumps(result or {}), job_id),
            )


def fail_job(job_id: str, error: str, result: dict[str, Any] | None = None) -> None:
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE refresh_jobs
                SET status = 'failed',
                    result = %s::jsonb,
                    error = %s,
                    completed_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (json_dumps(result or {}), str(error)[:1000], job_id),
            )


def upsert_json_table(table: str, key_column: str, key_value: str, payload: dict[str, Any], *, source: str | None = None) -> None:
    allowed = {
        "market_snapshots",
        "rule_outputs",
        "research_reports",
        "holdings",
        "market_regime_daily",
    }
    if table not in allowed:
        raise ValueError(f"Unsupported table: {table}")
    ensure_backend_schema()
    with db_connection() as conn:
        with conn.cursor() as cur:
            if table == "market_regime_daily":
                cur.execute(
                    """
                    INSERT INTO market_regime_daily (day, payload, source, generated_at, updated_at)
                    VALUES (%s::date, %s::jsonb, %s, NOW(), NOW())
                    ON CONFLICT (day) DO UPDATE
                        SET payload = EXCLUDED.payload,
                            source = EXCLUDED.source,
                            generated_at = EXCLUDED.generated_at,
                            updated_at = NOW()
                    """,
                    (key_value, json_dumps(payload), source),
                )
                return
            if table == "market_snapshots":
                cur.execute(
                    """
                    INSERT INTO market_snapshots (ticker, payload, source, as_of, updated_at)
                    VALUES (%s, %s::jsonb, %s, NOW(), NOW())
                    ON CONFLICT (ticker) DO UPDATE
                        SET payload = EXCLUDED.payload,
                            source = EXCLUDED.source,
                            as_of = EXCLUDED.as_of,
                            updated_at = NOW()
                    """,
                    (str(key_value).upper(), json_dumps(payload), source or "worker"),
                )
                return
            if table == "rule_outputs":
                trigger_text = payload.get("trigger_summary") or payload.get("trigger")
                if isinstance(trigger_text, (dict, list)):
                    trigger_text = json.dumps(json_safe(trigger_text), sort_keys=True)
                invalidation_text = payload.get("invalidation") or payload.get("risk")
                if isinstance(invalidation_text, (dict, list)):
                    invalidation_text = json.dumps(json_safe(invalidation_text), sort_keys=True)
                cur.execute(
                    """
                    INSERT INTO rule_outputs
                        (ticker, action, trigger_text, invalidation_text,
                         setup_type, confidence, payload, market_updated_at, updated_at)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s::jsonb, NOW(), NOW())
                    ON CONFLICT (ticker) DO UPDATE
                        SET action = EXCLUDED.action,
                            trigger_text = EXCLUDED.trigger_text,
                            invalidation_text = EXCLUDED.invalidation_text,
                            setup_type = EXCLUDED.setup_type,
                            confidence = EXCLUDED.confidence,
                            payload = EXCLUDED.payload,
                            market_updated_at = EXCLUDED.market_updated_at,
                            updated_at = NOW()
                    """,
                    (
                        str(key_value).upper(),
                        payload.get("action"),
                        trigger_text,
                        invalidation_text,
                        payload.get("setup_type") or payload.get("state"),
                        payload.get("confidence") or payload.get("setup_score"),
                        json_dumps(payload),
                    ),
                )
                return
            if table == "holdings":
                cur.execute(
                    """
                    INSERT INTO holdings (ticker, payload, updated_at)
                    VALUES (%s, %s::jsonb, NOW())
                    ON CONFLICT (ticker) DO UPDATE
                        SET payload = EXCLUDED.payload,
                            updated_at = NOW()
                    """,
                    (str(key_value).upper(), json_dumps(payload)),
                )
                return
            cur.execute(
                f"""
                INSERT INTO {table} ({key_column}, payload, source, generated_at, updated_at)
                VALUES (%s, %s::jsonb, %s, NOW(), NOW())
                ON CONFLICT ({key_column}) DO UPDATE
                    SET payload = EXCLUDED.payload,
                        source = EXCLUDED.source,
                        generated_at = EXCLUDED.generated_at,
                        updated_at = NOW()
                """,
                (key_value, json_dumps(payload), source),
            )
