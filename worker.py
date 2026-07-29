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
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import yfinance as yf

import backend_layer as backend
from pm_view import get_decision_dossier, get_pm_view
import tactical


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


def _api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    try:
        import streamlit as st  # type: ignore

        return str(st.secrets.get("ANTHROPIC_API_KEY", "")).strip()
    except Exception:
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
    rule_payload = dict(t_state)
    trigger = rule_payload.get("trigger") or {}
    if isinstance(trigger, dict):
        rule_payload["trigger_summary"] = trigger.get("summary")
    backend.upsert_json_table("market_snapshots", "ticker", ticker, market_payload, source="yahoo")
    backend.upsert_json_table("rule_outputs", "ticker", ticker, rule_payload, source="rules")
    return {
        "ticker": ticker,
        "price": market_payload.get("price"),
        "action": rule_payload.get("action"),
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
    backend.upsert_json_table("pm_memos", "ticker", ticker, pm_payload, source=pm_payload.get("_source") or "claude")
    return {
        "ticker": ticker,
        "source": pm_payload.get("_source"),
        "quality": (pm_payload.get("quality") or {}).get("tier") if isinstance(pm_payload.get("quality"), dict) else None,
        "updated_at": pm_payload.get("_worker_generated_at"),
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
    payload = {
        "pm": pm or {},
        "dossier": dossier or {},
        "meta": meta,
        "_worker_generated_at": datetime.now(timezone.utc).isoformat(),
        "_market_price": t_state.get("price"),
    }
    backend.upsert_json_table("pm_memos", "ticker", ticker, pm or {}, source=(pm or {}).get("_source") or "claude")
    backend.upsert_json_table("research_reports", "ticker", ticker, payload, source=(dossier or {}).get("_source") or "claude")
    return {
        "ticker": ticker,
        "pm_source": (pm or {}).get("_source"),
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


def run_once(worker_name: str = "worker") -> bool:
    job = backend.claim_next_job(worker_name=worker_name)
    if not job:
        return False
    try:
        result = process_job(job)
        backend.complete_job(job["id"], result)
        print(f"completed {job['job_type']} {job.get('ticker') or ''} {job['id']}")
        return True
    except Exception as exc:
        backend.fail_job(job["id"], str(exc))
        print(f"failed {job['job_type']} {job.get('ticker') or ''} {job['id']}: {exc}")
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Process one queued job and exit.")
    parser.add_argument("--drain", action="store_true", help="Process a batch of queued jobs and exit.")
    parser.add_argument("--loop", action="store_true", help="Continuously process jobs.")
    parser.add_argument("--max-jobs", type=int, default=25, help="Maximum jobs to process for --drain.")
    parser.add_argument("--sleep", type=float, default=10.0, help="Seconds to sleep when no job is queued.")
    parser.add_argument("--worker-name", default=os.environ.get("WORKER_NAME", "desk-worker"))
    args = parser.parse_args()

    backend.ensure_backend_schema()
    try:
        recovered = backend.recover_stale_running_jobs(max_age_minutes=30, limit=100)
        if recovered:
            print(f"recovered {recovered} stale running job(s)")
    except Exception as exc:
        print(f"stale job recovery skipped: {exc}")
    if args.drain:
        processed = 0
        limit = max(1, args.max_jobs)
        while processed < limit:
            did_work = run_once(worker_name=args.worker_name)
            if not did_work:
                break
            processed += 1
        print(f"drained {processed} job(s)")
        return
    if args.once or not args.loop:
        run_once(worker_name=args.worker_name)
        return
    while True:
        did_work = run_once(worker_name=args.worker_name)
        if not did_work:
            time.sleep(args.sleep)


if __name__ == "__main__":
    main()
