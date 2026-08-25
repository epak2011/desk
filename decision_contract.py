"""Canonical, deterministic decision receipts shared by UI and workers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


RECEIPT_SCHEMA_VERSION = 1


def _number(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def normalize_action(value):
    action = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return {"enter": "enter_now", "buy": "enter_now", "holdoff": "hold_off"}.get(action, action)


def _trace_factors(trace, limit=3):
    factors = []
    for step in reversed(trace or []):
        if isinstance(step, dict):
            text = str(step.get("detail") or step.get("label") or "").strip()
        else:
            text = str(step or "").strip()
        if text and text not in factors:
            factors.append(text)
        if len(factors) >= limit:
            break
    return list(reversed(factors))


def build_decision_receipt(ticker, state, *, engine_version, captured_at=None, previous=None):
    """Create the immutable, public-facing contract for one rules decision."""
    state = state if isinstance(state, dict) else {}
    trigger = state.get("trigger") if isinstance(state.get("trigger"), dict) else {}
    levels = trigger.get("levels") if isinstance(trigger.get("levels"), dict) else {}
    action = normalize_action(state.get("action"))
    price = _number(state.get("price") or state.get("last"))
    trigger_price = _number(levels.get("buy_above") or state.get("trigger_price"))
    invalidation_price = _number(
        levels.get("abort_below") or state.get("invalidation_price") or state.get("stop")
    )
    trigger_text = str(
        state.get("trigger_summary")
        or state.get("trigger_text")
        or trigger.get("detail")
        or trigger.get("summary")
        or ""
    ).strip()
    invalidation_text = str(state.get("invalidation_text") or "").strip()
    if not invalidation_text and invalidation_price is not None:
        invalidation_text = f"Setup invalid below ${invalidation_price:,.2f}."
    primary_risk = str(
        state.get("primary_risk")
        or state.get("reward_risk_gate_reason")
        or invalidation_text
        or ("Structure and relative strength have not repaired." if action == "avoid" else "")
    ).strip()
    trace = state.get("_rule_trace") or state.get("rule_trace") or []
    factors = _trace_factors(trace)
    if not factors and state.get("matrix_reason"):
        factors = [str(state.get("matrix_reason"))]
    captured_at = captured_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    core = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "ticker": str(ticker or "").upper().strip(),
        "captured_at": str(captured_at),
        "engine_version": str(engine_version or "unknown"),
        "action": action,
        "price": round(price, 4) if price is not None else None,
        "market_regime": state.get("market_regime"),
        "setup_score": _number(state.get("setup_score")),
        "confidence": state.get("decision_confidence") or state.get("confidence"),
        "entry_size": state.get("entry_size"),
        "setup_stage": state.get("entry_status") or state.get("state"),
        "top_factors": factors[:3],
        "primary_risk": primary_risk,
        "trigger": {"text": trigger_text, "price": trigger_price},
        "invalidation": {"text": invalidation_text, "price": invalidation_price},
        "source": "rules_engine",
        "data_trust": state.get("data_trust") if isinstance(state.get("data_trust"), dict) else {},
    }
    identity = {key: value for key, value in core.items() if key != "captured_at"}
    core["receipt_id"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    prior = previous if isinstance(previous, dict) else {}
    prior_action = normalize_action(prior.get("action"))
    prior_trigger = _number((prior.get("trigger") or {}).get("price"))
    changes = []
    if prior_action and prior_action != action:
        changes.append(f"Action changed from {prior_action.replace('_', ' ')} to {action.replace('_', ' ')}.")
    if prior_trigger is not None and trigger_price is not None and abs(prior_trigger - trigger_price) > 0.01:
        changes.append(f"Trigger moved from ${prior_trigger:,.2f} to ${trigger_price:,.2f}.")
    if prior and not changes:
        changes.append("Action and trigger are unchanged.")
    core["change_summary"] = changes
    return core


def receipt_consistency(ticker, surfaces):
    """Compare decision-bearing surfaces against one canonical receipt."""
    surfaces = surfaces if isinstance(surfaces, dict) else {}
    receipt = surfaces.get("receipt") if isinstance(surfaces.get("receipt"), dict) else {}
    expected = normalize_action(receipt.get("action"))
    mismatches = []
    if not expected:
        return mismatches
    for surface, payload in surfaces.items():
        if surface == "receipt" or not isinstance(payload, dict):
            continue
        observed = normalize_action(payload.get("action"))
        if observed and observed != expected:
            mismatches.append({
                "ticker": str(ticker or "").upper(),
                "surface": surface,
                "expected": expected,
                "observed": observed,
                "kind": "action",
            })
        observed_version = str(payload.get("engine_version") or payload.get("rule_engine_version") or "")
        expected_version = str(receipt.get("engine_version") or "")
        if observed_version and expected_version and observed_version != expected_version:
            mismatches.append({
                "ticker": str(ticker or "").upper(),
                "surface": surface,
                "expected": expected_version,
                "observed": observed_version,
                "kind": "engine_version",
            })
    return mismatches
