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
import json
import os
import ssl
import traceback
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import yfinance as yf
import certifi

import backend_layer as backend
import attention_engine
import decision_contract
import engine_candidates
import data_trust
import engine_evaluation
import market_freshness
import crypto_regime
import email_delivery
import notification_engine
import unsubscribe
from pm_view import _messages_create, get_decision_dossier, get_pm_view
import tactical


SCHEDULED_SAFE_JOB_TYPES = ["market_snapshot", "watchlist_market_scan", "market_regime_daily", "repair_missing_data", "full_report"]
SCHEDULED_SAFE_RUNTIME_SECONDS = 240
LEGACY_IGNORED_JOB_TYPES = {"pm_memo"}
OUTCOME_SCORE_VERSION = engine_evaluation.EVALUATION_VERSION
OUTCOME_MIN_AGE_DAYS = 7
RULE_ENGINE_VERSION = "rules-2026.08-d"
CRYPTO_REGIME_MODEL_VERSION = "crypto-cycle-2026.09.08-a"
MARKET_REGIME_SCHEMA_VERSION = 4


REGIME_NEWS_QUERIES = {
    "economy": (
        "Federal Reserve OR inflation OR CPI OR PCE OR payrolls OR unemployment OR GDP "
        "OR Treasury yields OR economic growth when:3d"
    ),
    "markets": (
        "S&P 500 OR Nasdaq OR stocks OR credit spreads OR bond market OR VIX "
        "OR earnings outlook OR oil price when:3d"
    ),
    "crypto_policy": (
        'crypto regulation OR digital asset policy OR "CLARITY Act" OR "Digital Asset Market Clarity Act" '
        "OR stablecoin legislation OR SEC crypto OR CFTC crypto when:7d"
    ),
    "crypto_markets": (
        "Bitcoin OR Ethereum OR crypto ETF OR stablecoin OR digital asset market "
        "OR crypto liquidity OR institutional crypto when:3d"
    ),
}

_NEWS_IMPACT_TERMS = {
    "federal reserve": 5, "rate cut": 5, "rate hike": 5, "inflation": 4,
    "payroll": 4, "unemployment": 4, "gdp": 4, "treasury": 3,
    "credit spread": 4, "earnings": 2, "oil": 2, "tariff": 3,
    "clarity act": 6, "digital asset market clarity": 6, "stablecoin": 4,
    "sec": 3, "cftc": 3, "bitcoin": 3, "ethereum": 3, "crypto etf": 4,
}


def _fetch_regime_news(*, per_topic: int = 5) -> tuple[list[dict], dict]:
    """Collect and rank fresh market-moving news across the regime's full remit."""
    def _fetch_topic(topic: str, query: str) -> tuple[str, list[dict], str | None]:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
            "q": query, "hl": "en-US", "gl": "US", "ceid": "US:en",
        })
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "TradingDesk/1.0"})
            tls_context = ssl.create_default_context(cafile=certifi.where())
            with urllib.request.urlopen(request, timeout=8, context=tls_context) as response:
                root = ET.fromstring(response.read())
            stories = []
            for item in root.findall("./channel/item")[: per_topic * 3]:
                title = " ".join((item.findtext("title") or "").split())
                link = (item.findtext("link") or "").strip()
                published_raw = (item.findtext("pubDate") or "").strip()
                source_node = item.find("source")
                source = " ".join(((source_node.text if source_node is not None else "") or "").split())
                if not title or not link:
                    continue
                try:
                    published_at = parsedate_to_datetime(published_raw).astimezone(timezone.utc).isoformat(timespec="seconds")
                except Exception:
                    published_at = None
                lowered = title.lower()
                impact_score = sum(weight for term, weight in _NEWS_IMPACT_TERMS.items() if term in lowered)
                stories.append({
                    "title": title, "url": link, "source": source or "Google News",
                    "published_at": published_at, "category": topic,
                    "impact_score": impact_score,
                })
            stories.sort(key=lambda row: (row["impact_score"], row.get("published_at") or ""), reverse=True)
            return topic, stories[:per_topic], None
        except Exception as exc:
            return topic, [], str(exc)[:180]

    collected, errors = [], {}
    with ThreadPoolExecutor(max_workers=len(REGIME_NEWS_QUERIES)) as pool:
        futures = [pool.submit(_fetch_topic, topic, query) for topic, query in REGIME_NEWS_QUERIES.items()]
        for future in as_completed(futures):
            topic, stories, error = future.result()
            collected.extend(stories)
            if error:
                errors[topic] = error

    deduped = {}
    for story in collected:
        key = " ".join(story["title"].lower().split())
        existing = deduped.get(key)
        if existing is None or story["impact_score"] > existing["impact_score"]:
            deduped[key] = story
    ranked = sorted(
        deduped.values(),
        key=lambda row: (row["impact_score"], row.get("published_at") or ""),
        reverse=True,
    )
    # Guarantee breadth before filling remaining slots by impact. Otherwise a
    # busy macro day can silently crowd all crypto policy (or vice versa) out
    # of the client payload even though collection succeeded.
    selected, selected_urls = [], set()
    for topic in REGIME_NEWS_QUERIES:
        for story in (row for row in ranked if row["category"] == topic):
            if len([row for row in selected if row["category"] == topic]) >= 3:
                break
            selected.append(story)
            selected_urls.add(story["url"])
    for story in ranked:
        if len(selected) >= 16:
            break
        if story["url"] not in selected_urls:
            selected.append(story)
            selected_urls.add(story["url"])
    selected.sort(key=lambda row: (row["impact_score"], row.get("published_at") or ""), reverse=True)
    return selected, errors


