"""Safe research diagnostics and research/evidence health; never changes rules."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
from pathlib import Path


def number_label(value, digits=2, prefix="", suffix=""):
    try:
        numeric = float(value)
        return f"{prefix}{numeric:.{digits}f}{suffix}" if math.isfinite(numeric) else "unavailable"
    except (TypeError, ValueError):
        return "unavailable"


def growth_percent_label(value):
    """Provider growth values are fractions; preserve zero and reject nonfinite."""
    try:
        number = float(value)
        return f"{number * 100:+.2f}%" if math.isfinite(number) else "unavailable"
    except (TypeError, ValueError):
        return "unavailable"


def failure_code(source, issues=()):
    text = str(source or "").lower()
    for code, markers in (
        ("provider_auth", ("401", "403", "authentication", "invalid api key", "api_key is not configured")),
        ("provider_quota", ("429", "rate limit", "credit balance", "billing", "quota")),
        ("provider_timeout", ("timeout", "timed out")),
        ("provider_model", ("model_not_found", "model not found", "not_found_error", "model is not available", "invalid model", "not a valid model")),
        ("response_truncated", ("research_response_truncated", "max_tokens")),
        ("invalid_json", ("research_invalid_json", "jsondecode", "expecting value", "unterminated", "expecting ','", "expecting property")),
        ("identity_rejected", ("identity guard",)),
    ):
        if any(marker in text for marker in markers):
            return code
    return "contract_validation" if issues else "generation_failed"


class ResearchFailure(RuntimeError):
    def __init__(self, source, issues=()):
        code = failure_code(source, issues)
        # Never put provider messages, prompts or keys in queue/API diagnostics.
        self.diagnostics = {"error_code": code, "stage": "full_report",
                            "validation_issue_count": len(issues), "previous_report_preserved": True}
        super().__init__(f"Claude research did not complete ({code}); previous report preserved. Retry research.")


def retry_window(row, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        failures = max(0, int(row.get("failures") or 0))
        stamp = datetime.fromisoformat(str(row.get("last_failed_at")).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return {"cooling_down": False}
    minutes = min(1440, 30 * 2 ** min(6, max(0, failures - 1)))
    if row.get("error_code") in {"provider_auth", "provider_quota"}:
        minutes = 1440
    until = stamp + timedelta(minutes=minutes)
    return {"cooling_down": failures > 0 and now < until, "failure_count": failures,
            "retry_after": until.isoformat(), "cooldown_minutes": minutes,
            "error_code": row.get("error_code") or "generation_failed"}


def experiment_health(root=None, today=None, registration=None):
    """Verify the actual immutable registration; never silently re-register."""
    import research_protocol
    root = Path(root or Path(__file__).parent)
    today = today or datetime.now(timezone.utc).date()
    try:
        spec = (json.loads((root / "experiments" / registration).read_text()) if registration
                else research_protocol.active_registration(root))
        actual = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                  for name in spec.get("source_hashes", {})}
        changed = sorted(name for name, expected in spec.get("source_hashes", {}).items()
                         if actual.get(name) != expected)
        ticker = next(iter(spec.get("universe") or []), "")
        status = research_protocol.registration_status(spec, source_hashes=actual,
                                                       ticker=ticker, session=today.isoformat())
        state = "eligible" if status["eligible"] else "scheduled" if status["reason"] == "before_prospective_start" else "blocked"
        return {**status, "status": state,
                "changed_sources": changed, "collection_verified": False,
                "promotion": "manual_only", "start_date_preserved": True}
    except (OSError, ValueError, TypeError):
        return {"status": "unavailable", "eligible": False, "collection_verified": False,
                "reason": "registration_unreadable", "promotion": "manual_only"}


def research_issue(research):
    """Audit the selected research revision, not an unrelated failed report."""
    age = evidence_age_minutes(research.get("generated_at"))
    saved = bool((research.get("selection") or {}).get("saved_successful_research"))
    if research.get("status") == "stale" or research.get("content_blocked"):
        return {"kind": "research_stale", "severity": "low", "age_minutes": age}
    if research.get("generation_status") == "incomplete" and not saved:
        return {"kind": "research_incomplete", "severity": "medium"}
    if research.get("completeness") in {"unavailable", "rules_only"} and not saved:
        return {"kind": "research_missing", "severity": "low"}
    if age is None or age >= 7 * 1440:
        return {"kind": "research_stale", "severity": "low", "age_minutes": age}
    return None


def evidence_age_minutes(stamp):
    try:
        value = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - value).total_seconds() / 60
        return None if age < -5 else max(0.0, age)
    except (TypeError, ValueError):
        return None


def performance_context(evidence):
    age = evidence_age_minutes(evidence.get("generated_at"))
    return {"basis": "forward_signal_checkpoints_not_portfolio_returns",
            "measurement_sessions": [5, 14, 30], "calendar_exit_enabled": False,
            "generated_at": evidence.get("generated_at"), "age_minutes": age,
            "freshness": "unknown" if age is None else "stale" if age > 36 * 60 else "fresh",
            "version_breakdown": evidence.get("engine_version_families") or [],
            "current_rules_performance_verified": False,
            "caveat": "Aggregate signal outcomes do not establish returns of current rules or the corrected exit-policy replay."}
