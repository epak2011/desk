"""Stable, presentation-neutral payloads for a future public frontend.

This module intentionally contains no trading rules.  It packages canonical
engine output so a web client cannot accidentally develop a second decision
engine with different actions, sizing, or trust semantics.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


PUBLIC_CONTRACT_VERSION = 2


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def response_meta(
    *,
    generated_at: str | None = None,
    engine_version: str | None = None,
    data_as_of: str | None = None,
    freshness: str = "unknown",
    request_id: str | None = None,
) -> dict[str, Any]:
    """Metadata every frontend response can render without interpreting rules."""
    return {
        "contract_version": PUBLIC_CONTRACT_VERSION,
        "generated_at": generated_at or _iso_now(),
        "engine_version": engine_version or "unknown",
        "data_as_of": data_as_of,
        "freshness": freshness,
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
) -> dict[str, Any]:
    """Return the single canonical decision response consumed by any UI."""
    trust = receipt.get("data_trust") or {}
    executable = bool(trust.get("executable", True)) if isinstance(trust, Mapping) else True
    decision = _public_receipt(receipt)
    return {
        "contract_version": PUBLIC_CONTRACT_VERSION,
        "meta": response_meta(
            generated_at=str(receipt.get("captured_at") or "") or None,
            engine_version=str(receipt.get("engine_version") or "unknown"),
            data_as_of=(trust.get("as_of") if isinstance(trust, Mapping) else None),
            freshness=str((trust.get("freshness") if isinstance(trust, Mapping) else None) or "unknown"),
        ),
        "decision": decision,
        "security_profile": dict(security_profile or {}),
        "portfolio_context": dict(portfolio_context or {}),
        "research": dict(research or {"status": "unavailable"}),
        "executable": executable,
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
        "dividend_yield", "earnings_date",
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
        "drivers": strings(first(bullets.get("drivers"), pm.get("drivers"), memo.get("drivers"), [])),
        "risks": strings(first(bullets.get("risks"), pm.get("risks"), memo.get("risks"), [])),
        "valuation": first(bullets.get("valuation"), pm.get("valuation"), memo.get("valuation")),
        "timing_watchpoint": first(bullets.get("timing_watchpoint"), pm.get("timing_watchpoint"), memo.get("timing_watchpoint")),
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
    return {
        "contract_version": PUBLIC_CONTRACT_VERSION,
        "meta": response_meta(),
        "events": safe_events,
        "count": len(safe_events),
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
    return {
        "contract_version": PUBLIC_CONTRACT_VERSION,
        "meta": response_meta(),
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
    )
    return {
        "contract_version": PUBLIC_CONTRACT_VERSION,
        "meta": response_meta(),
        "workspace": {key: state.get(key) for key in allowed if key in state},
    }
