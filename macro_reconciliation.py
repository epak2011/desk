"""Release-dated macro diagnostics; never changes the production entry score."""
from datetime import date
from math import isfinite

VERSION = "macro-reconciliation-1"
THRESHOLDS = {"ism": 50, "unemployment": 4.2, "unemployment_approach": 4.0,
              "hy_early_warning_bps": 450, "hy_tail_risk_bps": 475, "hy_crisis_bps": 600}


def _number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and isfinite(value)


def _consecutive(latest, previous):
    try:
        a, b = date.fromisoformat(str(latest)), date.fromisoformat(str(previous))
        return a.year * 12 + a.month - (b.year * 12 + b.month) == 1
    except (TypeError, ValueError):
        return False


def evaluate(macro):
    """Parity with Apps Script evaluateReconciledMacro; dates are observation months."""
    m = macro or {}
    ism, prior = m.get("ism"), m.get("ism_previous")
    prior_known = _number(prior) and _consecutive(m.get("ism_period"), m.get("ism_previous_period"))
    if not _number(ism):
        t1 = "UNKNOWN"
    elif ism >= 50:
        t1 = "RECOVERY" if prior_known and prior < 50 else "CLEAR"
    elif not prior_known:
        t1 = "UNKNOWN"
    else:
        t1 = "FIRING" if prior < 50 else "WARNING"
    meanings = {
        "WARNING": "First monthly contraction reading: review risk; no automatic allocation change.",
        "FIRING": "Consecutive contraction readings: do not repeat a first-miss exit instruction. Recovery is possible, not guaranteed.",
        "RECOVERY": "First expansion reading after contraction: recovery context, not an automatic buy.",
        "UNKNOWN": "Monthly observations unavailable; first miss versus prolonged contraction cannot be determined.",
        "CLEAR": "Manufacturing expansion context.",
    }
    current, previous, older = (m.get(k) for k in ("unemployment", "unemployment_previous", "unemployment_two_months_ago"))
    periods = m.get("unemployment_periods") or []
    known = all(_number(v) for v in (current, previous, older)) and len(periods) >= 3 and _consecutive(periods[0], periods[1]) and _consecutive(periods[1], periods[2])
    candidates = {}
    result = {"version": VERSION, "mode": "SHADOW", "allocation": None, "t1": t1, "t1Meaning": meanings[t1]}
    for key in ("dashboard_gt_0_10", "legacy_ge_0_15"):
        if not known:
            candidates[key] = "UNKNOWN"
            continue
        delta, old_delta = round(current - previous, 3), round(previous - older, 3)
        magnitude = delta > .10 if key == "dashboard_gt_0_10" else delta >= .15
        accelerating = delta > 0 and delta > old_delta
        candidates[key] = ("RETREATING" if delta < 0 else
                           ("FIRING" if accelerating and magnitude else "WATCH") if current >= 4.2 else
                           "APPROACHING" if current >= 4.0 and delta > 0 else "CLEAR")
        result.update(unemploymentMonthlyChange=delta, unemploymentPreviousChange=old_delta)
    result.update(t2Candidates=candidates, t2=candidates["dashboard_gt_0_10"] if len(set(candidates.values())) == 1 else "UNRESOLVED",
                  t2Meaning="Compare >0.10pp and >=0.15pp with increasing monthly change; equivalent on one-decimal UNRATE observations.")
    hy = m.get("hy_oas_bps")
    result.update(t3="UNKNOWN" if not _number(hy) else "CRISIS" if hy >= 600 else "TAIL_RISK" if hy >= 475 else "EARLY_WARNING" if hy >= 450 else "CLEAR",
                  t3Meaning="Credit stress is context. A crisis reading alone does not trigger a sell or a buy.")
    return result


def snapshot_key(snapshot):
    import hashlib
    import json
    return hashlib.sha256(json.dumps({"generated_at": snapshot.get("generated_at"), "macro": snapshot.get("macro")}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def for_snapshot(snapshot):
    """Read supplemental diagnostics only when tied to this exact saved input."""
    import backend_layer
    try:
        with backend_layer.db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM macro_regime_audits WHERE snapshot_key = %s", (snapshot_key(snapshot),))
                row = cur.fetchone()
        if row and isinstance(row[0], dict):
            return row[0]
    except Exception:
        pass  # No supplemental table yet: retain UNKNOWN instead of fabricating months.
    return evaluate(snapshot.get("macro"))
