"""Decision-time data contract for actionable Trading Desk calls."""

from __future__ import annotations


def assess_decision_data(state, *, price_age_kind="fresh", market_source="live", benchmark_source="live"):
    state = state if isinstance(state, dict) else {}
    blocked = []
    degraded = []
    if state.get("price") is None and state.get("last") is None:
        blocked.append("Current price is unavailable.")
    if not state.get("action"):
        blocked.append("Rules action is unavailable.")
    if state.get("ma50") is None or state.get("ma200") is None:
        blocked.append("Required trend history is incomplete.")
    if str(price_age_kind or "").lower() in {"stale", "unavailable"}:
        blocked.append("Market price is stale or unavailable.")
    if str(market_source or "").lower() in {"synthetic", "fallback"}:
        blocked.append("Market data is synthetic or fallback-only.")
    if state.get("setup_score") is None:
        degraded.append("Setup score is unavailable.")
    if state.get("rs") is None:
        degraded.append("Relative-strength context is unavailable.")
    if state.get("vol_ratio") is None:
        degraded.append("Volume participation is unavailable.")
    if state.get("reward_risk") is None:
        degraded.append("Reward/risk could not be calculated.")
    if str(benchmark_source or "").lower() in {"synthetic", "fallback", "cached"}:
        degraded.append(f"Benchmark context is {benchmark_source}.")
    status = "blocked" if blocked else "degraded" if degraded else "trusted"
    return {
        "status": status,
        "executable": not blocked,
        "blocked_reasons": blocked,
        "degraded_reasons": degraded,
        "market_source": market_source,
        "benchmark_source": benchmark_source,
        "price_age_kind": price_age_kind,
    }
