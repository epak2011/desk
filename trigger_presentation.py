"""Explain saved entry evidence without inventing a trigger or changing rules."""
from position_policy import finite_number


def entry_trigger_display(receipt, rule=None):
    rule = rule or {}
    trigger = receipt.get("trigger") or {}
    raw = rule.get("trigger") or {}
    levels = raw.get("levels") or {}
    level = finite_number(trigger.get("price") or levels.get("buy_above") or rule.get("trigger_price"))
    text = str(trigger.get("text") or raw.get("detail") or raw.get("summary") or "")
    action = str(receipt.get("action") or rule.get("action") or "").lower()
    blocked = action in {"hold_off", "avoid", "watch"}
    constraint = rule.get("reward_risk_gate_reason") or receipt.get("primary_risk")
    if action in {"enter", "enter_now", "accumulate"}:
        return {"status": "defined", "price": level, "text": text,
                "entry_blocked": False, "blocking_reason": constraint}
    if level is None and (not text or text.lower().startswith("watch trigger near")):
        return {"status": "undefined", "price": None,
                "text": "No entry trigger is defined. Wait for a new actionable setup.",
                "entry_blocked": blocked, "blocking_reason": constraint}
    price = finite_number(receipt.get("price") or rule.get("price"))
    kind = str(raw.get("kind") or "")
    reached = (level is not None and price is not None and price >= level and
               kind in {"breakout", "fast_momentum", "coil_break", "reclaim_ma50"})
    if reached and blocked:
        text = f"Price is at/above ${level:,.2f}; price alone is not entry confirmation."
        if constraint:
            text += f" Entry remains blocked: {constraint}"
    return {"status": "level_reached" if reached else "defined", "price": level,
            "text": text, "entry_blocked": blocked, "blocking_reason": constraint}