def _series_snapshot(frame, ticker: str) -> dict:
    frame = _flatten_yfinance(frame, ticker)
    if frame is None or frame.empty or "Close" not in frame:
        raise RuntimeError(f"No market history returned for {ticker}")
    close = frame["Close"].dropna()
    if len(close) < 50:
        raise RuntimeError(f"Insufficient market history returned for {ticker}")
    last = float(close.iloc[-1])
    previous = float(close.iloc[-2])
    ma20 = float(close.tail(20).mean())
    ma50 = float(close.tail(50).mean())
    ma200 = float(close.tail(200).mean()) if len(close) >= 200 else None
    peak5 = float(close.tail(5).max()) if len(close) >= 5 else None
    return {
        "last": round(last, 4),
        "change_pct": round((last / previous - 1) * 100, 4) if previous else None,
        "return_5d_pct": round((last / float(close.iloc[-6]) - 1) * 100, 4) if len(close) >= 6 else None,
        "return_20d_pct": round((last / float(close.iloc[-21]) - 1) * 100, 4) if len(close) >= 21 else None,
        "peak_5d": round(peak5, 4) if peak5 is not None else None,
        "drop_from_5d_peak_pct": round((peak5 / last - 1) * 100, 4) if peak5 and last else None,
        "vs_20d_pct": round((last / ma20 - 1) * 100, 4),
        "vs_50d_pct": round((last / ma50 - 1) * 100, 4),
        "vs_200d_pct": round((last / ma200 - 1) * 100, 4) if ma200 else None,
    }


def _fred_values(series_id: str, limit: int = 24) -> list[float]:
    """Read recent FRED values using the same public series as Streamlit."""
    try:
        import pandas as pd
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={urllib.parse.quote(series_id)}"
        frame = pd.read_csv(url).tail(limit)
        values = pd.to_numeric(frame[series_id], errors="coerce").dropna()
        return [float(value) for value in values]
    except Exception:
        return []


def _fear_greed_value() -> tuple[int | None, str | None]:
    try:
        request = urllib.request.Request("https://api.alternative.me/fng/", headers={"User-Agent": "TradingDesk/1.0"})
        with urllib.request.urlopen(request, timeout=5) as response:
            item = (json.loads(response.read().decode("utf-8")).get("data") or [{}])[0]
        return int(item.get("value")), str(item.get("value_classification") or "") or None
    except Exception:
        return None, None


def _macro_regime_inputs() -> dict:
    ism = _fred_values("NAPMPMI")
    unemp = _fred_values("UNRATE")
    hy = _fred_values("BAMLH0A0HYM2")
    curve = _fred_values("T10Y2Y")
    fed, rrp, tga = _fred_values("WALCL"), _fred_values("WLRRAL"), _fred_values("WTREGEN")
    fear_greed, fear_greed_label = _fear_greed_value()
    return {
        "ism": ism[-1] if ism else None,
        "unemployment": unemp[-1] if unemp else None,
        "unemployment_previous": unemp[-2] if len(unemp) >= 2 else None,
        "hy_oas_bps": hy[-1] * 100 if hy else None,
        "yield_curve_bps": curve[-1] * 100 if curve else None,
        "fed_now": fed[-1] if fed else None, "fed_previous": fed[-2] if len(fed) >= 2 else None,
        "rrp_now": rrp[-1] if rrp else None, "rrp_previous": rrp[-2] if len(rrp) >= 2 else None,
        "tga_now": tga[-1] if tga else None, "tga_previous": tga[-2] if len(tga) >= 2 else None,
        "fear_greed": fear_greed, "fear_greed_label": fear_greed_label,
    }


