"""Portfolio-aware sizing overlays that never change the underlying rules action."""

from __future__ import annotations


def _number(value, default=None):
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def portfolio_recommendation(
    ticker,
    state,
    holdings,
    prices,
    sectors,
    *,
    account_size,
    risk_per_trade,
    max_position_pct,
    sector_limit_pct=0.35,
):
    """Translate a rules call into a portfolio-cap-aware sizing recommendation."""
    ticker = str(ticker or "").upper()
    state = state if isinstance(state, dict) else {}
    holdings = holdings if isinstance(holdings, dict) else {}
    prices = prices if isinstance(prices, dict) else {}
    sectors = sectors if isinstance(sectors, dict) else {}
    account = max(_number(account_size, 0) or 0, 0)
    action = str(state.get("action") or "").lower()
    sector = str(sectors.get(ticker) or "Unknown")

    position_values = {}
    for held_ticker, holding in holdings.items():
        held_ticker = str(held_ticker).upper()
        holding = holding if isinstance(holding, dict) else {}
        shares = _number(holding.get("shares"), 0) or 0
        price = _number(prices.get(held_ticker)) or _number(holding.get("entry_price"), 0) or 0
        position_values[held_ticker] = max(0, shares * price)
    current_value = position_values.get(ticker, 0)
    current_weight = current_value / account if account else 0
    sector_value = sum(
        value for held_ticker, value in position_values.items()
        if str(sectors.get(held_ticker) or "Unknown") == sector
    )
    sector_weight = sector_value / account if account else 0
    holdings_tracked = sum(1 for value in position_values.values() if value > 0)

    no_entry = action not in {"enter_now", "accumulate"}
    if no_entry:
        return {
            "suggested_weight_pct": 0.0,
            "incremental_weight_pct": 0.0,
            "label": "0% — wait for an actionable call" if action != "avoid" else "0% — avoid",
            "reason": "The rules action is not currently actionable.",
            "current_weight_pct": round(current_weight * 100, 2),
            "sector_weight_pct": round(sector_weight * 100, 2),
            "sector": sector,
            "holdings_tracked": holdings_tracked,
            "concentration_flag": False,
        }

    size = str(state.get("entry_size") or "starter").lower()
    fraction = {"starter": 0.25, "normal": 0.5, "full": 1.0}.get(size, 0.25)
    target_cap = max(0, _number(max_position_pct, 0.25) or 0.25)
    base_target = target_cap * fraction
    entry = _number(state.get("entry") or state.get("price"))
    stop = _number(state.get("stop"))
    risk_cap = base_target
    if entry and stop and entry > stop and account:
        stop_pct = (entry - stop) / entry
        risk_cap = min(base_target, (_number(risk_per_trade, 0.01) or 0.01) / stop_pct)
    sector_room = max(0, sector_limit_pct - sector_weight)
    position_room = max(0, target_cap - current_weight)
    incremental = min(risk_cap, sector_room, position_room)
    concentration_flag = sector_room <= 0 or position_room <= 0
    suggested_total = current_weight + max(0, incremental)
    if concentration_flag:
        label = "0% add — concentration limit"
        reason = (
            f"No additional room: {sector} is {sector_weight * 100:.1f}% of the account."
            if sector_room <= 0
            else f"Position is already at the {target_cap * 100:.0f}% maximum."
        )
    elif incremental <= 0:
        label = "0% add — no risk budget"
        reason = "The stop-based risk budget leaves no incremental room."
    else:
        label = f"Add {incremental * 100:.1f}% · target {suggested_total * 100:.1f}%"
        reason = (
            f"Capped by position, {sector} exposure, and stop-based risk budget; "
            f"current position is {current_weight * 100:.1f}%."
        )
    return {
        "suggested_weight_pct": round(suggested_total * 100, 2),
        "incremental_weight_pct": round(max(0, incremental) * 100, 2),
        "label": label,
        "reason": reason,
        "current_weight_pct": round(current_weight * 100, 2),
        "sector_weight_pct": round(sector_weight * 100, 2),
        "sector": sector,
        "holdings_tracked": holdings_tracked,
        "concentration_flag": concentration_flag,
    }
