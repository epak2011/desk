"""Stable, presentation-neutral payloads for a future public frontend.

This module intentionally contains no trading rules.  It packages canonical
engine output so a web client cannot accidentally develop a second decision
engine with different actions, sizing, or trust semantics.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


PUBLIC_CONTRACT_VERSION = 2

PAGE_CONTRACTS = [
    {"key": "today", "label": "Today", "route": "/today", "endpoint": "/v1/attention", "auth": "required", "status": "shared", "sections": ["daily_decision_workflow", "attention_inbox"], "response_keys": ["daily_workflow_summary", "events"]},
    {"key": "market", "label": "Market", "route": "/market", "endpoint": "/v1/regime", "auth": "public", "status": "shared", "sections": ["outlook", "entry_timing", "todays_context", "market_highlights", "market_implications", "forward_watch", "framework_gauges", "market_news", "crypto_regime", "metric_guide"], "response_keys": ["regime"]},
    {"key": "analyze", "label": "Analyze", "route": "/analyze/{ticker}", "endpoint": "/v1/decisions/{ticker}", "auth": "public", "status": "shared", "sections": ["decision_header", "hero", "company_overview", "decision_evidence", "why_action", "call_changes", "technical_picture", "portfolio_manager", "full_research_report"], "response_keys": ["decision", "security_profile", "research", "analyze_page"]},
    {"key": "watchlist", "label": "Watchlist", "route": "/watchlist", "endpoint": "/v1/watchlist", "auth": "required", "status": "shared", "sections": ["decision_rows"], "response_keys": ["items"]},
    {"key": "alerts", "label": "Alerts", "route": "/alerts", "endpoint": "/v1/attention", "auth": "required", "status": "shared", "sections": ["attention_inbox"], "response_keys": ["events"]},
    {"key": "portfolio", "label": "Portfolio", "route": "/portfolio", "endpoint": "/v1/portfolio", "auth": "required", "status": "shared", "sections": ["holdings", "position_notes", "position_decisions", "portfolio_risk_summary"], "response_keys": ["workspace.holdings", "workspace.position_notes", "workspace.position_decisions", "workspace.portfolio_risk_summary"]},
    {"key": "ideas", "label": "Ideas", "route": "/ideas", "endpoint": "/v1/ideas", "auth": "required", "status": "shared", "sections": ["screen_request", "saved_screens", "criteria", "candidates", "evidence", "verify_next"], "response_keys": ["ideas", "generation"]},
    {"key": "calibration", "label": "Calibration", "route": "/calibration", "endpoint": "/v1/calibration", "auth": "required", "status": "shared", "sections": ["evidence_summary", "cohorts", "outcomes", "review_cases", "shadow_rules"], "response_keys": ["calibration", "shadow_rules"]},
    {"key": "health", "label": "System Health", "route": "/health", "endpoint": "/v1/system-health", "auth": "required", "status": "shared", "sections": ["status", "coverage", "issues", "worker_jobs", "checks"], "response_keys": ["status", "coverage", "issues", "worker_jobs", "checks"]},
    {"key": "methodology", "label": "Methodology", "route": "/methodology", "endpoint": "/v1/methodology", "auth": "public", "status": "shared", "sections": ["disclaimer", "actions", "methodology_sections", "quality_classifications"], "response_keys": ["disclaimer", "actions", "sections", "quality_classifications"]},
    {"key": "engine_updates", "label": "Engine Updates", "route": "/engine-updates", "endpoint": "/v1/operator/engine-updates", "auth": "owner", "status": "shared", "sections": ["rules_releases", "change_log", "validation"], "response_keys": ["updates"]},
]


QUALITY_CLASSIFICATIONS = [
    {"tier": "A", "label": "Quality A", "description": "Durable category leader; core-position candidate."},
    {"tier": "B", "label": "Quality B", "description": "Real moat with timing or execution risk; tactical and selective."},
    {"tier": "Speculative", "label": "Speculative", "description": "Real upside with binary risk; size accordingly."},
    {"tier": "Avoid", "label": "Quality Avoid", "description": "Structurally weak business; do not engage."},
]


def app_manifest_payload() -> dict[str, Any]:
    """Publish the backend-owned page map every frontend must implement."""
    pages = [dict(page) for page in PAGE_CONTRACTS]
    fingerprint = hashlib.sha256(json.dumps(pages, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    return {
        "contract_version": PUBLIC_CONTRACT_VERSION,
        "contract_fingerprint": fingerprint,
        "meta": response_meta(engine_version="saved-canonical-output", freshness="live"),
        "navigation": [page["key"] for page in pages],
        "pages": pages,
        "rules": {
            "backend_is_authoritative": True,
            "render_endpoint_payload_directly": True,
            "preserve_section_order": True,
            "preserve_backend_labels_and_copy": True,
            "no_client_decision_logic": True,
            "no_placeholder_market_data": True,
            "research_may_not_override_action": True,
            "show_data_as_of_on_every_page": True,
            "stale_state_must_be_red": True,
            "disable_actionable_language_when_blocked": True,
        },
    }


def ideas_payload(runs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Package saved thematic screens without inventing candidate data."""
    safe_runs = []
    for run in runs:
        result = run.get("result") if isinstance(run.get("result"), Mapping) else {}
        candidates = []
        for candidate in result.get("candidates") or []:
            if not isinstance(candidate, Mapping):
                continue
            candidates.append({key: candidate.get(key) for key in (
                "ticker", "company", "score", "theme_fit", "why_it_matters", "evidence", "verify_next",
                "_name", "_price", "_change", "_action", "_rs", "_market_cap", "_sector", "_industry",
                "_revenue_growth", "_debt_equity", "_starter",
            ) if key in candidate})
        safe_runs.append({
            "created_at": run.get("ts") or run.get("created_at"),
            "query": run.get("query"),
            "criteria": list(result.get("criteria") or []),
            "summary": result.get("summary"),
            "candidates": candidates,
            "metrics_refreshed_at": run.get("metrics_refreshed_at"),
        })
    return {
        "contract_version": PUBLIC_CONTRACT_VERSION,
        "meta": response_meta(),
        "ideas": safe_runs,
        "count": len(safe_runs),
        "generation": {"available": True, "request_url": "/v1/ideas/requests", "method": "POST", "poll_url_template": "/v1/idea-requests/{request_id}"},
    }