def _streamlit_regime_score(*, assets: dict, macro: dict) -> dict:
    """Canonical 2–12 week score ported from Streamlit's Market Regime engine."""
    spy, vix = assets.get("SPY", {}), assets.get("^VIX", {})
    spx20, spx50 = spy.get("vs_20d_pct"), spy.get("vs_50d_pct")
    ret5, ret20 = spy.get("return_5d_pct"), spy.get("return_20d_pct")
    vix_level, vix_peak5, vix_drop5 = vix.get("last"), vix.get("peak_5d"), vix.get("drop_from_5d_peak_pct")
    hy_bps, fg = macro.get("hy_oas_bps"), macro.get("fear_greed")
    ism = macro.get("ism")
    unemployment, unemployment_previous = macro.get("unemployment"), macro.get("unemployment_previous")
    t1 = "WARNING" if ism is not None and ism < 50 else "CLEAR"
    rising = unemployment is not None and unemployment_previous is not None and unemployment > unemployment_previous
    falling = unemployment is not None and unemployment_previous is not None and unemployment < unemployment_previous
    if unemployment is not None and unemployment >= 4.2 and rising:
        t2 = "FIRING"
    elif unemployment is not None and unemployment >= 4.2 and falling:
        t2 = "RETREATING"
    elif unemployment is not None and unemployment >= 4.0 and rising:
        t2 = "APPROACHING"
    else:
        t2 = "CLEAR"
    if hy_bps is not None and hy_bps >= 600:
        t3 = "FIRING"
    elif hy_bps is not None and hy_bps >= 450:
        t3 = "ELEVATED"
    else:
        t3 = "CLEAR"
    curve = macro.get("yield_curve_bps")
    curve_state = "INVERTED" if curve is not None and curve < 0 else "FLAT" if curve is not None and curve < 50 else "STEEPENING"

    score, drivers, risks = 0, [], []
    if spx20 is not None and spx50 is not None:
        if spx20 > 0 and spx50 > 0:
            score += 2; drivers.append("SPX above 20d/50d")
        elif spx50 > 0:
            score += 1; drivers.append("SPX above 50d")
        elif spx20 < 0 and spx50 < 0:
            score -= 2; risks.append("SPX below 20d/50d")
        else:
            score -= 1; risks.append("SPX trend mixed")
    if hy_bps is not None:
        if hy_bps < 350:
            score += 1; drivers.append("credit calm")
        elif hy_bps >= 475:
            score -= 2; risks.append("credit stress rising")
        elif hy_bps >= 400:
            score -= 1; risks.append("credit spreads elevated")
    if vix_peak5 is not None and vix_drop5 is not None and vix_peak5 >= 25 and vix_drop5 >= 20:
        score += 1; drivers.append("volatility compressed after stress")
    elif vix_level is not None and vix_level > 35:
        score -= 2; risks.append("VIX panic active")
    elif vix_level is not None and vix_level > 25:
        score -= 1; risks.append("VIX elevated")
    if fg is not None and fg <= 25 and ret5 is not None and ret5 > 0:
        score += 1; drivers.append("fear despite price recovery")
    elif fg is not None and fg >= 75 and ret20 is not None and ret20 > 5:
        score -= 1; risks.append("sentiment stretched")
    if t1 == "WARNING":
        score -= 3; risks.append("T1 macro trigger fired")
    elif curve_state == "INVERTED" and t1 == "CLEAR":
        score -= 1; risks.append("curve inverted")

    window = "Favorable" if score >= 3 else "Mixed" if score >= 0 else "Unfavorable"
    cluster_n = int(vix_level is not None and vix_level > 35) + int(fg is not None and fg < 25)
    if cluster_n >= 2:
        timing = "Add weakness"
    elif fg is not None and fg >= 75:
        timing = "Wait"
    elif spx20 is not None and spx20 > 3:
        timing = "Wait"
    elif spx20 is not None and spx20 < 0 and spx50 is not None and spx50 > 0:
        timing = "Pullback watch"
    elif (vix_peak5 is not None and vix_drop5 is not None and vix_peak5 >= 25 and vix_drop5 >= 20) or (fg is not None and fg <= 35 and ret5 is not None and ret5 > 0):
        timing = "Acceptable"
    else:
        timing = "Neutral"
    action = "Avoid" if window == "Unfavorable" else "Hold Off" if window == "Mixed" else "Wait" if timing in {"Wait", "Pullback watch"} else "Enter"
    return {"score": score, "window": window, "timing": timing, "action": action, "drivers": drivers, "risks": risks,
            "signals": {"t1_ism": t1, "t2_unemployment": t2, "t3_hy_oas": t3, "yield_curve": curve_state}}


