"""Shared deterministic long-position invalidation; no discretionary sizing."""
import math

POLICY_VERSION = "technical-invalidation-v1"


def finite_number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def full_exit_reason(state, saved_stop):
    """Evaluate the supplied price; caller owns bar completion and execution.

    Stops take precedence over event warnings. Avoid alone is not a sell.
    No targets, time limits, partial trims or stop adjustments are inferred.
    """
    price = finite_number(state.get("price"))
    stop = finite_number(saved_stop)
    ma50, ma200 = (finite_number(state.get(k)) for k in ("ma50", "ma200"))
    if price is None or price <= 0:
        return None
    if stop is not None and stop > 0 and price <= stop:
        return "invalidation"
    if (str(state.get("action") or "").lower() == "avoid"
            and ma50 is not None and ma200 is not None
            and min(ma50, ma200) > 0 and price < min(ma50, ma200)):
        return "structural"
    return None