def methodology_payload() -> dict[str, Any]:
    """Canonical public explanation of the decision system and its limits."""
    return {
        "contract_version": PUBLIC_CONTRACT_VERSION,
        "meta": response_meta(engine_version="rules-2026.08-d", freshness="live"),
        "title": "How Trading Desk works",
        "disclaimer": "Trading Desk is research and decision support for educational and informational purposes. It does not provide personalized investment advice, execute trades, or guarantee outcomes.",
        "actions": [
            {"key": "enter", "meaning": "Evidence and upside justify initiating exposure."},
            {"key": "accumulate", "meaning": "The setup supports adding to existing exposure."},
            {"key": "watch", "meaning": "The thesis may be constructive, but a named trigger or better entry is still required."},
            {"key": "hold_off", "meaning": "Evidence or timing is not ready; do not initiate or add yet."},
            {"key": "avoid", "meaning": "Current risk/reward does not justify exposure."},
        ],
        "sections": [
            {"title": "Decision methodology", "body": "Deterministic rules produce the action, sizing, trigger, and invalidation. AI supplies research and dissent but cannot silently replace the rules action. Every signal carries an engine version and auditable decision receipt."},
            {"title": "Market data", "body": "Prices and indicators may be delayed, incomplete, adjusted, or temporarily unavailable. Data trust and freshness travel with the decision; blocked decisions should not be executed."},
            {"title": "Performance evidence", "body": "Directional calls use independent cohorts and 5-, 14-, and 30-session paths. Watch and Hold Off are evaluated as patience systems. Small samples are calibration evidence, not proof of future performance."},
            {"title": "Research boundary", "body": "Company research can explain, challenge, or add context to a call. It never changes the canonical action unless the deterministic engine itself changes."},
            {"title": "Privacy", "body": "Private accounts can store watchlists, holdings, notes, settings, research, and decision history. Public demo activity is not saved."},
            {"title": "Risk disclosure", "body": "Investing involves loss of principal. Signals can fail, gaps can bypass stops, and historical results do not predict future returns. Verify important information independently before acting."},
        ],
        "quality_classifications": QUALITY_CLASSIFICATIONS,
    }


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def response_meta(
    *,
    generated_at: str | None = None,
    engine_version: str | None = None,
    data_as_of: str | None = None,
    freshness: str = "unknown",
    request_id: str | None = None,
    refresh_url: str | None = None,
    refresh_method: str = "POST",
) -> dict[str, Any]:
    """Metadata every frontend response can render without interpreting rules."""
    normalized_freshness = str(freshness or "unknown").lower()
    is_stale = normalized_freshness in {"stale", "blocked", "expired"}
    return {
        "contract_version": PUBLIC_CONTRACT_VERSION,
        "generated_at": generated_at or _iso_now(),
        "engine_version": engine_version or "unknown",
        "data_as_of": data_as_of,
        "freshness": freshness,
        "stale": is_stale,
        "refresh_required": is_stale,
        "refresh": {
            "available": bool(refresh_url),
            "url": refresh_url,
            "method": refresh_method if refresh_url else None,
        },
        "display": {
            "show_data_as_of": True,
            "stale_color": "red",
            "disable_actionable_language_when_blocked": True,
        },
        "request_id": request_id,
    }