def _crypto_regime_snapshot(frame) -> dict:
    """Return distinct canonical cycle, trend, and timing reads for clients."""
    frame = _flatten_yfinance(frame, "BTC-USD")
    if frame is None or frame.empty or "Close" not in frame:
        return {}
    close = frame["Close"].dropna()
    if len(close) < 200:
        return {}
    price = float(close.iloc[-1])
    ma20 = float(close.tail(20).mean())
    ma50 = float(close.tail(50).mean())
    ma200 = float(close.tail(200).mean())
    peak = float(close.max())
    vs20 = (price / ma20 - 1) * 100 if ma20 else None
    vs50 = (price / ma50 - 1) * 100 if ma50 else None
    vs200 = (price / ma200 - 1) * 100 if ma200 else None
    drawdown = (price / peak - 1) * 100 if peak else None
    return90 = (price / float(close.iloc[-90]) - 1) * 100 if len(close) >= 90 else None
    phase, cycle_label, cycle_detail, phase_number = crypto_regime.classify_cycle(
        btc_vs_200=vs200,
        btc_vs_20=vs20,
        drawdown_cycle=drawdown,
        return_90=return90,
        fear_greed=None,
    )
    if vs50 is not None and vs20 is not None and vs50 > 0 and vs20 > 0:
        trend_label, trend_detail = "Uptrend", "Bitcoin is above its 20-day and 50-day averages."
    elif vs50 is not None and vs50 > 0:
        trend_label, trend_detail = "Uptrend under pressure", "Bitcoin remains above its 50-day average but has lost its 20-day average."
    elif vs20 is not None and vs20 > 0:
        trend_label, trend_detail = "Recovery attempt", "Short-term momentum is improving, but Bitcoin remains below its 50-day average."
    else:
        trend_label, trend_detail = "Downtrend", "Bitcoin is below its 20-day and 50-day averages."
    if (vs20 or 0) >= 8 or ((assets_return := return90) is not None and assets_return >= 25):
        timing_label, timing_detail = "Extended — wait", "Momentum is stretched; wait for consolidation or a pullback instead of chasing."
    elif vs20 is not None and vs20 > 0:
        timing_label, timing_detail = "Constructive", "Short-term momentum is positive, but entry quality still depends on price and risk limits."
    else:
        timing_label, timing_detail = "Not ready", "Wait for Bitcoin to reclaim its 20-day average before treating timing as constructive."
    return {
        "cycle": {"phase": phase, "phase_number": phase_number, "label": cycle_label, "detail": cycle_detail},
        "medium_term_trend": {"label": trend_label, "detail": trend_detail},
        "tactical_timing": {"label": timing_label, "detail": timing_detail},
        "metrics": {
            "price": round(price, 4), "vs_20d_pct": round(vs20, 4),
            "vs_50d_pct": round(vs50, 4), "vs_200d_pct": round(vs200, 4),
            "drawdown_from_2y_high_pct": round(drawdown, 4),
            "return_90d_pct": round(return90, 4) if return90 is not None else None,
        },
        "model_version": CRYPTO_REGIME_MODEL_VERSION,
    }


def _level_from_distance(asset: dict, key: str):
    """Recover a moving-average level from price and its percentage distance."""
    last, distance = asset.get("last"), asset.get(key)
    if last is None or distance is None or float(distance) <= -99.9:
        return None
    return float(last) / (1 + float(distance) / 100)


