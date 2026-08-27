"""Presentation-neutral attention digest generation.

Delivery is intentionally separate: this module never sends email.  A future
scheduled worker can pass this payload to a configured provider with idempotency.
"""

from __future__ import annotations

import hashlib
import html
from datetime import date
from typing import Iterable, Mapping


def digest_events(events: Iterable[Mapping], *, include_medium: bool = False) -> list[dict]:
    priorities = {"critical", "high"} | ({"medium"} if include_medium else set())
    return [
        {
            key: (event.get("event_id") or event.get("id")) if key == "event_id" else event.get(key)
            for key in ("event_id", "ticker", "kind", "priority", "title", "detail")
            if key in event
        }
        for event in events
        if str(event.get("priority") or "").lower() in priorities
    ]


def build_digest(
    user_id: str,
    events: Iterable[Mapping],
    *,
    day: date | None = None,
    channel: str = "daily",
) -> dict:
    digest_day = day or date.today()
    selected = digest_events(events)
    # Daily delivery is one message per user/day even if events change while a
    # later worker run is executing. Immediate alerts use a distinct channel.
    identity = "|".join([str(user_id), digest_day.isoformat(), str(channel)])
    return {
        "digest_key": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
        "day": digest_day.isoformat(),
        "events": selected,
        "count": len(selected),
        "should_send": bool(selected),
    }


def render_digest_html(events: Iterable[Mapping], *, unsubscribe_url: str) -> str:
    rows = []
    for event in digest_events(events):
        rows.append(
            '<tr><td style="padding:14px 0;border-top:1px solid #e5e7eb;vertical-align:top;">'
            f'<div style="font-size:12px;font-weight:800;color:#64748b;">{html.escape(str(event.get("ticker") or "SYSTEM"))}</div>'
            f'<div style="font-size:16px;font-weight:800;color:#111827;margin-top:3px;">{html.escape(str(event.get("title") or "Decision update"))}</div>'
            f'<div style="font-size:13px;line-height:1.45;color:#475569;margin-top:4px;">{html.escape(str(event.get("detail") or ""))}</div>'
            '</td></tr>'
        )
    return (
        '<div style="max-width:640px;margin:auto;font-family:Arial,sans-serif;color:#111827;">'
        '<div style="font-size:11px;font-weight:800;letter-spacing:1.4px;text-transform:uppercase;color:#64748b;">Trading Desk</div>'
        '<h1 style="font-size:26px;margin:8px 0 14px;">Items needing attention</h1>'
        '<table role="presentation" style="width:100%;border-collapse:collapse;">'
        + "".join(rows)
        + '</table><p style="font-size:11px;color:#94a3b8;margin-top:26px;line-height:1.5;">'
        'Decision support only; verify market data independently. '
        f'<a href="{html.escape(unsubscribe_url, quote=True)}">Unsubscribe</a>.</p></div>'
    )
