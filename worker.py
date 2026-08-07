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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import yfinance as yf

import backend_layer as backend
from pm_view import get_decision_dossier, get_pm_view
import tactical


SCHEDULED_SAFE_JOB_TYPES = ["market_snapshot", "watchlist_market_scan", "pm_memo"]
SCHEDULED_SAFE_RUNTIME_SECONDS = 240


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


def refresh_pm_memo(ticker: str) -> dict:
    ticker = str(ticker or "").upper().strip()
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured for the worker")
    t_state, meta = _fresh_tactical_state(ticker)
    company_name = meta.get("company_name") or ticker
    pm = get_pm_view(ticker, t_state, api_key=api_key, company_name=company_name)
    pm_payload = {
        **(pm or {}),
        "_worker_generated_at": datetime.now(timezone.utc).isoformat(),
        "_market_price": t_state.get("price"),
    }
    stored_pm = backend.upsert_pm_memo(ticker, pm_payload, source=pm_payload.get("_source") or "claude")
    return {
        "ticker": ticker,
        "source": pm_payload.get("_source"),
        "quality": (pm_payload.get("quality") or {}).get("tier") if isinstance(pm_payload.get("quality"), dict) else None,
        "updated_at": pm_payload.get("_worker_generated_at"),
        "revision_id": stored_pm.get("_revision_id"),
    }


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


def process_job(job: dict) -> dict:
    job_type = job.get("job_type")
    ticker = job.get("ticker")
    payload = job.get("payload") or {}
    if job_type == "market_snapshot":
        return refresh_market_snapshot(ticker)
    if job_type == "watchlist_market_scan":
        return refresh_watchlist_market_scan(payload)
    if job_type == "pm_memo":
        return refresh_pm_memo(ticker)
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
    unknown = [job_type for job_type in job_types if job_type not in backend.JOB_TYPES]
    if unknown:
        raise ValueError(f"Unknown job type(s): {', '.join(unknown)}")
    return job_types or None


def run_once(worker_name: str = "worker", job_types: list[str] | None = None) -> tuple[bool, bool]:
    try:
        job = backend.claim_next_job(worker_name=worker_name, job_types=job_types)
    except Exception as exc:
        print(f"::warning::worker could not claim a queued job: {exc}")
        return False, False
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
            print(f"::warning::could not mark failed job {job.get('id')}: {fail_exc}")
        print(f"failed {job['job_type']} {job.get('ticker') or ''} {job['id']}: {exc}")
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

    backend.ensure_backend_schema()
    try:
        recovered = backend.recover_stale_running_jobs(max_age_minutes=30, limit=100)
        if recovered:
            print(f"recovered {recovered} stale running job(s)")
    except Exception as exc:
        print(f"stale job recovery skipped: {exc}")
    if args.maintenance:
        if _api_key():
            try:
                retried_pm = backend.retry_failed_jobs(
                    job_type="pm_memo",
                    error_contains="ANTHROPIC_API_KEY is not configured",
                    limit=50,
                )
                retried_reports = backend.retry_failed_jobs(
                    job_type="full_report",
                    error_contains="ANTHROPIC_API_KEY is not configured",
                    limit=20,
                )
                if retried_pm or retried_reports:
                    print(f"requeued {retried_pm} PM memo and {retried_reports} full report key-missing job(s)")
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
    main()
