"""Deterministic prioritization for Trading Desk's daily attention inbox."""

from __future__ import annotations

import hashlib


PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _number(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _event(kind, ticker, priority, title, detail, action=""):
    identity = f"{kind}|{str(ticker).upper()}|{action}|{detail}"
    return {
        "id": hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12],
        "kind": kind,
        "ticker": str(ticker or "").upper(),
        "priority": priority,
        "title": title,
        "detail": detail,
        "action": action,
    }


def build_attention_events(rows, *, holdings=None, logic_alerts=None, contract_mismatches=None):
    """Return de-duplicated, prioritized events from canonical ticker rows."""
    holdings = {str(t).upper() for t in (holdings or [])}
    events = []
    for alert in logic_alerts or []:
        status = str(alert.get("status") or "watch")
        label = str(alert.get("label") or "Rules family")
        count = int(alert.get("count") or 0)
        success = alert.get("success_rate_pct")
        edge = alert.get("avg_decision_return_pct")
        events.append(_event(
            "logic_review",
            "ENGINE",
            "critical" if status == "review_logic" else "high",
            "Logic review recommended" if status == "review_logic" else "Rules evidence needs attention",
            f"{label}: {success}% success, {edge:+.1f}% decision return, n={count}.",
            status,
        ))
    for mismatch in contract_mismatches or []:
        events.append(_event(
            "contract_mismatch",
            mismatch.get("ticker"),
            "critical",
            "Decision surfaces disagree",
            f'{mismatch.get("surface")} shows {mismatch.get("observed")}; receipt expects {mismatch.get("expected")}.',
        ))
    for row in rows or []:
        ticker = str(row.get("ticker") or "").upper()
        action = str(row.get("action") or "").lower()
        price = _number(row.get("price"))
        invalidation = _number(row.get("invalidation_price"))
        receipt = row.get("receipt") if isinstance(row.get("receipt"), dict) else {}
        trust = receipt.get("data_trust") if isinstance(receipt.get("data_trust"), dict) else {}
        if trust.get("status") == "blocked":
            events.append(_event(
                "data_blocked", ticker, "critical", "Decision data is not executable",
                " ".join(trust.get("blocked_reasons") or []) or "Refresh required market inputs.", action,
            ))
        changes = " ".join(receipt.get("change_summary") or [])
        if "Action changed" in changes:
            events.append(_event("action_change", ticker, "high", "Action changed", changes, action))
        if invalidation is not None and price is not None and price <= invalidation:
            events.append(_event(
                "invalidation",
                ticker,
                "critical",
                "Invalidation breached",
                f"Price ${price:,.2f} is at or below ${invalidation:,.2f}.",
                action,
            ))
        trigger_status = str(row.get("trigger_status") or "").lower()
        if trigger_status in {"fired", "hit", "now"} or action == "enter_now":
            events.append(_event(
                "trigger_fired",
                ticker,
                "high",
                "Entry trigger is actionable",
                str(row.get("trigger_detail") or "The rules entry condition has fired."),
                action,
            ))
        elif trigger_status == "near" or (
            _number(row.get("distance_pct")) is not None and abs(_number(row.get("distance_pct"))) <= 3
        ):
            events.append(_event(
                "near_trigger", ticker, "medium", "Near entry trigger",
                str(row.get("trigger_detail") or "Price is within 3% of the decision trigger."), action,
            ))
        earnings_days = row.get("earnings_days")
        try:
            earnings_days = int(earnings_days)
        except (TypeError, ValueError):
            earnings_days = None
        if earnings_days is not None and 0 <= earnings_days <= 7:
            events.append(_event(
                "earnings", ticker, "high" if earnings_days <= 2 else "medium",
                "Earnings risk approaching",
                f"Earnings are due in {earnings_days} day{'s' if earnings_days != 1 else ''}.", action,
            ))
        if ticker in holdings and action in {"avoid", "hold_off"}:
            events.append(_event(
                "position_review", ticker, "high", "Owned position needs review",
                f"The current rules action is {action.replace('_', ' ')} while this ticker is held.", action,
            ))
    unique = {event["id"]: event for event in events}
    return sorted(unique.values(), key=lambda event: (PRIORITY_RANK.get(event["priority"], 9), event["ticker"], event["kind"]))
