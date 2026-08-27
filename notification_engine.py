"""Presentation-neutral attention digest generation.

Delivery is intentionally separate: this module never sends email.  A future
scheduled worker can pass this payload to a configured provider with idempotency.
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Iterable, Mapping


def digest_events(events: Iterable[Mapping], *, include_medium: bool = False) -> list[dict]:
    priorities = {"critical", "high"} | ({"medium"} if include_medium else set())
    return [
        {
            key: event.get(key)
            for key in ("event_id", "ticker", "kind", "priority", "title", "detail")
            if key in event
        }
        for event in events
        if str(event.get("priority") or "").lower() in priorities
    ]


def build_digest(user_id: str, events: Iterable[Mapping], *, day: date | None = None) -> dict:
    digest_day = day or date.today()
    selected = digest_events(events)
    identity = "|".join(
        [str(user_id), digest_day.isoformat()] + sorted(str(e.get("event_id") or "") for e in selected)
    )
    return {
        "digest_key": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
        "day": digest_day.isoformat(),
        "events": selected,
        "count": len(selected),
        "should_send": bool(selected),
    }