def _regime_decision_context(*, stance: str, score: int, assets: dict, errors: dict, previous: dict | None = None,
                             canonical: dict | None = None, macro: dict | None = None) -> dict:
    """Build the complete, client-safe Market Regime narrative from saved inputs."""
    previous = previous or {}
    canonical, macro = canonical or {}, macro or {}
    spy, qqq = assets.get("SPY", {}), assets.get("QQQ", {})
    rsp, hyg, vix = assets.get("RSP", {}), assets.get("HYG", {}), assets.get("^VIX", {})
    label = canonical.get("window") or ("Favorable" if score >= 3 else "Unfavorable" if score < 0 else "Mixed")
    action_label = canonical.get("action") or ("Enter" if label == "Favorable" else "Avoid" if label == "Unfavorable" else "Hold Off")
    action = {"Enter": "enter", "Wait": "wait", "Hold Off": "hold_off", "Avoid": "avoid"}.get(action_label, "hold_off")
    favorable = label == "Favorable"
    defensive = label == "Unfavorable"
    above_20 = (spy.get("vs_20d_pct") or 0) > 0
    above_50 = (spy.get("vs_50d_pct") or 0) > 0
    breadth_gap = (rsp.get("return_20d_pct") or 0) - (spy.get("return_20d_pct") or 0)
    credit_20 = hyg.get("return_20d_pct")
    vix_level = vix.get("last")
    timing = canonical.get("timing") or "Neutral"

    prior_score = previous.get("score")
    prior_stance = previous.get("portfolio_stance")
    if prior_score is None:
        change_label = "First comparable snapshot"
        change_detail = "No prior saved regime snapshot is available for a day-over-day comparison."
    else:
        delta = score - int(prior_score)
        change_label = "Improved" if delta > 0 else "Weakened" if delta < 0 else "Unchanged"
        change_detail = (
            f"The stance moved from {prior_stance or 'the prior reading'} to {stance}; the score changed {delta:+d} to {score}."
            if delta else f"The {stance} stance and score of {score} are unchanged from the prior snapshot."
        )

    trend_text = (
        f"SPY is {abs(spy.get('vs_20d_pct') or 0):.1f}% {'above' if above_20 else 'below'} its 20-day average and "
        f"{abs(spy.get('vs_50d_pct') or 0):.1f}% {'above' if above_50 else 'below'} its 50-day average"
    )
    breadth_text = f"equal-weight breadth is {abs(breadth_gap):.1f} points {'ahead of' if breadth_gap >= 0 else 'behind'} SPY over 20 sessions"
    hy_oas = macro.get("hy_oas_bps")
    credit_text = f"high-yield spreads are {hy_oas:.0f} bps" if hy_oas is not None else ("credit data is unavailable" if credit_20 is None else f"high-yield credit is {credit_20:+.1f}% over 20 sessions")
    volatility_text = "volatility data is unavailable" if vix_level is None else f"VIX is {vix_level:.1f}"
    why_today = (
        f"{trend_text}. {breadth_text.capitalize()}, while {credit_text} and {volatility_text}. "
        f"Together those live inputs make the 2–12 week opportunity window {label.lower()}. "
        f"The practical call is to {'add exposure in stages only where company-level triggers agree' if favorable else 'protect capital and wait for confirmation' if defensive else 'keep exposure selective and wait for cleaner confirmation'}."
    )

    drivers, risks = list(canonical.get("drivers") or []), list(canonical.get("risks") or [])
    if not drivers and not risks:
        (drivers if above_50 else risks).append(f"SPY is {'above' if above_50 else 'below'} its 50-day trend.")

    spy20, spy50 = _level_from_distance(spy, "vs_20d_pct"), _level_from_distance(spy, "vs_50d_pct")
    triggers = []
    if spy20 is not None and spy50 is not None:
        if above_20 and above_50:
            triggers.append(f"SPY closes below ${max(spy20, spy50):,.2f} and then loses ${min(spy20, spy50):,.2f}: broad trend support is failing; stop adding marginal exposure.")
        else:
            triggers.append(f"SPY reclaims ${max(spy20, spy50):,.2f}: trend repair improves the case for selective new exposure.")
    triggers.extend([
        "Equal-weight RSP lags SPY by more than 1 percentage point over 20 sessions: participation is narrowing; favor stronger setups.",
        "High-yield credit falls more than 1% over 20 sessions: credit is no longer confirming risk appetite; reduce marginal cyclicals.",
        "VIX rises through 30: volatility stress is material; tighten risk and avoid chasing entries.",
    ])
    highlights = []
    for symbol, label_name in (("SPY", "S&P 500"), ("QQQ", "Nasdaq 100"), ("RSP", "Equal-weight breadth"), ("HYG", "High-yield credit"), ("^VIX", "Volatility"), ("BTC-USD", "Bitcoin")):
        item = assets.get(symbol) or {}
        if item.get("last") is not None:
            highlights.append({"symbol": symbol, "label": label_name, "value": item["last"], "change_pct": item.get("change_pct"), "return_20d_pct": item.get("return_20d_pct"), "vs_50d_pct": item.get("vs_50d_pct")})
    forward_watch = [
        {"title": "Broad trend", "body": "Watch whether SPY holds its 20-day and 50-day averages; those levels govern near-term execution."},
        {"title": "Breadth", "body": "RSP should keep pace with SPY. Persistent lag would make an index-led rally less dependable."},
        {"title": "Credit", "body": "HYG should remain stable. Weakening credit would challenge an otherwise constructive equity tape."},
        {"title": "Volatility", "body": "A VIX move through 30 would shift the focus from adding exposure to protecting capital."},
    ]
    return {
        "opportunity_action": action, "opportunity_label": label, "entry_timing": timing,
        "change_label": change_label, "change_detail": change_detail, "why_today": why_today,
        "market_highlights": highlights, "drivers": drivers, "risks": risks,
        "watch_triggers": triggers, "forward_watch": forward_watch,
        "data_trust": {"status": "caution" if errors else "trusted", "executable": "SPY" in assets, "issues": [f"{key}: unavailable" for key in errors]},
    }


