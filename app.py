"""Desk — decision-first tactical interface with two-layer PM view.

Hierarchy (strict, top-down):
  1. DECISION          — ENTER ⚡ / WATCH 👀 / AVOID ⛔
  2. TRIGGER ⚡         — single price-based condition
  3. INVALIDATION ⛔    — binary, directly under trigger
  4. IF TRIGGER HITS 📊 — trade structure, conditional
  5. PM VIEW 🧠
       Layer 1: scan — thesis / drivers / risks / valuation
       Layer 2: full thesis expansion (expandable)
"""

import json
from pathlib import Path
from datetime import datetime, timedelta

import streamlit as st
import yfinance as yf

import tactical
from pm_view import get_pm_view, get_decision_dossier, STATIC_SNAPSHOTS


st.set_page_config(
    page_title="Desk",
    page_icon="▸",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Persistent store lives in the user's home folder, NOT in the app folder.
# That way, replacing the desk-local folder during upgrades doesn't wipe
# the user's watchlist, decisions, account size, or PM cache.
STORE_PATH = Path.home() / ".desk_store.json"
LEGACY_STORE_PATH = Path(__file__).parent / "desk_store.json"

# One-time migration: if the legacy in-folder store exists and the new
# home-folder store doesn't, move the legacy file forward.
if LEGACY_STORE_PATH.exists() and not STORE_PATH.exists():
    try:
        STORE_PATH.write_text(LEGACY_STORE_PATH.read_text())
    except Exception:
        pass
PM_CACHE_TTL_DAYS = 7


def load_store():
    if STORE_PATH.exists():
        try:
            return json.loads(STORE_PATH.read_text())
        except Exception:
            pass
    return {
        "watchlist": ["NVDA", "META", "AAPL", "MSFT", "TSLA"],
        "log": [],
        "pm_cache": {},
        "account_size": 100000,
        "risk_per_trade": 0.01,
        "max_position_pct": 0.25,
    }


def save_store(store):
    STORE_PATH.write_text(json.dumps(store, indent=2))


if "store" not in st.session_state:
    st.session_state.store = load_store()
    if "pm_cache" not in st.session_state.store:
        st.session_state.store["pm_cache"] = {}
    if "account_size" not in st.session_state.store:
        st.session_state.store["account_size"] = 100000
    if "risk_per_trade" not in st.session_state.store:
        st.session_state.store["risk_per_trade"] = 0.01
    if "max_position_pct" not in st.session_state.store:
        st.session_state.store["max_position_pct"] = 0.25
if "current_ticker" not in st.session_state:
    st.session_state.current_ticker = "NVDA"
if "view" not in st.session_state:
    st.session_state.view = "analyze"
if "pm_expanded" not in st.session_state:
    st.session_state.pm_expanded = {}
if "nav_counter" not in st.session_state:
    st.session_state.nav_counter = 0


def get_cached_pm(ticker, tactical_output, api_key, company_name):
    ticker = ticker.upper()
    cache = st.session_state.store["pm_cache"]
    entry = cache.get(ticker)
    if entry:
        ts = entry.get("ts")
        try:
            age = datetime.now() - datetime.fromisoformat(ts)
            if age < timedelta(days=PM_CACHE_TTL_DAYS):
                pm = entry["view"]
                pm["_source"] = (entry.get("source") or "cached") + f" · {age.days}d old"
                return pm
        except Exception:
            pass
    pm = get_pm_view(ticker, tactical_output, api_key=api_key, company_name=company_name)
    cache[ticker] = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "view": {k: v for k, v in pm.items() if not k.startswith("_")},
        "source": pm.get("_source", "static"),
    }
    save_store(st.session_state.store)
    return pm


def clear_pm_cache(ticker):
    ticker = ticker.upper()
    if ticker in st.session_state.store["pm_cache"]:
        del st.session_state.store["pm_cache"][ticker]
        save_store(st.session_state.store)


def get_cached_dossier(ticker, t_state, modifiers, meta, pm_data, api_key, company_name):
    """Cache decision dossiers separately from PM views — they share the
    same 7-day TTL but key independently so a PM-view ↻ refresh doesn't
    cost a dossier regeneration too."""
    if not api_key:
        return {"dossier": None, "_source": "unavailable"}
    ticker = ticker.upper()
    cache = st.session_state.store.setdefault("dossier_cache", {})
    entry = cache.get(ticker)
    if entry:
        try:
            age = datetime.now() - datetime.fromisoformat(entry.get("ts"))
            if age < timedelta(days=PM_CACHE_TTL_DAYS):
                return {"dossier": entry["text"], "_source": entry.get("source", "claude") + f" · {age.days}d old"}
        except Exception:
            pass
    result = get_decision_dossier(
        ticker, t_state, modifiers, meta, pm_data,
        api_key=api_key, company_name=company_name,
    )
    if result.get("dossier"):
        cache[ticker] = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "text": result["dossier"],
            "source": result.get("_source", "claude"),
        }
        save_store(st.session_state.store)
    return result


def clear_dossier_cache(ticker):
    ticker = ticker.upper()
    cache = st.session_state.store.get("dossier_cache", {})
    if ticker in cache:
        del cache[ticker]
        save_store(st.session_state.store)


# ─────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500;600&display=swap');

