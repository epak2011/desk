"""Deterministic forward evaluation for Trading Desk rules decisions."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
import math
import os
from functools import lru_cache
from pathlib import Path
from statistics import median


EVALUATION_VERSION = 6
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


def calibration_eligible(entry):
    """Whether a scored row is safe to use for rule calibration.

    Legacy rows remain eligible; newly scored rows carry an explicit integrity
    result so incomplete benchmark/path data cannot silently tune live rules.
    """
    outcome = entry.get("outcome") if isinstance(entry, dict) else None
    if not isinstance(outcome, dict):
        return False
    integrity = outcome.get("integrity")
    return not isinstance(integrity, dict) or bool(integrity.get("calibration_eligible"))


def shadow_outcomes(entry, primary):
    """Score recorded shadow actions on the exact same matured price path."""
    rows = entry.get("shadow_evaluations") if isinstance(entry, dict) else []
    forward = number_or_none(primary.get("forward_return_pct"))
    excess = number_or_none(primary.get("excess_return_pct"))
    results = []
    if forward is None:
        return results
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if row.get("eligible") is False:
            continue
        family = decision_family(row.get("action"))
        evaluation_mode = str(row.get("evaluation_mode") or "directional")
        if evaluation_mode == "cash_control":
            # Waiting holds cash, not a synthetic short position. Do not
            # count a flat cash control as a successful directional call.
            decision_return = 0.0
            success = None
        elif evaluation_mode == "avoided_long_exposure":
            # A filtered Enter/Accumulate earns positive counterfactual return
            # when it avoids a subsequent loss and negative return when it
            # sidelines a winner.  This makes timing/regime filters directly
            # comparable with the live long decision on the identical path.
            decision_return = round(-forward, 4)
            success = bool(forward < 0)
        elif family not in {"long", "avoid"}:
            success = None
            decision_return = None
        else:
            direction = 1 if family == "long" else -1
            decision_return = round(direction * forward, 4)
            success = (
                bool(forward > 0 and (excess is None or excess > 0))
                if family == "long"
                else bool(forward < 0 or (excess is not None and excess <= -2))
            )
        results.append({
            "candidate": str(row.get("candidate") or "unknown"),
            "version": str(row.get("version") or "unknown"),
            "action": row.get("action"),
            "evaluation_mode": evaluation_mode,
            "directional_success": success,
            "decision_return_pct": decision_return,
        })
    return results


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
            patience_status, patience_success = "waiting", None

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
    missing = []
    if "14" not in horizon_results:
        missing.append("14_session_path")
    if family in {"long", "avoid"} and primary.get("benchmark_return_pct") is None:
        missing.append("14_session_benchmark")
    try:
        last_history_date = history.index[-1].date()
        if (as_of - last_history_date).days > 7:
            missing.append("stale_price_history")
    except Exception:
        missing.append("unverifiable_price_history_date")
    try:
        logged_rows = history[history.index.date <= logged]
        logged_close = number_or_none(logged_rows["Close"].iloc[-1]) if len(logged_rows) else None
        if logged_close and abs(logged_close / ref_price - 1) > 0.30:
            missing.append("reference_price_discontinuity")
    except Exception:
        pass
    result["integrity"] = {
        "calibration_eligible": not missing,
        "issues": missing,
        "price_history_adjusted": True,
        "duplicate_control": "independent_ticker_family_cohorts_7d",
    }
    result["shadow_results"] = shadow_outcomes(entry, result)
    result["credited"] = directional_success if family in {"long", "avoid"} else patience_success
    return result


def summarize_outcomes(entries):
    outcomes = [
        entry.get("outcome") or {}
        for entry in entries or []
        if isinstance(entry.get("outcome"), dict)
        and (
            entry.get("outcome", {}).get("forward_return_pct") is not None
            or bool(entry.get("outcome", {}).get("horizons"))
        )
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
    # Both clients and the worker must choose the same observation when their
    # database readers return opposite row orders or multiple calls on one day.
    dated.sort(key=lambda item: (item[0], str(item[3].get("ts") or ""), str(item[3].get("id") or "")))
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
        if not calibration_eligible(entry):
            continue
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
        if context.get("extension_warning"):
            severity = str(context.get("extension_warning_severity") or "active").title()
            attributes.append(("Entry warning", f"Stretched momentum · {severity}"))
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


def logic_review_flags(
    entries,
    *,
    watch_minimum=10,
    review_minimum=20,
    watch_success_rate=40,
    review_success_rate=35,
    poor_edge_pct=-2,
):
    """Return evidence gates for future logic review without changing live rules.

    Only independent directional cohorts with a resolved 14-session result belong
    here. A low hit rate alone is not enough: the review gate also requires
    negative direction-adjusted return, which avoids reacting to harmless misses.
    """
    flags = []
    for family, label in (("long", "Enter / Accumulate"), ("avoid", "Avoid")):
        family_entries = [
            entry for entry in entries or []
            if decision_family(entry.get("rule_action")) == family
            and calibration_eligible(entry)
            and isinstance(entry.get("outcome"), dict)
            and entry.get("outcome", {}).get("directional_success") is not None
            and number_or_none(entry.get("outcome", {}).get("decision_return_pct")) is not None
        ]
        count = len(family_entries)
        successes = sum(bool(entry["outcome"]["directional_success"]) for entry in family_entries)
        success_rate = round(100 * successes / count, 1) if count else None
        edge = (
            round(sum(number_or_none(entry["outcome"]["decision_return_pct"]) for entry in family_entries) / count, 2)
            if count else None
        )
        weak_watch = bool(
            count >= int(watch_minimum)
            and success_rate < float(watch_success_rate)
            and edge <= float(poor_edge_pct)
        )
        weak_review = bool(
            count >= int(review_minimum)
            and success_rate < float(review_success_rate)
            and edge <= float(poor_edge_pct)
        )
        if weak_review:
            status = "review_logic"
            reason = "Both success rate and decision return crossed the review threshold."
        elif weak_watch:
            status = "watch"
            reason = (
                f"Success is below {float(watch_success_rate):g}% with poor decision return. "
                f"Review logic requires at least {int(review_minimum)} observations, "
                f"success below {float(review_success_rate):g}%, and return at or below {float(poor_edge_pct):g}%."
            )
        else:
            status = "collecting" if count < int(review_minimum) else "stable"
            reason = (
                f"Collecting mature outcomes; review requires at least {int(review_minimum)}."
                if status == "collecting"
                else "The family has not crossed both weakness thresholds."
            )
        flags.append({
            "family": family,
            "label": label,
            "status": status,
            "count": count,
            "successes": successes,
            "success_rate_pct": success_rate,
            "avg_decision_return_pct": edge,
            "reason": reason,
        })
    return flags


def weakest_directional_cases(entries, limit=8):
    """Return the worst mature independent calls for human case review."""
    cases = []
    for entry in entries or []:
        if not calibration_eligible(entry):
            continue
        outcome = entry.get("outcome") or {}
        decision_return = number_or_none(outcome.get("decision_return_pct"))
        if outcome.get("directional_success") is not False or decision_return is None:
            continue
        cases.append({
            "ticker": str(entry.get("ticker") or "").upper(),
            "logged_date": outcome.get("logged_date") or (_entry_date(entry).isoformat() if _entry_date(entry) else None),
            "action": str(entry.get("rule_action") or "").replace("_", " ").title(),
            "rule_state": str(entry.get("rule_state") or "").replace("_", " ").title(),
            "decision_return_pct": round(decision_return, 2),
            "forward_return_pct": number_or_none(outcome.get("forward_return_pct")),
            "excess_return_pct": number_or_none(outcome.get("excess_return_pct")),
            "trigger_summary": str(entry.get("trigger_summary") or ""),
        })
    cases.sort(key=lambda row: row["decision_return_pct"])
    return cases[:max(0, int(limit))]


def performance_slices(entries, minimum_count=3):
    """Summarize mature directional edge by version and decision environment."""
    groups = {}
    for entry in entries or []:
        if not calibration_eligible(entry):
            continue
        outcome = entry.get("outcome") or {}
        if outcome.get("directional_success") is None:
            continue
        decision_return = number_or_none(outcome.get("decision_return_pct"))
        if decision_return is None:
            continue
        context = entry.get("decision_context") or {}
        receipt = entry.get("decision_receipt") or {}
        confidence = str(receipt.get("confidence") or "").split("·", 1)[0].strip()
        setup = number_or_none(entry.get("setup_score"))
        dimensions = [
            ("Engine", entry.get("rule_engine_version") or receipt.get("engine_version") or "Unrecorded"),
            ("Code version", entry.get("rule_code_fingerprint") or "Unrecorded"),
            ("Signal source", entry.get("rule_source") or entry.get("rule_implementation") or "Unrecorded"),
            ("Ticker", entry.get("ticker")),
            ("Action", str(entry.get("rule_action") or "").replace("_now", "").replace("_", " ").title()),
            ("Entry trigger", entry.get("trigger_kind")),
            ("Entry warning", "Unrecorded" if "extension_warning" not in context else "Extended" if context.get("extension_warning") else "No extension warning"),
            ("Regime", context.get("market_regime") or "Unrecorded"),
            ("Tape", context.get("tape_class")),
            ("Confidence", confidence or None),
            ("Setup", "≥8" if setup is not None and setup >= 8 else "6–8" if setup is not None and setup >= 6 else "<6" if setup is not None else None),
        ]
        for dimension, value in dimensions:
            if value in (None, ""):
                continue
            groups.setdefault((dimension, str(value)), []).append((bool(outcome["directional_success"]), decision_return))
    rows = []
    for (dimension, value), observations in groups.items():
        if len(observations) < int(minimum_count):
            continue
        successes = sum(success for success, _return in observations)
        rows.append({
            "dimension": dimension,
            "value": value,
            "count": len(observations),
            "successes": successes,
            "success_rate_pct": round(100 * successes / len(observations), 1),
            "avg_decision_return_pct": round(sum(ret for _success, ret in observations) / len(observations), 2),
        })
    return sorted(rows, key=lambda row: (row["dimension"], -row["count"], row["value"]))


def confidence_calibration(entries, minimum_count=5):
    """Measure whether higher displayed confidence corresponds to better outcomes."""
    rows = [row for row in performance_slices(entries, minimum_count=minimum_count) if row["dimension"] == "Confidence"]
    by_label = {str(row["value"]).lower(): row for row in rows}
    high = by_label.get("high")
    low = by_label.get("low")
    calibrated = None
    note = "Need mature outcomes in at least two confidence buckets."
    if high and low:
        calibrated = bool(
            high["success_rate_pct"] > low["success_rate_pct"]
            and high["avg_decision_return_pct"] > low["avg_decision_return_pct"]
        )
        note = (
            "Higher confidence is producing better success and decision return."
            if calibrated
            else "Displayed confidence is not ranking outcomes correctly; recalibration should be reviewed."
        )
    return {"rows": rows, "calibrated": calibrated, "note": note}


def paired_shadow_comparison(entries):
    """Matched long/cash policy returns; Avoid is not treated as a short.

    A weakened Avoid -> Hold off remains no exposure in both arms. It cannot
    earn invented investment gains merely by relabeling the recommendation.
    Unknown input rows stay visible in coverage but out of return averages.
    """
    groups = {}
    for entry in entries or []:
        if not calibration_eligible(entry):
            continue
        forward = number_or_none((entry.get("outcome") or {}).get("forward_return_pct"))
        if forward is None or not math.isfinite(forward):
            continue
        original = forward if decision_family(entry.get("rule_action")) == "long" else 0.0
        for row in entry.get("shadow_evaluations") or []:
            if not isinstance(row, dict) or row.get("candidate") not in {"confirmed_structural_avoid", "confirmed_entry_hold"}:
                continue
            key = (row.get("candidate"), row.get("version"))
            group = groups.setdefault(key, {"eligible_count": 0, "unknown_count": 0, "changed_count": 0, "original": [], "candidate": []})
            if row.get("eligible") is not True:
                group["unknown_count"] += 1
                continue
            candidate_return = forward if decision_family(row.get("action")) == "long" else 0.0
            group["eligible_count"] += 1
            group["changed_count"] += bool(row.get("differs_from_live"))
            group["original"].append(original)
            group["candidate"].append(candidate_return)
    results = []
    for (candidate, version), group in sorted(groups.items()):
        n = group["eligible_count"]
        originals, alternatives = group.pop("original"), group.pop("candidate")
        original = sum(originals) / n if n else None
        alternative = sum(alternatives) / n if n else None
        results.append({"candidate": candidate, "version": version, **group,
                        "original_avg_policy_return_pct": original,
                        "candidate_avg_policy_return_pct": alternative,
                        "paired_lift_pct": alternative - original if n else None,
                        "basis": "Matched logged-price horizon; long or cash, before costs. Not stop-executed P&L."})
    return results


def shadow_performance(entries, minimum_count=3):
    """Compare candidate shadow actions using only integrity-approved outcomes."""
    groups = {}
    for entry in entries or []:
        if not calibration_eligible(entry):
            continue
        for row in (entry.get("outcome") or {}).get("shadow_results") or []:
            success = row.get("directional_success")
            decision_return = number_or_none(row.get("decision_return_pct"))
            if success is None or decision_return is None:
                continue
            key = (str(row.get("candidate") or "unknown"), str(row.get("version") or "unknown"))
            groups.setdefault(key, []).append((bool(success), decision_return))
    results = []
    for (candidate, version), rows in groups.items():
        if len(rows) < int(minimum_count):
            continue
        results.append({
            "candidate": candidate,
            "version": version,
            "count": len(rows),
            "success_rate_pct": round(100 * sum(ok for ok, _ret in rows) / len(rows), 1),
            "avg_decision_return_pct": round(sum(ret for _ok, ret in rows) / len(rows), 2),
        })
    return sorted(results, key=lambda row: (-row["avg_decision_return_pct"], -row["count"]))


def shadow_promotion_readiness(
    entries,
    *,
    minimum_count=30,
    minimum_changed=8,
    minimum_regimes=2,
    minimum_per_regime=5,
    minimum_return_lift_pct=1.5,
    minimum_success_lift_pct=5.0,
    maximum_regime_harm_pct=-1.0,
):
    """Return evidence gates for human review of shadow candidates.

    This function can recommend review but can never promote a candidate. It
    compares every candidate with the live decision on the identical matured
    path and requires breadth across market regimes.
    """
    groups = {}
    for entry in entries or []:
        if not calibration_eligible(entry):
            continue
        outcome = entry.get("outcome") if isinstance(entry.get("outcome"), dict) else {}
        live_return = number_or_none(outcome.get("decision_return_pct"))
        live_success = outcome.get("directional_success")
        if live_return is None or live_success is None:
            continue
        regime = str((entry.get("decision_context") or {}).get("market_regime") or "Unknown").strip() or "Unknown"
        evaluations = {
            (str(row.get("candidate")), str(row.get("version"))): row
            for row in entry.get("shadow_evaluations") or [] if isinstance(row, dict)
        }
        for result in outcome.get("shadow_results") or []:
            if not isinstance(result, dict):
                continue
            candidate_return = number_or_none(result.get("decision_return_pct"))
            candidate_success = result.get("directional_success")
            if candidate_return is None or candidate_success is None:
                continue
            key = (str(result.get("candidate") or "unknown"), str(result.get("version") or "unknown"))
            evaluation = evaluations.get(key) or {}
            groups.setdefault(key, []).append({
                "regime": regime,
                "live_return": live_return,
                "candidate_return": candidate_return,
                "live_success": bool(live_success),
                "candidate_success": bool(candidate_success),
                "changed": bool(evaluation.get("differs_from_live")),
            })

    readiness = []
    for (candidate, version), rows in groups.items():
        count = len(rows)
        changed_count = sum(row["changed"] for row in rows)
        return_lift = sum(row["candidate_return"] - row["live_return"] for row in rows) / count
        success_lift = 100 * (
            sum(row["candidate_success"] for row in rows) - sum(row["live_success"] for row in rows)
        ) / count
        regime_groups = {}
        for row in rows:
            regime_groups.setdefault(row["regime"], []).append(row)
        mature_regimes = []
        for regime, regime_rows in regime_groups.items():
            if len(regime_rows) < int(minimum_per_regime):
                continue
            lift = sum(row["candidate_return"] - row["live_return"] for row in regime_rows) / len(regime_rows)
            mature_regimes.append({"regime": regime, "count": len(regime_rows), "return_lift_pct": round(lift, 2)})
        harmful_regimes = [row for row in mature_regimes if row["return_lift_pct"] <= float(maximum_regime_harm_pct)]
        gates = {
            "sample": count >= int(minimum_count),
            "changed_sample": changed_count >= int(minimum_changed),
            "regime_breadth": len(mature_regimes) >= int(minimum_regimes),
            "return_lift": return_lift >= float(minimum_return_lift_pct),
            "success_lift": success_lift >= float(minimum_success_lift_pct),
            "no_material_regime_harm": not harmful_regimes,
        }
        if all(gates.values()):
            status = "review_for_promotion"
            reason = "All evidence gates passed; human review is warranted before any production change."
        elif not gates["sample"] or not gates["changed_sample"] or not gates["regime_breadth"]:
            status = "collecting"
            reason = "More mature changed decisions across market regimes are required."
        else:
            status = "do_not_promote"
            reason = "The candidate does not improve outcomes consistently enough for promotion."
        readiness.append({
            "candidate": candidate,
            "version": version,
            "status": status,
            "count": count,
            "changed_count": changed_count,
            "success_lift_pct": round(success_lift, 2),
            "return_lift_pct": round(return_lift, 2),
            "regimes": mature_regimes,
            "gates": gates,
            "reason": reason,
        })
    return sorted(readiness, key=lambda row: (row["status"] != "review_for_promotion", -row["return_lift_pct"], row["candidate"]))


def _finite(value):
    value = number_or_none(value)
    return value if value is not None and math.isfinite(value) else None


@lru_cache(maxsize=8)
def _code_fingerprint(paths):
    digest = hashlib.sha256()
    for name, _mtime, _size in paths:
        path = Path(name)
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def engine_provenance(implementation):
    """Freeze actual code identity; the historic friendly version was reused."""
    implementation = "streamlit" if implementation == "streamlit" else "worker"
    root = Path(__file__).resolve().parent
    files = [root / "tactical.py", root / "decision_contract.py", root / ("app.py" if implementation == "streamlit" else "worker.py")]
    paths = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in files)
    revision = str(os.environ.get("GITHUB_SHA") or os.environ.get("RENDER_GIT_COMMIT") or "").strip()
    return {
        "rule_implementation": implementation,
        "rule_code_fingerprint": _code_fingerprint(paths),
        "deployment_revision": revision or None,
    }


def _mature_directional(entry):
    outcome = entry.get("outcome") or {}
    return bool(
        outcome.get("evaluation_version") == EVALUATION_VERSION
        and calibration_eligible(entry)
        and outcome.get("directional_success") is not None
        and _finite(outcome.get("decision_return_pct")) is not None
        and _finite(outcome.get("forward_return_pct")) is not None
    )


def non_overlapping_cohorts(entries):
    """Sensitivity check using one matured 14-session path per ticker/family.

    The existing seven-calendar-day gate remains published, but these rows
    expose how much its overlapping price paths reduce the effective sample.
    """
    selected, last_exit = [], {}
    for entry in sorted(entries, key=lambda row: (str(row.get("ts") or ""), str(row.get("id") or ""))):
        logged = _entry_date(entry)
        primary = ((entry.get("outcome") or {}).get("horizons") or {}).get("14") or {}
        try:
            exit_date = date.fromisoformat(str(primary.get("scored_date")))
        except ValueError:
            continue
        key = (str(entry.get("ticker") or "").upper(), decision_family(entry.get("rule_action")))
        if logged is None or (key in last_exit and logged <= last_exit[key]):
            continue
        selected.append(entry)
        last_exit[key] = exit_date
    return selected


def _family_evidence(entries, family, label):
    rows = [entry for entry in entries if decision_family(entry.get("rule_action")) == family]
    returns = sorted(_finite(row["outcome"]["decision_return_pct"]) for row in rows)
    summary = summarize_outcomes(rows)
    count = len(rows)
    losses = [value for value in returns if value < 0]
    trim = int(count * 0.1)
    trimmed = returns[trim:count - trim] if trim else returns
    tickers = {str(row.get("ticker") or "").upper() for row in rows}
    benchmark = [_finite(row["outcome"].get("benchmark_return_pct")) for row in rows]
    benchmark = [value for value in benchmark if value is not None]
    return {
        **summary,
        "family": family,
        "label": label,
        "success_rate_pct": summary["hit_rate_pct"],
        "successes": summary["successful_count"],
        "positive_direction_rate_pct": round(100 * sum(value > 0 for value in returns) / count, 1) if count else None,
        "median_decision_return_pct": round(median(returns), 2) if returns else None,
        "trimmed_mean_decision_return_pct": round(sum(trimmed) / len(trimmed), 2) if trimmed else None,
        "avg_benchmark_return_pct": round(sum(benchmark) / len(benchmark), 2) if benchmark else None,
        "benchmark_count": len(benchmark),
        "distinct_tickers": len(tickers),
        "worst_3_loss_share_pct": round(100 * sum(losses[:3]) / sum(losses), 1) if losses else None,
        "cash_reference_return_pct": 0.0,
        "interpretation": (
            "Unweighted stock return from the logged reference price. Cash is compared at 0%, before interest."
            if family == "long" else
            "Negative stock return measures losses avoided; a negative decision return means missed upside."
        ),
    }


def _review_case(entry):
    """An allowlist of frozen rule inputs and outcomes, without private notes."""
    outcome = entry.get("outcome") or {}
    context = entry.get("decision_context") or {}
    receipt = entry.get("decision_receipt") or {}
    return {
        "id": str(entry.get("id") or ""),
        "ticker": str(entry.get("ticker") or "").upper(),
        "logged_date": outcome.get("logged_date") or str(entry.get("ts") or "")[:10],
        "scored_date": ((outcome.get("horizons") or {}).get("14") or {}).get("scored_date"),
        "family": decision_family(entry.get("rule_action")),
        "action": str(entry.get("rule_action") or "").replace("_now", "").replace("_", " ").title(),
        "rule_state": entry.get("rule_state"),
        "engine_version": entry.get("rule_engine_version") or receipt.get("engine_version"),
        "rule_source": entry.get("rule_source"),
        "rule_implementation": entry.get("rule_implementation"),
        "rule_code_fingerprint": entry.get("rule_code_fingerprint"),
        "deployment_revision": entry.get("deployment_revision"),
        "reference_price": _finite(outcome.get("reference_price") or entry.get("price")),
        "setup_score": _finite(entry.get("setup_score")),
        "reward_risk": _finite(entry.get("reward_risk")),
        "rs": _finite(entry.get("rs")),
        "vol_ratio": _finite(entry.get("vol_ratio")),
        "market_regime": context.get("market_regime"),
        "confidence": str(receipt.get("confidence") or "").split("·", 1)[0].strip() or None,
        "extension_warning": bool(context["extension_warning"]) if "extension_warning" in context else None,
        "extension_warning_severity": context.get("extension_warning_severity"),
        "trigger_kind": entry.get("trigger_kind"),
        "trigger_summary": str(entry.get("trigger_summary") or ""),
        "trigger_price": _finite(entry.get("trigger_price")),
        "invalidation_price": _finite(entry.get("invalidation_price") or entry.get("stop_price")),
        "entry_is_projected": bool(entry["entry_is_projected"]) if "entry_is_projected" in entry else None,
        "directional_success": bool(outcome.get("directional_success")),
        **{key: _finite(outcome.get(key)) for key in (
            "forward_return_pct", "benchmark_return_pct", "excess_return_pct",
            "decision_return_pct", "decision_excess_pct", "mfe_pct", "mae_pct",
        )},
        "invalidation_fired": bool(outcome.get("invalidation_fired")),
        "invalidation_date": outcome.get("invalidation_date"),
    }


def rules_performance_snapshot(entries, *, as_of=None):
    """One evidence population and report builder for worker, API and Streamlit."""
    as_of = as_of or date.today()
    decisions = [row for row in entries or [] if isinstance(row, dict) and (
        row.get("auto_logged") or row.get("source") == "rules_engine"
    )]
    decisions.sort(key=lambda row: (str(row.get("ts") or ""), str(row.get("id") or "")), reverse=True)
    cohorts = independent_cohorts(decisions, spacing_days=7)
    evaluated = [row for row in cohorts if (row.get("outcome") or {}).get("evaluation_version") == EVALUATION_VERSION]
    directional = [row for row in evaluated if decision_family(row.get("rule_action")) in {"long", "avoid"} and _mature_directional(row)]
    patience = [row for row in evaluated if decision_family(row.get("rule_action")) == "wait"]
    mature_open, exclusions = [], {}
    for entry in cohorts:
        outcome = entry.get("outcome") or {}
        logged = _entry_date(entry)
        if logged and (as_of - logged).days >= 7 and (
            outcome.get("evaluation_version") != EVALUATION_VERSION or not outcome.get("evaluation_complete")
        ):
            mature_open.append(entry)
        if decision_family(entry.get("rule_action")) not in {"long", "avoid"} or _mature_directional(entry):
            continue
        reasons = (
            ["unscored"] if not outcome else
            ["old_evaluation_version"] if outcome.get("evaluation_version") != EVALUATION_VERSION else
            (outcome.get("integrity") or {}).get("issues") or ["14_session_result_pending"]
        )
        for reason in reasons:
            exclusions[str(reason)] = exclusions.get(str(reason), 0) + 1
    families = [("long", "Enter / Accumulate"), ("avoid", "Avoid")]
    patterns, slices = [], []
    for family, _label in families:
        rows = [row for row in directional if decision_family(row.get("rule_action")) == family]
        patterns.extend({**row, "family": family} for row in failure_patterns(rows))
        slices.extend({**row, "family": family} for row in performance_slices(rows))
    cases = sorted((_review_case(row) for row in directional), key=lambda row: (row["decision_return_pct"], row["id"]))
    fingerprint = [{"id": row["id"], "date": row["logged_date"], "return": row["decision_return_pct"], "success": row["directional_success"]} for row in cases]
    flags = logic_review_flags(directional)
    nonoverlap = non_overlapping_cohorts(directional)
    version_names = sorted({str(row.get("rule_engine_version") or (row.get("decision_receipt") or {}).get("engine_version") or "Unrecorded") for row in directional})
    versions = []
    for version in version_names:
        version_rows = [row for row in directional if str(row.get("rule_engine_version") or (row.get("decision_receipt") or {}).get("engine_version") or "Unrecorded") == version]
        versions.extend({**_family_evidence(version_rows, family, label), "engine_version": version}
                        for family, label in families if any(decision_family(row.get("rule_action")) == family for row in version_rows))
    return {
        "decisions": decisions,
        "scored": [row for row in decisions if (row.get("outcome") or {}).get("auto_scored")],
        "open": [row for row in decisions if not row.get("outcome")],
        "cohorts": cohorts,
        "scored_cohorts": [row for row in cohorts if _finite((row.get("outcome") or {}).get("forward_return_pct")) is not None],
        "evaluated_cohorts": evaluated,
        "directional_cohorts": directional,
        "patience_cohorts": patience,
        "mature_open": mature_open,
        "score_version": EVALUATION_VERSION,
        "evidence_id": hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:16],
        "summary": summarize_outcomes(directional),
        "patience_summary": summarize_patience(patience),
        "logic_review_flags": flags,
        "flags": flags,
        "alerting": [row for row in flags if row["status"] in {"watch", "review_logic"}],
        "directional_families": [_family_evidence(directional, family, label) for family, label in families],
        "non_overlapping_families": [_family_evidence(nonoverlap, family, label) for family, label in families],
        "engine_version_families": versions,
        "failure_patterns": sorted(patterns, key=lambda row: row["avg_decision_return_pct"]),
        "performance_slices": slices,
        "confidence_calibration": confidence_calibration(directional),
        "confidence_calibration_by_family": {
            family: confidence_calibration([row for row in directional if decision_family(row.get("rule_action")) == family])
            for family, _label in families
        },
        "weakest_cases": weakest_directional_cases(directional),
        "review_cases": cases,
        "shadow_performance": shadow_performance(directional),
        "paired_shadow_comparison": paired_shadow_comparison(directional),
        "shadow_promotion": shadow_promotion_readiness(directional),
        "evidence_quality": {
            "logged_count": len(decisions), "cohort_count": len(cohorts),
            "ignored_non_rule_log_count": len(entries or []) - len(decisions),
            "directional_count": len(directional), "patience_count": len(patience),
            "path_updates_due": len(mature_open), "excluded_directional_reasons": exclusions,
            "cohort_spacing_calendar_days": 7,
            "non_overlapping_count": len(nonoverlap),
            "recorded_input_coverage": {
                "engine_version": sum(bool(row.get("rule_engine_version") or (row.get("decision_receipt") or {}).get("engine_version")) for row in directional),
                "code_fingerprint": sum(bool(row.get("rule_code_fingerprint")) for row in directional),
                "regime": sum(bool((row.get("decision_context") or {}).get("market_regime")) for row in directional),
                "confidence": sum(bool((row.get("decision_receipt") or {}).get("confidence")) for row in directional),
                "extension_warning": sum("extension_warning" in (row.get("decision_context") or {}) for row in directional),
                "frozen_input_snapshot": sum(isinstance(row.get("decision_inputs"), dict) for row in directional),
            },
            "notes": [
                "Seven-day cohorts can share part of the same 14-session price path; the non-overlapping view checks that sensitivity.",
                "These are equal-weight signal returns, before fees and without simulated position sizing or stop execution.",
                "Success for Enter/Accumulate requires both a positive stock return and SPY outperformance.",
                "Historical results are grouped by their recorded engine version; current rules do not rewrite old calls.",
                "Older friendly engine labels were reused across code changes. A missing code fingerprint cannot establish performance of the currently deployed rules.",
            ],
        },
    }


def evidence_payload(snapshot, *, include_cases=False):
    """Publish the shared report without raw logs, receipts or user content."""
    fields = (
        "score_version", "evidence_id", "summary", "flags", "alerting",
        "directional_families", "non_overlapping_families", "engine_version_families", "patience_summary",
        "evidence_quality", "shadow_performance", "shadow_promotion", "paired_shadow_comparison",
        "confidence_calibration", "confidence_calibration_by_family",
    )
    result = {key: snapshot[key] for key in fields}
    if include_cases:
        result.update({key: snapshot[key] for key in (
            "performance_slices", "failure_patterns", "weakest_cases", "review_cases",
        )})
    result["case_review_available"] = bool(include_cases)
    return result