def _claude_regime_context(*, snapshot: dict, fallback: str) -> tuple[str, str]:
    """Turn the deterministic regime snapshot into natural prose without changing its call."""
    api_key = _api_key()
    if not api_key:
        return fallback, "rules_fallback"
    try:
        from anthropic import Anthropic

        prompt_payload = {
            "opportunity_label": snapshot.get("opportunity_label"),
            "opportunity_action": snapshot.get("opportunity_action"),
            "entry_timing": snapshot.get("entry_timing"),
            "score": snapshot.get("score"),
            "change_label": snapshot.get("change_label"),
            "change_detail": snapshot.get("change_detail"),
            "drivers": snapshot.get("drivers"),
            "risks": snapshot.get("risks"),
            "assets": snapshot.get("assets"),
            "macro": snapshot.get("macro"),
            "news": snapshot.get("news"),
        }
        response = _messages_create(
            Anthropic(api_key=api_key),
            max_tokens=260,
            temperature=0.35,
            system=(
                "You write the daily market context for a serious retail-investor decision workstation. "
                "The deterministic rules engine is authoritative. Explain its output; never change, soften, "
                "upgrade, or contradict the supplied action, stance, score, or timing."
            ),
            messages=[{"role": "user", "content": (
                "Write one natural paragraph of 70-115 words explaining what matters in today's market tape "
                "and what it means for a portfolio. Synthesize the tension among trend, breadth, credit, "
                "volatility, sentiment, and macro conditions instead of reciting every field in order. Lead "
                "with the most important development or conflict. Mention numbers only when they sharpen the "
                "explanation. Use calm, factual plain English, vary sentence structure, and end with the practical "
                "posture. Avoid sensational or loaded adjectives such as dangerous, alarming, severe, or dramatic. "
                "Treat the supplied news as context, not as a replacement for the rules. Mention only stories that "
                "materially affect growth, inflation, rates, liquidity, risk appetite, or crypto conditions. Explain "
                "the market and portfolio implication instead of listing headlines. Do not use a heading, bullets, "
                "markdown, predictions, outside facts, news, or economic events that are not present in the payload. "
                "Do not call this investment advice. Return only the paragraph.\n\n"
                + json.dumps(prompt_payload, separators=(",", ":"), default=str)
            )}],
        )
        text = " ".join(
            str(getattr(block, "text", "") or "").strip()
            for block in getattr(response, "content", [])
        ).strip()
        word_count = len(text.split())
        if not text or word_count < 45 or word_count > 150 or "```" in text:
            raise ValueError("Claude returned invalid market-context prose")
        return text, "claude"
    except Exception:
        return fallback, "rules_fallback"


def refresh_market_regime_daily(payload: dict | None = None) -> dict:
    """Persist a deterministic daily market-regime snapshot for cheap UI/API reads."""
    symbols = ("SPY", "QQQ", "RSP", "HYG", "^VIX", "BTC-USD")
    assets, errors = {}, {}
    frames = {}
    for symbol in symbols:
        try:
            frames[symbol] = _download_history(symbol)
            assets[symbol] = _series_snapshot(frames[symbol], symbol)
        except Exception as exc:
            errors[symbol] = str(exc)[:180]
    if "SPY" not in assets:
        raise RuntimeError(f"Regime refresh requires SPY history: {errors.get('SPY', 'unavailable')}")
    macro = _macro_regime_inputs()
    news, news_errors = _fetch_regime_news()
    canonical = _streamlit_regime_score(assets=assets, macro=macro)
    score = canonical["score"]
    stance = canonical["window"]
    reasons = list(canonical["drivers"] + canonical["risks"])
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    saved = backend.read_json_table("market_regime_daily", limit=3)
    previous = next((row for day, row in saved.items() if day != date.today().isoformat()), {}) if isinstance(saved, dict) else {}
    result = {
        "schema_version": MARKET_REGIME_SCHEMA_VERSION,
        "generated_at": generated_at,
        "data_as_of": generated_at,
        "freshness": "fresh",
        "engine_version": RULE_ENGINE_VERSION,
        "day": date.today().isoformat(),
        "portfolio_stance": stance,
        "score": score,
        "reasons": reasons,
        "assets": assets,
        "errors": errors,
        "source": "worker_market_regime_v1",
        "macro": macro,
        "news": news,
        "news_errors": news_errors,
        "signals": canonical["signals"],
        "crypto_regime": _crypto_regime_snapshot(frames.get("BTC-USD")),
    }
    result.update(_regime_decision_context(stance=stance, score=score, assets=assets, errors=errors, previous=previous, canonical=canonical, macro=macro))
    result["why_today_fallback"] = result["why_today"]
    result["why_today"], result["why_today_source"] = _claude_regime_context(
        snapshot=result,
        fallback=result["why_today_fallback"],
    )
    backend.upsert_json_table("market_regime_daily", "day", result["day"], result, source=result["source"])
    return result


def repair_missing_data(payload: dict | None = None) -> dict:
    """Repair missing/stale durable market and rule rows without touching user data."""
    payload = payload or {}
    explicit = payload.get("tickers") or []
    requested = explicit or backend.enabled_watchlist_tickers(limit=250)
    candidates = [str(t or "").upper().strip() for t in requested if str(t or "").strip()]
    candidates = list(dict.fromkeys(candidates))
    if explicit:
        tickers = candidates
    else:
        markets = backend.read_json_table_many("market_snapshots", candidates)
        rules = backend.read_json_table_many("rule_outputs", candidates)
        tickers = []
        for ticker in candidates:
            market = markets.get(ticker) if isinstance(markets.get(ticker), dict) else {}
            rule = rules.get(ticker) if isinstance(rules.get(ticker), dict) else {}
            price = market.get("price") or market.get("last")
            if price is None or not rule.get("action"):
                tickers.append(ticker)
    repaired, errors = [], {}
    bench = _flatten_yfinance(_download_benchmark(), "SPY") if tickers else None
    for ticker in tickers:
        try:
            repaired.append(refresh_market_snapshot(ticker, bench=bench))
        except Exception as exc:
            errors[ticker] = str(exc)[:180]
    regime = None
    if payload.get("include_regime", True):
        try:
            regime = refresh_market_regime_daily(payload)
        except Exception as exc:
            errors["market_regime_daily"] = str(exc)[:180]
    return {"checked": len(candidates), "missing": len(tickers), "repaired": len(repaired), "errors": errors, "regime_updated": bool(regime)}


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