.stApp { background: #FBFAF7; }
.main .block-container {
    padding-top: 1.2rem; padding-bottom: 3rem; max-width: 1400px;
    font-size: 18px;
}

html, body, [class*="css"], .main, .main p, .main div, .main span, .main li {
    font-family: 'Geist', -apple-system, system-ui, sans-serif;
    color: #0F0E0D;
    font-size: 18px;
}
/* Streamlit's default emotion classes also need the bump */
.stMarkdown, .stMarkdown p, .stMarkdown div, .stMarkdown span,
[data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] p {
    font-size: 18px;
}
#MainMenu, header, footer { visibility: hidden; }

/* ────────────────────────────────────────────────────────────── */
/*  Decision Dossier — top hero paragraph synthesizing everything */
/* ────────────────────────────────────────────────────────────── */
.desk-dossier {
    margin: 0 0 28px;
    padding: 18px 22px;
    background: #FFFFFF;
    border: 1px solid #E5E3DE;
    border-left: 4px solid #0F0E0D;
    border-radius: 4px;
}
.desk-dossier-label {
    font-family: 'Geist', sans-serif;
    font-size: 11px; font-weight: 600; letter-spacing: 0.18em;
    text-transform: uppercase; color: #6B655B;
    display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 10px;
}
.desk-dossier-label .src {
    font-family: 'Geist Mono', monospace; font-size: 10px;
    color: #B4ADA0; letter-spacing: 0.04em;
    text-transform: none; font-weight: 400;
}
.desk-dossier-text {
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 19px; line-height: 1.55; color: #0F0E0D;
    font-style: italic;
}

/* ────────────────────────────────────────────────────────────── */
/*  Decision modifiers — badges between decision word and trigger */
/* ────────────────────────────────────────────────────────────── */
.desk-modifiers {
    margin: 4px 0 26px;
    display: flex; flex-wrap: wrap; gap: 8px;
}
.desk-mod {
    padding: 7px 12px;
    font-size: 14px; line-height: 1.35;
    border-radius: 3px;
    border: 1px solid;
    display: flex; align-items: center; gap: 8px;
}
.desk-mod-high { background: #FDF5F5; border-color: #F5C8C8; color: #6E2E2E; }
.desk-mod-med  { background: #FEF7E8; border-color: #F5D88A; color: #6B4E1D; }
.desk-mod-low  { background: #F5F2EB; border-color: #E5E3DE; color: #3F3B34; }
.desk-mod .icon {
    font-size: 14px; line-height: 1;
}

/* Navbar */
.desk-bar {
    background: #0F0E0D; color: #FBFAF7;
    padding: 11px 20px;
    display: flex; justify-content: space-between; align-items: center;
    margin: -1.2rem -1rem 1.5rem;
}
.desk-bar .wordmark {
    font-family: 'Instrument Serif', Georgia, serif; font-style: italic;
    font-size: 22px; line-height: 1;
}
.desk-bar .wordmark .arrow { color: #00A870; margin-right: 3px; }
.desk-bar .meta {
    font-family: 'Geist Mono', ui-monospace, monospace; font-size: 11px;
    color: #8A857C; letter-spacing: 0.08em; text-transform: uppercase;
}

/* Sidebar: low visual weight */
section[data-testid="stSidebar"] { background: #F5F2EB; }
section[data-testid="stSidebar"] .stMarkdown h3 {
    font-size: 11px; font-weight: 600; letter-spacing: 0.1em;
    text-transform: uppercase; color: #6B655B; margin-bottom: 6px;
}
section[data-testid="stSidebar"] div.stButton > button {
    background: transparent; border: 1px solid transparent;
    color: #3F3B34;
    font-family: 'Geist Mono', monospace; font-size: 11px; font-weight: 500;
    border-radius: 3px; padding: 4px 8px; text-align: left; justify-content: flex-start;
}
section[data-testid="stSidebar"] div.stButton > button:hover {
    background: #EDE8DD; color: #0F0E0D;
}
section[data-testid="stSidebar"] [role="radiogroup"] label {
    padding: 6px 10px; margin: 1px 0; border-radius: 3px;
    font-size: 13px; color: #3F3B34;
}
section[data-testid="stSidebar"] [role="radiogroup"] label:hover { background: #EDE8DD; }

/* Ticker line */
.desk-ticker-row {
    display: flex; justify-content: space-between; align-items: baseline;
    padding-bottom: 10px; border-bottom: 1px solid #E5E3DE; margin-bottom: 24px;
}
.desk-ticker-row .sym {
    font-size: 30px; font-weight: 600; letter-spacing: -0.02em; line-height: 1;
}
.desk-ticker-row .name { font-size: 16px; color: #6B655B; margin-left: 12px; }
.desk-ticker-row .price {
    font-family: 'Geist Mono', monospace; font-variant-numeric: tabular-nums;
    font-size: 19px; font-weight: 500;
}
.desk-ticker-row .chg {
    font-family: 'Geist Mono', monospace; font-variant-numeric: tabular-nums;
    font-size: 15px; margin-left: 12px;
}
.desk-ticker-row .meta-inline {
    font-family: 'Geist Mono', monospace; font-variant-numeric: tabular-nums;
    font-size: 13px; color: #8A857C;
    margin-top: 5px; letter-spacing: 0.02em;
}

/* ────────────────────────────────────────────────────────────── */
/*  1. DECISION — hero                                            */
/* ────────────────────────────────────────────────────────────── */
.desk-decision {
    padding: 4px 0 24px;
    margin-bottom: 32px;
    border-bottom: 1px solid #E5E3DE;
}
.desk-decision .word {
    font-family: 'Instrument Serif', Georgia, serif; font-style: italic;
    font-size: 104px; line-height: 0.92; letter-spacing: -0.04em;
    display: inline-block;
}
.desk-decision .emoji { font-size: 52px; margin-left: 12px; vertical-align: 12px; }
.desk-decision .context {
    font-size: 24px; color: #0F0E0D; margin-top: 18px;
    line-height: 1.4; max-width: 660px; font-weight: 400;
}

/* ────────────────────────────────────────────────────────────── */
/*  2. TRIGGER — most important actionable                        */
/* ────────────────────────────────────────────────────────────── */
.desk-trigger-block { margin: 8px 0 32px; }
.desk-trigger-label {
    font-family: 'Geist', sans-serif;
    font-size: 12px; font-weight: 600; letter-spacing: 0.18em;
    text-transform: uppercase; color: #0F0E0D;
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 10px;
}
.desk-trigger-label .em { font-size: 15px; }
.desk-trigger-text {
    font-family: 'Instrument Serif', Georgia, serif; font-style: italic;
    font-size: 44px; line-height: 1.2; color: #0F0E0D;
    letter-spacing: -0.015em;
}
.desk-trigger-text b {
    font-family: 'Geist Mono', monospace; font-style: normal;
    font-weight: 600; font-variant-numeric: tabular-nums;
    background: #FFF1C4; padding: 0 7px; border-radius: 2px;
    font-size: 40px;
}

/* ────────────────────────────────────────────────────────────── */
/*  3. INVALIDATION — binary, directly under trigger              */
/* ────────────────────────────────────────────────────────────── */
.desk-invalidation {
    margin: 4px 0 36px;
    padding: 10px 14px;
    background: #FDF5F5;
    border-left: 3px solid #D14545;
    border-radius: 2px;
}
.desk-invalidation .label {
    font-family: 'Geist', sans-serif;
    font-size: 11px; font-weight: 600; letter-spacing: 0.18em;
    text-transform: uppercase; color: #8B2F2F;
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 3px;
}
.desk-invalidation .label .em { font-size: 13px; }
.desk-invalidation .text {
    font-size: 18px; color: #3F3B34; line-height: 1.4;
}
.desk-invalidation .text b {
    font-family: 'Geist Mono', monospace; font-weight: 600;
    font-variant-numeric: tabular-nums; color: #0F0E0D;
}

/* ────────────────────────────────────────────────────────────── */
/*  Read of the tape — concise current-state facts (Avoid layout) */
/* ────────────────────────────────────────────────────────────── */
.desk-tape-read {
    margin: 8px 0 16px;
    padding: 12px 16px;
    background: #FFFFFF;
    border: 1px solid #E5E3DE;
    border-radius: 3px;
}
.desk-tape-read .label {
    font-family: 'Geist', sans-serif;
    font-size: 11px; font-weight: 600; letter-spacing: 0.16em;
    text-transform: uppercase; color: #6B655B;
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 10px;
}
.desk-tape-read .label .em { font-size: 13px; }
.desk-tape-read .row {
    display: flex; justify-content: space-between; align-items: baseline;
    padding: 6px 0;
    border-top: 1px dashed #EFEDE7;
}
.desk-tape-read .row:first-of-type { border-top: none; }
.desk-tape-read .row .k {
    font-family: 'Geist', sans-serif;
    font-size: 12px; font-weight: 600; letter-spacing: 0.08em;
    text-transform: uppercase; color: #8A857C;
    flex-shrink: 0; min-width: 90px;
}
.desk-tape-read .row .v {
    font-family: 'Geist Mono', monospace; font-variant-numeric: tabular-nums;
    font-size: 14px; line-height: 1.4;
    text-align: right;
}
.desk-tape-read .row .v b {
    font-family: 'Geist Mono', monospace; font-weight: 600;
    color: #0F0E0D;
}

/* ────────────────────────────────────────────────────────────── */
/*  Avoid-state replacements for trigger / invalidation            */
/* ────────────────────────────────────────────────────────────── */
.desk-avoid-reasons {
    margin: 8px 0 20px;
    padding: 12px 16px;
    background: #FDF5F5;
    border-left: 3px solid #D14545;
    border-radius: 2px;
}
.desk-avoid-reasons .label {
    font-family: 'Geist', sans-serif;
    font-size: 12px; font-weight: 600; letter-spacing: 0.18em;
    text-transform: uppercase; color: #8B2F2F;
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 8px;
}
.desk-avoid-reasons .label .em { font-size: 14px; }
.desk-avoid-reasons ul {
    margin: 0; padding: 0; list-style: none;
}
.desk-avoid-reasons li {
    font-size: 17px; color: #3F3B34; line-height: 1.45;
    padding: 4px 0 4px 16px; position: relative;
}
.desk-avoid-reasons li:before {
    content: '–'; position: absolute; left: 0; top: 4px;
    color: #A8A29E;
}
.desk-avoid-reasons li b {
    font-family: 'Geist Mono', monospace; font-weight: 600;
    font-variant-numeric: tabular-nums; color: #0F0E0D;
    font-size: 16px;
}

.desk-reconsider {
    margin: 4px 0 36px;
    padding: 12px 16px;
    background: #F5F8F4;
    border-left: 3px solid #2E7D4F;
    border-radius: 2px;
}
.desk-reconsider .label {
    font-family: 'Geist', sans-serif;
    font-size: 12px; font-weight: 600; letter-spacing: 0.18em;
    text-transform: uppercase; color: #2E5E3A;
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 8px;
}
.desk-reconsider .label .em { font-size: 14px; }
.desk-reconsider ul {
    margin: 0; padding: 0; list-style: none;
}
.desk-reconsider li {
    font-size: 17px; color: #3F3B34; line-height: 1.45;
    padding: 4px 0 4px 16px; position: relative;
}
.desk-reconsider li:before {
    content: '→'; position: absolute; left: 0; top: 3px;
    color: #2E7D4F; font-weight: 500;
}
.desk-reconsider li b {
    font-family: 'Geist Mono', monospace; font-weight: 600;
    font-variant-numeric: tabular-nums; color: #0F0E0D;
    font-size: 16px;
}

/* ────────────────────────────────────────────────────────────── */
/*  4. IF TRIGGER HITS — conditional trade plan, secondary        */
/* ────────────────────────────────────────────────────────────── */
.desk-plan-label {
    font-family: 'Geist', sans-serif;
    font-size: 12px; font-weight: 600; letter-spacing: 0.18em;
    text-transform: uppercase; color: #6B655B;
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 12px;
}
.desk-plan-label .em { font-size: 14px; }
.desk-plan-row {
    display: flex; justify-content: space-between; align-items: baseline;
    padding: 10px 0; border-top: 1px dashed #E5E3DE;
}
.desk-plan-row:last-child { border-bottom: 1px dashed #E5E3DE; }
.desk-plan-row .k { font-size: 17px; color: #8A857C; }
.desk-plan-row .v {
    font-family: 'Geist Mono', monospace; font-variant-numeric: tabular-nums;
    font-size: 18px; font-weight: 500; color: #3F3B34;
}
.desk-plan-row .d {
    font-family: 'Geist Mono', monospace; font-variant-numeric: tabular-nums;
    font-size: 14px; margin-left: 6px;
}

/* ────────────────────────────────────────────────────────────── */
/*  5. PM VIEW — two-layer, right column, separated               */
/* ────────────────────────────────────────────────────────────── */
.desk-pm-container {
    border-left: 1px solid #E5E3DE;
    padding-left: 24px;
}
.desk-pm-header {
    font-family: 'Geist', sans-serif;
    font-size: 12px; font-weight: 600; letter-spacing: 0.16em;
    text-transform: uppercase; color: #6B655B;
    margin-bottom: 14px; padding-bottom: 6px;
    border-bottom: 1px solid #E5E3DE;
    display: flex; justify-content: space-between; align-items: baseline;
}
.desk-pm-header .em { font-size: 14px; margin-right: 6px; }
.desk-pm-header .src {
    font-family: 'Geist Mono', monospace; font-size: 10px; color: #B4ADA0;
    letter-spacing: 0.04em; text-transform: none; font-weight: 400;
}
.desk-pm-block { margin-bottom: 16px; }
.desk-pm-block .lb {
    font-family: 'Geist', sans-serif;
    font-size: 10px; font-weight: 600; letter-spacing: 0.14em;
    text-transform: uppercase; color: #8A857C; margin-bottom: 5px;
}
.desk-pm-block .body {
    font-family: 'Instrument Serif', Georgia, serif; font-style: italic;
    font-size: 21px; line-height: 1.5; color: #3F3B34;
}
.desk-pm-item {
    font-family: 'Instrument Serif', Georgia, serif; font-style: italic;
    font-size: 19px; line-height: 1.45; color: #3F3B34;
    padding: 3px 0 3px 16px; position: relative;
}
.desk-pm-item:before {
    content: '–'; position: absolute; left: 0; top: 1px;
    font-family: 'Geist', sans-serif; font-style: normal;
    color: #A8A29E; font-weight: 400;
}
.desk-pm-deep {
    margin-top: 20px; padding-top: 20px;
    border-top: 1px dashed #D9D5CC;
}
.desk-pm-deep .sub-lb {
    font-family: 'Geist', sans-serif;
    font-size: 9px; font-weight: 600; letter-spacing: 0.14em;
    text-transform: uppercase; color: #8A857C; margin: 14px 0 4px;
}
.desk-pm-deep .sub-body {
    font-family: 'Instrument Serif', Georgia, serif; font-style: italic;
    font-size: 14px; line-height: 1.5; color: #3F3B34;
}
.desk-pm-deep .variant-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
    margin-top: 4px;
}
.desk-pm-deep .variant-card {
    border: 1px solid #E5E3DE; border-radius: 3px;
    padding: 9px 11px;
    background: #FFFFFF;
}
.desk-pm-deep .variant-card .lb-bull { color: #2E7D4F; }
.desk-pm-deep .variant-card .lb-bear { color: #8B2F2F; }
.desk-pm-deep .variant-card .lb {
    font-size: 9px; font-weight: 600; letter-spacing: 0.12em;
    text-transform: uppercase; margin-bottom: 3px;
}
.desk-pm-deep .variant-card .body {
    font-family: 'Instrument Serif', Georgia, serif; font-style: italic;
    font-size: 13px; line-height: 1.45; color: #3F3B34;
}

/* Deep-dive expand button — aligned with stat cards */
.desk-pm-container ~ div.stButton,
.desk-stat-card ~ div.stButton {
    margin-left: 24px;
}
div.stButton > button {
    background: transparent; border: 1px solid #E5E3DE; color: #0F0E0D;
    font-family: 'Geist Mono', monospace; font-size: 12px; font-weight: 500;
    border-radius: 3px; padding: 6px 10px;
}
.main div.stButton > button:hover {
    background: #0F0E0D; color: #FBFAF7; border-color: #0F0E0D;
}

/* Technical details expander — full bordered container */
div[data-testid="stExpander"],
details.stExpander,
div.stExpander {
    border: 1px solid #E5E3DE !important;
    border-radius: 4px !important;
    background: #FFFFFF !important;
    margin-top: 14px !important;
    overflow: hidden;
}
div[data-testid="stExpander"] > details,
div[data-testid="stExpander"] details {
    border: none !important;
    background: transparent !important;
}
div[data-testid="stExpander"] summary,
details.stExpander summary,
div[data-testid="stExpander"] details summary {
    font-size: 13px !important; font-weight: 500 !important;
    color: #3F3B34 !important; letter-spacing: 0.04em !important;
    padding: 12px 16px !important;
    list-style: none;
}
div[data-testid="stExpanderDetails"],
div[data-testid="stExpander"] div[data-testid="stExpanderDetails"] {
    padding: 4px 16px 14px 16px !important;
    border-top: 1px solid #E5E3DE;
}
div.streamlit-expanderHeader {
    font-size: 13px !important; font-weight: 500 !important;
    color: #3F3B34 !important; letter-spacing: 0.04em !important;
}

/* ────────────────────────────────────────────────────────────── */
/*  Earnings banner — shown when earnings within 7 days           */
/* ────────────────────────────────────────────────────────────── */
.desk-earnings-banner {
    background: #FEF7E8;
    border: 1px solid #F5D88A;
    border-radius: 3px;
    padding: 10px 14px;
    margin: 0 0 20px;
    display: flex; align-items: center; gap: 10px;
    font-size: 13px; color: #6B4E1D;
    line-height: 1.4;
}
.desk-earnings-banner .em { font-size: 15px; }
.desk-earnings-banner b {
    font-family: 'Geist Mono', monospace; font-weight: 600;
    font-variant-numeric: tabular-nums; color: #3F2E0A;
}

/* ────────────────────────────────────────────────────────────── */
/*  Meta strip — sector · market cap · short interest · earnings  */
/* ────────────────────────────────────────────────────────────── */
.desk-meta-strip {
    display: flex; flex-wrap: wrap; gap: 18px;
    padding: 10px 0 4px;
    margin-bottom: 20px;
    border-bottom: 1px solid #E5E3DE;
}
.desk-meta-item {
    display: flex; flex-direction: column; gap: 1px;
}
.desk-meta-item .lb {
    font-family: 'Geist', sans-serif;
    font-size: 9px; font-weight: 600; letter-spacing: 0.12em;
    text-transform: uppercase; color: #8A857C;
}
.desk-meta-item .v {
    font-family: 'Geist Mono', monospace; font-variant-numeric: tabular-nums;
    font-size: 12px; font-weight: 500; color: #3F3B34;
}

/* ────────────────────────────────────────────────────────────── */
/*  Stat cards — earnings, analyst consensus, Lynch check          */
/* ────────────────────────────────────────────────────────────── */
.desk-stat-card {
    margin: 14px 0 0 24px;
    padding: 12px 14px;
    background: #FFFFFF;
    border: 1px solid #E5E3DE;
    border-radius: 4px;
}
.desk-stat-card .label {
    font-family: 'Geist', sans-serif;
    font-size: 10px; font-weight: 600; letter-spacing: 0.14em;
    text-transform: uppercase; color: #6B655B;
    margin-bottom: 8px;
}
.desk-stat-card .row {
    display: flex; justify-content: space-between; align-items: baseline;
    padding: 6px 0;
    font-size: 16px; color: #3F3B34;
}
.desk-stat-card .row + .row {
    border-top: 1px dashed #EFEDE7;
}
.desk-stat-card .row .v {
    font-family: 'Geist Mono', monospace; font-variant-numeric: tabular-nums;
    font-size: 15px; color: #0F0E0D;
}

/* Technical-read variant: prose inside a stat card */
.desk-stat-card-read .read-body {
    font-family: 'Geist', sans-serif;
    font-size: 14px; line-height: 1.55; color: #3F3B34;
    padding: 4px 0 2px;
}
.desk-stat-card-read .read-body p {
    margin: 0 0 10px; padding: 0;
}
.desk-stat-card-read .read-body p:last-child { margin-bottom: 0; }
.desk-stat-card-read .read-body b {
    font-family: 'Geist Mono', monospace; font-weight: 600;
    font-variant-numeric: tabular-nums; color: #0F0E0D;
    font-size: 13px;
}
.desk-chart-label {
    font-family: 'Geist', sans-serif;
    font-size: 11px; font-weight: 600; letter-spacing: 0.18em;
    text-transform: uppercase; color: #6B655B;
    margin: 32px 0 10px;
}

/* ────────────────────────────────────────────────────────────── */
/*  Technical commentary — sits under PM view in right column      */
/* ────────────────────────────────────────────────────────────── */
.desk-commentary {
    margin: 28px 0 0;
    padding-left: 24px;
    padding-top: 20px;
    border-top: 1px solid #E5E3DE;
    border-left: 1px solid #E5E3DE;
}
.desk-commentary-label {
    font-family: 'Geist', sans-serif;
    font-size: 12px; font-weight: 600; letter-spacing: 0.16em;
    text-transform: uppercase; color: #6B655B;
    margin-bottom: 10px;
    display: flex; align-items: center; gap: 8px;
}
.desk-commentary-label .em { font-size: 14px; }
.desk-commentary-body {
    font-family: 'Geist', sans-serif;
    font-size: 18px; line-height: 1.55; color: #3F3B34;
}
.desk-commentary-body p {
    margin: 0 0 12px; padding: 0;
}
.desk-commentary-body p:last-child { margin-bottom: 0; }
.desk-commentary-body b {
    font-family: 'Geist Mono', monospace; font-weight: 600;
    font-variant-numeric: tabular-nums; color: #0F0E0D;
    font-size: 17px;
}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────
# Data fetch
# ─────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_history(ticker):
    try:
        yf_ticker = yf.Ticker(ticker)
        hist = yf_ticker.history(period="2y", interval="1d", auto_adjust=True)
        if len(hist) == 0:
            return None, None
        info = {}
        try:
            info = yf_ticker.info or {}
        except Exception:
            pass
        name = info.get("longName") or info.get("shortName") or ticker
        return hist, name
    except Exception:
        return None, None


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_bench():
    try:
        hist = yf.Ticker("SPY").history(period="2y", interval="1d", auto_adjust=True)
        return hist if len(hist) > 0 else None
    except Exception:
        return None


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_quote_meta(ticker):
    """Pull sector, market cap, short interest, earnings date, valuation ratios,
    analyst rating + target, dividend yield, growth rate, debt/equity.
    All optional. Cached 1 hour.
    """
    out = {
        "sector": None,
        "industry": None,
        "market_cap": None,
        "short_pct_float": None,
        "earnings_date": None,
        "earnings_days": None,
        "expected_eps": None,
        "forward_pe": None,
        "trailing_pe": None,
        "peg": None,
        "ev_ebitda": None,
        "debt_to_equity": None,    # as percent (e.g. 24.0 = 0.24)
        "earnings_growth": None,   # yoy, as percent
        "revenue_growth": None,    # yoy, as percent
        "dividend_yield": None,    # as percent
        "analyst_target": None,
        "analyst_rec": None,       # "strongBuy" / "buy" / "hold" / "sell" / "strongSell"
        "analyst_n": None,
    }
    try:
        yf_ticker = yf.Ticker(ticker)
        try:
            info = yf_ticker.info or {}
        except Exception:
            info = {}

        out["sector"] = info.get("sector")
        out["industry"] = info.get("industry")
        out["market_cap"] = info.get("marketCap")

        spf = info.get("shortPercentOfFloat")
        if spf is not None:
            out["short_pct_float"] = float(spf) * 100

        # Valuation ratios
        out["forward_pe"] = info.get("forwardPE")
        out["trailing_pe"] = info.get("trailingPE")
        # yfinance sometimes returns pegRatio, sometimes trailingPegRatio
        out["peg"] = info.get("pegRatio") or info.get("trailingPegRatio")
        out["ev_ebitda"] = info.get("enterpriseToEbitda")

        # Balance sheet — yfinance returns debt/equity as an absolute (e.g. 24.3 meaning 24.3%)
        # or sometimes as a decimal. Normalize to percent.
        de = info.get("debtToEquity")
        if de is not None:
            out["debt_to_equity"] = float(de) if de > 5 else float(de) * 100

        # Growth
        eg = info.get("earningsGrowth")
        if eg is not None:
            out["earnings_growth"] = float(eg) * 100
        rg = info.get("revenueGrowth")
        if rg is not None:
            out["revenue_growth"] = float(rg) * 100

        # Dividend yield — yfinance returns 0.015 for 1.5%
        dy = info.get("dividendYield")
        if dy is not None:
            out["dividend_yield"] = float(dy) * 100 if dy < 1 else float(dy)

        # Analyst data
        out["analyst_target"] = info.get("targetMeanPrice")
        out["analyst_rec"] = info.get("recommendationKey")
        out["analyst_n"] = info.get("numberOfAnalystOpinions")

        # EPS — forward estimate if available
        out["expected_eps"] = info.get("forwardEps") or info.get("trailingEps")

        # Earnings date — calendar first, earnings_dates fallback
        try:
            cal = yf_ticker.calendar
            if cal is not None and isinstance(cal, dict):
                ed = cal.get("Earnings Date")
                if isinstance(ed, list) and len(ed) > 0:
                    e0 = ed[0]
                    if hasattr(e0, "to_pydatetime"):
                        e0 = e0.to_pydatetime()
                    out["earnings_date"] = e0
            elif cal is not None:
                try:
                    e0 = cal.loc["Earnings Date"].iloc[0]
                    if hasattr(e0, "to_pydatetime"):
                        e0 = e0.to_pydatetime()
                    out["earnings_date"] = e0
                except Exception:
                    pass
        except Exception:
            pass

        if out["earnings_date"] is None:
            try:
                ed = yf_ticker.earnings_dates
                if ed is not None and len(ed) > 0:
                    now = datetime.now()
                    future = [d for d in ed.index
                              if (d.to_pydatetime().replace(tzinfo=None) if hasattr(d, "to_pydatetime") else d) >= now]
                    if future:
                        f0 = future[0]
                        if hasattr(f0, "to_pydatetime"):
                            f0 = f0.to_pydatetime().replace(tzinfo=None)
                        out["earnings_date"] = f0
            except Exception:
                pass

        if out["earnings_date"] is not None:
            try:
                days = (out["earnings_date"] - datetime.now()).days
                out["earnings_days"] = days
            except Exception:
                pass
    except Exception:
        pass
    return out


def classify_lynch(meta):
    """Put the company into one of Lynch's six buckets based on growth + sector.

    Returns (category_key, category_label, expected_pe_range_str).
    """
    sector = (meta.get("sector") or "").lower()
    eg = meta.get("earnings_growth")
    rg = meta.get("revenue_growth")
    growth = None
    if eg is not None:
        growth = eg
    elif rg is not None:
        growth = rg

    # Cyclicals — industrials, materials, energy, autos often fit regardless of today's growth
    cyclical_sectors = {"energy", "basic materials", "industrials", "consumer cyclical"}
    if any(c in sector for c in cyclical_sectors):
        return ("cyclical", "Cyclical", "10–15× at cycle peaks, higher at troughs")

    if growth is None:
        return ("unknown", "Unclassified", "—")

    if growth >= 20:
        return ("fast_grower", "Fast grower", "PEG under 1.0 is cheap")
    if growth >= 10:
        return ("stalwart", "Stalwart", "15–25× forward is fair")
    if growth >= 2:
        return ("slow_grower", "Slow grower", "usually a dividend play")
    if growth >= -5:
        return ("slow_grower", "Slow grower", "usually a dividend play")
    return ("turnaround", "Potential turnaround", "earnings are recovering — PE may be distorted")


def format_recommendation(key, n):
    if not key:
        return None
    label_map = {
        "strong_buy": "Strong buy",
        "strongbuy": "Strong buy",
        "buy": "Buy",
        "hold": "Hold",
        "sell": "Sell",
        "strong_sell": "Strong sell",
        "strongsell": "Strong sell",
        "none": None,
    }
    lbl = label_map.get(str(key).lower().replace(" ", "_"))
    if not lbl:
        return None
    if n:
        return f"{lbl} · {int(n)} analysts"
    return lbl


def format_market_cap(cap):
    if not cap:
        return None
    if cap >= 1e12:
        return f"${cap/1e12:.2f}T"
    if cap >= 1e9:
        return f"${cap/1e9:.1f}B"
    if cap >= 1e6:
        return f"${cap/1e6:.0f}M"
    return f"${cap:,.0f}"


def format_earnings(meta):
    """Return (banner_text_or_None, footer_text_or_None).
    Banner if within 7 days, footer otherwise.
    """
    if not meta.get("earnings_date") or meta.get("earnings_days") is None:
        return None, None
    days = meta["earnings_days"]
    date_str = meta["earnings_date"].strftime("%b %d")
    if days < 0:
        # Old stale date — ignore
        return None, None
    if days <= 7:
        if days == 0:
            return f"Earnings today ({date_str}) — setup may reset after the print.", None
        if days == 1:
            return f"Earnings tomorrow ({date_str}) — setup may reset after the print.", None
        return f"Earnings in {days} days ({date_str}) — setup may reset after the print.", None
    return None, f"{date_str} · in {days} days"


# ─────────────────────────────────────────────────────────────────────
# Copy helpers
# ─────────────────────────────────────────────────────────────────────
STATE_STYLES = {
    "enter_now": {"color": "#00A870", "label": "Enter", "emoji": "⚡"},
    "watch":     {"color": "#D18700", "label": "Watch", "emoji": "👀"},
    "avoid":     {"color": "#D14545", "label": "Avoid", "emoji": "⛔"},
}


def decision_context(t):
    """One-line context. No numbers."""
    a = t["action"]
    if a == "enter_now":
        return "High-conviction setup — trend, structure, and volume aligned."
    if a == "watch":
        bias = (t.get("bias") or "bullish").capitalize()
        trg = t.get("trigger")
        if not trg:
            return f"{bias} — early setup."
        kind = trg["kind"]
        if kind == "reclaim_ma50":  return f"{bias} — waiting on 50-day reclaim."
        if kind == "fast_momentum": return f"{bias} — close to trigger."
        if kind == "breakout":      return f"{bias} — close to trigger."
        if kind == "coil_break":    return f"{bias} — coiling, needs direction."
        if kind == "pullback":      return f"{bias} — extended, waiting for pullback."
        if kind == "rs_catchup":    return f"{bias} — needs relative strength to confirm."
        return f"{bias} — needs confirmation."
    if a == "avoid":
        if t["raw_bias"] == "bearish":
            return "Avoid — bearish trend, no long setup."
        if not t["atr_ok"]:
            return "Avoid — daily range too tight for this system."
        return "Avoid — no directional edge, weak structure."
    return ""


def bold_numbers(s):
    import re
    s = re.sub(r"(\$[\d,]+\.?\d*)", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\$>])(\b\d{1,3}(?:,\d{3})+\b)", r"<b>\1</b>", s)
    return s


def trigger_text(t):
    """Short, price-based, single-condition."""
    if t["action"] == "enter_now":
        return f"Enter long at market — ${t['price']:.2f}."
    if t["action"] == "watch" and t.get("trigger"):
        trg = t["trigger"]
        kind = trg["kind"]
        buy = trg.get("levels", {}).get("buy_above")
        if kind == "reclaim_ma50" and buy:
            return f"Reclaim above ${buy:.2f} (the 50-day MA)."
        if kind in ("fast_momentum", "breakout") and buy:
            return f"Break above ${buy:.2f} on strong volume."
        if kind == "coil_break" and buy:
            return f"Break above ${buy:.2f} on expanding volume."
        if kind == "pullback" and buy:
            return f"Pullback to ${buy:.2f} that holds."
        if kind == "rs_catchup":
            return "Relative strength vs S&P 500 back above 1.00."
        if buy:
            return f"Close above ${buy:.2f}."
        return trg.get("summary", "").capitalize()
    if t["action"] == "avoid":
        if t["raw_bias"] == "bearish":
            return "No action — wait for a confirmed reversal."
        if not t["atr_ok"]:
            return "No action — volatility too low."
        return "No action — no edge."
    return ""


def invalidation_text(t):
    if t["action"] == "enter_now":
        return f"Below ${t['stop']:.2f}, setup is invalid."
    if t["action"] == "watch" and t.get("trigger"):
        abort = t["trigger"].get("levels", {}).get("abort_below")
        if abort:
            return f"Below ${abort:.2f}, setup is invalid."
    return None


def tape_read(t):
    """A tight 3-4 line read of the current technical state. Used in the
    Avoid layout where the user gets no Trigger/Trade-plan content and
    needs immediate technical numbers to ground the decision.

    Each line is a key/value pair: (label, value, optional_severity).
    severity drives color: 'pos' green, 'neg' red, '' neutral.
    """
    rows = []
    price = t["price"]
    ma50 = t["ma50"]
    ma200 = t["ma200"]
    rsi = t.get("rsi14")
    rs = t.get("rs", 1.0)
    pct_range = t.get("pct_of_52w_range")
    tech_delta = t.get("tech_delta", 0)
    vol_ratio = t.get("vol_ratio", 1.0)

    # 1. Trend posture — concrete dollar gap to MAs
    ma50_gap_pct = (price - ma50) / ma50 * 100
    ma200_gap_pct = (price - ma200) / ma200 * 100
    if price > ma50 and price > ma200:
        rows.append(("Trend", f"Above 50d (${ma50:.2f}, +{ma50_gap_pct:.1f}%) and 200d (${ma200:.2f}, +{ma200_gap_pct:.1f}%)", "pos"))
    elif price < ma50 and price < ma200:
        rows.append(("Trend", f"Below 50d (${ma50:.2f}, {ma50_gap_pct:.1f}%) and 200d (${ma200:.2f}, {ma200_gap_pct:.1f}%)", "neg"))
    elif price < ma50:
        rows.append(("Trend", f"Below 50d (${ma50:.2f}, {ma50_gap_pct:.1f}%), above 200d (${ma200:.2f}, +{ma200_gap_pct:.1f}%)", ""))
    else:
        rows.append(("Trend", f"Above 50d (${ma50:.2f}, +{ma50_gap_pct:.1f}%), below 200d (${ma200:.2f}, {ma200_gap_pct:.1f}%)", ""))

    # 2. Momentum
    if tech_delta >= 1.5:
        rows.append(("Momentum", f"Accelerating ({tech_delta:+.1f} over 10 sessions)", "pos"))
    elif tech_delta <= -1.5:
        rows.append(("Momentum", f"Rolling over ({tech_delta:+.1f} over 10 sessions)", "neg"))
    else:
        rows.append(("Momentum", f"Flat ({tech_delta:+.1f} over 10 sessions)", ""))

    # 3. RSI + 52w position combined
    rsi_part = f"RSI {rsi:.0f}" if rsi is not None else ""
    range_part = f"{pct_range:.0f}% of 52w range" if pct_range is not None else ""
    if rsi_part and range_part:
        sev = ""
        if rsi is not None and (rsi >= 75 or rsi <= 30):
            sev = "neg" if rsi <= 30 else ""
        rows.append(("Position", f"{rsi_part} · {range_part}", sev))

    # 4. Relative strength + volume
    rs_text = f"RS {rs:.2f} vs SPX"
    vol_text = f"vol {vol_ratio:.2f}× 20d avg"
    rs_sev = "neg" if rs < 0.95 else ("pos" if rs > 1.05 else "")
    rows.append(("Strength", f"{rs_text} · {vol_text}", rs_sev))

    return rows


def why_avoid_reasons(t):
    """For Avoid states, list concrete reasons the system is passing.
    Each reason references actual numbers from the tactical state."""
    reasons = []
    price = t["price"]
    ma50 = t["ma50"]
    ma200 = t["ma200"]
    bias = t.get("raw_bias")
    rs = t.get("rs", 1.0)
    sq = t.get("structure_quality", 5)
    atr_pct = t.get("atr_pct", 0)

    # Trend / MA stack
    if price < ma50 and price < ma200 and ma50 < ma200:
        reasons.append(f"Below both moving averages with the 50-day (${ma50:.2f}) under the 200-day (${ma200:.2f}) — full downtrend.")
    elif price < ma50 and price < ma200:
        reasons.append(f"Below both the 50-day (${ma50:.2f}) and 200-day (${ma200:.2f}) — no trend support overhead.")
    elif price < ma50:
        reasons.append(f"Below the 50-day moving average (${ma50:.2f}) — short-term trend has broken.")

    # Volatility gate
    if not t.get("atr_ok", True):
        reasons.append(f"Average true range is {atr_pct*100:.2f}% — below the 1.5% floor this system needs to work.")

    # Relative strength
    if rs < 0.95:
        reasons.append(f"Lagging the S&P 500 (relative strength {rs:.2f}) — the tape isn't supporting this name.")

    # Structure
    if sq <= 4:
        reasons.append(f"Structure score {sq:.1f}/10 — failing higher highs and higher lows on the daily.")

    # Bias-explicit fallback
    if not reasons:
        if bias == "bearish":
            reasons.append("Multiple bearish signals stacked — trend, momentum, and relative strength all negative.")
        elif bias == "neutral":
            reasons.append("Mixed signals across trend, structure, and relative strength — no directional edge.")
    return reasons


def reconsider_when(t):
    """Concrete conditions that would flip this from Avoid to Watch.
    Picks reversal levels intelligently — the nearest meaningful overhead
    resistance, not a far-off 52-week high that the stock would have to
    rally 50%+ to touch."""
    conditions = []
    price = t["price"]
    ma50 = t["ma50"]
    ma200 = t["ma200"]
    high_52w = t.get("high_52w", price)
    bias = t.get("raw_bias")
    rs = t.get("rs", 1.0)

    # ── Volatility-gated avoid: only one reversal condition matters ──
    if not t.get("atr_ok", True):
        conditions.append(
            f"Daily range expands above 1.5% (currently {t.get('atr_pct', 0)*100:.2f}%)."
        )
        return conditions

    # ── Choose the nearest overhead level for "break above" ──
    # We want the *closest* meaningful resistance above current price, not
    # a far-off 52w high that's irrelevant on the current timeframe.
    overhead_candidates = []
    if ma50 > price:
        overhead_candidates.append(("50-day moving average", ma50))
    if ma200 > price:
        overhead_candidates.append(("200-day moving average", ma200))
    # Only include 52w high if we're within 10% of it — otherwise it's noise
    if high_52w > price and (high_52w - price) / price < 0.10:
        overhead_candidates.append(("52-week high", high_52w))
    overhead_candidates.sort(key=lambda x: x[1])  # nearest first

    if bias == "bearish":
        # Need to reclaim trend levels first
        if price < ma50:
            conditions.append(f"Price reclaims and holds the 50-day moving average at ${ma50:.2f}.")
        if price < ma200 and len(conditions) < 2:
            conditions.append(f"Price reclaims and holds the 200-day moving average at ${ma200:.2f}.")
        if rs < 0.95:
            conditions.append("Relative strength versus the S&P 500 climbs back above 1.00.")

    elif bias == "neutral":
        # Reclaim the 50-day if below it, otherwise look for a clean break
        # above the nearest overhead resistance
        if price < ma50:
            conditions.append(f"Price reclaims and holds the 50-day moving average at ${ma50:.2f}.")
        elif overhead_candidates:
            level_name, level_price = overhead_candidates[0]
            conditions.append(
                f"A clean break above the {level_name} at ${level_price:.2f} on rising volume."
            )
        else:
            # Price is above all key MAs but bias is still neutral — likely
            # weak structure. Ask for a higher high.
            conditions.append("A higher high on the daily timeframe with confirming volume.")

        if rs < 1.0:
            conditions.append("Relative strength versus the S&P 500 turns positive.")

    if not conditions:
        conditions.append("Trend, structure, and relative strength all turn positive together.")
    return conditions


def technical_commentary(t):
    """Short prose synthesis. Focuses on what the chart + trigger DON'T
    already show: momentum, volume, relative strength, and setup-health
    context. We skip the MA-stack sentence (chart shows it) and the
    52-week-high sentence (trigger says it)."""
    lines = []

    # ── Setup-health lead: sets expectation before getting into signals ──
    action = t.get("action")
    if action == "enter_now":
        lines.append("All three confirming signals line up: trend, structure, and tape.")
    elif action == "watch":
        bias = (t.get("bias") or "bullish").lower()
        if bias == "bullish":
            lines.append("Directionally right, not yet tradable. Here's what the tape is doing while we wait:")
        else:
            lines.append("Mixed picture. Tape detail below.")
    elif action == "avoid":
        if t.get("raw_bias") == "bearish":
            lines.append("Trend is against you. Reading the tape anyway for completeness:")
        elif not t.get("atr_ok"):
            lines.append("Fine stock, wrong system — range is too tight to trade.")
        else:
            lines.append("No clean edge here. Tape detail:")

    # ── Momentum ──
    tech_delta = t.get("tech_delta", 0)
    if tech_delta >= 1.5:
        lines.append(f"Momentum is accelerating — technical score up {tech_delta:+.1f} over the last 10 sessions.")
    elif tech_delta <= -1.5:
        lines.append(f"Momentum is rolling over — technical score down {tech_delta:+.1f} over the last 10 sessions.")
    elif abs(tech_delta) < 0.5:
        lines.append("Momentum is flat over the last 10 sessions — neither building nor fading.")
    else:
        direction = "up" if tech_delta > 0 else "down"
        lines.append(f"Momentum drifting {direction} gently ({tech_delta:+.1f} over 10 sessions).")

    # ── Volume ──
    vol_ratio = t.get("vol_ratio", 1.0)
    if vol_ratio >= 1.4:
        lines.append(f"Today's volume is {vol_ratio:.1f}× the 20-day average — real participation behind the move.")
    elif vol_ratio >= 1.15:
        lines.append(f"Volume slightly above average ({vol_ratio:.1f}×) — mild confirmation.")
    elif vol_ratio <= 0.7:
        lines.append(f"Volume light — {vol_ratio:.1f}× the 20-day average. Any breakout here would lack confirmation.")
    else:
        lines.append(f"Volume near average ({vol_ratio:.2f}×) — nothing unusual.")

    # ── Relative strength ──
    rs = t.get("rs", 1.0)
    rs_delta = t.get("rs_delta", 0)
    if rs > 1.05 and rs_delta > 0:
        lines.append(f"Outpacing the S&P 500 over the last 60 days (RS {rs:.2f}) and the lead is widening.")
    elif rs > 1.05 and rs_delta <= 0:
        lines.append(f"Ahead of the S&P 500 (RS {rs:.2f}) but the lead is no longer widening.")
    elif rs < 0.95:
        lines.append(f"Lagging the S&P 500 (RS {rs:.2f}) — tape is not supporting the name.")
    else:
        lines.append(f"Roughly tracking the S&P 500 (RS {rs:.2f}).")

    # ── RSI ──
    rsi = t.get("rsi14")
    if rsi is not None:
        if rsi >= 75:
            lines.append(f"RSI at {rsi:.0f} — stretched. Pullback risk is elevated even in an uptrend.")
        elif rsi >= 60:
            lines.append(f"RSI at {rsi:.0f} — strong momentum, not yet overbought.")
        elif rsi >= 45:
            lines.append(f"RSI at {rsi:.0f} — neutral zone.")
        elif rsi >= 30:
            lines.append(f"RSI at {rsi:.0f} — momentum is soft, not oversold.")
        else:
            lines.append(f"RSI at {rsi:.0f} — oversold territory.")

    # ── 52-week range position ──
    pct_range = t.get("pct_of_52w_range")
    if pct_range is not None:
        if pct_range >= 85:
            lines.append(f"Trading at {pct_range:.0f}% of the 52-week range — upper zone.")
        elif pct_range >= 50:
            lines.append(f"Trading at {pct_range:.0f}% of the 52-week range — upper half.")
        elif pct_range >= 20:
            lines.append(f"Trading at {pct_range:.0f}% of the 52-week range — lower half.")
        else:
            lines.append(f"Trading at {pct_range:.0f}% of the 52-week range — near the lows.")

    # ── Historical context: how does price interact with the 50-day? ──
    hist = t.get("ma50_history")
    if hist and hist.get("tests", 0) >= 2:
        held = hist["held"]
        tests = hist["tests"]
        avg = hist["avg_bounce_pct"]
        months = hist["lookback_months"]
        if held >= tests * 0.7 and avg > 0:
            lines.append(
                f"Tested the 50-day {tests} times in the last ~{months} months — "
                f"held {held}/{tests}, average {avg:+.1f}% over the next 20 days. "
                f"This level has worked as support."
            )
        elif held <= tests * 0.3:
            lines.append(
                f"Tested the 50-day {tests} times in the last ~{months} months — "
                f"only held {held}/{tests}. This level has been unreliable."
            )
        else:
            lines.append(
                f"Tested the 50-day {tests} times in the last ~{months} months — "
                f"held {held}/{tests}, average {avg:+.1f}% over the next 20 days."
            )

    return lines


# ─────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────
with st.sidebar:
    view_labels = {"analyze": "Analyze", "watchlist": "Watchlist", "tracker": "Tracker"}
    current_view_label = view_labels[st.session_state.view]
    picked = st.radio(
        "View",
        options=list(view_labels.values()),
        index=list(view_labels.values()).index(current_view_label),
        label_visibility="collapsed",
    )
    picked_key = next(k for k, v in view_labels.items() if v == picked)
    if picked_key != st.session_state.view:
        st.session_state.view = picked_key
        st.rerun()

    st.markdown("---")
    st.markdown("### Ticker")
    input_ticker = st.text_input(
        "Any US ticker",
        value=st.session_state.current_ticker,
        key="ticker_input",
        label_visibility="collapsed",
        placeholder="NVDA, META, PLTR...",
    ).strip().upper()
    if input_ticker and input_ticker != st.session_state.current_ticker:
        st.session_state.current_ticker = input_ticker

    st.markdown("---")
    st.markdown("### Watchlist")

    watchlist = st.session_state.store["watchlist"]
    if watchlist:
        st.markdown(
            """<style>
section[data-testid='stSidebar'] [role='radiogroup'] {
    gap: 0px !important;
}
section[data-testid='stSidebar'] [role='radiogroup'] label {
    padding: 9px 12px !important;
    margin: 0 !important;
    font-family: 'Geist Mono', monospace !important;
    font-size: 16px !important;
    border-radius: 3px !important;
}
section[data-testid='stSidebar'] [role='radiogroup'] label p {
    font-size: 16px !important;
    font-weight: 500 !important;
    margin: 0 !important;
}
section[data-testid='stSidebar'] [role='radiogroup'] label:hover {
    background: #EDE8DD !important;
}
/* Hide the radio dot itself so the watchlist reads as a list, not a form */
section[data-testid='stSidebar'] [role='radiogroup'] label > div:has(> div[data-testid="stMarkdownContainer"]) ~ div,
section[data-testid='stSidebar'] [role='radiogroup'] label > div:first-child {
    display: none !important;
}
/* Highlight active selection with a solid background */
section[data-testid='stSidebar'] [role='radiogroup'] label:has(input:checked) {
    background: #0F0E0D !important;
    color: #FBFAF7 !important;
}
section[data-testid='stSidebar'] [role='radiogroup'] label:has(input:checked) p {
    color: #FBFAF7 !important;
}
</style>""",
            unsafe_allow_html=True,
        )

        current = st.session_state.current_ticker
        radio_index = watchlist.index(current) if current in watchlist else 0

        if "_watchlist_radio_last" not in st.session_state:
            st.session_state["_watchlist_radio_last"] = watchlist[radio_index]

        picked = st.radio(
            "Watchlist",
            options=watchlist,
            index=radio_index,
            label_visibility="collapsed",
            key="watchlist_radio",
        )
        if picked != st.session_state["_watchlist_radio_last"]:
            st.session_state["_watchlist_radio_last"] = picked
            st.session_state.current_ticker = picked
            st.session_state.view = "analyze"

        # Remove-ticker — a separate small dropdown at the bottom of the
        # watchlist. Used rarely; doesn't need to live next to each row.
        with st.expander("Remove a ticker", expanded=False):
            to_remove = st.selectbox(
                "Pick one to remove",
                options=["—"] + watchlist,
                index=0,
                label_visibility="collapsed",
                key="remove_picker",
            )
            if to_remove != "—":
                if st.button(f"Remove {to_remove}", use_container_width=True, key="confirm_remove"):
                    st.session_state.store["watchlist"].remove(to_remove)
                    save_store(st.session_state.store)
                    for k in ("watchlist_radio", "_watchlist_radio_last", "remove_picker"):
                        if k in st.session_state:
                            del st.session_state[k]
                    st.rerun()
    else:
        st.caption("Empty — type a ticker above and add it.")

    if st.session_state.current_ticker and st.session_state.current_ticker not in watchlist:
        if st.button(f"+ Add {st.session_state.current_ticker} to watchlist", use_container_width=True):
            st.session_state.store["watchlist"].append(st.session_state.current_ticker)
            save_store(st.session_state.store)
            st.rerun()

    st.markdown("---")
    st.markdown("### Sizing")
    new_account = st.number_input(
        "Account size ($)",
        min_value=1000,
        max_value=100_000_000,
        step=1000,
        value=int(st.session_state.store.get("account_size", 100000)),
    )
    if new_account != st.session_state.store.get("account_size"):
        st.session_state.store["account_size"] = int(new_account)
        save_store(st.session_state.store)

    new_risk = st.number_input(
        "Risk per trade (%)",
        min_value=0.1,
        max_value=5.0,
        step=0.1,
        value=float(st.session_state.store.get("risk_per_trade", 0.01)) * 100,
        help="The dollar amount you're willing to lose if the stop hits. Standard is 1.0%.",
    )
    if abs(new_risk / 100 - st.session_state.store.get("risk_per_trade", 0.01)) > 1e-6:
        st.session_state.store["risk_per_trade"] = new_risk / 100
        save_store(st.session_state.store)

    new_max_pct = st.number_input(
        "Max position size (% of account)",
        min_value=1.0,
        max_value=100.0,
        step=1.0,
        value=float(st.session_state.store.get("max_position_pct", 0.25)) * 100,
        help="Caps the position size in case the risk math produces something too large (happens when stops are tight). Standard is 20-25%.",
    )
    if abs(new_max_pct / 100 - st.session_state.store.get("max_position_pct", 0.25)) > 1e-6:
        st.session_state.store["max_position_pct"] = new_max_pct / 100
        save_store(st.session_state.store)

    st.markdown("---")
    st.markdown("### PM View")

    stored_key = st.session_state.store.get("anthropic_api_key", "")

    if stored_key:
        # Key is already stored — show a masked indicator and a clear button
        masked = stored_key[:7] + "…" + stored_key[-4:] if len(stored_key) > 12 else "saved"
        st.markdown(
            f'<div style="font-size:13px;color:#3F3B34;'
            f'padding:8px 10px;background:#F0FDF4;border:1px solid #BBF7D0;'
            f'border-radius:3px;font-family:Geist Mono,monospace;">'
            f'✓ key saved <span style="color:#8A857C;">({masked})</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
        if st.button("Replace or clear key", key="clear_api_key", use_container_width=True):
            st.session_state.store["anthropic_api_key"] = ""
            save_store(st.session_state.store)
            st.rerun()
        api_key = stored_key
    else:
        new_key = st.text_input(
            "Anthropic API key (optional)",
            type="password",
            help="Paste to generate live PM views with deep-dive content. Saved locally to ~/.desk_store.json so you only paste it once. Cached 7 days per ticker.",
            key="api_key_input",
        )
        if new_key:
            st.session_state.store["anthropic_api_key"] = new_key
            # Clear the current ticker's cached static thesis so the user
            # sees the live Claude-generated version right away. Other
            # tickers keep their cache and upgrade on first view.
            if st.session_state.current_ticker:
                clear_pm_cache(st.session_state.current_ticker)
                clear_dossier_cache(st.session_state.current_ticker)
            save_store(st.session_state.store)
            st.rerun()
        api_key = new_key


# ─────────────────────────────────────────────────────────────────────
# Navbar
# ─────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="desk-bar">
  <span class="wordmark"><span class="arrow">▸</span> Desk</span>
  <span class="meta">{st.session_state.current_ticker}</span>
</div>
""", unsafe_allow_html=True)


view = st.session_state.view


# ─────────────────────────────────────────────────────────────────────
# ANALYZE — decision-first
# ─────────────────────────────────────────────────────────────────────
if view == "analyze":
    ticker = st.session_state.current_ticker
    if not ticker:
        st.info("Type a ticker in the sidebar.")
        st.stop()

    with st.spinner(f"Loading {ticker}…"):
        hist, name = fetch_history(ticker)
        bench = fetch_bench()
        meta = fetch_quote_meta(ticker)

    if hist is None or len(hist) < 50:
        st.error(f"Couldn't find data for **{ticker}**.")
        st.stop()
    if bench is None:
        st.error("Couldn't load SPY benchmark.")
        st.stop()

    t = tactical.compute(hist, bench)
    if t is None:
        st.error(f"Insufficient history for {ticker}.")
        st.stop()

    # Compute decision modifiers — earnings proximity, market regime, RS
    modifiers = tactical.decision_modifiers(t, meta, t.get("market_regime", "unknown"))

    # Fetch PM data here (before splitting into columns) so the dossier on
    # the left can reference the thesis, and the right panel can render
    # the snapshot. Both share the same cached fetch.
    with st.spinner("Loading thesis…"):
        pm = get_cached_pm(ticker, t, api_key=api_key if api_key else None, company_name=name)
        dossier_result = get_cached_dossier(
            ticker, t, modifiers, meta, pm,
            api_key=api_key if api_key else None,
            company_name=name,
        )

    sty = STATE_STYLES[t["action"]]

    col_decision, col_pm = st.columns([5, 3])

    # ───── LEFT COLUMN: decision + trading logic ─────
    with col_decision:
        # Dossier — top of page when API key is configured. Skipped silently
        # when no key (the snapshot below is enough on its own).
        dossier_text = dossier_result.get("dossier") if dossier_result else None
        if dossier_text:
            src = dossier_result.get("_source", "claude")
            st.markdown(f"""
<div class="desk-dossier">
  <div class="desk-dossier-label">
    <span>📋  Decision dossier</span>
    <span class="src">{src}</span>
  </div>
  <div class="desk-dossier-text">{dossier_text}</div>
</div>
""", unsafe_allow_html=True)

        # 0. Compact ticker line with inline meta (sector · mcap · short · earnings)
        chg_color = "#2E7D4F" if t["change"] >= 0 else "#D14545"
        mcap = format_market_cap(meta.get("market_cap"))
        spf = meta.get("short_pct_float")
        earn_banner, earn_footer = format_earnings(meta)
        meta_bits = []
        if meta.get("sector"):
            meta_bits.append(meta["sector"])
        if mcap:
            meta_bits.append(mcap)
        if spf is not None:
            meta_bits.append(f"{spf:.1f}% short")
        dy = meta.get("dividend_yield")
        if dy is not None and dy > 0.05:
            meta_bits.append(f"{dy:.2f}% yield")
        if earn_footer and not earn_banner:
            meta_bits.append(f"Earnings {earn_footer}")
        meta_line = " · ".join(meta_bits)

        st.markdown(f"""
<div class="desk-ticker-row">
  <div>
    <div>
      <span class="sym">{ticker}</span>
      <span class="name">{name}</span>
    </div>
    <div class="meta-inline">{meta_line}</div>
  </div>
  <div>
    <div style="text-align:right;">
      <span class="price">${t['price']:.2f}</span>
      <span class="chg" style="color:{chg_color};">
        {'+' if t['change'] >= 0 else ''}{t['change']:.2f}%
      </span>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

        # 1. DECISION — hero
        st.markdown(f"""
<div class="desk-decision">
  <span class="word" style="color:{sty['color']};">
    {sty['label']}<span style="color:#0F0E0D;">.</span>
  </span>
  <span class="emoji">{sty['emoji']}</span>
  <div class="context">{decision_context(t)}</div>
</div>
""", unsafe_allow_html=True)

        # 1b. Decision modifiers — badges that nudge conviction up or down
        # on top of the same nominal decision (earnings proximity, market
        # regime, leadership/lag versus the index). Replaces the old
        # earnings-only banner.
        if modifiers:
            mod_icons = {"earnings": "📅", "regime": "🌐", "rs": "📊"}
            badges_html = "".join(
                f'<div class="desk-mod desk-mod-{m["severity"]}">'
                f'<span class="icon">{mod_icons.get(m["kind"], "•")}</span>'
                f'<span>{m["text"]}</span>'
                f'</div>'
                for m in modifiers
            )
            st.markdown(
                f'<div class="desk-modifiers">{badges_html}</div>',
                unsafe_allow_html=True,
            )

        # 2-3. For Watch/Enter, render Trigger + Invalidation.
        # For Avoid, render "Why avoid" + "Reconsider when" instead — those
        # answer the actually-relevant question for an Avoid decision.
        if t["action"] != "avoid":
            trig_line = trigger_text(t)
            st.markdown(f"""
<div class="desk-trigger-block">
  <div class="desk-trigger-label"><span class="em">⚡</span>Trigger</div>
  <div class="desk-trigger-text">{bold_numbers(trig_line)}</div>
</div>
""", unsafe_allow_html=True)

            inv = invalidation_text(t)
            if inv:
                st.markdown(f"""
<div class="desk-invalidation">
  <div class="label"><span class="em">⛔</span>Invalidation</div>
  <div class="text">{bold_numbers(inv)}</div>
</div>
""", unsafe_allow_html=True)
        else:
            # Avoid layout — read of the tape + concrete reasons + reversal conditions
            tape_rows = tape_read(t)
            reasons = why_avoid_reasons(t)
            reversals = reconsider_when(t)

            # Tape read first — concrete current-state numbers immediately
            # below the decision word so the user sees real data, not space.
            color_map = {"pos": "#2E7D4F", "neg": "#D14545", "": "#3F3B34"}
            tape_html = "".join(
                f'<div class="row">'
                f'  <span class="k">{label}</span>'
                f'  <span class="v" style="color:{color_map.get(sev, "#3F3B34")};">{bold_numbers(value)}</span>'
                f'</div>'
                for label, value, sev in tape_rows
            )
            st.markdown(f"""
<div class="desk-tape-read">
  <div class="label"><span class="em">📊</span>Read of the tape</div>
  {tape_html}
</div>
""", unsafe_allow_html=True)

            reasons_html = "".join(
                f'<li>{bold_numbers(r)}</li>' for r in reasons
            )
            st.markdown(f"""
<div class="desk-avoid-reasons">
  <div class="label"><span class="em">⛔</span>Why avoid</div>
  <ul>{reasons_html}</ul>
</div>
""", unsafe_allow_html=True)

            reversal_html = "".join(
                f'<li>{bold_numbers(c)}</li>' for c in reversals
            )
            st.markdown(f"""
<div class="desk-reconsider">
  <div class="label"><span class="em">🔄</span>Reconsider when</div>
  <ul>{reversal_html}</ul>
</div>
""", unsafe_allow_html=True)

        # 4. IF TRIGGER HITS — conditional trade plan
        if t["action"] != "avoid":
            when_label = "If trigger hits" if t["action"] == "watch" else "Entering now"
            st.markdown(f"""
<div class="desk-plan-label">
  <span class="em">📊</span>{when_label} · Trade plan
</div>
""", unsafe_allow_html=True)

            def plan_row(label, value, delta=None, delta_color=None, note=None):
                delta_html = ""
                if delta is not None:
                    delta_html = f'<span class="d" style="color:{delta_color};">{"+" if delta >= 0 else ""}{delta:.1f}%</span>'
                note_html = ""
                if note:
                    note_html = f'<div style="font-family:\'Geist Mono\',monospace;font-size:11px;color:#A8A29E;margin-top:2px;">{note}</div>'
                st.markdown(f"""
<div class="desk-plan-row">
  <span class="k">{label}</span>
  <span style="text-align:right;">
    <div><span class="v">${value:.2f}</span>{delta_html}</div>
    {note_html}
  </span>
</div>
""", unsafe_allow_html=True)

            # ATR in $ terms for distance calculations
            atr_dollars = t["atr_pct"] * t["entry"]
            stop_atrs = abs(t["entry"] - t["stop"]) / atr_dollars if atr_dollars > 0 else 0
            t1_atrs = abs(t["t1"] - t["entry"]) / atr_dollars if atr_dollars > 0 else 0
            risk_per_share = t["entry"] - t["stop"]
            reward_per_share = t["t1"] - t["entry"]
            rr_ratio = reward_per_share / risk_per_share if risk_per_share > 0 else 0

            plan_row("Entry", t["entry"])
            plan_row(
                "Stop",
                t["stop"],
                (t["stop"]/t["entry"] - 1)*100,
                "#D14545",
                note=f"{stop_atrs:.1f}× ATR away" if stop_atrs > 0 else None,
            )
            plan_row(
                "Target 1",
                t["t1"],
                (t["t1"]/t["entry"] - 1)*100,
                "#2E7D4F",
                note=f"{t1_atrs:.1f}× ATR away · reward/risk {rr_ratio:.2f}:1" if t1_atrs > 0 else None,
            )
            plan_row("Target 2", t["t2"], (t["t2"]/t["entry"] - 1)*100, "#2E7D4F")

            # Position sizing — risk-based shares vs max-position cap, take min.
            # Label is rewritten to show four numbers in a clear order:
            #   risk $X (Y% of account) · N shares · position $Z (W% of account)
            # The "binding constraint" gets a short note when the cap is active.
            account = st.session_state.store.get("account_size", 100000)
            risk_pct = st.session_state.store.get("risk_per_trade", 0.01)
            max_pos_pct = st.session_state.store.get("max_position_pct", 0.25)

            risk_dollars = account * risk_pct
            per_share_risk = t["entry"] - t["stop"]

            if per_share_risk > 0 and t["entry"] > 0:
                # 1. Shares the risk math says
                risk_shares = int(risk_dollars // per_share_risk)
                # 2. Shares the position cap allows
                cap_dollars = account * max_pos_pct
                cap_shares = int(cap_dollars // t["entry"])
                # 3. Use the smaller — risk math AND the cap must both be satisfied
                shares = min(risk_shares, cap_shares)
                cap_active = cap_shares < risk_shares

                position_value = shares * t["entry"]
                pos_pct = (position_value / account) * 100 if account > 0 else 0
                # Effective risk after the cap kicks in
                effective_risk = shares * per_share_risk
                effective_risk_pct = (effective_risk / account) * 100 if account > 0 else 0

                cap_note = ""
                if cap_active:
                    cap_note = (
                        f'<div style="font-family:\'Geist\',sans-serif;font-style:italic;'
                        f'font-size:13px;color:#8B6914;margin-top:6px;">'
                        f'Capped by max position size ({max_pos_pct*100:.0f}%). '
                        f'Risk math alone wanted {risk_shares:,} shares.'
                        f'</div>'
                    )

                st.markdown(f"""
<div style="margin-top:14px;padding:12px 14px;background:#F5F2EB;border-radius:3px;
            font-family:'Geist Mono',monospace;font-size:14px;color:#3F3B34;line-height:1.55;">
  <div>
    <span style="color:#8A857C;">Risk</span>
    <b style="color:#0F0E0D;">${effective_risk:,.0f}</b>
    <span style="color:#A8A29E;">({effective_risk_pct:.2f}% of ${account:,.0f})</span>
  </div>
  <div>
    <span style="color:#8A857C;">Shares</span>
    <b style="color:#0F0E0D;">{shares:,}</b>
    <span style="color:#A8A29E;">at ${t['entry']:.2f} entry</span>
  </div>
  <div>
    <span style="color:#8A857C;">Position</span>
    <b style="color:#0F0E0D;">${position_value:,.0f}</b>
    <span style="color:#A8A29E;">({pos_pct:.1f}% of account)</span>
  </div>
  {cap_note}
</div>
""", unsafe_allow_html=True)

        # 4. Chart — full width of the left column, tighter height
        st.markdown(f"""
<div class="desk-chart-label">📈 Chart · 9 · 50 · 100 · 200 day moving averages</div>
""", unsafe_allow_html=True)
        st.components.v1.html(f"""
<div class="tradingview-widget-container" style="height:360px;width:100%;">
  <div id="tv_chart_{ticker}" style="height:100%;width:100%;"></div>
  <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
  <script type="text/javascript">
    new TradingView.widget({{
      "autosize": true,
      "symbol": "{ticker}",
      "interval": "D",
      "timezone": "America/New_York",
      "theme": "light",
      "style": "1",
      "locale": "en",
      "toolbar_bg": "#FBFAF7",
      "enable_publishing": false,
      "allow_symbol_change": false,
      "hide_top_toolbar": false,
      "hide_legend": false,
      "save_image": false,
      "studies": [
        {{"id": "MASimple@tv-basicstudies", "inputs": {{"length": 9}}}},
        {{"id": "MASimple@tv-basicstudies", "inputs": {{"length": 50}}}},
        {{"id": "MASimple@tv-basicstudies", "inputs": {{"length": 100}}}},
        {{"id": "MASimple@tv-basicstudies", "inputs": {{"length": 200}}}}
      ],
      "container_id": "tv_chart_{ticker}"
    }});
  </script>
</div>
""", height=380)

        # 5. Log button
        st.markdown("<div style='margin-top:22px;'></div>", unsafe_allow_html=True)
        if st.button(f"Log this {sty['label'].lower()} decision"):
            st.session_state.store["log"].insert(0, {
                "date": datetime.now().strftime("%m/%d"),
                "ticker": ticker,
                "action": t["action"],
                "result": "open",
                "closed": False,
                "entry": round(t["entry"], 2),
            })
            save_store(st.session_state.store)
            st.success(f"Logged {ticker} as {sty['label'].lower()}.")

        # 6. Technical details — footer, collapsed
        with st.expander("Technical details"):
            rows = [
                ("Bias", f"{t['bias'].capitalize() if t['bias'] else '—'} ({t['bias_score']:+d} on ±10 scale)"),
                ("Technical score", f"{t['setup_score']:.1f} / 10"),
                ("50-day moving average", f"${t['ma50']:.2f}"),
                ("200-day moving average", f"${t['ma200']:.2f}"),
                ("Average true range", f"{t['atr_pct']*100:.2f}%" + (" · below 1.5% gate" if not t['atr_ok'] else "")),
                ("Relative strength vs S&P 500", f"{t['rs']:.3f} · 10d {'+' if t['rs_delta'] >= 0 else ''}{t['rs_delta']:.3f}"),
                ("52-week high", f"${t['high_52w']:.2f}"),
                ("20-day average volume", f"{t['avg_vol_20d']:,.0f}"),
                ("Today volume / average", f"{t['vol_ratio']:.2f}×"),
                ("Structure quality", f"{t['structure_quality']:.1f} / 10"),
            ]
            for label, value in rows:
                st.markdown(f"""
<div style="display:flex;justify-content:space-between;font-family:'Geist Mono',monospace;font-size:11px;color:#6B655B;padding:3px 0;">
  <span>{label}</span><span style="color:#0F0E0D;">{value}</span>
</div>
""", unsafe_allow_html=True)

    # ───── RIGHT COLUMN: PM view (two layers) ─────
    with col_pm:
        # PM data was fetched above (before column split) — use it directly.
        src_note = pm.get("_source", "the thesis")

        head_col, refresh_col = st.columns([5, 1])
        with head_col:
            st.markdown(f"""
<div class="desk-pm-header">
  <span><span class="em">🧠</span>Portfolio manager</span>
  <span class="src">{src_note}</span>
</div>
""", unsafe_allow_html=True)
        with refresh_col:
            if st.button("↻", help="Regenerate thesis."):
                clear_pm_cache(ticker)
                st.rerun()

        # Layer 1 — snapshot
        st.markdown(f"""
<div class="desk-pm-container">
  <div class="desk-pm-block">
    <div class="lb">Thesis</div>
    <div class="body">{pm.get('thesis', '')}</div>
  </div>
  <div class="desk-pm-block">
    <div class="lb">Drivers</div>
    {''.join(f'<div class="desk-pm-item">{d}</div>' for d in pm.get('drivers', []))}
  </div>
  <div class="desk-pm-block">
    <div class="lb">Risks</div>
    {''.join(f'<div class="desk-pm-item">{r}</div>' for r in pm.get('risks', []))}
  </div>
  <div class="desk-pm-block">
    <div class="lb">Valuation</div>
    <div class="body">{pm.get('valuation', '')}</div>
  </div>
</div>
""", unsafe_allow_html=True)

        # ── Earnings card (if a date is known) ──
        if meta.get("earnings_date"):
            eps = meta.get("expected_eps")
            days = meta.get("earnings_days")
            date_str = meta["earnings_date"].strftime("%b %d")
            days_str = ""
            if days is not None and days >= 0:
                days_str = f"Today" if days == 0 else f"In {days} day{'s' if days != 1 else ''}"
            eps_str = f"Expected EPS ${eps:.2f}" if eps else "EPS estimate not available"
            st.markdown(f"""
<div class="desk-stat-card">
  <div class="label">📅 Next earnings</div>
  <div class="row"><span>{date_str}{' · ' + days_str if days_str else ''}</span><span class="v">{eps_str}</span></div>
</div>
""", unsafe_allow_html=True)

        # ── Analyst consensus (if available) ──
        rec_str = format_recommendation(meta.get("analyst_rec"), meta.get("analyst_n"))
        target = meta.get("analyst_target")
        if rec_str or target:
            target_html = ""
            if target:
                pct = (target / t["price"] - 1) * 100
                color = "#2E7D4F" if pct >= 0 else "#D14545"
                target_html = f'<span class="v">${target:.2f} <span style="color:{color};">({"+" if pct >= 0 else ""}{pct:.1f}%)</span></span>'
            st.markdown(f"""
<div class="desk-stat-card">
  <div class="label">🧑‍💼 Analyst consensus</div>
  <div class="row">
    <span>{rec_str or "—"}</span>
    {target_html}
  </div>
</div>
""", unsafe_allow_html=True)

        # ── Lynch Check ──
        cat_key, cat_label, cat_hint = classify_lynch(meta)
        peg = meta.get("peg")
        fpe = meta.get("forward_pe")
        de = meta.get("debt_to_equity")
        eg = meta.get("earnings_growth")

        def pass_fail(is_pass, na=False):
            if na:
                return '<span style="color:#B4ADA0;">—</span>'
            return '<span style="color:#2E7D4F;">✓</span>' if is_pass else '<span style="color:#D14545;">✗</span>'

        peg_verdict = ""
        peg_row = ""
        if peg is not None:
            if peg < 1.0:
                peg_verdict = "cheap"
                peg_pass = True
            elif peg < 2.0:
                peg_verdict = "fair"
                peg_pass = True
            else:
                peg_verdict = "expensive"
                peg_pass = False
            peg_row = f'<div class="row"><span>PEG</span><span class="v">{peg:.2f} · {peg_verdict} {pass_fail(peg_pass)}</span></div>'

        pe_row = ""
        if fpe is not None:
            pe_context = ""
            pe_pass = True
            if cat_key == "fast_grower":
                pe_context = "reasonable for a fast grower" if fpe < 40 else "rich for a fast grower"
                pe_pass = fpe < 40
            elif cat_key == "stalwart":
                pe_context = "fair for a stalwart" if 15 <= fpe <= 25 else ("cheap" if fpe < 15 else "expensive for a stalwart")
                pe_pass = 15 <= fpe <= 25 or fpe < 15
            elif cat_key == "cyclical":
                pe_context = "low P/E on cyclicals usually signals a peak — be careful" if fpe < 10 else "fair mid-cycle"
                pe_pass = fpe > 10
            elif cat_key == "slow_grower":
                pe_context = "typical for a slow grower" if fpe < 20 else "expensive for a slow grower"
                pe_pass = fpe < 20
            else:
                pe_context = cat_hint
                pe_pass = True
            pe_row = f'<div class="row"><span>Forward P/E</span><span class="v">{fpe:.1f} · {pe_context} {pass_fail(pe_pass)}</span></div>'

        de_row = ""
        if de is not None:
            de_pass = de < 30
            de_note = "healthy" if de_pass else "leveraged"
            de_row = f'<div class="row"><span>Debt / Equity</span><span class="v">{de:.0f}% · {de_note} {pass_fail(de_pass)}</span></div>'

        growth_row = ""
        if eg is not None:
            growth_row = f'<div class="row"><span>Earnings growth</span><span class="v">{eg:+.1f}% YoY</span></div>'

        if any([peg_row, pe_row, de_row, growth_row]):
            st.markdown(f"""
<div class="desk-stat-card">
  <div class="label">📚 Lynch check</div>
  <div class="row"><span>Category</span><span class="v">{cat_label}</span></div>
  {growth_row}
  {peg_row}
  {pe_row}
  {de_row}
</div>
""", unsafe_allow_html=True)

        # Layer 2 — deep dive (expandable)
        deep = pm.get("deep_dive") or {}
        has_deep = deep.get("expanded_thesis") and pm.get("_source", "").startswith("claude") or deep.get("catalysts")

        # Track expansion state per ticker
        ticker_key = ticker.upper()
        expanded = st.session_state.pm_expanded.get(ticker_key, False)

        btn_label = "Collapse analysis ↑" if expanded else "View full thesis →"
        # Use a column-based indent. Streamlit re-renders buttons inside
        # their own container; CSS padding wrappers around the button get
        # collapsed. A spacer column is the only reliable way to push the
        # button right to match the .desk-pm-container 24px left padding.
        spacer, btn_col = st.columns([1, 12])
        with btn_col:
            if st.button(btn_label, key=f"pm_expand_{ticker_key}"):
                st.session_state.pm_expanded[ticker_key] = not expanded
                st.rerun()

        if expanded:
            if not has_deep or not deep.get("expanded_thesis"):
                st.markdown(f"""
<div class="desk-pm-deep">
  <div class="sub-body" style="color:#8A857C;">
    Deep-dive content is only generated when an Anthropic API key is configured in the sidebar.
    Paste a key, then click ↻ next to the Portfolio manager header to regenerate.
  </div>
</div>
""", unsafe_allow_html=True)
            else:
                # Expanded thesis
                html_parts = ['<div class="desk-pm-deep">']
                html_parts.append(f'<div class="sub-lb">Expanded thesis</div>')
                html_parts.append(f'<div class="sub-body">{deep.get("expanded_thesis", "")}</div>')

                # Business
                if deep.get("business"):
                    html_parts.append(f'<div class="sub-lb">Business</div>')
                    html_parts.append(f'<div class="sub-body">{deep["business"]}</div>')

                # Variant perception — bull vs bear, side by side
                if deep.get("variant_bull") or deep.get("variant_bear"):
                    html_parts.append(f'<div class="sub-lb">Variant perception</div>')
                    html_parts.append('<div class="variant-grid">')
                    if deep.get("variant_bull"):
                        html_parts.append(f'''
<div class="variant-card">
  <div class="lb lb-bull">Bull case</div>
  <div class="body">{deep["variant_bull"]}</div>
</div>''')
                    if deep.get("variant_bear"):
                        html_parts.append(f'''
<div class="variant-card">
  <div class="lb lb-bear">Bear case</div>
  <div class="body">{deep["variant_bear"]}</div>
</div>''')
                    html_parts.append('</div>')
                    if deep.get("variant_needs"):
                        html_parts.append(f'<div class="sub-body" style="margin-top:8px;"><em>What needs to happen:</em> {deep["variant_needs"]}</div>')

                # Catalysts
                if deep.get("catalysts"):
                    html_parts.append(f'<div class="sub-lb">Catalysts · next 1–2 quarters</div>')
                    html_parts.extend(f'<div class="desk-pm-item">{c}</div>' for c in deep["catalysts"])

                # Risk scenarios
                if deep.get("risk_scenarios"):
                    html_parts.append(f'<div class="sub-lb">Specific risk scenarios</div>')
                    html_parts.extend(f'<div class="desk-pm-item">{r}</div>' for r in deep["risk_scenarios"])

                # Valuation context
                if deep.get("valuation_context"):
                    html_parts.append(f'<div class="sub-lb">Valuation context</div>')
                    html_parts.append(f'<div class="sub-body">{deep["valuation_context"]}</div>')

                # What must be true
                if deep.get("must_be_true"):
                    html_parts.append(f'<div class="sub-lb">What must be true</div>')
                    html_parts.extend(f'<div class="desk-pm-item">{m}</div>' for m in deep["must_be_true"])

                # What would change my mind
                if deep.get("would_change_mind"):
                    html_parts.append(f'<div class="sub-lb">What would change my mind</div>')
                    html_parts.extend(f'<div class="desk-pm-item">{m}</div>' for m in deep["would_change_mind"])

                html_parts.append('</div>')
                st.markdown("".join(html_parts), unsafe_allow_html=True)

        # Technical read — same card treatment as the other PM stat cards
        commentary_lines = technical_commentary(t)
        if commentary_lines:
            paras = "".join(f"<p>{bold_numbers(line)}</p>" for line in commentary_lines)
            st.markdown(f"""
<div class="desk-stat-card desk-stat-card-read">
  <div class="label">🔎 Technical read</div>
  <div class="read-body">{paras}</div>
</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────
# WATCHLIST
# ─────────────────────────────────────────────────────────────────────
if view == "watchlist":
    if not st.session_state.store["watchlist"]:
        st.info("Your watchlist is empty. Type a ticker in the sidebar and add it.")
    else:
        bench = fetch_bench()
        rows_by_action = {"enter_now": [], "watch": [], "avoid": []}

        with st.spinner("Analyzing watchlist…"):
            for tkr in st.session_state.store["watchlist"]:
                hist, name = fetch_history(tkr)
                if hist is None or bench is None:
                    continue
                t = tactical.compute(hist, bench)
                if t is None:
                    continue
                rows_by_action[t["action"]].append((tkr, name, t))

        for key in ["enter_now", "watch", "avoid"]:
            sty = STATE_STYLES[key]
            st.markdown(f"""
<div style="font-size:11px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:{sty['color']};margin:14px 0 8px;">
  {sty['emoji']} {sty['label']} · {len(rows_by_action[key])}
</div>
""", unsafe_allow_html=True)
            if not rows_by_action[key]:
                st.markdown('<div style="color:#B4ADA0;font-style:italic;font-size:13px;margin:6px 0 18px;">Nothing here.</div>', unsafe_allow_html=True)
                continue
            for tkr, name, t in rows_by_action[key]:
                chg_color = "#2E7D4F" if t["change"] >= 0 else "#D14545"
                c1, c2 = st.columns([10, 1])
                with c1:
                    st.markdown(f"""
<div style="border-top:1px dashed #E5E3DE;display:flex;justify-content:space-between;align-items:baseline;padding:9px 0;">
  <div>
    <span style="font-size:14px;font-weight:600;">{tkr}</span>
    <span style="font-size:12px;color:#6B655B;margin-left:10px;">{name}</span>
  </div>
  <div style="display:flex;align-items:baseline;gap:14px;">
    <span style="font-family:'Geist Mono',monospace;font-size:11px;color:{chg_color};min-width:50px;text-align:right;">
      {'+' if t['change'] >= 0 else ''}{t['change']:.1f}%
    </span>
  </div>
</div>
""", unsafe_allow_html=True)
                with c2:
                    if st.button("Open", key=f"go_{tkr}_{key}"):
                        st.session_state.current_ticker = tkr
                        st.session_state.view = "analyze"
                        st.session_state.nav_counter += 1
                        st.rerun()


# ─────────────────────────────────────────────────────────────────────
# TRACKER
# ─────────────────────────────────────────────────────────────────────
if view == "tracker":
    log = st.session_state.store["log"]
    closed = [l for l in log if l.get("closed")]

    if closed:
        wins = [l for l in closed if (l.get("correct") if l["action"] == "avoid" else l.get("result_pct", 0) > 0)]
        hit_rate = round(100 * len(wins) / len(closed))
        entry_trades = [l for l in closed if l["action"] != "avoid"]
        avg_edge = (sum(l.get("result_pct", 0) for l in entry_trades) / len(entry_trades)) if entry_trades else 0
    else:
        hit_rate = 0
        avg_edge = 0

    c1, c2 = st.columns(2)
    c1.markdown(f"""
<div style="border:1px solid #E5E3DE;border-radius:4px;padding:12px 14px;">
  <div style="font-size:10px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:#6B655B;">Hit rate</div>
  <div style="font-family:'Geist Mono',monospace;font-size:24px;font-weight:500;margin-top:3px;letter-spacing:-0.02em;">{hit_rate}%</div>
</div>
""", unsafe_allow_html=True)
    c2.markdown(f"""
<div style="border:1px solid #E5E3DE;border-radius:4px;padding:12px 14px;">
  <div style="font-size:10px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:#6B655B;">Average edge</div>
  <div style="font-family:'Geist Mono',monospace;font-size:24px;font-weight:500;margin-top:3px;letter-spacing:-0.02em;">{'+' if avg_edge >= 0 else ''}{avg_edge:.1f}%</div>
</div>
""", unsafe_allow_html=True)

    st.markdown("&nbsp;", unsafe_allow_html=True)
    st.markdown('<div style="font-size:10px;font-weight:600;letter-spacing:0.14em;text-transform:uppercase;color:#6B655B;margin:6px 0;">Log</div>', unsafe_allow_html=True)

    if not log:
        st.markdown('<div style="color:#B4ADA0;font-style:italic;font-size:13px;">No decisions logged yet.</div>', unsafe_allow_html=True)
    else:
        for i, entry in enumerate(log):
            sty = STATE_STYLES[entry["action"]]
            result_color = "#B4ADA0"
            if entry.get("closed"):
                if entry["action"] == "avoid":
                    result_color = "#2E7D4F" if entry.get("correct") else "#D14545"
                else:
                    result_color = "#2E7D4F" if entry.get("result_pct", 0) > 0 else "#D14545"

            result_text = entry.get("result", "open")
            if entry.get("closed") and "result_pct" in entry and entry["action"] != "avoid":
                result_text = f"{'+' if entry['result_pct'] >= 0 else ''}{entry['result_pct']:.1f}%"

            cols = st.columns([1, 1, 2, 2, 1])
            cols[0].markdown(f'<div style="font-family:\'Geist Mono\',monospace;font-size:11px;color:#B4ADA0;padding-top:9px;">{entry["date"]}</div>', unsafe_allow_html=True)
            cols[1].markdown(f'<div style="font-size:13px;font-weight:600;padding-top:9px;">{entry["ticker"]}</div>', unsafe_allow_html=True)
            cols[2].markdown(f'<div style="font-family:\'Geist Mono\',monospace;font-size:11px;color:{sty["color"]};letter-spacing:0.06em;text-transform:uppercase;padding-top:9px;">{sty["emoji"]} {sty["label"]}</div>', unsafe_allow_html=True)
            cols[3].markdown(f'<div style="font-family:\'Geist Mono\',monospace;font-size:12px;color:{result_color};padding-top:9px;">{result_text}</div>', unsafe_allow_html=True)
            if not entry.get("closed"):
                if cols[4].button("Close", key=f"close_{i}"):
                    cur_hist, _ = fetch_history(entry["ticker"])
                    if cur_hist is not None and "entry" in entry:
                        cur_price = float(cur_hist["Close"].iloc[-1])
                        pct = (cur_price / entry["entry"] - 1) * 100
                        entry["result_pct"] = pct
                        entry["closed"] = True
                        save_store(st.session_state.store)
                        st.rerun()
            else:
                if cols[4].button("✕", key=f"del_{i}"):
                    st.session_state.store["log"].pop(i)
                    save_store(st.session_state.store)
                    st.rerun()
            st.markdown('<div style="border-top:1px dashed #E5E3DE;"></div>', unsafe_allow_html=True)
