"""Trading Desk background worker.

Run locally:
    python worker.py --once
    python worker.py --drain --max-jobs 25

Run on a hosted worker:
    python worker.py --loop --sleep 10

This is deliberately not a Streamlit script. It processes queued jobs from
Supabase and writes durable results back to tables the UI can read quickly.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import traceback
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import yfinance as yf

import backend_layer as backend
import attention_engine
import decision_contract
import data_trust
import engine_evaluation
import email_delivery
import notification_engine
import unsubscribe
from pm_view import get_decision_dossier, get_pm_view
import tactical


SCHEDULED_SAFE_JOB_TYPES = ["market_snapshot", "watchlist_market_scan"]
SCHEDULED_SAFE_RUNTIME_SECONDS = 240
LEGACY_IGNORED_JOB_TYPES = {"pm_memo"}
OUTCOME_SCORE_VERSION = engine_evaluation.EVALUATION_VERSION
OUTCOME_MIN_AGE_DAYS = 7
RULE_ENGINE_VERSION = "rules-2026.08-b"


def drain_notification_outbox(limit: int = 10) -> dict:
    """Deliver prebuilt messages only when the provider is explicitly enabled."""
    config = email_delivery.config_from_env()
    if not config.ready:
        return {"enabled": False, "claimed": 0, "sent": 0, "failed": 0}
    rows = backend.claim_notifications(limit=limit, max_attempts=3)
    sent = 0
    failed = 0
    for row in rows:
        try:
            if "unsubscribe" not in str(row.get("html") or "").lower():
                raise email_delivery.DeliveryError("Message has no unsubscribe control.")
            provider_id = email_delivery.send_email(
                recipient=row["recipient"],
                subject=row["subject"],
                html=row["html"],
                config=config,
            )
            backend.complete_notification(row["id"], provider_id)
            sent += 1
        except Exception as exc:
            retry = int(row.get("attempts") or 1) < 3
            backend.fail_notification(row["id"], str(exc), retry=retry)
            failed += 1
    return {"enabled": True, "claimed": len(rows), "sent": sent, "failed": failed}


def queue_daily_user_digests(*, now: datetime | None = None) -> dict:
    """Build one privacy-scoped digest per opted-in user after the ET send hour."""
    config = email_delivery.config_from_env()
    base_url = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    secret = os.environ.get("UNSUBSCRIBE_SECRET", "").strip()
    if not config.ready or not base_url or not secret:
        return {"enabled": False, "users": 0, "queued": 0, "empty": 0}
    current = now or datetime.now(timezone.utc)
    eastern = current.astimezone(ZoneInfo("America/New_York"))
    send_hour = max(0, min(23, int(os.environ.get("DIGEST_SEND_HOUR_ET", "17") or 17)))
    if eastern.hour < send_hour:
        return {"enabled": True, "users": 0, "queued": 0, "empty": 0, "waiting": True}

    review = backend.read_engine_review_status() or {}
    logic_alerts = review.get("alerting") or []
    users = backend.notification_users()
    queued = 0
    empty = 0
    for user in users:
        user_id = str(user.get("user_id") or "")
        state = user.get("state") if isinstance(user.get("state"), dict) else {}
        preferences = state.get("notification_preferences") or {}
        recipient = str(preferences.get("email") or "").strip().lower()
        tickers = [str(t).upper().strip() for t in state.get("watchlist", []) if str(t or "").strip()]
        holdings = (state.get("holdings") or {}).keys() if isinstance(state.get("holdings"), dict) else []
        snapshots = state.get("ticker_snapshots") if isinstance(state.get("ticker_snapshots"), dict) else {}
        rows = []
        for ticker in tickers:
            snapshot = snapshots.get(ticker) if isinstance(snapshots.get(ticker), dict) else {}
            market = snapshot.get("market") if isinstance(snapshot.get("market"), dict) else {}
            meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
            receipt = snapshot.get("decision_receipt") if isinstance(snapshot.get("decision_receipt"), dict) else {}
            trigger = market.get("trigger_monitor") if isinstance(market.get("trigger_monitor"), dict) else {}
            invalidation = receipt.get("invalidation") if isinstance(receipt.get("invalidation"), dict) else {}
            rows.append({
                "ticker": ticker,
                "action": _normalize_action_key(market.get("action") or receipt.get("action")),
                "price": market.get("last") or market.get("price"),
                "receipt": receipt,
                "invalidation_price": invalidation.get("price") or market.get("stop"),
                "trigger_status": trigger.get("status"),
                "trigger_detail": trigger.get("detail") or trigger.get("label"),
                "distance_pct": trigger.get("distance_pct"),
                "earnings_days": meta.get("earnings_days"),
            })
        events = attention_engine.build_attention_events(
            rows,
            holdings=holdings,
            logic_alerts=logic_alerts,
        )
        digest = notification_engine.build_digest(user_id, events, day=eastern.date())
        if not digest["should_send"]:
            empty += 1
            continue
        token = unsubscribe.create_token(user_id, recipient, secret)
        unsubscribe_url = f"{base_url}/?unsubscribe={urllib.parse.quote(token)}"
        message_html = notification_engine.render_digest_html(events, unsubscribe_url=unsubscribe_url)
        inserted = backend.enqueue_notification(
            user_id=user_id,
            digest_key=digest["digest_key"],
            recipient=recipient,
            subject=f"Trading Desk: {digest['count']} item{'s' if digest['count'] != 1 else ''} need attention",
            html=message_html,
        )
        queued += 1 if inserted else 0
    return {"enabled": True, "users": len(users), "queued": queued, "empty": empty}


def _gha_warning(message: str) -> None:
    """Emit a GitHub Actions warning without turning the run red."""
    clean = str(message or "").replace("\n", " ")[:900]
    print(f"::warning::{clean}")


def _download_history(ticker: str):
    return yf.download(ticker, period="2y", interval="1d", auto_adjust=True, progress=False, threads=False, timeout=10)


def _download_benchmark():
    return yf.download("SPY", period="2y", interval="1d", auto_adjust=True, progress=False, threads=False, timeout=10)


def _flatten_yfinance(hist, ticker: str):
    if hist is None or hist.empty:
        return hist
    if hasattr(hist.columns, "nlevels") and hist.columns.nlevels > 1:
        # yfinance may return either field->ticker or ticker->field. Support both.
        if ticker in hist.columns.get_level_values(0):
            hist = hist[ticker]
        elif ticker in hist.columns.get_level_values(-1):
            hist = hist.xs(ticker, axis=1, level=-1)
    return hist.dropna()


def _market_payload(ticker: str, hist, t_state: dict):
    close = hist["Close"]
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) > 1 else last
    change_pct = ((last / prev) - 1) * 100 if prev else 0.0
    return {
        "ticker": ticker,
        "price": last,
        "change_pct": change_pct,
        "high_52w": t_state.get("high_52w"),
        "low_52w": t_state.get("low_52w"),
        "pct_of_52w_range": t_state.get("pct_of_52w_range"),
        "volume_ratio": t_state.get("vol_ratio"),
        "ma20": t_state.get("ma20"),
        "ma50": t_state.get("ma50"),
        "ma100": t_state.get("ma100"),
        "ma200": t_state.get("ma200"),
        "rs": t_state.get("rs"),
        "rsi14": t_state.get("rsi14"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


RULE_AUTOLOG_VERSION = 1
RULE_AUTOLOG_ACTIONS = {"enter_now", "watch", "hold_off", "avoid", "accumulate"}
RULE_ACTION_LABELS = {
    "enter_now": ("🚀", "Enter"),
    "watch": ("👀", "Watch"),
    "hold_off": ("🤔", "Hold off"),
    "avoid": ("⛔", "Avoid"),
    "accumulate": ("🌱", "Accumulate"),
}


def _num_or_none(value):
    try:
        if value is None:
            return None
        value = float(value)
        if value != value:
            return None
        return value
    except Exception:
        return None


def _normalize_action_key(raw):
    value = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "enter": "enter_now",
        "buy": "enter_now",
        "enter_long": "enter_now",
        "holdoff": "hold_off",
        "hold": "hold_off",
    }
    return aliases.get(value, value)


def _rule_log_price_key(value):
    num = _num_or_none(value)
    if num is None:
        return "na"
    return f"{num:.2f}"


def _rule_log_signature(t_state: dict) -> str:
    trigger = t_state.get("trigger") if isinstance(t_state.get("trigger"), dict) else {}
    levels = trigger.get("levels") if isinstance(trigger.get("levels"), dict) else {}
    pieces = [
        _normalize_action_key(t_state.get("action")) or "",
        str(t_state.get("state") or ""),
        str(trigger.get("kind") or ""),
        _rule_log_price_key(levels.get("buy_above") or t_state.get("entry")),
        _rule_log_price_key(levels.get("abort_below") or t_state.get("stop")),
        _rule_log_price_key(t_state.get("t1")),
    ]
    return "|".join(pieces)


def _rule_log_level_snapshot(t_state: dict) -> dict:
    trigger = t_state.get("trigger") if isinstance(t_state.get("trigger"), dict) else {}
    levels = trigger.get("levels") if isinstance(trigger.get("levels"), dict) else {}
    out = {
        "entry_price": _num_or_none(t_state.get("entry")),
        "stop_price": _num_or_none(t_state.get("stop")),
        "target1_price": _num_or_none(t_state.get("t1")),
        "target2_price": _num_or_none(t_state.get("t2")),
        "trigger_price": _num_or_none(levels.get("buy_above")),
        "invalidation_price": _num_or_none(levels.get("abort_below")),
    }
    return {
        key: round(value, 2) if value is not None else None
        for key, value in out.items()
    }


def _trigger_summary(t_state: dict) -> str:
    trigger = t_state.get("trigger") if isinstance(t_state.get("trigger"), dict) else {}
    summary = trigger.get("detail") or trigger.get("summary") or t_state.get("trigger_summary")
    if summary:
        return str(summary)
    action = _normalize_action_key(t_state.get("action"))
    price = _num_or_none(t_state.get("price") or t_state.get("last"))
    entry = _num_or_none(t_state.get("entry"))
    if action == "enter_now" and price is not None:
        return f"Enter long at market — ${price:,.2f}."
    if entry is not None:
        return f"Watch trigger near ${entry:,.2f}."
    return "Rules action recorded from scheduled market scan."


def auto_log_rule_decision(ticker: str, t_state: dict, *, source: str = "worker") -> bool:
    """Persist one rules decision when the worker computes a fresh rules state."""
    tkr = str(ticker or "").upper().strip()
    if not tkr or not isinstance(t_state, dict):
        return False
    action = _normalize_action_key(t_state.get("action"))
    if action not in RULE_AUTOLOG_ACTIONS:
        return False
    price = _num_or_none(t_state.get("price") or t_state.get("last"))
    if price is None:
        return False
    signature = _rule_log_signature(t_state)
    if backend.recent_auto_rule_decision_exists(tkr, signature, within_days=7):
        return False

    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    signature_hash = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:10]
    emoji, label = RULE_ACTION_LABELS.get(action, ("", action.replace("_", " ").title()))
    trigger = t_state.get("trigger") if isinstance(t_state.get("trigger"), dict) else {}
    entry = {
        "id": f"rules-auto-{tkr}-{today_key}-{action}-{signature_hash}",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ticker": tkr,
        "price": round(price, 2),
        "rule_action": action,
        "rule_state": t_state.get("state"),
        "rule_signature": signature,
        "rule_source": source,
        "rule_autolog_version": RULE_AUTOLOG_VERSION,
        "rule_trace": t_state.get("_rule_trace") or [],
        "setup_score": t_state.get("setup_score"),
        "reward_risk": t_state.get("reward_risk"),
        "rs": t_state.get("rs"),
        "vol_ratio": t_state.get("vol_ratio"),
        "trigger_kind": trigger.get("kind"),
        "trigger_summary": _trigger_summary(t_state),
        "trigger_status": trigger.get("status"),
        "entry_is_projected": bool(t_state.get("entry_is_projected")),
        "auto_logged": True,
        "source": "rules_engine",
        "source_label": f"{emoji} {label}",
        "rule_engine_version": RULE_ENGINE_VERSION,
        "decision_receipt": decision_contract.build_decision_receipt(
            tkr,
            {
                **t_state,
                "trigger_summary": _trigger_summary(t_state),
                "data_trust": data_trust.assess_decision_data(t_state),
            },
            engine_version=RULE_ENGINE_VERSION,
        ),
        "outcome": None,
        **_rule_log_level_snapshot(t_state),
    }
    backend.upsert_decision_log(entry)
    return True


def _api_key() -> str:
    """Read Claude API key from the canonical name plus legacy aliases."""
    aliases = ("ANTHROPIC_API_KEY", "NTHROPIC_API_KEY", "CLAUDE_API_KEY")
    for name in aliases:
        key = os.environ.get(name, "").strip()
        if key:
            return key
    try:
        import streamlit as st  # type: ignore

        for name in aliases:
            key = str(st.secrets.get(name, "")).strip()
            if key:
                return key
    except Exception:
        pass
    return ""


def _quote_meta(ticker: str) -> dict:
    try:
        info = yf.Ticker(ticker).get_info() or {}
    except Exception:
        info = {}
    return {
        "company_name": info.get("shortName") or info.get("longName") or ticker,
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "market_cap": info.get("marketCap"),
        "forward_pe": info.get("forwardPE"),
        "peg": info.get("pegRatio"),
        "ev_ebitda": info.get("enterpriseToEbitda"),
        "earnings_growth": info.get("earningsGrowth"),
        "revenue_growth": info.get("revenueGrowth"),
        "debt_to_equity": info.get("debtToEquity"),
        "analyst_rec": info.get("recommendationKey"),
        "analyst_target": info.get("targetMeanPrice"),
    }


def refresh_market_snapshot(ticker: str, bench=None) -> dict:
    ticker = str(ticker or "").upper().strip()
    if not ticker:
        raise ValueError("ticker is required")
    hist = _flatten_yfinance(_download_history(ticker), ticker)
    bench = _flatten_yfinance(bench if bench is not None else _download_benchmark(), "SPY")
    if hist is None or hist.empty or bench is None or bench.empty:
        raise RuntimeError(f"No market history returned for {ticker}")
    t_state = tactical.compute(hist, bench)
    if not t_state:
        raise RuntimeError(f"Rule engine could not compute {ticker}")
    market_payload = _market_payload(ticker, hist, t_state)
    t_state["price"] = market_payload.get("price", t_state.get("price"))
    rule_payload = dict(t_state)
    trigger = rule_payload.get("trigger") or {}
    if isinstance(trigger, dict):
        rule_payload["trigger_summary"] = trigger.get("summary")
    backend.upsert_json_table("market_snapshots", "ticker", ticker, market_payload, source="yahoo")
    backend.upsert_json_table("rule_outputs", "ticker", ticker, rule_payload, source="rules")
    logged = auto_log_rule_decision(ticker, rule_payload, source="worker_market_scan")
    return {
        "ticker": ticker,
        "price": market_payload.get("price"),
        "action": rule_payload.get("action"),
        "auto_logged": logged,
        "updated_at": market_payload.get("updated_at"),
    }


def _fresh_tactical_state(ticker: str) -> tuple[dict, dict]:
    """Recompute the rule state so PM work is tied to current market data."""
    ticker = str(ticker or "").upper().strip()
    hist = _flatten_yfinance(_download_history(ticker), ticker)
    bench = _flatten_yfinance(_download_benchmark(), "SPY")
    if hist is None or hist.empty or bench is None or bench.empty:
        raise RuntimeError(f"No market history returned for {ticker}")
    t_state = tactical.compute(hist, bench) or {}
    if not t_state:
        raise RuntimeError(f"Rule engine could not compute {ticker}")
    market_payload = _market_payload(ticker, hist, t_state)
    t_state["price"] = market_payload.get("price", t_state.get("price"))
    backend.upsert_json_table("market_snapshots", "ticker", ticker, market_payload, source="yahoo")
    backend.upsert_json_table("rule_outputs", "ticker", ticker, t_state, source="rules")
    return t_state, _quote_meta(ticker)


def refresh_full_report(ticker: str) -> dict:
    ticker = str(ticker or "").upper().strip()
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured for the worker")
    t_state, meta = _fresh_tactical_state(ticker)
    company_name = meta.get("company_name") or ticker
    pm = get_pm_view(ticker, t_state, api_key=api_key, company_name=company_name)
    dossier = get_decision_dossier(
        ticker,
        t_state,
        modifiers=[],
        meta=meta,
        pm_data=pm,
        api_key=api_key,
        company_name=company_name,
        fast=False,
    )
    generated_at = datetime.now(timezone.utc).isoformat()
    pm_payload = {
        **(pm or {}),
        "_worker_generated_at": generated_at,
        "_market_price": t_state.get("price"),
    }
    payload = {
        "pm": pm_payload,
        "dossier": dossier or {},
        "meta": meta,
        "_worker_generated_at": generated_at,
        "_market_price": t_state.get("price"),
    }
    backend.upsert_json_table("research_reports", "ticker", ticker, payload, source=(dossier or {}).get("_source") or "claude")
    return {
        "ticker": ticker,
        "report_source": (dossier or {}).get("_source"),
        "updated_at": payload.get("_worker_generated_at"),
    }


def refresh_watchlist_market_scan(payload: dict | None = None) -> dict:
    payload = payload or {}
    tickers = payload.get("tickers") or []
    clean = [str(t or "").upper().strip() for t in tickers]
    clean = [t for t in dict.fromkeys(clean) if t]
    if not clean:
        return {"updated": 0, "errors": {"watchlist": "No tickers supplied"}}
    backend.sync_watchlist_assets(clean)
    updated = []
    errors = {}
    bench = _flatten_yfinance(_download_benchmark(), "SPY")
    if bench is None or bench.empty:
        raise RuntimeError("No benchmark history returned for SPY")
    max_workers = min(8, max(1, len(clean)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(refresh_market_snapshot, ticker, bench): ticker for ticker in clean}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                updated.append(future.result())
            except Exception as exc:
                errors[ticker] = str(exc)[:240]
    return {"updated": len(updated), "errors": errors, "tickers": [r["ticker"] for r in updated]}


def queue_stale_watchlist_market_scan(max_age_minutes: int = 10, limit: int = 100) -> dict:
    """Queue a watchlist scan when scheduled worker runs find stale rows."""
    tickers = backend.stale_watchlist_market_tickers(
        max_age_minutes=max_age_minutes,
        limit=limit,
    )
    if not tickers:
        return {"queued": False, "tickers": [], "reason": "market snapshots fresh"}
    job_id = backend.enqueue_job(
        "watchlist_market_scan",
        payload={
            "tickers": tickers,
            "source": "scheduled_maintenance",
            "max_age_minutes": max_age_minutes,
        },
        priority=30,
        requested_by="worker-maintenance",
    )
    return {"queued": True, "job_id": job_id, "tickers": tickers}


def score_due_rule_outcomes(max_entries: int = 12) -> dict:
    """Refresh due outcome paths once daily and persist the current review gate."""
    all_entries = backend.read_decision_logs()
    cohorts = engine_evaluation.independent_cohorts(all_entries, spacing_days=7)
    today = date.today()
    due = []
    for entry in cohorts:
        try:
            logged = datetime.fromisoformat(str(entry.get("ts")).replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            continue
        if (today - logged).days < OUTCOME_MIN_AGE_DAYS:
            continue
        outcome = entry.get("outcome") or {}
        if outcome.get("evaluation_complete") and outcome.get("score_version") == OUTCOME_SCORE_VERSION:
            continue
        try:
            scored_today = datetime.fromisoformat(str(outcome.get("ts")).replace("Z", "+00:00")).date() == today
        except (TypeError, ValueError):
            scored_today = False
        if not scored_today:
            due.append(entry)
    total_due = len(due)
    due = due[:max(1, int(max_entries))]

    benchmark = None
    if due:
        benchmark = _flatten_yfinance(_download_benchmark(), "SPY")
    updated = 0
    errors = {}
    for entry in due:
        ticker = str(entry.get("ticker") or "").upper().strip()
        if not ticker:
            continue
        try:
            hist = _flatten_yfinance(_download_history(ticker), ticker)
            if hist is None or hist.empty:
                raise RuntimeError("No market history returned")
            scored = engine_evaluation.score_forward_outcome(
                entry,
                hist,
                benchmark_history=(None if ticker.endswith("-USD") else benchmark),
                as_of=today,
            )
            if not scored:
                continue
            primary = (scored.get("horizons") or {}).get("14") or {}
            if scored.get("rule_family") == "wait":
                note = f"Patience outcome: {str(scored.get('patience_status') or 'waiting').replace('_', ' ')}."
            elif primary:
                note = f"14-session outcome: {primary.get('return_pct', 0):+.1f}%."
            else:
                note = "Evaluation started; the 14-session directional outcome is still pending."
            entry["outcome"] = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "result": "auto_scored",
                "right_sources": ["rules"] if scored.get("credited") is True else [],
                "result_pct": scored.get("forward_return_pct"),
                "note": note,
                "auto_scored": True,
                "score_version": OUTCOME_SCORE_VERSION,
                **scored,
            }
            backend.upsert_decision_log(entry)
            updated += 1
        except Exception as exc:
            errors[ticker] = str(exc)[:180]

    refreshed = backend.read_decision_logs()
    refreshed_cohorts = engine_evaluation.independent_cohorts(refreshed, spacing_days=7)
    directional = [
        entry for entry in refreshed_cohorts
        if engine_evaluation.decision_family(entry.get("rule_action")) in {"long", "avoid"}
    ]
    flags = engine_evaluation.logic_review_flags(directional)
    alerting = [row for row in flags if row.get("status") in {"watch", "review_logic"}]
    status = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "score_version": OUTCOME_SCORE_VERSION,
        "scored_now": updated,
        "remaining_due": max(0, total_due - updated),
        "flags": flags,
        "alerting": alerting,
    }
    backend.write_engine_review_status(status)
    return {"scored": updated, "errors": errors, "alerts": len(alerting), "remaining_due": status["remaining_due"]}


def process_job(job: dict) -> dict:
    job_type = job.get("job_type")
    ticker = job.get("ticker")
    payload = job.get("payload") or {}
    if job_type in LEGACY_IGNORED_JOB_TYPES:
        return {
            "retired": True,
            "message": f"Legacy {job_type} job ignored. PM memos refresh inline from the Analyze page.",
        }
    if job_type == "market_snapshot":
        return refresh_market_snapshot(ticker)
    if job_type == "watchlist_market_scan":
        return refresh_watchlist_market_scan(payload)
    if job_type == "full_report":
        return refresh_full_report(ticker)
    if job_type in {"market_regime_daily", "repair_missing_data"}:
        # Queue contract exists now; these processors can be added without
        # changing Streamlit or the database schema.
        return {"queued_contract": job_type, "message": "processor pending"}
    raise ValueError(f"Unsupported job type: {job_type}")


def _parse_job_types(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    job_types = [piece.strip() for piece in raw.split(",") if piece.strip()]
    legacy = [job_type for job_type in job_types if job_type in LEGACY_IGNORED_JOB_TYPES]
    if legacy:
        print(f"::warning::Ignoring legacy job type(s): {', '.join(legacy)}")
        job_types = [job_type for job_type in job_types if job_type not in LEGACY_IGNORED_JOB_TYPES]
    unknown = [job_type for job_type in job_types if job_type not in backend.JOB_TYPES]
    if unknown:
        raise ValueError(f"Unknown job type(s): {', '.join(unknown)}")
    return job_types or None


def run_once(worker_name: str = "worker", job_types: list[str] | None = None) -> tuple[bool, bool]:
    try:
        job = backend.claim_next_job(worker_name=worker_name, job_types=job_types)
    except Exception as exc:
        _gha_warning(f"Worker could not claim a queued job: {exc}")
        return False, True
    if not job:
        return False, True
    try:
        result = process_job(job)
        backend.complete_job(job["id"], result)
        print(f"completed {job['job_type']} {job.get('ticker') or ''} {job['id']}")
        return True, True
    except Exception as exc:
        try:
            backend.fail_job(job["id"], str(exc))
        except Exception as fail_exc:
            _gha_warning(f"Could not mark failed job {job.get('id')}: {fail_exc}")
        _gha_warning(f"Queued job failed but worker will continue: {job['job_type']} {job.get('ticker') or ''} {job['id']}: {exc}")
        return True, False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Process one queued job and exit.")
    parser.add_argument("--drain", action="store_true", help="Process a batch of queued jobs and exit.")
    parser.add_argument("--loop", action="store_true", help="Continuously process jobs.")
    parser.add_argument("--maintenance", action="store_true", help="Queue stale recurring work before processing jobs.")
    parser.add_argument("--max-jobs", type=int, default=25, help="Maximum jobs to process for --drain.")
    parser.add_argument("--max-runtime-seconds", type=int, default=0, help="Stop --drain before this many seconds elapse.")
    parser.add_argument("--job-types", default="", help="Comma-separated job types this worker may claim.")
    parser.add_argument("--market-max-age-minutes", type=int, default=10, help="Max watchlist market row age before scheduled refresh.")
    parser.add_argument("--sleep", type=float, default=10.0, help="Seconds to sleep when no job is queued.")
    parser.add_argument("--worker-name", default=os.environ.get("WORKER_NAME", "desk-worker"))
    args = parser.parse_args()
    allowed_job_types = _parse_job_types(args.job_types)
    if args.maintenance and args.drain and not allowed_job_types:
        allowed_job_types = SCHEDULED_SAFE_JOB_TYPES
    if args.maintenance and args.drain and not args.max_runtime_seconds:
        args.max_runtime_seconds = SCHEDULED_SAFE_RUNTIME_SECONDS

    if not backend.has_database():
        print("::warning::DATABASE_URL is not configured for this worker environment. "
              "Add the GitHub Actions DATABASE_URL secret to enable background refresh jobs.")
        return

    try:
        backend.ensure_backend_schema()
    except Exception as exc:
        _gha_warning(
            "Worker could not connect to the database or prepare backend tables. "
            f"Queued refreshes will try again on the next run. Detail: {exc}"
        )
        return
    try:
        recovered = backend.recover_stale_running_jobs(max_age_minutes=30, limit=100)
        if recovered:
            print(f"recovered {recovered} stale running job(s)")
    except Exception as exc:
        print(f"stale job recovery skipped: {exc}")
    try:
        retired_pm_jobs = backend.retire_job_type("pm_memo", statuses=("queued", "failed"), limit=500)
        if retired_pm_jobs:
            print(f"retired {retired_pm_jobs} obsolete PM memo job(s)")
    except Exception as exc:
        print(f"obsolete PM memo job retirement skipped: {exc}")
    if args.maintenance:
        try:
            digest_queue = queue_daily_user_digests()
            if digest_queue.get("enabled"):
                print(
                    "notification digest queue: "
                    f"users={digest_queue['users']} queued={digest_queue['queued']} empty={digest_queue['empty']}"
                )
            else:
                print("notification digest generation disabled")
        except Exception as exc:
            _gha_warning(f"Notification digest generation skipped: {exc}")
        try:
            delivery = drain_notification_outbox(limit=10)
            if delivery.get("enabled"):
                print(
                    "notification delivery: "
                    f"claimed={delivery['claimed']} sent={delivery['sent']} failed={delivery['failed']}"
                )
            else:
                print("notification delivery disabled")
        except Exception as exc:
            _gha_warning(f"Notification outbox drain skipped: {exc}")
        if _api_key():
            try:
                retried_reports = backend.retry_failed_jobs(
                    job_type="full_report",
                    error_contains="ANTHROPIC_API_KEY is not configured",
                    limit=20,
                )
                if retried_reports:
                    print(f"requeued {retried_reports} full report key-missing job(s)")
            except Exception as exc:
                print(f"key-missing job retry skipped: {exc}")
        try:
            queued = queue_stale_watchlist_market_scan(
                max_age_minutes=args.market_max_age_minutes,
                limit=100,
            )
            if queued.get("queued"):
                print(f"queued stale watchlist market scan for {len(queued.get('tickers') or [])} ticker(s)")
            else:
                print("scheduled maintenance: market snapshots fresh")
        except Exception as exc:
            print(f"scheduled maintenance skipped: {exc}")
        try:
            outcome_result = score_due_rule_outcomes(max_entries=12)
            print(
                "scheduled outcome scoring: "
                f"{outcome_result.get('scored', 0)} updated, "
                f"{outcome_result.get('alerts', 0)} review alert(s)"
            )
        except Exception as exc:
            _gha_warning(f"Scheduled outcome scoring skipped: {exc}")
    if args.drain:
        processed = 0
        failed = 0
        limit = max(1, args.max_jobs)
        started_at = time.monotonic()
        while processed < limit:
            if args.max_runtime_seconds and time.monotonic() - started_at >= args.max_runtime_seconds:
                print(f"drain stopped at {processed} job(s): runtime budget reached")
                break
            did_work, ok = run_once(worker_name=args.worker_name, job_types=allowed_job_types)
            if not did_work:
                break
            if not ok:
                failed += 1
            processed += 1
        print(f"drained {processed} job(s), {failed} failed job(s)")
        return
    if args.once or not args.loop:
        run_once(worker_name=args.worker_name, job_types=allowed_job_types)
        return
    while True:
        did_work, _ok = run_once(worker_name=args.worker_name, job_types=allowed_job_types)
        if not did_work:
            time.sleep(args.sleep)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _gha_warning(f"Worker run degraded instead of failing the workflow: {exc}")
        print(traceback.format_exc())
