"""Stable, presentation-neutral payloads for a future public frontend.

This module intentionally contains no trading rules.  It packages canonical
engine output so a web client cannot accidentally develop a second decision
engine with different actions, sizing, or trust semantics.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


PUBLIC_CONTRACT_VERSION = 1


def _public_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
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
        "entry_stage",
        "top_factors",
        "primary_risk",
        "trigger",
        "invalidation",
        "data_trust",
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
    return {
        "contract_version": PUBLIC_CONTRACT_VERSION,
        "decision": _public_receipt(receipt),
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
        "events": safe_events,
        "count": len(safe_events),
    }
