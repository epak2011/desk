"""Non-production rule candidates evaluated beside the live engine.

Candidates in this module must never replace the user-facing action. They exist
to make proposed rule changes replayable and measurable on identical price paths.
"""

from __future__ import annotations


STRUCTURAL_CANDIDATE_VERSION = "shadow-2026.09-b"
NO_MOMENTUM_EXCEPTION_VERSION = "shadow-2026.09-c"


def _number(value, default=None):
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def structural_state(state: dict) -> dict:
    """Return the proposed canonical structural classification.

    BROKEN is evaluated first. TRANSITION requires either improving evidence or
    an explicit weakening location; the phase makes that distinction auditable.
    """
    state = state if isinstance(state, dict) else {}
    price = _number(state.get("price"), 0.0)
    ma50 = _number(state.get("ma50"), 0.0)
    ma200 = _number(state.get("ma200"), 0.0)
    rs = _number(state.get("rs"), 1.0)
    rs_delta = _number(state.get("rs_delta"), 0.0)
    tech_delta = _number(state.get("tech_delta"), 0.0)
    if price <= 0 or ma50 <= 0 or ma200 <= 0:
        return {"state": "TRENDING", "phase": "unknown", "reason": "Moving-average inputs are incomplete."}

    pct_vs_ma200 = (price - ma200) / ma200
    broken = price < ma200 * 0.85 and rs < 0.90 and rs_delta < 0.01 and tech_delta <= 0
    if broken:
        return {
            "state": "BROKEN",
            "phase": "deteriorating",
            "reason": "Price is more than 15% below the 200-day average with weak, non-improving relative strength and momentum.",
        }

    recovering = (
        (price > ma50 and price < ma200 and (tech_delta > 0 or rs_delta >= 0.02))
        or (rs < 1.0 and rs_delta >= 0.02)
        or (-0.20 <= pct_vs_ma200 <= -0.05 and (tech_delta > 0 or rs_delta >= 0.02))
    )
    if recovering:
        return {"state": "TRANSITION", "phase": "recovering", "reason": "Structure is below its long-term trend but momentum or relative strength is improving."}

    weakening = price < ma200 and (price < ma50 or rs < 0.95 or tech_delta <= 0)
    if weakening:
        return {"state": "TRANSITION", "phase": "weakening", "reason": "Structure is below its long-term trend without enough deterioration to qualify as broken."}

    return {"state": "TRENDING", "phase": "established", "reason": "Price structure and relative strength do not meet transition or broken conditions."}


def shadow_evaluations(state: dict) -> list[dict]:
    """Return all current candidates without mutating the production action."""
    state = state if isinstance(state, dict) else {}
    live_action = str(state.get("action") or "").strip().lower().replace("-", "_").replace(" ", "_")
    live_action = {"enter": "enter_now", "holdoff": "hold_off"}.get(live_action, live_action)
    warning = state.get("extension_warning") if isinstance(state.get("extension_warning"), dict) else {}

    strict_extension = live_action
    if live_action in {"enter_now", "accumulate"} and warning.get("severity") == "high":
        strict_extension = "watch"

    proposed_structure = structural_state(state)
    structural_action = live_action
    if proposed_structure["state"] == "BROKEN":
        structural_action = "avoid"
    elif live_action == "avoid":
        structural_action = "hold_off"

    no_exception = live_action
    if live_action in {"enter_now", "accumulate"}:
        ma50 = _number(state.get("ma50"), 0.0)
        ma100 = _number(state.get("ma100"), 0.0)
        price = _number(state.get("price"), 0.0)
        extended = bool(
            price > 0 and ma50 > 0 and (
                ((price / ma50 - 1) > 0.12 and ma100 > 0 and (price / ma100 - 1) > 0.08)
                or ((price / ma50 - 1) > 0.15 and ma100 <= 0)
            )
        )
        if extended:
            no_exception = "watch"

    return [
        {
            "candidate": "strict_extreme_extension",
            "version": "shadow-2026.09-a",
            "action": strict_extension,
            "differs_from_live": strict_extension != live_action,
            "reason": "Extreme stretched momentum waits for a pullback or base." if strict_extension != live_action else "No extreme-extension override.",
        },
        {
            "candidate": "unified_structural_state",
            "version": STRUCTURAL_CANDIDATE_VERSION,
            "action": structural_action,
            "state": proposed_structure["state"],
            "phase": proposed_structure["phase"],
            "structure_quality": _number(state.get("structure_quality_candidate")),
            "differs_from_live": structural_action != live_action,
            "reason": proposed_structure["reason"],
        },
        {
            "candidate": "no_momentum_exception",
            "version": NO_MOMENTUM_EXCEPTION_VERSION,
            "action": no_exception,
            "differs_from_live": no_exception != live_action,
            "reason": "Extended entries wait for a base without a momentum exception." if no_exception != live_action else "Momentum exception is not decisive for this setup.",
        },
    ]