def _shadow_evaluations(t_state: dict) -> list[dict]:
    """Record candidate behavior without allowing it to replace the live action."""
    return engine_candidates.shadow_evaluations(t_state)


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
    receipt = decision_contract.build_decision_receipt(
        tkr,
        {
            **t_state,
            "trigger_summary": _trigger_summary(t_state),
            "data_trust": data_trust.assess_decision_data(t_state),
        },
        engine_version=RULE_ENGINE_VERSION,
    )
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
        "decision_receipt": receipt,
        "decision_attribution": decision_contract.build_rule_attribution(t_state),
        "decision_inputs": decision_contract.build_input_snapshot(t_state, captured_at=receipt["captured_at"]),
        "decision_invariant_issues": decision_contract.decision_invariant_issues(t_state),
        "decision_context": {
            "market_regime": t_state.get("market_regime"),
            "tape_class": t_state.get("state"),
            "extension_warning": bool(t_state.get("extension_warning")),
            "extension_warning_severity": (t_state.get("extension_warning") or {}).get("severity") if isinstance(t_state.get("extension_warning"), dict) else None,
        },
        "shadow_evaluations": _shadow_evaluations(t_state),
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
    earnings_timestamp = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
    try:
        earnings_date = datetime.fromtimestamp(float(earnings_timestamp), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        earnings_date = None
    earnings_days = None
    if earnings_date:
        try:
            earnings_days = (datetime.fromisoformat(earnings_date).date() - datetime.now(timezone.utc).date()).days
        except (TypeError, ValueError):
            earnings_days = None
    quote_type = str(info.get("quoteType") or "").lower() or None
    return {
        "company_name": info.get("shortName") or info.get("longName") or ticker,
        "long_business_summary": info.get("longBusinessSummary"),
        "quote_type": quote_type,
        "asset_category": "crypto" if quote_type == "cryptocurrency" or ticker.endswith("-USD") else "equity",
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "market_cap": info.get("marketCap"),
        "short_pct_float": info.get("shortPercentOfFloat"),
        "institutional_ownership_pct": info.get("heldPercentInstitutions"),
        "dividend_yield": info.get("dividendYield"),
        "earnings_date": earnings_date,
        "earnings_days": earnings_days,
        "expected_eps": info.get("epsForward") or info.get("epsCurrentYear"),
        "forward_pe": info.get("forwardPE"),
        "peg": info.get("pegRatio"),
        "ev_ebitda": info.get("enterpriseToEbitda"),
        "earnings_growth": info.get("earningsGrowth"),
        "revenue_growth": info.get("revenueGrowth"),
        "debt_to_equity": info.get("debtToEquity"),
        "analyst_rec": info.get("recommendationKey"),
        "analyst_target": info.get("targetMeanPrice"),
        "analyst_n": info.get("numberOfAnalystOpinions"),
        "trailing_pe": info.get("trailingPE"),
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
    t_state = tactical.apply_extension_execution_overlay(t_state) or t_state
    market_payload = _market_payload(ticker, hist, t_state)
    t_state["price"] = market_payload.get("price", t_state.get("price"))
    rule_payload = dict(t_state)
    trigger = rule_payload.get("trigger") or {}
    if isinstance(trigger, dict):
        rule_payload["trigger_summary"] = trigger.get("summary")
    receipt = decision_contract.build_decision_receipt(
        ticker,
        {**rule_payload, "trigger_summary": _trigger_summary(rule_payload)},
        engine_version=RULE_ENGINE_VERSION,
    )
    consistency = decision_contract.receipt_consistency(ticker, {
        "receipt": receipt,
        "rule_output": {**rule_payload, "rule_engine_version": RULE_ENGINE_VERSION},
    })
    rule_payload["decision_receipt"] = receipt
    rule_payload["decision_attribution"] = decision_contract.build_rule_attribution(rule_payload)
    rule_payload["decision_consistency"] = {"ok": not consistency, "mismatches": consistency}
    rule_payload["shadow_evaluations"] = _shadow_evaluations(rule_payload)
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
    t_state = tactical.apply_extension_execution_overlay(t_state) or t_state
    market_payload = _market_payload(ticker, hist, t_state)
    meta = _quote_meta(ticker)
    market_payload["company_name"] = meta.get("company_name")
    market_payload["security_profile"] = {**meta, "updated_at": market_payload.get("updated_at")}
    t_state["price"] = market_payload.get("price", t_state.get("price"))
    rule_payload = dict(t_state)
    trigger = rule_payload.get("trigger") or {}
    if isinstance(trigger, dict):
        rule_payload["trigger_summary"] = trigger.get("summary")
    receipt = decision_contract.build_decision_receipt(
        ticker,
        {**rule_payload, "trigger_summary": _trigger_summary(rule_payload)},
        engine_version=RULE_ENGINE_VERSION,
    )
    consistency = decision_contract.receipt_consistency(ticker, {
        "receipt": receipt,
        "rule_output": {**rule_payload, "rule_engine_version": RULE_ENGINE_VERSION},
    })
    rule_payload["decision_receipt"] = receipt
    rule_payload["decision_attribution"] = decision_contract.build_rule_attribution(rule_payload)
    rule_payload["decision_consistency"] = {"ok": not consistency, "mismatches": consistency}
    rule_payload["shadow_evaluations"] = _shadow_evaluations(rule_payload)
    backend.upsert_json_table("market_snapshots", "ticker", ticker, market_payload, source="yahoo")
    backend.upsert_json_table("rule_outputs", "ticker", ticker, rule_payload, source="rules")
    return rule_payload, meta


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
        # Background jobs need a dependable, bounded response more than the
        # oversized legacy dossier.  The fast contract still returns every
        # public research field while avoiding frequent 45-second timeouts.
        fast=True,
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
    tickers = [ticker for ticker in tickers if market_freshness.worker_should_refresh(ticker)]
    if not tickers:
        return {
            "queued": False,
            "tickers": [],
            "reason": "snapshots current for the active or last completed market session",
        }
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


def queue_scheduled_backend_maintenance() -> dict:
    """Keep regime intraday-current; run slower repair work once per UTC day."""
    today = date.today()
    now = datetime.now(timezone.utc)
    recent = backend.latest_jobs(limit=100)
    saved_regime_rows = backend.read_json_table("market_regime_daily", limit=1)
    saved_regime = next(iter(saved_regime_rows.values()), {}) if saved_regime_rows else {}
    saved_crypto = saved_regime.get("crypto_regime") if isinstance(saved_regime, dict) else {}
    force_crypto_upgrade = not isinstance(saved_crypto, dict) or saved_crypto.get("model_version") != CRYPTO_REGIME_MODEL_VERSION
    force_regime_upgrade = not isinstance(saved_regime, dict) or saved_regime.get("schema_version") != MARKET_REGIME_SCHEMA_VERSION
    regime_stamp = saved_regime.get("generated_at") if isinstance(saved_regime, dict) else None
    try:
        regime_age_minutes = (now - datetime.fromisoformat(str(regime_stamp).replace("Z", "+00:00"))).total_seconds() / 60
    except (TypeError, ValueError):
        regime_age_minutes = float("inf")
    queued = []
    for job_type, priority in (("market_regime_daily", 20), ("repair_missing_data", 40)):
        already_today = False
        for row in recent:
            if row.get("job_type") != job_type or row.get("status") not in {"queued", "running", "succeeded"}:
                continue
            stamp = row.get("created_at")
            stamp_date = stamp.date() if hasattr(stamp, "date") else None
            if stamp_date == today:
                already_today = True
                break
        if job_type == "market_regime_daily" and (force_crypto_upgrade or force_regime_upgrade):
            already_today = False
        elif job_type == "market_regime_daily" and regime_age_minutes >= 90:
            # GitHub runs throughout the day. A once-daily gate left Lovable on
            # an overnight decision while Streamlit used current market data.
            # Refresh the canonical snapshot whenever it is materially stale;
            # the recent-job check above still prevents duplicate active runs.
            recent_regime_job = next((
                row for row in recent
                if row.get("job_type") == "market_regime_daily"
                and row.get("status") in {"queued", "running", "succeeded"}
            ), None)
            recent_stamp = recent_regime_job.get("created_at") if recent_regime_job else None
            try:
                recent_age = (now - recent_stamp).total_seconds() / 60
            except (TypeError, AttributeError):
                recent_age = float("inf")
            already_today = recent_age < 60
        if not already_today:
            job_id = backend.enqueue_job(job_type, priority=priority, requested_by="worker-maintenance")
            if job_id:
                queued.append(job_type)
    return {"queued": queued}


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
    if job_type == "market_regime_daily":
        return refresh_market_regime_daily(payload)
    if job_type == "repair_missing_data":
        return repair_missing_data(payload)
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
            maintenance_jobs = queue_scheduled_backend_maintenance()
            if maintenance_jobs.get("queued"):
                print(f"queued backend maintenance: {', '.join(maintenance_jobs['queued'])}")
        except Exception as exc:
            _gha_warning(f"Backend maintenance queue skipped: {exc}")
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
