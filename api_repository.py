"""Database-facing application service for the standalone frontend API."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

import attention_engine
import backend_layer
import public_contract
import user_state_store


TICKER_PATTERN = re.compile(r"^[A-Z0-9.-]{1,15}$")
WORKSPACE_FIELDS = {
    "watchlist",
    "holdings",
    "notes",
    "position_notes",
    "settings",
    "notification_preferences",
}


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


def normalize_ticker(value: str) -> str:
    ticker = str(value or "").upper().strip()
    if not TICKER_PATTERN.fullmatch(ticker):
        raise ValueError("Ticker is invalid or unsupported.")
    return ticker


def _revision(state: dict[str, Any]) -> str:
    public_state = {key: value for key, value in state.items() if key != "_revision"}
    encoded = json.dumps(public_state, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _workspace_state(user_id: str) -> dict[str, Any]:
    with backend_layer.db_connection() as conn:
        with conn.cursor() as cur:
            state = user_state_store.load(cur, user_id) or {}
    state = dict(state) if isinstance(state, dict) else {}
    state["_revision"] = str(state.get("_revision") or _revision(state))
    return state


def health() -> dict[str, Any]:
    return {
        "status": "ok" if backend_layer.has_database() else "degraded",
        "contract_version": public_contract.PUBLIC_CONTRACT_VERSION,
        "engine_version": "saved-canonical-output",
    }


def decision(ticker: str) -> dict[str, Any]:
    ticker = normalize_ticker(ticker)
    rule = (backend_layer.read_json_table("rule_outputs", ticker) or {}).get(ticker) or {}
    if not rule:
        raise NotFoundError(f"No saved decision is available for {ticker}.")
    receipt = rule.get("decision_receipt") if isinstance(rule.get("decision_receipt"), dict) else {}
    if not receipt:
        raise NotFoundError(f"No canonical decision receipt is available for {ticker}.")
    return public_contract.decision_payload(receipt)


def regime() -> dict[str, Any]:
    rows = backend_layer.read_json_table("market_regime_daily", limit=1)
    if not rows:
        raise NotFoundError("No saved market regime is available.")
    snapshot = next(iter(rows.values()))
    return public_contract.regime_payload(snapshot)


def workspace(user_id: str) -> dict[str, Any]:
    state = _workspace_state(user_id)
    payload = public_contract.user_workspace_payload(state)
    payload["workspace"]["revision"] = state["_revision"]
    return payload


def patch_workspace(user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    patch = patch if isinstance(patch, dict) else {}
    state = _workspace_state(user_id)
    supplied_revision = str(patch.get("revision") or "").strip()
    if supplied_revision and supplied_revision != state["_revision"]:
        raise ConflictError("Workspace changed since it was last loaded.")
    updated = dict(state)
    updated.pop("_revision", None)
    for key in WORKSPACE_FIELDS:
        if key in patch:
            updated[key] = patch[key]
    updated["_revision"] = _revision(updated)
    with backend_layer.db_connection() as conn:
        with conn.cursor() as cur:
            user_state_store.save(cur, user_id, updated)
    return workspace(user_id)


def _watchlist_items(tickers: Iterable[str]) -> list[dict[str, Any]]:
    clean = [normalize_ticker(ticker) for ticker in tickers]
    rule_rows = backend_layer.read_json_table_many("rule_outputs", clean)
    market_rows = backend_layer.read_json_table_many("market_snapshots", clean)
    items = []
    for ticker in clean:
        rule = rule_rows.get(ticker) or {}
        market = market_rows.get(ticker) or {}
        receipt = rule.get("decision_receipt") if isinstance(rule.get("decision_receipt"), dict) else {}
        trigger = receipt.get("trigger") if isinstance(receipt.get("trigger"), dict) else {}
        invalidation = receipt.get("invalidation") if isinstance(receipt.get("invalidation"), dict) else {}
        items.append({
            "ticker": ticker,
            "company_name": market.get("company_name") or rule.get("company_name"),
            "price": market.get("price") or market.get("last") or receipt.get("price"),
            "change_pct": market.get("change_pct"),
            "action": receipt.get("action") or rule.get("action"),
            "confidence": receipt.get("confidence") or rule.get("decision_confidence"),
            "trigger_price": trigger.get("price"),
            "invalidation_price": invalidation.get("price"),
            "data_trust": receipt.get("data_trust") or rule.get("data_trust") or {},
        })
    return items


def watchlist(user_id: str) -> dict[str, Any]:
    state = _workspace_state(user_id)
    tickers = state.get("watchlist") if isinstance(state.get("watchlist"), list) else []
    return public_contract.watchlist_payload(_watchlist_items(tickers))


def set_watchlist_ticker(user_id: str, ticker: str, *, present: bool) -> dict[str, Any]:
    ticker = normalize_ticker(ticker)
    state = _workspace_state(user_id)
    tickers = [normalize_ticker(item) for item in state.get("watchlist", []) if str(item or "").strip()]
    if present and ticker not in tickers:
        tickers.append(ticker)
    if not present:
        tickers = [item for item in tickers if item != ticker]
    return patch_workspace(user_id, {"watchlist": tickers, "revision": state["_revision"]}) and watchlist(user_id)


def attention(user_id: str) -> dict[str, Any]:
    state = _workspace_state(user_id)
    tickers = [normalize_ticker(item) for item in state.get("watchlist", []) if str(item or "").strip()]
    rules = backend_layer.read_json_table_many("rule_outputs", tickers)
    markets = backend_layer.read_json_table_many("market_snapshots", tickers)
    holdings = state.get("holdings") if isinstance(state.get("holdings"), dict) else {}
    rows = []
    for ticker in tickers:
        rule = rules.get(ticker) or {}
        market = markets.get(ticker) or {}
        receipt = rule.get("decision_receipt") if isinstance(rule.get("decision_receipt"), dict) else {}
        invalidation = receipt.get("invalidation") if isinstance(receipt.get("invalidation"), dict) else {}
        monitor = rule.get("trigger_monitor") if isinstance(rule.get("trigger_monitor"), dict) else {}
        rows.append({
            "ticker": ticker,
            "action": receipt.get("action") or rule.get("action"),
            "price": market.get("price") or market.get("last") or receipt.get("price"),
            "invalidation_price": invalidation.get("price"),
            "receipt": receipt,
            "market_fresh": str((receipt.get("data_trust") or {}).get("freshness") or "").lower() != "stale",
            "trigger_status": monitor.get("status"),
            "trigger_detail": monitor.get("detail") or (receipt.get("trigger") or {}).get("text"),
            "distance_pct": monitor.get("distance_pct"),
            "trigger_sessions_ago": rule.get("trigger_sessions_ago"),
            "earnings_days": rule.get("earnings_days"),
        })
    events = attention_engine.build_attention_events(rows, holdings=holdings.keys())
    normalized = [{**event, "event_id": event.get("event_id") or event.get("id")} for event in events]
    return public_contract.attention_payload(normalized)


def portfolio(user_id: str) -> dict[str, Any]:
    state = _workspace_state(user_id)
    return public_contract.user_workspace_payload({
        "holdings": state.get("holdings") or {},
        "position_notes": state.get("position_notes") or {},
        "settings": state.get("settings") or {},
    })


def calibration() -> dict[str, Any]:
    return {
        "meta": public_contract.response_meta(),
        "calibration": backend_layer.read_engine_review_status(),
    }
