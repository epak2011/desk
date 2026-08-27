"""Validation helpers for the authenticated first-run experience."""

from __future__ import annotations

import re
from typing import Iterable


_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")


def parse_tickers(value: str | Iterable[str], limit: int = 20) -> list[str]:
    raw = re.split(r"[\s,]+", value) if isinstance(value, str) else list(value)
    result = []
    for item in raw:
        ticker = str(item or "").upper().strip()
        if ticker and _TICKER.fullmatch(ticker) and ticker not in result:
            result.append(ticker)
        if len(result) >= limit:
            break
    return result


def notification_preferences(
    email: str,
    *,
    daily_digest: bool,
    high_priority: bool,
) -> dict:
    return {
        "email": str(email or "").strip().lower(),
        "daily_digest": bool(daily_digest),
        "high_priority": bool(high_priority),
        "delivery_enabled": False,
    }
