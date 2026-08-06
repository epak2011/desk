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
    "pm_memo",
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


def claim_next_job(worker_name: str = "worker") -> dict[str, Any] | None:
    ensure_backend_schema()
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH next_job AS (
                    SELECT id
                    FROM refresh_jobs
                    WHERE status = 'queued'
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
                (worker_name,),
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
        "pm_memos",
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
