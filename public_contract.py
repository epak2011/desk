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
        "portfolio_context": dict(portfolio_context or {}),
        "executable": executable,
    }


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
