"""Session-aware freshness rules for scheduled market snapshots."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


MARKET_TZ = ZoneInfo("America/New_York")
EQUITY_OPEN = time(9, 30)
EQUITY_CLOSE = time(16, 0)
WORKER_GRACE_CLOSE = time(16, 30)


def is_continuous_market(ticker: str) -> bool:
    ticker = str(ticker or "").upper().strip()
    return ticker.endswith("-USD") or ticker.endswith("-USDT")


def market_now(value: datetime | None = None) -> datetime:
    value = value or datetime.now(MARKET_TZ)
    if value.tzinfo is None:
        return value.replace(tzinfo=MARKET_TZ)
    return value.astimezone(MARKET_TZ)


def parse_timestamp(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def equity_market_open(value: datetime | None = None) -> bool:
    now = market_now(value)
    return now.weekday() < 5 and EQUITY_OPEN <= now.time() <= EQUITY_CLOSE


def worker_should_refresh(ticker: str, value: datetime | None = None) -> bool:
    """Refresh crypto continuously and equities only near/open market hours."""
    if is_continuous_market(ticker):
        return True
    now = market_now(value)
    return now.weekday() < 5 and time(9, 25) <= now.time() <= WORKER_GRACE_CLOSE


def _previous_equity_close(now: datetime) -> datetime:
    cursor = now
    if cursor.weekday() < 5 and cursor.time() >= EQUITY_CLOSE:
        return cursor.replace(hour=16, minute=0, second=0, microsecond=0)
    cursor = cursor - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor.replace(hour=16, minute=0, second=0, microsecond=0)


def snapshot_freshness(
    ticker: str,
    updated_at: datetime | None,
    *,
    now: datetime | None = None,
    open_max_age_minutes: int = 30,
    continuous_max_age_minutes: int = 60,
) -> dict:
    """Return freshness plus user-facing context for the relevant market session."""
    current = market_now(now)
    if updated_at is None:
        return {"fresh": False, "age_minutes": None, "reason": "timestamp missing"}
    stamp = market_now(updated_at)
    age = max(0, int((current - stamp).total_seconds() // 60))
    if is_continuous_market(ticker):
        return {
            "fresh": age <= continuous_max_age_minutes,
            "age_minutes": age,
            "reason": "continuous market",
        }
    if equity_market_open(current):
        return {
            "fresh": age <= open_max_age_minutes,
            "age_minutes": age,
            "reason": "U.S. market open",
        }
    latest_close = _previous_equity_close(current)
    fresh = stamp >= latest_close - timedelta(minutes=30)
    return {
        "fresh": fresh,
        "age_minutes": age,
        "reason": "current for last completed U.S. session" if fresh else "older than last completed U.S. session",
    }
