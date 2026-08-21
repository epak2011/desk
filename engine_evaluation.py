"""Deterministic forward evaluation for Trading Desk rules decisions."""

from __future__ import annotations

from datetime import date, datetime, timedelta


EVALUATION_VERSION = 3
DEFAULT_HORIZON_DAYS = 14


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
    horizon_days=DEFAULT_HORIZON_DAYS,
):
    """Score one decision at a fixed calendar horizon without look-ahead drift.

    The exit is the first available close on or after ``logged + horizon``.
    MFE/MAE use only bars available through that exit. Returns are decimals.
    """
    logged = _entry_date(entry)
    if logged is None:
        return None
    as_of = as_of or date.today()
    if isinstance(as_of, datetime):
        as_of = as_of.date()
    target = logged + timedelta(days=int(horizon_days))
    if as_of < target:
        return None

    history = _dated_frame(history)
    if history is None:
        return None
    try:
        eligible = history[history.index.date >= target]
    except Exception:
        return None
    if len(eligible) == 0:
        return None
    exit_row = eligible.iloc[0]
    exit_index = eligible.index[0]
    exit_date = exit_index.date() if hasattr(exit_index, "date") else target
    if exit_date > as_of:
        return None

    ref_price = number_or_none(entry.get("price"))
    if not ref_price or ref_price <= 0:
        return None
    exit_price = number_or_none(exit_row.get("Close"))
    if exit_price is None:
        return None
    forward_return = (exit_price - ref_price) / ref_price

    window = _rows_between(history, logged, exit_date)
    if window is None or len(window) == 0:
        return None
    high = number_or_none(window["High"].max()) if "High" in window else None
    low = number_or_none(window["Low"].min()) if "Low" in window else None
    mfe = (high - ref_price) / ref_price if high is not None else None
    mae = (low - ref_price) / ref_price if low is not None else None

    benchmark_return = _benchmark_return(benchmark_history, logged, exit_date)
    excess_return = (
        forward_return - benchmark_return
        if benchmark_return is not None
        else None
    )
    family = decision_family(entry.get("rule_action"))
    if forward_return >= 0.03:
        winning_family = "long"
    elif forward_return <= -0.03:
        winning_family = "avoid"
    else:
        winning_family = "wait"
    credited = (
        family == winning_family
        or (family == "avoid" and winning_family == "wait")
    )
    return {
        "evaluation_version": EVALUATION_VERSION,
        "horizon_days": int(horizon_days),
        "logged_date": logged.isoformat(),
        "target_date": target.isoformat(),
        "scored_date": exit_date.isoformat(),
        "reference_price": round(ref_price, 4),
        "scored_price": round(exit_price, 4),
        "forward_return_pct": round(forward_return * 100, 4),
        "benchmark_return_pct": (
            round(benchmark_return * 100, 4)
            if benchmark_return is not None else None
        ),
        "excess_return_pct": (
            round(excess_return * 100, 4)
            if excess_return is not None else None
        ),
        "mfe_pct": round(mfe * 100, 4) if mfe is not None else None,
        "mae_pct": round(mae * 100, 4) if mae is not None else None,
        "rule_family": family,
        "winning_family": winning_family,
        "credited": credited,
    }


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
            "hit_rate_pct": None,
            "avg_return_pct": None,
            "avg_excess_return_pct": None,
            "avg_mfe_pct": None,
            "avg_mae_pct": None,
        }

    def average(key):
        values = [number_or_none(row.get(key)) for row in outcomes]
        values = [value for value in values if value is not None]
        return round(sum(values) / len(values), 2) if values else None

    return {
        "count": len(outcomes),
        "hit_rate_pct": round(
            100 * sum(bool(row.get("credited")) for row in outcomes) / len(outcomes),
            1,
        ),
        "avg_return_pct": average("forward_return_pct"),
        "avg_excess_return_pct": average("excess_return_pct"),
        "avg_mfe_pct": average("mfe_pct"),
        "avg_mae_pct": average("mae_pct"),
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
