"""Non-production rule candidates evaluated beside the live engine.

Candidates in this module must never replace the user-facing action. They exist
to make proposed rule changes replayable and measurable on identical price paths.
"""

from __future__ import annotations


STRUCTURAL_CANDIDATE_VERSION = "shadow-2026.09-b"
NO_MOMENTUM_EXCEPTION_VERSION = "shadow-2026.09-c"
ENTRY_TIMING_GUARD_VERSION = "shadow-2026.09-d"
REGIME_QUALITY_GATE_VERSION = "shadow-2026.09-e"


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

    # Candidate 4: require a cleaner entry whenever the live decision still
    # carries an extension warning.  This deliberately tests a broader guard
    # than the existing high-severity-only candidate.
    timing_guard = live_action
    if live_action in {"enter_now", "accumulate"} and warning.get("severity") in {"med", "medium", "high"}:
        timing_guard = "watch"

    # Candidate 5: weak/mixed market regimes demand both a high-quality setup
    # and strong reward/risk before permitting new or additional exposure.
    regime = str(state.get("market_regime") or "").strip().lower().replace("_", " ").replace("-", " ")
    constrained_regime = regime in {
        "mixed", "unfavorable", "unfavourable", "hold off", "risk off",
        "bearish", "defensive", "cautious",
    }
    setup_score = _number(state.get("setup_score"))
    reward_risk = _number(state.get("reward_risk"))
    regime_gate = live_action
    if live_action in {"enter_now", "accumulate"} and constrained_regime:
        strong_setup = setup_score is not None and setup_score >= 8
        strong_reward_risk = reward_risk is not None and reward_risk >= 2
        if not (strong_setup and strong_reward_risk):
            regime_gate = "watch"

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
        {
            "candidate": "entry_timing_guard",
            "version": ENTRY_TIMING_GUARD_VERSION,
            "action": timing_guard,
            "evaluation_mode": "avoided_long_exposure" if timing_guard != live_action else "directional",
            "differs_from_live": timing_guard != live_action,
            "reason": "Any material extension warning waits for a pullback or base." if timing_guard != live_action else "No material extension warning blocks this entry.",
        },
        {
            "candidate": "regime_quality_gate",
            "version": REGIME_QUALITY_GATE_VERSION,
            "action": regime_gate,
            "evaluation_mode": "avoided_long_exposure" if regime_gate != live_action else "directional",
            "differs_from_live": regime_gate != live_action,
            "reason": (
                "Mixed or unfavorable conditions require setup score at least 8 and reward/risk at least 2."
                if regime_gate != live_action
                else "The market regime is unconstrained or both quality gates pass."
            ),
        },
    ]
