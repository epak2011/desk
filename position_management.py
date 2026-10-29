"""Shared existing-long-position presentation, separate from entry decisions.

Extracted unchanged from Streamlit; advisory trims are not backtest exits.
"""
import position_policy

def position_management_read(entry, t):
    """Exit/take-profit read for an already-entered long position."""
    def _num(value):
        try:
            if value is None:
                return None
            return position_policy.finite_number(value)
        except (TypeError, ValueError):
            return None

    price = _num(t.get("price"))
    entry_px = _num(entry.get("entry_hit_price")) or _num(entry.get("entry_price")) or _num(entry.get("price"))
    stop_px = _num(entry.get("stop_price"))
    target_px = _num(entry.get("target1_price"))
    ma20 = _num(t.get("ma20"))
    ma50 = _num(t.get("ma50"))
    ma200 = _num(t.get("ma200"))
    rsi = _num(t.get("rsi14"))
    earnings_days = t.get("event_risk_days")

    if price is None or entry_px is None:
        return None
    if entry_px <= 0:
        return None

    entry_ratio = entry_px / price if price else None
    if entry_ratio is not None and (entry_ratio < 0.20 or entry_ratio > 5.0):
        source = str(entry.get("source") or "position").lower()
        action = "Fix holding entry" if source == "holding" else "Confirm entry"
        summary = (
            f"The saved {source} entry (${entry_px:,.2f}) is far away from the current "
            f"price (${price:,.2f}), so the app will not calculate P/L or trim/sell logic from it."
        )
        return {
            "action": action,
            "emoji": "⚠️",
            "color": "var(--color-warning-text)",
            "summary": summary,
            "stats": [
                ("Now", f"${price:,.2f}"),
                ("Saved entry", f"${entry_px:,.2f}"),
                ("P/L", "not shown"),
            ],
            "pnl_pct": None,
            "r_multiple": None,
            "target_hit": False,
            "stop_broken": False,
        }

    pnl_pct = (price / entry_px - 1) * 100
    risk_per_share = entry_px - stop_px if stop_px is not None else None
    r_multiple = None
    if risk_per_share and risk_per_share > 0:
        r_multiple = (price - entry_px) / risk_per_share

    target_gap_pct = None
    if target_px:
        target_gap_pct = (target_px / price - 1) * 100
    stop_gap_pct = None
    if stop_px:
        stop_gap_pct = (price / stop_px - 1) * 100

    extended_vs_20 = bool(ma20 and price > ma20 * 1.08)
    extended_vs_50 = bool(ma50 and price > ma50 * 1.15)
    trend_intact = bool(
        (ma50 is None or price >= ma50) and
        (ma200 is None or price >= ma200)
    )
    target_hit = bool(target_px and price >= target_px)
    stop_broken = bool(stop_px and price <= stop_px)
    near_stop = bool(stop_gap_pct is not None and stop_gap_pct <= 3)
    hot_rsi = bool(rsi and rsi >= 70)
    setup_action = t.get("action")
    trend_broken = bool(
        (ma50 is not None and price < ma50) and
        (ma200 is not None and price < ma200)
    )

    exit_reason = position_policy.full_exit_reason(t, stop_px)
    if exit_reason == "invalidation":
        action = "Exit"
        emoji = "⛔"
        color = "var(--color-negative)"
        summary = "Stop is broken. Close the position unless you are deliberately re-underwriting the thesis."
    elif exit_reason == "structural":
        action = "Exit"
        emoji = "⛔"
        color = "var(--color-negative)"
        summary = "The position is now in avoid territory with trend support broken. Exit or deliberately re-underwrite from scratch."
    elif earnings_days is not None and 0 <= earnings_days <= 2:
        action = "Review after earnings"
        emoji = "📅"
        color = "var(--color-warning-text)"
        summary = "Earnings are imminent. Do not add before the print; decide whether to trim risk or hold through the event deliberately."
    elif target_hit and (extended_vs_20 or hot_rsi):
        action = "Take profit"
        emoji = "💰"
        color = "var(--color-positive)"
        summary = "Target is hit and the move is extended. Trim 1/3 to 1/2, then trail the rest."
    elif target_hit:
        action = "Take profit"
        emoji = "💰"
        color = "var(--color-positive)"
        summary = "Target is hit. Take some profit or raise the stop so the trade cannot turn into a loss."
    elif r_multiple is not None and r_multiple >= 2 and (extended_vs_50 or hot_rsi):
        action = "Trim"
        emoji = "✂️"
        color = "var(--color-warning-text)"
        summary = "You have more than 2R and the stock is stretched. Bank a partial and trail the balance."
    elif r_multiple is not None and r_multiple >= 1.2 and trend_intact:
        action = "Raise stop"
        emoji = "🔒"
        color = "var(--color-warning-text)"
        summary = "The trade is working. Raise the stop toward breakeven or a higher technical level so gains are protected."
    elif near_stop:
        action = "Respect stop"
        emoji = "⚠️"
        color = "var(--color-warning-text)"
        summary = "Price is close to the stop. Do not add; either let the stop work or reduce risk now."
    elif pnl_pct > 0 and ma20 and price < ma20:
        action = "Raise stop"
        emoji = "🔒"
        color = "var(--color-warning-text)"
        summary = "The trade is profitable but short-term trend is slipping. Tighten the stop or trim."
    elif trend_intact:
        action = "Hold"
        emoji = "🟢"
        color = "var(--color-positive)"
        summary = "Trend remains intact. Hold the position and keep the stop/target live."
    else:
        action = "Check exit"
        emoji = "⚠️"
        color = "var(--color-warning-text)"
        summary = "Not an automatic sell, but trend support is weakening. Check the stop, sizing, and whether this still deserves capital."

    stats = [
        ("Entry", f"${entry_px:,.2f}"),
        ("Now", f"${price:,.2f}"),
        ("P/L", f"{pnl_pct:+.1f}%"),
    ]
    if r_multiple is not None:
        stats.append(("R", f"{r_multiple:.1f}R"))
    if target_px is not None:
        stats.append(("Target", f"${target_px:,.2f}"))
        if target_gap_pct is not None:
            stats.append(("To target", f"{target_gap_pct:+.1f}%"))
    if stop_px is not None:
        stats.append(("Stop", f"${stop_px:,.2f}"))
        if stop_gap_pct is not None:
            stats.append(("Stop room", f"{stop_gap_pct:.1f}%"))

    return {
        "action": action,
        "emoji": emoji,
        "color": color,
        "summary": summary,
        "pnl_pct": pnl_pct,
        "r_multiple": r_multiple,
        "target_hit": target_hit,
        "near_stop": near_stop,
        "stats": stats,
    }
