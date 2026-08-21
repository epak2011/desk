"""Deterministic forward evaluation for Trading Desk rules decisions."""

from __future__ import annotations

from datetime import date, datetime, timedelta


EVALUATION_VERSION = 5
DEFAULT_HORIZONS = (5, 14, 30)


def number_or_none(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def decision_family(action):
    action = str(action or "").upper().replace("_NOW", "").replace("_", " ")
    if action in {"ENTER", "ACCUMULATE"}:
        return "long"
    if action == "AVOID":
        return "avoid"
    return "wait"


def _entry_date(entry):
    try:
        return datetime.fromisoformat(str(entry.get("ts")).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def _dated_frame(frame):
    if frame is None or len(frame) == 0 or "Close" not in frame:
        return None
    cleaned = frame.copy()
    try:
        cleaned = cleaned.sort_index()
        cleaned = cleaned[~cleaned.index.duplicated(keep="last")]
    except Exception:
        return None
    return cleaned


def _rows_between(frame, start_date, end_date):
    try:
        dates = frame.index.date
        return frame[(dates >= start_date) & (dates <= end_date)]
    except Exception:
        return None


def _benchmark_return(frame, start_date, end_date):
    frame = _dated_frame(frame)
    if frame is None:
        return None
    rows = _rows_between(frame, start_date, end_date)
    if rows is None or len(rows) < 2:
        return None
    start = number_or_none(rows["Close"].iloc[0])
    end = number_or_none(rows["Close"].iloc[-1])
    if not start or end is None:
        return None
    return (end - start) / start


def score_forward_outcome(
    entry,
    history,
    *,
    benchmark_history=None,
    as_of=None,
    horizons=DEFAULT_HORIZONS,
):
    """Evaluate the price path, trigger lifecycle, and fixed trading-session returns."""
    logged = _entry_date(entry)
    if logged is None:
        return None
    as_of = as_of or date.today()
    if isinstance(as_of, datetime):
        as_of = as_of.date()
    history = _dated_frame(history)
    if history is None:
        return None
    try:
        future = history[(history.index.date > logged) & (history.index.date <= as_of)]
    except Exception:
        return None
    if len(future) == 0:
        return None

    ref_price = number_or_none(entry.get("price"))
    if not ref_price or ref_price <= 0:
        return None
    family = decision_family(entry.get("rule_action"))
    horizon_results = {}
    for horizon in sorted({int(value) for value in horizons if int(value) > 0}):
        if len(future) < horizon:
            continue
        exit_row = future.iloc[horizon - 1]
        exit_index = future.index[horizon - 1]
        exit_date = exit_index.date()
        exit_price = number_or_none(exit_row.get("Close"))
        if exit_price is None:
            continue
        forward_return = (exit_price - ref_price) / ref_price
        window = future.iloc[:horizon]
        high = number_or_none(window["High"].max()) if "High" in window else None
        low = number_or_none(window["Low"].min()) if "Low" in window else None
        benchmark_return = _benchmark_return(benchmark_history, logged, exit_date)
        excess_return = forward_return - benchmark_return if benchmark_return is not None else None
        horizon_results[str(horizon)] = {
            "sessions": horizon,
            "scored_date": exit_date.isoformat(),
            "scored_price": round(exit_price, 4),
            "return_pct": round(forward_return * 100, 4),
            "benchmark_return_pct": round(benchmark_return * 100, 4) if benchmark_return is not None else None,
            "excess_return_pct": round(excess_return * 100, 4) if excess_return is not None else None,
            "mfe_pct": round((high - ref_price) / ref_price * 100, 4) if high is not None else None,
            "mae_pct": round((low - ref_price) / ref_price * 100, 4) if low is not None else None,
        }

    trigger_price = number_or_none(entry.get("trigger_price"))
    invalidation_price = number_or_none(entry.get("invalidation_price") or entry.get("stop_price"))

    def first_close_event(level, direction):
        if level is None:
            return None
        for idx, row in future.iterrows():
            close = number_or_none(row.get("Close"))
            if close is None:
                continue
            if (direction == "up" and close >= level) or (direction == "down" and close <= level):
                return {"date": idx.date().isoformat(), "price": round(close, 4), "index": idx}
        return None

    trigger_event = first_close_event(trigger_price, "up")
    invalidation_event = first_close_event(invalidation_price, "down")
    trigger_first = bool(
        trigger_event and (
            not invalidation_event or trigger_event["date"] <= invalidation_event["date"]
        )
    )
    invalidation_first = bool(invalidation_event and not trigger_first)

    post_trigger = {}
    if trigger_event:
        after_trigger = future[future.index > trigger_event["index"]]
        trigger_ref = trigger_price or trigger_event["price"]
        for horizon in (5, 14):
            if len(after_trigger) < horizon or not trigger_ref:
                continue
            row = after_trigger.iloc[horizon - 1]
            value = number_or_none(row.get("Close"))
            if value is not None:
                post_trigger[str(horizon)] = round((value - trigger_ref) / trigger_ref * 100, 4)

    primary = horizon_results.get("14") or {}
    forward_return_pct = primary.get("return_pct")
    excess_return_pct = primary.get("excess_return_pct")
    directional_success = None
    success_definition = None
    if family == "long" and forward_return_pct is not None:
        directional_success = bool(
            forward_return_pct > 0
            and (excess_return_pct is None or excess_return_pct > 0)
        )
        success_definition = "positive 14-session return and SPY outperformance when benchmarked"
    elif family == "avoid" and forward_return_pct is not None:
        directional_success = bool(
            forward_return_pct < 0
            or (excess_return_pct is not None and excess_return_pct <= -2)
        )
        success_definition = "negative 14-session return or at least 2 points of SPY underperformance"

    patience_status = None
    patience_success = None
    if family == "wait":
        if invalidation_first:
            patience_status, patience_success = "invalidation_before_trigger", True
        elif trigger_first:
            trigger_14 = post_trigger.get("14")
            patience_status = "triggered_then_matured" if trigger_14 is not None else "triggered_pending"
            patience_success = (trigger_14 > 0) if trigger_14 is not None else None
        elif "30" in horizon_results:
            patience_status, patience_success = "expired_without_trigger", None
        else:
            patience_status, patience_success = "waiting"

    decision_return_pct = None
    decision_excess_pct = None
    if forward_return_pct is not None and family in {"long", "avoid"}:
        direction = 1 if family == "long" else -1
        decision_return_pct = round(direction * forward_return_pct, 4)
        decision_excess_pct = round(direction * excess_return_pct, 4) if excess_return_pct is not None else None

    result = {
        "evaluation_version": EVALUATION_VERSION,
        "horizon_unit": "trading_sessions",
        "logged_date": logged.isoformat(),
        "reference_price": round(ref_price, 4),
        "horizons": horizon_results,
        "forward_return_pct": forward_return_pct,
        "benchmark_return_pct": primary.get("benchmark_return_pct"),
        "excess_return_pct": excess_return_pct,
        "decision_return_pct": decision_return_pct,
        "decision_excess_pct": decision_excess_pct,
        "mfe_pct": primary.get("mfe_pct"),
        "mae_pct": primary.get("mae_pct"),
        "rule_family": family,
        "directional_success": directional_success,
        "success_definition": success_definition,
        "patience_status": patience_status,
        "patience_success": patience_success,
        "trigger_fired": bool(trigger_event),
        "trigger_date": trigger_event.get("date") if trigger_event else None,
        "invalidation_fired": bool(invalidation_event),
        "invalidation_date": invalidation_event.get("date") if invalidation_event else None,
        "event_order": "trigger_first" if trigger_first else "invalidation_first" if invalidation_first else "unresolved",
        "post_trigger_returns_pct": post_trigger,
        "evaluation_complete": "30" in horizon_results,
    }
    result["credited"] = directional_success if family in {"long", "avoid"} else patience_success
    return result


def summarize_outcomes(entries):
    outcomes = [
        entry.get("outcome") or {}
        for entry in entries or []
        if isinstance(entry.get("outcome"), dict)
        and entry.get("outcome", {}).get("forward_return_pct") is not None
    ]
    if not outcomes:
        return {
            "count": 0,
            "resolved_count": 0,
            "successful_count": 0,
            "hit_rate_pct": None,
            "avg_return_pct": None,
            "avg_excess_return_pct": None,
            "avg_decision_return_pct": None,
            "avg_decision_excess_pct": None,
            "avg_mfe_pct": None,
            "avg_mae_pct": None,
            "avg_5d_return_pct": None,
            "avg_30d_return_pct": None,
        }

    def average(key):
        values = [number_or_none(row.get(key)) for row in outcomes]
        values = [value for value in values if value is not None]
        return round(sum(values) / len(values), 2) if values else None

    resolved = [row for row in outcomes if row.get("credited") is not None]

    def horizon_average(horizon):
        values = [
            number_or_none(((row.get("horizons") or {}).get(str(horizon)) or {}).get("return_pct"))
            for row in outcomes
        ]
        values = [value for value in values if value is not None]
        return round(sum(values) / len(values), 2) if values else None

    return {
        "count": len(outcomes),
        "resolved_count": len(resolved),
        "successful_count": sum(bool(row.get("credited")) for row in resolved),
        "hit_rate_pct": (
            round(100 * sum(bool(row.get("credited")) for row in resolved) / len(resolved), 1)
            if resolved else None
        ),
        "avg_return_pct": average("forward_return_pct"),
        "avg_excess_return_pct": average("excess_return_pct"),
        "avg_decision_return_pct": average("decision_return_pct"),
        "avg_decision_excess_pct": average("decision_excess_pct"),
        "avg_mfe_pct": average("mfe_pct"),
        "avg_mae_pct": average("mae_pct"),
        "avg_5d_return_pct": horizon_average(5),
        "avg_30d_return_pct": horizon_average(30),
    }


def independent_cohorts(entries, spacing_days=7):
    """Return one observation per ticker/action family in each spacing window."""
    dated = []
    for entry in entries or []:
        logged = _entry_date(entry)
        ticker = str(entry.get("ticker") or "").upper().strip()
        if logged and ticker:
            dated.append((logged, ticker, decision_family(entry.get("rule_action")), entry))
    dated.sort(key=lambda item: item[0])
    selected = []
    last_by_key = {}
    for logged, ticker, family, entry in dated:
        key = (ticker, family)
        last = last_by_key.get(key)
        if last is not None and (logged - last).days < int(spacing_days):
            continue
        selected.append(entry)
        last_by_key[key] = logged
    return selected


def summarize_patience(entries):
    outcomes = [
        entry.get("outcome") or {}
        for entry in entries or []
        if isinstance(entry.get("outcome"), dict)
        and entry.get("outcome", {}).get("rule_family") == "wait"
    ]
    statuses = {}
    for outcome in outcomes:
        status = str(outcome.get("patience_status") or "waiting")
        statuses[status] = statuses.get(status, 0) + 1
    resolved = [row for row in outcomes if row.get("patience_success") is not None]
    successful = sum(bool(row.get("patience_success")) for row in resolved)
    return {
        "count": len(outcomes),
        "resolved_count": len(resolved),
        "successful_count": successful,
        "success_rate_pct": round(100 * successful / len(resolved), 1) if resolved else None,
        "triggered": sum(bool(row.get("trigger_fired")) for row in outcomes),
        "invalidation_first": statuses.get("invalidation_before_trigger", 0),
        "triggered_pending": statuses.get("triggered_pending", 0),
        "triggered_matured": statuses.get("triggered_then_matured", 0),
        "expired": statuses.get("expired_without_trigger", 0),
        "waiting": statuses.get("waiting", 0),
    }


def failure_patterns(entries, minimum_count=3):
    """Rank sufficiently populated decision-time attributes by decision return."""
    groups = {}
    for entry in entries or []:
        outcome = entry.get("outcome") or {}
        decision_return = number_or_none(outcome.get("decision_return_pct"))
        if decision_return is None:
            continue
        context = entry.get("decision_context") or {}
        setup = number_or_none(entry.get("setup_score"))
        reward_risk = number_or_none(entry.get("reward_risk"))
        attributes = [
            ("Action", str(entry.get("rule_action") or "unknown").replace("_", " ").title()),
            ("Structure", str(entry.get("rule_state") or "unknown").title()),
        ]
        regime = context.get("market_regime")
        if regime:
            attributes.append(("Regime", str(regime).title()))
        if setup is not None:
            bucket = "Setup ≥ 8" if setup >= 8 else "Setup 6-8" if setup >= 6 else "Setup < 6"
            attributes.append(("Setup", bucket))
        if reward_risk is not None:
            bucket = "R/R ≥ 2" if reward_risk >= 2 else "R/R 1-2" if reward_risk >= 1 else "R/R < 1"
            attributes.append(("Reward/risk", bucket))
        for dimension, value in attributes:
            groups.setdefault((dimension, value), []).append((decision_return, bool(outcome.get("directional_success"))))
    patterns = []
    for (dimension, value), rows in groups.items():
        if len(rows) < int(minimum_count):
            continue
        patterns.append({
            "dimension": dimension,
            "value": value,
            "count": len(rows),
            "successes": sum(success for _ret, success in rows),
            "avg_decision_return_pct": round(sum(ret for ret, _success in rows) / len(rows), 2),
        })
    return sorted(patterns, key=lambda row: (row["avg_decision_return_pct"], -row["count"]))