def error_payload(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    request_id: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Stable, non-secret error shape for web and mobile clients."""
    error = {
        "code": str(code or "unknown_error"),
        "message": str(message or "Trading Desk could not complete this request."),
        "retryable": bool(retryable),
    }
    if detail:
        error["detail"] = str(detail)
    return {"meta": response_meta(request_id=request_id), "error": error}


def _public_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "schema_version",
        "receipt_id",
        "ticker",
        "captured_at",
        "engine_version",
        "action",
        "price",
        "market_regime",
        "setup_score",
        "confidence",
        "entry_size",
        "setup_stage",
        "top_factors",
        "primary_risk",
        "trigger",
        "invalidation",
        "data_trust",
        "attribution",
        "source",
        "change_summary",
    )
    return {key: receipt.get(key) for key in allowed if key in receipt}


def decision_payload(
    receipt: Mapping[str, Any],
    *,
    portfolio_context: Mapping[str, Any] | None = None,
    research: Mapping[str, Any] | None = None,
    security_profile: Mapping[str, Any] | None = None,
    analyze_page: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the single canonical decision response consumed by any UI."""
    trust = receipt.get("data_trust") or {}
    input_snapshot = receipt.get("input_snapshot") or {}
    executable = bool(trust.get("executable", True)) if isinstance(trust, Mapping) else True
    decision = _public_receipt(receipt)
    data_as_of = (
        trust.get("as_of") if isinstance(trust, Mapping) else None
    ) or (
        input_snapshot.get("source_as_of") if isinstance(input_snapshot, Mapping) else None
    ) or receipt.get("captured_at")
    return {
        "contract_version": PUBLIC_CONTRACT_VERSION,
        "meta": response_meta(
            generated_at=str(receipt.get("captured_at") or "") or None,
            engine_version=str(receipt.get("engine_version") or "unknown"),
            data_as_of=str(data_as_of or "") or None,
            freshness=str((trust.get("freshness") if isinstance(trust, Mapping) else None) or "unknown"),
            refresh_url=f"/v1/decisions/{str(receipt.get('ticker') or '').upper()}/requests",
        ),
        "decision": decision,
        "security_profile": dict(security_profile or {}),
        "portfolio_context": dict(portfolio_context or {}),
        "research": dict(research or {"status": "unavailable"}),
        "analyze_page": dict(analyze_page or {}),
        "executable": executable,
    }


def analyze_page_payload(
    ticker: str,
    *,
    rule: Mapping[str, Any] | None = None,
    market: Mapping[str, Any] | None = None,
    report: Mapping[str, Any] | None = None,
    memo: Mapping[str, Any] | None = None,
    research: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a complete, ready-to-render Analyze page without UI inference."""
    rule = rule if isinstance(rule, Mapping) else {}
    market = market if isinstance(market, Mapping) else {}
    report = report if isinstance(report, Mapping) else {}
    memo = memo if isinstance(memo, Mapping) else {}
    research = research if isinstance(research, Mapping) else {}
    receipt = rule.get("decision_receipt") if isinstance(rule.get("decision_receipt"), Mapping) else {}
    trigger = receipt.get("trigger") if isinstance(receipt.get("trigger"), Mapping) else {}
    invalidation = receipt.get("invalidation") if isinstance(receipt.get("invalidation"), Mapping) else {}
    action = str(receipt.get("action") or rule.get("action") or "").lower()

    def first(*values):
        return next((value for value in values if value not in (None, "", [], {})), None)

    def number(value):
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    def text_items(value, limit=8):
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item).strip() for item in value if str(item or "").strip()][:limit]

    action_labels = {
        "enter_now": "Enter", "accumulate": "Accumulate", "watch": "Watch",
        "hold_off": "Hold Off", "avoid": "Avoid",
    }
    wait_action = action in {"watch", "hold_off"}
    entry_size = first(receipt.get("entry_size"), rule.get("entry_size"))
    if entry_size in (None, "") and wait_action:
        entry_size = "0% — wait for an actionable call"
    entry_text = first(trigger.get("text"), rule.get("trigger_summary"), rule.get("entry_status"))
    constraint = first(
        rule.get("extension_overlay_reason"), rule.get("reward_risk_gate_reason"),
        rule.get("matrix_reason"), receipt.get("primary_risk"),
    )
    change_text = first(invalidation.get("text"), entry_text, "Reassess when the technical structure changes.")

    setup_breakdown = rule.get("setup_score_breakdown") if isinstance(rule.get("setup_score_breakdown"), Mapping) else {}
    setup_components = []
    for component in setup_breakdown.get("components") or []:
        if isinstance(component, Mapping):
            setup_components.append({
                "label": component.get("label"), "points": number(component.get("points")),
                "max_points": number(component.get("max_points")), "note": component.get("note"),
            })
    checks = [
        {"key": "trend", "label": "Trend", "passed": bool(number(rule.get("price")) and number(rule.get("ma50")) and number(rule.get("price")) > number(rule.get("ma50")))},
        {"key": "relative_strength", "label": "Relative strength", "passed": bool(number(rule.get("rs")) and number(rule.get("rs")) >= 1)},
        {"key": "reward_risk", "label": "Reward/risk", "passed": bool(number(rule.get("reward_risk")) is not None and number(rule.get("reward_risk")) >= 1.2)},
        {"key": "trigger", "label": "Trigger", "passed": bool(rule.get("trigger_fired"))},
        {"key": "volume", "label": "Volume", "passed": bool(number(rule.get("vol_ratio")) is not None and number(rule.get("vol_ratio")) >= 0.6)},
    ]
    why = text_items(receipt.get("top_factors"))
    if constraint and str(constraint) not in why:
        why.append(str(constraint))
    changes = []
    if entry_text:
        changes.append({"kind": "upgrade", "text": str(entry_text)})
    if invalidation.get("text"):
        changes.append({"kind": "invalidate", "text": str(invalidation.get("text"))})

    price = number(first(rule.get("price"), market.get("price")))
    ma20, ma50, ma200 = number(rule.get("ma20")), number(rule.get("ma50")), number(rule.get("ma200"))
    rs, tech_delta, vol_ratio = number(rule.get("rs")), number(rule.get("tech_delta")), number(rule.get("vol_ratio"))
    range_position = number(rule.get("pct_of_52w_range"))
    technical = [
        {"key": "trend", "label": "Trend", "status": "Strong" if price and ma50 and ma200 and price > ma50 and price > ma200 else "Mixed", "detail": "Above both major moving averages." if price and ma50 and ma200 and price > ma50 and price > ma200 else "Price is not above both major moving averages."},
        {"key": "momentum", "label": "Momentum", "status": "Improving" if tech_delta is not None and tech_delta > 0 else ("Fading" if tech_delta is not None and tech_delta < 0 else "Stable"), "detail": "Technical score change over the last 10 sessions."},
        {"key": "strength", "label": "Strength", "status": "Leading" if rs is not None and rs >= 1 else "Lagging", "detail": "Relative performance versus the S&P 500."},
        {"key": "volume", "label": "Volume", "status": "Strong" if vol_ratio is not None and vol_ratio >= 1 else "Light", "detail": f"Participation is {vol_ratio:.2f}x the 20-day average." if vol_ratio is not None else "Volume reading unavailable."},
        {"key": "location", "label": "Location", "status": "Upper half" if range_position is not None and range_position >= 50 else "Lower half", "detail": "Position within the 52-week range."},
    ]

    meta = report.get("meta") if isinstance(report.get("meta"), Mapping) else {}
    quality = research.get("quality") if isinstance(research.get("quality"), Mapping) else {}
    report_generated = first(report.get("_worker_generated_at"), report.get("generated_at"), research.get("generated_at"))
    profile_fields = {
        key: first(meta.get(key), (market.get("security_profile") or {}).get(key) if isinstance(market.get("security_profile"), Mapping) else None)
        for key in (
            "earnings_date", "earnings_days", "expected_eps", "analyst_rec", "analyst_target", "analyst_n",
            "forward_pe", "trailing_pe", "peg", "ev_ebitda", "debt_to_equity", "earnings_growth",
            "revenue_growth", "gross_margins", "operating_margins", "profit_margins",
        )
    }
    target = number(profile_fields.get("analyst_target"))
    analyst_upside = ((target / price - 1) * 100) if target is not None and price else None
    return {
        "schema_version": 1,
        "ticker": str(ticker or "").upper(),
        "hero": {
            "action": action, "action_label": action_labels.get(action, action.replace("_", " ").title()),
            "size_now": entry_size, "entry_label": "Entry trigger" if wait_action else "Execution",
            "entry_trigger": {"text": entry_text, "price": number(trigger.get("price"))},
            "active_constraint": constraint,
            "what_changes_the_call": {"text": change_text, "price": number(invalidation.get("price"))},
        },
        "decision_evidence": {
            "setup_quality": {"score": number(first(setup_breakdown.get("score"), rule.get("setup_score"))), "components": setup_components},
            "confidence": first(receipt.get("confidence"), rule.get("decision_confidence"), rule.get("confidence")),
            "reward_risk": number(rule.get("reward_risk")),
            "setup_stage": first(rule.get("entry_status"), receipt.get("setup_stage"), rule.get("state")),
            "checks": checks,
            "passed_count": sum(1 for item in checks if item["passed"]),
            "total_count": len(checks),
            "data_trust": dict(receipt.get("data_trust") or {}),
        },
        "why_action": {"title": f"Why {action_labels.get(action, action).upper()}", "items": why},
        "call_changes": changes,
        "technical_picture": technical,
        "portfolio_manager": {
            "quality": dict(quality),
            "quality_classifications": QUALITY_CLASSIFICATIONS,
            "thesis": research.get("thesis"), "drivers": list(research.get("drivers") or []),
            "risks": list(research.get("risks") or []), "valuation": research.get("valuation"),
            "timing_watchpoint": research.get("timing_watchpoint"),
            "next_earnings": {"date": profile_fields.get("earnings_date"), "days": profile_fields.get("earnings_days"), "expected_eps": profile_fields.get("expected_eps")},
            "analyst_consensus": {"rating": profile_fields.get("analyst_rec"), "analyst_count": profile_fields.get("analyst_n"), "target": target, "upside_pct": analyst_upside},
            "lynch_check": {key: profile_fields.get(key) for key in ("earnings_growth", "peg", "forward_pe", "debt_to_equity")},
        },
        "full_research_report": {
            "status": research.get("status"), "available": bool(research.get("decision_memo")),
            "generated_at": report_generated, "request_endpoint": f"/v1/decisions/{str(ticker or '').upper()}/research/requests",
            "polling_note": "POST the request endpoint when signed in, then poll the returned poll_url until ready.",
            "sections": {
                "decision_memo": research.get("decision_memo"),
                "technical_narrative": research.get("technical_narrative"),
                "portfolio_manager_view": research.get("pm_narrative"),
            },
        },
    }


def security_profile_payload(
    ticker: str,
    *,
    report: Mapping[str, Any] | None = None,
    market: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return stable company metadata without exposing provider payloads."""
    report = report if isinstance(report, Mapping) else {}
    market = market if isinstance(market, Mapping) else {}
    report_meta = report.get("meta") if isinstance(report.get("meta"), Mapping) else {}
    market_profile = market.get("security_profile") if isinstance(market.get("security_profile"), Mapping) else {}

    def first(key: str):
        return next(
            (source.get(key) for source in (report_meta, market_profile, market) if source.get(key) not in (None, "")),
            None,
        )

    fields = (
        "company_name", "quote_type", "asset_category", "sector", "industry",
        "market_cap", "short_pct_float", "institutional_ownership_pct",
        "dividend_yield", "earnings_date", "earnings_days", "expected_eps",
        "analyst_rec", "analyst_target", "analyst_n", "forward_pe", "trailing_pe",
        "peg", "ev_ebitda", "debt_to_equity", "earnings_growth", "revenue_growth",
    )
    return {
        "ticker": str(ticker or "").upper(),
        **{key: first(key) for key in fields},
        "updated_at": first("updated_at"),
    }


def research_payload(
    ticker: str,
    *,
    report: Mapping[str, Any] | None = None,
    memo: Mapping[str, Any] | None = None,
    market: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize saved AI research without allowing it to alter the rule decision."""
    report = report if isinstance(report, Mapping) else {}
    memo = memo if isinstance(memo, Mapping) else {}
    market = market if isinstance(market, Mapping) else {}
    pm = report.get("pm") if isinstance(report.get("pm"), Mapping) else {}
    dossier = report.get("dossier") if isinstance(report.get("dossier"), Mapping) else {}
    bullets = dossier.get("bullets") if isinstance(dossier.get("bullets"), Mapping) else {}
    deep_dive = pm.get("deep_dive") if isinstance(pm.get("deep_dive"), Mapping) else {}

    def first(*values):
        return next((value for value in values if value not in (None, "", [], {})), None)

    def strings(value, limit=6):
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item).strip() for item in value if str(item or "").strip()][:limit]

    # The dossier is generated after the compact PM snapshot. Prefer its
    # fields so a successful Claude dossier cannot be masked by an earlier
    # rules-backed compact fallback.
    thesis = first(bullets.get("thesis"), pm.get("thesis"), memo.get("thesis"))
    company_overview = first(
        (report.get("meta") or {}).get("long_business_summary") if isinstance(report.get("meta"), Mapping) else None,
        deep_dive.get("business"), pm.get("business"), memo.get("business"),
        dossier.get("pm_narrative"), memo.get("pm_narrative"), thesis,
    )
    generated_at = first(
        report.get("_worker_generated_at"), report.get("generated_at"),
        memo.get("_worker_generated_at"), memo.get("generated_at"), memo.get("updated_at"),
    )
    source = first(dossier.get("_source"), pm.get("_source"), memo.get("_source"), memo.get("source"))
    dossier_completed = "claude" in str(dossier.get("_source") or "").lower()
    quality = first(dossier.get("quality"), pm.get("quality"), memo.get("quality"), {})
    has_research = bool(company_overview or thesis)
    stale_reasons = []
    age_days = None
    if has_research:
        try:
            generated = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=timezone.utc)
            age_days = max(0, (datetime.now(timezone.utc) - generated.astimezone(timezone.utc)).days)
            if age_days >= 7:
                stale_reasons.append(f"Research is {age_days} days old.")
        except (TypeError, ValueError):
            stale_reasons.append("Research date is unavailable.")
        report_price = first(report.get("_market_price"), pm.get("_market_price"), memo.get("_market_price"))
        current_price = first(market.get("price"), market.get("last"))
        try:
            price_move = abs(float(current_price) / float(report_price) - 1) * 100
            if price_move >= 10:
                stale_reasons.append(f"Price has moved {price_move:.0f}% since this research was generated.")
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    status = "unavailable" if not has_research else ("stale" if stale_reasons else "ready")
    result = {
        "status": status,
        "ticker": str(ticker or "").upper(),
        "company_name": first((report.get("meta") or {}).get("company_name") if isinstance(report.get("meta"), Mapping) else None, market.get("company_name")),
        "company_overview": company_overview,
        "thesis": thesis,
        # Once Claude completed a dossier, never splice rule-fallback prose into
        # missing dossier fields. Empty is more honest than a contradictory
        # hybrid memo that claims Claude did not complete.
        "drivers": strings(bullets.get("drivers") if dossier_completed else first(bullets.get("drivers"), pm.get("drivers"), memo.get("drivers"), [])),
        "risks": strings(bullets.get("risks") if dossier_completed else first(bullets.get("risks"), pm.get("risks"), memo.get("risks"), [])),
        "valuation": bullets.get("valuation") if dossier_completed else first(bullets.get("valuation"), pm.get("valuation"), memo.get("valuation")),
        "timing_watchpoint": bullets.get("timing_watchpoint") if dossier_completed else first(bullets.get("timing_watchpoint"), pm.get("timing_watchpoint"), memo.get("timing_watchpoint")),
        "decision_memo": first(dossier.get("dossier"), report.get("dossier") if isinstance(report.get("dossier"), str) else None, memo.get("dossier")),
        "technical_narrative": first(dossier.get("technical_narrative"), memo.get("technical_narrative")),
        "pm_narrative": first(dossier.get("pm_narrative"), memo.get("pm_narrative")),
        "quality": dict(quality) if isinstance(quality, Mapping) else {},
        "generated_at": generated_at,
        "age_days": age_days,
        "stale_reasons": stale_reasons,
        "source": source,
    }
    return result


def attention_payload(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return ordered, privacy-safe attention events for a client inbox."""
    safe_events = []
    allowed = ("event_id", "ticker", "kind", "priority", "title", "detail")
    for event in events:
        safe_events.append({key: event.get(key) for key in allowed if key in event})
    priority_counts: dict[str, int] = {}
    kind_counts: dict[str, int] = {}
    tickers = []
    for event in safe_events:
        priority = str(event.get("priority") or "normal").lower()
        kind = str(event.get("kind") or "other").lower()
        priority_counts[priority] = priority_counts.get(priority, 0) + 1
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        ticker = str(event.get("ticker") or "").upper()
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    urgent = sum(priority_counts.get(key, 0) for key in ("critical", "high"))
    workflow = {
        "status": "needs_attention" if urgent else ("review" if safe_events else "clear"),
        "headline": (
            f"{urgent} high-priority item{'s' if urgent != 1 else ''} need review."
            if urgent else (f"{len(safe_events)} item{'s' if len(safe_events) != 1 else ''} to review." if safe_events else "Nothing needs attention right now.")
        ),
        "total_items": len(safe_events),
        "urgent_items": urgent,
        "affected_tickers": tickers,
        "priority_counts": priority_counts,
        "event_type_counts": kind_counts,
        "next_step": "Review the highest-priority event first." if safe_events else "No action is required.",
    }
    return {
        "contract_version": PUBLIC_CONTRACT_VERSION,
        "meta": response_meta(),
        "events": safe_events,
        "count": len(safe_events),
        "daily_workflow_summary": workflow,
    }


def regime_payload(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Package the saved canonical regime without recomputing it in the client."""
    allowed = (
        "schema_version",
        "day",
        "portfolio_stance",
        "score",
        "reasons",
        "assets",
        "macro",
        "news",
        "news_errors",
        "signals",
        "errors",
        "source",
        "opportunity_action",
        "opportunity_label",
        "entry_timing",
        "change_label",
        "change_detail",
        "why_today",
        "market_highlights",
        "drivers",
        "risks",
        "watch_triggers",
        "forward_watch",
        "crypto_regime",
        "data_trust",
    )
    safe = {key: snapshot.get(key) for key in allowed if key in snapshot}
    return {
        "contract_version": PUBLIC_CONTRACT_VERSION,
        "meta": response_meta(
            generated_at=str(snapshot.get("generated_at") or "") or None,
            engine_version=str(snapshot.get("engine_version") or "unknown"),
            data_as_of=str(snapshot.get("data_as_of") or "") or None,
            freshness=str(snapshot.get("freshness") or "unknown"),
            refresh_url="/v1/regime/requests",
        ),
        "regime": safe,
    }


def watchlist_payload(items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return decision summaries for an authenticated user's ordered watchlist."""
    allowed = (
        "ticker",
        "company_name",
        "price",
        "change_pct",
        "action",
        "confidence",
        "trigger_price",
        "invalidation_price",
        "attention_priority",
        "data_trust",
    )
    safe_items = [{key: item.get(key) for key in allowed if key in item} for item in items]
    freshness_values = [str((item.get("data_trust") or {}).get("freshness") or "unknown").lower() for item in safe_items]
    aggregate_freshness = "stale" if any(value in {"stale", "blocked", "expired"} for value in freshness_values) else ("fresh" if freshness_values and all(value in {"fresh", "live", "trusted"} for value in freshness_values) else "unknown")
    return {
        "contract_version": PUBLIC_CONTRACT_VERSION,
        "meta": response_meta(freshness=aggregate_freshness),
        "items": safe_items,
        "count": len(safe_items),
    }


def user_workspace_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return only frontend-owned private state after server-side auth/RLS checks."""
    allowed = (
        "watchlist",
        "holdings",
        "notes",
        "position_notes",
        "settings",
        "notification_preferences",
        "account_size",
        "risk_per_trade",
        "max_position_pct",
        "position_decisions",
        "portfolio_risk_summary",
    )
    return {
        "contract_version": PUBLIC_CONTRACT_VERSION,
        "meta": response_meta(),
        "workspace": {key: state.get(key) for key in allowed if key in state},
    }
