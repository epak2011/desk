"""Desk — decision-first tactical interface with two-layer PM view.

Hierarchy (strict, top-down):
  1. DECISION          — ENTER 🚀 / WATCH 👀 / AVOID ⛔
  2. TRIGGER ⚡         — single price-based condition
  3. INVALIDATION ⛔    — binary, directly under trigger
  4. IF TRIGGER HITS 📊 — trade structure, conditional
  5. PM VIEW 🧠
       Layer 1: scan — thesis / drivers / risks / valuation
       Layer 2: full thesis expansion (expandable)
"""

import json
import html
import math
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

import streamlit as st
import yfinance as yf

import tactical
from pm_view import CLAUDE_MODEL, get_pm_view, get_decision_dossier, STATIC_SNAPSHOTS, RESEARCH_CONTEXT_TICKERS

try:
    from pm_view import _messages_create as anthropic_messages_create
except ImportError:
    def anthropic_messages_create(client, **kwargs):
        return client.messages.create(model=CLAUDE_MODEL, **kwargs)


st.set_page_config(
    page_title="Trading Desk",
    page_icon="▸",
    layout="wide",
    initial_sidebar_state="auto",
)

# Persistent store lives in the user's home folder, NOT in the app folder.
# That way, replacing the desk-local folder during upgrades doesn't wipe
# the user's watchlist, decisions, account size, or PM cache.
STORE_PATH = Path.home() / ".desk_store.json"
LEGACY_STORE_PATH = Path(__file__).parent / "desk_store.json"
BENCH_CACHE_PATH = Path.home() / ".desk_spy_benchmark_cache.json"
HISTORY_CACHE_DIR = Path.home() / ".desk_history_cache"
MARKET_TZ = ZoneInfo("America/New_York") if ZoneInfo else timezone(timedelta(hours=-4), "ET")
REGIME_DAILY_REFRESH_HOUR = 9
REGIME_DAILY_REFRESH_MINUTE = 10
REGIME_DAILY_MEMO_SCHEMA_VERSION = 3
REGIME_CLAUDE_TIMEOUT_SECONDS = max(30, int(os.environ.get("REGIME_CLAUDE_TIMEOUT_SECONDS", "55")))


def now_market_time():
    return datetime.now(MARKET_TZ)


def regime_daily_anchor(now=None):
    now = now or now_market_time()
    anchor = now.replace(
        hour=REGIME_DAILY_REFRESH_HOUR,
        minute=REGIME_DAILY_REFRESH_MINUTE,
        second=0,
        microsecond=0,
    )
    if now < anchor:
        anchor -= timedelta(days=1)
    return anchor


def regime_daily_key(now=None):
    return regime_daily_anchor(now).strftime("%Y-%m-%d")


def format_market_time(value, fmt="%b %d · %-I:%M %p %Z"):
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MARKET_TZ).strftime(fmt)
    except Exception:
        return "cached"

# One-time migration: if the legacy in-folder store exists and the new
# home-folder store doesn't, move the legacy file forward.
if LEGACY_STORE_PATH.exists() and not STORE_PATH.exists():
    try:
        STORE_PATH.write_text(LEGACY_STORE_PATH.read_text())
    except Exception:
        pass
PM_CACHE_TTL_DAYS = 7
DOSSIER_SCHEMA_VERSION = 8
SPECIAL_CONTEXT_REFRESH_TICKERS = RESEARCH_CONTEXT_TICKERS

# Display-only fallbacks for common watchlist names when Yahoo omits profile
# metadata during rate-limit windows. Live quote/math still comes from data.
FALLBACK_PROFILE_META = {
    "ASTS": {"name": "AST SpaceMobile", "sector": "Communication Services", "industry": "Telecom services"},
    "SATS": {"name": "EchoStar Corporation", "sector": "Communication Services", "industry": "Telecom services"},
    "NVDA": {"name": "NVIDIA Corporation", "sector": "Technology", "industry": "Semiconductors"},
    "AVGO": {"name": "Broadcom Inc.", "sector": "Technology", "industry": "Semiconductors"},
    "PLTR": {"name": "Palantir Technologies", "sector": "Technology", "industry": "Software"},
    "DASH": {"name": "DoorDash", "sector": "Consumer Cyclical", "industry": "Internet content & information"},
    "COIN": {"name": "Coinbase Global", "sector": "Financial Services", "industry": "Capital markets"},
    "RKLB": {"name": "Rocket Lab", "sector": "Industrials", "industry": "Aerospace & defense"},
    "VRT": {"name": "Vertiv", "sector": "Industrials", "industry": "Electrical equipment"},
    "ICOP": {"name": "iShares Copper and Metals Mining ETF", "sector": "ETF", "category": "Copper and metals mining"},
    "BTC-USD": {"name": "Bitcoin", "sector": "Crypto", "industry": "Digital asset"},
    "CQQQ": {
        "name": "Invesco China Technology ETF",
        "sector": "ETF",
        "category": "China technology",
        "total_assets": 2_700_000_000,
    },
    "EWY": {
        "name": "iShares MSCI South Korea ETF",
        "sector": "ETF",
        "category": "South Korea equities",
        "total_assets": 15_736_000_000,
        "expense_ratio": 0.0059,
    },
}


def infer_security_profile(ticker, meta=None, company_name=None):
    """Fill display-only identity gaps when Yahoo returns sparse ETF/fund data."""
    ticker = (ticker or "").upper().strip()
    meta = meta or {}
    profile = dict(FALLBACK_PROFILE_META.get(ticker, {}))

    name = (
        company_name
        or meta.get("long_name")
        or meta.get("short_name")
        or profile.get("name")
        or ticker
    )
    quote_type = str(meta.get("quote_type") or "").upper()
    name_upper = str(name).upper()
    has_fund_fields = bool(
        meta.get("category") or meta.get("fund_family") or
        meta.get("total_assets") or meta.get("net_assets") or
        meta.get("expense_ratio")
    )
    looks_like_etf = (
        quote_type == "ETF" or
        profile.get("sector") == "ETF" or
        " ETF" in f" {name_upper}" or
        name_upper.endswith("ETF")
    )
    looks_like_fund = (
        quote_type in {"ETF", "MUTUALFUND", "FUND"} or
        looks_like_etf or
        has_fund_fields or
        any(token in name_upper for token in (" INDEX FUND", " TRUST", " FUND"))
    )

    if looks_like_fund:
        profile.setdefault("sector", "ETF" if looks_like_etf else "Fund")
        profile.setdefault("name", name)
        if not profile.get("category"):
            category = _infer_fund_category_from_name(name, looks_like_etf)
            if category:
                profile["category"] = category
    return profile


def _infer_fund_category_from_name(name, is_etf=True):
    """Turn sparse fund names into concise header categories."""
    if not name:
        return None
    text = str(name)
    remove_terms = [
        "iShares", "Vanguard", "Invesco", "SPDR", "Global X", "ARK", "VanEck",
        "WisdomTree", "ProShares", "Direxion", "First Trust", "Schwab",
        "ETF", "Trust", "Fund", "Index", "Shares",
    ]
    for term in remove_terms:
        text = text.replace(term, " ")
    words = [w.strip(" -–—,") for w in text.split() if w.strip(" -–—,")]
    if not words:
        return "ETF" if is_etf else "Fund"
    category = " ".join(words[:5])
    lower = category.lower()
    if is_etf and not any(token in lower for token in ("bond", "treasury", "income", "bitcoin", "ether", "gold", "cash")):
        category = f"{category} equities"
    return category


def normalize_percent_value(value):
    """Normalize Yahoo decimal/percent fields to display percent units."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if abs(v) <= 1.5:
        v *= 100
    return v

# ─────────────────────────────────────────────────────────────────────
# STORAGE LAYER — Postgres when DATABASE_URL is set (hosted), local
# JSON file otherwise (development on your Mac). Same code, same shape.
# ─────────────────────────────────────────────────────────────────────
import os

def _get_database_url():
    """Read DATABASE_URL from env first, then Streamlit secrets if available.
    Also strips literal [brackets] that Supabase sometimes wraps around the
    password and host in the connection string."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        try:
            url = st.secrets.get("DATABASE_URL", "").strip()
        except Exception:
            return ""
    # Strip literal [ ] brackets that appear in some Supabase connection strings
    # e.g. postgresql://user:[password]@[host]:port/db
    import re as _re
    url = _re.sub(r'(?<=:)\[([^\]@]*)\](?=@)', r'\1', url)    # [password]
    url = _re.sub(r'(?<=@)\[([^\]/:]*)\]', r'\1', url)         # @[host]
    return url

USE_POSTGRES = bool(_get_database_url())


@st.cache_resource(show_spinner=False)
def _pg_pool():
    """Reuse Supabase connections instead of opening TCP/TLS on every click."""
    from psycopg_pool import ConnectionPool

    return ConnectionPool(
        _get_database_url(),
        min_size=1,
        max_size=5,
        kwargs={"autocommit": True},
    )


def _pg_connect():
    """Return a pooled Postgres connection when hosted."""
    return _pg_pool().connection()


def _pg_init():
    """Ensure persistence tables exist.

    The app still uses one in-memory `store`, but high-growth sections live in
    their own rows so normal clicks do not rewrite one ever-growing JSON blob.
    """
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY,
                    value JSONB NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_history (
                    ticker TEXT PRIMARY KEY,
                    messages JSONB NOT NULL DEFAULT '[]'::jsonb,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS decisions_log (
                    id TEXT PRIMARY KEY,
                    entry JSONB NOT NULL,
                    entry_ts TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS regime_daily_cache (
                    day TEXT PRIMARY KEY,
                    entry JSONB NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)


def _store_default():
    return {
        "watchlist": ["NVDA", "META", "AAPL", "MSFT", "TSLA"],
        "log": [],
        "pm_cache": {},
        "account_size": 100000,
        "risk_per_trade": 0.01,
        "max_position_pct": 0.25,
        # User-marked support/resistance levels per ticker. Shape:
        # {"COIN": {"support": [145, 110], "resistance": [213, 280]}, ...}
        # These are merged with auto-detected key_levels at app render time.
        "manual_levels": {},
        # User-hidden auto-detected levels per ticker. Shape:
        # {"DASH": [155.22, 168.40], ...}. A hidden auto level stays hidden
        # until restored from the key-levels section.
        "hidden_levels": {},
        # First-class positions owned outside the decision logger.
        # Shape: {"NVDA": {"ticker": "NVDA", "entry_price": 198.45,
        #                  "shares": 10, "target1_price": 240,
        #                  "stop_price": 190, "user_note": "..."}}
        "holdings": {},
        # Persist the last analyzed ticker so browser refreshes do not fall
        # back to the default name.
        "last_ticker": "NVDA",
        # Decision comparison log — one entry per "Log this comparison"
        # click. Side-by-side record of rule engine action, Claude action,
        # and (optionally) user action at a point in time so we can later
        # evaluate which source produces better calls.
        # Shape per entry:
        # {
        #   "id": "uuid", "ts": iso8601, "ticker": "DASH", "price": 171.97,
        #   "rule_action": "watch", "rule_state": "TRENDING",
        #   "claude_action": "HOLD_OFF", "claude_confidence": 6,
        #   "claude_reasoning": "...", "claude_trigger": "...",
        #   "user_action": "watch" | null, "user_note": "...",
        #   "outcome": null | { "ts": iso, "result": "right"|"wrong"|"unclear",
        #                       "right_sources": ["rule"|"claude"|"user"],
        #                       "result_pct": float, "note": "..." }
        # }
        "decisions_log": [],
        "chat_history": {},  # {ticker: [{role, content}, ...]}
        # Market Regime daily memo cache. Shape:
        # {"2026-06-25": {"ts": iso, "source": "claude"|"fallback",
        #                  "memo": {...}}}
        # Mirrors the Google Apps Script pattern: generate once per day,
        # render cached text fast on every page load.
        "regime_daily_cache": {},
    }


_SPLIT_STORE_KEYS = {"chat_history", "decisions_log", "regime_daily_cache"}


def _split_store_sections(store):
    """Separate large append-heavy sections from the small core settings blob."""
    core = {
        k: v
        for k, v in (store or {}).items()
        if k not in _SPLIT_STORE_KEYS
    }
    chat = (store or {}).get("chat_history") or {}
    decisions = (store or {}).get("decisions_log") or []
    regime_cache = (store or {}).get("regime_daily_cache") or {}
    return core, chat, decisions, regime_cache


def _persist_cache():
    """Per-session fingerprints so unchanged rows are not rewritten."""
    try:
        return st.session_state.setdefault(
            "_persist_fingerprints",
            {"core": None, "chat": {}, "decisions": {}, "regime": {}},
        )
    except Exception:
        return {"core": None, "chat": {}, "decisions": {}, "regime": {}}


def _stable_json(value):
    return json.dumps(_json_safe(value), allow_nan=False, sort_keys=True, separators=(",", ":"))


def _load_split_sections(cur, store):
    """Merge normalized Postgres rows back into the legacy in-memory store."""
    cur.execute("SELECT ticker, messages FROM chat_history")
    chat_rows = cur.fetchall()
    if chat_rows:
        store["chat_history"] = {str(t).upper(): (m or []) for t, m in chat_rows}

    cur.execute("""
        SELECT entry
        FROM decisions_log
        ORDER BY COALESCE(entry_ts, updated_at) DESC
    """)
    decision_rows = cur.fetchall()
    if decision_rows:
        store["decisions_log"] = [row[0] for row in decision_rows if row and row[0]]

    cur.execute("SELECT day, entry FROM regime_daily_cache")
    regime_rows = cur.fetchall()
    if regime_rows:
        store["regime_daily_cache"] = {str(day): (entry or {}) for day, entry in regime_rows}
    return store


def _save_split_sections(cur, chat, decisions, regime_cache):
    """Persist high-growth store sections with targeted row upserts."""
    fingerprints = _persist_cache()
    chat = chat if isinstance(chat, dict) else {}
    chat_keys = []
    for ticker, messages in chat.items():
        key = str(ticker).upper().strip()
        if not key:
            continue
        chat_keys.append(key)
        messages_json = _stable_json(messages or [])
        if fingerprints["chat"].get(key) == messages_json:
            continue
        cur.execute("""
            INSERT INTO chat_history (ticker, messages, updated_at)
            VALUES (%s, %s::jsonb, NOW())
            ON CONFLICT (ticker) DO UPDATE
                SET messages = EXCLUDED.messages, updated_at = NOW()
        """, (key, messages_json))
        fingerprints["chat"][key] = messages_json
    if not chat_keys:
        cur.execute("DELETE FROM chat_history")
        fingerprints["chat"] = {}
    elif set(fingerprints["chat"].keys()) != set(chat_keys):
        cur.execute("DELETE FROM chat_history WHERE NOT (ticker = ANY(%s))", (chat_keys,))
        fingerprints["chat"] = {k: v for k, v in fingerprints["chat"].items() if k in set(chat_keys)}

    decisions = decisions if isinstance(decisions, list) else []
    decision_ids = []
    for idx, entry in enumerate(decisions):
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "").strip() or f"legacy-{idx}"
        decision_ids.append(entry_id)
        entry_ts = entry.get("ts") or entry.get("created_at")
        entry_json = _stable_json(entry)
        if fingerprints["decisions"].get(entry_id) == entry_json:
            continue
        cur.execute("""
            INSERT INTO decisions_log (id, entry, entry_ts, updated_at)
            VALUES (%s, %s::jsonb, NULLIF(%s, '')::timestamptz, NOW())
            ON CONFLICT (id) DO UPDATE
                SET entry = EXCLUDED.entry,
                    entry_ts = EXCLUDED.entry_ts,
                    updated_at = NOW()
        """, (
            entry_id,
            entry_json,
            str(entry_ts or ""),
        ))
        fingerprints["decisions"][entry_id] = entry_json
    if not decision_ids:
        cur.execute("DELETE FROM decisions_log")
        fingerprints["decisions"] = {}
    elif set(fingerprints["decisions"].keys()) != set(decision_ids):
        cur.execute("DELETE FROM decisions_log WHERE NOT (id = ANY(%s))", (decision_ids,))
        fingerprints["decisions"] = {k: v for k, v in fingerprints["decisions"].items() if k in set(decision_ids)}

    regime_cache = regime_cache if isinstance(regime_cache, dict) else {}
    regime_days = []
    for day, entry in regime_cache.items():
        key = str(day).strip()
        if not key:
            continue
        regime_days.append(key)
        entry_json = _stable_json(entry or {})
        if fingerprints["regime"].get(key) == entry_json:
            continue
        cur.execute("""
            INSERT INTO regime_daily_cache (day, entry, updated_at)
            VALUES (%s, %s::jsonb, NOW())
            ON CONFLICT (day) DO UPDATE
                SET entry = EXCLUDED.entry, updated_at = NOW()
        """, (key, entry_json))
        fingerprints["regime"][key] = entry_json
    if not regime_days:
        cur.execute("DELETE FROM regime_daily_cache")
        fingerprints["regime"] = {}
    elif set(fingerprints["regime"].keys()) != set(regime_days):
        cur.execute("DELETE FROM regime_daily_cache WHERE NOT (day = ANY(%s))", (regime_days,))
        fingerprints["regime"] = {k: v for k, v in fingerprints["regime"].items() if k in set(regime_days)}


def load_store():
    if USE_POSTGRES:
        try:
            _pg_init()
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM kv_store WHERE key = 'default'")
                    row = cur.fetchone()
                    if row:
                        store = row[0] or _store_default()
                        defaults = _store_default()
                        for key, value in defaults.items():
                            store.setdefault(key, value)
                        return _load_split_sections(cur, store)
                    # No row yet — return defaults; first save creates the row.
                    return _store_default()
        except Exception as e:
            # CRITICAL: When DATABASE_URL is set but unreachable, do NOT fall
            # through to the local file. On Streamlit Cloud the container
            # filesystem gets wiped on every reboot, so a "save" to the file
            # appears to succeed but vanishes — silent data loss. Store the
            # error for a prominent banner and return defaults.
            try:
                st.session_state["_db_error"] = str(e)
            except Exception:
                pass
            return _store_default()
    # File fallback — only reached when DATABASE_URL is unset (local dev)
    if STORE_PATH.exists():
        try:
            return json.loads(STORE_PATH.read_text())
        except Exception:
            pass
    return _store_default()


def _json_safe(value):
    """Recursively convert app state into values Postgres JSONB can store."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, set):
        return [_json_safe(v) for v in sorted(value, key=str)]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if hasattr(value, "isoformat") and value.__class__.__module__.startswith(("datetime", "pandas")):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def save_store(store):
    safe_store = _json_safe(store)
    if USE_POSTGRES:
        try:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    core_store, chat, decisions, regime_cache = _split_store_sections(safe_store)
                    fingerprints = _persist_cache()
                    core_json = _stable_json(core_store)
                    if fingerprints.get("core") != core_json:
                        cur.execute("""
                            INSERT INTO kv_store (key, value, updated_at)
                            VALUES ('default', %s::jsonb, NOW())
                            ON CONFLICT (key) DO UPDATE
                                SET value = EXCLUDED.value, updated_at = NOW()
                        """, (core_json,))
                        fingerprints["core"] = core_json
                    _save_split_sections(cur, chat, decisions, regime_cache)
            return
        except Exception as e:
            # CRITICAL: Do NOT fall through to local file when USE_POSTGRES
            # is true. The container filesystem is wiped on Streamlit Cloud
            # reboots, so a "successful" file save would silently vanish.
            try:
                st.error(
                    f"⚠️ Database save failed: {e}. "
                    f"Your changes were NOT persisted. Check DATABASE_URL secret."
                )
            except Exception:
                pass
            return
    # File fallback — only reached when DATABASE_URL is unset (local dev)
    STORE_PATH.write_text(json.dumps(safe_store, indent=2, allow_nan=False))


ACTIVE_VIEWS = {"regime", "analyze", "watchlist", "holdings", "ideas"}
ARCHIVED_VIEWS = {"tracker"}
SHOW_ARCHIVED_TRACKER = False


if "store" not in st.session_state:
    st.session_state.store = load_store()
    # Track whether any defaults were applied so we save once after init.
    # Without this, defaults stayed in-memory and were never persisted, so
    # every session re-applied them from scratch.
    _needs_save = False
    if "pm_cache" not in st.session_state.store:
        st.session_state.store["pm_cache"] = {}
        _needs_save = True
    if "account_size" not in st.session_state.store:
        st.session_state.store["account_size"] = 100000
        _needs_save = True
    if "risk_per_trade" not in st.session_state.store:
        st.session_state.store["risk_per_trade"] = 0.01
        _needs_save = True
    if "max_position_pct" not in st.session_state.store:
        st.session_state.store["max_position_pct"] = 0.25
        _needs_save = True
    if "manual_levels" not in st.session_state.store:
        st.session_state.store["manual_levels"] = {}
        _needs_save = True
    if "hidden_levels" not in st.session_state.store:
        st.session_state.store["hidden_levels"] = {}
        _needs_save = True
    if "holdings" not in st.session_state.store:
        st.session_state.store["holdings"] = {}
        _needs_save = True
    if "decisions_log" not in st.session_state.store:
        st.session_state.store["decisions_log"] = []
        _needs_save = True
    if "regime_daily_cache" not in st.session_state.store:
        st.session_state.store["regime_daily_cache"] = {}
        _needs_save = True
    if "final_action_cache" not in st.session_state.store:
        st.session_state.store["final_action_cache"] = {}
        _needs_save = True
    if "idea_discovery_runs" not in st.session_state.store:
        st.session_state.store["idea_discovery_runs"] = []
        _needs_save = True
    if "quote_meta_cache" not in st.session_state.store:
        st.session_state.store["quote_meta_cache"] = {}
        _needs_save = True
    if "ticker_snapshots" not in st.session_state.store:
        st.session_state.store["ticker_snapshots"] = {}
        _needs_save = True
    if "last_ticker" not in st.session_state.store:
        st.session_state.store["last_ticker"] = "NVDA"
        _needs_save = True
    if _needs_save:
        save_store(st.session_state.store)
if "current_ticker" not in st.session_state:
    try:
        _qp_ticker = (
            st.query_params.get("ticker")
            or st.query_params.get("open")
            or st.query_params.get("pm_refresh")
            or st.query_params.get("research_refresh")
            or st.query_params.get("data_refresh")
        )
    except Exception:
        _qp_ticker = ""
    st.session_state.current_ticker = str(
        _qp_ticker or st.session_state.store.get("last_ticker") or "NVDA"
    ).upper().strip()
if "view" not in st.session_state:
    try:
        _qp_view = str(st.query_params.get("view") or "").strip().lower()
    except Exception:
        _qp_view = ""
    _stored_view = str(st.session_state.store.get("last_view") or "regime").strip().lower()
    st.session_state.view = (
        _qp_view
        if _qp_view in ACTIVE_VIEWS
        else (_stored_view if _stored_view in ACTIVE_VIEWS else "regime")
    )
if "pm_expanded" not in st.session_state:
    st.session_state.pm_expanded = {}
if "nav_counter" not in st.session_state:
    st.session_state.nav_counter = 0


def get_cached_pm(ticker, tactical_output, api_key, company_name, allow_generate=True, force_generate=False):
    ticker = ticker.upper()
    cache = st.session_state.store["pm_cache"]
    entry = cache.get(ticker)
    if entry and not force_generate:
        ts = entry.get("ts")
        try:
            age = datetime.now() - datetime.fromisoformat(ts)
            if age < timedelta(days=PM_CACHE_TTL_DAYS):
                pm = entry["view"]
                pm["_source"] = (entry.get("source") or "cached") + f" · {age.days}d old"
                return pm
        except Exception:
            pass
        if not allow_generate:
            try:
                age = datetime.now() - datetime.fromisoformat(entry.get("ts"))
                pm = entry["view"]
                pm["_source"] = (entry.get("source") or "cached") + f" · {age.days}d old · refresh to update"
                return pm
            except Exception:
                pass
    if not allow_generate:
        pm = get_pm_view(ticker, tactical_output, api_key=None, company_name=company_name)
        pm["_source"] = "static · fast mode"
        return pm
    if not api_key and entry:
        try:
            age = datetime.now() - datetime.fromisoformat(entry.get("ts"))
            pm = dict(entry["view"])
            pm["_source"] = (entry.get("source") or "cached") + f" · {age.days}d old · API key unavailable"
            return pm
        except Exception:
            pass

    pm = get_pm_view(ticker, tactical_output, api_key=api_key, company_name=company_name)
    # Count this fresh Claude call if it actually hit the API. Source is
    # "claude" on success, "static" or "static (claude call failed: ...)"
    # on fallback — only the first counts toward session cost.
    if pm.get("_source") == "claude":
        st.session_state["claude_calls_this_session"] = (
            st.session_state.get("claude_calls_this_session", 0) + 1
        )
    thesis_text = str(pm.get("thesis") or "")
    is_placeholder_pm = (
        thesis_text.startswith(f"No thesis on file for {ticker}")
        or thesis_text.startswith(f"No generated PM thesis yet for {ticker}")
    )
    if (is_placeholder_pm or pm.get("_source") != "claude") and entry:
        try:
            age = datetime.now() - datetime.fromisoformat(entry.get("ts"))
            fallback = dict(entry["view"])
            reason = str(pm.get("_source") or "refresh failed")
            fallback["_source"] = (
                (entry.get("source") or "cached")
                + f" · {age.days}d old · refresh failed ({reason})"
            )
            return fallback
        except Exception:
            pass
    # Do not persist a generic "no thesis" fallback after a failed refresh.
    # Otherwise one bad refresh makes the PM side look permanently stale.
    if not is_placeholder_pm:
        cache[ticker] = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "view": {k: v for k, v in pm.items() if not k.startswith("_")},
            "source": pm.get("_source", "static"),
        }
        save_store(st.session_state.store)
    return pm


def clear_pm_cache(ticker):
    """Clear BOTH the pm_cache and dossier_cache for a ticker.

    Originally these were separate caches (dossier was expensive, PM was
    cheap, refresh shouldn't trigger both). But now the dossier holds
    `tactical_call` for the calibration trial — so clearing only pm_cache
    leaves a stale Claude action on the comparison panel forever, which
    defeats the purpose of the ↻ button. Clear both.
    """
    ticker = ticker.upper()
    changed = False
    if ticker in st.session_state.store.get("pm_cache", {}):
        del st.session_state.store["pm_cache"][ticker]
        changed = True
    if ticker in st.session_state.store.get("dossier_cache", {}):
        del st.session_state.store["dossier_cache"][ticker]
        changed = True
    snapshot = st.session_state.store.setdefault("ticker_snapshots", {}).get(ticker)
    if isinstance(snapshot, dict):
        for key in ("pm", "pm_updated_at"):
            if key in snapshot:
                snapshot.pop(key, None)
                changed = True
    if changed:
        save_store(st.session_state.store)


def get_cached_dossier(ticker, t_state, modifiers, meta, pm_data, api_key, company_name, allow_generate=True, force_generate=False, fast_generate=False):
    """Cache decision dossiers with staleness rules + live-value substitution.

    Cost-saving design:
    - Claude generates prose ONCE with placeholder tokens like {{price}},
      {{rs}}, {{pct_ma200}} instead of hardcoded numbers.
    - On every render, we substitute live values from the tactical engine
      (free; uses already-cached yfinance data).
    - We only regenerate (paid Claude call) when:
        - The cached prose is older than PM_CACHE_TTL_DAYS, OR
        - Price has moved >10% since generation (qualitative read may have
          shifted), OR
        - The action label has changed (e.g. Watch → Avoid means the
          interpretation needs to flip).

    This keeps narrative numbers always-current while spending Claude
    budget only when the qualitative interpretation might genuinely have
    shifted. Target cost: ~$5-8/month at 30 tickers/regular use.
    """
    ticker = ticker.upper()
    cache = st.session_state.store.setdefault("dossier_cache", {})
    entry = cache.get(ticker)
    if not api_key:
        if entry:
            try:
                age = datetime.now() - datetime.fromisoformat(entry.get("ts"))
                full = entry.get("result") or {
                    "dossier": entry.get("text"),
                    "technical_narrative": None,
                    "pm_narrative": None,
                    "bullets": {},
                    "quality": {},
                    "tactical_call": {},
                }
                full.setdefault("quality", {})
                full.setdefault("tactical_call", {})
                age_label = "today" if age.days == 0 else f"{age.days}d ago"
                return {
                    **full,
                    "_source": (entry.get("source") or "claude") + f" · {age_label} · API key unavailable",
                }
            except Exception:
                pass
        return {"dossier": None, "technical_narrative": None,
                "pm_narrative": None, "bullets": {}, "quality": {},
                "_source": "unavailable"}
    current_price = t_state.get("price") if isinstance(t_state, dict) else None
    current_action = t_state.get("action") if isinstance(t_state, dict) else None
    cache_schema_stale = bool(
        entry and entry.get("schema_version", 0) < DOSSIER_SCHEMA_VERSION
    )

    stale_cached_result = None
    stale_cached_age = None
    stale_cached_price = None
    if entry:
        try:
            age = datetime.now() - datetime.fromisoformat(entry.get("ts"))
            cached_price = entry.get("price_at_generation")
            cached_action = entry.get("action_at_generation")
            full = entry.get("result") or {
                "dossier": entry.get("text"),
                "technical_narrative": None,
                "pm_narrative": None,
                "bullets": {},
                "quality": {},
                "tactical_call": {},
            }
            full.setdefault("quality", {})
            full.setdefault("tactical_call", {})
            stale_cached_result = full
            stale_cached_age = age
            stale_cached_price = cached_price

            staleness_failed = (
                cache_schema_stale or
                age >= timedelta(days=PM_CACHE_TTL_DAYS)
            )

            if not staleness_failed and cached_price and current_price:
                pct_moved = abs(current_price - cached_price) / cached_price
                if pct_moved >= 0.10:
                    staleness_failed = True

            if not staleness_failed and cached_action and current_action:
                if cached_action != current_action:
                    staleness_failed = True

            if not staleness_failed and not force_generate:
                # Live-value substitution. Free — uses already-cached
                # yfinance data via the tactical engine output we already
                # have. Replaces {{price}}, {{rs}}, etc. with current.
                from pm_view import substitute_live_values
                substituted = {**full}
                if isinstance(t_state, dict):
                    for k in ("dossier", "technical_narrative", "pm_narrative"):
                        if substituted.get(k):
                            substituted[k] = substitute_live_values(
                                substituted[k], t_state
                            )

                age_label = "today" if age.days == 0 else f"{age.days}d ago"
                return {
                    **substituted,
                    "_source": entry.get("source", "claude") + f" · {age_label}",
                    "_freshness": {
                        "age_days": age.days,
                        "price_at_generation": cached_price,
                        "current_price": current_price,
                    },
                }
        except Exception:
            pass

    if not allow_generate:
        if stale_cached_result:
            try:
                from pm_view import substitute_live_values
                substituted = {**stale_cached_result}
                if isinstance(t_state, dict):
                    for k in ("dossier", "technical_narrative", "pm_narrative"):
                        if substituted.get(k):
                            substituted[k] = substitute_live_values(
                                substituted[k], t_state
                            )
                age_days = stale_cached_age.days if stale_cached_age else 0
                age_label = "today" if age_days == 0 else f"{age_days}d ago"
                source_label = (
                    "research upgraded · refresh to update"
                    if cache_schema_stale
                    else entry.get("source", "claude") + f" · {age_label} · refresh to update"
                )
                return {
                    **substituted,
                    "_source": source_label,
                    "_freshness": {
                        "age_days": age_days,
                        "price_at_generation": stale_cached_price,
                        "current_price": current_price,
                        "stale": True,
                        "schema_stale": cache_schema_stale,
                    },
                }
            except Exception:
                pass
        return {
            "dossier": None,
            "technical_narrative": None,
            "pm_narrative": None,
            "bullets": {},
            "quality": {},
            "tactical_call": {},
            "_source": "cached only · fast mode",
        }

    # Cache miss or staleness failed — regenerate via Claude.
    result = get_decision_dossier(
        ticker, t_state, modifiers, meta, pm_data,
        api_key=api_key, company_name=company_name, fast=fast_generate,
    )
    if result.get("dossier"):
        st.session_state["claude_calls_this_session"] = (
            st.session_state.get("claude_calls_this_session", 0) + 1
        )
        cache[ticker] = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "schema_version": DOSSIER_SCHEMA_VERSION,
            "price_at_generation": current_price,
            "action_at_generation": current_action,
            "result": {
                "dossier": result.get("dossier"),
                "technical_narrative": result.get("technical_narrative"),
                "pm_narrative": result.get("pm_narrative"),
                "bullets": result.get("bullets") or {},
                "quality": result.get("quality") or {},
                "tactical_call": result.get("tactical_call") or {},
            },
            "source": result.get("_source", "claude"),
        }
        merge_ticker_snapshot(ticker, pm_entry=cache[ticker])
        save_store(st.session_state.store)
    elif stale_cached_result:
        try:
            from pm_view import substitute_live_values
            substituted = {**stale_cached_result}
            if isinstance(t_state, dict):
                for k in ("dossier", "technical_narrative", "pm_narrative"):
                    if substituted.get(k):
                        substituted[k] = substitute_live_values(substituted[k], t_state)
            age_days = stale_cached_age.days if stale_cached_age else 0
            reason = str(result.get("_source") or "refresh failed")
            return {
                **substituted,
                "_source": f"refresh failed ({reason}) · using cached {age_days}d old",
                "_freshness": {
                    "age_days": age_days,
                    "price_at_generation": stale_cached_price,
                    "current_price": current_price,
                    "stale": True,
                    "refresh_failed": True,
                },
            }
        except Exception:
            pass

    # Substitute on freshly-generated result too. If Claude used tokens,
    # this replaces them with current values. If not, this is a no-op.
    from pm_view import substitute_live_values
    if isinstance(t_state, dict) and result.get("dossier"):
        for k in ("dossier", "technical_narrative", "pm_narrative"):
            if result.get(k):
                result[k] = substitute_live_values(result[k], t_state)
    return result


def clear_dossier_cache(ticker):
    ticker = ticker.upper()
    cache = st.session_state.store.get("dossier_cache", {})
    if ticker in cache:
        del cache[ticker]
        save_store(st.session_state.store)


def dossier_cache_needs_upgrade(ticker):
    """True when cached prose predates the current research prompt/schema."""
    entry = st.session_state.store.get("dossier_cache", {}).get(str(ticker).upper())
    return bool(entry and entry.get("schema_version", 0) < DOSSIER_SCHEMA_VERSION)


# ─────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Source+Serif+4:opsz,wght@8..60,400;8..60,500&family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500;600&display=swap');

/* ────────────────────────────────────────────────────────────── */
/*  TYPE SCALE — six tokens, used everywhere                      */
/*    --xs:    section labels, small caps                         */
/*    --sm:    tertiary metadata, footnotes                       */
/*    --base:  body text, controls, list items                    */
/*    --md:    body emphasis, dossier paragraphs                  */
/*    --lg:    section headers inside cards                       */
/*    --hero:  the giant decision word                            */
/* ────────────────────────────────────────────────────────────── */
:root {
    /* Type scale */
    --fs-xs: 10px;
    --fs-sm: 11px;
    --fs-base: 13px;
    --fs-md: 15px;
    --fs-lg: 18px;
    --fs-xl: 24px;
    --fs-hero: 88px;

    /* Font families */
    --font-sans:  'Geist', -apple-system, system-ui, sans-serif;
    --font-serif: 'Source Serif 4', Georgia, serif;
    --font-mono:  'Geist Mono', monospace;

    /* Letter-spacing scale */
    --ls-tight:    0.02em;
    --ls-normal:   0.04em;
    --ls-caps-xs:  0.06em;
    --ls-caps-sm:  0.08em;
    --ls-caps:     0.10em;
    --ls-caps-md:  0.12em;
    --ls-caps-lg:  0.14em;
    --ls-caps-xl:  0.16em;
    --ls-caps-xxl: 0.18em;

    /* Color palette — text */
    --color-text:      #101114;
    --color-body:      #343842;
    --color-muted:     #69707D;
    --color-faint:     #8D95A3;
    --color-fainter:   #A9B1BE;
    --color-faintest:  #B9C1CC;

    /* Color palette — surfaces */
    --color-bg:              #FCFCFF;
    --color-surface:         #FFFFFF;
    --color-surface-soft:    #F4F7FB;
    --color-surface-warning: #FFF2F3;
    --color-surface-trigger: #EEF6FF;
    --color-border:          #E3E8F0;
    --color-border-soft:     #EEF2F7;

    /* Color palette — semantic */
    --color-accent:       #18C964;
    --color-accent-hover: #0A9F4C;
    --color-positive:     #16A34A;
    --color-negative:     #FF4D6D;
    --color-warning:      #D83A5E;
    --color-warning-text: #9A6700;
    --color-purple:       #7C3AED;
    --color-blue:         #2563EB;
    --color-cyan:         #06B6D4;
}

/* ────────────────────────────────────────────────────────────── */
/*  DECISION COMPARISON PANEL — single source of truth            */
/* ────────────────────────────────────────────────────────────── */
.desk-cmp {
    background: rgba(255, 255, 255, 0.92);
    border: 1px solid var(--color-border);
    border-radius: 8px;
    padding: 16px 18px;
    margin: 8px 0 14px;
    box-shadow: 0 12px 28px rgba(15, 23, 42, 0.05);
}
.desk-cmp-grid {
    display: grid;
    grid-template-columns: 1fr 1px 1fr;
    gap: 12px;
    align-items: start;
    margin-bottom: 6px;
}
.desk-cmp-divider {
    background: var(--color-border);
    width: 1px;
    height: 100%;
}
.desk-cmp-header {
    font-family: var(--font-sans);
    font-size: var(--fs-xs) !important;
    font-weight: 600;
    letter-spacing: var(--ls-caps-xl);
    text-transform: uppercase;
    color: var(--color-muted);
    margin-bottom: 10px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.desk-cmp-side-label {
    font-family: var(--font-sans);
    font-size: var(--fs-xs) !important;
    font-weight: 600;
    letter-spacing: var(--ls-caps-md);
    text-transform: uppercase;
    color: var(--color-faint);
    margin-bottom: 4px;
}
.desk-cmp-action {
    font-family: var(--font-sans);
    font-size: 20px !important;
    font-weight: 750;
    line-height: 1.15;
}
.desk-cmp-meta {
    font-size: var(--fs-base) !important;
    color: var(--color-muted);
    margin-top: 4px;
}
.desk-cmp-fallback {
    font-family: var(--font-serif);
    font-size: var(--fs-md) !important;
    font-weight: 400;
    color: var(--color-fainter);
    font-style: italic;
    line-height: 1.4;
}
.desk-cmp-reasoning {
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px dashed var(--color-border-soft);
    font-size: var(--fs-md) !important;
    line-height: 1.55;
    color: var(--color-body);
}
.desk-cmp-reasoning-label {
    font-size: var(--fs-xs) !important;
    font-weight: 600;
    letter-spacing: var(--ls-caps-md);
    text-transform: uppercase;
    color: var(--color-faint);
    display: block;
    margin-bottom: 4px;
}
.desk-cmp-trigger {
    margin-top: 10px;
    padding: 8px 10px;
    border: 1px solid var(--color-border);
    border-left: 3px solid var(--color-warning);
    border-radius: 6px;
    background: #FFFFFF;
    font-size: var(--fs-base) !important;
    line-height: 1.45;
    color: var(--color-body);
}
.desk-cmp-trigger-label {
    font-size: var(--fs-xs) !important;
    font-weight: 600;
    letter-spacing: var(--ls-caps-md);
    text-transform: uppercase;
    color: var(--color-faint);
    margin-right: 4px;
}
.desk-cmp-yourcall-label {
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px dashed var(--color-border-soft);
    font-family: var(--font-sans);
    font-size: var(--fs-xs) !important;
    font-weight: 600;
    letter-spacing: var(--ls-caps-md);
    text-transform: uppercase;
    color: var(--color-faint);
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.desk-cmp-info {
    width: 20px; height: 20px;
    border-radius: 50%;
    border: 1px solid #C9C5BC;
    color: var(--color-faint);
    font-family: var(--font-serif);
    font-size: var(--fs-base) !important;
    font-weight: 600;
    font-style: italic;
    line-height: 18px;
    text-align: center;
    text-transform: none !important;
    cursor: help;
    display: inline-block;
    flex-shrink: 0;
}
.desk-cmp-badge {
    padding: 2px 8px;
    border-radius: 3px;
    font-size: var(--fs-xs) !important;
    font-weight: 600;
    letter-spacing: var(--ls-caps);
    text-transform: uppercase;
    line-height: 1.4;
}
.desk-cmp-badge-disagree { background: #FFF2F3; color: #8A1F1F; }
.desk-cmp-badge-agree    { background: #D1FAE5; color: #065F46; }
.desk-cmp-badge-unknown  { background: var(--color-surface-soft); color: var(--color-muted); }
.desk-cmp-read {
    margin: -2px 0 12px;
    padding: 8px 10px;
    border: 1px solid var(--color-border-soft);
    border-left: 3px solid var(--color-blue);
    border-radius: 4px;
    background: #FFFFFF;
    font-size: var(--fs-sm) !important;
    line-height: 1.45;
    color: var(--color-muted);
}
.desk-cmp-read strong {
    color: var(--color-text);
    font-weight: 650;
}
.desk-cmp-resolution {
    margin: -2px 0 12px;
    padding: 10px 12px;
    border: 1px solid var(--color-border);
    border-left: 3px solid var(--color-text);
    border-radius: 4px;
    background: #FFFFFF;
    color: var(--color-body);
}
.desk-cmp-resolution-top {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 5px;
}
.desk-cmp-resolution-chip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 7px;
    border: 1px solid var(--color-border);
    border-radius: 4px;
    background: var(--color-surface);
    font-family: var(--font-sans);
    font-size: var(--fs-xs) !important;
    font-weight: 700;
    letter-spacing: var(--ls-caps-md);
    text-transform: uppercase;
    color: var(--color-muted);
}
.desk-cmp-resolution-title {
    font-family: var(--font-sans);
    font-size: var(--fs-xs) !important;
    font-weight: 700;
    letter-spacing: var(--ls-caps-md);
    text-transform: uppercase;
    color: var(--color-faint);
}
.desk-cmp-resolution-text {
    font-size: var(--fs-sm) !important;
    line-height: 1.5;
    color: var(--color-body);
}
.desk-cmp-resolution-text strong {
    color: var(--color-text);
    font-weight: 700;
}

/* Green primary Log button inside the comparison panel.
   Streamlit doesn't allow per-button styling, so we wrap the button in
   a div with class `desk-log-btn-wrap` and target the button inside. */
.desk-log-btn-wrap div[data-testid="stButton"] > button,
.desk-log-btn-wrap button[kind="secondary"] {
    background: var(--color-accent) !important;
    color: var(--color-bg) !important;
    border: 1px solid var(--color-accent) !important;
    font-weight: 600 !important;
}
.desk-log-btn-wrap div[data-testid="stButton"] > button:hover,
.desk-log-btn-wrap button[kind="secondary"]:hover {
    background: var(--color-accent-hover) !important;
    border-color: var(--color-accent-hover) !important;
    color: var(--color-bg) !important;
}

.stApp {
    background:
        linear-gradient(180deg, #FCFCFF 0%, #F7FAFF 48%, #FCFCFF 100%);
}
/* Keep horizontal overflow hidden to avoid scrollbars */
body { overflow-x: hidden !important; }

.main .block-container {
    padding-top: 0; padding-bottom: 3rem; max-width: 1400px;
    font-size: var(--fs-lg);
}

html, body, .main, .main p, .main li {
    font-family: var(--font-sans);
    color: var(--color-text);
}
/* Default body size — only applied to elements WITHOUT a more specific
   class. Without this scoping, Streamlit's emotion classes plus the
   wildcard div/span selectors override every class-defined font-size
   in the app, causing 10px badges to render at 18px, etc. */
html, body, .main, .main p, .main li {
    font-size: var(--fs-lg);
}
.main p:not([class*="desk-"]):not([class*="stMarkdown"]) {
    font-size: var(--fs-lg);
}
#MainMenu, footer { visibility: hidden; }

/* ────────────────────────────────────────────────────────────── */
/*  Decision Dossier — synthesis paragraph below the decision     */
/* ────────────────────────────────────────────────────────────── */
.desk-dossier {
    margin: 0 0 28px;
    padding: 20px 22px;
    background: rgba(255, 255, 255, 0.94);
    border: 1px solid var(--color-border);
    border-left: 4px solid var(--color-blue);
    border-radius: 8px;
    box-shadow: 0 14px 30px rgba(15, 23, 42, 0.055);
}
.desk-dossier-label {
    font-family: var(--font-sans);
    font-size: var(--fs-sm); font-weight: 600; letter-spacing: var(--ls-caps-xxl);
    text-transform: uppercase; color: var(--color-muted);
    display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 10px;
}
.desk-dossier-label .em { font-size: var(--fs-base); margin-right: 6px; }
.desk-dossier-label .src {
    font-family: var(--font-mono); font-size: var(--fs-xs);
    color: var(--color-faintest); letter-spacing: var(--ls-normal);
    text-transform: none; font-weight: 400;
}
.desk-dossier-text {
    font-family: var(--font-serif);
    font-size: var(--fs-lg); line-height: 1.55; color: var(--color-text);
    font-style: normal;
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
    font-size: var(--fs-base); line-height: 1.35;
    border-radius: 999px;
    border: 1px solid;
    display: flex; align-items: center; gap: 8px;
}
.desk-mod-high { background: var(--color-surface-warning); border-color: #F5C8C8; color: #6E2E2E; }
.desk-mod-med  { background: var(--color-surface-trigger); border-color: #CFE0FF; color: var(--color-warning-text); }
.desk-mod-low  { background: var(--color-surface-soft); border-color: var(--color-border); color: var(--color-body); }
.desk-mod .icon {
    font-size: var(--fs-base); line-height: 1;
}

/* Navbar */
div[data-testid="stElementContainer"]:has(.desk-bar),
div[data-testid="element-container"]:has(.desk-bar) {
    position: sticky !important;
    top: 72px !important;
    z-index: 1000 !important;
}
.desk-bar {
    background: rgba(255, 255, 255, 0.88);
    color: var(--color-text);
    padding: 7px 0 8px;
    display: flex; justify-content: space-between; align-items: center;
    margin: 0 0 calc(1.2rem + 52px);
    position: relative;
    z-index: 999;
    border-bottom: 1px solid var(--color-border);
    box-shadow: none;
    backdrop-filter: blur(8px);
}
.main .block-container {
    padding-top: 4px !important;
}
[data-testid="stMainBlockContainer"] {
    padding-top: 4px !important;
}
.desk-bar .wordmark {
    font-family: var(--font-mono); font-weight: 600;
    font-size: var(--fs-sm); line-height: 1;
    letter-spacing: var(--ls-caps-xl);
    text-transform: uppercase;
    color: var(--color-text);
}
.desk-bar .wordmark .arrow { color: var(--color-accent); margin-right: 6px; }
.desk-bar .meta {
    font-family: var(--font-mono); font-size: var(--fs-sm);
    color: var(--color-muted); letter-spacing: var(--ls-caps-sm); text-transform: uppercase;
}

/* Sidebar: low visual weight */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #F4F7FB 0%, #EFFAF5 100%);
}
section[data-testid="stSidebar"] .stMarkdown h3 {
    font-size: var(--fs-sm); font-weight: 600; letter-spacing: var(--ls-caps);
    text-transform: uppercase; color: var(--color-muted); margin-bottom: 6px;
}
section[data-testid="stSidebar"] div.stButton > button {
    background: transparent; border: 1px solid transparent;
    color: var(--color-body);
    font-family: var(--font-mono); font-size: var(--fs-sm); font-weight: 500;
    border-radius: 8px; padding: 5px 9px; text-align: left; justify-content: flex-start;
}
section[data-testid="stSidebar"] div.stButton > button:hover {
    background: rgba(24, 201, 100, 0.10); color: var(--color-text);
    border-color: rgba(24, 201, 100, 0.20);
}

/* Ticker line */
.desk-ticker-row {
    display: flex; justify-content: space-between; align-items: flex-start;
    height: 58px;
    padding-bottom: 10px; border-bottom: 1px solid var(--color-border); margin-bottom: 18px;
    box-sizing: border-box;
}
.desk-ticker-row .sym {
    font-size: var(--fs-xl); font-weight: 600; letter-spacing: -0.02em; line-height: 1;
}
.desk-ticker-row .name { font-size: var(--fs-base); color: var(--color-muted); margin-left: 10px; }
.desk-ticker-row .price {
    font-family: var(--font-mono); font-variant-numeric: tabular-nums;
    font-size: var(--fs-lg); font-weight: 500;
}
.desk-ticker-row .chg {
    font-family: var(--font-mono); font-variant-numeric: tabular-nums;
    font-size: var(--fs-base); margin-left: 10px;
}
.desk-ticker-row .meta-inline {
    font-family: var(--font-mono); font-variant-numeric: tabular-nums;
    font-size: var(--fs-sm); color: var(--color-faint);
    margin-top: 4px; letter-spacing: var(--ls-normal);
}

/* ────────────────────────────────────────────────────────────── */
/*  1. DECISION — hero                                            */
/* ────────────────────────────────────────────────────────────── */
.desk-decision {
    padding: 4px 0 20px;
    margin-bottom: 28px;
    border-bottom: 1px solid var(--color-border);
    position: relative;
}
.desk-decision .word {
    font-family: var(--font-serif); font-weight: 500;
    font-size: 88px; line-height: 0.95; letter-spacing: -0.035em;
    display: inline-block;
}
.desk-decision .emoji { font-size: 44px; margin-left: 10px; vertical-align: 10px; }
.desk-decision .context {
    font-size: var(--fs-lg); color: var(--color-text); margin-top: 14px;
    line-height: 1.4; max-width: 680px; font-weight: 400;
}

/* Info icon — corner of the decision card. Hover reveals the criteria
   tooltip without taking real estate from the trigger block below. */
.desk-decision-info {
    position: absolute;
    top: 6px;
    right: 0;
    width: 22px; height: 22px;
    border-radius: 11px;
    border: 1px solid #C9C5BC;
    color: var(--color-muted);
    font-family: var(--font-sans);
    font-size: var(--fs-base);
    font-weight: 600;
    line-height: 20px;
    text-align: center;
    cursor: help;
    background: var(--color-bg);
    transition: background 0.12s, color 0.12s, border-color 0.12s;
}
.desk-decision-info:hover {
    background: #F1F5F9;
    color: var(--color-text);
    border-color: var(--color-muted);
}
.desk-decision-info-tooltip {
    visibility: hidden;
    opacity: 0;
    position: absolute;
    top: 30px; right: 0;
    width: 320px;
    background: var(--color-text);
    color: var(--color-bg);
    padding: 12px 14px;
    border-radius: 6px;
    font-family: var(--font-sans);
    font-size: var(--fs-sm);
    line-height: 1.45;
    z-index: 100;
    box-shadow: 0 4px 14px rgba(0,0,0,0.18);
    transition: opacity 0.15s;
    pointer-events: none;
}
.desk-decision-info:hover + .desk-decision-info-tooltip,
.desk-decision-info-tooltip:hover {
    visibility: visible;
    opacity: 1;
    pointer-events: auto;
}
.desk-decision-info-tooltip .tt-title {
    font-family: var(--font-serif);
    font-size: var(--fs-md); font-weight: 600;
    margin-bottom: 4px;
}
.desk-decision-info-tooltip .tt-tagline {
    font-size: var(--fs-sm); color: #C9C5BC; margin-bottom: 8px;
}
.desk-decision-info-tooltip ul {
    margin: 0; padding-left: 16px;
}
.desk-decision-info-tooltip li {
    margin: 3px 0; color: var(--color-bg); font-size: 11.5px;
}
.desk-decision-info-tooltip .tt-footer {
    margin-top: 10px;
    padding-top: 8px;
    border-top: 1px solid #2A2725;
    font-size: var(--fs-sm);
    color: var(--color-faintest);
}

/* ────────────────────────────────────────────────────────────── */
/*  2. TRIGGER — most important actionable                        */
/* ────────────────────────────────────────────────────────────── */
.desk-trigger-block { margin: 8px 0 24px; }
.desk-trigger-label {
    font-family: var(--font-sans);
    font-size: var(--fs-sm); font-weight: 600; letter-spacing: var(--ls-caps-xxl);
    text-transform: uppercase; color: var(--color-text);
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 8px;
}
.desk-trigger-label .em { font-size: var(--fs-base); }
.desk-trigger-text {
    font-family: var(--font-serif); font-weight: 500;
    font-size: var(--fs-xl); line-height: 1.25; color: var(--color-text);
    letter-spacing: -0.01em;
}
.desk-trigger-text b {
    font-family: var(--font-mono); font-style: normal;
    font-weight: 600; font-variant-numeric: tabular-nums;
    background: #E8F1FF; padding: 0 6px; border-radius: 2px;
    font-size: 28px;
}

/* ────────────────────────────────────────────────────────────── */
/*  3. INVALIDATION — binary, directly under trigger              */
/* ────────────────────────────────────────────────────────────── */
.desk-invalidation {
    margin: 4px 0 36px;
    padding: 10px 14px;
    background: var(--color-surface-warning);
    border-left: 3px solid var(--color-negative);
    border-radius: 2px;
}
.desk-invalidation .label {
    font-family: var(--font-sans);
    font-size: var(--fs-sm); font-weight: 600; letter-spacing: var(--ls-caps-xxl);
    text-transform: uppercase; color: var(--color-warning);
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 3px;
}
.desk-invalidation .label .em { font-size: var(--fs-base); }
.desk-invalidation .text {
    font-size: var(--fs-lg); color: var(--color-body); line-height: 1.4;
}
.desk-invalidation .text b {
    font-family: var(--font-mono); font-weight: 600;
    font-variant-numeric: tabular-nums; color: var(--color-text);
}

/* ────────────────────────────────────────────────────────────── */
/*  Read of the tape — concise current-state facts (Avoid layout) */
/* ────────────────────────────────────────────────────────────── */
.desk-tape-read {
    margin: 8px 0 16px;
    padding: 12px 16px;
    background: var(--color-surface);
    border: 1px solid var(--color-border);
    border-radius: 3px;
}
.desk-tape-read .label {
    font-family: var(--font-sans);
    font-size: var(--fs-sm); font-weight: 600; letter-spacing: var(--ls-caps-xl);
    text-transform: uppercase; color: var(--color-muted);
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 10px;
}
.desk-tape-read .label .em { font-size: var(--fs-base); }
.desk-tape-read .row {
    display: flex; justify-content: space-between; align-items: baseline;
    padding: 6px 0;
    border-top: 1px dashed var(--color-border-soft);
}
.desk-tape-read .row:first-of-type { border-top: none; }
.desk-tape-read .row .k {
    font-family: var(--font-sans);
    font-size: var(--fs-sm); font-weight: 600; letter-spacing: var(--ls-caps-sm);
    text-transform: uppercase; color: var(--color-faint);
    flex-shrink: 0; min-width: 90px;
}
.desk-tape-read .row .v {
    font-family: var(--font-mono); font-variant-numeric: tabular-nums;
    font-size: var(--fs-base); line-height: 1.4;
    text-align: right;
}
.desk-tape-read .row .v b {
    font-family: var(--font-mono); font-weight: 600;
    color: var(--color-text);
}

/* ────────────────────────────────────────────────────────────── */
/*  Avoid-state replacements for trigger / invalidation            */
/* ────────────────────────────────────────────────────────────── */
.desk-avoid-reasons {
    margin: 8px 0 20px;
    padding: 14px 16px;
    background: var(--color-surface-warning);
    border-left: 3px solid var(--color-negative);
    border-radius: 8px;
    box-shadow: inset 0 0 0 1px rgba(255, 77, 109, 0.06);
}
.desk-avoid-reasons .label {
    font-family: var(--font-sans);
    font-size: var(--fs-sm); font-weight: 600; letter-spacing: var(--ls-caps-xxl);
    text-transform: uppercase; color: var(--color-warning);
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 8px;
}
.desk-avoid-reasons .label .em { font-size: var(--fs-base); }
.desk-avoid-reasons ul {
    margin: 0; padding: 0; list-style: none;
}
.desk-avoid-reasons li {
    font-size: var(--fs-md); color: var(--color-body); line-height: 1.45;
    padding: 4px 0 4px 16px; position: relative;
}
.desk-avoid-reasons li:before {
    content: '–'; position: absolute; left: 0; top: 4px;
    color: var(--color-fainter);
}
.desk-avoid-reasons li b {
    font-family: var(--font-mono); font-weight: 600;
    font-variant-numeric: tabular-nums; color: var(--color-text);
    font-size: var(--fs-md);
}

.desk-reconsider {
    margin: 4px 0 36px;
    padding: 14px 16px;
    background: #F0FFF6;
    border-left: 3px solid var(--color-positive);
    border-radius: 8px;
    box-shadow: inset 0 0 0 1px rgba(22, 163, 74, 0.07);
}
.desk-reconsider .label {
    font-family: var(--font-sans);
    font-size: var(--fs-sm); font-weight: 600; letter-spacing: var(--ls-caps-xxl);
    text-transform: uppercase; color: var(--color-positive);
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 8px;
}
.desk-reconsider .label .em { font-size: var(--fs-base); }
.desk-reconsider ul {
    margin: 0; padding: 0; list-style: none;
}
.desk-reconsider li {
    font-size: var(--fs-md); color: var(--color-body); line-height: 1.45;
    padding: 4px 0 4px 16px; position: relative;
}
.desk-reconsider li:before {
    content: '→'; position: absolute; left: 0; top: 3px;
    color: var(--color-positive); font-weight: 500;
}
.desk-reconsider li b {
    font-family: var(--font-mono); font-weight: 600;
    font-variant-numeric: tabular-nums; color: var(--color-text);
    font-size: var(--fs-md);
}

/* ────────────────────────────────────────────────────────────── */
/*  4. IF TRIGGER HITS — conditional trade plan, secondary        */
/* ────────────────────────────────────────────────────────────── */
.desk-plan-label {
    font-family: var(--font-sans);
    font-size: var(--fs-sm); font-weight: 600; letter-spacing: var(--ls-caps-xxl);
    text-transform: uppercase; color: var(--color-muted);
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 8px;
}
.desk-plan-label .em { font-size: var(--fs-sm); }
.desk-plan-row {
    display: flex; justify-content: space-between; align-items: baseline;
    padding: 6px 0; border-top: 1px dashed var(--color-border);
}
.desk-plan-row:last-child { border-bottom: 1px dashed var(--color-border); }
.desk-plan-row .k { font-size: var(--fs-base); color: var(--color-muted); font-weight: 500; }
.desk-plan-row .v {
    font-family: var(--font-mono); font-variant-numeric: tabular-nums;
    font-size: var(--fs-base); font-weight: 500; color: var(--color-text);
}
.desk-plan-row .d {
    font-family: var(--font-mono); font-variant-numeric: tabular-nums;
    font-size: var(--fs-sm); margin-left: 6px; color: var(--color-muted);
}
.desk-plan-row .sub {
    font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--color-fainter);
    display: block; text-align: right; margin-top: 1px;
}

/* ────────────────────────────────────────────────────────────── */
/*  5. PM VIEW — two-layer, right column, separated               */
/* ────────────────────────────────────────────────────────────── */
.desk-pm-container {
    border-left: 1px solid var(--color-border);
    padding-left: 24px;
    min-height: calc(100vh - 132px);
    height: 100%;
}
[data-testid="stHorizontalBlock"]:has(.desk-decision) > [data-testid="column"]:nth-child(2) {
    border-left: 1px solid var(--color-border);
    padding-left: 24px;
    min-height: calc(100vh - 132px);
    position: relative;
}
.desk-pm-header {
    font-family: var(--font-sans);
    font-size: var(--fs-sm); font-weight: 600; letter-spacing: var(--ls-caps-xl);
    text-transform: uppercase; color: var(--color-muted);
    height: 58px;
    margin-bottom: 14px; padding: 0 0 10px 0;
    border-bottom: 1px solid var(--color-border);
    display: flex; justify-content: space-between; align-items: flex-start; gap: 12px;
    width: 100%;
    box-sizing: border-box;
}
.pm-refresh-link {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    flex: 0 0 30px;
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.72) !important;
    border: 1px solid var(--color-border);
    color: var(--color-muted) !important;
    text-decoration: none !important;
    font-family: var(--font-mono);
    font-size: var(--fs-base);
    line-height: 1;
    letter-spacing: var(--ls-normal);
}
.pm-refresh-link:hover {
    border-color: var(--color-blue);
    color: var(--color-text) !important;
    box-shadow: 0 8px 18px rgba(37, 99, 235, 0.10);
}
.desk-pm-header .em { font-size: var(--fs-base); margin-right: 6px; }
.desk-pm-header .src {
    font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--color-faintest);
    letter-spacing: var(--ls-normal); text-transform: none; font-weight: 400;
}
.desk-pm-block { margin-bottom: 16px; }
.desk-pm-block .lb {
    font-family: var(--font-sans);
    font-size: var(--fs-xs); font-weight: 600; letter-spacing: var(--ls-caps-lg);
    text-transform: uppercase; color: var(--color-faint); margin-bottom: 5px;
}
.desk-quality-info {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    margin-left: 6px;
    border: 1px solid var(--color-border);
    border-radius: 50%;
    color: var(--color-faint);
    background: #FFFFFF;
    font-family: var(--font-serif);
    font-size: 12px;
    font-style: italic;
    font-weight: 700;
    line-height: 1;
    cursor: help;
    text-transform: none;
    letter-spacing: 0;
}
.desk-quality-info:hover {
    color: var(--color-text);
    border-color: var(--color-muted);
}
.desk-quality-card {
    margin: -2px 0 20px 0;
}
.desk-pm-block .body {
    font-family: var(--font-serif); font-style: normal;
    font-size: var(--fs-md); line-height: 1.5; color: var(--color-body);
}
.desk-pm-item {
    font-family: var(--font-serif); font-style: normal;
    font-size: var(--fs-md); line-height: 1.45; color: var(--color-body);
    padding: 3px 0 3px 16px; position: relative;
}
.desk-pm-item:before {
    content: '–'; position: absolute; left: 0; top: 1px;
    font-family: var(--font-sans); font-style: normal;
    color: var(--color-fainter); font-weight: 400;
}
.desk-pm-deep {
    margin-top: 16px;
    padding-top: 16px;
    padding-left: 20px;
    border-top: 1px dashed var(--color-border);
    border-left: 1px solid var(--color-border);
}
.desk-pm-thesis {
    margin-top: 18px;
    padding: 16px 16px 2px;
    border: 1px solid var(--color-border-soft);
    border-left: 3px solid var(--color-purple);
    border-radius: 8px;
    background: rgba(124, 58, 237, 0.035);
}
.desk-pm-thesis p {
    margin: 0 0 12px;
    font-family: var(--font-serif);
    font-size: var(--fs-md);
    line-height: 1.55;
    color: var(--color-body);
}
.desk-pm-thesis p:last-child { margin-bottom: 0; }
.desk-pm-deep .sub-lb {
    font-family: var(--font-sans);
    font-size: var(--fs-xs); font-weight: 600; letter-spacing: var(--ls-caps-lg);
    text-transform: uppercase; color: var(--color-faint); margin: 14px 0 4px;
}
.desk-pm-deep .sub-body {
    font-family: var(--font-serif); font-style: normal;
    font-size: var(--fs-base); line-height: 1.5; color: var(--color-body);
}
.desk-pm-deep .variant-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
    margin-top: 4px;
}
.desk-pm-deep .variant-card {
    border: 1px solid var(--color-border); border-radius: 8px;
    padding: 9px 11px;
    background: var(--color-surface);
}
.desk-pm-deep .variant-card .lb-bull { color: var(--color-positive); }
.desk-pm-deep .variant-card .lb-bear { color: var(--color-warning); }
.desk-pm-deep .variant-card .lb {
    font-size: var(--fs-xs); font-weight: 600; letter-spacing: var(--ls-caps-lg);
    text-transform: uppercase; margin-bottom: 3px;
}
.desk-pm-deep .variant-card .body {
    font-family: var(--font-serif); font-style: normal;
    font-size: var(--fs-base); line-height: 1.45; color: var(--color-body);
}

/* Follow-up chat */
[class*="st-key-chat_input_"] {
    width: 100% !important;
}
[class*="st-key-chat_input_"] textarea {
    display: block !important;
    width: 100% !important;
    min-height: 92px !important;
    padding: 14px 16px !important;
    border-radius: 10px !important;
    border: 1px solid var(--color-border) !important;
    background: rgba(255, 255, 255, 0.86) !important;
    color: var(--color-body) !important;
    font-family: var(--font-sans) !important;
    font-size: var(--fs-base) !important;
    line-height: 1.45 !important;
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.7), 0 8px 20px rgba(15, 23, 42, 0.04) !important;
}
[class*="st-key-chat_input_"] textarea:focus {
    border-color: var(--color-blue) !important;
    box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.10) !important;
}
[class*="st-key-chat_send_"] {
    width: 100% !important;
    margin: 10px auto 0 !important;
}
[class*="st-key-chat_send_"] > div {
    display: flex !important;
    justify-content: center !important;
}
[class*="st-key-chat_send_"] button {
    text-align: center !important;
    justify-content: center !important;
    background: linear-gradient(135deg, var(--color-text), #263241) !important;
    color: var(--color-bg) !important;
    border: none !important;
    font-family: var(--font-sans) !important;
    font-size: var(--fs-base) !important;
    font-weight: 500 !important;
    border-radius: 999px !important;
    letter-spacing: 0.02em !important;
    min-height: 36px !important;
    height: 36px !important;
    width: auto !important;
    min-width: 94px !important;
    padding: 7px 18px !important;
    white-space: nowrap !important;
}
[class*="st-key-chat_send_"] button p {
    white-space: nowrap !important;
    margin: 0 !important;
}
.desk-chat-history {
    margin: 0 0 14px;
    padding: 12px 14px;
    background: rgba(244, 247, 251, 0.74);
    border: 1px solid var(--color-border);
    border-radius: 8px;
}
.desk-chat-history-title {
    font-family: var(--font-sans);
    font-size: var(--fs-xs);
    font-weight: 600;
    letter-spacing: var(--ls-caps-lg);
    text-transform: uppercase;
    color: var(--color-faint);
    margin-bottom: 6px;
}
.desk-chat-history-item {
    padding: 6px 0;
    border-top: 1px dashed var(--color-border);
}
.desk-chat-history-item:first-of-type { border-top: none; }
.desk-chat-history-q {
    font-size: var(--fs-base);
    color: var(--color-body);
    line-height: 1.35;
}
.desk-chat-history-a {
    margin-top: 2px;
    font-size: var(--fs-sm);
    color: var(--color-muted);
    line-height: 1.4;
}
[class*="st-key-clear_chat_"] {
    width: 100% !important;
    margin: 6px auto 0 !important;
}
[class*="st-key-clear_chat_"] > div {
    display: flex !important;
    justify-content: center !important;
}
[class*="st-key-pm_expand_"] > div,
[class*="st-key-pm_collapse_"] > div {
    margin: 0 !important;
    padding: 0 !important;
    display: flex !important;
    justify-content: flex-start !important;
}
[class*="st-key-pm_expand_"] button,
[class*="st-key-pm_collapse_"] button {
    margin: 10px 0 0 !important;
    padding: 7px 12px !important;
    font-family: var(--font-sans) !important;
    font-size: var(--fs-sm) !important;
    font-weight: 600 !important;
    color: var(--color-body) !important;
    background: transparent !important;
    border: 1px solid var(--color-border) !important;
    border-radius: 3px !important;
    letter-spacing: 0 !important;
    text-transform: none !important;
    min-height: 34px !important;
    width: auto !important;
}
[class*="st-key-pm_expand_"] button:hover,
[class*="st-key-pm_collapse_"] button:hover {
    background: var(--color-text) !important;
    color: var(--color-bg) !important;
    border-color: var(--color-text) !important;
}
div.stButton > button {
    background: transparent; border: 1px solid var(--color-border); color: var(--color-text);
    font-family: var(--font-mono); font-size: var(--fs-sm); font-weight: 500;
    border-radius: 3px; padding: 6px 10px;
}
.main div.stButton > button:hover {
    background: var(--color-text); color: var(--color-bg); border-color: var(--color-text);
}

/* Technical details expander — full bordered container */
div[data-testid="stExpander"],
details.stExpander,
div.stExpander {
    border: 1px solid var(--color-border) !important;
    border-radius: 4px !important;
    background: var(--color-surface) !important;
    margin-top: 14px !important;
    overflow: hidden;
}
div[data-testid="stExpander"]:focus-within,
details.stExpander:focus-within,
div.stExpander:focus-within {
    border-color: var(--color-border) !important;
    box-shadow: none !important;
}
div[data-testid="stExpander"] > details:focus,
div[data-testid="stExpander"] summary:focus,
div[data-testid="stExpander"] summary:focus-visible {
    outline: none !important;
    box-shadow: none !important;
}
div[data-testid="stExpander"] > details,
div[data-testid="stExpander"] details {
    border: none !important;
    background: transparent !important;
}
div[data-testid="stExpander"] summary,
details.stExpander summary,
div[data-testid="stExpander"] details summary {
    font-size: var(--fs-base) !important; font-weight: 500 !important;
    color: var(--color-body) !important; letter-spacing: var(--ls-normal) !important;
    padding: 12px 16px !important;
    list-style: none;
}
div[data-testid="stExpanderDetails"],
div[data-testid="stExpander"] div[data-testid="stExpanderDetails"] {
    padding: 4px 16px 14px 16px !important;
    border-top: 1px solid var(--color-border);
}
div.streamlit-expanderHeader {
    font-size: var(--fs-base) !important; font-weight: 500 !important;
    color: var(--color-body) !important; letter-spacing: var(--ls-normal) !important;
}

/* ────────────────────────────────────────────────────────────── */
/*  Earnings banner — shown when earnings within 7 days           */
/* ────────────────────────────────────────────────────────────── */
.desk-earnings-banner {
    background: var(--color-surface-trigger);
    border: 1px solid #CFE0FF;
    border-radius: 3px;
    padding: 10px 14px;
    margin: 0 0 20px;
    display: flex; align-items: center; gap: 10px;
    font-size: var(--fs-base); color: var(--color-warning-text);
    line-height: 1.4;
}
.desk-earnings-banner .em { font-size: var(--fs-md); }
.desk-earnings-banner b {
    font-family: var(--font-mono); font-weight: 600;
    font-variant-numeric: tabular-nums; color: #3F2E0A;
}

/* ────────────────────────────────────────────────────────────── */
/*  Meta strip — sector · market cap · short interest · earnings  */
/* ────────────────────────────────────────────────────────────── */
.desk-meta-strip {
    display: flex; flex-wrap: wrap; gap: 18px;
    padding: 10px 0 4px;
    margin-bottom: 20px;
    border-bottom: 1px solid var(--color-border);
}
.desk-meta-item {
    display: flex; flex-direction: column; gap: 1px;
}
.desk-meta-item .lb {
    font-family: var(--font-sans);
    font-size: var(--fs-xs); font-weight: 600; letter-spacing: var(--ls-caps-lg);
    text-transform: uppercase; color: var(--color-faint);
}
.desk-meta-item .v {
    font-family: var(--font-mono); font-variant-numeric: tabular-nums;
    font-size: var(--fs-sm); font-weight: 500; color: var(--color-body);
}

/* ────────────────────────────────────────────────────────────── */
/*  Stat cards — earnings, analyst consensus, Lynch check          */
/* ────────────────────────────────────────────────────────────── */
.desk-stat-card {
    margin: 12px 0;
    padding: 13px 15px;
    background: rgba(255, 255, 255, 0.94);
    border: 1px solid var(--color-border);
    border-radius: 8px;
    box-shadow: 0 10px 22px rgba(15, 23, 42, 0.04);
}
.desk-stat-card .label {
    font-family: var(--font-sans);
    font-size: var(--fs-xs); font-weight: 600; letter-spacing: var(--ls-caps-lg);
    text-transform: uppercase; color: var(--color-muted);
    margin-bottom: 8px;
}
.desk-stat-card .row {
    display: flex; justify-content: space-between; align-items: baseline;
    padding: 6px 0;
    font-family: var(--font-sans);
    font-size: var(--fs-base);
    color: var(--color-body);
}
.desk-stat-card .row + .row {
    border-top: 1px dashed var(--color-border-soft);
}
.desk-stat-card .row .v {
    font-family: var(--font-sans);
    font-size: var(--fs-base);
    color: var(--color-text);
}
/* Number portions inside .v get mono treatment via .num inner span */
.desk-stat-card .row .v .num {
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
    font-weight: 500;
    margin-right: 4px;
}

/* Technical-read variant: prose inside a stat card */
.desk-stat-card-read .read-body {
    font-family: var(--font-sans);
    font-size: var(--fs-base); line-height: 1.55; color: var(--color-body);
    padding: 4px 0 2px;
}
.desk-stat-card-read .read-body p {
    margin: 0 0 10px; padding: 0;
}
.desk-stat-card-read .read-body p:last-child { margin-bottom: 0; }
.desk-stat-card-read .read-body b {
    font-family: var(--font-mono); font-weight: 600;
    font-variant-numeric: tabular-nums; color: var(--color-text);
    font-size: var(--fs-base);
}
.research-link {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    margin: 4px 0 14px;
    padding: 8px 12px;
    border: 1px solid var(--color-border);
    border-radius: 999px;
    color: var(--color-text) !important;
    text-decoration: none !important;
    font-family: var(--font-sans);
    font-size: var(--fs-sm);
    font-weight: 600;
    background: rgba(255, 255, 255, 0.86);
    box-shadow: 0 8px 18px rgba(15, 23, 42, 0.045);
}
.research-link:hover {
    border-color: var(--color-blue);
    background: #EFF6FF;
}
.research-page {
    max-width: 1180px;
    margin: 0 auto 80px;
}
.research-page .hero {
    border-bottom: 1px solid var(--color-border);
    padding: 0 0 22px;
    margin-bottom: 26px;
    background: transparent;
    box-shadow: none;
}
.research-page .eyebrow,
.research-section .eyebrow {
    font-family: var(--font-mono);
    font-size: var(--fs-xs);
    letter-spacing: var(--ls-caps-xl);
    text-transform: uppercase;
    color: var(--color-muted);
    font-weight: 600;
}
.research-page h1 {
    font-family: var(--font-sans);
    font-size: 46px;
    line-height: 1;
    font-weight: 820;
    letter-spacing: -0.02em;
    margin: 8px 0 10px;
}
.research-page .deck {
    font-family: var(--font-sans);
    font-size: 19px;
    line-height: 1.42;
    color: var(--color-body);
    max-width: 920px;
}
.research-grid {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 0;
    margin: 20px 0 0;
    border: 1px solid var(--color-border);
    border-radius: 6px;
    overflow: hidden;
    background: #FFFFFF;
}
.research-metric-group {
    padding: 12px 14px;
    border-right: 1px solid var(--color-border-soft);
    min-height: 112px;
}
.research-metric-group:last-child {
    border-right: 0;
}
.research-metric-group .group-title {
    font-family: var(--font-mono);
    font-size: var(--fs-xs);
    letter-spacing: var(--ls-caps-xl);
    text-transform: uppercase;
    color: var(--color-muted);
    font-weight: 750;
    margin-bottom: 8px;
}
.research-metric-row {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 5px 0;
    border-top: 1px solid var(--color-border-soft);
    align-items: baseline;
}
.research-metric-row:first-of-type {
    border-top: 0;
}
.research-metric-row .k {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--color-faint);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 650;
}
.research-metric-row .v {
    font-family: var(--font-mono);
    font-size: 15px;
    color: var(--color-text);
    font-weight: 780;
    text-align: right;
    font-variant-numeric: tabular-nums;
}
.research-layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(340px, 0.72fr);
    gap: 34px;
}
.research-section {
    border-top: 1px solid var(--color-border);
    padding-top: 16px;
    margin-top: 16px;
}
.research-section h2 {
    font-family: var(--font-sans);
    font-size: 22px;
    line-height: 1.16;
    font-weight: 760;
    letter-spacing: -0.01em;
    margin: 7px 0 9px;
}
.research-section p,
.research-section li {
    font-size: var(--fs-base);
    line-height: 1.55;
    color: var(--color-body);
}
.research-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--fs-sm);
}
.research-table th,
.research-table td {
    border-bottom: 1px solid var(--color-border-soft);
    padding: 8px 6px;
    text-align: right;
}
.research-table th:first-child,
.research-table td:first-child { text-align: left; }
.research-table th {
    font-family: var(--font-mono);
    font-size: var(--fs-xs);
    letter-spacing: var(--ls-caps-lg);
    text-transform: uppercase;
    color: var(--color-muted);
}
.research-table td {
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
}
.research-data-pack {
    border: 1px solid var(--color-border);
    border-radius: 6px;
    background: #FFFFFF;
    padding: 13px 14px 4px;
    margin-top: 20px;
}
.research-data-pack > .eyebrow {
    margin-bottom: 2px;
}
.research-data-pack .research-section {
    margin-top: 12px;
    padding-top: 12px;
}
.desk-data-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin: 10px 0 0;
}
.desk-data-chip {
    display: inline-flex;
    gap: 5px;
    align-items: baseline;
    border: 1px solid var(--color-border);
    border-radius: 4px;
    background: #FFFFFF;
    color: var(--color-muted);
    padding: 4px 7px;
    font-family: var(--font-mono);
    font-size: 11px;
    line-height: 1.25;
}
.desk-data-chip b {
    color: var(--color-text);
    font-weight: 800;
}
.desk-data-chip.warn {
    border-color: #CFE0FF;
    background: #F6F9FF;
}
.desk-data-chip.fresh {
    border-color: rgba(0, 168, 112, 0.22);
    background: rgba(0, 168, 112, 0.06);
}
.desk-data-chip.stale {
    border-color: #F5B5B5;
    background: #FFF7F7;
}
.desk-data-chip.info {
    border-color: #CFE0FF;
    background: #F6F9FF;
}
.desk-data-chip.neutral {
    border-color: var(--color-border);
    background: #FFFFFF;
}
.desk-freshness-panel {
    border: 1px solid var(--color-border);
    border-radius: 8px;
    background: #FFFFFF;
    padding: 12px 13px;
    margin: 0 0 12px;
}
.desk-freshness-title {
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 850;
    letter-spacing: var(--ls-caps-lg);
    text-transform: uppercase;
    color: var(--color-muted);
    margin-bottom: 8px;
}
.desk-freshness-row {
    display: flex;
    justify-content: space-between;
    gap: 14px;
    padding: 5px 0;
    border-top: 1px dashed var(--color-border-soft);
    font-family: var(--font-mono);
    font-size: 11px;
    line-height: 1.25;
}
.desk-freshness-row:first-of-type {
    border-top: 0;
}
.desk-freshness-row .k {
    color: var(--color-faint);
    font-weight: 800;
    letter-spacing: var(--ls-caps-sm);
    text-transform: uppercase;
}
.desk-freshness-row .v {
    color: var(--color-text);
    text-align: right;
    max-width: 62%;
    overflow-wrap: anywhere;
}
.desk-freshness-row.stale .v { color: var(--color-negative); }
.desk-freshness-row.warn .v { color: var(--color-warning-text); }
.desk-freshness-row.fresh .v { color: var(--color-positive); }
.desk-refresh-receipt {
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px dashed var(--color-border-soft);
    font-family: var(--font-mono);
    font-size: 11px;
    line-height: 1.35;
    color: var(--color-positive);
}
.desk-refresh-receipt.warn {
    color: var(--color-warning-text);
}
.position-decision-panel {
    border: 1px solid var(--color-border);
    border-radius: 8px;
    background: #FFFFFF;
    margin: 16px 0 18px;
    padding: 14px 16px;
}
.position-decision-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
    margin-bottom: 10px;
}
.position-decision-label {
    font-family: var(--font-mono);
    font-size: var(--fs-xs);
    font-weight: 800;
    letter-spacing: var(--ls-caps-lg);
    text-transform: uppercase;
    color: var(--color-muted);
    margin-bottom: 5px;
}
.position-decision-action {
    font-family: var(--font-sans);
    font-size: 24px;
    font-weight: 850;
    line-height: 1.05;
}
.position-decision-meta {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--color-muted);
    text-align: right;
    white-space: nowrap;
}
.position-decision-summary {
    font-size: var(--fs-md);
    line-height: 1.55;
    color: var(--color-body);
    margin-bottom: 10px;
}
.position-decision-stats {
    display: grid;
    grid-template-columns: repeat(6, minmax(0, 1fr));
    border-top: 1px solid var(--color-border-soft);
    padding-top: 10px;
    gap: 8px;
}
.position-decision-stat .k {
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 800;
    letter-spacing: var(--ls-caps-sm);
    text-transform: uppercase;
    color: var(--color-faint);
}
.position-decision-stat .v {
    margin-top: 3px;
    font-family: var(--font-mono);
    font-size: 13px;
    font-weight: 800;
    color: var(--color-text);
    font-variant-numeric: tabular-nums;
}
.tech-memo-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
    margin-bottom: 14px;
}
.tech-memo-table {
    border: 1px solid var(--color-border);
    border-radius: 6px;
    background: #FFFFFF;
    overflow: hidden;
}
.tech-memo-title {
    padding: 8px 10px;
    border-bottom: 1px solid var(--color-border-soft);
    font-family: var(--font-mono);
    font-size: var(--fs-xs);
    font-weight: 800;
    letter-spacing: var(--ls-caps-lg);
    text-transform: uppercase;
    color: var(--color-muted);
}
.tech-memo-row {
    display: grid;
    grid-template-columns: minmax(82px, 0.72fr) minmax(0, 1.28fr);
    gap: 12px;
    padding: 7px 10px;
    border-top: 1px solid var(--color-border-soft);
    align-items: baseline;
    font-family: var(--font-mono);
    font-size: var(--fs-sm);
}
.tech-memo-title + .tech-memo-row {
    border-top: 0;
}
.tech-memo-row .k {
    color: var(--color-faint);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 650;
}
.tech-memo-row .v {
    text-align: right;
    font-variant-numeric: tabular-nums;
}
.key-level-summary {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 8px;
    margin: 6px 0 14px;
}
.key-level-card {
    border: 1px solid var(--color-border);
    border-radius: 6px;
    background: #FFFFFF;
    padding: 9px 10px;
    min-height: 82px;
}
.key-level-label {
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 800;
    letter-spacing: var(--ls-caps-lg);
    text-transform: uppercase;
    color: var(--color-muted);
    margin-bottom: 6px;
}
.key-level-price {
    font-family: var(--font-mono);
    font-size: 18px;
    font-weight: 850;
    color: var(--color-text);
    font-variant-numeric: tabular-nums;
}
.key-level-note {
    margin-top: 5px;
    font-size: 11px;
    line-height: 1.35;
    color: var(--color-muted);
}
@media (max-width: 900px) {
    .tech-memo-grid {
        grid-template-columns: 1fr;
    }
    .key-level-summary {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
}
.watch-queue-grid {
    display: grid;
    grid-template-columns: repeat(7, minmax(0, 1fr));
    gap: 8px;
    margin: 10px 0 14px;
}
.watch-queue-card {
    border: 1px solid var(--color-border);
    border-radius: 6px;
    background: #FFFFFF;
    padding: 9px 10px;
    min-height: 78px;
}
.watch-queue-label {
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 800;
    letter-spacing: var(--ls-caps-lg);
    text-transform: uppercase;
    margin-bottom: 5px;
}
.watch-queue-count {
    font-family: var(--font-mono);
    font-size: 24px;
    font-weight: 850;
    line-height: 1;
    color: var(--color-text);
}
.watch-queue-preview {
    margin-top: 5px;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--color-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.watchlist-column-key {
    margin: 12px 0 8px;
    border: 1px solid var(--color-border);
    border-radius: 6px;
    background: #FFFFFF;
    padding: 0;
}
.watchlist-column-key summary {
    cursor: pointer;
    padding: 8px 10px;
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 800;
    letter-spacing: var(--ls-caps-lg);
    text-transform: uppercase;
    color: var(--color-muted);
    list-style-position: inside;
}
.watchlist-column-key div {
    border-top: 1px solid var(--color-border-soft);
    padding: 9px 12px 10px;
    font-size: 13px;
    line-height: 1.6;
    color: var(--color-muted);
}
.watchlist-column-key b {
    font-family: var(--font-mono);
    color: var(--color-text);
    font-size: 12px;
}
.watchlist-dissent-panel {
    border: 1px solid var(--color-border);
    border-left: 3px solid var(--color-blue);
    border-radius: 6px;
    background: #FFFFFF;
    padding: 10px 12px;
    margin: 12px 0 10px;
}
.watchlist-dissent-title {
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 850;
    letter-spacing: var(--ls-caps-lg);
    text-transform: uppercase;
    color: var(--color-blue);
    margin-bottom: 5px;
}
.watchlist-dissent-copy {
    font-family: var(--font-sans);
    font-size: var(--fs-sm);
    color: var(--color-muted);
    line-height: 1.45;
    margin-bottom: 7px;
}
.watchlist-dissent-list {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 6px 14px;
}
.watchlist-dissent-item {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--color-body);
    line-height: 1.45;
    min-width: 0;
}
.watchlist-dissent-item a {
    color: var(--color-text) !important;
    font-weight: 850;
    text-decoration: none !important;
}
.watchlist-dissent-note {
    margin-top: 2px;
    font-family: var(--font-sans);
    font-size: var(--fs-sm);
    color: var(--color-muted);
    line-height: 1.4;
}
.watchlist-review-link {
    color: var(--color-blue) !important;
    font-weight: 850;
    text-decoration: none !important;
}
.watchlist-review-link:hover {
    text-decoration: underline !important;
}
.watchlist-grid-row {
    min-width: 0;
}
.watchlist-grid-row > * {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
}
.watchlist-grid-row span,
.watchlist-grid-row a {
    white-space: nowrap;
}
.watchlist-grid-row .state-pill {
    max-width: 100%;
}
.watchlist-control-note {
    font-family: var(--font-sans);
    font-size: var(--fs-sm);
    color: var(--color-muted);
    line-height: 1.35;
    padding-top: 5px;
}
div[data-baseweb="select"] > div,
div[data-baseweb="input"] > div,
div[data-baseweb="popover"],
ul[role="listbox"] {
    background: #FFFFFF !important;
    background-color: #FFFFFF !important;
}
div[data-baseweb="select"] [role="button"],
div[data-baseweb="select"] input,
div[data-baseweb="input"] input {
    background: #FFFFFF !important;
    background-color: #FFFFFF !important;
}
@media (max-width: 900px) {
    .research-grid {
        grid-template-columns: 1fr;
    }
    .research-metric-group {
        border-right: 0;
        border-bottom: 1px solid var(--color-border-soft);
    }
    .research-metric-group:last-child {
        border-bottom: 0;
    }
    .research-layout { grid-template-columns: 1fr; }
    .research-page h1 { font-size: 42px; }
    .watchlist-dissent-list { grid-template-columns: 1fr; }
    .watchlist-grid-row {
        font-size: 11px !important;
        column-gap: 7px !important;
    }
    .watch-queue-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
.desk-chart-label {
    font-family: var(--font-sans);
    font-size: var(--fs-sm); font-weight: 600; letter-spacing: var(--ls-caps-xxl);
    text-transform: uppercase; color: var(--color-muted);
    margin: 32px 0 10px;
}

/* ────────────────────────────────────────────────────────────── */
/*  Technical commentary — sits under PM view in right column      */
/* ────────────────────────────────────────────────────────────── */
.desk-commentary {
    margin: 28px 0 0;
    padding-left: 24px;
    padding-top: 20px;
    border-top: 1px solid var(--color-border);
    border-left: 1px solid var(--color-border);
}
.desk-commentary-label {
    font-family: var(--font-sans);
    font-size: var(--fs-sm); font-weight: 600; letter-spacing: var(--ls-caps-xl);
    text-transform: uppercase; color: var(--color-muted);
    margin-bottom: 10px;
    display: flex; align-items: center; gap: 8px;
}
.desk-commentary-label .em { font-size: var(--fs-base); }
.desk-commentary-body {
    font-family: var(--font-sans);
    font-size: var(--fs-lg); line-height: 1.55; color: var(--color-body);
}
.desk-commentary-body p {
    margin: 0 0 12px; padding: 0;
}
.desk-commentary-body p:last-child { margin-bottom: 0; }
.desk-commentary-body b {
    font-family: var(--font-mono); font-weight: 600;
    font-variant-numeric: tabular-nums; color: var(--color-text);
    font-size: var(--fs-md);
}

/* ────────────────────────────────────────────────────────────── */
/*  Young visual system — brighter, cleaner, more product-native   */
/* ────────────────────────────────────────────────────────────── */
.stApp {
    background:
        linear-gradient(135deg, #FBFFFE 0%, #F4FBFF 42%, #FFF7FB 100%) !important;
}
.main .block-container {
    background: rgba(255, 255, 255, 0.58);
}
.desk-bar {
    background: rgba(255, 255, 255, 0.88) !important;
    border: 0 !important;
    border-bottom: 1px solid #DCE3EA !important;
    box-shadow: none !important;
}
.desk-bar .wordmark {
    color: #334155 !important;
}
.desk-bar .wordmark .arrow {
    color: #18C964 !important;
    text-shadow: 0 0 12px rgba(24, 201, 100, 0.45);
}
.desk-ticker-row,
.desk-pm-header {
    border-bottom: 2px solid rgba(15, 23, 42, 0.08) !important;
}
.desk-ticker-row .sym {
    font-size: 26px !important;
    font-weight: 750 !important;
    letter-spacing: 0 !important;
}
.desk-ticker-row .name,
.desk-ticker-row .meta-inline,
.desk-pm-header .src {
    color: #64748B !important;
}
.desk-ticker-row .price {
    font-size: 20px !important;
    font-weight: 750 !important;
}
.desk-decision {
    padding: 18px 0 24px !important;
    border-bottom: 2px solid rgba(15, 23, 42, 0.07) !important;
}
.desk-decision .word {
    font-family: var(--font-sans) !important;
    font-weight: 850 !important;
    letter-spacing: -0.045em !important;
}
.desk-decision .emoji {
    filter: drop-shadow(0 8px 18px rgba(255, 77, 109, 0.18));
}
.desk-decision .context {
    font-size: 19px !important;
    color: #111827 !important;
    max-width: 760px !important;
}
.desk-decision-info,
.pm-refresh-link {
    border-color: rgba(37, 99, 235, 0.18) !important;
    background: #FFFFFF !important;
    box-shadow: 0 10px 22px rgba(37, 99, 235, 0.08) !important;
}
.desk-mod,
.desk-cmp-badge,
.research-link {
    border-radius: 999px !important;
}
.desk-avoid-reasons,
.desk-reconsider,
.desk-dossier,
.desk-cmp,
.desk-stat-card,
.research-kpi,
.desk-pm-thesis {
    border-radius: 14px !important;
    box-shadow: 0 18px 42px rgba(15, 23, 42, 0.07) !important;
}
.desk-avoid-reasons {
    background: linear-gradient(135deg, #FFF5F7, #FFF8FA) !important;
    border-left: 4px solid #FF4D6D !important;
}
.desk-reconsider {
    background: linear-gradient(135deg, #EEFFF6, #F7FFFB) !important;
    border-left: 4px solid #18C964 !important;
}
.desk-dossier {
    border-left: 5px solid #2563EB !important;
    background: linear-gradient(135deg, rgba(255,255,255,0.98), rgba(246,250,255,0.98)) !important;
}
.desk-dossier-label,
.desk-cmp-header,
.desk-stat-card .label,
.research-page .eyebrow,
.research-section .eyebrow,
.desk-pm-block .lb {
    color: #475569 !important;
}
.desk-dossier-text {
    font-family: var(--font-sans) !important;
    font-size: 17px !important;
    line-height: 1.68 !important;
}
.desk-pm-thesis {
    background: linear-gradient(135deg, rgba(245, 243, 255, 0.98), rgba(236, 253, 245, 0.80)) !important;
    border-left: 5px solid #7C3AED !important;
}
.research-link {
    gap: 7px;
    border: 1px solid rgba(37, 99, 235, 0.18) !important;
    background: linear-gradient(135deg, #FFFFFF, #EFF6FF) !important;
    box-shadow: 0 12px 28px rgba(37, 99, 235, 0.12) !important;
}
.research-link:hover {
    transform: translateY(-1px);
    box-shadow: 0 16px 32px rgba(37, 99, 235, 0.16) !important;
}
[class*="st-key-chat_input_"] textarea {
    border-radius: 16px !important;
    border: 1px solid rgba(37, 99, 235, 0.18) !important;
    background: rgba(255,255,255,0.92) !important;
}
[class*="st-key-chat_send_"] button {
    background: linear-gradient(135deg, #111827 0%, #2563EB 58%, #06B6D4 100%) !important;
    box-shadow: 0 14px 26px rgba(37, 99, 235, 0.18) !important;
}
.research-page .hero {
    background: linear-gradient(135deg, rgba(255,255,255,0.98), rgba(239,246,255,0.95) 55%, rgba(255,247,251,0.96)) !important;
    border: 1px solid rgba(37, 99, 235, 0.12) !important;
    border-radius: 18px !important;
    box-shadow: 0 24px 60px rgba(15, 23, 42, 0.09) !important;
}
.research-page h1 {
    font-family: var(--font-sans) !important;
    font-weight: 850 !important;
    letter-spacing: -0.045em !important;
}
.research-page .deck {
    font-family: var(--font-sans) !important;
    color: #1F2937 !important;
}
.research-kpi {
    border-top: 3px solid #18C964 !important;
}

/* ────────────────────────────────────────────────────────────── */
/*  MOBILE RESPONSIVE — narrow viewports (≤768px)                  */
/*  Goal: app stays usable on a phone. Not optimal, but readable. */
/* ────────────────────────────────────────────────────────────── */
@media (max-width: 768px) {
    /* Mobile: the desktop sticky brand strip collides with the browser/
       Streamlit chrome and clips the ticker header. Let it scroll normally. */
    div[data-testid="stElementContainer"]:has(.desk-bar),
    div[data-testid="element-container"]:has(.desk-bar) {
        position: static !important;
        top: auto !important;
        z-index: auto !important;
    }
    .desk-bar {
        margin: 0 0 18px !important;
        padding: 9px 0 10px !important;
        position: relative !important;
    }

    /* Loosen the desktop max-width and reduce side padding */
    .main .block-container {
        padding-left: 0.8rem !important;
        padding-right: 0.8rem !important;
        font-size: var(--fs-md) !important;
    }

    /* Hero decision word: shrink so "Accumulate." doesn't overflow */
    .desk-decision .word {
        font-size: 56px !important;
        letter-spacing: -0.025em !important;
    }
    .desk-decision .emoji {
        font-size: var(--fs-xl) !important;
        margin-left: 8px !important;
        vertical-align: 6px !important;
    }
    .desk-decision .context {
        font-size: var(--fs-md) !important;
        margin-top: 10px !important;
    }
    .desk-decision-info-tooltip {
        width: 260px !important;
        right: -8px !important;
    }

    /* Trigger block: keep readable but stop dominating the screen */
    .desk-trigger-text { font-size: var(--fs-xl) !important; line-height: 1.3 !important; }
    .desk-trigger-text b { font-size: var(--fs-xl) !important; }

    /* Ticker row: no fixed height on mobile. Metadata often wraps, so the
       divider must move down instead of cutting the row in half. */
    .desk-ticker-row {
        height: auto !important;
        min-height: 0 !important;
        align-items: flex-start !important;
        gap: 10px !important;
        padding-bottom: 12px !important;
        margin-bottom: 16px !important;
    }
    .desk-ticker-row > div:first-child {
        min-width: 0 !important;
        flex: 1 1 auto !important;
    }
    .desk-ticker-row > div:last-child {
        flex: 0 0 auto !important;
    }
    .desk-ticker-row .sym { font-size: var(--fs-xl) !important; }
    .desk-ticker-row .name { font-size: var(--fs-sm) !important; margin-left: 6px !important; }
    .desk-ticker-row .price { font-size: var(--fs-md) !important; }
    .desk-ticker-row .chg { font-size: var(--fs-sm) !important; margin-left: 6px !important; }
    .desk-ticker-row .meta-inline {
        font-size: var(--fs-xs) !important;
        line-height: 1.45 !important;
        white-space: normal !important;
        overflow: visible !important;
        word-break: normal !important;
        overflow-wrap: anywhere !important;
    }

    /* Stack Streamlit columns vertically on mobile.
       This is the critical fix — col_decision (5) + col_pm (3) split
       becomes ~238px + 143px on a 380px phone, both unreadable.
       Stacked, each gets full width. */
    [data-testid="stHorizontalBlock"] {
        flex-direction: column !important;
    }
    [data-testid="stHorizontalBlock"] > [data-testid="column"] {
        width: 100% !important;
        flex: 1 1 100% !important;
        min-width: 100% !important;
    }

    /* PM column on mobile: kill the desktop left-border + indent */
    .desk-pm-container {
        border-left: none !important;
        padding-left: 0 !important;
        margin-top: 24px !important;
        padding-top: 18px !important;
        border-top: 1px solid var(--color-border) !important;
    }
    [data-testid="stHorizontalBlock"]:has(.desk-decision) > [data-testid="column"]:nth-child(2) {
        border-left: none !important;
        padding-left: 0 !important;
        min-height: 0 !important;
        margin-top: 24px !important;
        padding-top: 18px !important;
        border-top: 1px solid var(--color-border) !important;
    }

    /* Trade plan: trim row padding further on mobile and stack the
       sub-line under the value rather than to the right */
    .desk-plan-row { padding: 5px 0 !important; }
    .desk-plan-row .k { font-size: var(--fs-sm) !important; }
    .desk-plan-row .v { font-size: var(--fs-base) !important; }
    .desk-plan-row .d { font-size: var(--fs-sm) !important; }
    .desk-plan-row .sub { font-size: var(--fs-xs) !important; }

    /* Read of the tape: smaller numbers, tighter rows */
    .desk-tape-read .row { padding: 4px 0 !important; }
    .desk-tape-read .row .v { font-size: var(--fs-sm) !important; }
    .desk-tape-read .row .lbl { font-size: var(--fs-sm) !important; }

    /* PM panel body + bullets: shrink */
    .desk-pm-block .body { font-size: var(--fs-base) !important; line-height: 1.45 !important; }
    .desk-pm-item { font-size: var(--fs-base) !important; line-height: 1.4 !important; }
    .desk-pm-header { font-size: var(--fs-xs) !important; }

    /* Dossier text: serif body still readable but smaller */
    .desk-dossier-text { font-size: var(--fs-md) !important; line-height: 1.5 !important; }
    .desk-dossier { padding: 12px 14px !important; }

    /* Modifier badges: stack vertically (each on own line) */
    .desk-modifiers { flex-direction: column !important; gap: 4px !important; }
    .desk-mod { font-size: var(--fs-xs) !important; }

    /* Why-avoid / reconsider lists: tighter */
    .desk-avoid-reasons li, .desk-reconsider li {
        font-size: var(--fs-sm) !important; line-height: 1.45 !important;
    }

    /* Sidebar nav buttons: keep tappable size (44px min recommended) */
    section[data-testid='stSidebar'] [role='radiogroup'] label {
        padding: 10px 12px !important;
    }

    /* Stack any 2-col or 3-col inline grids vertically on mobile.
       Catches the variant-grid (bull/bear cards), the decision-comparison
       Rules/Claude side-by-side, and the Accuracy by source panel. */
    .desk-pm-deep .variant-grid,
    div[style*="grid-template-columns:1fr 1fr"],
    div[style*="grid-template-columns: 1fr 1fr"],
    div[style*="grid-template-columns:1fr 1fr 1fr"],
    div[style*="grid-template-columns: 1fr 1fr 1fr"] {
        grid-template-columns: 1fr !important;
    }

    /* Streamlit sometimes leaves stray horizontal scroll on overflow */
    body, html { overflow-x: hidden !important; }
}

/* Even narrower (small phones, ≤380px): one more shrink step */
@media (max-width: 380px) {
    .desk-decision .word { font-size: 48px !important; }
    .desk-decision .emoji { font-size: var(--fs-xl) !important; }
    .desk-trigger-text { font-size: var(--fs-lg) !important; }
    .desk-trigger-text b { font-size: var(--fs-lg) !important; padding: 0 4px !important; }
    .main .block-container {
        padding-left: 0.6rem !important;
        padding-right: 0.6rem !important;
    }
}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────
# Data fetch
# ─────────────────────────────────────────────────────────────────────
def _history_cache_path(ticker):
    safe = "".join(ch for ch in str(ticker).upper() if ch.isalnum() or ch in ("-", "_"))
    return HISTORY_CACHE_DIR / f"{safe}.json"


def _prepare_history_frame(hist, source="live"):
    if hist is None or len(hist) == 0:
        return None
    try:
        hist = hist.copy()
        if "Close" not in hist.columns:
            return None
        hist = hist.dropna(subset=["Close"])
        if len(hist) == 0:
            return None
        for col in ("Open", "High", "Low"):
            if col not in hist.columns:
                hist[col] = hist["Close"]
        if "Volume" not in hist.columns:
            hist["Volume"] = 0
        hist = hist.sort_index()
        hist.attrs["source"] = source
        return hist
    except Exception:
        return None


def _write_history_cache(ticker, hist):
    try:
        hist = _prepare_history_frame(hist, source="cache-write")
        if hist is None or len(hist) < 50:
            return
        HISTORY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = hist.reset_index().rename(columns={"index": "Date"})
        payload["Date"] = payload["Date"].astype(str)
        cols = [c for c in ("Date", "Open", "High", "Low", "Close", "Volume") if c in payload.columns]
        _history_cache_path(ticker).write_text(payload[cols].tail(540).to_json(orient="records"))
    except Exception:
        pass


def _read_history_cache(ticker, max_age_hours=24):
    try:
        import pandas as pd
        path = _history_cache_path(ticker)
        if not path.exists():
            return None
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if max_age_hours is not None and age_hours > max_age_hours:
            return None
        payload = json.loads(path.read_text())
        hist = pd.DataFrame(payload)
        if hist.empty or "Date" not in hist.columns or "Close" not in hist.columns:
            return None
        hist["Date"] = pd.to_datetime(hist["Date"], errors="coerce")
        hist = hist.dropna(subset=["Date", "Close"]).set_index("Date").sort_index()
        return _prepare_history_frame(hist, source="cached")
    except Exception:
        return None


def _delete_history_cache(ticker):
    try:
        path = _history_cache_path(ticker)
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _fetch_yahoo_chart_history(ticker):
    try:
        import pandas as pd
        end = int(time.time())
        start = end - 60 * 60 * 24 * 760
        symbol = urllib.parse.quote(str(ticker).upper().strip())
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?period1={start}&period2={end}&interval=1d&includePrePost=false&events=history"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        result = ((payload.get("chart") or {}).get("result") or [None])[0]
        if not result:
            return None
        timestamps = result.get("timestamp") or []
        quote = (((result.get("indicators") or {}).get("quote") or [{}])[0]) or {}
        adj = (((result.get("indicators") or {}).get("adjclose") or [{}])[0]) or {}
        if not timestamps or not quote.get("close"):
            return None
        hist = pd.DataFrame({
            "Date": pd.to_datetime(timestamps, unit="s").normalize(),
            "Open": quote.get("open"),
            "High": quote.get("high"),
            "Low": quote.get("low"),
            "Close": adj.get("adjclose") or quote.get("close"),
            "Volume": quote.get("volume"),
        }).dropna(subset=["Date", "Close"])
        hist = hist.set_index("Date").sort_index()
        return _prepare_history_frame(hist, source="yahoo-chart")
    except Exception:
        return None


PRICE_CACHE_TTL_SECONDS = 300


@st.cache_data(ttl=PRICE_CACHE_TTL_SECONDS, show_spinner=False)
def fetch_history(ticker):
    """Fetch 2y daily history. Returns (hist, name, err_reason).

    err_reason is None on success; otherwise a short string explaining why
    the fetch failed (rate limit, 404, JSON decode error, etc.). The UI
    surfaces this so the user sees WHY data didn't load instead of a
    generic 'couldn't find data' message.

    Keep this intentionally lean: ticker changes should not wait for the
    slower Yahoo profile/info endpoint. Display metadata comes from the
    separately cached quote-meta fetch.
    """
    ticker = str(ticker or "").upper().strip()
    cached = _read_history_cache(ticker, max_age_hours=PRICE_CACHE_TTL_SECONDS / 3600)
    if cached is not None and len(cached) >= 50:
        return cached, None, None

    last_error = None
    try:
        hist = _fetch_yahoo_chart_history(ticker)
        if hist is not None and len(hist) >= 50:
            _write_history_cache(ticker, hist)
            return hist, None, None
        last_error = "Yahoo chart endpoint returned no usable rows"
    except Exception as e:
        last_error = f"{type(e).__name__}: {str(e)[:160]}"

    try:
        hist = yf.download(
            ticker, period="2y", interval="1d", auto_adjust=True,
            progress=False, threads=False, timeout=3,
        )
        if hist is not None and len(hist) > 0:
            if hasattr(hist.columns, "nlevels") and hist.columns.nlevels > 1:
                try:
                    hist.columns = hist.columns.get_level_values(0)
                except Exception:
                    pass
            hist = _prepare_history_frame(hist, source="yfinance-download")
            if hist is not None and len(hist) >= 50:
                _write_history_cache(ticker, hist)
                return hist, None, None
        last_error = last_error or "Yahoo returned no usable rows"
    except Exception as e:
        last_error = f"{type(e).__name__}: {str(e)[:160]}"

    stale = _read_history_cache(ticker, max_age_hours=None)
    if stale is not None and len(stale) >= 50:
        stale.attrs["source"] = "stale-cache"
        return stale, None, None

    return None, None, (last_error or "Yahoo returned no rows for this ticker")


def _prepare_benchmark_hist(hist, source="live", error=None):
    """Normalize benchmark history and tag where it came from."""
    if hist is None or len(hist) == 0:
        return None
    try:
        if "Close" not in hist.columns:
            return None
        hist = hist.copy()
        hist = hist.dropna(subset=["Close"])
        if len(hist) == 0:
            return None
        for col in ("Open", "High", "Low"):
            if col not in hist.columns:
                hist[col] = hist["Close"]
        if "Volume" not in hist.columns:
            hist["Volume"] = 0
        hist.attrs["source"] = source
        if error:
            hist.attrs["error"] = error
        return hist
    except Exception:
        return None


def _synthetic_benchmark_history(error=None):
    """Flat benchmark fallback so the app can still render during Yahoo outages."""
    try:
        import pandas as pd
        dates = pd.bdate_range(end=datetime.now().date(), periods=520)
        hist = pd.DataFrame(
            {
                "Open": 100.0,
                "High": 100.0,
                "Low": 100.0,
                "Close": 100.0,
                "Volume": 0,
            },
            index=dates,
        )
        hist.attrs["source"] = "synthetic"
        if error:
            hist.attrs["error"] = error
        return hist
    except Exception:
        return None


def _write_benchmark_cache(hist):
    try:
        if hist is None or len(hist) == 0:
            return
        payload = hist.reset_index().rename(columns={"index": "Date"})
        payload["Date"] = payload["Date"].astype(str)
        cols = [c for c in ("Date", "Open", "High", "Low", "Close", "Volume") if c in payload.columns]
        BENCH_CACHE_PATH.write_text(payload[cols].tail(540).to_json(orient="records"))
    except Exception:
        pass


def _read_benchmark_cache(error=None):
    try:
        import pandas as pd
        if not BENCH_CACHE_PATH.exists():
            return None
        payload = json.loads(BENCH_CACHE_PATH.read_text())
        hist = pd.DataFrame(payload)
        if hist.empty or "Date" not in hist.columns or "Close" not in hist.columns:
            return None
        hist["Date"] = pd.to_datetime(hist["Date"], errors="coerce")
        hist = hist.dropna(subset=["Date", "Close"]).set_index("Date").sort_index()
        if len(hist) < 50:
            return None
        return _prepare_benchmark_hist(hist, source="cached", error=error)
    except Exception:
        return None


@st.cache_data(ttl=300, show_spinner=False)
def fetch_bench():
    """Fetch SPY benchmark, but never let a benchmark outage block the app."""
    last_error = None
    try:
        hist = yf.Ticker("SPY").history(period="2y", interval="1d", auto_adjust=True)
        hist = _prepare_benchmark_hist(hist, source="live")
        if hist is not None and len(hist) >= 200:
            _write_benchmark_cache(hist)
            return hist
        last_error = "Yahoo returned no usable SPY rows"
    except Exception as e:
        last_error = f"{type(e).__name__}: {str(e)[:160]}"

    cached = _read_benchmark_cache(error=last_error)
    if cached is not None:
        return cached

    synthetic = _synthetic_benchmark_history(error=last_error)
    if synthetic is not None:
        return synthetic
    return None


@st.cache_data(ttl=300, show_spinner=False)
def sidebar_watchlist_snapshot(tickers):
    """Small cached payload for the always-visible sidebar watchlist."""
    bench = fetch_bench()
    snapshot = {}
    for raw_ticker in tickers:
        tkr = str(raw_ticker or "").upper().strip()
        if not tkr:
            continue
        hist, _, _ = fetch_history(tkr)
        if hist is None or len(hist) < 2:
            snapshot[tkr] = {
                "last": None,
                "change_pct": None,
                "action": None,
                "price_age": "unavailable",
                "price_age_kind": "stale",
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            continue
        last = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        price_age_label, price_age_kind = format_market_data_age(hist)
        action = None
        t_state = None
        if bench is not None:
            try:
                t_state = tactical.compute(hist, bench) or {}
                action = t_state.get("action")
            except Exception:
                t_state = None
                action = None
        snapshot[tkr] = {
            "last": last,
            "change_pct": (last / prev - 1) * 100 if prev else 0,
            "action": action,
            "state": (t_state or {}).get("state"),
            "rs": (t_state or {}).get("rs"),
            "pct_ma50": (
                ((t_state or {}).get("price") - (t_state or {}).get("ma50")) / (t_state or {}).get("ma50") * 100
                if (t_state or {}).get("price") and (t_state or {}).get("ma50") else None
            ),
            "high_52w": (t_state or {}).get("high_52w"),
            "low_52w": (t_state or {}).get("low_52w"),
            "pct_52w_range": (t_state or {}).get("pct_of_52w_range"),
            "vol_ratio": (t_state or {}).get("vol_ratio"),
            "price_age": price_age_label,
            "price_age_kind": price_age_kind,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    return snapshot


def update_sidebar_watchlist_cache(tickers):
    """Refresh and persist sidebar rows for the supplied tickers immediately."""
    normalized = tuple(
        t for t in (str(raw or "").upper().strip() for raw in (tickers or ()))
        if t
    )
    if not normalized:
        return {}
    refreshed = sidebar_watchlist_snapshot(normalized)
    if refreshed:
        cache = st.session_state.store.setdefault("watchlist_sidebar_cache", {})
        cache.update({
            k: v for k, v in refreshed.items()
            if isinstance(v, dict) and v.get("last") is not None
        })
        for k, v in refreshed.items():
            if isinstance(v, dict) and v.get("last") is not None:
                merge_ticker_snapshot(k, market=v)
        save_store(st.session_state.store)
    return refreshed


def _ticker_key(ticker):
    return str(ticker or "").upper().strip()


def _slim_dict(data):
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def _market_snapshot_from_t_state(t_state, hist=None):
    """Canonical market/rule payload used by sidebar, watchlist, and Analyze."""
    if not isinstance(t_state, dict):
        return {}
    price = t_state.get("price")
    change = t_state.get("change")
    if price is None:
        return {}
    price_age_label, price_age_kind = format_market_data_age(hist)
    return {
        "last": float(price),
        "change_pct": float(change) if change is not None else None,
        "action": t_state.get("action"),
        "state": t_state.get("state"),
        "rs": t_state.get("rs"),
        "pct_ma50": (
            (t_state.get("price") - t_state.get("ma50")) / t_state.get("ma50") * 100
            if t_state.get("price") and t_state.get("ma50") else None
        ),
        "high_52w": t_state.get("high_52w"),
        "low_52w": t_state.get("low_52w"),
        "pct_52w_range": t_state.get("pct_of_52w_range"),
        "vol_ratio": t_state.get("vol_ratio"),
        "trigger": t_state.get("trigger"),
        "entry": t_state.get("entry"),
        "stop": t_state.get("stop"),
        "t1": t_state.get("t1"),
        "price_age": price_age_label,
        "price_age_kind": price_age_kind,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def merge_ticker_snapshot(ticker, *, market=None, meta=None, pm_entry=None, final_action=None):
    """Merge one or more layers into the canonical per-ticker snapshot."""
    tkr = _ticker_key(ticker)
    if not tkr:
        return {}
    snapshots = st.session_state.store.setdefault("ticker_snapshots", {})
    current = snapshots.get(tkr, {}) if isinstance(snapshots.get(tkr), dict) else {}
    updated = dict(current)
    now = datetime.now().isoformat(timespec="seconds")
    if isinstance(market, dict) and market:
        updated["market"] = {**(updated.get("market") or {}), **_slim_dict(market)}
        updated["market_updated_at"] = now
    if isinstance(meta, dict) and meta:
        updated["meta"] = {**(updated.get("meta") or {}), **_slim_dict(meta)}
        updated["meta_updated_at"] = now
    if isinstance(pm_entry, dict):
        result = pm_entry.get("result") if isinstance(pm_entry.get("result"), dict) else {}
        tactical_call = result.get("tactical_call") if isinstance(result, dict) else {}
        quality = result.get("quality") if isinstance(result, dict) else {}
        updated["pm"] = {
            "source": result.get("_source") or pm_entry.get("_source"),
            "ts": pm_entry.get("ts"),
            "action": normalize_action_key((tactical_call or {}).get("action")),
            "confidence": (tactical_call or {}).get("confidence"),
            "quality": (quality or {}).get("tier"),
            "has_memo": bool(tactical_call),
        }
        updated["pm_updated_at"] = now
    if isinstance(final_action, dict) and final_action:
        updated["final_action"] = _slim_dict(final_action)
        updated["final_action_updated_at"] = now
    updated["updated_at"] = now
    snapshots[tkr] = updated
    return updated


def ticker_snapshot(ticker):
    """Canonical per-ticker read model with legacy cache fallbacks."""
    tkr = _ticker_key(ticker)
    if not tkr:
        return {}
    snapshots = st.session_state.store.setdefault("ticker_snapshots", {})
    snap = dict(snapshots.get(tkr, {}) or {})
    market = dict(snap.get("market") or {})
    legacy_market = st.session_state.store.get("watchlist_sidebar_cache", {}).get(tkr, {})
    if isinstance(legacy_market, dict):
        market = {**legacy_market, **market}
    meta = dict(snap.get("meta") or {})
    legacy_meta_entry = st.session_state.store.get("quote_meta_cache", {}).get(tkr, {})
    legacy_meta = legacy_meta_entry.get("meta") if isinstance(legacy_meta_entry, dict) else {}
    if isinstance(legacy_meta, dict):
        meta = {**legacy_meta, **meta}
    pm = dict(snap.get("pm") or {})
    dossier_entry = st.session_state.store.get("dossier_cache", {}).get(tkr, {})
    if isinstance(dossier_entry, dict):
        result = dossier_entry.get("result") or {}
        call = (result.get("tactical_call") or {}) if isinstance(result, dict) else {}
        quality = (result.get("quality") or {}) if isinstance(result, dict) else {}
        pm = {
            **pm,
            "source": result.get("_source") or dossier_entry.get("_source") or pm.get("source"),
            "ts": dossier_entry.get("ts") or pm.get("ts"),
            "action": normalize_action_key(call.get("action")) or pm.get("action"),
            "confidence": call.get("confidence", pm.get("confidence")),
            "quality": quality.get("tier", pm.get("quality")),
            "has_memo": bool(call) or pm.get("has_memo"),
        }
    final_action = dict(snap.get("final_action") or {})
    legacy_final = st.session_state.store.get("final_action_cache", {}).get(tkr, {})
    if isinstance(legacy_final, dict):
        final_action = {**legacy_final, **final_action}
    return {
        **snap,
        "ticker": tkr,
        "market": market,
        "meta": meta,
        "pm": pm,
        "final_action": final_action,
    }


def remember_sidebar_ticker_snapshot(ticker, t_state, hist=None, *, persist=False):
    """Keep the sidebar price/action aligned with the main Analyze read."""
    tkr = str(ticker or "").upper().strip()
    if not tkr or not isinstance(t_state, dict):
        return
    market_payload = _market_snapshot_from_t_state(t_state, hist)
    if not market_payload:
        return
    cache = st.session_state.store.setdefault("watchlist_sidebar_cache", {})
    cache[tkr] = market_payload
    merge_ticker_snapshot(tkr, market=market_payload)
    if persist:
        save_store(st.session_state.store)


def sidebar_row_needs_refresh(row, *, max_age_minutes=5):
    if not isinstance(row, dict) or row.get("last") is None:
        return True
    try:
        ts = row.get("updated_at")
        if not ts:
            return True
        return (datetime.now() - datetime.fromisoformat(ts)) > timedelta(minutes=max_age_minutes)
    except Exception:
        return True


def normalize_action_key(raw):
    """Normalize logged/user/Claude action labels to internal action keys."""
    if raw is None:
        return None
    value = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "enter": "enter_now",
        "enter_now": "enter_now",
        "buy": "enter_now",
        "watch": "watch",
        "hold": "hold_off",
        "hold_off": "hold_off",
        "holdoff": "hold_off",
        "avoid": "avoid",
        "sell": "avoid",
        "accumulate": "accumulate",
    }
    return aliases.get(value)


def sidebar_action_hint(ticker, snapshot=None):
    """Sidebar emoji source aligned with the main Analyze decision."""
    tkr = str(ticker or "").upper().strip()
    snapshot = snapshot or {}

    if tkr == str(st.session_state.get("current_ticker", "")).upper():
        current_t = st.session_state.get("_current_tactical")
        if (
            isinstance(current_t, dict) and
            str(current_t.get("ticker", "")).upper() == tkr and
            current_t.get("action")
        ):
            return current_t.get("action")

    final_cached = (ticker_snapshot(tkr).get("final_action") or {})
    final_action = (
        normalize_action_key(final_cached.get("action"))
        if final_cached.get("source") != "claude" else ""
    )
    if final_action:
        return final_action

    action = snapshot.get("action")
    if action:
        return action

    for entry in reversed(st.session_state.store.get("decisions_log", [])):
        if str(entry.get("ticker", "")).upper() != tkr:
            continue
        if entry.get("outcome") is not None:
            continue
        if entry.get("position_status") == "entered":
            return "position"
        raw = entry.get("user_action") or entry.get("rule_action") or entry.get("claude_action")
        key = normalize_action_key(raw)
        if key:
            return key
    return None


def benchmark_fallback_notice(bench):
    if bench is None:
        return None
    source = getattr(bench, "attrs", {}).get("source")
    if source == "live":
        return None
    if source == "cached":
        return "SPY benchmark is temporarily using the last cached copy because Yahoo did not return fresh data."
    if source == "synthetic":
        return (
            "SPY benchmark is temporarily using a neutral fallback because Yahoo did not return fresh data. "
            "Relative-strength and market-regime reads may be less precise until the next refresh."
        )
    return None


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_yahoo_quote_summary(ticker):
    """Secondary metadata path when yfinance.info is sparse or rate-limited."""
    import urllib.parse
    import urllib.request
    import http.cookiejar

    symbol = urllib.parse.quote((ticker or "").upper().strip())
    if not symbol:
        return {}
    modules = ",".join([
        "summaryProfile",
        "price",
        "defaultKeyStatistics",
        "summaryDetail",
        "fundProfile",
    ])
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
    }

    def _load(url, opener=None):
        req = urllib.request.Request(url, headers=headers)
        open_fn = opener.open if opener else urllib.request.urlopen
        with open_fn(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        result = (((payload or {}).get("quoteSummary") or {}).get("result") or [])
        return result[0] if result else {}

    for host in ("query2.finance.yahoo.com", "query1.finance.yahoo.com"):
        try:
            url = f"https://{host}/v10/finance/quoteSummary/{symbol}?modules={modules}&formatted=false"
            summary = _load(url)
            if summary:
                return summary
        except Exception:
            pass

    # Some Yahoo stats fields require the same cookie/crumb handshake that
    # finance.yahoo.com uses. Keep this as a fallback so price loading is not
    # blocked by a deeper fundamentals endpoint.
    try:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        opener.open(urllib.request.Request("https://fc.yahoo.com", headers=headers), timeout=8).close()
        with opener.open(urllib.request.Request("https://query1.finance.yahoo.com/v1/test/getcrumb", headers=headers), timeout=8) as resp:
            crumb = resp.read().decode("utf-8").strip()
        if crumb:
            quoted_crumb = urllib.parse.quote(crumb, safe="")
            url = (
                f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
                f"?modules={modules}&formatted=false&crumb={quoted_crumb}"
            )
            return _load(url, opener)
    except Exception:
        pass
    return {}


def _raw_yahoo_value(value):
    if isinstance(value, dict):
        if value.get("raw") is not None:
            return value.get("raw")
        return value.get("fmt")
    return value


def _first_present(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _safe_fast_info_get(fast_info, key):
    try:
        if fast_info is None:
            return None
        if hasattr(fast_info, "get"):
            return fast_info.get(key)
        return fast_info[key]
    except Exception:
        return None


def _clean_html_text(value):
    import re
    if value is None:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value))
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_first_pct(text, patterns):
    import re
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except Exception:
                pass
    return None


def _marketbeat_exchange_candidates(exchange_hint=None):
    hint = str(exchange_hint or "").upper()
    mapped = []
    if hint in {"NMS", "NGM", "NCM", "NAS", "NASDAQ"}:
        mapped.append("NASDAQ")
    elif hint in {"NYQ", "NYS", "NYSE"}:
        mapped.append("NYSE")
    elif hint in {"PCX", "ARCX", "NYSEARCA"}:
        mapped.append("NYSEARCA")
    elif hint in {"ASE", "AMEX"}:
        mapped.append("AMEX")
    for exchange in ("NASDAQ", "NYSE", "NYSEARCA", "AMEX"):
        if exchange not in mapped:
            mapped.append(exchange)
    return mapped


@st.cache_data(ttl=21600, show_spinner=False)
def fetch_marketbeat_stats(ticker, exchange_hint=None):
    """Last-resort fallback for short interest and institutional ownership."""
    import re
    import ssl
    import urllib.parse
    import urllib.request

    symbol = (ticker or "").upper().strip()
    if not symbol or "-" in symbol or "." in symbol:
        return {}
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    def _read(url):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.read().decode("utf-8", "ignore")
        except Exception:
            # Public quote pages are display-only metadata; fallback to an
            # unverified context only after the normal certificate path fails.
            context = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=8, context=context) as resp:
                return resp.read().decode("utf-8", "ignore")

    for exchange in _marketbeat_exchange_candidates(exchange_hint):
        base = f"https://www.marketbeat.com/stocks/{exchange}/{urllib.parse.quote(symbol)}/"
        try:
            short_html = _read(base + "short-interest/")
            short_text = _clean_html_text(short_html)
            if symbol not in short_text[:10000]:
                continue
            short_pct = _extract_first_pct(short_text, [
                r"Short Percent of Float\s*([0-9]+(?:\.[0-9]+)?)%",
                r"representing\s*([0-9]+(?:\.[0-9]+)?)%\s*of the public float",
                r"([0-9]+(?:\.[0-9]+)?)%\s+of\s+[^.]*shares are currently sold short",
            ])
            short_ratio = None
            ratio_match = re.search(r"Short Interest Ratio\s*([0-9]+(?:\.[0-9]+)?)", short_text, re.I)
            if ratio_match:
                short_ratio = float(ratio_match.group(1))

            inst_pct = None
            try:
                inst_html = _read(base + "institutional-ownership/")
                inst_text = _clean_html_text(inst_html)
                inst_pct = _extract_first_pct(inst_text, [
                    r"Current Institutional Ownership Percentage\s*([0-9]+(?:\.[0-9]+)?)%",
                    r"([0-9]+(?:\.[0-9]+)?)%\s+of\s+[^.]*stock is owned by institutional investors",
                ])
            except Exception:
                pass

            if short_pct is not None or inst_pct is not None or short_ratio is not None:
                return {
                    "short_pct_float": short_pct,
                    "institutional_ownership_pct": inst_pct,
                    "short_ratio": short_ratio,
                    "exchange": exchange,
                }
        except Exception:
            continue
    return {}


@st.cache_data(ttl=21600, show_spinner=False)
def fetch_marketbeat_earnings_date(ticker, exchange_hint=None):
    """Fallback next-earnings date when Yahoo/yfinance omits calendar data."""
    import re
    import ssl
    import urllib.parse
    import urllib.request

    symbol = (ticker or "").upper().strip()
    if not symbol or "-" in symbol or "." in symbol:
        return None
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    def _read(url):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.read().decode("utf-8", "ignore")
        except Exception:
            context = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=8, context=context) as resp:
                return resp.read().decode("utf-8", "ignore")

    for exchange in _marketbeat_exchange_candidates(exchange_hint):
        url = f"https://www.marketbeat.com/stocks/{exchange}/{urllib.parse.quote(symbol)}/earnings/"
        try:
            page = _read(url)
            if symbol not in page[:15000]:
                continue
            matches = re.findall(r'data-sort-value="(20\d{6})000000"[^>]*>\s*([0-9/]+/20\d{2}|[A-Za-z]+ \d{1,2}, 20\d{2})', page)
            dates = []
            for raw, _label in matches:
                try:
                    dt = datetime.strptime(raw, "%Y%m%d")
                    if dt.date() >= datetime.now().date():
                        dates.append(dt)
                except Exception:
                    pass
            if dates:
                return min(dates)
        except Exception:
            continue
    return None


def normalize_earnings_date(value):
    """Return a naive datetime for known earnings date shapes."""
    if value is None:
        return None
    try:
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if hasattr(value, "date") and hasattr(value, "strftime"):
            return value.replace(tzinfo=None) if hasattr(value, "replace") else value
        text = str(value).strip()
        if not text or text in {"—", "-", "None", "nan", "NaT"}:
            return None
        text = text.replace("Z", "").split("T")[0].strip()
        try:
            return datetime.fromisoformat(text).replace(tzinfo=None)
        except Exception:
            pass
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(text, fmt)
            except Exception:
                continue
        for fmt in ("%b %d", "%B %d"):
            try:
                parsed = datetime.strptime(text, fmt)
                return parsed.replace(year=datetime.now().year)
            except Exception:
                continue
    except Exception:
        return None
    return None


def format_earnings_date_label(value, fallback="—"):
    normalized = normalize_earnings_date(value)
    if normalized is None:
        return str(value).strip() if value else fallback
    return normalized.strftime("%b %d")


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_quote_meta(ticker, include_slow_fallbacks=False):
    """Pull sector, market cap, short interest, earnings date, valuation ratios,
    analyst rating + target, dividend yield, growth rate, debt/equity.
    All optional. Cached 1 hour.

    Normal ticker navigation keeps slow web fallbacks off. Explicit refreshes
    and full reports can opt in with `include_slow_fallbacks=True`.
    """
    out = {
        "long_name": None,
        "short_name": None,
        "quote_type": None,
        "exchange": None,
        "sector": None,
        "industry": None,
        "category": None,
        "fund_family": None,
        "market_cap": None,
        "total_assets": None,
        "net_assets": None,
        "expense_ratio": None,
        "institutional_ownership_pct": None,
        "insider_ownership_pct": None,
        "shares_short": None,
        "short_ratio": None,
        "enterprise_value": None,
        "total_revenue": None,
        "gross_margins": None,
        "operating_margins": None,
        "ebitda_margins": None,
        "profit_margins": None,
        "free_cashflow": None,
        "operating_cashflow": None,
        "total_cash": None,
        "total_debt": None,
        "enterprise_to_revenue": None,
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
        try:
            fast_info = yf_ticker.fast_info or {}
        except Exception:
            fast_info = {}
        summary = fetch_yahoo_quote_summary(ticker)
        price_summary = summary.get("price") or {}
        profile_summary = summary.get("summaryProfile") or {}
        stats_summary = summary.get("defaultKeyStatistics") or {}
        detail_summary = summary.get("summaryDetail") or {}
        fund_summary = summary.get("fundProfile") or {}

        out["long_name"] = _first_present(info.get("longName"), price_summary.get("longName"))
        out["short_name"] = _first_present(info.get("shortName"), price_summary.get("shortName"))
        out["quote_type"] = _first_present(info.get("quoteType"), price_summary.get("quoteType"), _safe_fast_info_get(fast_info, "quoteType"))
        out["exchange"] = _first_present(info.get("exchange"), price_summary.get("exchangeName"), _safe_fast_info_get(fast_info, "exchange"))
        out["sector"] = _first_present(info.get("sector"), profile_summary.get("sector"))
        out["industry"] = _first_present(info.get("industry"), profile_summary.get("industry"))
        out["category"] = _first_present(info.get("category"), fund_summary.get("categoryName"))
        out["fund_family"] = _first_present(info.get("fundFamily"), fund_summary.get("family"))
        out["market_cap"] = _first_present(
            info.get("marketCap"),
            _raw_yahoo_value(price_summary.get("marketCap")),
            _safe_fast_info_get(fast_info, "marketCap"),
            _safe_fast_info_get(fast_info, "market_cap"),
        )
        out["total_assets"] = _first_present(info.get("totalAssets"), _raw_yahoo_value(fund_summary.get("totalAssets")))
        out["net_assets"] = _first_present(info.get("netAssets"), _raw_yahoo_value(fund_summary.get("netAssets")))
        out["enterprise_value"] = info.get("enterpriseValue")
        out["total_revenue"] = info.get("totalRevenue")
        out["free_cashflow"] = info.get("freeCashflow")
        out["operating_cashflow"] = info.get("operatingCashflow")
        out["total_cash"] = info.get("totalCash")
        out["total_debt"] = info.get("totalDebt")
        out["enterprise_to_revenue"] = info.get("enterpriseToRevenue")
        for src_key, out_key in [
            ("grossMargins", "gross_margins"),
            ("operatingMargins", "operating_margins"),
            ("ebitdaMargins", "ebitda_margins"),
            ("profitMargins", "profit_margins"),
        ]:
            val = info.get(src_key)
            if val is not None:
                out[out_key] = float(val) * 100 if abs(float(val)) <= 1.5 else float(val)

        spf = _first_present(
            info.get("shortPercentOfFloat"),
            info.get("sharesPercentSharesOut"),
            _raw_yahoo_value(stats_summary.get("shortPercentOfFloat")),
            _raw_yahoo_value(stats_summary.get("sharesPercentSharesOut")),
        )
        if spf is not None:
            out["short_pct_float"] = normalize_percent_value(spf)
        out["shares_short"] = _first_present(info.get("sharesShort"), _raw_yahoo_value(stats_summary.get("sharesShort")))
        out["short_ratio"] = _first_present(info.get("shortRatio"), _raw_yahoo_value(stats_summary.get("shortRatio")))
        out["institutional_ownership_pct"] = normalize_percent_value(
            _first_present(info.get("heldPercentInstitutions"), _raw_yahoo_value(stats_summary.get("heldPercentInstitutions")))
        )
        out["insider_ownership_pct"] = normalize_percent_value(
            _first_present(info.get("heldPercentInsiders"), _raw_yahoo_value(stats_summary.get("heldPercentInsiders")))
        )

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
        if dy is None:
            dy = _first_present(info.get("yield"), _raw_yahoo_value(detail_summary.get("dividendYield")), _raw_yahoo_value(summary.get("yield")))
        if dy is not None:
            normalized_dy = normalize_percent_value(dy)
            # Yahoo/yfinance occasionally gives dividend *rate* through a
            # yield-like field (NVDA can show 0.47, which would become 47%).
            # When the normalized value is implausibly high, recompute from
            # dividendRate/current price if possible; otherwise omit it.
            if normalized_dy is not None and normalized_dy > 20:
                div_rate = _first_present(info.get("dividendRate"), _raw_yahoo_value(detail_summary.get("dividendRate")))
                current_px = _first_present(
                    info.get("currentPrice"),
                    _raw_yahoo_value(price_summary.get("regularMarketPrice")),
                    _safe_fast_info_get(fast_info, "lastPrice"),
                    _safe_fast_info_get(fast_info, "last_price"),
                )
                try:
                    if div_rate is not None and current_px:
                        normalized_dy = float(div_rate) / float(current_px) * 100
                    else:
                        normalized_dy = None
                except (TypeError, ValueError, ZeroDivisionError):
                    normalized_dy = None
            out["dividend_yield"] = normalized_dy

        # Fund/ETF expense ratio — yfinance usually returns 0.0065 for 0.65%.
        er = info.get("annualReportExpenseRatio")
        if er is None:
            er = _first_present(info.get("expenseRatio"), _raw_yahoo_value(fund_summary.get("annualReportExpenseRatio")))
        if er is not None:
            out["expense_ratio"] = float(er) * 100 if abs(float(er)) <= 1.5 else float(er)

        is_fund_for_fallback = str(out.get("quote_type") or "").upper() in {"ETF", "MUTUALFUND", "FUND"} or bool(
            out.get("category") or out.get("fund_family") or out.get("total_assets") or out.get("net_assets")
        )
        if include_slow_fallbacks and not is_fund_for_fallback and (
            out.get("short_pct_float") is None or out.get("institutional_ownership_pct") is None
        ):
            mb = fetch_marketbeat_stats(ticker, out.get("exchange"))
            if out.get("short_pct_float") is None and mb.get("short_pct_float") is not None:
                out["short_pct_float"] = mb.get("short_pct_float")
            if out.get("institutional_ownership_pct") is None and mb.get("institutional_ownership_pct") is not None:
                out["institutional_ownership_pct"] = mb.get("institutional_ownership_pct")
            if out.get("short_ratio") is None and mb.get("short_ratio") is not None:
                out["short_ratio"] = mb.get("short_ratio")

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

        if include_slow_fallbacks and out["earnings_date"] is None:
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

        if include_slow_fallbacks and out["earnings_date"] is None:
            try:
                out["earnings_date"] = fetch_marketbeat_earnings_date(ticker, out.get("exchange"))
            except Exception:
                pass

        if out["earnings_date"] is not None:
            try:
                normalized_earnings_date = normalize_earnings_date(out["earnings_date"])
                if normalized_earnings_date is not None:
                    out["earnings_date"] = normalized_earnings_date
                    days = (normalized_earnings_date.date() - datetime.now().date()).days
                    out["earnings_days"] = days
            except Exception:
                pass
    except Exception:
        pass
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_financial_snapshot(ticker):
    """Compact financial statement snapshot for the research report.

    yfinance statement labels vary by company and release vintage, so every
    row lookup is best-effort. Missing rows render as — rather than breaking
    the report.
    """
    def _safe_df(getter):
        try:
            df = getter()
            if df is None or getattr(df, "empty", True):
                return None
            return df
        except Exception:
            return None

    def _row(df, names, idx=0):
        if df is None:
            return None
        for name in names:
            if name in df.index and len(df.columns) > idx:
                try:
                    val = df.loc[name].iloc[idx]
                    if val == val:
                        return float(val)
                except Exception:
                    pass
        return None

    def _series(df, names, n=4):
        if df is None:
            return []
        out = []
        cols = list(df.columns)[:n]
        for col in cols:
            val = None
            for name in names:
                if name in df.index:
                    try:
                        raw = df.loc[name, col]
                        if raw == raw:
                            val = float(raw)
                            break
                    except Exception:
                        pass
            out.append((col, val))
        return out

    yf_ticker = yf.Ticker(ticker)
    q_income = _safe_df(lambda: yf_ticker.quarterly_financials)
    q_cash = _safe_df(lambda: yf_ticker.quarterly_cashflow)
    q_bal = _safe_df(lambda: yf_ticker.quarterly_balance_sheet)
    annual_income = _safe_df(lambda: yf_ticker.financials)

    revenue_names = ["Total Revenue", "Operating Revenue"]
    gross_profit_names = ["Gross Profit"]
    operating_income_names = ["Operating Income"]
    net_income_names = ["Net Income", "Net Income Common Stockholders"]
    ebitda_names = ["EBITDA", "Normalized EBITDA"]
    ocf_names = ["Operating Cash Flow", "Total Cash From Operating Activities"]
    capex_names = ["Capital Expenditure", "Capital Expenditures"]
    cash_names = ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]
    debt_names = ["Total Debt", "Long Term Debt And Capital Lease Obligation"]

    latest_revenue = _row(q_income, revenue_names)
    prev_year_revenue = _row(q_income, revenue_names, 4)
    revenue_yoy = None
    if latest_revenue is not None and prev_year_revenue:
        revenue_yoy = (latest_revenue / prev_year_revenue - 1) * 100

    latest_gross = _row(q_income, gross_profit_names)
    gross_margin = latest_gross / latest_revenue * 100 if latest_gross and latest_revenue else None
    latest_op = _row(q_income, operating_income_names)
    operating_margin = latest_op / latest_revenue * 100 if latest_op is not None and latest_revenue else None
    latest_ebitda = _row(q_income, ebitda_names)
    ebitda_margin = latest_ebitda / latest_revenue * 100 if latest_ebitda and latest_revenue else None
    latest_ocf = _row(q_cash, ocf_names)
    latest_capex = _row(q_cash, capex_names)
    latest_fcf = latest_ocf + latest_capex if latest_ocf is not None and latest_capex is not None else None
    fcf_margin = latest_fcf / latest_revenue * 100 if latest_fcf is not None and latest_revenue else None
    cash = _row(q_bal, cash_names)
    debt = _row(q_bal, debt_names)
    net_cash = cash - debt if cash is not None and debt is not None else None

    return {
        "quarterly_revenue": _series(q_income, revenue_names),
        "annual_revenue": _series(annual_income, revenue_names, n=5),
        "latest_revenue": latest_revenue,
        "revenue_yoy": revenue_yoy,
        "gross_margin": gross_margin,
        "operating_margin": operating_margin,
        "ebitda_margin": ebitda_margin,
        "fcf_margin": fcf_margin,
        "net_income": _row(q_income, net_income_names),
        "free_cash_flow": latest_fcf,
        "cash": cash,
        "debt": debt,
        "net_cash": net_cash,
    }


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
        return f"${cap/1e12:,.2f}T"
    if cap >= 1e9:
        return f"${cap/1e9:.1f}B"
    if cap >= 1e6:
        return f"${cap/1e6:.0f}M"
    return f"${cap:,.0f}"


def format_plain_pct(value, digits=1):
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return f"{v:.{digits}f}%"


def format_expense_pct(value):
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    # Fallback profiles may store ratios (0.0059), while yfinance fetch_meta
    # stores display percent (0.59). Treat sub-5 bps-looking values as ratios.
    if 0 < abs(v) < 0.05:
        v *= 100
    return f"{v:.2f}%"


def display_security_name(ticker, name=None, meta=None, fallback_profile=None):
    meta = meta or {}
    fallback_profile = fallback_profile or {}
    ticker = (ticker or "").upper().strip()
    candidates = [
        name,
        meta.get("long_name"),
        meta.get("short_name"),
        fallback_profile.get("name"),
    ]
    for candidate in candidates:
        if candidate and str(candidate).strip().upper() != ticker:
            return str(candidate).strip()
    return ""


def build_security_meta_bits(ticker, meta=None, fallback_profile=None):
    """Build the compact header identity line, with ETF-aware fields."""
    meta = meta or {}
    fallback_profile = fallback_profile or {}
    quote_type = str(meta.get("quote_type") or "").upper()
    sector = meta.get("sector") or fallback_profile.get("sector")
    category = meta.get("category") or fallback_profile.get("category")
    is_fund = quote_type in {"ETF", "MUTUALFUND", "FUND"} or bool(
        category or meta.get("fund_family") or meta.get("total_assets") or
        meta.get("net_assets") or sector in {"ETF", "Fund"}
    )
    bits = []
    if is_fund:
        label = "ETF" if quote_type == "ETF" or sector == "ETF" else "Fund"
        bits.append(label)
        if category:
            bits.append(str(category).title())
        else:
            bits.append("Category —")
        assets = format_market_cap(
            meta.get("total_assets") or meta.get("net_assets") or
            fallback_profile.get("total_assets") or fallback_profile.get("net_assets")
        )
        if assets:
            bits.append(f"{assets} AUM")
        else:
            bits.append("AUM —")
        er = meta.get("expense_ratio")
        if er is None:
            er = fallback_profile.get("expense_ratio")
        er_pct = format_expense_pct(er)
        if er_pct:
            bits.append(f"{er_pct} expense")
        else:
            bits.append("expense —")
        dy = meta.get("dividend_yield") or fallback_profile.get("dividend_yield")
        dy_pct = format_plain_pct(dy, digits=2)
        if dy_pct:
            bits.append(f"{dy_pct} yield")
        family = meta.get("fund_family")
        if family:
            bits.append(str(family))
        return bits

    bits.append(str(sector) if sector else "Sector —")
    industry = meta.get("industry") or fallback_profile.get("industry")
    if industry and str(industry).lower() != str(sector or "").lower():
        bits.append(str(industry))
    elif not industry:
        bits.append("Industry —")
    mcap = format_market_cap(meta.get("market_cap") or fallback_profile.get("market_cap"))
    bits.append(mcap if mcap else "Market cap —")
    short_pct = format_plain_pct(meta.get("short_pct_float"))
    bits.append(f"{short_pct} short" if short_pct else "short —")
    inst_pct = format_plain_pct(meta.get("institutional_ownership_pct"))
    bits.append(f"{inst_pct} inst. owned" if inst_pct else "inst. owned —")
    dy_pct = format_plain_pct(meta.get("dividend_yield") or fallback_profile.get("dividend_yield"), digits=2)
    if dy_pct:
        bits.append(f"{dy_pct} yield")
    return bits


def format_earnings(meta):
    """Return (banner_text_or_None, footer_text_or_None).
    Banner if within 7 days, footer otherwise.
    """
    if not meta.get("earnings_date") or meta.get("earnings_days") is None:
        return None, None
    days = meta["earnings_days"]
    date_str = format_earnings_date_label(meta.get("earnings_date"))
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


def apply_earnings_event_gate(t_state, earnings_days):
    """Treat near-term earnings as a final safety overlay on fresh entries."""
    if t_state is None or earnings_days is None:
        return t_state
    action = t_state.get("action")
    if 0 <= earnings_days <= 2 and action in ("enter_now", "watch", "accumulate"):
        return {
            **t_state,
            "action": "hold_off",
            "trigger": None,
            "event_risk_hold": True,
            "event_risk_days": earnings_days,
        }
    if 3 <= earnings_days <= 7:
        updated = {**t_state, "event_risk_watch": True, "event_risk_days": earnings_days}
        if action in ("enter_now", "accumulate"):
            updated["action"] = "watch"
        return updated
    return t_state


def classify_setup_personality(t_state, quality_tier=""):
    """Descriptive setup type, separate from the action recommendation."""
    if not t_state:
        return {"label": "Unclassified", "emoji": "🧭", "rank": 99, "description": "Not enough data yet."}
    action = t_state.get("action")
    price = t_state.get("price") or 0
    ma20 = t_state.get("ma20") or price
    ma50 = t_state.get("ma50") or price
    ma200 = t_state.get("ma200") or price
    rs = t_state.get("rs", 1.0) or 1.0
    rs_delta = t_state.get("rs_delta", 0) or 0
    rsi = t_state.get("rsi14", 50) or 50
    pct_52w = t_state.get("pct_of_52w_range", 50) or 50
    vol_ratio = t_state.get("vol_ratio", 1.0) or 1.0
    structure = t_state.get("structure_quality", 5) or 5
    tech_delta = t_state.get("tech_delta", 0) or 0

    if action == "avoid" or (price < ma200 and rs < 0.9 and tech_delta <= 0):
        return {
            "label": "Broken / Repair",
            "emoji": "🛠️",
            "rank": 7,
            "description": "Long-term structure or relative strength is not healthy enough yet.",
        }
    if t_state.get("is_accumulation_eligible") or action == "accumulate":
        return {
            "label": "Mean Reversion",
            "emoji": "↩️",
            "rank": 3,
            "description": "A beaten-down name starting to stabilize; entry discipline matters.",
        }
    if price > ma50 > ma200 and rs >= 1.1 and pct_52w >= 80:
        if price > ma20 * 1.08 or price > ma50 * 1.15 or rsi >= 70:
            return {
                "label": "Extended Momentum",
                "emoji": "🔥",
                "rank": 1,
                "description": "Leadership tape, but stretched enough that chasing can be risky.",
            }
        return {
            "label": "Momentum Leader",
            "emoji": "🚀",
            "rank": 0,
            "description": "Strong trend, strong relative strength, and leadership behavior.",
        }
    if quality_tier in ("A", "B") and price > ma200 and structure >= 7 and vol_ratio < 1.5:
        return {
            "label": "Steady Compounder",
            "emoji": "🏛️",
            "rank": 2,
            "description": "Quality trend with less dependence on one explosive technical moment.",
        }
    if price < ma200 and (rs_delta > 0 or tech_delta > 0):
        return {
            "label": "Turnaround",
            "emoji": "🔄",
            "rank": 4,
            "description": "Still below long-term trend, but the tape is trying to repair.",
        }
    if vol_ratio >= 1.5 and pct_52w >= 70 and structure >= 7:
        return {
            "label": "Spec Breakout",
            "emoji": "🧨",
            "rank": 5,
            "description": "High-energy setup with more execution and volatility risk.",
        }
    return {
        "label": "Transition",
        "emoji": "🧭",
        "rank": 6,
        "description": "Mixed structure; useful to watch, but not a clean category yet.",
    }


def format_source_note(source):
    """Shorten noisy backend/source labels before rendering them in the UI."""
    source = source or "the thesis"
    lower = str(source).lower()
    if "claude call failed" in lower or "authentication_error" in lower:
        age_suffix = ""
        if " · " in str(source):
            age_suffix = " · " + str(source).split(" · ", 1)[1]
        return "static fallback" + age_suffix
    return source


def format_market_data_age(hist):
    """Human-readable last bar date for trust/freshness UI."""
    try:
        if hist is None or len(hist) == 0:
            return "price data unavailable", "stale"
        last_idx = hist.index[-1]
        if hasattr(last_idx, "date"):
            last_date = last_idx.date()
        else:
            last_date = datetime.fromisoformat(str(last_idx)[:10]).date()
        today = datetime.now().date()
        age_days = max((today - last_date).days, 0)
        if age_days == 0:
            age_label = "today"
        elif age_days == 1:
            age_label = "yesterday"
        else:
            age_label = f"{age_days}d ago"
        return f"last close {last_date.strftime('%b %d')} · {age_label}", ("fresh" if age_days <= 3 else "stale")
    except Exception:
        return "price data date unknown", "stale"


def benchmark_source_label(bench):
    source = getattr(bench, "attrs", {}).get("source") if bench is not None else None
    if source == "cached":
        return "SPY cached fallback", "warn"
    if source == "synthetic":
        return "SPY neutral fallback", "warn"
    if source == "live":
        return "SPY live", "fresh"
    return "SPY source unknown", "warn"


def pm_status_label(source):
    source = format_source_note(source or "")
    lower = str(source).lower()
    if not source:
        return "not generated", "warn"
    if "cached only" in lower or "fast mode" in lower:
        return "cached/static · fast mode", "info"
    if "unavailable" in lower:
        return "not available", "warn"
    if "fallback" in lower or "failed" in lower:
        return source, "warn"
    if "d old" in lower:
        try:
            days = int(lower.split("d old", 1)[0].split()[-1])
            return source, "stale" if days >= 3 else "info"
        except Exception:
            return source, "info"
    if "today" in lower or "claude" in lower:
        return source, "fresh"
    return source, "neutral"


def research_health_items(pm, dossier_result, api_key):
    """Explicit research-state rows for the right-side status panel."""
    pm = pm or {}
    dossier_result = dossier_result or {}
    has_key = bool(api_key)
    pm_source = str(pm.get("_source") or "")
    dossier_source = str(dossier_result.get("_source") or "")
    thesis = str(pm.get("thesis") or "")
    dossier_text = dossier_result.get("dossier")

    if not has_key:
        return [
            ("PM memo", "AI key unavailable", "warn"),
            ("Full dossier", "AI key unavailable", "warn"),
        ]

    pm_is_placeholder = thesis.startswith("No generated PM thesis yet")
    pm_label, pm_kind = pm_status_label(pm_source)
    if "timed out" in pm_source.lower():
        pm_row = (
            ("PM memo", "cached after timeout", "info")
            if not pm_is_placeholder
            else ("PM memo", "timed out", "warn")
        )
    elif "failed" in pm_source.lower() or "fallback" in pm_source.lower():
        pm_row = ("PM memo", pm_label, "warn")
    elif pm_is_placeholder:
        pm_row = ("PM memo", "not generated", "warn")
    else:
        pm_row = ("PM memo", pm_label, pm_kind)

    dossier_lower = dossier_source.lower()
    if dossier_text:
        dossier_label, dossier_kind = pm_status_label(dossier_source)
        dossier_row = ("Full dossier", dossier_label, dossier_kind)
    elif "timed out" in dossier_lower:
        dossier_row = ("Full dossier", "timed out", "warn")
    elif "error:" in dossier_lower:
        dossier_row = (
            "Full dossier",
            dossier_source.replace("error:", "").strip()[:64],
            "warn",
        )
    elif "cached only" in dossier_lower or "fast mode" in dossier_lower:
        dossier_row = ("Full dossier", "cached/static · fast mode", "info")
    else:
        dossier_row = ("Full dossier", "not generated", "warn")

    return [pm_row, dossier_row]


def inferred_quality_from_pm(pm, t_state=None):
    """Conservative quality tier when an older PM memo lacks a quality object."""
    if not isinstance(pm, dict):
        return {}
    thesis = str(pm.get("thesis") or "").strip()
    if not thesis or thesis.startswith("No generated PM thesis yet"):
        return {}
    drivers = " ".join(str(x) for x in (pm.get("drivers") or []))
    risks = " ".join(str(x) for x in (pm.get("risks") or []))
    valuation = str(pm.get("valuation") or "")
    text = f"{thesis} {drivers} {risks} {valuation}".lower()

    if any(term in text for term in (
        "pre-revenue", "binary", "clinical", "single-asset",
        "going concern",
    )):
        tier = "Speculative"
        rationale = "Generated PM memo describes a real upside path, but the risk profile is binary or balance-sheet sensitive."
    elif any(term in text for term in (
        "structural decline", "secular decline", "no moat",
        "melting ice cube", "avoid", "broken business",
    )):
        tier = "Avoid"
        rationale = "Generated PM memo flags structural business-quality concerns rather than only timing risk."
    elif any(term in text for term in (
        "dominant", "category leader", "durable", "irreplaceable",
        "wide moat", "switching costs", "platform", "mission-critical",
    )):
        tier = "A"
        rationale = "Generated PM memo points to durable leadership or platform economics; tactical timing remains separate."
    else:
        tier = "B"
        rationale = "Generated PM memo supports a real business thesis, but quality is treated as selective until Claude returns a formal tier."

    return {"tier": tier, "rationale": rationale}


def metadata_status_label(meta):
    if not meta:
        return "deferred for fast load", "info"
    if meta.get("_deferred"):
        return "deferred for fast load", "info"
    quote_type = str(meta.get("quote_type") or "").upper()
    is_fund = quote_type in {"ETF", "MUTUALFUND", "FUND"} or bool(
        meta.get("category") or meta.get("fund_family") or meta.get("total_assets") or meta.get("net_assets")
    )
    if is_fund:
        key_fields = ("total_assets", "category", "fund_family", "expense_ratio")
    else:
        key_fields = ("market_cap", "short_pct_float", "institutional_ownership_pct", "earnings_date")
    available = sum(1 for field in key_fields if meta.get(field) is not None)
    if available >= len(key_fields):
        return "Yahoo/meta cached ≤1h · complete", "fresh"
    if available >= max(2, len(key_fields) - 1):
        return f"Yahoo/meta cached ≤1h · partial {available}/{len(key_fields)}", "warn"
    return f"Yahoo/meta sparse · {available}/{len(key_fields)}", "stale"


def cached_quote_meta_snapshot(ticker):
    """Return saved quote metadata without making a network call."""
    tkr = str(ticker or "").upper().strip()
    if not tkr:
        return {"_deferred": True}
    snap_meta = (ticker_snapshot(tkr).get("meta") or {})
    if isinstance(snap_meta, dict) and snap_meta:
        return {**snap_meta, "_cached_snapshot": True}
    cache = st.session_state.store.setdefault("quote_meta_cache", {})
    entry = cache.get(tkr) or {}
    meta = entry.get("meta") if isinstance(entry, dict) else None
    if isinstance(meta, dict) and meta:
        return {**meta, "_cached_snapshot": True}
    return {"_deferred": True}


def remember_quote_meta(ticker, meta):
    """Persist metadata fetched on slower pages so Analyze can open fast later."""
    if not isinstance(meta, dict) or not meta:
        return
    tkr = str(ticker or "").upper().strip()
    if not tkr:
        return
    slim = {k: v for k, v in meta.items() if not str(k).startswith("_")}
    st.session_state.store.setdefault("quote_meta_cache", {})[tkr] = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "meta": slim,
    }
    merge_ticker_snapshot(tkr, meta=slim)


def watchlist_pm_status(dossier_cache, tickers):
    tickers = [str(t).upper() for t in tickers]
    missing = 0
    old = 0
    for ticker in tickers:
        cached = dossier_cache.get(ticker, {})
        if not ((cached.get("result") or {}).get("tactical_call") or {}):
            missing += 1
            continue
        try:
            ts = cached.get("ts")
            if ts and (datetime.now() - datetime.fromisoformat(ts)).days >= 3:
                old += 1
        except Exception:
            pass
    if missing or old:
        parts = []
        if missing:
            parts.append(f"{missing} no memo")
        if old:
            parts.append(f"{old} old memos")
        return "PM " + " · ".join(parts), "warn"
    if tickers:
        return "PM cached/generated for all rows", "fresh"
    return "PM no rows", "neutral"


def data_status_html(items):
    chips = "".join(
        f'<span class="desk-data-chip {html.escape(kind)}"><b>{html.escape(label)}</b> {html.escape(value)}</span>'
        for label, value, kind in items
    )
    return f'<div class="desk-data-strip">{chips}</div>'


def canonical_freshness_html(items, refresh_event=None):
    """One canonical freshness panel so status language stays consistent."""
    receipt = ""
    if isinstance(refresh_event, dict):
        refreshed_at = refresh_event.get("time")
        research_requested = bool(refresh_event.get("research"))
    else:
        refreshed_at = refresh_event
        research_requested = False
    if refreshed_at:
        if research_requested:
            pm_label = str(refresh_event.get("pm_label") or "").strip()
            pm_kind = str(refresh_event.get("pm_kind") or "").strip()
            dossier_label = str(refresh_event.get("dossier_label") or "").strip()
            pm_ok = pm_kind in ("fresh", "info") and not any(
                marker in pm_label.lower()
                for marker in ("timeout", "failed", "not generated", "unavailable", "cached/static")
            )
            if pm_ok:
                status = f"PM refreshed ({pm_label})."
            else:
                status = (
                    f"Market data updated; PM did not refresh cleanly"
                    f"{': ' + pm_label if pm_label else ''}."
                )
            if dossier_label and "not generated" not in dossier_label.lower():
                status += f" Dossier: {dossier_label}."
            receipt = (
                f'<div class="desk-refresh-receipt {"" if pm_ok else "warn"}">'
                f'{html.escape(status)} Updated at {html.escape(refreshed_at)}.</div>'
            )
        else:
            receipt = (
                f'<div class="desk-refresh-receipt">Updated price, fundamentals, '
                f'and sidebar row at {html.escape(refreshed_at)}.</div>'
            )
    return (
        '<div class="desk-freshness-panel">'
        '<div class="desk-freshness-title">Freshness</div>'
        f'{data_status_html(items)}'
        f'{receipt}'
        '</div>'
    )


def active_refresh_event(ticker):
    """Return a refresh event only for the currently viewed ticker."""
    event = st.session_state.pop("_refresh_result", None)
    if not isinstance(event, dict):
        return None
    if str(event.get("ticker") or "").upper() != str(ticker or "").upper():
        return None
    return event


def sidebar_cache_status(ticker):
    """Freshness label for the active ticker row in the sidebar cache."""
    entry = ticker_snapshot(ticker).get("market") or {}
    try:
        ts = entry.get("updated_at")
        if not ts:
            return "not refreshed", "stale"
        age = datetime.now() - datetime.fromisoformat(ts)
        minutes = int(age.total_seconds() // 60)
        if minutes < 1:
            return "just now", "fresh"
        if minutes <= 20:
            return f"{minutes}m old", "fresh"
        return f"{minutes}m old", "stale"
    except Exception:
        return "age unknown", "stale"


def refresh_current_ticker_state(ticker, *, refresh_research=False):
    """Refresh the four visible ticker layers: price, fundamentals, PM, sidebar."""
    refresh_ticker = str(ticker or "").upper().strip()
    if not refresh_ticker:
        return
    st.session_state.current_ticker = refresh_ticker
    st.session_state.view = "analyze"
    # Do not write to st.session_state["ticker_input"] here. This helper is
    # called from refresh buttons after the sidebar text_input widget has
    # already been instantiated, and Streamlit forbids mutating a widget's
    # session key at that point. The normal sidebar sync block updates the
    # widget safely on the next rerun if current_ticker changed.
    try:
        fetch_history.clear(refresh_ticker)
    except Exception:
        fetch_history.clear()
    _delete_history_cache(refresh_ticker)
    if refresh_research:
        # Do not do slow Yahoo metadata fetches inside the button callback.
        # The callback reruns the app immediately; doing network work here
        # makes the click feel frozen and then the render repeats work again.
        # Clear caches, mark the PM refresh pending, and let the normal Analyze
        # render fetch price once and generate research once.
        try:
            fetch_quote_meta.clear(refresh_ticker)
        except Exception:
            pass
    else:
        try:
            fetch_quote_meta.clear(refresh_ticker)
        except Exception:
            fetch_quote_meta.clear()
        try:
            refreshed_meta = fetch_quote_meta(refresh_ticker, include_slow_fallbacks=False)
            remember_quote_meta(refresh_ticker, refreshed_meta)
        except Exception:
            pass
        try:
            refreshed_hist, _name, _err = fetch_history(refresh_ticker)
            refreshed_bench = fetch_bench()
            if refreshed_hist is not None and refreshed_bench is not None:
                refreshed_t = tactical.compute(refreshed_hist, refreshed_bench)
                if refreshed_t is not None:
                    remember_sidebar_ticker_snapshot(refresh_ticker, refreshed_t, refreshed_hist)
        except Exception:
            pass
    sidebar_watchlist_snapshot.clear()
    if refresh_research:
        st.session_state["_force_pm_refresh_ticker"] = refresh_ticker
        pending = st.session_state.setdefault("_pending_pm_refreshes", {})
        pending[refresh_ticker] = datetime.now().isoformat(timespec="seconds")
    st.session_state["_refresh_result"] = {
        "ticker": refresh_ticker,
        "time": now_market_time().strftime("%-I:%M %p"),
        "research": bool(refresh_research),
    }
    save_store(st.session_state.store)


def get_effective_api_key():
    try:
        secret_key = st.secrets.get("ANTHROPIC_API_KEY", "").strip()
    except Exception:
        secret_key = ""
    env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    session_key = st.session_state.get("session_anthropic_api_key", "").strip()
    return secret_key or env_key or session_key


def fmt_big_number(value):
    if value is None:
        return "—"
    v = float(value)
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e12:
        return f"{sign}${v/1e12:,.2f}T"
    if v >= 1e9:
        return f"{sign}${v/1e9:,.2f}B"
    if v >= 1e6:
        return f"{sign}${v/1e6:,.0f}M"
    return f"{sign}${v:,.0f}"


def fmt_pct(value):
    if value is None:
        return "—"
    return f"{float(value):+.1f}%"


def fmt_mult(value):
    if value is None:
        return "—"
    return f"{float(value):.1f}x"


def render_research_report(ticker):
    ticker = (ticker or "").upper().strip()
    if not ticker:
        st.error("No ticker supplied.")
        st.stop()

    hist, name, err_reason = fetch_history(ticker)
    bench = fetch_bench()
    meta = fetch_quote_meta(ticker, include_slow_fallbacks=True)
    remember_quote_meta(ticker, meta)
    fin = fetch_financial_snapshot(ticker)
    fallback_profile = infer_security_profile(ticker, meta, name)
    company = (
        (name if name and name.upper() != ticker else None)
        or meta.get("long_name")
        or meta.get("short_name")
        or fallback_profile.get("name")
        or ticker
    )

    if hist is None or bench is None:
        st.error(f"Couldn't build research report for {ticker}. {err_reason or 'Market data unavailable.'}")
        st.stop()
    bench_notice = benchmark_fallback_notice(bench)
    if bench_notice:
        st.warning(bench_notice)

    t = tactical.compute(hist, bench)
    api_key = get_effective_api_key()
    pm = get_cached_pm(ticker, t, api_key=api_key if api_key else None, company_name=company)
    price_age_label, price_age_kind = format_market_data_age(hist)
    bench_label, bench_kind = benchmark_source_label(bench)
    modifiers = tactical.decision_modifiers(t, meta, t.get("market_regime", "unknown"))
    dossier = get_cached_dossier(
        ticker, t, modifiers, meta, pm,
        api_key=api_key if api_key else None, company_name=company,
        allow_generate=True,
    )
    quality = (dossier or {}).get("quality") or {}
    q_label = quality.get("tier") or "Unrated"
    q_text = quality.get("rationale") or pm.get("thesis") or "No long-form quality note is available yet."

    quote_type = str(meta.get("quote_type") or "").upper()
    is_fund_report = quote_type in {"ETF", "MUTUALFUND", "FUND"} or bool(
        meta.get("category") or meta.get("fund_family") or
        fallback_profile.get("sector") in {"ETF", "Fund"}
    )
    market_cap = format_market_cap(meta.get("market_cap")) or "—"
    fund_assets = format_market_cap(
        meta.get("total_assets") or meta.get("net_assets") or
        fallback_profile.get("total_assets") or fallback_profile.get("net_assets")
    ) or "—"
    category_label = (
        meta.get("category") or fallback_profile.get("category") or
        ("ETF" if is_fund_report else "—")
    )
    enterprise_value = format_market_cap(meta.get("enterprise_value")) or "—"
    rec = format_recommendation(meta.get("analyst_rec"), meta.get("analyst_n")) or "—"
    fpe = meta.get("forward_pe")
    ev_ebitda = meta.get("ev_ebitda")
    revenue = fin.get("latest_revenue") or meta.get("total_revenue")
    revenue_yoy = fin.get("revenue_yoy") if fin.get("revenue_yoy") is not None else meta.get("revenue_growth")
    gross_margin = fin.get("gross_margin") if fin.get("gross_margin") is not None else meta.get("gross_margins")
    operating_margin = fin.get("operating_margin") if fin.get("operating_margin") is not None else meta.get("operating_margins")
    ebitda_margin = fin.get("ebitda_margin") if fin.get("ebitda_margin") is not None else meta.get("ebitda_margins")
    profit_margin = meta.get("profit_margins")
    free_cash_flow = fin.get("free_cash_flow") if fin.get("free_cash_flow") is not None else meta.get("free_cashflow")
    fcf_margin = fin.get("fcf_margin")
    if fcf_margin is None and free_cash_flow is not None and revenue:
        fcf_margin = free_cash_flow / revenue * 100
    cash = fin.get("cash") if fin.get("cash") is not None else meta.get("total_cash")
    debt = fin.get("debt") if fin.get("debt") is not None else meta.get("total_debt")
    net_cash = fin.get("net_cash")
    if net_cash is None and cash is not None and debt is not None:
        net_cash = cash - debt

    action_label = STATE_STYLES.get(t.get("action"), {}).get("label", t.get("action", "Watch"))
    action_emoji = STATE_STYLES.get(t.get("action"), {}).get("emoji", "")
    if t.get("action") == "enter_now":
        timing = "Technical setup is actionable now, but sizing should still respect reward/risk and event risk."
    elif t.get("action") == "watch":
        timing = "The name belongs on the screen, but the entry still depends on a cleaner trigger."
    elif t.get("action") == "hold_off":
        timing = "The research may be useful, but the trade setup is not clean enough yet."
    elif t.get("action") == "accumulate":
        timing = "This is a small-starter, long-horizon setup rather than a clean tactical entry."
    else:
        timing = "Current setup does not justify fresh exposure without a material repair signal."

    def clean_value(value):
        if value is None:
            return "—"
        value = str(value)
        return value if value.strip() else "—"

    metric_groups = [
        (
            "Market",
            [
                ("Price", f"${t['price']:,.2f}"),
                ("AUM" if is_fund_report else "Market cap", fund_assets if is_fund_report else market_cap),
                ("Category" if is_fund_report else "Enterprise value", str(category_label).title() if is_fund_report else enterprise_value),
            ],
        ),
        (
            "Growth",
            [
                ("Revenue", fmt_big_number(revenue or meta.get("total_revenue"))),
                ("Revenue YoY", fmt_pct(revenue_yoy)),
                ("EPS YoY", fmt_pct(meta.get("earnings_growth"))),
            ],
        ),
        (
            "Margins",
            [
                ("Gross", fmt_pct(gross_margin)),
                ("Operating", fmt_pct(operating_margin)),
                ("FCF", fmt_pct(fcf_margin)),
            ],
        ),
        (
            "Valuation",
            [
                ("EV/Sales", fmt_mult(meta.get("enterprise_to_revenue"))),
                ("EV/EBITDA", fmt_mult(ev_ebitda)),
                ("Forward P/E", fmt_mult(fpe)),
            ],
        ),
        (
            "Setup",
            [
                ("Quality", q_label),
                ("Short", f"{meta.get('short_pct_float'):.1f}%" if meta.get("short_pct_float") is not None else "—"),
                ("Inst. owned", f"{meta.get('institutional_ownership_pct'):.1f}%" if meta.get("institutional_ownership_pct") is not None else "—"),
            ],
        ),
    ]

    def metrics_html():
        groups_html = []
        for title, rows in metric_groups:
            row_html = "".join(
                f'<div class="research-metric-row"><span class="k">{html.escape(label)}</span>'
                f'<span class="v">{html.escape(clean_value(value))}</span></div>'
                for label, value in rows
            )
            groups_html.append(
                f'<div class="research-metric-group"><div class="group-title">{html.escape(title)}</div>{row_html}</div>'
            )
        return "".join(groups_html)

    def table_html(title, rows):
        body = "".join(
            f"<tr><td>{html.escape(label)}</td><td>{html.escape(clean_value(value))}</td></tr>"
            for label, value in rows
        )
        return (
            f'<div class="research-section"><div class="eyebrow">{html.escape(title)}</div>'
            f'<table class="research-table"><tbody>{body}</tbody></table></div>'
        )

    def bullets_html(items, fallback):
        items = [str(x).strip() for x in (items or []) if str(x).strip()]
        if not items:
            items = [fallback]
        return "".join(f"<li>{html.escape(x)}</li>" for x in items)

    def paragraph(value, fallback):
        value = str(value or "").strip()
        return html.escape(value if value else fallback)

    def series_rows(series, label):
        rows = []
        for period, value in (series or [])[:4]:
            try:
                period_label = period.strftime("%Y-%m-%d")
            except Exception:
                period_label = str(period)
            rows.append((f"{label} {period_label}", fmt_big_number(value)))
        return rows or [(f"{label} history", "Unavailable from current data source")]

    earnings_label = "Next earnings"
    if meta.get("earnings_date") and meta.get("earnings_days") is not None:
        d = meta["earnings_days"]
        earnings_value = format_earnings_date_label(meta.get("earnings_date")) + (f" · in {d}d" if d >= 0 else f" · {abs(d)}d ago")
    else:
        earnings_value = "—"
    earnings_rows = [
        (earnings_label, earnings_value),
        ("Expected EPS", f"${meta.get('expected_eps'):,.2f}" if meta.get("expected_eps") is not None else "—"),
        ("Latest quarterly revenue", fmt_big_number(revenue)),
        ("Revenue growth YoY", fmt_pct(revenue_yoy)),
        ("Gross margin", fmt_pct(gross_margin)),
        ("Operating margin", fmt_pct(operating_margin)),
        ("EBITDA margin", fmt_pct(ebitda_margin)),
        ("Net income", fmt_big_number(fin.get("net_income"))),
        ("Free cash flow", fmt_big_number(free_cash_flow)),
        ("Free cash flow margin", fmt_pct(fcf_margin)),
    ]
    history_rows = (
        series_rows(fin.get("quarterly_revenue"), "Quarterly revenue") +
        series_rows(fin.get("annual_revenue"), "Annual revenue")
    )
    trading_rows = [
        ("Action", f"{action_emoji} {action_label}".strip()),
        ("Decision context", decision_context(t)),
        ("Price vs 50d", f"{((t['price'] - t['ma50']) / t['ma50'] * 100):+.1f}%" if t.get("ma50") else "—"),
        ("Price vs 200d", f"{((t['price'] - t['ma200']) / t['ma200'] * 100):+.1f}%" if t.get("ma200") else "—"),
        ("52-week position", f"{t.get('pct_of_52w_range'):.0f}% of range" if t.get("pct_of_52w_range") is not None else "—"),
        ("Relative strength", f"{t.get('rs', 1):.2f} vs SPX"),
        ("Volume", f"{t.get('vol_ratio', 1):.2f}x 20d avg"),
    ]
    growth_rows = [
        ("Trailing revenue", fmt_big_number(revenue or meta.get("total_revenue"))),
        ("Revenue growth YoY", fmt_pct(revenue_yoy)),
        ("Earnings growth YoY", fmt_pct(meta.get("earnings_growth"))),
        ("Gross margin", fmt_pct(gross_margin)),
        ("Operating margin", fmt_pct(operating_margin)),
        ("Free cash flow margin", fmt_pct(fcf_margin)),
    ]
    balance_rows = [
        ("Cash & investments", fmt_big_number(cash)),
        ("Total debt", fmt_big_number(debt)),
        ("Net cash / debt", fmt_big_number(net_cash)),
        ("Debt / equity", fmt_pct(meta.get("debt_to_equity"))),
    ]
    valuation_rows = [
        ("AUM" if is_fund_report else "Market cap", fund_assets if is_fund_report else market_cap),
        ("Fund family" if is_fund_report else "Enterprise value", meta.get("fund_family") or "—" if is_fund_report else enterprise_value),
        ("EV/Sales", fmt_mult(meta.get("enterprise_to_revenue"))),
        ("Forward P/E", fmt_mult(fpe)),
        ("Trailing P/E", fmt_mult(meta.get("trailing_pe"))),
        ("PEG", fmt_mult(meta.get("peg"))),
        ("EV/EBITDA", fmt_mult(ev_ebitda)),
        ("Analyst target", fmt_big_number(meta.get("analyst_target")) if meta.get("analyst_target") else "—"),
        ("Analyst view", rec),
    ]
    ownership_rows = [
        ("Short interest", f"{meta.get('short_pct_float'):.1f}% of float" if meta.get("short_pct_float") is not None else "—"),
        ("Institutional ownership", f"{meta.get('institutional_ownership_pct'):.1f}%" if meta.get("institutional_ownership_pct") is not None else "—"),
        ("Insider ownership", f"{meta.get('insider_ownership_pct'):.1f}%" if meta.get("insider_ownership_pct") is not None else "—"),
        ("Dividend yield", fmt_pct(meta.get("dividend_yield")) if meta.get("dividend_yield") is not None else "—"),
        ("Expense ratio", format_expense_pct(meta.get("expense_ratio") or fallback_profile.get("expense_ratio")) or "—"),
        ("Quality tier", q_label),
        ("Tactical state", STATE_STYLES.get(t.get("action"), {}).get("label", t.get("action", "—"))),
    ]
    fund_rows = [
        ("Fund category", category_label),
        ("Fund family", meta.get("fund_family") or fallback_profile.get("fund_family") or "—"),
        ("AUM / assets", fund_assets),
        ("Expense ratio", format_expense_pct(meta.get("expense_ratio") or fallback_profile.get("expense_ratio")) or "—"),
        ("Yield", fmt_pct(meta.get("dividend_yield")) if meta.get("dividend_yield") is not None else "—"),
    ]
    available_count = sum(
        1 for _label, value in (
            earnings_rows + growth_rows + balance_rows + valuation_rows + ownership_rows
        )
        if clean_value(value) != "—"
    )
    total_count = len(earnings_rows + growth_rows + balance_rows + valuation_rows + ownership_rows)
    data_note = (
        f"{available_count}/{total_count} core data fields are available from Yahoo/statement data. "
        "Unavailable items are shown explicitly rather than hidden."
    )
    pm_source_label = format_source_note(pm.get("_source", "the thesis"))
    pm_source_label, pm_source_kind = pm_status_label(pm.get("_source", "the thesis"))
    report_status_html = data_status_html([
        ("Price", price_age_label, price_age_kind),
        ("Benchmark", bench_label, bench_kind),
        ("PM", pm_source_label, pm_source_kind),
        ("Fundamentals", metadata_status_label(meta)[0], metadata_status_label(meta)[1]),
        ("Fields", f"{available_count}/{total_count}", "fresh" if available_count >= total_count * 0.75 else "warn"),
    ])
    watch_items = [
        f"Reclaim or lose the 50-day moving average at ${t.get('ma50', 0):,.2f}.",
        f"Hold the 200-day moving average near ${t.get('ma200', 0):,.2f}.",
        "Watch whether revenue growth is translating into cash flow rather than only headline scale.",
        "Track whether valuation multiples compress because fundamentals disappoint or because the stock de-risks into the numbers.",
    ]
    deep = pm.get("deep_dive") or {}
    variant_bull = deep.get("variant_bull") or ((pm.get("drivers") or ["Execution improves and the market assigns a higher-quality multiple."])[0])
    variant_bear = deep.get("variant_bear") or ((pm.get("risks") or ["Valuation is already discounting too much good news."])[0])
    debate_sentence = (
        f"The debate is whether {str(variant_bull).rstrip('.')} or {str(variant_bear).rstrip('.')}."
    )
    must_be_true = deep.get("must_be_true") or watch_items[:3]
    would_change_mind = deep.get("would_change_mind") or pm.get("risks") or watch_items[1:]

    st.markdown(f"""
<div class="research-page">
  <div class="hero">
    <div class="eyebrow">Full research report · {html.escape(ticker)}</div>
    <h1>{html.escape(company)}</h1>
    <div class="deck">{html.escape(timing)}</div>
    {report_status_html}
    <div class="research-grid">{metrics_html()}</div>
  </div>
  <div class="research-layout">
    <div>
      <div class="research-section research-brief">
        <div class="eyebrow">Executive read</div>
        <h2>{html.escape(action_emoji)} {html.escape(action_label)} · {html.escape(q_label)}</h2>
        <p>{html.escape(decision_context(t))}</p>
        <p><b>Data quality:</b> {html.escape(data_note)}</p>
      </div>
      <div class="research-section">
        <div class="eyebrow">Investment thesis</div>
        <h2>What matters most</h2>
        <p>{html.escape(q_text)}</p>
      </div>
      <div class="research-section">
        <div class="eyebrow">Business model</div>
        <h2>How the company makes money</h2>
        <p>{paragraph(pm.get("thesis"), "Business summary is generated from the PM view and financial profile.")}</p>
      </div>
      <div class="research-section">
        <div class="eyebrow">Financial quality</div>
        <h2>Scale, margins, and cash conversion</h2>
        <p>{html.escape(company)} is showing {html.escape(fmt_pct(revenue_yoy))} revenue growth, {html.escape(fmt_pct(gross_margin))}
        gross margin, and {html.escape(fmt_pct(fcf_margin))} free-cash-flow margin based on the best available
        current data. The key question is whether growth is converting into durable earnings power or simply
        supporting a higher multiple before normalized profitability is proven.</p>
      </div>
      <div class="research-section">
        <div class="eyebrow">Bull / bear debate</div>
        <h2>The variant perception</h2>
        <p><b>{html.escape(debate_sentence)}</b></p>
        <p><b>Bull case:</b> {html.escape(str(variant_bull))}</p>
        <p><b>Bear case:</b> {html.escape(str(variant_bear))}</p>
      </div>
      <div class="research-section">
        <div class="eyebrow">Valuation judgment</div>
        <h2>What the market is paying for</h2>
        <p>{paragraph(pm.get("valuation") or deep.get("valuation_context"), "Valuation context is not available yet.")}</p>
      </div>
      <div class="research-section">
        <div class="eyebrow">Drivers</div>
        <h2>What could make the stock work</h2>
        <ul>{bullets_html(pm.get('drivers'), "Execution improves and the market assigns a higher-quality multiple.")}</ul>
      </div>
      <div class="research-section">
        <div class="eyebrow">Risks</div>
        <h2>What could break the thesis</h2>
        <ul>{bullets_html(pm.get('risks'), "Valuation is already discounting too much good news.")}</ul>
      </div>
    </div>
    <div>
      <div class="research-section">
        <div class="eyebrow">Must be true</div>
        <h2>What has to hold</h2>
        <ul>{bullets_html(must_be_true, "The business must keep converting growth into cash flow and hold key trend support.")}</ul>
      </div>
      <div class="research-section">
        <div class="eyebrow">Would change mind</div>
        <h2>Invalidation signals</h2>
        <ul>{bullets_html(would_change_mind, "A break of trend support plus weakening fundamentals would invalidate the setup.")}</ul>
      </div>
      <div class="research-section">
        <div class="eyebrow">What to watch next</div>
        <h2>Next proof points</h2>
        <ul>{bullets_html(watch_items, "Watch the next earnings print, margin trend, and 50-day moving average.")}</ul>
      </div>
      <div class="research-data-pack">
        <div class="eyebrow">Data appendix</div>
        {table_html("Fund profile", fund_rows) if is_fund_report else table_html("Earnings snapshot", earnings_rows)}
        {table_html("Financial quality", growth_rows)}
        {table_html("Revenue history", history_rows)}
        {table_html("Balance sheet", balance_rows) if not is_fund_report else ""}
        {table_html("Valuation", valuation_rows) if not is_fund_report else ""}
        {table_html("Ownership and setup", ownership_rows)}
        {table_html("Trading overlay", trading_rows)}
      </div>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────
# Copy helpers
# ─────────────────────────────────────────────────────────────────────
STATE_STYLES = {
    "enter_now": {
        "color": "#00A870", "label": "Enter", "emoji": "🚀",
        "tagline": "High-conviction setup — buy without waiting on a condition",
        "criteria": [
            "Bullish bias — above both 50d and 200d MAs, both rising, leading the index",
            "Setup score ≥ 8.5/10 — trend, structure, and volume all aligned",
            "No condition required: buy now",
        ],
    },
    "watch": {
        "color": "#D18700", "label": "Watch", "emoji": "👀",
        "tagline": "Bullish but waiting on a specific trigger",
        "criteria": [
            "Bullish bias is in place",
            "One thing has to happen first: 50d reclaim, breakout, pullback, RS catchup, etc.",
            "Trigger and invalidation levels are defined — act immediately if trigger fires",
        ],
    },
    "hold_off": {
        "color": "#5B6B7D", "label": "Hold off", "emoji": "🤔",
        "tagline": "Default state for ambiguity — pullbacks, transitions, leadership in correction",
        "criteria": [
            "Pullback in uptrend (above 200d, below 50d) — short-term weakness, long-term trend intact",
            "Transition / repair shape — recovering from drawdown",
            "Below 200d but tape still loyal (RS strong) — leadership name in correction",
            "Mixed signals where edge isn't clear in either direction",
            "Universal default when not clearly bullish (Enter/Watch) and not clearly broken (Avoid)",
        ],
    },
    "accumulate": {
        "color": "#7C5DD9", "label": "Accumulate", "emoji": "🌱",
        "tagline": "Quality name stabilizing after deep drawdown — early entry, small sizing only",
        "criteria": [
            "Quality tier A or B — durable business",
            "Drawdown ≥ 35% from 52-week high AND price within 20% of 52-week low",
            "Stabilization signal: RS improving OR no new 30-day low in last 5 sessions",
            "Not actively breaking down: above 20-day MA OR positive 5-day return",
            "Position size: small / staggered. Not a full allocation signal.",
        ],
    },
    "avoid": {
        "color": "#D14545", "label": "Avoid", "emoji": "⛔",
        "tagline": "Truly broken — long-term structure broken AND tape rejecting AND no signs of repair",
        "criteria": [
            "Below the 200-day moving average (long-term structure broken)",
            "Relative strength < 0.9 (tape actively rejecting the name)",
            "RS not improving (10-day delta < 0.01)",
            "Technical score not improving over 10 sessions",
            "Not a quality-drawdown candidate (Accumulation Watch doesn't apply)",
            "ALL FIVE conditions required — short-term weakness alone is never Avoid",
        ],
    },
}


def decision_context(t):
    """One-line context. No numbers."""
    a = t["action"]
    if a == "enter_now":
        if t.get("trigger_fired"):
            return "Enter — prior trigger has fired and held."
        return "High-conviction setup — trend, structure, and volume aligned."
    if a == "watch":
        if t.get("event_risk_watch"):
            return "Watch — setup is valid, but earnings are too close for a fresh entry."
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
        if kind == "historical_support_test":
            level = trg["levels"].get("buy_above")
            meta = trg.get("support_meta", {})
            src = "user-marked" if meta.get("source") == "manual" else (
                "key support level"
            )
            if meta.get("status") == "held_above":
                price = t.get("price")
                price_part = f" above ${float(price):,.2f}" if price else ""
                return (
                    f"{src.capitalize()} at ${level:,.2f} already held. "
                    f"Still Watch; Enter only on continuation{price_part} with volume."
                )
            if meta.get("status") == "wick_test":
                return f"Testing {src} at ${level:,.2f} — needs a reclaim close."
            return f"Approaching {src} at ${level:,.2f} — buy on a hold."
        return f"{bias} — needs confirmation."
    if a == "hold_off":
        if t.get("event_risk_hold"):
            return "Hold off — earnings are imminent, so the setup should reset after the print."
        # Universal fall-through state. Several distinct shapes can land
        # here under the new strict-Avoid model — pick copy that matches
        # the dominant signal.
        price = t.get("price", 0)
        ma50 = t.get("ma50", price)
        ma200 = t.get("ma200", price)
        rs = t.get("rs", 1.0)

        # Pullback in uptrend: above ma200 but below ma50.
        if price > ma200 and price < ma50:
            return "Hold off — short-term trend weakening, long-term trend intact."

        # Below ma200 but tape still loyal (RS holding up).
        if price < ma200 and rs >= 0.95:
            return "Hold off — below the 200-day but tape still respecting the name."

        # NVO-shape: below ma200 with weak RS (<0.95) but the strict-Avoid
        # 5-condition test didn't fire — typically because the name is
        # accumulation-eligible (deep drawdown + first signs of stabilization)
        # OR rs_delta isn't quite negative enough. The "fundamentals say
        # buy, tape says no" pattern. Don't dress this up as a recovery
        # trade — the right answer is patience.
        if price < ma200 and rs < 0.95:
            return ("Hold off — extended drawdown without stabilization. "
                    "Tape is rejecting despite intact fundamentals; wait for "
                    "a real basing pattern or RS turn before engaging.")

        # Marginal bullishness, structurally intact (legacy gray-zone path).
        if price > ma200:
            if rs < 0.95:
                return "Hold off — structure intact but lagging the index."
            if t.get("vol_ratio", 1.0) < 0.8:
                return "Hold off — structure intact but conviction light."
            if t.get("structure_quality", 5) <= 5:
                return "Hold off — structure intact but mixed signals."
            return "Hold off — leaning bullish but not enough edge yet."

        # Below 200-day with weakening but not fully broken tape.
        return "Hold off — structure weakening but not broken — wait for direction."

    if a == "accumulate":
        return "Accumulation Watch — high-quality name stabilizing after deep drawdown."

    if a == "avoid":
        # Under the new strict rules, Avoid means: below 200d AND RS<0.9
        # AND not improving AND no momentum AND not a quality drawdown
        # candidate. Copy reflects that reality.
        if not t["atr_ok"]:
            return "Avoid — daily range too tight for this system to work."
        return "Avoid — below the 200-day with weak relative strength and no signs of repair."

    return ""


def bold_numbers(s):
    import re
    s = re.sub(r"(\$[\d,]+\.?\d*)", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\$>])(\b\d{1,3}(?:,\d{3})+\b)", r"<b>\1</b>", s)
    return s


def trigger_text(t):
    """Short, price-based, single-condition."""
    if t["action"] == "enter_now":
        if t.get("trigger_fired") and t.get("trigger"):
            reason = str(t.get("trigger_fired_reason") or "").strip()
            return reason or "Prior trigger fired — enter at market."
        entry = t.get("entry") or t.get("price")
        return f"Enter long at market — ${entry:,.2f}." if entry is not None else "Enter long at market."
    if t["action"] == "watch" and t.get("trigger"):
        trg = t["trigger"]
        kind = trg["kind"]
        buy = trg.get("levels", {}).get("buy_above")
        if kind == "reclaim_ma50" and buy:
            return f"Reclaim above ${buy:,.2f} (the 50-day MA)."
        if kind in ("fast_momentum", "breakout") and buy:
            return f"Break above ${buy:,.2f} on strong volume."
        if kind == "coil_break" and buy:
            return f"Break above ${buy:,.2f} on expanding volume."
        if kind == "pullback" and buy:
            return f"Pullback to ${buy:,.2f} that holds."
        if kind == "rs_catchup":
            return "Relative strength vs S&P 500 back above 1.00."
        if kind == "historical_support_test" and buy:
            meta = trg.get("support_meta", {})
            descriptor = "user-marked support" if meta.get("source") == "manual" else (
                "key support level"
            )
            if meta.get("status") == "held_above":
                price = t.get("price")
                price_part = f" above ${float(price):,.2f}" if price else ""
                return (
                    f"Support held at ${buy:,.2f}; this is not an entry by itself. "
                    f"Enter only on continuation{price_part} with expanding volume."
                )
            if meta.get("status") == "wick_test":
                return f"Testing ${buy:,.2f} support now — needs a reclaim close."
            return f"Hold of ${buy:,.2f} — {descriptor}, wait for tap-and-bounce."
        if buy:
            return f"Close above ${buy:,.2f}."
        return trg.get("summary", "").capitalize()
    if t["action"] == "watch" and t.get("entry") is not None:
        entry = t.get("entry")
        price = t.get("price")
        try:
            if price and abs(float(entry) - float(price)) / float(price) <= 0.005:
                return f"Watch ${float(entry):,.2f}: close above this level with expanding volume."
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        return f"Target entry at ${entry:,.2f}; wait for confirmation."
    if t["action"] == "avoid":
        if t["raw_bias"] == "bearish":
            return "No action — wait for a confirmed reversal."
        if not t["atr_ok"]:
            return "No action — volatility too low."
        return "No action — no edge."
    return ""


def invalidation_text(t):
    if t["action"] == "enter_now":
        return f"Below ${t['stop']:,.2f}, setup is invalid."
    if t["action"] == "watch" and t.get("trigger"):
        abort = t["trigger"].get("levels", {}).get("abort_below")
        if abort:
            return f"Below ${abort:,.2f}, setup is invalid."
    return None


def position_management_read(entry, t):
    """Exit/take-profit read for an already-entered long position."""
    def _num(value):
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    price = _num(t.get("price"))
    entry_px = _num(entry.get("entry_hit_price")) or _num(entry.get("entry_price")) or _num(entry.get("price"))
    stop_px = _num(entry.get("stop_price"))
    target_px = _num(entry.get("target1_price"))
    ma20 = _num(t.get("ma20"))
    ma50 = _num(t.get("ma50"))
    ma200 = _num(t.get("ma200"))
    rsi = _num(t.get("rsi14"))
    earnings_days = t.get("event_risk_days")

    if price is None or entry_px is None:
        return None
    if entry_px <= 0:
        return None

    entry_ratio = entry_px / price if price else None
    if entry_ratio is not None and (entry_ratio < 0.20 or entry_ratio > 5.0):
        source = str(entry.get("source") or "position").lower()
        action = "Fix holding entry" if source == "holding" else "Confirm entry"
        summary = (
            f"The saved {source} entry (${entry_px:,.2f}) is far away from the current "
            f"price (${price:,.2f}), so the app will not calculate P/L or trim/sell logic from it."
        )
        return {
            "action": action,
            "emoji": "⚠️",
            "color": "var(--color-warning-text)",
            "summary": summary,
            "stats": [
                ("Now", f"${price:,.2f}"),
                ("Saved entry", f"${entry_px:,.2f}"),
                ("P/L", "not shown"),
            ],
            "pnl_pct": None,
            "r_multiple": None,
            "target_hit": False,
            "stop_broken": False,
        }

    pnl_pct = (price / entry_px - 1) * 100
    risk_per_share = entry_px - stop_px if stop_px is not None else None
    r_multiple = None
    if risk_per_share and risk_per_share > 0:
        r_multiple = (price - entry_px) / risk_per_share

    target_gap_pct = None
    if target_px:
        target_gap_pct = (target_px / price - 1) * 100
    stop_gap_pct = None
    if stop_px:
        stop_gap_pct = (price / stop_px - 1) * 100

    extended_vs_20 = bool(ma20 and price > ma20 * 1.08)
    extended_vs_50 = bool(ma50 and price > ma50 * 1.15)
    trend_intact = bool(
        (ma50 is None or price >= ma50) and
        (ma200 is None or price >= ma200)
    )
    target_hit = bool(target_px and price >= target_px)
    stop_broken = bool(stop_px and price <= stop_px)
    near_stop = bool(stop_gap_pct is not None and stop_gap_pct <= 3)
    hot_rsi = bool(rsi and rsi >= 70)
    setup_action = t.get("action")
    trend_broken = bool(
        (ma50 is not None and price < ma50) and
        (ma200 is not None and price < ma200)
    )

    if earnings_days is not None and 0 <= earnings_days <= 2:
        action = "Review after earnings"
        emoji = "📅"
        color = "var(--color-warning-text)"
        summary = "Earnings are imminent. Do not add before the print; decide whether to trim risk or hold through the event deliberately."
    elif stop_broken:
        action = "Exit"
        emoji = "⛔"
        color = "var(--color-negative)"
        summary = "Stop is broken. Close the position unless you are deliberately re-underwriting the thesis."
    elif setup_action == "avoid" and trend_broken:
        action = "Exit"
        emoji = "⛔"
        color = "var(--color-negative)"
        summary = "The position is now in avoid territory with trend support broken. Exit or deliberately re-underwrite from scratch."
    elif target_hit and (extended_vs_20 or hot_rsi):
        action = "Take profit"
        emoji = "💰"
        color = "var(--color-positive)"
        summary = "Target is hit and the move is extended. Trim 1/3 to 1/2, then trail the rest."
    elif target_hit:
        action = "Take profit"
        emoji = "💰"
        color = "var(--color-positive)"
        summary = "Target is hit. Take some profit or raise the stop so the trade cannot turn into a loss."
    elif r_multiple is not None and r_multiple >= 2 and (extended_vs_50 or hot_rsi):
        action = "Trim"
        emoji = "✂️"
        color = "var(--color-warning-text)"
        summary = "You have more than 2R and the stock is stretched. Bank a partial and trail the balance."
    elif r_multiple is not None and r_multiple >= 1.2 and trend_intact:
        action = "Raise stop"
        emoji = "🔒"
        color = "var(--color-warning-text)"
        summary = "The trade is working. Raise the stop toward breakeven or a higher technical level so gains are protected."
    elif near_stop:
        action = "Respect stop"
        emoji = "⚠️"
        color = "var(--color-warning-text)"
        summary = "Price is close to the stop. Do not add; either let the stop work or reduce risk now."
    elif pnl_pct > 0 and ma20 and price < ma20:
        action = "Raise stop"
        emoji = "🔒"
        color = "var(--color-warning-text)"
        summary = "The trade is profitable but short-term trend is slipping. Tighten the stop or trim."
    elif trend_intact:
        action = "Hold"
        emoji = "🟢"
        color = "var(--color-positive)"
        summary = "Trend remains intact. Hold the position and keep the stop/target live."
    else:
        action = "Check exit"
        emoji = "⚠️"
        color = "var(--color-warning-text)"
        summary = "Not an automatic sell, but trend support is weakening. Check the stop, sizing, and whether this still deserves capital."

    stats = [
        ("Entry", f"${entry_px:,.2f}"),
        ("Now", f"${price:,.2f}"),
        ("P/L", f"{pnl_pct:+.1f}%"),
    ]
    if r_multiple is not None:
        stats.append(("R", f"{r_multiple:.1f}R"))
    if target_px is not None:
        stats.append(("Target", f"${target_px:,.2f}"))
        if target_gap_pct is not None:
            stats.append(("To target", f"{target_gap_pct:+.1f}%"))
    if stop_px is not None:
        stats.append(("Stop", f"${stop_px:,.2f}"))
        if stop_gap_pct is not None:
            stats.append(("Stop room", f"{stop_gap_pct:.1f}%"))

    return {
        "action": action,
        "emoji": emoji,
        "color": color,
        "summary": summary,
        "pnl_pct": pnl_pct,
        "r_multiple": r_multiple,
        "target_hit": target_hit,
        "near_stop": near_stop,
        "stats": stats,
    }


def holding_to_position_entry(ticker, holding):
    """Adapt a first-class holding to the position-management shape."""
    if not holding:
        return None
    ticker = str(ticker or holding.get("ticker") or "").upper()
    return {
        "id": holding.get("id") or f"holding-{ticker}",
        "ticker": ticker,
        "ts": holding.get("ts") or holding.get("updated_at") or datetime.now().isoformat(timespec="seconds"),
        "price": holding.get("entry_price"),
        "entry_price": holding.get("entry_price"),
        "entry_hit_price": holding.get("entry_price"),
        "target1_price": holding.get("target1_price"),
        "stop_price": holding.get("stop_price"),
        "user_note": holding.get("user_note", ""),
        "shares": holding.get("shares"),
        "position_status": "entered",
        "source": "holding",
    }


def parse_optional_float(value):
    value = str(value or "").replace("$", "").replace(",", "").strip()
    if not value:
        return None
    return float(value)


def add_or_update_holding(ticker, entry_price, shares=None, target1_price=None, stop_price=None, user_note=""):
    ticker = str(ticker or "").upper().strip()
    if not ticker:
        raise ValueError("Ticker is required.")
    entry = parse_optional_float(entry_price)
    if entry is None:
        raise ValueError("Entry price is required.")
    import uuid
    holdings = st.session_state.store.setdefault("holdings", {})
    existing = holdings.get(ticker, {})
    holdings[ticker] = {
        **existing,
        "id": existing.get("id") or str(uuid.uuid4())[:8],
        "ticker": ticker,
        "entry_price": round(entry, 2),
        "shares": parse_optional_float(shares),
        "target1_price": parse_optional_float(target1_price),
        "stop_price": parse_optional_float(stop_price),
        "user_note": str(user_note or "").strip(),
        "created_at": existing.get("created_at") or datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if ticker not in st.session_state.store.setdefault("watchlist", []):
        st.session_state.store["watchlist"].append(ticker)
    return holdings[ticker]


def cleanup_tracker_synced_holdings():
    """Remove holdings that were auto-created from tracker rows.

    Holdings should be explicit-only: real positions the user added directly
    or via "I own this / add position" on Analyze. Tracker rows stay in
    Tracker unless the user promotes them manually.
    """
    holdings = st.session_state.store.setdefault("holdings", {})
    synced = [
        ticker for ticker, holding in holdings.items()
        if isinstance(holding, dict) and holding.get("source_log_id")
    ]
    for ticker in synced:
        holdings.pop(ticker, None)
    if synced:
        save_store(st.session_state.store)
    return synced


def get_holding_entry(ticker):
    holdings = st.session_state.store.setdefault("holdings", {})
    ticker = str(ticker or "").upper()
    return holding_to_position_entry(ticker, holdings.get(ticker))


def get_logged_position_entry(ticker):
    ticker = str(ticker or "").upper()
    return next(
        (
            d for d in st.session_state.store.get("decisions_log", [])
            if str(d.get("ticker", "")).upper() == ticker
            and d.get("outcome") is None
            and d.get("position_status") == "entered"
        ),
        None,
    )


def get_active_position_entry(ticker):
    """Holdings are authoritative; tracker positions are fallback."""
    return get_holding_entry(ticker) or get_logged_position_entry(ticker)


def active_position_tickers():
    tickers = {
        str(t).upper()
        for t in st.session_state.store.setdefault("holdings", {}).keys()
    }
    tickers.update(
        str(d.get("ticker", "")).upper()
        for d in st.session_state.store.get("decisions_log", [])
        if d.get("outcome") is None and d.get("position_status") == "entered"
    )
    return {t for t in tickers if t}


TRIAL_DAYS = 14
TARGET_COMPARISONS = 15
AUTO_SCORE_VERSION = 2


def tracker_trial_snapshot():
    """Return calibration-trial status shared by Tracker and Watchlist."""
    decisions = st.session_state.store.get("decisions_log", [])
    scored = [d for d in decisions if d.get("outcome") is not None]
    unscored = [d for d in decisions if d.get("outcome") is None]
    snapshot = {
        "decisions": decisions,
        "scored": scored,
        "unscored": unscored,
        "days_in": 0,
        "days_remaining": TRIAL_DAYS,
        "progress_pct": 0,
        "status": (
            f"Trial period: {TRIAL_DAYS} days from first log · "
            f"target {TARGET_COMPARISONS} comparisons · not started yet"
        ),
        "overdue": False,
    }
    if not decisions:
        return snapshot
    try:
        first_ts = min(
            datetime.fromisoformat(d["ts"])
            for d in decisions if d.get("ts")
        )
        trial_end = first_ts + timedelta(days=TRIAL_DAYS)
        days_in = (datetime.now() - first_ts).days
        days_remaining = max(0, (trial_end - datetime.now()).days)
        progress_pct = min(100, round(100 * len(decisions) / TARGET_COMPARISONS))
        status = (
            f"Day {days_in} of {TRIAL_DAYS} · "
            f"{len(decisions)}/{TARGET_COMPARISONS} comparisons logged · "
            f"{days_remaining}d remaining"
        )
        overdue = days_remaining == 0 and bool(unscored)
        if days_remaining == 0:
            status += " · trial complete — score outcomes"
        snapshot.update({
            "days_in": days_in,
            "days_remaining": days_remaining,
            "progress_pct": progress_pct,
            "status": status,
            "overdue": overdue,
        })
    except Exception:
        snapshot["status"] = f"{len(decisions)} comparisons logged"
        snapshot["overdue"] = bool(unscored)
    return snapshot


def open_tracker_view():
    st.session_state.view = "tracker"
    st.session_state.store["last_view"] = "tracker"
    try:
        st.query_params["view"] = "tracker"
    except Exception:
        pass
    save_store(st.session_state.store)
    st.rerun()


def _tracker_action_family(action):
    action = str(action or "").upper().replace("_NOW", "").replace("_", " ")
    if action in {"ENTER", "ACCUMULATE"}:
        return "long"
    if action == "AVOID":
        return "avoid"
    if action in {"WATCH", "HOLD OFF", "HOLD"}:
        return "wait"
    return ""


def _compact_dissent_note(text, limit=260):
    """Keep Claude's stock-specific dissent readable in scan panels."""
    text = " ".join(str(text or "").strip().split())
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" .") + "..."


def claude_dissent_signal(rule_action, claude_action, confidence, note=""):
    """Flag high-confidence Claude disagreement without letting it replace rules."""
    rule_family = _tracker_action_family(rule_action)
    claude_family = _tracker_action_family(claude_action)
    try:
        confidence = int(confidence or 0)
    except (TypeError, ValueError):
        confidence = 0
    if not rule_family or not claude_family or confidence < 7:
        return {"flag": False, "label": "", "reason": ""}
    if rule_family == claude_family:
        return {"flag": False, "label": "", "reason": ""}
    rule_label = STATE_STYLES.get(rule_action, {}).get(
        "label", str(rule_action or "rules").replace("_", " ").title()
    )
    claude_key = normalize_action_key(claude_action)
    claude_label = STATE_STYLES.get(claude_key, {}).get(
        "label", str(claude_action or "Claude").replace("_", " ").title()
    )
    return {
        "flag": True,
        "label": "★ Claude dissent",
        "reason": f"Claude {claude_label} ({confidence}/10) vs rules {rule_label}",
        "note": _compact_dissent_note(note),
    }


def _tracker_first_hit(hist, start_date, level, direction):
    if hist is None or level is None:
        return None
    try:
        level = float(level)
    except (TypeError, ValueError):
        return None
    for idx, bar in hist.iterrows():
        try:
            bar_date = idx.date() if hasattr(idx, "date") else idx.to_pydatetime().date()
            if start_date and bar_date < start_date:
                continue
            high = float(bar.get("High", bar.get("Close")))
            low = float(bar.get("Low", bar.get("Close")))
        except Exception:
            continue
        if direction == "up" and high >= level:
            return bar_date
        if direction == "down" and low <= level:
            return bar_date
    return None


def auto_close_tracker_outcomes(force_all=False):
    """Auto-score stale calibration rows so the trial produces decisions."""
    decisions = st.session_state.store.get("decisions_log", [])
    changed = 0
    today = datetime.now().date()
    for entry in decisions:
        existing_outcome = entry.get("outcome") or {}
        if entry.get("outcome") is not None and not (
            force_all
            and existing_outcome.get("auto_scored")
            and existing_outcome.get("score_version", 0) < AUTO_SCORE_VERSION
        ):
            continue
        try:
            logged_dt = datetime.fromisoformat(str(entry.get("ts"))).date()
        except Exception:
            logged_dt = today
        age_days = (today - logged_dt).days
        if not force_all and age_days < TRIAL_DAYS:
            continue

        ticker = str(entry.get("ticker") or "").upper().strip()
        if not ticker:
            continue
        hist, _, _ = fetch_history(ticker)
        if hist is None or len(hist) == 0:
            continue

        try:
            after = hist[hist.index.date >= logged_dt] if hasattr(hist.index, "date") else hist
            if after is None or len(after) == 0:
                after = hist
            current_price = float(after["Close"].iloc[-1])
        except Exception:
            continue

        def _num(value):
            try:
                if value is None:
                    return None
                return float(value)
            except (TypeError, ValueError):
                return None

        ref_price = _num(entry.get("price")) or current_price
        entry_px = _num(entry.get("entry_hit_price")) or _num(entry.get("entry_price")) or ref_price
        ref_return = (current_price - ref_price) / ref_price if ref_price else 0
        entry_return = (current_price - entry_px) / entry_px if entry_px else ref_return
        score_return = ref_return
        if entry.get("position_status") == "entered" and entry_px:
            score_return = entry_return

        if score_return >= 0.03:
            winning_family = "long"
            reason = f"today's price is {score_return * 100:.1f}% above the logged reference"
        elif score_return <= -0.03:
            winning_family = "avoid"
            reason = f"today's price is {score_return * 100:.1f}% below the logged reference"
        else:
            winning_family = "wait"
            reason = f"today's price is only {score_return * 100:.1f}% from the logged reference"

        source_actions = {
            "rules": entry.get("rule_action"),
            "claude": entry.get("claude_action"),
            "user": entry.get("user_action"),
        }
        credit_families = {
            "long": {"long"},
            "avoid": {"avoid", "wait"},
            "wait": {"wait"},
        }.get(winning_family, {winning_family})
        right_sources = [
            source
            for source, action in source_actions.items()
            if _tracker_action_family(action) in credit_families
        ]
        new_outcome = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "result": "auto_scored",
            "right_sources": right_sources,
            "result_pct": None,
            "note": (
                f"Auto-scored from today's price: {reason}. "
                f"Current {current_price:.2f}; logged/ref {ref_price:.2f}."
            ),
            "auto_scored": True,
            "winning_family": winning_family,
            "credit_families": sorted(credit_families),
            "score_version": AUTO_SCORE_VERSION,
        }
        if entry.get("outcome") != new_outcome:
            entry["outcome"] = new_outcome
            changed += 1

    if changed:
        save_store(st.session_state.store)
    return changed


def _decision_signal_snapshot(t_state):
    """Compact tape snapshot used by the Claude-vs-rules comparison."""
    price = t_state.get("price") or 0
    ma20 = t_state.get("ma20") or 0
    ma50 = t_state.get("ma50") or 0
    ma200 = t_state.get("ma200") or 0
    rs = t_state.get("rs")
    rr = t_state.get("reward_risk")
    setup = t_state.get("setup_score")
    rsi = t_state.get("rsi14")
    vol_ratio = t_state.get("vol_ratio")
    reads = []

    if setup is not None:
        try:
            reads.append(f"setup {float(setup):.1f}/10")
        except (TypeError, ValueError):
            pass
    if rr is not None:
        try:
            reads.append(f"reward/risk {float(rr):.2f}:1")
        except (TypeError, ValueError):
            pass
    if price and ma20:
        reads.append(f"{((price - ma20) / ma20 * 100):+.1f}% vs 20d")
    if price and ma50:
        reads.append(f"{((price - ma50) / ma50 * 100):+.1f}% vs 50d")
    if price and ma200:
        reads.append(f"{((price - ma200) / ma200 * 100):+.1f}% vs 200d")
    if rs is not None:
        try:
            reads.append(f"RS {float(rs):.2f}")
        except (TypeError, ValueError):
            pass
    if rsi is not None:
        try:
            reads.append(f"RSI {float(rsi):.0f}")
        except (TypeError, ValueError):
            pass
    if vol_ratio is not None:
        try:
            reads.append(f"vol {float(vol_ratio):.1f}x")
        except (TypeError, ValueError):
            pass
    return ", ".join(reads[:5])


def classify_decision_disagreement(rule_key, claude_key, t_state, claude_data=None, position_read=None):
    """Explain what kind of disagreement exists and how to resolve it."""
    claude_data = claude_data or {}

    rule_label = STATE_STYLES.get(rule_key, {}).get(
        "label", str(rule_key or "Rules").replace("_", " ").title()
    )
    claude_label = STATE_STYLES.get(claude_key, {}).get(
        "label", str(claude_key or "Claude").replace("_", " ").title()
    )
    signal = _decision_signal_snapshot(t_state)
    trigger = t_state.get("trigger") or {}
    trigger_kind = (trigger.get("kind") or "").replace("_", " ")
    reasoning = (claude_data.get("reasoning") or "").lower()

    if not claude_key:
        return {
            "kind": "Data gap",
            "emoji": "🧩",
            "title": "Claude data missing",
            "read": (
                "Refresh Portfolio Manager before treating this as a real comparison. "
                "The rule engine can still define the trigger, but the PM thesis side is stale."
            ),
        }

    if position_read and position_read.get("action") in {
        "Exit", "Take profit", "Trim", "Raise stop", "Respect stop", "Review after earnings"
    }:
        return {
            "kind": "Position conflict",
            "emoji": "📌",
            "title": "New-entry signal vs existing-position management",
            "read": (
                f"The position read says {position_read.get('action')}. Treat the comparison as a "
                "fresh-entry view only; for capital already at work, follow the trim/sell box first."
            ),
        }

    if rule_key == claude_key:
        read = f"Both sources read this as {rule_label}."
        if signal:
            read += f" Shared tape: {signal}."
        return {
            "kind": "Agreement",
            "emoji": "✅",
            "title": "No conflict",
            "read": read,
        }

    if t_state.get("event_risk_hold") or t_state.get("event_risk_watch"):
        days = t_state.get("event_risk_days")
        day_text = f" in {days} days" if days is not None else " soon"
        return {
            "kind": "Event risk",
            "emoji": "📅",
            "title": "The catalyst overrides the setup",
            "read": (
                f"Earnings are{day_text}. Even if one source likes the setup, do not add before the print. "
                "Let the stock reset, then rerun the call with the new price, gap, and guidance."
            ),
        }

    if rule_key in ("enter_now", "watch") and claude_key in ("hold_off", "avoid"):
        if any(word in reasoning for word in ("valuation", "earnings", "debt", "risk", "stretched", "extension")):
            kind = "Risk conflict"
            title = "Rules like the tape; Claude is worried about underwriting"
            read = (
                "Use the rules for the trigger, but size or skip based on Claude's risk objection. "
                "This is not an automatic buy until the thesis risk is acceptable."
            )
        else:
            kind = "Timing conflict"
            title = "Rules see a setup; Claude wants a cleaner entry"
            read = (
                "Treat this as a watchlist setup, not a chase. Let the price trigger fire with confirmation "
                "or wait for the pullback/reclaim Claude is asking for."
            )
        if trigger_kind:
            read += f" Trigger focus: {trigger_kind}."
        if signal:
            read += f" Current tape: {signal}."
        return {"kind": kind, "emoji": "⚖️", "title": title, "read": read}

    if claude_key in ("enter_now", "watch", "accumulate") and rule_key in ("hold_off", "avoid"):
        price = t_state.get("price") or 0
        ma50 = t_state.get("ma50") or 0
        rr = t_state.get("reward_risk")
        too_extended = bool(price and ma50 and price > ma50 * 1.08)
        poor_rr = False
        try:
            poor_rr = rr is not None and float(rr) < 1.5
        except (TypeError, ValueError):
            poor_rr = False

        if too_extended or poor_rr:
            kind = "Timing conflict"
            title = "Claude likes the name; rules reject the entry math"
            read = (
                "Use Claude for the thesis and the rule engine for entry discipline. "
                "Do not force a fresh buy until reward/risk improves or the trigger resets."
            )
        elif rule_key == "avoid":
            kind = "Quality vs tape"
            title = "Claude sees quality; rules see technical damage"
            read = (
                "Keep it in research mode. The business may be good, but wait for the tape to stop rejecting it "
                "before committing tactical capital."
            )
        else:
            kind = "Conviction gap"
            title = "Claude is more willing to anticipate"
            read = (
                "Let Claude define why it belongs on the screen, but wait for the rule trigger before logging an entry."
            )
        if signal:
            read += f" Current tape: {signal}."
        return {"kind": kind, "emoji": "🧭", "title": title, "read": read}

    if "avoid" in (rule_key, claude_key):
        return {
            "kind": "Damage check",
            "emoji": "⛔",
            "title": "Weakness may be structural",
            "read": (
                "Resolve this defensively: require a reclaim, improving RS, and a defined stop before treating it "
                "as anything more than repair work."
            ) + (f" Current tape: {signal}." if signal else ""),
        }

    if "accumulate" in (rule_key, claude_key):
        return {
            "kind": "Sizing conflict",
            "emoji": "🌱",
            "title": "Long-term quality vs tactical timing",
            "read": (
                "If you act, make it a small starter only. Add size only after the trigger confirms and the trend improves."
            ) + (f" Current tape: {signal}." if signal else ""),
        }

    return {
        "kind": "Conviction gap",
        "emoji": "⚖️",
        "title": f"Claude: {claude_label}; rules: {rule_label}",
        "read": (
            "The split is mainly about timing and conviction. Let the trigger decide: no trigger, no fresh capital."
        ) + (f" Current tape: {signal}." if signal else ""),
    }


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
        rows.append(("Trend", f"Above 50d (${ma50:,.2f}, +{ma50_gap_pct:.1f}%) and 200d (${ma200:,.2f}, +{ma200_gap_pct:.1f}%)", "pos"))
    elif price < ma50 and price < ma200:
        rows.append(("Trend", f"Below 50d (${ma50:,.2f}, {ma50_gap_pct:.1f}%) and 200d (${ma200:,.2f}, {ma200_gap_pct:.1f}%)", "neg"))
    elif price < ma50:
        rows.append(("Trend", f"Below 50d (${ma50:,.2f}, {ma50_gap_pct:.1f}%), above 200d (${ma200:,.2f}, +{ma200_gap_pct:.1f}%)", ""))
    else:
        rows.append(("Trend", f"Above 50d (${ma50:,.2f}, +{ma50_gap_pct:.1f}%), below 200d (${ma200:,.2f}, {ma200_gap_pct:.1f}%)", ""))

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
    """For Avoid OR Hold-off states, list concrete reasons the system isn't
    saying go. The wording shifts based on action: 'broken' for avoid,
    'not yet aligned' for hold_off. Each reason references actual numbers."""
    reasons = []
    price = t["price"]
    ma50 = t["ma50"]
    ma200 = t["ma200"]
    bias = t.get("raw_bias")
    action = t.get("action", "avoid")
    rs = t.get("rs", 1.0)
    sq = t.get("structure_quality", 5)
    atr_pct = t.get("atr_pct", 0)
    rs_delta = t.get("rs_delta", 0)
    vol_ratio = t.get("vol_ratio", 1.0)
    tech_delta = t.get("tech_delta", 0)
    bias_score = t.get("bias_score", 0)

    if action == "hold_off":
        if t.get("event_risk_hold"):
            reasons.append(
                "Earnings are within two trading days — event risk can gap through both trigger and stop."
            )
            return reasons

        # New universal-fallthrough hold_off covers several distinct shapes.
        # Cite the dominant one(s) — what's MISSING, not what's broken.

        # Shape 1: pullback in uptrend (above ma200, below ma50)
        if price > ma200 and price < ma50:
            reasons.append(
                f"Below the 50-day (${ma50:,.2f}) but holding above the 200-day "
                f"(${ma200:,.2f}) — short-term pullback in an intact long-term trend."
            )

        # Shape 2: below ma200 but tape still loyal
        if price < ma200 and rs >= 0.95:
            reasons.append(
                f"Below the 200-day (${ma200:,.2f}) but relative strength "
                f"{rs:.2f} suggests the tape is still respecting this name — "
                f"long-term structure is breaking, leadership isn't yet."
            )

        # Always-applicable signal callouts (light-touch — only cite the
        # ones that stand out)
        if rs < 0.95 and price > ma200:
            reasons.append(f"Relative strength {rs:.2f} — lagging the S&P 500.")
        if vol_ratio < 0.85:
            reasons.append(f"Volume {vol_ratio:.2f}× the 20-day average — light participation.")
        if tech_delta < -0.5:
            reasons.append(f"Technical score drifting down ({tech_delta:+.1f} over 10 sessions) — momentum cooling.")
        if sq <= 5 and price > ma200:
            reasons.append(f"Structure score {sq:.1f}/10 — pattern isn't clean enough to anchor a trade plan.")

        if not reasons:
            reasons.append(
                f"Mixed signals — bias score {bias_score:+d}/±10. "
                f"Directionally ambiguous, no edge to act on yet."
            )
        return reasons

    # Default: AVOID copy — under the new strict rules, this means below
    # ma200 AND weak RS AND not improving AND no momentum AND not a
    # quality drawdown candidate. Cite those specifically — never blame
    # the 50-day alone.
    if not t.get("atr_ok", True):
        reasons.append(f"Average true range is {atr_pct*100:.2f}% — below the 1.5% floor this system needs to work.")

    if price < ma200:
        reasons.append(f"Below the 200-day moving average (${ma200:,.2f}) — long-term structure has broken.")

    if rs < 0.9:
        reasons.append(f"Relative strength {rs:.2f} — tape is actively rejecting this name (vs 0.9 threshold).")

    if rs_delta < 0.01:
        reasons.append(f"Relative strength not improving (Δ {rs_delta:+.3f} over 10 sessions) — no signs of leadership repair.")

    if tech_delta <= 0:
        reasons.append(f"Technical score Δ {tech_delta:+.1f} over 10 sessions — no momentum recovery.")

    if not reasons:
        reasons.append("Multiple structural and tape signals weak simultaneously — trend, leadership, and momentum all negative.")
    return reasons


def reconsider_when(t):
    """Concrete conditions that would flip this from Avoid/Hold off to Watch.

    Each candidate price level is tagged on TWO dimensions:
      - DISTANCE: near (≤7%), mid (7-15%), far (>15%)
      - IMPORTANCE: primary, secondary, reference

    Selection rule: lead with one level that is BOTH (near OR mid) AND
    primary. Mid-distance Primary levels can lead if structurally
    important. Never lead with Far levels regardless of importance.

    Always include at least one signal-based proximate condition (RS
    turning, momentum, no-new-lows) so the user gets a leading-indicator
    answer to "what would change my mind in the near term."
    """
    conditions = []
    price = t["price"]
    ma50 = t["ma50"]
    ma200 = t["ma200"]
    high_52w = t.get("high_52w", price)
    swing_high_60d = t.get("swing_high_60d", high_52w)
    rs = t.get("rs", 1.0)
    rs_delta = t.get("rs_delta", 0)
    tech_delta = t.get("tech_delta", 0)

    # ── Volatility-gated avoid: only one reversal condition matters ──
    if not t.get("atr_ok", True):
        conditions.append(
            f"Daily range expands above 1.5% (currently {t.get('atr_pct', 0)*100:.2f}%)."
        )
        return conditions

    # ── Tier candidate levels by distance ──
    def distance_tier(level):
        if level <= price:
            return None
        pct = (level - price) / price
        if pct <= 0.07:  return "near"
        if pct <= 0.15:  return "mid"
        return "far"

    # ── Tag candidates with importance ──
    # Importance is structural, not distance-based:
    #   - ma200 reclaim: always Primary (defines long-term trend)
    #   - ma50 reclaim:  Primary if above ma200 (real pullback support);
    #                    Secondary if below ma200 (a reclaim wouldn't fix
    #                    the bigger picture)
    #   - swing_high_60d: Primary if it's distinctly above current price
    #                    AND distinct from the MAs (>2% gap to nearest MA)
    #                    — this is the recent breakout level
    #   - 52w high: Primary only when close (≤5%) — true breakout pivot;
    #               Reference otherwise
    candidates = []  # list of (label, level, distance_tier, importance)

    if ma200 > price:
        d = distance_tier(ma200)
        candidates.append(("200-day moving average", ma200, d, "primary"))

    if ma50 > price:
        d = distance_tier(ma50)
        # Primary if a reclaim would put us back above the ma200 line
        # (i.e. price is currently above ma200 already, or ma50 is above
        # ma200 so reclaiming ma50 = strong recovery). Otherwise Secondary.
        importance = "primary" if (price >= ma200 or ma50 > ma200) else "secondary"
        candidates.append(("50-day moving average", ma50, d, importance))

    if swing_high_60d > price * 1.005:
        d = distance_tier(swing_high_60d)
        # Primary only if the swing high is meaningfully distinct from
        # ma50/ma200 (within 2% of either MA = redundant).
        too_close_to_ma = (
            abs(swing_high_60d - ma50) / ma50 < 0.02 or
            abs(swing_high_60d - ma200) / ma200 < 0.02
        )
        importance = "reference" if too_close_to_ma else "primary"
        candidates.append(("recent swing high", swing_high_60d, d, importance))

    if high_52w > price:
        d = distance_tier(high_52w)
        importance = "primary" if d == "near" else "reference"
        candidates.append(("52-week high", high_52w, d, importance))

    # ── Select THE primary level to lead with ──
    # Rule: must be (near OR mid) AND primary. Near beats mid. Among ties,
    # nearest level wins.
    leadable = [c for c in candidates
                if c[2] in ("near", "mid") and c[3] == "primary"]
    if leadable:
        leadable.sort(key=lambda c: (c[2] != "near", c[1]))  # near first, then nearest
        lead = leadable[0]
        name, level, dist, _ = lead
        if dist == "near":
            conditions.append(
                f"A clean break above the {name} at ${level:,.2f} on rising volume."
            )
        else:
            pct = (level / price - 1) * 100
            conditions.append(
                f"A reclaim of the {name} at ${level:,.2f} ({pct:+.0f}% from here) "
                f"on rising volume."
            )
        leading_with_price_level = True
    else:
        leading_with_price_level = False

    # ── Always include signal-based proximate conditions ──
    signal_conditions = []
    if rs < 1.0:
        if rs_delta < 0.02:
            signal_conditions.append(
                f"Relative strength climbs back toward 1.00 with daily improvement "
                f"(currently {rs:.2f}, Δ {rs_delta:+.3f} over 10 sessions)."
            )
        else:
            signal_conditions.append(
                f"Relative strength continues improving from {rs:.2f} toward 1.00 — "
                f"the tape starting to lead is the earliest repair signal."
            )
    if tech_delta <= 0:
        signal_conditions.append(
            "Technical score turns positive over a 10-session window — "
            "momentum reversing is the next confirmation."
        )

    # No-new-lows is a particularly clean signal for deeply-broken names
    if price < ma200 and not leading_with_price_level:
        signal_conditions.append(
            "Price holds without setting a new 30-day low for several weeks — "
            "structural basing is required before any reclaim becomes plausible."
        )

    if leading_with_price_level:
        # Supplement the price-level lead with 1-2 signal conditions
        conditions.extend(signal_conditions[:2])
    else:
        # No leadable price level. Lead with signals; mention the closest
        # Far/Reference level as long-horizon context only.
        conditions.extend(signal_conditions[:2])

        far_or_ref = [c for c in candidates if c[2] == "far" or c[3] == "reference"]
        if far_or_ref:
            far_or_ref.sort(key=lambda c: c[1])  # nearest first
            name, level, _, _ = far_or_ref[0]
            pct = (level / price - 1) * 100
            conditions.append(
                f"Eventually, a reclaim of the {name} at ${level:,.2f} ({pct:+.0f}% away) — "
                f"but treat that as a long-horizon target, not a near-term trigger."
            )

    if not conditions:
        conditions.append(
            "Trend, structure, and relative strength turn positive together "
            "across a multi-week window."
        )
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


def _fmt_signed_pct(value, digits=1):
    try:
        return f"{float(value):+.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_px(value):
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def _td_setup_count(closes):
    """Approximate DeMark TD Sequential setup count.

    Buy setup: consecutive closes below the close four bars earlier.
    Sell setup: consecutive closes above the close four bars earlier.
    Counts reset on opposite signal. This is intentionally a setup read,
    not a full proprietary TD Sequential implementation.
    """
    if closes is None or len(closes) < 5:
        return {"side": "—", "count": 0, "status": "insufficient history"}
    buy_count = 0
    sell_count = 0
    for i in range(4, len(closes)):
        close = float(closes.iloc[i])
        prior = float(closes.iloc[i - 4])
        if close < prior:
            buy_count += 1
            sell_count = 0
        elif close > prior:
            sell_count += 1
            buy_count = 0
        else:
            buy_count = 0
            sell_count = 0
    if sell_count:
        status = "exhaustion watch" if sell_count >= 8 else "upside setup building"
        return {"side": "Sell", "count": min(sell_count, 9), "status": status}
    if buy_count:
        status = "downside exhaustion watch" if buy_count >= 8 else "downside setup building"
        return {"side": "Buy", "count": min(buy_count, 9), "status": status}
    return {"side": "—", "count": 0, "status": "no active setup"}


def _technical_snapshot_from_hist(hist, bench, t):
    closes = hist["Close"].dropna()
    weekly = hist.resample("W-FRI").agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }).dropna()

    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd = ema12 - ema26
    macd_signal = _ema(macd, 9)
    macd_hist = macd - macd_signal
    macd_now = float(macd.iloc[-1]) if len(macd) else 0.0
    macd_sig_now = float(macd_signal.iloc[-1]) if len(macd_signal) else 0.0
    macd_hist_now = float(macd_hist.iloc[-1]) if len(macd_hist) else 0.0
    macd_hist_prev = float(macd_hist.iloc[-2]) if len(macd_hist) > 1 else macd_hist_now

    weekly_closes = weekly["Close"].dropna()
    weekly_ma10 = float(weekly_closes.rolling(10).mean().iloc[-1]) if len(weekly_closes) >= 10 else None
    weekly_ma30 = float(weekly_closes.rolling(30).mean().iloc[-1]) if len(weekly_closes) >= 30 else None
    weekly_price = float(weekly_closes.iloc[-1]) if len(weekly_closes) else t.get("price")
    weekly_rsi = tactical._rsi(weekly_closes) if len(weekly_closes) >= 15 else None

    td_daily = _td_setup_count(closes)
    td_weekly = _td_setup_count(weekly_closes)

    returns = closes.pct_change()
    realized_vol_20 = float(returns.iloc[-20:].std() * (252 ** 0.5) * 100) if len(returns) >= 20 else None
    high_20 = float(hist["High"].iloc[-20:].max()) if len(hist) >= 20 else None
    low_20 = float(hist["Low"].iloc[-20:].min()) if len(hist) >= 20 else None

    bench_rs_20 = None
    if bench is not None and len(bench) >= 21 and len(hist) >= 21:
        try:
            t_ret = closes.iloc[-1] / closes.iloc[-21] - 1
            b_closes = bench["Close"].dropna()
            b_ret = b_closes.iloc[-1] / b_closes.iloc[-21] - 1
            bench_rs_20 = (t_ret - b_ret) * 100
        except Exception:
            bench_rs_20 = None

    ma20 = t.get("ma20")
    ma50 = t.get("ma50")
    ma100 = t.get("ma100")
    ma200 = t.get("ma200")
    price = t.get("price")
    stack_bullish = bool(price and ma20 and ma50 and ma100 and ma200 and price > ma20 > ma50 > ma100 > ma200)
    stack_bearish = bool(price and ma20 and ma50 and ma100 and ma200 and price < ma20 < ma50 < ma100 < ma200)
    if stack_bullish:
        stack_read = "full bullish stack"
        stack_sev = "pos"
    elif stack_bearish:
        stack_read = "full bearish stack"
        stack_sev = "neg"
    elif price and ma50 and ma200 and price > ma50 and price > ma200:
        stack_read = "above core trend"
        stack_sev = "pos"
    elif price and ma50 and ma200 and price < ma50 and price < ma200:
        stack_read = "below core trend"
        stack_sev = "neg"
    else:
        stack_read = "mixed / transition"
        stack_sev = ""

    weekly_trend_pos = bool(weekly_ma10 and weekly_ma30 and weekly_price > weekly_ma10 > weekly_ma30)
    weekly_trend_neg = bool(weekly_ma10 and weekly_ma30 and weekly_price < weekly_ma10 < weekly_ma30)
    if weekly_trend_pos:
        weekly_read = "weekly uptrend confirmed"
        weekly_sev = "pos"
    elif weekly_trend_neg:
        weekly_read = "weekly downtrend confirmed"
        weekly_sev = "neg"
    else:
        weekly_read = "weekly mixed"
        weekly_sev = ""

    if macd_now > macd_sig_now and macd_hist_now > macd_hist_prev:
        macd_read = "bullish and improving"
        macd_sev = "pos"
    elif macd_now > macd_sig_now:
        macd_read = "bullish but flattening"
        macd_sev = ""
    elif macd_now < macd_sig_now and macd_hist_now < macd_hist_prev:
        macd_read = "bearish and deteriorating"
        macd_sev = "neg"
    else:
        macd_read = "bearish but improving"
        macd_sev = ""

    return {
        "stack_read": stack_read,
        "stack_sev": stack_sev,
        "weekly_read": weekly_read,
        "weekly_sev": weekly_sev,
        "weekly_ma10": weekly_ma10,
        "weekly_ma30": weekly_ma30,
        "weekly_rsi": weekly_rsi,
        "macd": macd_now,
        "macd_signal": macd_sig_now,
        "macd_hist": macd_hist_now,
        "macd_read": macd_read,
        "macd_sev": macd_sev,
        "td_daily": td_daily,
        "td_weekly": td_weekly,
        "realized_vol_20": realized_vol_20,
        "high_20": high_20,
        "low_20": low_20,
        "bench_rs_20": bench_rs_20,
    }


def detailed_technical_rows(hist, bench, t):
    snap = _technical_snapshot_from_hist(hist, bench, t)
    price = t.get("price")
    rsi = t.get("rsi14")
    rs = t.get("rs", 1.0)
    rs_delta = t.get("rs_delta", 0)
    vol_ratio = t.get("vol_ratio", 1.0)
    pct_52w = t.get("pct_of_52w_range")

    ma_rows = [
        ("MA stack", snap["stack_read"], snap["stack_sev"]),
        ("20d", f"{_fmt_px(t.get('ma20'))} · {_fmt_signed_pct((price / t.get('ma20') - 1) * 100 if price and t.get('ma20') else None)}", ""),
        ("50d", f"{_fmt_px(t.get('ma50'))} · {_fmt_signed_pct((price / t.get('ma50') - 1) * 100 if price and t.get('ma50') else None)}", ""),
        ("100d", f"{_fmt_px(t.get('ma100'))} · {_fmt_signed_pct((price / t.get('ma100') - 1) * 100 if price and t.get('ma100') else None)}", ""),
        ("200d", f"{_fmt_px(t.get('ma200'))} · {_fmt_signed_pct((price / t.get('ma200') - 1) * 100 if price and t.get('ma200') else None)}", ""),
    ]
    momentum_rows = [
        ("RSI 14", f"{rsi:.0f}" if rsi is not None else "—", "neg" if rsi and (rsi >= 75 or rsi <= 30) else ("pos" if rsi and 50 <= rsi < 70 else "")),
        ("MACD", f"{snap['macd']:.2f} vs signal {snap['macd_signal']:.2f} · hist {snap['macd_hist']:+.2f}", snap["macd_sev"]),
        ("MACD read", snap["macd_read"], snap["macd_sev"]),
        ("TD daily", f"{snap['td_daily']['side']} {snap['td_daily']['count']}/9 · {snap['td_daily']['status']}", "neg" if snap["td_daily"]["side"] == "Sell" and snap["td_daily"]["count"] >= 8 else ""),
        ("TD weekly", f"{snap['td_weekly']['side']} {snap['td_weekly']['count']}/9 · {snap['td_weekly']['status']}", "neg" if snap["td_weekly"]["side"] == "Sell" and snap["td_weekly"]["count"] >= 8 else ""),
    ]
    strength_rows = [
        ("Relative strength", f"{rs:.2f} vs SPY · 10d {rs_delta:+.2f}", "pos" if rs >= 1.05 else ("neg" if rs < 0.95 else "")),
        ("20d alpha", _fmt_signed_pct(snap["bench_rs_20"]), "pos" if snap["bench_rs_20"] and snap["bench_rs_20"] > 0 else ("neg" if snap["bench_rs_20"] and snap["bench_rs_20"] < 0 else "")),
        ("Volume", f"{vol_ratio:.2f}× 20d average", "pos" if vol_ratio >= 1.2 else ("neg" if vol_ratio <= 0.7 else "")),
        ("20d realized vol", f"{snap['realized_vol_20']:.1f}%" if snap["realized_vol_20"] is not None else "—", ""),
        ("52w position", f"{pct_52w:.0f}%" if pct_52w is not None else "—", "pos" if pct_52w and pct_52w >= 70 else ("neg" if pct_52w and pct_52w <= 20 else "")),
    ]
    timeframe_rows = [
        ("Daily", snap["stack_read"], snap["stack_sev"]),
        ("Weekly", snap["weekly_read"], snap["weekly_sev"]),
        ("Weekly 10w", _fmt_px(snap["weekly_ma10"]), ""),
        ("Weekly 30w", _fmt_px(snap["weekly_ma30"]), ""),
        ("Weekly RSI", f"{snap['weekly_rsi']:.0f}" if snap["weekly_rsi"] is not None else "—", ""),
    ]
    levels_rows = [
        ("20d high", _fmt_px(snap["high_20"]), ""),
        ("20d low", _fmt_px(snap["low_20"]), ""),
        ("52w high", _fmt_px(t.get("high_52w")), ""),
        ("52w low", _fmt_px(t.get("low_52w")), ""),
        ("ATR", f"{(t.get('atr_pct') or 0) * 100:.2f}%", "neg" if not t.get("atr_ok", True) else ""),
    ]
    return ma_rows, momentum_rows, strength_rows, timeframe_rows, levels_rows


def render_technical_table(title, rows):
    color_map = {"pos": "#2E7D4F", "neg": "#D14545", "": "var(--color-text)"}
    body = "".join(
        f'<div class="tech-memo-row">'
        f'<span class="k">{html.escape(label)}</span>'
        f'<span class="v" style="color:{color_map.get(sev, "var(--color-text)")};">{bold_numbers(html.escape(str(value)))}</span>'
        f'</div>'
        for label, value, sev in rows
    )
    return (
        '<div class="tech-memo-table">'
        f'<div class="tech-memo-title">{html.escape(title)}</div>'
        f'{body}'
        '</div>'
    )


def _valid_view(value, default="analyze"):
    view_value = str(value or "").strip().lower()
    if view_value in ACTIVE_VIEWS:
        return view_value
    if SHOW_ARCHIVED_TRACKER and view_value in ARCHIVED_VIEWS:
        return view_value
    return default


def _clean_ticker(value):
    return str(value or "").upper().strip()


def _query_get(key):
    try:
        return st.query_params.get(key)
    except Exception:
        return None


def _query_set(key, value):
    try:
        if value is None:
            if key in st.query_params:
                del st.query_params[key]
        elif st.query_params.get(key) != str(value):
            st.query_params[key] = str(value)
    except Exception:
        pass


def route_to(*, ticker=None, view=None, reason="", sync_url=True, sync_widget=True, rerun=False, persist=True):
    """Single owner for app navigation state.

    Returns True when ticker/view changed. Query params are updated only when
    needed, and persistence happens only on a real state transition.
    """
    changed = False
    ticker_value = _clean_ticker(ticker) if ticker is not None else None
    view_value = _valid_view(view, st.session_state.get("view", "analyze")) if view is not None else None

    if ticker_value and ticker_value != st.session_state.get("current_ticker"):
        st.session_state.current_ticker = ticker_value
        st.session_state.store["last_ticker"] = ticker_value
        changed = True
        if sync_widget and "ticker_input" in st.session_state:
            st.session_state["ticker_input"] = ticker_value
            st.session_state["_last_synced_ticker"] = ticker_value

    if view_value and view_value != st.session_state.get("view"):
        st.session_state.view = view_value
        st.session_state.store["last_view"] = view_value
        changed = True
    elif view_value and st.session_state.store.get("last_view") != view_value:
        st.session_state.store["last_view"] = view_value
        if persist:
            save_store(st.session_state.store)

    if sync_url:
        if ticker_value:
            _query_set("ticker", ticker_value)
        if view_value:
            _query_set("view", view_value)

    if changed:
        st.session_state["_last_route_event"] = {
            "reason": reason,
            "ticker": st.session_state.get("current_ticker"),
            "view": st.session_state.get("view"),
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        if persist:
            save_store(st.session_state.store)
        if rerun:
            st.rerun()
    return changed


# ─────────────────────────────────────────────────────────────────────
# Global query-param handlers — run before any view renders
# ─────────────────────────────────────────────────────────────────────
# These handle anchor-link clicks from anywhere in the app, sidestepping
# Streamlit's column layout system which has caused persistent alignment
# bugs. Each <a href="?param=value"> click triggers a rerun, the param
# is consumed here, and the page state updates accordingly.
try:
    qp_global = st.query_params
    if "report" in qp_global:
        render_research_report(qp_global.get("report"))
        st.stop()
    if "ticker" in qp_global:
        tkr_from_url = str(qp_global.get("ticker") or "").upper().strip()
        if tkr_from_url and tkr_from_url != st.session_state.current_ticker:
            route_to(ticker=tkr_from_url, view="analyze", reason="url ticker", rerun=True)
    if "open" in qp_global:
        tkr_to_open = str(qp_global.get("open") or "").upper().strip()
        del qp_global["open"]
        if tkr_to_open:
            route_to(ticker=tkr_to_open, view="analyze", reason="open ticker", rerun=True)
    if "view" in qp_global:
        view_to_open = str(qp_global.get("view") or "").strip().lower()
        if view_to_open in ARCHIVED_VIEWS and not SHOW_ARCHIVED_TRACKER:
            del qp_global["view"]
            route_to(view="analyze", reason="archived view", rerun=True)
        if view_to_open in ACTIVE_VIEWS or (
            SHOW_ARCHIVED_TRACKER and view_to_open in ARCHIVED_VIEWS
        ):
            route_to(view=view_to_open, reason="url view", rerun=True)
    if "wldel" in qp_global:
        tkr_to_del = str(qp_global.get("wldel") or "").upper().strip()
        del qp_global["wldel"]
        if tkr_to_del:
            watchlist_store = st.session_state.store.setdefault("watchlist", [])
            remaining_watchlist = [
                str(t).upper().strip()
                for t in watchlist_store
                if str(t).upper().strip() and str(t).upper().strip() != tkr_to_del
            ]
            st.session_state.store["watchlist"] = remaining_watchlist
            st.session_state.pop("_pending_wldel", None)
            if tkr_to_del == str(st.session_state.get("current_ticker") or "").upper().strip():
                next_ticker = remaining_watchlist[0] if remaining_watchlist else ""
                if next_ticker:
                    st.session_state.current_ticker = next_ticker
                    st.session_state.store["last_ticker"] = next_ticker
                    try:
                        st.query_params["ticker"] = next_ticker
                    except Exception:
                        pass
                else:
                    st.session_state.store.pop("last_ticker", None)
            save_store(st.session_state.store)
            st.rerun()
    if "idea_watch" in qp_global:
        tkr_to_watch = str(qp_global.get("idea_watch") or "").upper().strip()
        del qp_global["idea_watch"]
        if tkr_to_watch:
            watchlist_store = st.session_state.store.setdefault("watchlist", [])
            if tkr_to_watch not in watchlist_store:
                watchlist_store.append(tkr_to_watch)
                update_sidebar_watchlist_cache((tkr_to_watch,))
                save_store(st.session_state.store)
            route_to(ticker=tkr_to_watch, view="analyze", reason="idea watch", rerun=True)
    if "data_refresh" in qp_global:
        tkr_to_refresh = qp_global.get("data_refresh")
        del qp_global["data_refresh"]
        if tkr_to_refresh:
            refresh_ticker = str(tkr_to_refresh).upper().strip()
            route_to(ticker=refresh_ticker, view="analyze", reason="data refresh", rerun=False)
            st.session_state["_force_data_refresh_ticker"] = refresh_ticker
            refresh_current_ticker_state(refresh_ticker, refresh_research=False)
            st.rerun()
    if "pm_refresh" in qp_global or "research_refresh" in qp_global:
        refresh_param = "pm_refresh" if "pm_refresh" in qp_global else "research_refresh"
        tkr_to_refresh = qp_global.get(refresh_param)
        del qp_global[refresh_param]
        if tkr_to_refresh:
            refresh_ticker = str(tkr_to_refresh).upper().strip()
            route_to(ticker=refresh_ticker, view="analyze", reason="research refresh", rerun=False)
            refresh_current_ticker_state(refresh_ticker, refresh_research=True)
            st.rerun()
except Exception:
    pass


# ─────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(
        '<div class="desk-sidebar-wordmark">'
        '<span class="desk-sidebar-mark">▸</span><span>Trading Desk</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    view_labels = {
        "regime": "Market Regime",
        "analyze": "Analyze",
        "watchlist": "Watchlist",
        "holdings": "Holdings",
        "ideas": "Ideas",
    }
    if SHOW_ARCHIVED_TRACKER:
        view_labels["tracker"] = "Tracker"
    if st.session_state.view not in view_labels:
        st.session_state.view = "regime"
    st.markdown(
        """
        <style>
        .desk-sidebar-nav {
            display: flex !important;
            flex-direction: column !important;
            gap: 4px !important;
            margin-bottom: 22px !important;
        }
        .desk-sidebar-nav a {
            display: flex !important;
            align-items: center !important;
            min-height: 36px !important;
            padding: 9px 13px !important;
            border-radius: 5px !important;
            color: var(--color-text) !important;
            font-size: var(--fs-base) !important;
            font-weight: 650 !important;
            line-height: 1.15 !important;
            text-decoration: none !important;
        }
        .desk-sidebar-nav a:hover {
            background: #EEF2F7 !important;
        }
        .desk-sidebar-nav a.active {
            background: var(--color-text) !important;
            color: var(--color-bg) !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    nav_html = '<nav class="desk-sidebar-nav">'
    for view_key, view_label in view_labels.items():
        active_class = " active" if view_key == st.session_state.view else ""
        nav_html += (
            f'<a class="{active_class.strip()}" '
            f'href="?view={urllib.parse.quote(view_key)}">'
            f'{html.escape(view_label)}</a>'
        )
    nav_html += "</nav>"
    st.markdown(nav_html, unsafe_allow_html=True)

    st.markdown("---")
    st.markdown(
        '<div style="font-family: var(--font-mono);font-size:var(--fs-xs);'
        'font-weight:600;letter-spacing: var(--ls-caps-xl);text-transform:uppercase;'
        'color:var(--color-muted);margin:6px 0 8px;">Ticker</div>',
        unsafe_allow_html=True,
    )
    # Ticker text input — TRICKY: Streamlit's text_input ignores `value=`
    # parameter on reruns after the first render. The widget owns its
    # state via the `key` parameter.
    #
    # Two separate update paths must both work:
    #  1. User types → widget changes → update current_ticker
    #  2. Watchlist click → current_ticker changes → update widget
    #
    # We use a sentinel ("_last_synced_ticker") to detect path 2 — when
    # current_ticker has drifted from what we last synced, we know the
    # change came from outside the widget (watchlist) and we update
    # widget state. Otherwise we treat any difference as user typing
    # (path 1) and update current_ticker.
    if "ticker_input" not in st.session_state:
        st.session_state["ticker_input"] = st.session_state.current_ticker
        st.session_state["_last_synced_ticker"] = st.session_state.current_ticker

    if st.session_state.get("_last_synced_ticker") != st.session_state.current_ticker:
        # current_ticker changed externally (watchlist click, etc.).
        # Update the widget to match.
        st.session_state["ticker_input"] = st.session_state.current_ticker
        st.session_state["_last_synced_ticker"] = st.session_state.current_ticker

    input_ticker = st.text_input(
        "Any US ticker",
        key="ticker_input",
        label_visibility="collapsed",
        placeholder="NVDA, META, PLTR...",
    ).strip().upper()
    # If user typed something different from current_ticker, update.
    # The sync above guarantees current_ticker matches what was rendered,
    # so any difference here is genuine user input.
    if input_ticker and input_ticker != st.session_state.current_ticker:
        route_to(
            ticker=input_ticker,
            view="analyze",
            reason="sidebar ticker",
            sync_widget=False,
            rerun=True,
        )

    if st.session_state.get("view") == "analyze" and st.session_state.current_ticker:
        active_ticker = str(st.session_state.current_ticker).upper().strip()
        _query_set("ticker", active_ticker)
        _query_set("view", "analyze")

    st.markdown("---")
    st.markdown(
        '<div style="font-family: var(--font-mono);font-size:var(--fs-xs);'
        'font-weight:600;letter-spacing: var(--ls-caps-xl);text-transform:uppercase;'
        'color:var(--color-muted);margin:6px 0 8px;display:flex;align-items:center;gap:6px;">'
        '<span>Watchlist</span>'
        '<span class="desk-sidebar-help">i'
        '<span class="desk-sidebar-help-tip">Sidebar symbols match the main dashboard call:<br>'
        '🚀 Enter · 👀 Watch · 🤔 Hold off<br>🌱 Accumulate · ⛔ Avoid<br>'
        '<span class="muted">Same source as the Watchlist Action column.</span></span>'
        '</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    watchlist = st.session_state.store["watchlist"]
    if watchlist:
        st.markdown(
            """<style>
section[data-testid='stSidebar'] [role='radiogroup'] {
    gap: 0px !important;
}
section[data-testid='stSidebar'] [role='radiogroup'] label {
    padding: 8px 12px !important;
    margin: 1px 0 !important;
    font-family: var(--font-sans) !important;
    font-size: var(--fs-base) !important;
    border-radius: 4px !important;
    display: flex !important;
    align-items: center !important;
    line-height: 1.2 !important;
    cursor: pointer;
}
section[data-testid='stSidebar'] [role='radiogroup'] label p {
    font-size: var(--fs-base) !important;
    font-weight: 500 !important;
    margin: 0 !important;
    line-height: 1.2 !important;
}
section[data-testid='stSidebar'] [role='radiogroup'] label:hover {
    background: #F1F5F9 !important;
}
/* Hide the radio dot AND the empty wrapper so text owns the full width */
section[data-testid='stSidebar'] [role='radiogroup'] label > div:first-child {
    display: none !important;
}
section[data-testid='stSidebar'] [role='radiogroup'] label > div:has(> div[data-testid="stMarkdownContainer"]) ~ div {
    display: none !important;
}
/* When the only remaining child is the markdown wrapper, give it
   explicit margin-zero so vertical alignment is clean. */
section[data-testid='stSidebar'] [role='radiogroup'] label > div {
    margin: 0 !important;
    padding: 0 !important;
    display: flex !important;
    align-items: center !important;
}
/* Highlight active selection with a solid background */
section[data-testid='stSidebar'] [role='radiogroup'] label:has(input:checked) {
    background: var(--color-text) !important;
    color: var(--color-bg) !important;
}
section[data-testid='stSidebar'] [role='radiogroup'] label:has(input:checked) p {
    color: var(--color-bg) !important;
}

/* Watchlist row — Streamlit button styled to look like a list item */
section[data-testid='stSidebar'] div[data-testid="stButton"] > button[kind="secondary"][data-testid*="wl_select"],
section[data-testid='stSidebar'] button[data-testid^="stBaseButton-secondary"]:not([kind="primary"]) {
    /* These rules use Streamlit's data-testid which is fragile but the
       cleanest available hook for "the watchlist row buttons" specifically. */
}
/* Generic styling for sidebar wl_select buttons — they all share key prefix */
section[data-testid='stSidebar'] div[data-testid="stButton"] button:has(p:only-child) {
    /* Transparent default, left-aligned, tight padding */
}
/* Target the ✕ delete buttons by their st-key-* class wrapper.
   Streamlit 1.36+ adds a CSS class `st-key-{key}` to the element-container
   wrapping each widget when a key is set. This is documented stable DOM,
   unlike data-testid which can shift between versions.

   We use [class*="st-key-wl_del_"] to match any wl_del_NVDA, wl_del_META etc.
   This is the reliable way to style specific Streamlit buttons. */

/* Tighten sidebar horizontal blocks (watchlist rows) so they align
   left with the WATCHLIST header rather than being indented inward. */
section[data-testid='stSidebar'] [data-testid="stHorizontalBlock"] {
    gap: 4px !important;
    padding: 0 !important;
    margin: 0 !important;
}
section[data-testid='stSidebar'] [data-testid="stHorizontalBlock"] [data-testid="stColumn"] {
    padding: 0 !important;
}

/* Sidebar ticker SELECT button — wl_select_{TKR} for inactive,
   wl_select_active_{TKR} for the currently-active ticker.
   The black highlight is on the BUTTON only, not on the row. */
section[data-testid='stSidebar'] [class*="st-key-wl_select_"] button {
    background: transparent !important;
    border: 1px solid transparent !important;
    box-shadow: none !important;
    padding: 5px 10px !important;
    font-family: var(--font-sans) !important;
    font-size: var(--fs-base) !important;
    font-weight: 600 !important;
    color: var(--color-text) !important;
    text-align: left !important;
    justify-content: flex-start !important;
    min-height: 28px !important;
    height: 28px !important;
    line-height: 1.1 !important;
    border-radius: 3px !important;
}
section[data-testid='stSidebar'] [class*="st-key-wl_select_"] button p {
    font-size: var(--fs-base) !important;
    margin: 0 !important;
    line-height: 1.1 !important;
}
section[data-testid='stSidebar'] [class*="st-key-wl_select_"] button:hover {
    background: var(--color-surface-soft) !important;
    color: var(--color-text) !important;
}
/* ACTIVE ticker — black background, white text, ONLY on the button.
   Distinct key prefix wl_select_active_ so this rule beats the generic
   wl_select_ rule above. */
section[data-testid='stSidebar'] [class*="st-key-wl_select_active_"] button {
    background: var(--color-text) !important;
    color: var(--color-bg) !important;
}
section[data-testid='stSidebar'] [class*="st-key-wl_select_active_"] button p {
    color: var(--color-bg) !important;
}
section[data-testid='stSidebar'] [class*="st-key-wl_select_active_"] button:hover {
    background: var(--color-text) !important;
    color: var(--color-bg) !important;
}

/* Sidebar ticker DELETE ✕ — small, transparent, red on hover. */
section[data-testid='stSidebar'] [class*="st-key-wl_del_"] button {
    padding: 0 !important;
    min-height: 28px !important;
    height: 28px !important;
    width: 28px !important;
    font-size: var(--fs-base) !important;
    color: var(--color-fainter) !important;
    background: transparent !important;
    border: 1px solid transparent !important;
    box-shadow: none !important;
    line-height: 1 !important;
}
section[data-testid='stSidebar'] [class*="st-key-wl_del_"] button p {
    font-size: var(--fs-base) !important;
    line-height: 1 !important;
    margin: 0 !important;
}
section[data-testid='stSidebar'] [class*="st-key-wl_del_"] button:hover {
    color: var(--color-negative) !important;
    background: transparent !important;
    border-color: var(--color-border) !important;
}

/* Add-to-watchlist button — full width, flush with watchlist rows */
section[data-testid='stSidebar'] [class*="st-key-add_to_watchlist_btn"],
section[data-testid='stSidebar'] [class*="st-key-add_to_watchlist_btn"] > div {
    width: 100% !important;
    padding: 0 !important;
    margin: 0 !important;
}
section[data-testid='stSidebar'] [class*="st-key-add_to_watchlist_btn"] button {
    width: 100% !important;
    margin: 0 !important;
    border-radius: 4px !important;
    font-size: var(--fs-sm) !important;
    text-align: center !important;
    justify-content: center !important;
}

/* Watchlist Pro ticker buttons — same style pattern but in main content */
.main [class*="st-key-wlpro_open_"] button {
    background: transparent !important;
    border: 1px solid transparent !important;
    box-shadow: none !important;
    padding: 4px 8px !important;
    font-family: var(--font-mono) !important;
    font-size: var(--fs-base) !important;
    font-weight: 600 !important;
    color: var(--color-text) !important;
    text-align: left !important;
    justify-content: flex-start !important;
    min-height: 28px !important;
    line-height: 1.3 !important;
}
.main [class*="st-key-wlpro_open_"] button:hover {
    background: var(--color-surface-soft) !important;
    border-color: var(--color-border) !important;
    color: var(--color-text) !important;
}
.main [class*="st-key-wlpro_open_"] button p {
    font-size: var(--fs-base) !important;
    margin: 0 !important;
}

/* Final sidebar skin: brighter, younger, more app-like */
section[data-testid='stSidebar'] {
    background: linear-gradient(180deg, #F8FAFC 0%, #ECFDF5 48%, #EFF6FF 100%) !important;
    border-right: 1px solid rgba(15, 23, 42, 0.08) !important;
}
section[data-testid='stSidebar'] hr {
    border-color: rgba(15, 23, 42, 0.09) !important;
}
section[data-testid='stSidebar'] input {
    border-radius: 14px !important;
    background: #FFFFFF !important;
    border: 1px solid rgba(37, 99, 235, 0.14) !important;
    box-shadow: 0 10px 24px rgba(37, 99, 235, 0.06) !important;
}
section[data-testid='stSidebar'] div.stButton > button,
section[data-testid='stSidebar'] [class*="st-key-wl_select_"] button {
    border-radius: 12px !important;
    transition: transform 0.12s ease, box-shadow 0.12s ease, background 0.12s ease !important;
}
section[data-testid='stSidebar'] div.stButton > button:hover,
section[data-testid='stSidebar'] [class*="st-key-wl_select_"] button:hover {
    transform: translateX(2px);
    background: rgba(37, 99, 235, 0.08) !important;
    border-color: rgba(37, 99, 235, 0.14) !important;
}
section[data-testid='stSidebar'] [class*="st-key-wl_select_active_"] button {
    background: linear-gradient(135deg, #111827 0%, #2563EB 72%, #06B6D4 100%) !important;
    color: #FFFFFF !important;
    box-shadow: 0 12px 26px rgba(37, 99, 235, 0.18) !important;
}
section[data-testid='stSidebar'] [class*="st-key-wl_select_active_"] button:hover {
    background: linear-gradient(135deg, #111827 0%, #2563EB 72%, #06B6D4 100%) !important;
}
section[data-testid='stSidebar'] [class*="st-key-add_to_watchlist_btn"] button {
    border-radius: 999px !important;
    background: #FFFFFF !important;
    border: 1px solid rgba(37, 99, 235, 0.16) !important;
    box-shadow: 0 10px 22px rgba(37, 99, 235, 0.08) !important;
}

/* Refined skin: young but premium, less noisy than the first pass */
.stApp {
    background: #F7F8FA !important;
}
.main .block-container {
    background: transparent !important;
}
section[data-testid='stSidebar'] {
    background: #F4F6F8 !important;
    border-right: 1px solid #E5E7EB !important;
}
section[data-testid='stSidebar'] input {
    border-radius: 12px !important;
    border: 1px solid #E0E4EA !important;
    box-shadow: none !important;
}
section[data-testid='stSidebar'] [class*="st-key-wl_select_active_"] button {
    background: #101114 !important;
    color: #FFFFFF !important;
    box-shadow: 0 8px 18px rgba(16, 17, 20, 0.14) !important;
}
section[data-testid='stSidebar'] [class*="st-key-wl_select_active_"] button:hover {
    background: #101114 !important;
}
section[data-testid='stSidebar'] div.stButton > button:hover,
section[data-testid='stSidebar'] [class*="st-key-wl_select_"] button:hover {
    transform: none !important;
    background: #FFFFFF !important;
    border-color: #E0E4EA !important;
}
.desk-bar {
    background: rgba(255, 255, 255, 0.88) !important;
    color: #334155 !important;
    border: 0 !important;
    border-radius: 0 !important;
    border-top: none !important;
    border-bottom: 1px solid #DCE3EA !important;
    box-shadow: none !important;
}
.desk-bar .wordmark,
.desk-bar .meta {
    color: #334155 !important;
}
.desk-bar .wordmark .arrow {
    color: var(--color-accent) !important;
    text-shadow: none !important;
}
.desk-ticker-row,
.desk-pm-header {
    border-bottom: 1px solid #DDE2E8 !important;
}
.desk-decision {
    padding: 10px 0 22px !important;
}
.desk-decision .word {
    font-size: 78px !important;
    font-weight: 780 !important;
    letter-spacing: -0.035em !important;
}
.desk-decision .emoji {
    filter: none !important;
    font-size: 38px !important;
}
.desk-decision .context {
    font-size: 18px !important;
    color: #1F2937 !important;
}
.desk-avoid-reasons,
.desk-reconsider,
.desk-dossier,
.desk-cmp,
.desk-stat-card,
.research-kpi,
.desk-pm-thesis {
    border-radius: 10px !important;
    box-shadow: none !important;
}
.desk-avoid-reasons {
    background: #FFF4F6 !important;
}
.desk-reconsider {
    background: #F0FFF6 !important;
}
.desk-dossier {
    background: #FFFFFF !important;
    border-left-color: var(--color-text) !important;
}
.desk-dossier-text {
    font-family: var(--font-serif) !important;
    font-size: var(--fs-lg) !important;
    line-height: 1.58 !important;
}
.desk-pm-thesis {
    background: #FFFFFF !important;
    border-left-color: var(--color-accent) !important;
}
.research-link {
    background: #FFFFFF !important;
    border-color: #DDE2E8 !important;
    box-shadow: none !important;
}
.research-link:hover {
    background: #F2F8FF !important;
    box-shadow: none !important;
}
[class*="st-key-chat_send_"] button {
    background: #101114 !important;
    box-shadow: none !important;
}
.research-page .hero {
    background: #FFFFFF !important;
    border: 1px solid #E2E8F0 !important;
    border-radius: 14px !important;
    box-shadow: none !important;
}
.research-page h1 {
    font-weight: 760 !important;
}

/* Quiet product skin: current without trying to be cool */
.stApp {
    background: #F8FAFC !important;
}
section[data-testid='stSidebar'] {
    background: #F1F5F9 !important;
    border-right: 1px solid #DCE3EA !important;
}
section[data-testid='stSidebar'] input {
    border-radius: 8px !important;
    border: 1px solid #DCE3EA !important;
    background: #FFFFFF !important;
}
section[data-testid='stSidebar'] div.stButton > button,
section[data-testid='stSidebar'] [class*="st-key-wl_select_"] button {
    border-radius: 6px !important;
    transition: none !important;
}
section[data-testid='stSidebar'] [class*="st-key-wl_select_active_"] button {
    background: #111111 !important;
    box-shadow: none !important;
}
.desk-bar {
    background: rgba(255, 255, 255, 0.88) !important;
    color: #334155 !important;
    box-shadow: none !important;
    border-bottom: 1px solid #DCE3EA !important;
    padding: 7px 0 8px !important;
}
.desk-bar .wordmark {
    color: #334155 !important;
    letter-spacing: 0.12em !important;
}
.desk-bar .meta {
    color: #64748B !important;
}
.desk-ticker-row .sym {
    font-size: 24px !important;
    font-weight: 700 !important;
}
.desk-ticker-row .price {
    font-size: 18px !important;
    font-weight: 650 !important;
}
.desk-decision {
    padding: 8px 0 20px !important;
}
.desk-decision .word {
    font-size: 72px !important;
    font-weight: 720 !important;
    letter-spacing: -0.03em !important;
}
.desk-decision .emoji {
    font-size: 34px !important;
    margin-left: 8px !important;
}
.desk-decision .context {
    font-size: 17px !important;
}
.desk-avoid-reasons,
.desk-reconsider,
.desk-dossier,
.desk-cmp,
.desk-stat-card,
.research-kpi,
.desk-pm-thesis,
.research-page .hero {
    border-radius: 6px !important;
    box-shadow: none !important;
}
.desk-dossier {
    border-left: 3px solid #111111 !important;
}
.desk-dossier-text {
    font-family: var(--font-serif) !important;
    font-size: 17px !important;
    line-height: 1.55 !important;
}
.desk-pm-thesis {
    border-left: 3px solid var(--color-accent) !important;
}
.research-link {
    border-radius: 8px !important;
    padding: 7px 11px !important;
}

/* Research report: memo layout, not a landing-page tile wall */
.research-page .hero {
    background: transparent !important;
    border: 0 !important;
    border-bottom: 1px solid #DDE2E8 !important;
    border-radius: 0 !important;
    box-shadow: none !important;
    padding: 0 0 22px !important;
}
.research-page h1 {
    font-family: var(--font-sans) !important;
    font-size: 46px !important;
    font-weight: 820 !important;
    letter-spacing: -0.02em !important;
}
.research-page .deck {
    font-size: 19px !important;
    line-height: 1.42 !important;
    max-width: 920px !important;
}
.research-grid {
    grid-template-columns: repeat(5, minmax(0, 1fr)) !important;
    gap: 0 !important;
    border: 1px solid #DDE2E8 !important;
    border-radius: 6px !important;
    background: #FFFFFF !important;
    overflow: hidden !important;
    box-shadow: none !important;
}
.research-metric-group {
    border-right: 1px solid #E8EDF3 !important;
}
.research-metric-group:last-child {
    border-right: 0 !important;
}
.research-section h2 {
    font-family: var(--font-sans) !important;
    font-size: 22px !important;
    font-weight: 760 !important;
}
.research-kpi {
    border-top: 0 !important;
}
[class*="st-key-chat_input_"] textarea {
    border-radius: 8px !important;
}
[class*="st-key-chat_send_"] button {
    border-radius: 8px !important;
    background: #111111 !important;
}

/* Kill remaining warm/cream Streamlit input surfaces */
.main input,
.main textarea,
.main [data-baseweb="input"],
.main [data-baseweb="textarea"],
.main [data-baseweb="base-input"],
.main [data-testid="stTextInput"] input,
.main [data-testid="stTextArea"] textarea {
    background: #FFFFFF !important;
    background-color: #FFFFFF !important;
    border-color: #DCE3EA !important;
}
.main input::placeholder,
.main textarea::placeholder {
    color: #6B7280 !important;
}

/* Decision comparison controls: no cream, no pills */
div[data-testid="stVerticalBlockBorderWrapper"]:has(.desk-cmp-header),
div[data-testid="stElementContainer"]:has(.desk-cmp-header),
div[data-testid="element-container"]:has(.desk-cmp-header) {
    display: none !important;
}

.desk-cmp-header,
.desk-cmp-read,
.desk-cmp-resolution,
.desk-cmp-grid,
.desk-cmp-reasoning,
.desk-cmp-trigger,
.desk-cmp-yourcall-label,
.main [class*="st-key-decision_compare_user_pick_"],
.main [class*="st-key-decision_compare_user_note_"],
.main [class*="st-key-log_compare_"],
.main [class*="st-key-mark_entered_"] {
    display: none !important;
}

.desk-cmp-badge {
    border-radius: 5px !important;
}
.main [class*="st-key-decision_compare_user_note_"],
.main [class*="st-key-decision_compare_user_note_"] *,
.main [class*="st-key-decision_compare_user_note_"] [data-baseweb="input"],
.main [class*="st-key-decision_compare_user_note_"] [data-baseweb="base-input"] {
    background: #FFFFFF !important;
    background-color: #FFFFFF !important;
}
.main [class*="st-key-decision_compare_user_note_"] input {
    background: #FFFFFF !important;
    background-color: #FFFFFF !important;
    border: 1px solid #DCE3EA !important;
    border-radius: 6px !important;
}
.main [class*="st-key-decision_compare_user_pick_"] [role="radiogroup"] {
    gap: 8px !important;
}
.main [class*="st-key-decision_compare_user_pick_"] [role="radiogroup"] label {
    border: 1px solid #DCE3EA !important;
    border-radius: 6px !important;
    background: #FFFFFF !important;
    padding: 7px 10px !important;
    margin: 0 !important;
    min-height: 34px !important;
}
.main [class*="st-key-decision_compare_user_pick_"] [role="radiogroup"] label:hover {
    border-color: #94A3B8 !important;
    background: #F8FAFC !important;
}
.main [class*="st-key-decision_compare_user_pick_"] [role="radiogroup"] label:has(input:checked) {
    border-color: #111111 !important;
    background: #111111 !important;
    color: #FFFFFF !important;
}
.main [class*="st-key-decision_compare_user_pick_"] [role="radiogroup"] label:has(input:checked) p {
    color: #FFFFFF !important;
}
.main [class*="st-key-decision_compare_user_pick_"] [role="radiogroup"] label > div:first-child {
    display: none !important;
}

/* Final top rail override: no heavy black strip */
.desk-bar {
    background: rgba(255, 255, 255, 0.88) !important;
    color: #334155 !important;
    border: 0 !important;
    border-bottom: 1px solid #DCE3EA !important;
    box-shadow: none !important;
    backdrop-filter: blur(8px) !important;
    padding: 7px 0 8px !important;
}
.desk-bar .wordmark {
    color: #334155 !important;
    letter-spacing: 0.12em !important;
}
.desk-bar .wordmark .arrow {
    color: var(--color-accent) !important;
}
.desk-bar .meta {
    color: #64748B !important;
}
</style>""",
            unsafe_allow_html=True,
        )

        current = st.session_state.current_ticker

        # Keep Analyze fast: refresh the active ticker row only, and reserve
        # full-watchlist price/action scans for the Watchlist page.
        saved_sidebar_cache = st.session_state.store.setdefault("watchlist_sidebar_cache", {})
        current_key = str(current or "").upper().strip()
        if current_key in {str(t or "").upper().strip() for t in watchlist}:
            current_market = ticker_snapshot(current_key).get("market") or {}
            if sidebar_row_needs_refresh(current_market, max_age_minutes=5):
                try:
                    hist_current, _name_current, _err_current = fetch_history(current_key)
                    bench_current = fetch_bench()
                    if hist_current is not None and bench_current is not None:
                        t_current = tactical.compute(hist_current, bench_current)
                        if t_current is not None:
                            remember_sidebar_ticker_snapshot(current_key, t_current, hist_current)
                except Exception:
                    pass
        wl_data = {
            str(tkr).upper(): (ticker_snapshot(tkr).get("market") or {})
            for tkr in watchlist
        }
        try:
            # Never scan the full watchlist during ordinary page load. A
            # 20-name watchlist means 20 network/history fetches on every
            # rerun, which is exactly why mobile feels frozen. The Watchlist
            # page has explicit refresh buttons for the heavier scan; sidebar
            # rows read the latest saved snapshots only.
            wl_data = {
                str(tkr).upper(): (ticker_snapshot(tkr).get("market") or {})
                for tkr in watchlist
            }
        except Exception:
            wl_data = saved_sidebar_cache

        # Each watchlist row rendered as ONE HTML markdown block.
        # No st.columns — flex layout in pure HTML, fully aligned, no
        # Streamlit padding interference. Ticker click → ?open=TICKER,
        # ✕ click → ?wldel=TICKER, both handled by the global handler.
        rows_html = []
        for tkr in watchlist:
            row_snapshot = wl_data.get(tkr.upper(), {})
            last = row_snapshot.get("last")
            chg_pct = row_snapshot.get("change_pct")
            price_age_kind = row_snapshot.get("price_age_kind") or "stale"
            price_age_label = row_snapshot.get("price_age") or ""
            try:
                updated_at = row_snapshot.get("updated_at")
                if updated_at:
                    sidebar_age = datetime.now() - datetime.fromisoformat(updated_at)
                    if sidebar_age > timedelta(minutes=5):
                        price_age_kind = "stale"
                        minutes_old = int(sidebar_age.total_seconds() // 60)
                        price_age_label = f"sidebar cached {minutes_old}m ago"
                else:
                    price_age_kind = "stale"
                    price_age_label = "sidebar cache not refreshed yet"
            except Exception:
                price_age_kind = "stale"
                price_age_label = "sidebar cache age unknown"
            action = sidebar_action_hint(tkr, row_snapshot)
            is_active = (tkr == current)
            if is_active:
                st.session_state["_active_sidebar_action_rendered"] = action
            if action == "position":
                marker_emoji = "🟢"
                marker_title = "Owned position"
            else:
                sty = STATE_STYLES.get(action or "")
                marker_emoji = (sty or {}).get("emoji", "")
                marker_title = (sty or {}).get("label", "Signal not loaded yet")
            action_marker = (
                f'<span title="{html.escape(marker_title)}" style="margin-left:5px;'
                f'font-size:11px;vertical-align:1px;">{html.escape(marker_emoji)}</span>'
                if marker_emoji else ""
            )
            chg_color = (
                "var(--color-positive)" if (chg_pct or 0) >= 0
                else "var(--color-negative)"
            )
            chg_str = f"{chg_pct:+.2f}%" if chg_pct is not None else "—"
            px_str = f"${last:,.2f}" if last is not None else "—"
            # Freshness is shown in the dedicated data-freshness panel, not
            # repeated on every compact sidebar row.
            stale_note = ""

            # Active ticker: black bg + white text on the ticker label only
            active_bg = "var(--color-text)" if is_active else "transparent"
            active_fg = "var(--color-bg)" if is_active else "var(--color-text)"
            active_hover = "" if is_active else (
                "onmouseover=\"this.style.background='var(--color-surface-soft)'\" "
                "onmouseout=\"this.style.background='transparent'\""
            )

            rows_html.append(
                f'<div style="display: flex; align-items: center;'
                f'gap: 4px; padding: 2px 0; width: 100%;">'
                # Ticker label — clickable, takes ~40% of row width
                f'<a href="?open={tkr}" target="_self" '
                f'style="flex: 0 0 38%; min-width: 0;'
                f'font-family: var(--font-sans); font-size: var(--fs-base);'
                f'font-weight: 600; color: {active_fg};'
                f'background: {active_bg}; padding: 6px 8px;'
                f'border-radius: 3px; text-decoration: none;'
                f'text-align: left; cursor: pointer;'
                f'overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" '
                f'{active_hover}>'
                f'{tkr}{action_marker}</a>'
                # Price + change — right aligned in middle area
                f'<div style="flex: 1 1 auto; min-width: 0;'
                f'display: flex; flex-direction: column; align-items: flex-end;'
                f'font-family: var(--font-mono); font-variant-numeric: tabular-nums;'
                f'line-height: 1.15; padding: 0 4px;">'
                f'<span style="font-size: var(--fs-base); color: var(--color-text); font-weight: 500;">{px_str}</span>'
                f'<span style="font-size: var(--fs-sm); color: {chg_color};">{chg_str}</span>'
                f'{stale_note}'
                f'</div>'
                # ✕ delete — clickable
                f'<a href="?wldel={tkr}" target="_self" title="Remove {tkr}" '
                f'style="flex: 0 0 22px; height: 22px;'
                f'display: flex; align-items: center; justify-content: center;'
                f'font-family: var(--font-sans); font-size: var(--fs-base);'
                f'color: var(--color-fainter); text-decoration: none;'
                f'border: 1px solid transparent; border-radius: 3px;'
                f'cursor: pointer;" '
                f'onmouseover="this.style.color=\'var(--color-negative)\';'
                f'this.style.borderColor=\'var(--color-border)\'" '
                f'onmouseout="this.style.color=\'var(--color-fainter)\';'
                f'this.style.borderColor=\'transparent\'">✕</a>'
                f'</div>'
            )
        st.markdown("".join(rows_html), unsafe_allow_html=True)
    else:
        st.caption("Empty — type a ticker above and add it.")

    pending_del = st.session_state.get("_pending_wldel")
    if pending_del:
        if pending_del in st.session_state.store.get("watchlist", []):
            st.warning(f"Remove {pending_del} from watchlist?")
            c_del, c_cancel = st.columns(2)
            if c_del.button("Remove", key=f"confirm_wldel_{pending_del}", use_container_width=True):
                st.session_state.store["watchlist"].remove(pending_del)
                save_store(st.session_state.store)
                if pending_del == st.session_state.current_ticker and st.session_state.store["watchlist"]:
                    next_ticker = st.session_state.store["watchlist"][0]
                    st.session_state.current_ticker = next_ticker
                    st.session_state.store["last_ticker"] = next_ticker
                    try:
                        st.query_params["ticker"] = next_ticker
                    except Exception:
                        pass
                    save_store(st.session_state.store)
                st.session_state.pop("_pending_wldel", None)
                st.rerun()
            if c_cancel.button("Cancel", key=f"cancel_wldel_{pending_del}", use_container_width=True):
                st.session_state.pop("_pending_wldel", None)
                st.rerun()
        else:
            st.session_state.pop("_pending_wldel", None)

    if st.session_state.current_ticker and st.session_state.current_ticker not in watchlist:
        st.markdown('<div style="margin-top:8px;">', unsafe_allow_html=True)
        if st.button(f"+ Add {st.session_state.current_ticker} to watchlist",
                     use_container_width=True, key="add_to_watchlist_btn"):
            st.session_state.store["watchlist"].append(st.session_state.current_ticker)
            st.session_state.store["last_ticker"] = st.session_state.current_ticker
            save_store(st.session_state.store)
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

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

    # Resolution order for the Anthropic API key:
    #   1. Streamlit secrets (canonical for hosted deployments)
    #   2. ANTHROPIC_API_KEY environment variable (local dev with shell export)
    #   3. Session-only pasted key (never persisted)
    secret_key = ""
    try:
        secret_key = st.secrets.get("ANTHROPIC_API_KEY", "").strip()
    except Exception:
        pass
    env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if st.session_state.store.get("anthropic_api_key"):
        # Older builds persisted pasted API keys into the app store. Stop
        # carrying that forward, especially when DATABASE_URL points at
        # hosted Postgres.
        st.session_state.store.pop("anthropic_api_key", None)
        save_store(st.session_state.store)

    session_key = st.session_state.get("session_anthropic_api_key", "").strip()
    effective_key = secret_key or env_key or session_key

    if effective_key:
        if secret_key:
            source_note = "from cloud secret"
        elif env_key:
            source_note = "from env"
        else:
            source_note = "session only"
        st.markdown(
            f'<div style="font-size:var(--fs-base);color:var(--color-body);'
            f'padding:8px 10px;background:#F0FDF4;border:1px solid #BBF7D0;'
            f'border-radius:3px;font-family:Geist Mono,monospace;">'
            f'✓ API key configured <span style="color:var(--color-faint);">({source_note})</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
        if session_key and not env_key and not secret_key:
            if st.button("Clear session key", key="clear_api_key", use_container_width=True):
                st.session_state["session_anthropic_api_key"] = ""
                st.rerun()
        elif secret_key:
            st.caption("Set via Streamlit Cloud secret. Update in app settings.")
        else:
            st.caption("Set via `ANTHROPIC_API_KEY` env var. To change, edit your shell profile.")
        api_key = effective_key
    else:
        new_key = st.text_input(
            "Anthropic API key (optional)",
            type="password",
            help="Paste to generate live PM views for this browser session only. For persistence, use Streamlit secrets or ANTHROPIC_API_KEY.",
            key="api_key_input",
        )
        if new_key:
            st.session_state["session_anthropic_api_key"] = new_key.strip()
            if st.session_state.current_ticker:
                clear_pm_cache(st.session_state.current_ticker)
                clear_dossier_cache(st.session_state.current_ticker)
            st.rerun()
        api_key = session_key

    # ── Session usage tracker ──────────────────────────────────────
    # Counts fresh Claude calls this session (cache hits don't count).
    # ~$0.03 per fresh dossier+narratives call. Resets on browser refresh.
    _calls = st.session_state.get("claude_calls_this_session", 0)
    if _calls > 0:
        _est_cost = _calls * 0.03
        st.markdown(
            f'<div style="margin-top:24px;padding:8px 10px;background:var(--color-surface-soft);'
            f'border-radius:3px;font-family:Geist Mono,monospace;font-size:var(--fs-sm);'
            f'color:var(--color-muted);line-height:1.5;">'
            f'<div style="font-size:var(--fs-xs);letter-spacing: var(--ls-caps-md);text-transform:uppercase;'
            f'color:var(--color-faint);margin-bottom:3px;">Session usage</div>'
            f'<div>{_calls} fresh call{"s" if _calls != 1 else ""} \u00b7 ~${_est_cost:,.2f}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # ── Persistence health indicator ──────────────────────────────
    # Tells the user at a glance whether their data is persisting.
    # Health check runs once per session and is cached in session_state
    # so we don't hammer Postgres on every render.
    if USE_POSTGRES:
        if "_db_health" not in st.session_state:
            try:
                with _pg_connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        cur.fetchone()
                st.session_state["_db_health"] = "ok"
            except Exception as _e:
                st.session_state["_db_health"] = f"error: {str(_e)[:80]}"
        _health = st.session_state["_db_health"]
        if _health == "ok":
            st.markdown(
                '<div style="margin-top:8px;padding:6px 10px;background:#F0FDF4;'
                'border:1px solid #BBF7D0;border-radius:3px;'
                'font-family:Geist Mono,monospace;font-size:var(--fs-xs);color:#15803D;">'
                '✓ Database connected · data persisting'
                '</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<div style="margin-top:8px;padding:6px 10px;background:#FEF2F2;'
                f'border:1px solid #FECACA;border-radius:3px;'
                f'font-family:Geist Mono,monospace;font-size:var(--fs-xs);color:#B91C1C;">'
                f'✗ Database error · data NOT persisting<br>'
                f'<span style="font-size:var(--fs-xs);color:var(--color-warning);">{_health}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
    else:
        # No DATABASE_URL configured — running in session-only mode
        st.markdown(
            '<div style="margin-top:8px;padding:6px 10px;background:var(--color-surface-trigger);'
            'border:1px solid #CFE0FF;border-radius:3px;'
            'font-family:Geist Mono,monospace;font-size:var(--fs-xs);color:var(--color-warning-text);">'
            '⚠ Session-only · data resets on refresh'
            '</div>',
            unsafe_allow_html=True,
        )


# ─────────────────────────────────────────────────────────────────────
# Final visual skin — intentionally late so older experimental layers
# cannot leak beige surfaces, black rails, or pill-heavy controls back in.
# ─────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
:root {
    --desk-bg: #F6F8FB;
    --desk-panel: #FFFFFF;
    --desk-panel-soft: #F9FBFD;
    --desk-border: #D8E0E8;
    --desk-border-strong: #B8C4D0;
    --desk-text: #151A22;
    --desk-muted: #64748B;
    --desk-blue: #2563EB;
    --desk-green: #0F9F5A;
    --desk-red: #D43F3A;
    --desk-amber: #B97705;
}

html,
body,
.stApp,
[data-testid="stAppViewContainer"],
[data-testid="stHeader"],
.main,
.block-container {
    background: var(--desk-bg) !important;
    background-color: var(--desk-bg) !important;
    background-image: none !important;
    color: var(--desk-text) !important;
}

.main .block-container,
section.main .block-container,
div[data-testid="stMainBlockContainer"] {
    padding-top: 3.25rem !important;
}

[data-testid="stHeader"],
header[data-testid="stHeader"] {
    background: rgba(246,248,251,0.96) !important;
    box-shadow: none !important;
}

section[data-testid="stSidebar"] {
    background: #F3F5F7 !important;
    border-right: 1px solid var(--desk-border) !important;
}

.desk-sidebar-wordmark {
    display: flex;
    align-items: center;
    gap: 7px;
    margin: 2px 0 24px;
    padding: 0 0 18px;
    border-bottom: 1px solid var(--desk-border);
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 12px;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #263241;
}

.desk-sidebar-mark {
    color: #0F9F5A;
    font-size: 10px;
    line-height: 1;
}

.desk-sidebar-help {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 14px;
    height: 14px;
    border: 1px solid var(--desk-border);
    border-radius: 4px;
    color: var(--desk-muted);
    background: rgba(255,255,255,0.55);
    font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 0;
    text-transform: none;
    cursor: help;
}

.desk-sidebar-help-tip {
    position: absolute;
    left: calc(100% + 8px);
    top: 50%;
    z-index: 9999;
    width: 236px;
    max-width: min(260px, calc(100vw - 42px));
    padding: 9px 10px;
    border: 1px solid var(--desk-border);
    border-radius: 6px;
    background: #FFFFFF;
    color: var(--desk-text);
    box-shadow: 0 10px 24px rgba(15,23,42,0.12);
    font-size: 11px;
    font-weight: 600;
    line-height: 1.5;
    letter-spacing: 0;
    text-transform: none;
    white-space: normal;
    overflow-wrap: anywhere;
    text-align: left;
    opacity: 0;
    pointer-events: none;
    transform: translateY(-50%) translateX(-2px);
    transition: opacity 120ms ease, transform 120ms ease;
}

.desk-sidebar-help-tip .muted {
    color: var(--desk-muted);
    font-weight: 500;
}

.desk-sidebar-help:hover .desk-sidebar-help-tip {
    opacity: 1;
    transform: translateY(-50%) translateX(0);
}

section[data-testid="stSidebar"] input,
section[data-testid="stSidebar"] button,
section[data-testid="stSidebar"] [data-testid="stNumberInput"] input {
    border-radius: 6px !important;
}

.desk-bar {
    display: none !important;
}

div[data-testid="stElementContainer"]:has(.desk-bar),
div[data-testid="element-container"]:has(.desk-bar) {
    display: none !important;
    height: 0 !important;
    min-height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
}

.desk-bar .wordmark {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace !important;
    font-size: 12px !important;
    font-weight: 800 !important;
    letter-spacing: 0.10em !important;
    text-transform: uppercase !important;
    color: #111827 !important;
    opacity: 1 !important;
}

.desk-bar .arrow {
    color: var(--desk-green) !important;
}

.desk-bar .meta {
    display: none !important;
}

.desk-top,
.desk-hero,
.desk-ticker-row {
    background: transparent !important;
    box-shadow: none !important;
}

.desk-ticker-row {
    align-items: flex-start !important;
    min-height: 78px !important;
    height: 78px !important;
    padding: 0 0 14px 0 !important;
    margin: 0 0 18px 0 !important;
    border-bottom: 1px solid var(--desk-border) !important;
    overflow: visible !important;
    opacity: 1 !important;
    filter: none !important;
    transform: none !important;
}

.desk-ticker-row > div:first-child {
    min-width: 0 !important;
    padding-right: 18px !important;
}

.desk-ticker-row .meta-inline {
    line-height: 1.55 !important;
    max-width: 100% !important;
}

.desk-pm-header {
    min-height: 78px !important;
    height: 78px !important;
    padding: 0 0 14px 0 !important;
    margin: 0 0 18px 0 !important;
    border-bottom: 1px solid var(--desk-border) !important;
    align-items: flex-start !important;
}

.desk-position-read {
    border: 1px solid var(--desk-border);
    border-left: 4px solid var(--desk-green);
    border-radius: 6px;
    background: #FFFFFF;
    padding: 10px 12px;
    margin: -4px 0 14px;
}

.desk-position-kicker {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    margin-bottom: 5px;
}

.desk-position-main {
    display: flex;
    gap: 9px;
    align-items: baseline;
    flex-wrap: wrap;
    font-size: 14px;
    line-height: 1.45;
    color: var(--desk-text);
}

.desk-position-main > span:first-child {
    font-size: 20px;
    font-weight: 850;
}

.desk-position-stats {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 12px;
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px dashed var(--desk-border);
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 11px;
    color: var(--desk-muted);
}

.desk-analyze-dissent {
    border: 1px solid var(--desk-border);
    border-left: 3px solid var(--desk-blue);
    border-radius: 6px;
    background: #FFFFFF;
    padding: 9px 12px;
    margin: -6px 0 14px;
}

.desk-analyze-dissent-title {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 10px;
    font-weight: 850;
    letter-spacing: 0.13em;
    text-transform: uppercase;
    color: var(--desk-blue);
    margin-bottom: 4px;
}

.desk-analyze-dissent-copy {
    font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 14px;
    line-height: 1.45;
    color: var(--desk-text);
}

.desk-analyze-dissent-copy span {
    color: var(--desk-muted);
}

.desk-analyze-dissent-note {
    margin-top: 6px;
    padding-top: 6px;
    border-top: 1px dashed var(--desk-border);
    font-size: 13px;
    line-height: 1.45;
    color: var(--desk-muted);
}

.desk-decision-stack {
    display: flex;
    flex-direction: column;
    gap: 0;
    border: 1px solid var(--desk-border);
    border-radius: 8px;
    background: #FFFFFF;
    overflow: hidden;
    margin: -2px 0 16px;
}

.desk-trade-plan-label {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 10px;
    font-weight: 850;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--desk-muted);
    margin: 0 0 6px;
}

.desk-stack-cell {
    background: #FFFFFF;
    padding: 9px 12px;
    min-height: 0;
    border-top: 1px solid var(--desk-border);
}

.desk-stack-cell:first-child {
    border-top: 0;
}

.desk-stack-cell.compact {
    display: grid;
    grid-template-columns: 132px minmax(0, 1fr);
    gap: 14px;
    align-items: baseline;
}

.desk-stack-cell.hero {
    padding: 11px 12px;
    background: #FBFCFE;
}

.desk-stack-label {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 10px;
    font-weight: 850;
    letter-spacing: 0.13em;
    text-transform: uppercase;
    color: var(--desk-muted);
    margin-bottom: 0;
}

.desk-stack-value {
    font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 15px;
    line-height: 1.4;
    color: var(--desk-text);
}

.desk-stack-value strong,
.desk-stack-value b {
    font-weight: 850;
}

.desk-stack-call {
    font-size: 18px;
    font-weight: 850;
    line-height: 1.1;
    margin-right: 6px;
}

.desk-stack-context {
    color: var(--desk-muted);
    font-size: 14px;
}

.desk-pm-utility {
    border: 1px solid var(--desk-border);
    border-radius: 6px;
    background: #FFFFFF;
    padding: 8px 10px;
    margin: 0 0 14px;
}

.desk-pm-utility-label {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 10px;
    font-weight: 850;
    letter-spacing: 0.13em;
    text-transform: uppercase;
    color: var(--desk-muted);
    margin-bottom: 6px;
}

.desk-pm-memo {
    border-top: 1px solid var(--desk-border);
    margin-top: 14px;
    padding-top: 14px;
}

.desk-pm-memo .desk-pm-block {
    margin: 0;
    padding: 11px 0;
    border-bottom: 1px dashed var(--desk-border);
}

.desk-pm-memo .desk-pm-block:first-child {
    padding-top: 0;
}

.desk-pm-memo .desk-pm-block:last-child {
    border-bottom: 0;
}

.desk-stack-owned {
    color: var(--desk-muted);
    font-size: 13px;
    margin-left: 6px;
}

@media (max-width: 760px) {
    .desk-decision-stack {
        grid-template-columns: 1fr;
    }
}

.desk-ticker-row .ticker {
    font-size: 24px !important;
    line-height: 1 !important;
    font-weight: 850 !important;
    color: var(--desk-text) !important;
}

.desk-ticker-row .name {
    color: var(--desk-muted) !important;
    font-size: 12px !important;
    font-weight: 650 !important;
}

.desk-ticker-row .sub,
.desk-ticker-row .meta-line {
    margin-top: 8px !important;
    color: var(--desk-muted) !important;
    font-size: 12px !important;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace !important;
}

.desk-price {
    color: var(--desk-text) !important;
    font-size: 24px !important;
    font-weight: 850 !important;
}

.desk-card,
.desk-cmp,
.desk-dossier,
.desk-stat-card,
.desk-pm-card,
.desk-pm-thesis,
.desk-followup,
.desk-question-history,
div[data-testid="stExpander"] {
    background: var(--desk-panel) !important;
    border: 1px solid var(--desk-border) !important;
    border-radius: 8px !important;
    box-shadow: none !important;
}

.desk-avoid-reasons,
.desk-reconsider {
    border-radius: 8px !important;
    box-shadow: none !important;
}

.desk-avoid-reasons {
    background: #FFF7F8 !important;
    border: 1px solid #FFD8DE !important;
    border-left: 4px solid var(--desk-red) !important;
}

.desk-reconsider {
    background: #F2FFF8 !important;
    border: 1px solid #C9F3DA !important;
    border-left: 4px solid var(--desk-green) !important;
}

.desk-dossier {
    border-left: 4px solid var(--desk-blue) !important;
}

.desk-cmp {
    background: #FFFFFF !important;
}

.desk-cmp-badge,
.desk-logged-badge,
.research-link,
.desk-thesis-toggle,
.stButton > button,
button[kind="secondary"],
button[kind="primary"] {
    border-radius: 6px !important;
    box-shadow: none !important;
}

.desk-cmp-badge {
    padding: 4px 8px !important;
    border: 1px solid #A7F3D0 !important;
    background: #ECFDF5 !important;
    color: #047857 !important;
}

input,
textarea,
[data-baseweb="input"],
[data-baseweb="textarea"],
[data-testid="stTextInput"] input,
[data-testid="stTextInput"] div,
[data-testid="stTextArea"] textarea,
[data-testid="stTextArea"] div {
    border-radius: 6px !important;
    background: #FFFFFF !important;
    background-color: #FFFFFF !important;
    background-image: none !important;
}

[data-baseweb="input"] > div,
[data-baseweb="textarea"] > div,
[data-baseweb="base-input"],
[data-baseweb="base-input"] > div {
    background: #FFFFFF !important;
    background-color: #FFFFFF !important;
    background-image: none !important;
}

.desk-decision .word,
.decision-word,
.hero-decision {
    color: #506176 !important;
}

.desk-decision {
    padding: 12px 0 18px !important;
    margin: 0 0 18px 0 !important;
    overflow: visible !important;
}

.desk-decision .word {
    line-height: 1.02 !important;
    padding-top: 2px !important;
    padding-bottom: 2px !important;
    overflow: visible !important;
}

.desk-decision .emoji {
    line-height: 1 !important;
}

.desk-decision .context {
    margin-top: 8px !important;
}

.desk-trigger-block {
    margin: 0 0 16px !important;
}

.desk-invalidation {
    margin: 10px 0 20px !important;
}

.desk-dossier {
    margin-top: 14px !important;
}

.action-enter,
.decision-enter,
[data-action="enter"] { color: var(--desk-green) !important; }
.action-watch,
.decision-watch,
[data-action="watch"] { color: var(--desk-amber) !important; }
.action-avoid,
.decision-avoid,
[data-action="avoid"] { color: var(--desk-red) !important; }

.desk-chart-wrap,
.chart-shell {
    background: #FFFFFF !important;
    border: 1px solid var(--desk-border) !important;
    border-radius: 8px !important;
}

/* Decision comparison choice boxes: kill radio dots and pill geometry. */
.main [class*="st-key-decision_compare_user_pick_"] [role="radiogroup"] {
    display: flex !important;
    gap: 8px !important;
    align-items: stretch !important;
    flex-wrap: wrap !important;
}

.main [class*="st-key-decision_compare_user_pick_"] [role="radiogroup"] label {
    min-height: 36px !important;
    padding: 7px 12px !important;
    border: 1px solid var(--desk-border-strong) !important;
    border-radius: 6px !important;
    background: #FFFFFF !important;
    background-color: #FFFFFF !important;
    color: var(--desk-text) !important;
    box-shadow: none !important;
}

.main [class*="st-key-decision_compare_user_pick_"] [role="radiogroup"] label:hover {
    background: #F8FAFC !important;
    border-color: #94A3B8 !important;
}

.main [class*="st-key-decision_compare_user_pick_"] [role="radiogroup"] label:has(input:checked) {
    background: #EAF2FF !important;
    border-color: var(--desk-blue) !important;
    color: #17325F !important;
}

.main [class*="st-key-decision_compare_user_pick_"] [role="radiogroup"] label:has(input:checked) p,
.main [class*="st-key-decision_compare_user_pick_"] [role="radiogroup"] label p {
    color: inherit !important;
    margin: 0 !important;
}

.main [class*="st-key-decision_compare_user_pick_"] input[type="radio"],
.main [class*="st-key-decision_compare_user_pick_"] [role="radiogroup"] label > div:first-child,
.main [class*="st-key-decision_compare_user_pick_"] [role="radiogroup"] label svg {
    display: none !important;
    width: 0 !important;
    height: 0 !important;
    opacity: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
}

/* Last-mile beige removal for call notes and any Base Web fill. */
.main [class*="st-key-decision_compare_user_note_"] *,
.main [class*="st-key-decision_compare_user_note_"] input {
    background: #FFFFFF !important;
    background-color: #FFFFFF !important;
    background-image: none !important;
}

/* Absolute final expander/dropdown surface cleanup. Streamlit nests
   expanders several layers deep; force every layer away from beige fills. */
.main div[data-testid="stExpander"],
.main div[data-testid="stExpander"] *,
.main details.stExpander,
.main details.stExpander *,
.main div.stExpander,
.main div.stExpander *,
.main div[data-testid="stExpander"] > details,
.main div[data-testid="stExpander"] summary,
.main div[data-testid="stExpanderDetails"],
.main div[data-testid="stExpander"] div[data-testid="stExpanderDetails"] {
    background: #FFFFFF !important;
    background-color: #FFFFFF !important;
    background-image: none !important;
}
.main div[data-testid="stExpander"] summary,
.main details.stExpander summary,
.main div.stExpander summary {
    border-bottom: 1px solid var(--desk-border) !important;
}
.main div[data-testid="stExpander"] summary:hover,
.main details.stExpander summary:hover,
.main div.stExpander summary:hover {
    background: #F8FAFC !important;
    background-color: #F8FAFC !important;
}
.main div[data-testid="stExpander"] svg,
.main details.stExpander svg,
.main div.stExpander svg {
    background: transparent !important;
}

/* No warm-tinted dropdown/accordion surfaces anywhere in the app. */
.main details,
.main details *,
.main summary,
.main [data-baseweb="select"],
.main [data-baseweb="select"] *,
.main [data-baseweb="popover"],
.main [data-baseweb="popover"] *,
.main [role="listbox"],
.main [role="listbox"] *,
.main [role="option"],
.main [role="option"] * {
    background-color: #FFFFFF !important;
    background-image: none !important;
}
.main summary:hover,
.main [role="option"]:hover {
    background-color: #F8FAFC !important;
}

/* Streamlit sometimes renders expanders outside the .main scope; keep the
   final neutral surface rule global so technical sections cannot inherit tint. */
div[data-testid="stExpander"],
div[data-testid="stExpander"] *,
details.stExpander,
details.stExpander *,
div.stExpander,
div.stExpander *,
details,
details *,
summary {
    background-color: #FFFFFF !important;
    background-image: none !important;
}
div[data-testid="stExpander"] summary:hover,
details summary:hover {
    background-color: #F8FAFC !important;
}

/* ────────────────────────────────────────────────────────────── */
/*  Tablet / phone responsive polish                              */
/*  Keep the desktop workstation dense, but stop iPad/iPhone from  */
/*  inheriting clipped headers, crowded grids, and tiny tap targets.*/
/* ────────────────────────────────────────────────────────────── */
@media (max-width: 1180px) {
    .main .block-container,
    section.main .block-container,
    div[data-testid="stMainBlockContainer"] {
        padding-left: 1.1rem !important;
        padding-right: 1.1rem !important;
        max-width: 100vw !important;
    }

    .desk-ticker-row,
    .desk-pm-header {
        min-height: 88px !important;
        height: auto !important;
        padding-bottom: 12px !important;
    }

    .desk-ticker-row .meta-inline,
    .desk-ticker-row .sub,
    .desk-ticker-row .meta-line {
        white-space: normal !important;
        overflow-wrap: anywhere !important;
    }

    .desk-decision .word {
        font-size: clamp(58px, 8vw, 88px) !important;
    }

    .position-decision-stats {
        grid-template-columns: repeat(3, minmax(0, 1fr)) !important;
    }

    .research-layout {
        grid-template-columns: 1fr !important;
        gap: 22px !important;
    }

    .research-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
    }
}

@media (max-width: 1024px) {
    section[data-testid="stSidebar"] {
        min-width: 248px !important;
        max-width: 288px !important;
    }

    .desk-hero,
    .desk-top {
        min-width: 0 !important;
        overflow: visible !important;
    }

    .desk-decision-stack {
        grid-template-columns: 1fr !important;
    }

    .watch-queue-grid {
        grid-template-columns: repeat(3, minmax(0, 1fr)) !important;
    }

    .tech-memo-grid,
    .key-level-summary {
        grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
    }

    div[data-testid="stElementContainer"]:has(> div[style*="minmax(58px"]),
    div[data-testid="element-container"]:has(> div[style*="minmax(58px"]),
    div[data-testid="stElementContainer"]:has(.tracker-table),
    div[data-testid="element-container"]:has(.tracker-table),
    div[data-testid="stElementContainer"]:has(.holdings-grid),
    div[data-testid="element-container"]:has(.holdings-grid) {
        overflow-x: auto !important;
        -webkit-overflow-scrolling: touch !important;
        padding-bottom: 4px !important;
    }

    div[style*="minmax(58px,0.75fr)"],
    .tracker-table,
    .holdings-grid {
        min-width: 980px !important;
    }
}

@media (max-width: 900px) {
    [data-testid="stHorizontalBlock"] {
        flex-direction: column !important;
        gap: 0.65rem !important;
    }

    [data-testid="stHorizontalBlock"] > [data-testid="column"] {
        width: 100% !important;
        min-width: 100% !important;
        flex: 1 1 100% !important;
    }

    [data-testid="stHorizontalBlock"]:has(.desk-decision) > [data-testid="column"]:nth-child(2) {
        border-left: none !important;
        border-top: 1px solid var(--desk-border) !important;
        padding-left: 0 !important;
        padding-top: 18px !important;
        margin-top: 18px !important;
        min-height: 0 !important;
    }

    .desk-pm-container {
        border-left: none !important;
        border-top: 1px solid var(--desk-border) !important;
        padding-left: 0 !important;
        padding-top: 18px !important;
        margin-top: 18px !important;
    }
}

@media (max-width: 760px) {
    .main .block-container,
    section.main .block-container,
    div[data-testid="stMainBlockContainer"] {
        padding-top: 1.25rem !important;
        padding-left: 0.85rem !important;
        padding-right: 0.85rem !important;
    }

    .desk-sidebar-wordmark {
        margin-bottom: 14px !important;
        padding-bottom: 12px !important;
    }

    .desk-sidebar-help-tip {
        position: absolute !important;
        left: 50% !important;
        right: auto !important;
        top: calc(100% + 8px) !important;
        width: 230px !important;
        max-width: calc(100vw - 32px) !important;
        z-index: 99999 !important;
        transform: translateX(-50%) translateY(-2px) !important;
    }

    .desk-sidebar-help:hover .desk-sidebar-help-tip {
        transform: translateX(-50%) translateY(0) !important;
    }

    .desk-ticker-row,
    .desk-pm-header {
        min-height: 0 !important;
        height: auto !important;
        margin-bottom: 14px !important;
        padding-bottom: 11px !important;
    }

    .desk-ticker-row {
        display: grid !important;
        grid-template-columns: minmax(0, 1fr) auto !important;
        gap: 10px !important;
        align-items: start !important;
    }

    .desk-ticker-row .ticker,
    .desk-ticker-row .sym {
        font-size: 25px !important;
        line-height: 1.05 !important;
    }

    .desk-ticker-row .name {
        display: inline !important;
        font-size: 11px !important;
        margin-left: 5px !important;
        overflow-wrap: anywhere !important;
    }

    .desk-price,
    .desk-ticker-row .price {
        font-size: 20px !important;
        line-height: 1.1 !important;
        text-align: right !important;
    }

    .desk-ticker-row .chg {
        display: block !important;
        margin-left: 0 !important;
        margin-top: 3px !important;
        text-align: right !important;
    }

    .desk-decision {
        padding-top: 8px !important;
        margin-bottom: 14px !important;
    }

    .desk-decision .word {
        font-size: clamp(44px, 15vw, 64px) !important;
        letter-spacing: -0.03em !important;
        line-height: 0.98 !important;
    }

    .desk-decision .emoji {
        font-size: 30px !important;
        vertical-align: 5px !important;
        margin-left: 5px !important;
    }

    .desk-decision .context {
        font-size: 16px !important;
        line-height: 1.35 !important;
        max-width: 100% !important;
    }

    .desk-trigger-block,
    .desk-invalidation,
    .desk-avoid-reasons,
    .desk-reconsider,
    .desk-dossier,
    .desk-cmp,
    .position-decision-panel,
    .desk-stat-card {
        border-radius: 7px !important;
        margin-left: 0 !important;
        margin-right: 0 !important;
    }

    .desk-trigger-text,
    .desk-trigger-text b {
        font-size: 20px !important;
        line-height: 1.3 !important;
    }

    .desk-avoid-reasons,
    .desk-reconsider {
        padding: 12px 13px !important;
    }

    .desk-avoid-reasons li,
    .desk-reconsider li,
    .desk-dossier-text,
    .desk-pm-block .body,
    .desk-pm-item {
        font-size: 14px !important;
        line-height: 1.48 !important;
    }

    .position-decision-header {
        display: block !important;
    }

    .position-decision-meta {
        margin-top: 6px !important;
        text-align: left !important;
        white-space: normal !important;
    }

    .position-decision-stats,
    .research-grid,
    .watch-queue-grid,
    .tech-memo-grid,
    .key-level-summary {
        grid-template-columns: 1fr !important;
    }

    .research-page h1 {
        font-size: 34px !important;
        line-height: 1.04 !important;
        letter-spacing: -0.03em !important;
    }

    .research-page .deck {
        font-size: 16px !important;
        line-height: 1.45 !important;
    }

    .research-page .hero {
        padding: 14px 0 18px !important;
        border-radius: 0 !important;
        border-left: 0 !important;
        border-right: 0 !important;
    }

    .research-metric-row {
        gap: 8px !important;
    }

    .desk-data-strip {
        display: grid !important;
        grid-template-columns: 1fr !important;
    }

    .desk-data-chip {
        width: 100% !important;
        justify-content: space-between !important;
    }

    .main [class*="st-key-decision_compare_user_pick_"] [role="radiogroup"] label {
        min-height: 42px !important;
        flex: 1 1 calc(50% - 8px) !important;
        justify-content: center !important;
    }

    [data-testid="stTextInput"] input,
    [data-testid="stTextArea"] textarea,
    [data-testid="stNumberInput"] input,
    div.stButton > button {
        min-height: 42px !important;
        font-size: 16px !important;
    }

    .research-link,
    [class*="st-key-chat_send_"] button,
    [class*="st-key-clear_chat_"] button {
        width: 100% !important;
        min-height: 42px !important;
    }

    .tracker-table,
    .holdings-grid {
        min-width: 900px !important;
    }
}

@media (max-width: 430px) {
    .main .block-container,
    section.main .block-container,
    div[data-testid="stMainBlockContainer"] {
        padding-left: 0.65rem !important;
        padding-right: 0.65rem !important;
    }

    .desk-ticker-row .ticker,
    .desk-ticker-row .sym {
        font-size: 22px !important;
    }

    .desk-price,
    .desk-ticker-row .price {
        font-size: 18px !important;
    }

    .desk-decision .word {
        font-size: clamp(38px, 14vw, 54px) !important;
    }

    .desk-decision .emoji {
        font-size: 26px !important;
    }

    .desk-stack-cell {
        padding: 9px 10px !important;
        min-height: 0 !important;
    }

    .main [class*="st-key-decision_compare_user_pick_"] [role="radiogroup"] label {
        flex-basis: 100% !important;
    }
}
</style>
""", unsafe_allow_html=True)


view = st.session_state.view


# ── Database error banner — shown on every page if DB is unreachable ──
if st.session_state.get("_db_error"):
    st.error(
        "🚨 **Database connection failed — your watchlist and tracker are NOT being saved this session.**\n\n"
        "Fix: Go to Streamlit Cloud → Settings → Secrets and check your `DATABASE_URL`. "
        "The password must not contain literal `[brackets]`. Reset your Supabase password, "
        'paste the new URL wrapped in double quotes: `DATABASE_URL = "postgresql://..."`\n\n'
        f"Error detail: `{st.session_state['_db_error']}`"
    )

# ─────────────────────────────────────────────────────────────────────
# ANALYZE — decision-first
# ─────────────────────────────────────────────────────────────────────
if view == "analyze":
    ticker = st.session_state.current_ticker
    if not ticker:
        st.info("Type a ticker in the sidebar.")
        st.stop()

    with st.spinner(f"Loading {ticker}…"):
        hist, name, err_reason = fetch_history(ticker)
        bench = fetch_bench()
        meta = cached_quote_meta_snapshot(ticker)
        if not name:
            name = (
                (meta or {}).get("long_name")
                or (meta or {}).get("short_name")
                or infer_security_profile(ticker, meta).get("name")
                or ticker
            )

    if hist is None or len(hist) < 50:
        if err_reason:
            st.error(
                f"**Couldn't load data for {ticker}.**  \n"
                f"Reason: `{err_reason}`  \n\n"
                f"This is almost always a yfinance / Yahoo Finance API issue, "
                f"not a bug in this app. The yfinance library ships near-weekly "
                f"patches for Yahoo's API changes — if this persists, bumping "
                f"the version in `requirements.txt` and rebooting the app usually "
                f"fixes it within a day or two."
            )
        else:
            st.error(f"Couldn't find data for **{ticker}** — only {len(hist) if hist is not None else 0} rows of history (need ≥50).")
        st.stop()
    if bench is None:
        st.error("Couldn't load SPY benchmark — yfinance API issue, see above.")
        st.stop()
    bench_notice = benchmark_fallback_notice(bench)
    if bench_notice:
        st.warning(bench_notice)

    t = tactical.compute(hist, bench)
    if t is None:
        st.error(f"Insufficient history for {ticker}.")
        st.stop()
    try:
        t = {
            **t,
            "technical_prompt_context": _technical_snapshot_from_hist(hist, bench, t),
        }
    except Exception:
        t = {**t, "technical_prompt_context": {}}
    remember_sidebar_ticker_snapshot(ticker, t, hist)

    earnings_days = meta.get("earnings_days") if meta else None
    t = apply_earnings_event_gate(t, earnings_days)
    st.session_state["_current_tactical"] = {**t, "ticker": ticker.upper()}

    # Compute decision modifiers — earnings proximity, market regime, RS
    modifiers = tactical.decision_modifiers(t, meta, t.get("market_regime", "unknown"))

    # ── Single full-width header row ──────────────────────────────────
    # Render this before Claude/PM work so the page anchors immediately.
    chg_color  = "#2E7D4F" if t["change"] >= 0 else "#D14545"
    fallback_profile = infer_security_profile(ticker, meta, name)
    earn_banner, earn_footer = format_earnings(meta)
    setup_personality = classify_setup_personality(t)
    meta_bits  = [f"{setup_personality['emoji']} {setup_personality['label']}"]
    meta_bits.extend(build_security_meta_bits(ticker, meta, fallback_profile))
    if earn_footer and not earn_banner: meta_bits.append(f"Earnings {earn_footer}")
    meta_line  = " · ".join(meta_bits)
    chg_sign   = "+" if t["change"] >= 0 else ""
    company_label = display_security_name(ticker, name, meta, fallback_profile)
    company_html = (
        f'<span class="name">{html.escape(str(company_label))}</span>'
        if company_label else ""
    )
    meta_html = html.escape(meta_line)
    price_age_label, price_age_kind = format_market_data_age(hist)

    # Fetch PM data here (before splitting into columns) so the dossier on
    # the left can reference the thesis, and the right panel can render
    # the snapshot. Both share the same cached fetch.
    ticker_key = ticker.upper()
    pending_pm_refreshes = st.session_state.setdefault("_pending_pm_refreshes", {})
    force_pm_refresh = (
        st.session_state.pop("_force_pm_refresh_ticker", "") == ticker_key
        or ticker_key in pending_pm_refreshes
    )
    # Ordinary ticker navigation stays fast/static. Manual refresh uses the
    # dossier call as the single source of truth for thesis, quality, bullets,
    # and Claude dissent. Do not make a separate PM-note call first: it can
    # succeed while the dossier times out, which makes the UI claim "PM
    # refreshed" even though the visible thesis/quality layer stayed stale.
    allow_pm_generate = False
    needs_context_refresh = (
        ticker.upper() in SPECIAL_CONTEXT_REFRESH_TICKERS
        and dossier_cache_needs_upgrade(ticker)
    )
    # Never auto-generate research during normal ticker navigation. It makes
    # mobile first-paint painfully slow. Manual refresh/full report can still
    # regenerate; ordinary Analyze loads use cached/static PM content.
    allow_dossier_generate = False

    if force_pm_refresh:
        thesis_spinner = f"Refreshing {ticker.upper()} research…"
    elif needs_context_refresh:
        thesis_spinner = "Loading cached thesis…"
    elif allow_dossier_generate:
        thesis_spinner = f"Updating {ticker.upper()} research format…"
    else:
        thesis_spinner = "Loading cached thesis…"
    with st.spinner(thesis_spinner):
        pm = get_cached_pm(
            ticker, t,
            api_key=api_key if api_key else None,
            company_name=name,
            allow_generate=allow_pm_generate,
            force_generate=False,
        )
        allow_dossier_generate = bool(force_pm_refresh and api_key)
        dossier_result = get_cached_dossier(
            ticker, t, modifiers, meta, pm,
            api_key=api_key if api_key else None,
            company_name=name,
            allow_generate=allow_dossier_generate,
            force_generate=force_pm_refresh,
            fast_generate=force_pm_refresh,
        )
    if force_pm_refresh:
        pending_pm_refreshes.pop(ticker_key, None)

    # Live PM bullets: when the dossier call returned bullets, prefer those
    # over the static template. This is what makes non-hardcoded tickers
    # (DASH, PLTR, COIN, etc.) show real thesis/drivers/risks/valuation
    # instead of "Not yet analyzed". Bullets come from the SAME Claude call
    # as the dossier, so there's no extra cost.
    live_bullets = (dossier_result or {}).get("bullets") or {}
    dossier_source_for_bullets = str((dossier_result or {}).get("_source") or "").lower()
    dossier_bullets_are_current = not any(
        marker in dossier_source_for_bullets
        for marker in (
            "refresh to update",
            "research upgraded",
            "cached only",
            "fast mode",
            "unavailable",
            "error:",
        )
    )
    if live_bullets.get("thesis") and dossier_bullets_are_current:
        pm = {
            **pm,
            "thesis": live_bullets.get("thesis", pm.get("thesis", "")),
            "drivers": live_bullets.get("drivers") or pm.get("drivers", []),
            "risks": live_bullets.get("risks") or pm.get("risks", []),
            "valuation": live_bullets.get("valuation", pm.get("valuation", "")),
            "_source": (dossier_result or {}).get("_source", pm.get("_source", "")),
        }
    elif live_bullets.get("thesis") and str(pm.get("thesis") or "").startswith("No generated PM thesis yet"):
        pm = {
            **pm,
            "thesis": live_bullets.get("thesis", pm.get("thesis", "")),
            "drivers": live_bullets.get("drivers") or pm.get("drivers", []),
            "risks": live_bullets.get("risks") or pm.get("risks", []),
            "valuation": live_bullets.get("valuation", pm.get("valuation", "")),
            "_source": "cached dossier fallback · refresh to update",
        }
    research_items = research_health_items(pm, dossier_result, api_key)
    pm_status_item = research_items[0] if research_items else ("PM memo", "not generated", "warn")
    dossier_status_item = (
        research_items[1] if len(research_items) > 1 else ("Full dossier", "not generated", "warn")
    )
    sidebar_status_item = sidebar_cache_status(ticker)
    refresh_event = active_refresh_event(ticker)
    if isinstance(refresh_event, dict) and refresh_event.get("research"):
        refresh_event.update({
            "pm_label": pm_status_item[1],
            "pm_kind": pm_status_item[2],
            "dossier_label": dossier_status_item[1],
            "dossier_kind": dossier_status_item[2],
        })
    freshness_panel_html = canonical_freshness_html([
        ("Price", price_age_label, price_age_kind),
        ("Fundamentals", metadata_status_label(meta)[0], metadata_status_label(meta)[1]),
        ("PM memo", pm_status_item[1], pm_status_item[2]),
        ("Sidebar", sidebar_status_item[0], sidebar_status_item[1]),
    ], refresh_event=refresh_event)

    # Accumulation Watch override: if compute() flagged the name as
    # accumulation-eligible (deep drawdown + near low + stabilizing + not
    # breaking down) AND the dossier returned a Quality A or B tier, then
    # upgrade the action from "avoid" to "accumulate". Quality gate is
    # hard — this is the value-trap protection. Only fires when there's a
    # dossier (i.e., API key is set).
    quality_tier = ((dossier_result or {}).get("quality") or {}).get("tier", "")
    if t.get("is_accumulation_eligible") and t["action"] == "avoid":
        new_action = tactical.apply_accumulation_override(
            t["action"], True, quality_tier
        )
        if new_action != t["action"]:
            t = {**t, "action": new_action}

    # Historical-support trigger override: when price is approaching a
    # meaningful S/R level (auto-detected or user-marked), upgrade hold_off
    # to watch with a precise trigger. Only overrides hold_off — never
    # touches enter_now / watch / accumulate / avoid. The override is
    # gated on Quality A/B when a dossier is available, but auto-detected
    # levels can also fire on names where Claude hasn't been run yet (the
    # support setup itself is meaningful regardless of fundamentals).
    hidden_auto_levels = st.session_state.store.get("hidden_levels", {}).get(ticker.upper(), []) or []

    def _is_hidden_auto_level(level):
        try:
            level = float(level)
            return any(abs(level - float(h)) <= 0.02 for h in hidden_auto_levels)
        except (TypeError, ValueError):
            return False

    auto_levels = [
        lv for lv in (t.get("key_levels") or [])
        if not _is_hidden_auto_level(lv.get("level"))
    ]
    user_levels_for_ticker = (
        st.session_state.store.get("manual_levels", {}).get(ticker.upper(), {})
    )
    user_supports = user_levels_for_ticker.get("support", []) or []

    # Tag auto levels with source, build user-level objects matching shape
    merged_supports = []
    for lv in auto_levels:
        if lv.get("kind") == "support":
            merged_supports.append({**lv, "source": "auto"})
    for level_price in user_supports:
        try:
            merged_supports.append({
                "level": float(level_price),
                "touches": 0,
                "is_flip": False,
                "_score": 999,  # user-marked levels rank ABOVE auto
                "source": "manual",
            })
        except (TypeError, ValueError):
            continue

    support_trigger_override = None
    if t["action"] == "hold_off" and merged_supports:
        support_trigger_override = tactical.historical_support_trigger(
            price=t["price"], ma50=t["ma50"], atr_pct=t["atr_pct"],
            support_levels=merged_supports,
        )
        if support_trigger_override:
            # Block the upgrade for low-quality names (value trap risk).
            # When dossier is unavailable, allow the upgrade — the support
            # setup is real regardless. Only block when we explicitly know
            # quality is "Avoid" or "Speculative" (and the level is auto-
            # detected, not user-marked — user-marked = trusted override).
            is_user_marked = support_trigger_override["support_meta"]["source"] == "manual"
            blocked_by_quality = (
                quality_tier in ("Avoid", "Speculative") and not is_user_marked
            )
            if not blocked_by_quality:
                # Promote to watch and inject the trigger. If the support
                # trigger already fired and held, promote to Enter instead
                # of keeping the user trapped in a stale "almost there"
                # state for multiple sessions.
                support_action = "watch"
                support_trigger_fired = False
                support_trigger_fired_reason = ""
                support_meta = support_trigger_override.get("support_meta") or {}
                support_level = support_trigger_override["levels"].get("buy_above")
                support_confirmation_ok = (
                    t.get("vol_ratio", 1.0) >= 0.75 or
                    t.get("tech_delta", 0) > 0 or
                    t.get("rs_delta", 0) >= 0
                )
                if (
                    support_meta.get("status") == "held_above" and
                    support_confirmation_ok and
                    support_level is not None
                ):
                    support_action = "enter_now"
                    support_trigger_fired = True
                    support_trigger_fired_reason = (
                        f"Support at ${float(support_level):,.2f} already held; "
                        "continuation is sufficient for an entry signal."
                    )
                    support_trigger_override = {
                        **support_trigger_override,
                        "fired": True,
                        "fired_reason": support_trigger_fired_reason,
                    }
                t = {
                    **t,
                    "action": support_action,
                    "trigger": support_trigger_override,
                    "trigger_fired": support_trigger_fired,
                    "trigger_fired_reason": support_trigger_fired_reason,
                    "entry": (
                        t["price"] if support_trigger_fired
                        else support_trigger_override["levels"]["buy_above"]
                    ),
                    "entry_is_projected": not support_trigger_fired,
                    "stop": support_trigger_override["levels"].get("abort_below", t.get("stop")),
                }
            else:
                support_trigger_override = None  # don't render banner

    # Trigger memory: if the app previously told the user "act above X",
    # keep that level alive for a short window. Without this, recomputing
    # the setup after price moves can shift the target upward and make a
    # fired trigger look like it never happened.
    trigger_memory = st.session_state.store.setdefault("trigger_memory", {})
    trigger_memory_changed = False
    prior_trigger = trigger_memory.get(ticker_key) if isinstance(trigger_memory, dict) else None
    if isinstance(prior_trigger, dict) and t.get("action") in ("watch", "hold_off"):
        try:
            remembered_level = float(prior_trigger.get("buy_above"))
        except (TypeError, ValueError):
            remembered_level = None
        try:
            remembered_at = datetime.fromisoformat(str(prior_trigger.get("ts")))
            remembered_age_days = (datetime.now() - remembered_at).days
        except Exception:
            remembered_age_days = 999
        if (
            remembered_level and
            remembered_age_days <= 30 and
            float(t.get("price") or 0) >= remembered_level * 1.003 and
            (
                t.get("vol_ratio", 1.0) >= 0.75 or
                t.get("tech_delta", 0) > 0 or
                t.get("rs_delta", 0) >= 0
            )
        ):
            fired_reason = (
                f"Prior trigger at ${remembered_level:,.2f} fired and held; "
                "do not move the goalpost to the next resistance level."
            )
            fired_trigger = {
                "kind": prior_trigger.get("kind") or "remembered_trigger",
                "summary": prior_trigger.get("summary") or f"prior trigger above ${remembered_level:,.2f}",
                "buy_rule": prior_trigger.get("buy_rule") or f"Buy once price clears ${remembered_level:,.2f}.",
                "abort_rule": prior_trigger.get("abort_rule") or "",
                "levels": {
                    "buy_above": remembered_level,
                    "abort_below": prior_trigger.get("abort_below") or t.get("stop"),
                    "volume_min": prior_trigger.get("volume_min"),
                },
                "fired": True,
                "fired_reason": fired_reason,
            }
            t = {
                **t,
                "action": "enter_now",
                "trigger": fired_trigger,
                "trigger_fired": True,
                "trigger_fired_reason": fired_reason,
                "entry": t.get("price"),
                "entry_is_projected": False,
                "stop": prior_trigger.get("abort_below") or t.get("stop"),
            }
            prior_trigger["fired_at"] = datetime.now().isoformat(timespec="seconds")
            prior_trigger["fired_price"] = round(float(t.get("price") or remembered_level), 2)
            trigger_memory_changed = True

    active_trigger = t.get("trigger") if isinstance(t.get("trigger"), dict) else None
    active_buy_level = (active_trigger or {}).get("levels", {}).get("buy_above")
    try:
        active_buy_level = float(active_buy_level) if active_buy_level is not None else None
    except (TypeError, ValueError):
        active_buy_level = None
    if (
        active_buy_level and
        t.get("action") == "watch" and
        float(t.get("price") or 0) < active_buy_level * 0.997
    ):
        trigger_memory[ticker_key] = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "kind": active_trigger.get("kind"),
            "summary": active_trigger.get("summary"),
            "buy_rule": active_trigger.get("buy_rule"),
            "abort_rule": active_trigger.get("abort_rule"),
            "buy_above": round(active_buy_level, 2),
            "abort_below": (active_trigger.get("levels") or {}).get("abort_below"),
            "volume_min": (active_trigger.get("levels") or {}).get("volume_min"),
        }
        trigger_memory_changed = True
    if trigger_memory_changed:
        save_store(st.session_state.store)

    # The rule engine owns the actionable headline. Claude works as a PM
    # context / dissent layer, not as a replacement for the tactical gate.
    rule_t = dict(t)
    claude_call = (dossier_result or {}).get("tactical_call") or {}
    claude_action_raw = (claude_call.get("action") or "").upper()
    _claude_to_engine = {
        "ENTER": "enter_now", "WATCH": "watch", "HOLD_OFF": "hold_off",
        "AVOID": "avoid", "ACCUMULATE": "accumulate",
    }
    claude_action_key = _claude_to_engine.get(claude_action_raw, "")
    try:
        claude_confidence = int(claude_call.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        claude_confidence = 0
    claude_dissent_note_source = claude_call.get("reasoning") or claude_call.get("trigger") or ""
    try:
        from pm_view import substitute_live_values as _sub_live_values
        claude_dissent_note_source = _sub_live_values(
            claude_dissent_note_source,
            {**t, "price": t.get("price", 0)},
        )
    except Exception:
        pass
    claude_source_text = str((dossier_result or {}).get("_source") or "").lower()
    claude_is_current_enough = not any(
        marker in claude_source_text
        for marker in ("refresh to update", "research upgraded", "cached only", "unavailable")
    )
    hard_rule_lock = (
        (not t.get("atr_ok", True)) or
        bool(t.get("event_risk_hold")) or
        bool(t.get("event_risk_watch"))
    )
    if claude_action_key and claude_is_current_enough:
        dissent_signal = claude_dissent_signal(
            rule_t.get("action"),
            claude_action_raw,
            claude_confidence,
            claude_dissent_note_source,
        )
        if dissent_signal.get("flag"):
            source_note = f"★ {dissent_signal['reason']}"
        else:
            source_note = "Rules primary"
        if hard_rule_lock:
            source_note = "Rules primary · safety gate active"
        t = {
            **t,
            "_primary_source": "rule",
            "_rule_action": rule_t.get("action"),
            "_claude_action": claude_action_raw,
            "_claude_confidence": claude_confidence,
            "_claude_dissent": dissent_signal,
            "_source_note": source_note,
        }
    else:
        if hard_rule_lock:
            fallback_note = "Rules primary · safety gate active"
        else:
            fallback_note = "Rules primary"
        t = {
            **t,
            "_primary_source": "rule",
            "_rule_action": rule_t.get("action"),
            "_claude_action": claude_action_raw,
            "_claude_confidence": claude_confidence,
            "_claude_dissent": {"flag": False, "label": "", "reason": ""},
            "_source_note": fallback_note,
        }
    t = apply_earnings_event_gate(t, earnings_days)
    st.session_state["_current_tactical"] = {**t, "ticker": ticker.upper()}
    final_action_cache = st.session_state.store.setdefault("final_action_cache", {})
    cached_final = final_action_cache.get(ticker.upper(), {})
    final_payload = {
        "action": t.get("action"),
        "source": t.get("_primary_source", "rule"),
    }
    snapshots = st.session_state.store.setdefault("ticker_snapshots", {})
    snapshot_entry = snapshots.get(ticker.upper(), {})
    snapshot_final = (
        snapshot_entry.get("final_action")
        if isinstance(snapshot_entry, dict)
        else {}
    ) or {}
    if cached_final != final_payload or snapshot_final != final_payload:
        final_action_cache[ticker.upper()] = final_payload
        merge_ticker_snapshot(ticker, final_action=final_payload)
        save_store(st.session_state.store)
    rendered_sidebar_action = st.session_state.get("_active_sidebar_action_rendered")
    sync_key = f"{ticker.upper()}:{t.get('action')}"
    if (
        rendered_sidebar_action is not None and
        rendered_sidebar_action != t.get("action") and
        st.session_state.get("_sidebar_action_sync_key") != sync_key
    ):
        st.session_state["_sidebar_action_sync_key"] = sync_key
        st.rerun()

    sty = STATE_STYLES[t["action"]]

    col_decision, col_pm = st.columns([5, 3])

    # ───── LEFT COLUMN: decision + trading logic ─────
    with col_decision:
        ticker_header_html = (
            '<div class="desk-ticker-row">'
            '<div>'
            '<div style="display:flex;align-items:baseline;gap:10px;">'
            f'<span class="sym">{html.escape(str(ticker))}</span>'
            f'{company_html}'
            '</div>'
            f'<div class="meta-inline">{meta_html}</div>'
            '</div>'
            '<div style="white-space:nowrap;text-align:right;">'
            f'<span class="price">${t["price"]:,.2f}</span>'
            f'<span class="chg" style="color:{chg_color};">{chg_sign}{t["change"]:.2f}%</span>'
            '</div>'
            '</div>'
        )
        st.markdown(ticker_header_html, unsafe_allow_html=True)
        if earn_banner:
            st.markdown(
                f'<div class="desk-earnings-banner"><span class="em">📅</span>'
                f'<span><b>Event risk:</b> {html.escape(earn_banner)}</span></div>',
                unsafe_allow_html=True,
            )

        # 1. DECISION — hero
        # Structure state copy maps action+state to a one-line rationale
        # that appears under the giant decision word per the 2026-04-28
        # spec. State comes from classify_state() in tactical.py.
        _state = t.get("state", "TRENDING")
        _state_copy = {
            ("enter_now", "TRENDING"):  "Trend intact",
            ("enter_now", "TRANSITION"): "Trend reasserting",
            ("enter_now", "BROKEN"):    "Trend intact",
            ("watch", "TRENDING"):      "Trend intact",
            ("watch", "TRANSITION"):    "Trend reasserting",
            ("watch", "BROKEN"):        "Trend intact",
            ("hold_off", "TRENDING"):   "Signals not yet aligned",
            ("hold_off", "TRANSITION"): "Transitioning structure",
            ("hold_off", "BROKEN"):     "Signals not yet aligned",
            ("avoid", "TRENDING"):      "No directional edge",
            ("avoid", "TRANSITION"):    "No directional edge",
            ("avoid", "BROKEN"):        "Structure broken",
            ("accumulate", "TRENDING"): "Quality name stabilizing after deep drawdown",
            ("accumulate", "TRANSITION"): "Quality name stabilizing after deep drawdown",
            ("accumulate", "BROKEN"):   "Quality name stabilizing after deep drawdown",
        }.get((t["action"], _state), "")
        # Treat enter as "deploy" in the state copy per spec language
        _state_action_label = "Deploy" if t["action"] == "enter_now" else sty["label"]
        _source_note = t.get("_source_note") or (
            "Rules primary"
            if t.get("_primary_source") == "rule"
            else "Rules primary"
        )

        # Build the criteria tooltip for the current action.
        # Hover the info icon in the corner to reveal it; clicking does
        # nothing — purely a hover affordance to keep real estate clean.
        _current_style = STATE_STYLES[t["action"]]
        _criteria_items = "".join(
            f"<li>{c}</li>" for c in _current_style.get("criteria", [])
        )
        _tooltip_html = (
            f"<div class='tt-title' style=\"color:{sty['color']};\">"
            f"{sty['emoji']} {sty['label']}</div>"
            f"<div class='tt-tagline'>{_current_style.get('tagline', '')}</div>"
            f"<ul>{_criteria_items}</ul>"
            f"<div class='tt-footer'>The other states (Enter, Watch, Accumulate, "
            f"Hold off, Avoid) and their criteria are described in the help "
            f"area in the sidebar.</div>"
        )

        st.markdown(f"""
<div class="desk-decision">
  <div class="desk-decision-info" title="What does this mean?">i</div>
  <div class="desk-decision-info-tooltip">{_tooltip_html}</div>
  <span class="word" style="color:{sty['color']};">
    {sty['label']}<span style="color:var(--color-text);">.</span>
  </span>
  <span class="emoji">{sty['emoji']}</span>
  <div class="context">{decision_context(t)}</div>
  <div style="font-family: var(--font-mono); font-size:var(--fs-sm); font-weight:600;
              letter-spacing: var(--ls-caps-sm); text-transform:uppercase; color:{sty['color']};
              margin-top:8px; opacity:0.85;">
    {_state_action_label} — {_state_copy}
  </div>
</div>
""", unsafe_allow_html=True)

        analyze_dissent = t.get("_claude_dissent") or {}
        if analyze_dissent.get("flag"):
            analyze_dissent_note = analyze_dissent.get("note") or ""
            analyze_dissent_note_html = (
                f'<div class="desk-analyze-dissent-note">{html.escape(analyze_dissent_note)}</div>'
                if analyze_dissent_note else ""
            )
            st.markdown(
                f'<div class="desk-analyze-dissent">'
                f'<div class="desk-analyze-dissent-title">★ Claude dissent</div>'
                f'<div class="desk-analyze-dissent-copy">'
                f'{html.escape(analyze_dissent.get("reason", "Claude disagrees with rules."))} '
                f'<span>Rules remain the primary action; use this as a manual review flag.</span>'
                f'</div>{analyze_dissent_note_html}</div>',
                unsafe_allow_html=True,
            )

        active_position_entry = get_active_position_entry(ticker)
        active_holding = st.session_state.store.setdefault("holdings", {}).get(ticker.upper())
        position_read = (
            position_management_read(active_position_entry, t)
            if active_position_entry else None
        )
        stack_trigger_line = trigger_text(t)
        if not stack_trigger_line:
            stack_reconsider = reconsider_when(t)
            stack_trigger_line = stack_reconsider[0] if stack_reconsider else "No clean trigger yet."

        stack_risk_line = invalidation_text(t)
        if not stack_risk_line:
            if earn_banner:
                stack_risk_line = f"Event risk: {earn_banner}"
            else:
                stack_why = why_avoid_reasons(t)
                stack_risk_line = stack_why[0] if stack_why else "Respect sizing until the setup improves."

        if position_read:
            position_stat_line = " · ".join(
                f"{label} {value}" for label, value in position_read["stats"][:4]
            )
            owned_value = (
                f'<span class="desk-stack-call" style="color:{position_read["color"]};">'
                f'{html.escape(position_read["emoji"])} {html.escape(position_read["action"])}</span>'
                f'<span>{html.escape(position_stat_line)}</span>'
            )
        else:
            owned_value = (
                '<span class="desk-stack-call" style="color:var(--desk-muted);">Not tracked</span>'
                '<span class="desk-stack-owned">Add a holding to get trim/sell logic.</span>'
            )

        st.markdown(f"""
<div class="desk-trade-plan-label">Trade plan</div>
<div class="desk-decision-stack">
  <div class="desk-stack-cell hero">
    <div class="desk-stack-value">
      <span class="desk-stack-call" style="color:{sty['color']};">{sty['emoji']} {html.escape(sty['label'])}</span>
      <span class="desk-stack-context">{html.escape(decision_context(t))}</span>
    </div>
  </div>
  <div class="desk-stack-cell compact">
    <div class="desk-stack-label">Trigger</div>
    <div class="desk-stack-value">{bold_numbers(stack_trigger_line)}</div>
  </div>
  <div class="desk-stack-cell compact">
    <div class="desk-stack-label">Risk</div>
    <div class="desk-stack-value">{bold_numbers(stack_risk_line)}</div>
  </div>
  <div class="desk-stack-cell compact">
    <div class="desk-stack-label">If owned</div>
    <div class="desk-stack-value">{owned_value}</div>
  </div>
</div>
""", unsafe_allow_html=True)

        if position_read:
            stat_html = "".join(
                f'<div class="position-decision-stat">'
                f'<div class="k">{html.escape(label)}</div>'
                f'<div class="v">{html.escape(value)}</div>'
                f'</div>'
                for label, value in position_read.get("stats", [])[:6]
            )
            note = ""
            try:
                user_note = (active_holding or {}).get("user_note", "").strip()
                if user_note:
                    note = (
                        f'<div style="border-top:1px dashed var(--color-border-soft);'
                        f'margin-top:10px;padding-top:9px;font-size:var(--fs-sm);'
                        f'color:var(--color-muted);">'
                        f'<span style="font-family:var(--font-mono);font-size:10px;'
                        f'font-weight:800;letter-spacing:var(--ls-caps-sm);'
                        f'text-transform:uppercase;color:var(--color-faint);">Your note</span> '
                        f'{html.escape(user_note)}</div>'
                    )
            except Exception:
                note = ""
            st.markdown(
                f'<div class="position-decision-panel" '
                f'style="border-left:4px solid {position_read["color"]};">'
                f'<div class="position-decision-header">'
                f'<div>'
                f'<div class="position-decision-label">Position decision</div>'
                f'<div class="position-decision-action" style="color:{position_read["color"]};">'
                f'{html.escape(position_read["emoji"])} {html.escape(position_read["action"])}</div>'
                f'</div>'
                f'<div class="position-decision-meta">{html.escape(ticker.upper())} · owned position</div>'
                f'</div>'
                f'<div class="position-decision-summary">{html.escape(position_read["summary"])}</div>'
                f'<div class="position-decision-stats">{stat_html}</div>'
                f'{note}'
                f'</div>',
                unsafe_allow_html=True,
            )

        def _position_input_value(value):
            try:
                if value is None:
                    return ""
                return f"{float(value):.2f}"
            except (TypeError, ValueError):
                return ""

        def _parse_position_price(value):
            value = str(value or "").strip().replace("$", "").replace(",", "")
            if not value:
                return None
            try:
                return round(float(value), 2)
            except ValueError:
                return "invalid"

        def _save_position_fields(entry, entry_value, target_value, stop_value, note_value, shares_value=None):
            parsed_entry = _parse_position_price(entry_value)
            parsed_target = _parse_position_price(target_value)
            parsed_stop = _parse_position_price(stop_value)
            if "invalid" in (parsed_entry, parsed_target, parsed_stop):
                return False
            if parsed_entry is not None:
                entry["entry_price"] = parsed_entry
                entry["entry_hit_price"] = parsed_entry
            entry["target1_price"] = parsed_target
            entry["stop_price"] = parsed_stop
            entry["user_note"] = str(note_value or "").strip()
            if shares_value is not None:
                try:
                    shares = float(str(shares_value).replace(",", "").strip()) if str(shares_value).strip() else None
                    entry["shares"] = shares
                except (TypeError, ValueError):
                    return False
            entry["levels_edited_at"] = datetime.now().isoformat(timespec="seconds")
            return True

        if position_read:
            with st.expander("Edit holding / position", expanded=False):
                st.caption("Update the live position inputs used by the trim/sell read.")
                p_col1, p_col2, p_col3, p_col4 = st.columns(4)
                with p_col1:
                    analyze_entry_px = st.text_input(
                        "Entry",
                        value=_position_input_value(
                            active_position_entry.get("entry_hit_price") or
                            active_position_entry.get("entry_price")
                        ),
                        key=f"analyze_position_entry_{ticker}_{active_position_entry.get('id', '')}",
                    )
                with p_col2:
                    analyze_target_px = st.text_input(
                        "Target",
                        value=_position_input_value(active_position_entry.get("target1_price")),
                        key=f"analyze_position_target_{ticker}_{active_position_entry.get('id', '')}",
                    )
                with p_col3:
                    analyze_stop_px = st.text_input(
                        "Stop",
                        value=_position_input_value(active_position_entry.get("stop_price")),
                        key=f"analyze_position_stop_{ticker}_{active_position_entry.get('id', '')}",
                    )
                with p_col4:
                    analyze_shares = st.text_input(
                        "Shares",
                        value=str(active_position_entry.get("shares") or ""),
                        key=f"analyze_position_shares_{ticker}_{active_position_entry.get('id', '')}",
                        placeholder="Optional",
                    )
                analyze_position_note = st.text_input(
                    "Note",
                    value=str(active_position_entry.get("user_note") or ""),
                    key=f"analyze_position_note_{ticker}_{active_position_entry.get('id', '')}",
                    placeholder="Optional note",
                )
                if st.button(
                    "Save position levels",
                    key=f"analyze_save_position_{ticker}_{active_position_entry.get('id', '')}",
                    use_container_width=True,
                ):
                    if not _save_position_fields(
                        active_position_entry,
                        analyze_entry_px,
                        analyze_target_px,
                        analyze_stop_px,
                        analyze_position_note,
                        analyze_shares,
                    ):
                        st.warning("One of the edited prices is not a valid number.")
                    else:
                        if active_holding is not None:
                            holding = st.session_state.store.setdefault("holdings", {}).setdefault(ticker.upper(), {})
                            holding.update({
                                "ticker": ticker.upper(),
                                "entry_price": active_position_entry.get("entry_price"),
                                "target1_price": active_position_entry.get("target1_price"),
                                "stop_price": active_position_entry.get("stop_price"),
                                "shares": active_position_entry.get("shares"),
                                "user_note": active_position_entry.get("user_note", ""),
                                "updated_at": datetime.now().isoformat(timespec="seconds"),
                            })
                        save_store(st.session_state.store)
                        st.success("Position levels updated.")
                        st.rerun()
                if active_holding is not None:
                    if st.button(
                        f"Remove {ticker.upper()} from holdings",
                        key=f"remove_holding_{ticker}",
                        use_container_width=True,
                    ):
                        st.session_state.store.setdefault("holdings", {}).pop(ticker.upper(), None)
                        save_store(st.session_state.store)
                        st.rerun()
        else:
            with st.expander("I own this / add position", expanded=False):
                st.caption("Add an existing position so the app can show a trim/sell read for this ticker.")
                p_col1, p_col2, p_col3, p_col4 = st.columns(4)
                with p_col1:
                    new_position_entry_px = st.text_input(
                        "Entry",
                        key=f"new_position_entry_{ticker}",
                        placeholder=f"{t['price']:.2f}",
                    )
                with p_col2:
                    new_position_target_px = st.text_input(
                        "Target",
                        value=_position_input_value(t.get("t1")),
                        key=f"new_position_target_{ticker}",
                    )
                with p_col3:
                    new_position_stop_px = st.text_input(
                        "Stop",
                        value=_position_input_value(t.get("stop")),
                        key=f"new_position_stop_{ticker}",
                    )
                with p_col4:
                    new_position_shares = st.text_input(
                        "Shares",
                        key=f"new_position_shares_{ticker}",
                        placeholder="Optional",
                    )
                new_position_note = st.text_input(
                    "Note",
                    key=f"new_position_note_{ticker}",
                    placeholder="Optional note",
                )
                if st.button("Save as position", key=f"save_new_position_{ticker}", use_container_width=True):
                    import uuid
                    new_entry_px = _parse_position_price(new_position_entry_px)
                    if new_entry_px in (None, "invalid"):
                        st.warning("Add a valid entry price first.")
                    else:
                        new_position = {
                            "id": str(uuid.uuid4())[:8],
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "ticker": ticker.upper(),
                        }
                        if not _save_position_fields(
                            new_position,
                            new_position_entry_px,
                            new_position_target_px,
                            new_position_stop_px,
                            new_position_note,
                            new_position_shares,
                        ):
                            st.warning("One of the edited prices is not a valid number.")
                        else:
                            st.session_state.store.setdefault("holdings", {})[ticker.upper()] = {
                                "id": new_position["id"],
                                "ticker": ticker.upper(),
                                "entry_price": new_position.get("entry_price"),
                                "target1_price": new_position.get("target1_price"),
                                "stop_price": new_position.get("stop_price"),
                                "shares": new_position.get("shares"),
                                "user_note": new_position.get("user_note", ""),
                                "created_at": datetime.now().isoformat(timespec="seconds"),
                                "updated_at": datetime.now().isoformat(timespec="seconds"),
                            }
                            save_store(st.session_state.store)
                            st.success(f"Holding added for {ticker.upper()}.")
                            st.rerun()

        # 1c. Decision dossier — the synthesis paragraph. Now sits ABOVE
        # the comparison panel so users read Claude's full reasoning
        # first, then see the side-by-side comparison and log their call.
        dossier_text = dossier_result.get("dossier") if dossier_result else None
        # Auto-clear cache if it still contains raw {token} placeholders
        import re as _re
        if dossier_text and _re.search(r"\{[a-z_]+\}", dossier_text):
            clear_dossier_cache(ticker)
            dossier_result = None
            dossier_text   = None
            st.rerun()
        if dossier_text:
            src = dossier_result.get("_source", "claude")

            # Freshness caption — only shown when the cached prose is from
            # a previous day. Tells the user "this analysis is N days old
            # but numbers shown are current". Helps build trust in the
            # substitution layer.
            freshness = (dossier_result or {}).get("_freshness") or {}
            freshness_caption = ""
            if freshness.get("age_days", 0) > 0:
                age_days = freshness["age_days"]
                cached_p = freshness.get("price_at_generation")
                live_p = freshness.get("current_price")
                if cached_p and live_p and cached_p != live_p:
                    pct_moved = (live_p - cached_p) / cached_p * 100
                    pct_color = (
                        "var(--color-positive)" if pct_moved >= 0
                        else "var(--color-negative)"
                    )
                    freshness_caption = (
                        f'<div style="font-family: var(--font-sans);'
                        f'font-size: var(--fs-sm); color: var(--color-faint);'
                        f'margin-top: 8px; padding-top: 8px;'
                        f'border-top: 1px dashed var(--color-border-soft);">'
                        f'Analysis from {age_days}d ago. Current numbers shown. '
                        f'Price has moved <span style="color:{pct_color};">'
                        f'{pct_moved:+.1f}%</span> since (${cached_p:,.2f} → ${live_p:,.2f}).'
                        f'</div>'
                    )
                else:
                    freshness_caption = (
                        f'<div style="font-family: var(--font-sans);'
                        f'font-size: var(--fs-sm); color: var(--color-faint);'
                        f'margin-top: 8px; padding-top: 8px;'
                        f'border-top: 1px dashed var(--color-border-soft);">'
                        f'Analysis from {age_days}d ago. Current numbers shown.'
                        f'</div>'
                    )

            st.markdown(f"""
<div class="desk-dossier">
  <div class="desk-dossier-label">
    <span><span class="em">📋</span>Decision dossier</span>
    <span class="src">{src}</span>
  </div>
  <div class="desk-dossier-text">{dossier_text}</div>
  {freshness_caption}
</div>
""", unsafe_allow_html=True)

        # 1a-extra. DECISION COMPARISON — rule engine vs Claude vs you.
        # Diagnostic panel for the 2-4 week trial period to evaluate which
        # decision source is producing better calls. Shows side-by-side
        # whenever the user wants to log a comparison. Renders even when
        # Claude data is missing (older cached dossiers without the
        # tactical_call field) — in that case the Claude side shows a
        # "regenerate to compare" nudge but the Rules+You logging works.
        claude_call = (dossier_result or {}).get("tactical_call") or {}
        claude_action_raw = (claude_call.get("action") or "").upper()
        claude_action_key = _claude_to_engine.get(claude_action_raw, "")
        rule_action = rule_t.get("action", t["action"])
        # Three states: agree, disagree, unknown.
        if not claude_action_key:
            agreement_state = "unknown"
        elif claude_action_key == rule_action:
            agreement_state = "agree"
        else:
            agreement_state = "disagree"

        def _decision_compare_read(rule_key, claude_key, t_state, claude_data):
            """Plain-English read of why the two decision systems differ."""
            if not claude_key:
                return (
                    "Claude comparison is missing because this row was generated before "
                    "tactical_call data existed. Refresh Portfolio Manager to compare it."
                )

            rule_label_local = STATE_STYLES.get(rule_key, {}).get(
                "label", str(rule_key or "Rules").replace("_", " ").title()
            )
            claude_label_local = STATE_STYLES.get(claude_key, {}).get(
                "label", str(claude_key or "Claude").replace("_", " ").title()
            )

            signal = _decision_signal_snapshot(t_state)
            trig = t_state.get("trigger") or {}
            trig_kind = (trig.get("kind") or "").replace("_", " ")
            if rule_key == claude_key:
                return (
                    f"Both sources read this as {rule_label_local}. "
                    f"The shared signal is {signal}."
                    if signal else f"Both sources read this as {rule_label_local}."
                )

            if rule_key == "enter_now" and claude_key in ("watch", "hold_off"):
                driver = "Claude is applying more timing discipline than the rules."
            elif rule_key in ("watch", "hold_off") and claude_key == "enter_now":
                driver = "Claude is willing to act before the rule engine fully clears the entry."
            elif rule_key == "watch" and claude_key == "hold_off":
                driver = "Rules see a defined setup, while Claude wants a cleaner confirmation first."
            elif rule_key == "hold_off" and claude_key == "watch":
                driver = "Claude sees enough upside structure to track a trigger, while rules still classify it as ambiguous."
            elif "avoid" in (rule_key, claude_key):
                driver = "The split is about whether weakness is structural damage or just poor timing."
            elif "accumulate" in (rule_key, claude_key):
                driver = "The split is about whether this is a long-term quality entry or still just tactical waiting."
            else:
                driver = "The split is mainly timing and conviction."

            if trig_kind:
                driver += f" Trigger focus: {trig_kind}."
            if signal:
                driver += f" Current tape: {signal}."
            return f"Claude: {claude_label_local}. Rules: {rule_label_local}. {driver}"

        comparison_read = _decision_compare_read(
            rule_action, claude_action_key, t, claude_call
        )
        disagreement_read = classify_decision_disagreement(
            rule_action, claude_action_key, t, claude_call, position_read
        )

        # Always render the comparison panel. Build agree/disagree/unknown badge.
        rule_sty = STATE_STYLES.get(rule_action, {})
        claude_sty = STATE_STYLES.get(claude_action_key, {}) if claude_action_key else {}
        _badge_class = {
            "disagree": "desk-cmp-badge desk-cmp-badge-disagree",
            "agree":    "desk-cmp-badge desk-cmp-badge-agree",
            "unknown":  "desk-cmp-badge desk-cmp-badge-unknown",
        }[agreement_state]
        _badge_text = {
            "disagree": "disagree",
            "agree":    "agree",
            "unknown":  "claude data missing",
        }[agreement_state]
        disagree_marker = f'<span class="{_badge_class}">{_badge_text}</span>'

        confidence = claude_call.get("confidence", 0)
        try:
            confidence = int(confidence)
        except (TypeError, ValueError):
            confidence = 0
        reasoning = (claude_call.get("reasoning") or "").strip()
        claude_trigger = (claude_call.get("trigger") or "").strip()

        # Substitute live tokens ({price}, {pct_ma50}, etc.) in reasoning/trigger
        try:
            from pm_view import substitute_live_values as _sub
            _t_state_for_sub = {**t, "price": t.get("price", 0)}
            if reasoning:
                reasoning = _sub(reasoning, _t_state_for_sub)
            if claude_trigger:
                claude_trigger = _sub(claude_trigger, _t_state_for_sub)
        except Exception:
            pass

        # Claude side content varies based on whether data is present.
        if claude_action_raw:
            _claude_label = claude_action_raw.replace("_", " ").title()
            claude_html = (
                f'<div class="desk-cmp-action" style="color:{claude_sty.get("color", "var(--color-text)")};">'
                f'{claude_sty.get("emoji", "")} {_claude_label}'
                f'</div>'
                f'<div class="desk-cmp-meta">Confidence: {confidence}/10</div>'
            )
        else:
            claude_html = (
                '<div class="desk-cmp-fallback">'
                'Click ↻ on Portfolio Manager panel to regenerate'
                '</div>'
                '<div class="desk-cmp-meta" style="color:var(--color-fainter);">'
                'Older cached dossier without tactical_call data'
                '</div>'
            )

        reasoning_html = (
            f'<div class="desk-cmp-reasoning">'
            f'<span class="desk-cmp-reasoning-label">Claude reasoning</span>'
            f'{reasoning}</div>'
            if reasoning else ''
        )
        trigger_html = (
            f'<div class="desk-cmp-trigger">'
            f'<span class="desk-cmp-trigger-label">⚡ Trigger</span> {claude_trigger}</div>'
            if claude_trigger else ''
        )

        # Render comparison + logging UI inside a single bordered Streamlit
        # container. Single-line HTML strings (no leading whitespace inside
        # any string content — Streamlit's markdown processor treats
        # indented lines as code blocks). Use Streamlit's native columns
        # for the side-by-side rather than CSS grid, since the HTML
        # markdown block doesn't share a DOM with the widget block.
        if SHOW_ARCHIVED_TRACKER:
            # Header row: "Decision comparison" + agree/disagree badge
            st.markdown(
                f'<div class="desk-cmp-header"><span>Decision comparison</span>{disagree_marker}</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="desk-cmp-read"><strong>Read:</strong> '
                f'{html.escape(comparison_read)}</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="desk-cmp-resolution">'
                f'<div class="desk-cmp-resolution-top">'
                f'<span class="desk-cmp-resolution-chip">'
                f'{html.escape(disagreement_read.get("emoji", ""))} '
                f'{html.escape(disagreement_read.get("kind", "Decision read"))}</span>'
                f'<span class="desk-cmp-resolution-title">'
                f'{html.escape(disagreement_read.get("title", "How to resolve it"))}</span>'
                f'</div>'
                f'<div class="desk-cmp-resolution-text">'
                f'<strong>Resolution:</strong> {html.escape(disagreement_read.get("read", ""))}'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            # Side-by-side via CSS grid: Claude (primary) | divider | Rule engine (secondary)
            # Claude is on the left because it's the primary decision source —
            # the rule engine is the comparison baseline. The 1px middle column
            # of the grid renders as a vertical divider that spans the full
            # height of both sides.
            rule_color = rule_sty.get("color", "#0F0E0D")
            rule_label = rule_sty.get("label", rule_action)
            rule_emoji = rule_sty.get("emoji", "")
            state_label = t.get("state", "TRENDING")
            st.markdown(
                f'<div class="desk-cmp-grid">'
                f'<div>'
                f'<div class="desk-cmp-side-label">Claude <span style="color:var(--color-muted);font-weight:500;">· primary</span></div>'
                f'{claude_html}'
                f'</div>'
                f'<div class="desk-cmp-divider"></div>'
                f'<div>'
                f'<div class="desk-cmp-side-label">Rule engine <span style="color:var(--color-faint);font-weight:500;">· secondary</span></div>'
                f'<div class="desk-cmp-action" style="color:{rule_color};">{rule_emoji} {rule_label}</div>'
                f'<div class="desk-cmp-meta">State: {state_label}</div>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            # Reasoning + trigger blocks (only when present)
            if reasoning_html:
                st.markdown(reasoning_html, unsafe_allow_html=True)
            if trigger_html:
                st.markdown(trigger_html, unsafe_allow_html=True)

            # ── Already logged? Show status ───────────────────────────
            dlog_check = st.session_state.store.get("decisions_log", [])
            existing_entry = next(
                (d for d in dlog_check
                 if d.get("ticker") == ticker.upper() and d.get("outcome") is None),
                None,
            )
            if existing_entry:
                logged_ts = existing_entry.get("ts", "")[:10]
                logged_price = existing_entry.get("price", 0)
                logged_action = (existing_entry.get("user_action") or "").replace("_", " ").title()
                logged_note = existing_entry.get("user_note", "")
                logged_status = ""
                if existing_entry.get("position_status") == "entered" and existing_entry.get("entry_hit_at"):
                    try:
                        entered_px = f'${float(existing_entry.get("entry_hit_price") or existing_entry.get("entry_price")):,.2f}'
                    except (TypeError, ValueError):
                        entered_px = "entry"
                    logged_status = (
                        f' · entered <span style="font-family:var(--font-mono);">'
                        f'{entered_px}</span>'
                        f' on {existing_entry.get("entry_hit_at")}'
                    )
                st.markdown(
                    f'<div style="background:var(--color-surface);border:1px solid var(--color-border);'
                    f'border-left:3px solid var(--color-positive);border-radius:4px;'
                    f'padding:8px 12px;margin-bottom:8px;font-size:var(--fs-base);">'
                    f'<span style="font-weight:600;color:var(--color-positive);">✓ Logged</span>'
                    f' — {logged_action} at <span style="font-family:var(--font-mono);">'
                    f'${logged_price:,.2f}</span> on {logged_ts}'
                    + logged_status
                    + (f' · <span style="color:var(--color-muted);font-style:italic;">{logged_note}</span>' if logged_note else '')
                    + '</div>',
                    unsafe_allow_html=True,
                )
                if (
                    existing_entry.get("position_status") != "entered" and
                    existing_entry.get("user_action") in ("ENTER", "WATCH", "ACCUMULATE")
                ):
                    if st.button(
                        "Mark as in position",
                        key=f"mark_entered_{ticker}_{existing_entry.get('id', '')}",
                        use_container_width=True,
                    ):
                        existing_entry["position_status"] = "entered"
                        existing_entry["entry_hit_at"] = datetime.now().date().isoformat()
                        existing_entry["entry_hit_price"] = round(float(t.get("price", logged_price)), 2)
                        existing_entry["manual_entry_logged"] = True
                        save_store(st.session_state.store)
                        st.rerun()

            # "Your call" header with info-icon hover
            _action_help = (
                "Enter — high-conviction setup, buy now (bullish + setup ≥ 8.5, no bad extension). "
                "Watch — bullish but waiting on a specific trigger. "
                "Hold off — universal default for ambiguity (pullbacks, transitions, mixed signals). "
                "Avoid — truly broken (below ma200 + RS<0.9 + not improving + no momentum). "
                "Accumulate — quality A/B name in deep drawdown, stabilizing — small starter only."
            )
            st.markdown(
                f'<div class="desk-cmp-yourcall-label">'
                f'<span>Your call</span>'
                f'<span class="desk-cmp-info" title="{_action_help}">i</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

            # Radio: stored as canonical UPPER_SNAKE, displayed via format_func
            user_choice_options = ["ENTER", "WATCH", "HOLD_OFF", "AVOID", "ACCUMULATE"]
            _display_labels = {
                "ENTER": "Enter",
                "WATCH": "Watch",
                "HOLD_OFF": "Hold off",
                "AVOID": "Avoid",
                "ACCUMULATE": "Accumulate",
            }
            _engine_to_user_label = {
                "enter_now": "ENTER",
                "watch": "WATCH",
                "hold_off": "HOLD_OFF",
                "avoid": "AVOID",
                "accumulate": "ACCUMULATE",
            }
            _default_label = _engine_to_user_label.get(rule_action, "WATCH")
            user_pick = st.radio(
                "Your call",
                options=user_choice_options,
                format_func=lambda x: _display_labels.get(x, x),
                index=user_choice_options.index(_default_label),
                horizontal=True,
                key=f"decision_compare_user_pick_{ticker}",
                label_visibility="collapsed",
            )

            # Note input + Log button on a single row
            log_c1, log_c2 = st.columns([3, 1])
            with log_c1:
                user_note = st.text_input(
                    "Note (optional)",
                    key=f"decision_compare_user_note_{ticker}",
                    placeholder="Why this call? (optional)",
                    label_visibility="collapsed",
                )
            with log_c2:
                log_clicked = st.button(
                    "Log",
                    key=f"log_compare_{ticker}",
                    use_container_width=True,
                    type="primary",
                )
            if log_clicked:
                import uuid
                entry = {
                    "id": str(uuid.uuid4())[:8],
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "ticker": ticker.upper(),
                    "price": round(t["price"], 2),
                    "rule_action": rule_action,
                    "rule_state": t.get("state", ""),
                    "claude_action": claude_action_raw,
                    "claude_confidence": confidence,
                    "claude_reasoning": reasoning,
                    "claude_trigger": claude_trigger,
                    "agreement_state": agreement_state,
                    "agreement_read": comparison_read,
                    "entry_price": round(float(t.get("entry")), 2) if t.get("entry") is not None else None,
                    "stop_price": round(float(t.get("stop")), 2) if t.get("stop") is not None else None,
                    "target1_price": round(float(t.get("t1")), 2) if t.get("t1") is not None else None,
                    "target2_price": round(float(t.get("t2")), 2) if t.get("t2") is not None else None,
                    "avoid_price": round(float(t.get("price")), 2) if (
                        rule_action == "avoid" or claude_action_raw == "AVOID" or user_pick == "AVOID"
                    ) else None,
                    "entry_is_projected": bool(t.get("entry_is_projected")),
                    "trigger_summary": trigger_text(t),
                    "user_action": user_pick,
                    "user_note": user_note.strip() if user_note else "",
                    "outcome": None,
                }
                dlog = st.session_state.store.setdefault("decisions_log", [])
                dlog.insert(0, entry)
                save_store(st.session_state.store)
                st.success(f"Logged ({entry['id']}). View it in Tracker.")

        # 1b-chat. Follow-up chat ────────────────────────────────────────
        # Chat history is persisted in the store so it survives page reloads.
        if "chat_history" not in st.session_state.store:
            st.session_state.store["chat_history"] = {}
        chat_store = st.session_state.store["chat_history"]
        chat_key = ticker.upper()
        if chat_key not in chat_store:
            chat_store[chat_key] = []

        with st.expander("💬 Ask a follow-up question", expanded=bool(chat_store[chat_key])):
            if not api_key:
                st.caption("Add an Anthropic API key in the sidebar to use chat.")
            else:
                import html as _html

                def _chat_display_text(value):
                    text = _html.unescape(str(value or "")).strip()
                    # Normalize common escaped/model-formatting artifacts so
                    # chat answers render as clean prose instead of raw code.
                    text = text.replace("`", "")
                    text = text.replace("\\n", "\n")
                    return text

                def _chat_norm(text):
                    return " ".join((text or "").strip().lower().split())

                def _saved_chat_answers():
                    saved = []
                    seen = set()
                    history = chat_store[chat_key]
                    for i, msg in enumerate(history):
                        if msg.get("role") != "user":
                            continue
                        q_text = msg.get("content", "").strip()
                        q_norm = _chat_norm(q_text)
                        if not q_norm or q_norm in seen:
                            continue
                        answer = ""
                        for nxt in history[i + 1:]:
                            if nxt.get("role") == "assistant":
                                answer = nxt.get("content", "").strip()
                                break
                            if nxt.get("role") == "user":
                                break
                        if answer:
                            seen.add(q_norm)
                            saved.append({"question": q_text, "answer": answer})
                    return saved

                saved_answers = _saved_chat_answers()
                if saved_answers:
                    items_html = []
                    for item in saved_answers[-5:][::-1]:
                        q_html = _html.escape(item["question"])
                        a_preview = _chat_display_text(item["answer"]).replace("\n", " ").strip()
                        if len(a_preview) > 170:
                            a_preview = a_preview[:170].rstrip() + "..."
                        a_html = _html.escape(a_preview)
                        items_html.append(
                            f'<div class="desk-chat-history-item">'
                            f'<div class="desk-chat-history-q">{q_html}</div>'
                            f'<div class="desk-chat-history-a">{a_html}</div>'
                            f'</div>'
                        )
                    st.markdown(
                        '<div class="desk-chat-history">'
                        '<div class="desk-chat-history-title">Previously asked</div>'
                        + "".join(items_html) +
                        '</div>',
                        unsafe_allow_html=True,
                    )

                # Render message history
                for msg in chat_store[chat_key]:
                    if msg["role"] == "user":
                        st.markdown(
                            f'<div style="margin-bottom:6px;font-size:var(--fs-sm);'
                            f'color:var(--color-faint);line-height:1.4;">'
                            f'<span style="font-weight:600;text-transform:uppercase;'
                            f'letter-spacing:0.05em;font-size:var(--fs-xs);">You</span> '
                            f'{_html.escape(msg["content"])}</div>',
                            unsafe_allow_html=True,
                        )
                    else:
                        with st.container(border=True):
                            st.markdown(_chat_display_text(msg["content"]))

                # Centered input module. Use a form + text input so pressing
                # Enter submits the question, matching the Ask button.
                _, chat_col, _ = st.columns([1, 10, 1])
                with chat_col:
                    with st.form(key=f"chat_form_{ticker}", clear_on_submit=True):
                        user_q = st.text_input(
                            "Ask anything about this ticker",
                            key=f"chat_input_{ticker}",
                            label_visibility="collapsed",
                            placeholder=f"Ask anything about {ticker}…",
                        )
                        _, ask_col, _ = st.columns([5, 2, 5])
                        with ask_col:
                            send = st.form_submit_button("Ask", use_container_width=True)

                if chat_store[chat_key]:
                    _, clear_col, _ = st.columns([5, 2, 5])
                    with clear_col:
                        if st.button("Clear", key=f"clear_chat_{ticker}", type="secondary", use_container_width=True):
                            chat_store[chat_key] = []
                            save_store(st.session_state.store)
                            st.rerun()

                if send and user_q.strip():
                    q = user_q.strip()

                    chat_store[chat_key].append({"role": "user", "content": q})

                    dossier_ctx = (dossier_result or {}).get("dossier") or "Not yet generated."
                    tech_ctx    = (dossier_result or {}).get("technical_narrative") or ""
                    pm_ctx      = (dossier_result or {}).get("pm_narrative") or ""
                    sys_prompt = (
                        f"You are a sharp, concise portfolio analyst assistant. "
                        f"The user is looking at {ticker} ({name}) right now. "
                        f"IMPORTANT: {ticker} is the US-listed stock ({name}) on NYSE/NASDAQ. "
                        f"Do NOT confuse with any foreign company sharing this ticker. Live context:\n\n"
                        f"Price: ${t.get('price', 0):,.2f}\n"
                        f"Action: {t.get('action', '').replace('_', ' ').upper()}\n"
                        f"MA50: ${t.get('ma50', 0):,.2f} | MA200: ${t.get('ma200', 0):,.2f}\n"
                        f"RSI: {t.get('rsi14', t.get('rsi', 0)):.0f} | RS: {t.get('rs', 0):.2f}\n"
                        f"ATR%: {t.get('atr_pct', 0):.1f}%\n"
                        f"Support: ${t.get('support', 0):,.2f} | Resistance: ${t.get('resistance', 0):,.2f}\n\n"
                        f"Decision dossier:\n{dossier_ctx}\n\n"
                        + (f"Technical narrative:\n{tech_ctx}\n\n" if tech_ctx else "")
                        + (f"PM narrative:\n{pm_ctx}\n\n" if pm_ctx else "")
                        + "Answer concisely. 2–4 sentences unless more is clearly needed."
                    )
                    import anthropic as _anthropic
                    with st.spinner("Thinking…"):
                        try:
                            _client = _anthropic.Anthropic(api_key=api_key)
                            _tools  = [{"type": "web_search_20250305", "name": "web_search"}]
                            _msgs   = list(chat_store[chat_key])
                            _in, _out = 0, 0
                            reply = ""
                            def _create_followup_message(messages, use_tools=True):
                                kwargs = {
                                    "max_tokens": 600,
                                    "system": sys_prompt,
                                    "messages": messages,
                                }
                                if use_tools:
                                    kwargs["tools"] = _tools
                                    kwargs["betas"] = ["web-search-2025-03-05"]
                                try:
                                    return anthropic_messages_create(_client, **kwargs)
                                except TypeError as _err:
                                    if "betas" not in str(_err):
                                        raise
                                    # Older Anthropic SDKs do not accept the
                                    # `betas` kwarg. Fall back to normal Claude
                                    # chat rather than surfacing an SDK error.
                                    kwargs.pop("betas", None)
                                    kwargs.pop("tools", None)
                                    return anthropic_messages_create(_client, **kwargs)

                            for _ in range(6):
                                _resp = _create_followup_message(_msgs, use_tools=True)
                                _in  += _resp.usage.input_tokens
                                _out += _resp.usage.output_tokens
                                text_parts = [b.text for b in _resp.content if hasattr(b, "text") and b.text]
                                if _resp.stop_reason == "end_turn":
                                    reply = " ".join(text_parts).strip()
                                    break
                                if _resp.stop_reason == "tool_use":
                                    _msgs.append({"role": "assistant", "content": _resp.content})
                                    _results = []
                                    for _b in _resp.content:
                                        if _b.type == "tool_use":
                                            _rc = getattr(_b, "content", "") or ""
                                            if isinstance(_rc, list):
                                                _rc = " ".join(c.get("text","") if isinstance(c,dict) else str(c) for c in _rc)
                                            _results.append({"type": "tool_result", "tool_use_id": _b.id, "content": str(_rc)})
                                    _msgs.append({"role": "user", "content": _results})
                                else:
                                    reply = " ".join(text_parts).strip()
                                    break
                            st.session_state["session_cost"] = (
                                st.session_state.get("session_cost", 0.0) + (_in * 3 + _out * 15) / 1_000_000
                            )
                        except Exception as e:
                            reply = f"Error: {e}"
                    chat_store[chat_key].append({"role": "assistant", "content": reply})
                    save_store(st.session_state.store)
                    st.rerun()

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

        # Read of the tape — always visible, anchors every screen with the
        # current technical state in concrete numbers.
        tape_rows = tape_read(t)
        color_map = {"pos": "#2E7D4F", "neg": "#D14545", "": "#334155"}
        tape_html = "".join(
            f'<div class="row">'
            f'  <span class="k">{label}</span>'
            f'  <span class="v" style="color:{color_map.get(sev, "var(--color-body)")};">{bold_numbers(value)}</span>'
            f'</div>'
            for label, value, sev in tape_rows
        )
        st.markdown(f"""
    <div class="desk-tape-read">
      <div class="label"><span class="em">📊</span>Read of the tape</div>
      {tape_html}
    </div>
    """, unsafe_allow_html=True)

        # Technical narrative/context is rendered once in the footer
        # "Technical details" expander below the chart. Keep the variables
        # here because chat context also uses the dossier output.
        tech_narrative = dossier_result.get("technical_narrative") if dossier_result else None
        commentary_lines = technical_commentary(t)

        # 4. IF TRIGGER HITS — conditional trade plan
        if t["action"] in ("enter_now", "watch"):
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
                note_html = f'<span class="sub">{note}</span>' if note else ""
                st.markdown(f"""
    <div class="desk-plan-row">
      <span class="k">{label}</span>
      <span style="text-align:right;line-height:1.2;">
    <span class="v">${value:,.2f}</span>{delta_html}
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
                        f'<div style="font-family: var(--font-sans);font-style:italic;'
                        f'font-size:var(--fs-sm);color:#8B6914;margin-top:4px;">'
                        f'Capped by max position size ({max_pos_pct*100:.0f}%). '
                        f'Risk math alone wanted {risk_shares:,} shares.'
                        f'</div>'
                    )

                st.markdown(f"""
    <div style="margin-top:12px;padding:10px 12px;background:var(--color-surface-soft);border-radius:3px;
            font-family: var(--font-mono);font-size:var(--fs-sm);color:var(--color-body);line-height:1.45;">
      <div>
    <span style="color:var(--color-faint);">Risk</span>
    <b style="color:var(--color-text);">${effective_risk:,.0f}</b>
    <span style="color:var(--color-fainter);">({effective_risk_pct:.2f}% of ${account:,.0f})</span>
      </div>
      <div>
    <span style="color:var(--color-faint);">Shares</span>
    <b style="color:var(--color-text);">{shares:,}</b>
    <span style="color:var(--color-fainter);">at ${t['entry']:,.2f} entry</span>
      </div>
      <div>
    <span style="color:var(--color-faint);">Position</span>
    <b style="color:var(--color-text);">${position_value:,.0f}</b>
    <span style="color:var(--color-fainter);">({pos_pct:.1f}% of account)</span>
      </div>
      {cap_note}
    </div>
    """, unsafe_allow_html=True)

        # Keep chart and detailed technicals visible by default. This is the
        # core tape-read surface, not an optional advanced view.
        show_deep_technicals = True
        if show_deep_technicals:
            # 4. Chart — TradingView Lightweight Charts (open-source, ~40KB).
            # Renders client-side from our existing OHLCV data. Replaces the
            # earlier Plotly attempt because Lightweight Charts produces a
            # professional-looking trading chart (matches the paid TradingView
            # widget aesthetic) with full styling control — the free TradingView
            # embed widget doesn't allow per-MA color overrides, which is why
            # we own the rendering ourselves.
            st.markdown(f"""
        <div class="desk-chart-label">
          <span style="color:var(--color-muted);">📈 Chart · </span>
          <span style="color:#F97316;font-weight:700;">MA 20</span>
          <span style="color:var(--color-muted);"> · </span>
          <span style="color:#2563EB;font-weight:700;">MA 50</span>
          <span style="color:var(--color-muted);"> · </span>
          <span style="color:#9333EA;font-weight:700;">MA 100</span>
          <span style="color:var(--color-muted);"> · </span>
          <span style="color:#DC2626;font-weight:700;">MA 200</span>
        </div>
        """, unsafe_allow_html=True)

            try:
                import json as _chart_json

                # Build the data payload for Lightweight Charts.
                # Show 1 year so MA200 has proper context. MAs are computed
                # on the FULL hist series so they're valid even at the start
                # of the visible window.
                chart_hist = hist.iloc[-252:].copy()
                chart_hist["MA20"] = hist["Close"].rolling(20).mean().iloc[-252:]
                chart_hist["MA50"] = hist["Close"].rolling(50).mean().iloc[-252:]
                chart_hist["MA100"] = hist["Close"].rolling(100).mean().iloc[-252:]
                chart_hist["MA200"] = hist["Close"].rolling(200).mean().iloc[-252:]

                # Lightweight Charts wants seconds-since-epoch (UTCTimestamp)
                # for time values. The hist index is daily DatetimeIndex.
                def _ts(idx):
                    return int(idx.timestamp())

                candles = [
                    {
                        "time": _ts(idx),
                        "open": round(float(row["Open"]), 4),
                        "high": round(float(row["High"]), 4),
                        "low": round(float(row["Low"]), 4),
                        "close": round(float(row["Close"]), 4),
                    }
                    for idx, row in chart_hist.iterrows()
                ]
                volume = [
                    {
                        "time": _ts(idx),
                        "value": float(row["Volume"]),
                        "color": ("rgba(22,163,74,0.45)" if row["Close"] >= row["Open"]
                                  else "rgba(220,38,38,0.45)"),
                    }
                    for idx, row in chart_hist.iterrows()
                ]

                def _ma_series(col):
                    out = []
                    for idx, val in chart_hist[col].items():
                        # Lightweight Charts skips points with None/null
                        if val is None:
                            continue
                        try:
                            if val != val:  # NaN check
                                continue
                        except Exception:
                            continue
                        out.append({"time": _ts(idx), "value": round(float(val), 4)})
                    return out

                ma_data = {
                    "MA20":  _ma_series("MA20"),
                    "MA50":  _ma_series("MA50"),
                    "MA100": _ma_series("MA100"),
                    "MA200": _ma_series("MA200"),
                }

                payload = _chart_json.dumps({
                    "candles": candles,
                    "volume": volume,
                    "ma": ma_data,
                })

                chart_html = f"""
        <div id="lwchart_{ticker}" style="width:100%;height:480px;background:#FFFFFF;"></div>
        <script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
        <script>
        (function() {{
          const data = {payload};
          const container = document.getElementById('lwchart_{ticker}');
          if (!container) return;

          const chart = LightweightCharts.createChart(container, {{
        width: container.clientWidth,
        height: 480,
        layout: {{
          background: {{ type: 'solid', color: '#FFFFFF' }},
          textColor: '#334155',
          fontFamily: 'Geist Mono, monospace',
          fontSize: 11,
        }},
        grid: {{
          vertLines: {{ color: '#E5E7EB' }},
          horzLines: {{ color: '#E5E7EB' }},
        }},
        rightPriceScale: {{
          borderColor: '#DCE3EA',
          scaleMargins: {{ top: 0.08, bottom: 0.28 }},
        }},
        timeScale: {{
          borderColor: '#DCE3EA',
          timeVisible: false,
          secondsVisible: false,
        }},
        crosshair: {{
          mode: LightweightCharts.CrosshairMode.Normal,
          vertLine: {{ color: '#94A3B8', width: 1, style: 2 }},
          horzLine: {{ color: '#94A3B8', width: 1, style: 2 }},
        }},
        handleScroll: true,
        handleScale: true,
          }});

          // Candlesticks
          const candleSeries = chart.addCandlestickSeries({{
        upColor: '#16A34A',
        downColor: '#DC2626',
        borderUpColor: '#16A34A',
        borderDownColor: '#DC2626',
        wickUpColor: '#16A34A',
        wickDownColor: '#DC2626',
        priceLineVisible: true,
        priceLineColor: '#94A3B8',
        priceLineWidth: 1,
        priceLineStyle: 2,
          }});
          candleSeries.setData(data.candles);

          // Four colored MA lines — the whole reason we're not using the
          // free TradingView embed widget, which doesn't allow per-study colors.
          const maConfigs = [
        {{ key: 'MA20',  color: '#F97316' }},
        {{ key: 'MA50',  color: '#2563EB' }},
        {{ key: 'MA100', color: '#9333EA' }},
        {{ key: 'MA200', color: '#DC2626' }},
          ];
          for (const cfg of maConfigs) {{
        const series = chart.addLineSeries({{
          color: cfg.color,
          lineWidth: 1.5,
          priceLineVisible: false,
          lastValueVisible: true,
          title: cfg.key,
          crosshairMarkerVisible: false,
        }});
        series.setData(data.ma[cfg.key]);
          }}

          // Volume on a separate price scale at the bottom (overlay)
          const volumeSeries = chart.addHistogramSeries({{
        priceFormat: {{ type: 'volume' }},
        priceScaleId: 'volume',
        color: 'rgba(107, 101, 91, 0.4)',
          }});
          volumeSeries.priceScale().applyOptions({{
        scaleMargins: {{ top: 0.78, bottom: 0 }},
          }});
          volumeSeries.setData(data.volume);

          chart.timeScale().fitContent();

          // Re-fit on window resize so the chart stays full-width
          const resize = () => {{
        chart.applyOptions({{ width: container.clientWidth }});
          }};
          window.addEventListener('resize', resize);
        }})();
        </script>
        """
                st.components.v1.html(chart_html, height=500)
            except Exception as _chart_err:
                st.warning(f"Chart could not render: {_chart_err}")

            # 5. Technical details — footer, collapsed
            with st.expander("Technical details"):
                ma_rows, momentum_rows, strength_rows, timeframe_rows, levels_rows = detailed_technical_rows(hist, bench, t)
                st.markdown(
                    '<div style="font-family:Geist,sans-serif;font-size:var(--fs-xs);'
                    'font-weight:700;letter-spacing: var(--ls-caps-lg);text-transform:uppercase;'
                    'color:var(--color-muted);margin-bottom:8px;">Technical memo</div>'
                    '<div class="tech-memo-grid">'
                    f'{render_technical_table("Trend / moving averages", ma_rows)}'
                    f'{render_technical_table("Momentum", momentum_rows)}'
                    f'{render_technical_table("Relative strength / volume", strength_rows)}'
                    f'{render_technical_table("Daily vs weekly", timeframe_rows)}'
                    f'{render_technical_table("Levels / volatility", levels_rows)}'
                    '</div>',
                    unsafe_allow_html=True,
                )
                if commentary_lines:
                    commentary_html = "".join(
                        f'<p style="margin: 0 0 8px; font-size: var(--fs-md); line-height: 1.65; '
                        f'color: var(--color-body); font-family: Geist, sans-serif;">'
                        f'{bold_numbers(line)}</p>'
                        for line in commentary_lines
                    )
                    st.markdown(
                        f'<div style="border-top:1px dashed var(--color-border);margin:12px 0 14px;"></div>'
                        f'<div style="font-family:Geist,sans-serif;font-size:var(--fs-xs);'
                        f'font-weight:700;letter-spacing: var(--ls-caps-lg);text-transform:uppercase;'
                        f'color:var(--color-muted);margin-bottom:8px;">Tape detail</div>'
                        f'<div style="padding: 0 2px 8px;">{commentary_html}</div>',
                        unsafe_allow_html=True,
                    )
                if tech_narrative:
                    paragraphs = [p.strip() for p in tech_narrative.split("\n\n") if p.strip()]
                    paras_html = "".join(
                        f'<p style="margin: 0 0 12px; font-size: var(--fs-md); line-height: 1.65; '
                        f'color: var(--color-body); font-family: Geist, sans-serif;">{p}</p>'
                        for p in paragraphs
                    )
                    st.markdown(
                        '<div style="border-top:1px dashed var(--color-border);margin:12px 0 14px;"></div>'
                        '<div style="font-family:Geist,sans-serif;font-size:var(--fs-xs);'
                        'font-weight:700;letter-spacing: var(--ls-caps-lg);text-transform:uppercase;'
                        'color:var(--color-muted);margin-bottom:8px;">Narrative</div>'
                        f'<div style="padding: 0 2px;">{paras_html}</div>',
                        unsafe_allow_html=True,
                    )
                st.markdown(
                    '<div style="border-top:1px dashed var(--color-border);margin:12px 0 14px;"></div>'
                    '<div style="font-family:Geist,sans-serif;font-size:var(--fs-xs);'
                    'font-weight:700;letter-spacing: var(--ls-caps-lg);text-transform:uppercase;'
                    'color:var(--color-muted);margin-bottom:8px;">Rule engine inputs</div>',
                    unsafe_allow_html=True,
                )
                rows = [
                    ("Bias", f"{t['bias'].capitalize() if t['bias'] else '—'} ({t['bias_score']:+d} on ±10 scale)"),
                    ("Technical score", f"{t['setup_score']:.1f} / 10"),
                    ("50-day moving average", f"${t['ma50']:,.2f}"),
                    ("200-day moving average", f"${t['ma200']:,.2f}"),
                    ("Average true range", f"{t['atr_pct']*100:.2f}%" + (" · below 1.5% gate" if not t['atr_ok'] else "")),
                    ("Relative strength vs S&P 500", f"{t['rs']:.3f} · 10d {'+' if t['rs_delta'] >= 0 else ''}{t['rs_delta']:.3f}"),
                    ("52-week high", f"${t['high_52w']:,.2f}"),
                    ("20-day average volume", f"{t['avg_vol_20d']:,.0f}"),
                    ("Today volume / average", f"{t['vol_ratio']:.2f}×"),
                    ("Structure quality", f"{t['structure_quality']:.1f} / 10"),
                ]
                for label, value in rows:
                    st.markdown(f"""
        <div style="display:flex;justify-content:space-between;font-family: var(--font-mono);font-size:var(--fs-sm);color:var(--color-muted);padding:3px 0;">
          <span>{label}</span><span style="color:var(--color-text);">{value}</span>
        </div>
        """, unsafe_allow_html=True)

        # 7. Key Levels — auto-detected support/resistance, focused on
        # proximate actionable levels. Collapsed by default; opens to show
        # nearby levels first with cleaner language than the prior version.
        with st.expander("🎯 Key levels — support / resistance"):
            raw_auto_lvls = t.get("key_levels") or []
            user_lvls = st.session_state.store.setdefault("manual_levels", {}).setdefault(
                ticker.upper(), {"support": [], "resistance": []}
            )
            hidden_lvls = st.session_state.store.setdefault("hidden_levels", {}).setdefault(
                ticker.upper(), []
            )
            current_price = t["price"]

            def _level_is_hidden(level):
                try:
                    level = float(level)
                    return any(abs(level - float(h)) <= 0.02 for h in hidden_lvls)
                except (TypeError, ValueError):
                    return False

            auto_lvls = [
                lv for lv in raw_auto_lvls
                if not _level_is_hidden(lv.get("level"))
            ]

            def _level_kind(level):
                return "support" if level <= current_price else "resistance"

            def _level_distance(level):
                pct = (level - current_price) / current_price * 100
                if abs(pct) < 0.5:
                    return "at current price"
                if pct > 0:
                    return f"{pct:.1f}% above"
                return f"{abs(pct):.1f}% below"

            def _level_note(lv):
                if not lv:
                    return "No clean tested level."
                level = float(lv["level"])
                touches = lv.get("touches", 0)
                flip = " · S/R flip" if lv.get("is_flip") else ""
                return f"{_level_distance(level)} · {touches}× tested{flip}"

            manual_rows = (
                [
                    {"level": float(v), "touches": 1, "is_flip": False, "kind": "support", "bucket": "support"}
                    for v in (user_lvls.get("support", []) or [])
                ] +
                [
                    {"level": float(v), "touches": 1, "is_flip": False, "kind": "resistance", "bucket": "resistance"}
                    for v in (user_lvls.get("resistance", []) or [])
                ]
            )

            all_visible_levels = manual_rows + [
                {**lv, "kind": _level_kind(float(lv["level"]))}
                for lv in auto_lvls
            ]
            support_candidates = sorted(
                [lv for lv in all_visible_levels if float(lv["level"]) <= current_price],
                key=lambda lv: abs(float(lv["level"]) - current_price),
            )
            resistance_candidates = sorted(
                [lv for lv in all_visible_levels if float(lv["level"]) > current_price],
                key=lambda lv: abs(float(lv["level"]) - current_price),
            )
            primary_support = support_candidates[0] if support_candidates else None
            primary_resistance = resistance_candidates[0] if resistance_candidates else None
            reclaim_level = primary_resistance
            invalidation_level = primary_support or (
                {"level": t.get("ma50"), "touches": 0, "is_flip": False}
                if t.get("ma50") and t.get("ma50") < current_price else None
            )

            def _card(label, lv, note=None, accent="var(--color-text)"):
                if lv and lv.get("level"):
                    value = f"${float(lv['level']):,.2f}"
                    sub = note or _level_note(lv)
                else:
                    value = "—"
                    sub = note or "No clean tested level."
                return (
                    '<div class="key-level-card">'
                    f'<div class="key-level-label">{html.escape(label)}</div>'
                    f'<div class="key-level-price" style="color:{accent};">{html.escape(value)}</div>'
                    f'<div class="key-level-note">{html.escape(sub)}</div>'
                    '</div>'
                )

            st.markdown(
                '<div style="font-family:Geist,sans-serif;font-size:var(--fs-xs);'
                'font-weight:800;letter-spacing: var(--ls-caps-lg);text-transform:uppercase;'
                'color:var(--color-muted);margin:4px 0 6px;">Decision levels</div>'
                '<div class="key-level-summary">'
                + _card("Primary support", primary_support, accent="var(--color-positive)")
                + _card("Primary resistance", primary_resistance, accent="var(--color-negative)")
                + _card(
                    "Reclaim / breakout",
                    reclaim_level,
                    note="Next upside level to reclaim." if reclaim_level else "No nearby resistance to reclaim.",
                    accent="var(--color-blue)",
                )
                + _card(
                    "Invalidation",
                    invalidation_level,
                    note="Nearest support to lose." if invalidation_level else "No clean support below.",
                    accent="var(--color-negative)",
                )
                + '</div>',
                unsafe_allow_html=True,
            )

            def _render_level_row(lv, row_key, *, source):
                level = float(lv["level"])
                kind = lv.get("kind") or _level_kind(level)
                color = "#00A870" if kind == "support" else "#D14545"
                touches_text = f"{lv['touches']}× tested"
                flip_text = " · S/R flip" if lv.get("is_flip") else ""
                label = "Remove" if source == "manual" else "Hide"
                c1, c2, c3 = st.columns([2.2, 2.6, 0.8])
                c1.markdown(
                    f'<div style="font-family:var(--font-mono);font-size:var(--fs-md);'
                    f'font-weight:700;color:{color};padding:8px 0;">${level:,.2f} '
                    f'<span style="font-family:var(--font-sans);font-size:var(--fs-sm);'
                    f'font-weight:600;color:var(--color-muted);margin-left:6px;">{kind}</span></div>',
                    unsafe_allow_html=True,
                )
                c2.markdown(
                    f'<div style="font-family:var(--font-sans);font-size:var(--fs-base);'
                    f'color:var(--color-muted);padding:9px 0;">'
                    f'{_level_distance(level)} · {touches_text}{flip_text}</div>',
                    unsafe_allow_html=True,
                )
                if c3.button(label, key=row_key, use_container_width=True):
                    if source == "manual":
                        bucket = lv.get("bucket") or ("support" if kind == "support" else "resistance")
                        user_lvls[bucket] = [
                            v for v in user_lvls.get(bucket, [])
                            if abs(float(v) - level) > 0.02
                        ]
                        st.session_state.store["manual_levels"][ticker.upper()] = user_lvls
                    else:
                        hidden_lvls.append(round(level, 2))
                        st.session_state.store["hidden_levels"][ticker.upper()] = sorted(set(hidden_lvls))
                    save_store(st.session_state.store)
                    st.rerun()
                st.markdown(
                    '<div style="border-bottom:1px solid var(--color-border-soft);margin:0 0 2px;"></div>',
                    unsafe_allow_html=True,
                )

            # Split into proximate (within 15%) vs distant
            proximate = [
                lv for lv in auto_lvls
                if abs((lv["level"] - current_price) / current_price) <= 0.15
            ]
            distant = [lv for lv in auto_lvls if lv not in proximate]

            if manual_rows:
                st.markdown(
                    '<div style="font-family:Geist,sans-serif;font-size:var(--fs-xs);'
                    'font-weight:700;letter-spacing: var(--ls-caps-lg);text-transform:uppercase;'
                    'color:var(--color-muted);margin:4px 0 6px;">Marked by you</div>',
                    unsafe_allow_html=True,
                )
                for idx, lv in enumerate(sorted(manual_rows, key=lambda x: x["level"])):
                    _render_level_row(lv, f"manual_lvl_{ticker}_{idx}_{lv['level']}", source="manual")

            if proximate:
                st.markdown(
                    '<div style="font-family:Geist,sans-serif;font-size:var(--fs-xs);'
                    'font-weight:700;letter-spacing: var(--ls-caps-lg);text-transform:uppercase;'
                    'color:var(--color-muted);margin:10px 0 6px;">Nearby auto levels — within 15%</div>',
                    unsafe_allow_html=True,
                )
                for idx, lv in enumerate(proximate[:5]):
                    _render_level_row(lv, f"hide_near_lvl_{ticker}_{idx}_{lv['level']}", source="auto")
            elif auto_lvls:
                st.markdown(
                    '<div style="font-size:var(--fs-sm);color:var(--color-muted);font-style:italic;'
                    'margin:4px 0 8px;">No tested levels within 15% of current price '
                    '— price has run away from established support/resistance zones.</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<div style="font-size:var(--fs-sm);color:var(--color-faintest);font-style:italic;'
                    'margin:4px 0 8px;">No significant clusters in price history yet.</div>',
                    unsafe_allow_html=True,
                )

            # Distant levels — show inline, not nested expander (Streamlit forbids nested expanders)
            if distant:
                st.markdown(
                    '<div style="font-size:var(--fs-xs);color:var(--color-faint);'
                    'text-transform:uppercase;letter-spacing:0.06em;margin:8px 0 4px;">'
                    f'Other tested levels ({len(distant)})</div>',
                    unsafe_allow_html=True,
                )
                for idx, lv in enumerate(distant[:5]):
                    _render_level_row(lv, f"hide_far_lvl_{ticker}_{idx}_{lv['level']}", source="auto")

            if hidden_lvls:
                if st.button(
                    f"Restore hidden auto levels ({len(hidden_lvls)})",
                    key=f"restore_hidden_levels_{ticker}",
                    use_container_width=True,
                ):
                    st.session_state.store["hidden_levels"][ticker.upper()] = []
                    save_store(st.session_state.store)
                    st.rerun()

            # Manual override section — collapsed by default. Most users
            # never need it; the auto-detection is usually right.
            user_has_levels = (
                bool(user_lvls.get("support")) or bool(user_lvls.get("resistance"))
            )
        with st.expander(
            "Mark a custom level" + (
                f" ({len(user_lvls.get('support',[])) + len(user_lvls.get('resistance',[]))} marked)"
                if user_has_levels else ""
            ),
            expanded=user_has_levels,
        ):
            st.markdown(
                '<div style="font-size:var(--fs-sm);color:var(--color-muted);margin-bottom:10px;">'
                'Manually-marked levels override auto-detection and bypass '
                'quality gating in the support-test trigger.</div>',
                unsafe_allow_html=True,
            )

            mc1, mc2 = st.columns(2)
            with mc1:
                st.markdown(
                    '<div style="font-family:Geist,sans-serif;font-size:var(--fs-sm);'
                    'font-weight:600;color:var(--color-accent);margin-bottom:4px;">Support</div>',
                    unsafe_allow_html=True,
                )
                for i, lv_price in enumerate(list(user_lvls.get("support", []))):
                    sub_c1, sub_c2 = st.columns([3, 1])
                    sub_c1.markdown(
                        f"<div style='font-family:\"Geist Mono\",monospace;font-size:var(--fs-base);"
                        f"color:var(--color-accent);padding-top:7px;'>${float(lv_price):,.2f}</div>",
                        unsafe_allow_html=True,
                    )
                    if sub_c2.button("✕", key=f"rm_sup_{ticker}_{i}"):
                        user_lvls["support"].pop(i)
                        st.session_state.store["manual_levels"][ticker.upper()] = user_lvls
                        save_store(st.session_state.store)
                        st.rerun()
                new_support = st.text_input(
                    "Add support",
                    key=f"new_sup_{ticker}",
                    placeholder="e.g. 145",
                    label_visibility="collapsed",
                )
                if st.button("Add", key=f"btn_sup_{ticker}", use_container_width=True):
                    try:
                        v = float(new_support)
                        if v > 0:
                            user_lvls.setdefault("support", []).append(round(v, 2))
                            user_lvls["support"] = sorted(set(user_lvls["support"]), reverse=True)
                            st.session_state.store["manual_levels"][ticker.upper()] = user_lvls
                            save_store(st.session_state.store)
                            st.rerun()
                    except (ValueError, TypeError):
                        st.warning("Enter a positive number.")

            with mc2:
                st.markdown(
                    '<div style="font-family:Geist,sans-serif;font-size:var(--fs-sm);'
                    'font-weight:600;color:var(--color-negative);margin-bottom:4px;">Resistance</div>',
                    unsafe_allow_html=True,
                )
                for i, lv_price in enumerate(list(user_lvls.get("resistance", []))):
                    sub_c1, sub_c2 = st.columns([3, 1])
                    sub_c1.markdown(
                        f"<div style='font-family:\"Geist Mono\",monospace;font-size:var(--fs-base);"
                        f"color:var(--color-negative);padding-top:7px;'>${float(lv_price):,.2f}</div>",
                        unsafe_allow_html=True,
                    )
                    if sub_c2.button("✕", key=f"rm_res_{ticker}_{i}"):
                        user_lvls["resistance"].pop(i)
                        st.session_state.store["manual_levels"][ticker.upper()] = user_lvls
                        save_store(st.session_state.store)
                        st.rerun()
                new_res = st.text_input(
                    "Add resistance",
                    key=f"new_res_{ticker}",
                    placeholder="e.g. 213",
                    label_visibility="collapsed",
                )
                if st.button("Add", key=f"btn_res_{ticker}", use_container_width=True):
                    try:
                        v = float(new_res)
                        if v > 0:
                            user_lvls.setdefault("resistance", []).append(round(v, 2))
                            user_lvls["resistance"] = sorted(set(user_lvls["resistance"]))
                            st.session_state.store["manual_levels"][ticker.upper()] = user_lvls
                            save_store(st.session_state.store)
                            st.rerun()
                    except (ValueError, TypeError):
                        st.warning("Enter a positive number.")

        # ───── RIGHT COLUMN: PM view (two layers) ─────
    with col_pm:
        # PM header + refresh button
        st.markdown(f"""
<div class="desk-pm-header">
  <div>
    <div><span class="em">🧠</span>Portfolio manager</div>
  </div>
</div>
""", unsafe_allow_html=True)
        st.markdown(
            '<div class="desk-pm-utility">'
            '<div class="desk-pm-utility-label">Data / actions</div>',
            unsafe_allow_html=True,
        )
        st.markdown(freshness_panel_html, unsafe_allow_html=True)
        if st.button(
            f"↻ Refresh {ticker.upper()}",
            key=f"refresh_current_everything_{ticker.upper()}",
            help="Refresh price, fundamentals, sidebar row, PM thesis, quality box, drivers, risks, valuation, and decision dossier.",
            use_container_width=True,
        ):
            refresh_current_ticker_state(ticker, refresh_research=True)
            st.rerun()
        st.markdown(
            f'<a class="research-link" href="?report={html.escape(ticker.upper())}" '
            f'target="_blank" rel="noopener">✨ Full research report ↗</a>',
            unsafe_allow_html=True,
        )
        st.markdown('</div>', unsafe_allow_html=True)

        # Quality tier badge — informational, NOT a gate. Sourced from the
        # dossier Claude call (5th field). Shows long-term ownership read
        # alongside the tactical action: e.g. "Avoid · Quality A" means the
        # chart is broken right now but it's a name worth owning at a
        # better entry. Hidden when no quality data (no API key or pre-
        # cache miss).
        quality = (dossier_result or {}).get("quality") or {}
        if not (quality.get("tier") or "").strip():
            pm_quality = (pm.get("quality") or {}) if isinstance(pm, dict) else {}
            if isinstance(pm_quality, dict) and (pm_quality.get("tier") or "").strip():
                quality = pm_quality
        if not (quality.get("tier") or "").strip():
            snapshot_quality = ((ticker_snapshot(ticker).get("pm") or {}).get("quality") or "").strip()
            if snapshot_quality:
                quality = {
                    "tier": snapshot_quality,
                    "rationale": (
                        (pm.get("thesis") or "").strip()
                        if isinstance(pm, dict) else ""
                    ),
                }
        if not (quality.get("tier") or "").strip():
            quality = inferred_quality_from_pm(pm, t)
        q_tier = (quality.get("tier") or "").strip()
        q_rationale = (quality.get("rationale") or "").strip()
        if q_tier:
            tier_styles = {
                "A":           {"color": "#00A870", "bg": "#E8F5EF",
                                "label": "Quality A",
                                "sub": "Durable category leader · core position candidate"},
                "B":           {"color": "#5B6B7D", "bg": "#EEF1F4",
                                "label": "Quality B",
                                "sub": "Real moat with timing or execution risk · tactical + selective"},
                "Speculative": {"color": "#9B5DE5", "bg": "#F4ECFB",
                                "label": "Speculative",
                                "sub": "Real upside with binary risk · size accordingly"},
                "Avoid":       {"color": "#D14545", "bg": "#FCEBEB",
                                "label": "Quality Avoid",
                                "sub": "Structurally weak business · do not engage"},
            }
            ts = tier_styles.get(q_tier, tier_styles["B"])
            quality_help = (
                "Quality A — durable category leader, core position candidate. "
                "Quality B — real moat but timing/execution risk, selective sizing. "
                "Speculative — real upside with binary risk, size small. "
                "Quality Avoid — structurally weak business, do not engage."
            )
            rationale_html = f'<div style="font-size:var(--fs-sm); color:#4A453E; margin-top:4px; line-height:1.4;">{q_rationale}</div>' if q_rationale else ""
            st.markdown(f"""
<div class="desk-quality-card" style="background:{ts['bg']}; border-left:3px solid {ts['color']};
            padding:8px 12px; border-radius:4px;">
  <div style="font-family: var(--font-mono); font-size:var(--fs-sm); font-weight:600;
              letter-spacing: var(--ls-caps-xs); text-transform:uppercase; color:{ts['color']};">
    {ts['label']}<span class="desk-quality-info" title="{quality_help}">i</span>
  </div>
  <div style="font-size:var(--fs-sm); color:var(--color-muted); margin-top:2px;">{ts['sub']}</div>
  {rationale_html}
</div>
""", unsafe_allow_html=True)
        else:
            st.markdown("""
<div class="desk-quality-card" style="background:#F8FAFC; border-left:3px solid #94A3B8;
            padding:8px 12px; border-radius:4px;">
  <div style="font-family: var(--font-mono); font-size:var(--fs-sm); font-weight:600;
              letter-spacing: var(--ls-caps-xs); text-transform:uppercase; color:#64748B;">
    Quality pending<span class="desk-quality-info" title="Quality appears after PM research is generated for this ticker. If refresh cannot reach Claude, the app keeps any prior research instead of replacing it with a blank memo.">i</span>
  </div>
  <div style="font-size:var(--fs-sm); color:var(--color-muted); margin-top:2px;">
    PM research has not produced a long-term quality tier for this ticker yet.
  </div>
</div>
""", unsafe_allow_html=True)

        # Layer 1 — snapshot
        st.markdown(f"""
<div class="desk-pm-memo">
  <div class="desk-pm-block">
    <div class="lb">🧭 Thesis</div>
    <div class="body">{pm.get('thesis', '')}</div>
  </div>
  <div class="desk-pm-block">
    <div class="lb">🚀 Drivers</div>
    {''.join(f'<div class="desk-pm-item">{d}</div>' for d in pm.get('drivers', []))}
  </div>
  <div class="desk-pm-block">
    <div class="lb">⚠️ Risks</div>
    {''.join(f'<div class="desk-pm-item">{r}</div>' for r in pm.get('risks', []))}
  </div>
  <div class="desk-pm-block">
    <div class="lb">💵 Valuation</div>
    <div class="body">{pm.get('valuation', '')}</div>
  </div>
</div>
""", unsafe_allow_html=True)

        # ── Earnings card (if a date is known) ──
        if meta.get("earnings_date"):
            eps = meta.get("expected_eps")
            days = meta.get("earnings_days")
            date_str = format_earnings_date_label(meta.get("earnings_date"))
            # Only show if upcoming OR reported within the last 45 days
            if days is not None and days >= -45:
                if days < 0:
                    label = "📅 Last earnings"
                    days_str = f"Reported {abs(days)} day{'s' if abs(days) != 1 else ''} ago"
                    eps_str = f"Expected EPS ${eps:,.2f}" if eps else ""
                else:
                    label = "📅 Next earnings"
                    days_str = "Today" if days == 0 else f"In {days} day{'s' if days != 1 else ''}"
                    eps_str = f"Expected EPS ${eps:,.2f}" if eps else "EPS estimate not available"
                st.markdown(f"""
<div class="desk-stat-card">
  <div class="label">{label}</div>
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
                target_html = f'<span class="v">${target:,.2f} <span style="color:{color};">({"+" if pct >= 0 else ""}{pct:.1f}%)</span></span>'
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
                return '<span style="color:var(--color-faintest);">—</span>'
            return '<span style="color:var(--color-positive);">✓</span>' if is_pass else '<span style="color:var(--color-negative);">✗</span>'

        unavailable = '<span class="v" style="color:var(--color-fainter);">— unavailable</span>'
        is_fund_meta = (
            str(meta.get("quote_type") or "").upper() in {"ETF", "MUTUALFUND", "FUND"} or
            str(meta.get("sector") or "").upper() == "ETF" or
            bool(meta.get("category") and str(meta.get("sector") or "").upper() == "ETF")
        )

        peg_verdict = ""
        peg_row = f'<div class="row"><span>PEG</span>{unavailable}</div>'
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
            peg_row = f'<div class="row"><span>PEG</span><span class="v"><span class="num">{peg:.2f}</span> · {peg_verdict} {pass_fail(peg_pass)}</span></div>'

        pe_row = f'<div class="row"><span>Forward P/E</span>{unavailable}</div>'
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
            pe_row = f'<div class="row"><span>Forward P/E</span><span class="v"><span class="num">{fpe:.1f}</span> · {pe_context} {pass_fail(pe_pass)}</span></div>'

        de_row = f'<div class="row"><span>Debt / Equity</span>{unavailable}</div>'
        if de is not None:
            de_pass = de < 30
            de_note = "healthy" if de_pass else "leveraged"
            de_row = f'<div class="row"><span>Debt / Equity</span><span class="v"><span class="num">{de:.0f}%</span> · {de_note} {pass_fail(de_pass)}</span></div>'

        growth_row = f'<div class="row"><span>Earnings growth</span>{unavailable}</div>'
        if eg is not None:
            growth_row = f'<div class="row"><span>Earnings growth</span><span class="v"><span class="num">{eg:+.1f}%</span> YoY</span></div>'

        if is_fund_meta:
            st.markdown(f"""
<div class="desk-stat-card">
  <div class="label">📚 Lynch check</div>
  <div class="row"><span>Category</span><span class="v">ETF / fund</span></div>
  <div class="row"><span>Framework</span><span class="v">Not applicable to fund-level holdings</span></div>
</div>
""", unsafe_allow_html=True)
        else:
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

        # Investment thesis is visible by default. The extra structured
        # deep-dive stays behind one compact button so the PM panel remains
        # readable on first load.
        pm_narrative = dossier_result.get("pm_narrative") if dossier_result else None
        deep = pm.get("deep_dive") or {}
        has_deep = any(
            deep.get(key)
            for key in (
                "expanded_thesis",
                "business",
                "variant_bull",
                "variant_bear",
                "variant_needs",
                "catalysts",
                "risk_scenarios",
                "valuation_context",
                "must_be_true",
                "would_change_mind",
            )
        )

        if pm_narrative:
            paragraphs = [p.strip() for p in pm_narrative.split("\n\n") if p.strip()]
            narrative_html = "".join(f"<p>{p}</p>" for p in paragraphs)
            st.markdown(
                f'<div class="desk-pm-thesis">'
                f'<div class="desk-pm-block"><div class="lb">🧠 Investment thesis</div></div>'
                f'{narrative_html}'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            fallback_thesis = pm.get("thesis") or "Investment thesis appears here after the PM dossier is generated."
            st.markdown(
                f'<div class="desk-pm-thesis">'
                f'<div class="desk-pm-block"><div class="lb">🧠 Investment thesis</div></div>'
                f'<p>{fallback_thesis}</p>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # Track expanded deep-dive state per ticker.
        ticker_key = ticker.upper()
        expanded = st.session_state.pm_expanded.get(ticker_key, False)

        if has_deep:
            btn_label = "Hide expanded thesis ↑" if expanded else "Expanded thesis ↓"
            if st.button(btn_label, key=f"pm_expand_{ticker_key}", use_container_width=False):
                st.session_state.pm_expanded[ticker_key] = not expanded
                st.rerun()
        elif expanded:
            st.session_state.pm_expanded[ticker_key] = False

        if expanded and has_deep:
            # Structured deep-dive: variant perception, catalysts, risks,
            # what-must-be-true, what-would-change-my-mind.
            html_parts = ['<div class="desk-pm-deep">']
            if deep.get("expanded_thesis"):
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


# ─────────────────────────────────────────────────────────────────────
# IDEAS — thematic discovery
# ─────────────────────────────────────────────────────────────────────
DEFAULT_DISCOVERY_UNIVERSE = (
    "AAPL, ABNB, AMZN, ANF, BIRK, BRZE, BROS, CAVA, CELH, CMG, COIN, CROX, "
    "DASH, DECK, DUOL, ELF, ETSY, HIMS, HOOD, LULU, META, NFLX, NKE, ONON, "
    "PINS, PLNT, RBLX, RDDT, RVLV, SE, SHOP, SOFI, SPOT, SQ, TOST, UBER, "
    "ULTA, VFC, WING, ZM"
)

DEFAULT_DISCOVERY_STARTER_CANDIDATES = [
    {"ticker": "DASH", "company": "DoorDash", "score": 82, "theme_fit": "Gen-Z/mobile ordering, local commerce, and marketplace habit formation.", "_sector": "Consumer Cyclical", "_industry": "Internet content & information"},
    {"ticker": "SPOT", "company": "Spotify", "score": 80, "theme_fit": "Streaming, creator economy, podcasts, and music discovery tied to younger consumers.", "_sector": "Communication Services", "_industry": "Entertainment"},
    {"ticker": "RBLX", "company": "Roblox", "score": 78, "theme_fit": "Gaming, virtual worlds, and younger-user social entertainment.", "_sector": "Communication Services", "_industry": "Electronic gaming"},
    {"ticker": "DUOL", "company": "Duolingo", "score": 76, "theme_fit": "Mobile-first education app with strong consumer engagement and brand affinity.", "_sector": "Technology", "_industry": "Software"},
    {"ticker": "ELF", "company": "e.l.f. Beauty", "score": 75, "theme_fit": "Value beauty, TikTok-native demand creation, and younger consumer share gains.", "_sector": "Consumer Defensive", "_industry": "Household & personal products"},
    {"ticker": "HOOD", "company": "Robinhood", "score": 73, "theme_fit": "Retail investing, crypto access, and younger financial-services adoption.", "_sector": "Financial Services", "_industry": "Capital markets"},
    {"ticker": "RDDT", "company": "Reddit", "score": 72, "theme_fit": "Online communities, interest graphs, and AI/search-adjacent consumer behavior.", "_sector": "Communication Services", "_industry": "Internet content & information"},
    {"ticker": "SHOP", "company": "Shopify", "score": 70, "theme_fit": "Creator commerce, DTC brands, and merchant infrastructure for consumer trends.", "_sector": "Technology", "_industry": "Software"},
]


def _extract_json_object(text):
    """Best-effort JSON extraction for Claude responses."""
    if not text:
        return {}
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return {}
    return {}


def generate_theme_discovery(query, universe_text, api_key):
    """Ask Claude to turn a natural-language theme into researched candidates."""
    if not api_key:
        raise ValueError("Anthropic API key is required for idea discovery.")
    try:
        from anthropic import Anthropic
    except Exception as exc:
        raise ValueError(f"Anthropic client unavailable: {exc}") from exc

    cleaned_query = str(query or "").strip()
    if len(cleaned_query) < 8:
        raise ValueError("Write a little more about the theme you want.")

    universe = [t.strip().upper() for t in str(universe_text or "").replace("\n", ",").split(",") if t.strip()]
    universe = list(dict.fromkeys(universe))[:80]
    universe_block = ", ".join(universe) if universe else "No explicit universe supplied; use liquid US-listed names."

    prompt = f"""
You are an equity idea discovery analyst inside a trading workstation.

User request:
{cleaned_query}

Candidate universe:
{universe_block}

Find US-listed stocks or ETFs that plausibly match the request. This is NOT a buy list.
Return candidates with evidence, caveats, and what the app should verify next.

Research checklist:
- Translate the user's plain-English theme into 4-6 structured criteria.
- Look for data that may not appear in financial statements: customer demographic, product adoption, brand resonance, hidden assets, private-company stakes, strategic partnerships, regulatory catalysts, and cultural/consumer behavior shifts.
- For financial constraints, prefer evidence like revenue growth, gross margin, debt/equity, net cash, FCF, market cap, valuation, or balance-sheet commentary.
- Penalize weak evidence. If a company only loosely matches the theme, say so.
- Avoid hallucinating. If a metric is unknown, mark it unknown rather than inventing it.
- Do not recommend more than 12 candidates.

Return ONLY JSON:
{{
  "criteria": ["criterion", "..."],
  "summary": "one concise paragraph on the opportunity set",
  "candidates": [
    {{
      "ticker": "TICKER",
      "company": "Company name",
      "score": 1-100,
      "theme_fit": "1 sentence",
      "financial_fit": "1 sentence",
      "why_it_matters": "1-2 sentences",
      "risks": "1 sentence",
      "evidence": ["specific evidence item", "specific evidence item"],
      "verify_next": ["metric/fact to verify", "metric/fact to verify"]
    }}
  ]
}}
"""
    client = Anthropic(api_key=api_key)
    tools = [{"type": "web_search_20250305", "name": "web_search"}]
    messages = [{"role": "user", "content": prompt}]
    final_text = ""

    for _ in range(6):
        try:
            response = anthropic_messages_create(
                client,
                max_tokens=3500,
                tools=tools,
                messages=messages,
                betas=["web-search-2025-03-05"],
            )
        except TypeError as err:
            if "betas" not in str(err):
                raise
            response = anthropic_messages_create(
                client,
                max_tokens=3500,
                messages=messages,
            )
        text_parts = [b.text for b in response.content if hasattr(b, "text") and b.text]
        final_text = "\n".join(text_parts).strip() or final_text
        if response.stop_reason == "end_turn":
            break
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if getattr(block, "type", "") == "tool_use":
                    content = getattr(block, "content", "") or ""
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") if isinstance(c, dict) else str(c)
                            for c in content
                        )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(content),
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            break

    parsed = _extract_json_object(final_text)
    candidates = parsed.get("candidates") if isinstance(parsed, dict) else []
    if not isinstance(candidates, list):
        candidates = []
    parsed["candidates"] = [
        c for c in candidates
        if isinstance(c, dict) and str(c.get("ticker", "")).strip()
    ][:12]
    if not parsed["candidates"]:
        raise ValueError("Claude did not return usable candidates. Try a narrower prompt or candidate universe.")
    return parsed


def enrich_discovery_candidate(candidate, bench):
    ticker = str(candidate.get("ticker", "")).upper().strip()
    if not ticker:
        return {**candidate, "_action": "—", "_price": None}
    hist, name, _err = fetch_history(ticker)
    meta = fetch_quote_meta(ticker)
    fallback_profile = get_ticker_profile(ticker)
    enriched = {**candidate}
    enriched["_name"] = name or meta.get("long_name") or meta.get("short_name") or candidate.get("company") or ticker
    enriched["_market_cap"] = format_market_cap(meta.get("market_cap"))
    enriched["_sector"] = meta.get("sector") or fallback_profile.get("sector") or "—"
    enriched["_industry"] = (
        meta.get("industry")
        or meta.get("category")
        or fallback_profile.get("industry")
        or fallback_profile.get("category")
        or "—"
    )
    enriched["_revenue_growth"] = format_plain_pct(meta.get("revenue_growth"), digits=1)
    enriched["_debt_equity"] = format_plain_pct(meta.get("debt_to_equity"), digits=0)
    enriched["_earnings_days"] = meta.get("earnings_days")
    if hist is not None and len(hist) >= 2 and bench is not None:
        try:
            t_state = tactical.compute(hist, bench)
            if t_state:
                t_state = apply_earnings_event_gate(t_state, meta.get("earnings_days") if meta else None)
                enriched["_price"] = t_state.get("price")
                enriched["_change"] = t_state.get("change")
                enriched["_action"] = t_state.get("action")
                enriched["_state"] = t_state.get("state")
                enriched["_rs"] = t_state.get("rs")
                enriched["_score"] = t_state.get("setup_score")
            else:
                enriched["_action"] = None
        except Exception:
            enriched["_action"] = None
    else:
        enriched["_action"] = None
    return enriched


def cached_discovery_candidate(candidate):
    """Fast Ideas row using saved snapshots only; never hits Yahoo/Claude."""
    ticker = str(candidate.get("ticker", "")).upper().strip()
    if not ticker:
        return {**candidate, "_action": None, "_price": None}
    canonical = ticker_snapshot(ticker)
    market = canonical.get("market") or {}
    meta = canonical.get("meta") or cached_quote_meta_snapshot(ticker)
    fallback_profile = get_ticker_profile(ticker)
    enriched = {**candidate}
    enriched["_name"] = (
        meta.get("long_name")
        or meta.get("short_name")
        or candidate.get("company")
        or ticker
    )
    enriched["_market_cap"] = format_market_cap(meta.get("market_cap"))
    enriched["_sector"] = meta.get("sector") or fallback_profile.get("sector") or "—"
    enriched["_industry"] = (
        meta.get("industry")
        or meta.get("category")
        or fallback_profile.get("industry")
        or fallback_profile.get("category")
        or "—"
    )
    enriched["_revenue_growth"] = format_plain_pct(meta.get("revenue_growth"), digits=1)
    enriched["_debt_equity"] = format_plain_pct(meta.get("debt_to_equity"), digits=0)
    enriched["_earnings_days"] = meta.get("earnings_days")
    enriched["_price"] = market.get("last")
    enriched["_change"] = market.get("change_pct")
    enriched["_action"] = normalize_action_key(market.get("action"))
    enriched["_state"] = market.get("state")
    enriched["_rs"] = market.get("rs")
    enriched["_cached_metrics"] = True
    return enriched


if view == "regime":
    st.markdown(
        """
        <style>
        .regime-shell{max-width:1480px;margin:0 auto;padding:18px 10px 56px}
        .regime-top{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(340px,.8fr);gap:28px;align-items:start}
        .regime-label{font-family:var(--font-mono);font-size:var(--fs-xs);font-weight:850;letter-spacing:var(--ls-caps-xl);text-transform:uppercase;color:var(--color-muted)}
        .regime-action{font-size:clamp(48px,7vw,96px);line-height:.95;font-weight:900;letter-spacing:0;margin:28px 0 16px}
        .regime-sub{font-size:clamp(19px,2vw,30px);line-height:1.25;color:var(--color-text);max-width:980px;margin:0 0 18px}
        .regime-meta{font-family:var(--font-mono);font-size:var(--fs-xs);font-weight:850;letter-spacing:var(--ls-caps-lg);text-transform:uppercase}
        .regime-divider{height:1px;background:var(--color-border);margin:30px 0}
        .regime-panel{border:1px solid #CCD6E3;border-radius:8px;background:var(--color-surface);box-shadow:none;overflow:hidden}
        .regime-pad{padding:24px}
        .regime-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
        .regime-two{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
        .regime-metric{border:1px solid #CCD6E3;border-radius:7px;background:var(--color-surface);padding:17px 16px 16px}
        .regime-metric .k,.regime-section-title{display:block;font-family:var(--font-mono);font-size:13px;font-weight:850;letter-spacing:var(--ls-caps-lg);text-transform:uppercase;color:var(--color-muted);margin-bottom:10px}
        .regime-metric .v{display:block;font-family:var(--font-mono);font-size:28px;font-weight:900;color:var(--color-text)}
        .regime-metric .s{display:block;margin-top:8px;font-size:14px;color:var(--color-muted);line-height:1.45}
        .regime-table{width:100%;border-collapse:collapse}
        .regime-table td{border-top:1px solid var(--color-border);padding:14px 16px;vertical-align:top}
        .regime-table tr:first-child td{border-top:0}
        .regime-table .t{font-family:var(--font-mono);font-size:var(--fs-xs);font-weight:850;letter-spacing:var(--ls-caps-lg);text-transform:uppercase;width:170px}
        .regime-table .r{font-size:var(--fs-md);line-height:1.35;color:var(--color-text)}
        .regime-chip{display:inline-block;border:1px solid var(--color-border);border-radius:5px;background:var(--color-surface);padding:7px 9px;font-family:var(--font-mono);font-size:var(--fs-xs);font-weight:850;letter-spacing:.04em;color:var(--color-muted);margin:0 6px 6px 0}
        .regime-watch{border-left:3px solid var(--color-border);padding:10px 0 10px 14px;margin:0 0 12px;color:var(--color-text);line-height:1.35}
        .regime-watch strong{font-family:var(--font-mono);font-size:var(--fs-xs);letter-spacing:var(--ls-caps-lg);text-transform:uppercase}
        .regime-layer{border:1px solid #CCD6E3;border-radius:7px;background:var(--color-surface);padding:14px}
        .regime-layer .n{font-family:var(--font-mono);font-size:var(--fs-xs);font-weight:850;letter-spacing:var(--ls-caps-lg);text-transform:uppercase;color:var(--color-muted)}
        .regime-layer .v{display:block;margin-top:8px;font-size:26px;font-weight:850}
        .regime-bullet{display:flex;gap:9px;padding:6px 0;border-top:1px dashed rgba(148,163,184,.26);line-height:1.35}
        .regime-bullet:first-child{border-top:0}
        .regime-bullet span:first-child{color:var(--color-muted)}
        .regime-brief{display:grid;grid-template-columns:minmax(0,1.12fr) minmax(360px,.88fr);gap:18px;align-items:stretch}
        .regime-implication{display:block;padding:18px 0;border-top:1px solid var(--color-border)}
        .regime-implication:first-of-type{border-top:0;padding-top:4px}
        .regime-implication .h{font-family:var(--font-mono);font-size:var(--fs-xs);font-weight:850;letter-spacing:var(--ls-caps-lg);text-transform:uppercase;color:var(--color-muted)}
        .regime-implication .b{font-size:19px;line-height:1.48;color:var(--color-text);margin-top:5px}
        .regime-scenario{border-top:1px solid var(--color-border);padding:17px 0}
        .regime-scenario:first-of-type{border-top:0;padding-top:2px}
        .regime-scenario .h{font-family:var(--font-mono);font-size:var(--fs-xs);font-weight:850;letter-spacing:var(--ls-caps-lg);text-transform:uppercase;margin-bottom:7px}
        .regime-scenario .b{font-size:17px;line-height:1.48;color:var(--color-text)}
        .regime-forward-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:14px}
        .regime-forward-row{display:grid;grid-template-columns:34px minmax(0,1fr);gap:12px;align-items:start;padding:16px;border:1px solid #CCD6E3;border-radius:8px;background:var(--color-surface)}
        .regime-forward-row .num{display:flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:6px;background:var(--color-surface-soft);font-family:var(--font-mono);font-size:13px;font-weight:900;color:var(--color-muted)}
        .regime-forward-row .trig{font-size:17px;font-weight:850;color:var(--color-text)}
        .regime-forward-row .why{font-size:15px;line-height:1.45;color:var(--color-muted);margin-top:7px}
        .regime-forward-row .status{display:inline-block;margin-top:8px;border:1px solid var(--color-border);border-radius:5px;padding:4px 7px;font-family:var(--font-mono);font-size:12px;font-weight:850;letter-spacing:.04em;text-transform:uppercase;color:var(--color-muted)}
        .regime-dashboard{border:1px solid #CCD6E3;border-radius:10px;background:var(--color-surface);overflow:hidden;box-shadow:none}
        .regime-dark{background:var(--color-surface);color:var(--color-text);border:1px solid #CCD6E3;border-left:4px solid var(--color-blue);border-radius:8px;padding:24px 28px}
        .regime-dark-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:0;border-bottom:1px solid #D7DFEA;padding-bottom:20px;margin-bottom:20px}
        .regime-dark-cell{padding:0 20px;border-right:1px solid #D7DFEA}
        .regime-dark-cell:first-child{padding-left:0}
        .regime-dark-cell:last-child{border-right:0;padding-right:0}
        .regime-dark .k{font-family:var(--font-mono);font-size:13px;font-weight:850;letter-spacing:var(--ls-caps-lg);text-transform:uppercase;color:var(--color-muted);margin-bottom:8px}
        .regime-dark .v{font-size:30px;font-weight:900;line-height:1.08}
        .regime-dark-bottom{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(320px,.9fr);gap:24px}
        .regime-highlight{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px solid #D7DFEA;font-size:15px;color:var(--color-muted)}
        .regime-highlight strong{font-family:var(--font-mono);font-size:16px;font-weight:900}
        .regime-highlight .chg{margin-left:8px}
        .regime-highlight:last-child{border-bottom:0}
        .regime-signal-cards{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px}
        .regime-signal-card{border:1px solid #CCD6E3;border-radius:8px;background:var(--color-surface);padding:16px;min-height:148px}
        .regime-signal-card .name{font-family:var(--font-mono);font-size:12px;font-weight:850;letter-spacing:var(--ls-caps-lg);text-transform:uppercase;color:var(--color-muted)}
        .regime-signal-card .state{font-size:22px;font-weight:900;margin:8px 0 8px}
        .regime-signal-card .metric-sub{font-size:13px;line-height:1.35;color:var(--color-muted);font-weight:850;margin-bottom:10px}
        .regime-signal-card .copy{font-size:15px;line-height:1.45;color:var(--color-muted)}
        .regime-signal-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0;border:1px solid var(--color-border);border-radius:8px;overflow:hidden;background:var(--color-surface)}
        .regime-signal-box{padding:16px 18px;border-right:1px solid var(--color-border)}
        .regime-signal-box:last-child{border-right:0}
        .regime-signal-box .label{font-family:var(--font-mono);font-size:var(--fs-xs);font-weight:900;letter-spacing:var(--ls-caps-lg);text-transform:uppercase;margin-bottom:5px}
        .regime-signal-box .value{font-family:var(--font-mono);font-size:24px;font-weight:950;margin:8px 0 4px;color:var(--color-text)}
        .regime-signal-box .detail{font-size:var(--fs-sm);line-height:1.4;color:var(--color-muted)}
        .regime-action-box{display:grid;grid-template-columns:210px minmax(0,1fr);gap:20px;background:var(--color-surface);color:var(--color-text);border-radius:8px;padding:18px 22px;border:1px solid #CCD6E3}
        .regime-action-box li{list-style:none;padding:7px 0;border-bottom:1px solid #D7DFEA;line-height:1.45}
        .regime-action-box li:last-child{border-bottom:0}
        .crypto-wrap{margin-top:4px}
        .crypto-header{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
        .crypto-accent{width:3px;height:22px;border-radius:2px;background:var(--color-blue)}
        .crypto-badge{border:1px solid var(--color-border);border-radius:5px;padding:4px 8px;font-family:var(--font-mono);font-size:11px;font-weight:850;letter-spacing:.04em;text-transform:uppercase;background:var(--color-surface);color:var(--color-text)}
        .crypto-price-strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border:1px solid #CCD6E3;border-radius:8px;background:var(--color-surface);margin-bottom:12px;color:var(--color-text);overflow:hidden}
        .crypto-price-cell{min-height:118px;border-right:1px solid #D7DFEA;padding:18px 20px;display:flex;flex-direction:column;justify-content:flex-start;align-items:flex-start;gap:8px}
        .crypto-price-cell:first-child{padding-left:20px}
        .crypto-price-cell:last-child{border-right:0;padding-right:20px}
        .crypto-price-cell .k{font-family:var(--font-mono);font-size:var(--fs-xs);font-weight:850;letter-spacing:var(--ls-caps-lg);text-transform:uppercase;color:var(--color-muted);margin:0}
        .crypto-price-cell .v{font-family:var(--font-mono);font-size:clamp(22px,2.3vw,32px);line-height:1.1;font-weight:900;color:var(--color-text);max-width:100%;overflow-wrap:anywhere}
        .crypto-price-cell.good{background:rgba(22,163,74,.06)}
        .crypto-price-cell.warn{background:rgba(139,98,20,.07)}
        .crypto-price-cell.bad{background:rgba(209,69,69,.06)}
        .crypto-price-cell.neutral{background:var(--color-surface)}
        .crypto-price-cell .sub.good,.crypto-badge.good{color:var(--color-positive);border-color:rgba(22,163,74,.28);background:rgba(22,163,74,.06)}
        .crypto-price-cell .sub.warn,.crypto-badge.warn{color:var(--color-warning-text);border-color:rgba(139,98,20,.28);background:rgba(139,98,20,.07)}
        .crypto-price-cell .sub.bad,.crypto-badge.bad{color:var(--color-negative);border-color:rgba(209,69,69,.28);background:rgba(209,69,69,.06)}
        .crypto-price-cell .sub.neutral,.crypto-badge.neutral{color:var(--color-muted)}
        .crypto-decisions{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:10px}
        .crypto-card{border:1px solid #CCD6E3;border-radius:8px;background:var(--color-surface);padding:18px}
        .crypto-card.good{background:rgba(22,163,74,.06);border-color:rgba(22,163,74,.30)}
        .crypto-card.warn{background:rgba(139,98,20,.07);border-color:rgba(139,98,20,.30)}
        .crypto-card.bad{background:rgba(209,69,69,.06);border-color:rgba(209,69,69,.30)}
        .crypto-card.neutral{background:var(--color-surface)}
        .crypto-card .q{font-family:var(--font-mono);font-size:var(--fs-xs);font-weight:850;letter-spacing:var(--ls-caps-lg);text-transform:uppercase;color:var(--color-muted);margin-bottom:10px}
        .crypto-card .answer{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--color-border);border-radius:6px;padding:7px 10px;font-size:18px;font-weight:850;margin-bottom:10px}
        .crypto-dot{width:8px;height:8px;border-radius:50%;display:inline-block}
        .crypto-tag{display:inline-block;margin:0 5px 6px 0;border:1px solid var(--color-border);border-radius:5px;padding:3px 6px;font-family:var(--font-mono);font-size:11px;color:var(--color-muted);background:#F8FAFC}
        .crypto-note{font-size:15px;line-height:1.45;color:var(--color-muted)}
        .crypto-cycle-note{font-size:15px;font-style:italic;color:var(--color-muted);margin:-4px 0 14px}
        .crypto-cycle-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:14px 0 18px}
        .crypto-phase{position:relative;border:1px solid #CCD6E3;border-radius:8px;background:var(--color-surface);padding:18px;min-height:196px}
        .crypto-phase.good{background:rgba(22,163,74,.06);border-color:rgba(22,163,74,.26)}
        .crypto-phase.warn{background:rgba(139,98,20,.07);border-color:rgba(139,98,20,.28)}
        .crypto-phase.bad{background:rgba(209,69,69,.06);border-color:rgba(209,69,69,.30)}
        .crypto-phase.current{border-width:2px;padding-top:42px}
        .crypto-phase.current:before{content:"◂ YOU ARE HERE";position:absolute;top:0;left:0;right:0;background:var(--color-text);color:#fff;border-radius:5px 5px 0 0;text-align:center;font-family:var(--font-mono);font-size:12px;font-weight:900;letter-spacing:.08em;padding:8px 10px}
        .crypto-phase.current.good:before{background:var(--color-positive)}
        .crypto-phase.current.warn:before{background:var(--color-warning-text)}
        .crypto-phase.current.bad:before{background:var(--color-negative)}
        .crypto-phase .phase{font-family:var(--font-mono);font-size:12px;font-weight:850;color:var(--color-muted);text-transform:uppercase}
        .crypto-phase .name{font-size:18px;font-weight:900;margin:6px 0 10px;color:var(--color-text)}
        .crypto-phase.current .name,.crypto-phase.current .phase{color:var(--color-text)}
        .crypto-phase .desc{font-size:15px;line-height:1.45;color:var(--color-muted);margin-bottom:12px}
        .crypto-phase .meta{font-family:var(--font-mono);font-size:12px;letter-spacing:.04em;text-transform:uppercase;font-weight:850;color:var(--color-muted);margin-top:12px}
        .crypto-phase .meta-text{font-size:14px;line-height:1.4;color:var(--color-muted);margin-top:4px}
        .crypto-narrative{border:1px solid #CCD6E3;border-radius:8px;background:var(--color-surface);padding:0 24px}
        .crypto-narrative-row{padding:20px 0;border-bottom:1px solid var(--color-border)}
        .crypto-narrative-row:last-child{border-bottom:0}
        .crypto-narrative-row .h{font-family:var(--font-mono);font-size:13px;font-weight:900;letter-spacing:var(--ls-caps-lg);text-transform:uppercase;color:var(--color-muted);margin-bottom:8px}
        .crypto-narrative-row .b{font-size:19px;line-height:1.52;color:var(--color-text)}
        .risk-engine-page{width:100%;max-width:none;margin:0;padding:0 0 56px;color:var(--color-text);overflow-x:hidden}
        .risk-engine-title{font-size:32px;line-height:1.05;font-weight:950;color:var(--color-text);margin:4px 0 6px;letter-spacing:0}
        .risk-engine-snapshot{font-size:13px;font-weight:760;color:var(--color-muted);margin-bottom:16px}
        .risk-brief-card{background:var(--color-surface);border:1px solid #CCD6E3;border-radius:10px;padding:0;overflow:hidden;margin-top:16px;box-shadow:none}
        .risk-brief-pad{padding:24px 30px 28px}
        .risk-brief-label{font-family:var(--font-mono);font-size:13px;font-weight:950;letter-spacing:.3em;text-transform:uppercase;color:var(--color-purple);margin-bottom:22px}
        .risk-brief-grid{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid #D7DFEA;margin-bottom:22px}
        .risk-brief-cell{min-height:76px;padding:0 34px 20px 0;border-right:1px solid #D7DFEA}
        .risk-brief-cell:nth-child(2n){border-right:0;padding-left:34px;padding-right:0}
        .risk-brief-cell:nth-child(n+3){border-top:1px solid #D7DFEA;padding-top:20px}
        .risk-opportunity-card{background:var(--color-surface);color:var(--color-text);border:1px solid #CCD6E3;border-left:4px solid var(--color-blue);border-radius:8px;padding:0;margin-top:16px;box-shadow:none;overflow:hidden}
        .risk-op-top{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:0;margin-bottom:20px}
        .risk-op-cell{padding:18px 22px;border-right:1px solid #D7DFEA}
        .risk-op-cell:first-child{padding-left:22px}
        .risk-op-cell:last-child{padding-right:22px;border-right:0}
        .risk-op-label{font-family:var(--font-mono);font-size:12px;font-weight:950;letter-spacing:var(--ls-caps-lg);text-transform:uppercase;color:var(--color-muted);margin-bottom:10px}
        .risk-op-main{font-size:24px;font-weight:950;line-height:1.08;color:var(--color-text)}
        .risk-op-sub{font-size:13px;line-height:1.4;color:var(--color-muted);font-weight:700;margin-top:7px}
        .risk-op-context{display:flex;flex-direction:column;gap:7px;font-size:14px;font-weight:850;line-height:1.3}
        .risk-op-bottom{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(300px,1fr);gap:0;border-top:1px solid #D7DFEA;padding-top:0}
        .risk-op-bottom>div:first-child{padding:20px 22px}
        .risk-op-why{font-size:16px;line-height:1.55;color:var(--color-body);font-weight:560}
        .risk-op-why strong{color:var(--color-text);font-weight:850}
        .risk-op-highlights{border-left:1px solid #D7DFEA;padding:20px 22px}
        .risk-op-highlight-row{display:flex;align-items:baseline;justify-content:space-between;gap:18px;border-bottom:1px solid #D7DFEA;padding:10px 0;font-size:16px;color:var(--color-muted)}
        .risk-op-highlight-row:last-child{border-bottom:0}
        .risk-op-highlight-row strong{font-family:var(--font-mono);font-size:20px;font-weight:950;color:var(--color-text);font-variant-numeric:tabular-nums}
        .risk-market-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 28px;border-bottom:1px solid #D7DFEA;padding-bottom:16px;margin-bottom:16px}
        .risk-market-item{display:flex;align-items:baseline;justify-content:space-between;gap:16px;min-height:34px}
        .risk-market-item .name{font-size:18px;color:var(--color-muted);font-weight:760}
        .risk-market-item strong{font-family:var(--font-mono);font-size:24px;font-weight:950;color:var(--color-text);font-variant-numeric:tabular-nums}
        .risk-market-context{border-top:1px solid #D7DFEA;margin-top:16px;padding-top:18px}
        .risk-k{font-family:var(--font-mono);font-size:12px;font-weight:950;letter-spacing:.26em;text-transform:uppercase;color:var(--color-muted);margin-bottom:10px}
        .risk-v{font-size:clamp(20px,1.75vw,26px);line-height:1.12;font-weight:950;color:var(--color-text);overflow-wrap:normal;word-break:normal}
        .risk-v.good{color:var(--color-positive)}
        .risk-v.warn{color:var(--color-warning-text)}
        .risk-v.bad{color:var(--color-negative)}
        .risk-why{font-size:18px;line-height:1.65;font-weight:560;color:var(--color-body);max-width:1340px}
        .risk-highlights{border-left:1px solid #D7DFEA;padding:20px 22px}
        .risk-highlight-row{display:flex;align-items:baseline;justify-content:space-between;gap:18px;border-bottom:1px solid #D7DFEA;padding:10px 0;font-size:16px;color:var(--color-muted)}
        .risk-highlight-row:last-child{border-bottom:0}
        .risk-highlight-row strong{font-size:20px;font-weight:950;color:var(--color-text);font-variant-numeric:tabular-nums}
        .risk-change{font-size:14px;font-weight:900;margin-left:6px}
        .risk-card{margin-top:18px;border:1px solid #CCD6E3;border-radius:8px;background:var(--color-surface);overflow:hidden;box-shadow:none}
        .risk-card-head{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:16px 20px;background:var(--color-surface);border-bottom:1px solid #D7DFEA}
        .risk-card-title{font-family:var(--font-mono);font-size:13px;font-weight:950;letter-spacing:.24em;text-transform:uppercase;color:var(--color-muted)}
        .risk-card-sub{font-size:14px;color:var(--color-muted);font-weight:780;margin-left:10px;letter-spacing:0;text-transform:none}
        .risk-badge{border:1px solid var(--color-positive);background:rgba(22,163,74,.08);color:var(--color-positive);border-radius:5px;padding:7px 12px;font-family:var(--font-mono);font-size:12px;font-weight:950;letter-spacing:.14em;text-transform:uppercase}
        .forward-watch-body{padding:18px 20px 22px}
        .forward-watch-row{display:grid;grid-template-columns:26px minmax(0,1fr);gap:12px;padding:13px 14px;border-radius:6px;color:var(--color-body);font-size:17px;line-height:1.45}
        .forward-watch-row:nth-child(even){background:var(--color-surface-soft)}
        .forward-watch-row .idx{font-weight:950;color:var(--color-muted);text-align:center}
        .forward-watch-row strong{font-weight:950;color:var(--color-text)}
        .market-imp-body{display:grid;grid-template-columns:minmax(0,1fr) 300px}
        .market-imp-main{padding:22px 24px 26px}
        .market-imp-side{background:var(--color-surface-soft);border-left:1px solid #D7DFEA;padding:22px 24px}
        .market-imp-headline{font-size:19px;line-height:1.45;font-weight:950;color:var(--color-text);margin-bottom:20px}
        .market-imp-bullet{display:flex;align-items:flex-start;gap:10px;border-top:1px solid #D7DFEA;padding:14px 0;font-size:16px;line-height:1.48;color:var(--color-body)}
        .market-imp-bullet:first-of-type{border-top:0}
        .market-imp-dot{width:7px;height:7px;border-radius:50%;background:var(--color-muted);margin-top:9px;flex:0 0 7px}
        .watch-trigger{display:grid;grid-template-columns:18px minmax(0,1fr);gap:10px;padding:13px 0;font-size:15px;line-height:1.5;color:var(--color-body)}
        .watch-trigger .arrow{color:var(--color-muted);font-weight:950}
        .risk-secondary{margin-top:18px}
        .framework-details{margin-top:12px;margin-bottom:0;border:1px solid #CCD6E3;border-radius:8px;background:var(--color-surface);overflow:hidden}
        .framework-details summary{list-style:none;cursor:pointer;padding:17px 20px;border-bottom:1px solid #D7DFEA;font-family:var(--font-mono);font-size:13px;font-weight:950;letter-spacing:.24em;text-transform:uppercase;color:var(--color-muted);display:flex;align-items:center;justify-content:space-between;gap:14px}
        .framework-details summary::-webkit-details-marker{display:none}
        .framework-details summary:after{content:"+";font-family:var(--font-mono);font-size:20px;font-weight:900;color:var(--color-muted)}
        .framework-details[open] summary:after{content:"–"}
        .framework-body{padding:20px}
        .metric-guide-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border:1px solid #CCD6E3;border-radius:8px;overflow:hidden;background:var(--color-surface);margin-bottom:18px}
        .metric-guide-card{padding:20px;border-right:1px solid #D7DFEA;min-height:150px}
        .metric-guide-card:last-child{border-right:0}
        .metric-guide-card.good{background:rgba(22,163,74,.06)}
        .metric-guide-card.warn{background:rgba(139,98,20,.07)}
        .metric-guide-card.info{background:rgba(37,99,235,.05)}
        .metric-guide-card .label{font-family:var(--font-mono);font-size:12px;font-weight:950;letter-spacing:.18em;text-transform:uppercase;color:var(--color-muted);margin-bottom:8px}
        .metric-guide-card .metric-name{font-size:15px;font-weight:850;color:var(--color-body);margin-bottom:12px}
        .metric-guide-card .value{font-family:var(--font-mono);font-size:30px;font-weight:950;color:var(--color-text);margin-bottom:8px}
        .metric-guide-card .note{font-size:14px;line-height:1.45;color:var(--color-muted)}
        .framework-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
        .framework-col{border:1px solid #CCD6E3;border-radius:8px;background:var(--color-surface);padding:16px}
        .framework-col .title{font-family:var(--font-mono);font-size:12px;font-weight:950;letter-spacing:.2em;text-transform:uppercase;color:var(--color-muted);margin-bottom:5px}
        .framework-col .question{font-size:14px;font-style:italic;color:var(--color-muted);margin-bottom:12px}
        .framework-state{border:1px solid #D7DFEA;border-radius:7px;background:var(--color-surface-soft);padding:13px;margin-top:10px}
        .framework-state.good{background:rgba(22,163,74,.06);border-color:rgba(22,163,74,.28)}
        .framework-state.warn{background:rgba(139,98,20,.07);border-color:rgba(139,98,20,.28)}
        .framework-state.bad{background:rgba(209,69,69,.06);border-color:rgba(209,69,69,.28)}
        .framework-state.current{box-shadow:inset 3px 0 0 var(--color-blue)}
        .framework-state .state-title{font-size:16px;font-weight:950;color:var(--color-text);margin-bottom:6px}
        .framework-state .state-copy{font-size:14px;line-height:1.45;color:var(--color-body)}
        .framework-state .action{font-size:14px;line-height:1.45;color:var(--color-muted);font-weight:850;margin-top:7px}
        .framework-note{margin-top:12px;border:1px solid #D7DFEA;border-radius:7px;background:var(--color-surface-soft);padding:12px 14px;font-size:14px;line-height:1.45;color:var(--color-muted)}
        .regime-framework-break{border-top:1px solid #CBD5E1;margin:28px 0 42px}
        @media(max-width:1280px){.risk-engine-title{font-size:30px}.risk-v{font-size:clamp(20px,2.7vw,26px)}.risk-why{max-width:none;font-size:19px;line-height:1.65}.risk-op-top{grid-template-columns:repeat(2,minmax(0,1fr))}.risk-op-cell:nth-child(2){border-right:0;padding-right:0}.risk-op-cell:nth-child(3),.risk-op-cell:nth-child(4){border-top:1px solid rgba(148,163,184,.24);padding-top:18px;margin-top:18px}.risk-op-bottom{grid-template-columns:1fr}.risk-op-highlights{border-left:0;padding-left:0}.market-imp-body{grid-template-columns:1fr}.market-imp-side{border-left:0;border-top:1px solid #D7DFEA}}
        @media(max-width:900px){.regime-top,.regime-two{grid-template-columns:1fr}.regime-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.regime-action{font-size:54px}}
        @media(max-width:1100px){.regime-signal-cards{grid-template-columns:repeat(2,minmax(0,1fr))}.regime-forward-list{grid-template-columns:1fr}.framework-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
        @media(max-width:900px){.regime-brief,.regime-dark-bottom,.regime-action-box,.risk-brief-grid,.risk-market-grid,.risk-op-top{grid-template-columns:1fr}.risk-brief-pad{padding:20px}.risk-brief-cell,.risk-brief-cell:nth-child(2n){min-height:auto;border-right:0;border-top:1px solid #D7DFEA;padding:18px 0}.risk-brief-cell:first-child{border-top:0;padding-top:0}.risk-opportunity-card{padding:20px}.risk-op-cell,.risk-op-cell:nth-child(2),.risk-op-cell:nth-child(3),.risk-op-cell:nth-child(4){border-right:0;border-top:1px solid rgba(148,163,184,.24);padding:18px 0 0;margin-top:18px}.risk-op-cell:first-child{border-top:0;padding-top:0;margin-top:0}.regime-dark-grid,.regime-signal-grid,.crypto-price-strip,.crypto-decisions,.crypto-cycle-grid,.market-imp-body,.metric-guide-grid{grid-template-columns:1fr}.regime-dark-cell,.regime-signal-box,.crypto-price-cell,.metric-guide-card{border-right:0;border-bottom:1px solid rgba(148,163,184,.26);padding:12px 0}.metric-guide-card{padding:16px}.risk-highlights{border-left:0;padding-left:0}.market-imp-side{border-left:0;border-top:1px solid var(--color-border)}.regime-forward-row .why{grid-column:2}}
        @media(max-width:560px){.regime-grid,.framework-grid{grid-template-columns:1fr}.regime-table .t{width:110px}.regime-table td{padding:12px 10px}.regime-implication{grid-template-columns:1fr;gap:6px}.regime-forward-row{grid-template-columns:1fr;gap:4px}.regime-forward-row .why{grid-column:auto}}
        </style>
        """,
        unsafe_allow_html=True,
    )

    def _fmt_regime(value, suffix="", digits=1):
        try:
            if value is None or (isinstance(value, float) and math.isnan(value)):
                return "—"
            return f"{float(value):,.{digits}f}{suffix}"
        except (TypeError, ValueError):
            return "—"

    def _signed_regime(value, suffix="%", digits=1):
        try:
            if value is None or (isinstance(value, float) and math.isnan(value)):
                return "—"
            return f"{float(value):+,.{digits}f}{suffix}"
        except (TypeError, ValueError):
            return "—"

    def _regime_value_color(value, inverse=False):
        try:
            if value is None or (isinstance(value, float) and math.isnan(value)):
                return "var(--color-muted)"
            value = float(value)
            if inverse:
                value = -value
            if value > 0:
                return "var(--color-positive)"
            if value < 0:
                return "var(--color-negative)"
        except (TypeError, ValueError):
            pass
        return "var(--color-muted)"

    def _condition_color(label):
        label = str(label or "").lower()
        if any(word in label for word in ("constructive", "healthy", "momentum", "oversold", "acceptable", "add weakness", "favorable", "enter")):
            return "var(--color-positive)"
        if any(word in label for word in ("extended", "choppy", "fragile", "wait", "mixed", "hold off", "pullback", "neutral")):
            return "var(--color-warning-text)"
        if any(word in label for word in ("risk", "broken", "stress", "unfavorable", "avoid")):
            return "var(--color-negative)"
        return "var(--color-muted)"

    def _fear_greed_color(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "var(--color-muted)"
        if value < 25:
            return "var(--color-negative)"
        if value < 45:
            return "var(--color-warning-text)"
        if value <= 70:
            return "var(--color-positive)"
        return "var(--color-warning-text)"

    @st.cache_data(ttl=60 * 60, show_spinner=False)
    def _fred_rows(series_id, limit=24):
        try:
            import pandas as pd
            url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={urllib.parse.quote(series_id)}"
            df = pd.read_csv(url).tail(limit)
            df[series_id] = pd.to_numeric(df[series_id], errors="coerce")
            df = df.dropna(subset=[series_id])
            return [{"date": str(r["observation_date"]), "value": float(r[series_id])} for _, r in df.iterrows()]
        except Exception:
            return []

    @st.cache_data(ttl=15 * 60, show_spinner=False)
    def _quote(symbol, period="70d"):
        try:
            hist = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=True)
            closes = hist["Close"].dropna() if hist is not None and "Close" in hist else []
            if len(closes) < 2:
                return {"last": None, "change": None}
            last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
            ma20 = float(closes.tail(20).mean()) if len(closes) >= 20 else None
            ma50 = float(closes.tail(50).mean()) if len(closes) >= 50 else None
            ret5 = (last / float(closes.iloc[-6]) - 1) * 100 if len(closes) >= 6 and float(closes.iloc[-6]) else None
            ret20 = (last / float(closes.iloc[-21]) - 1) * 100 if len(closes) >= 21 and float(closes.iloc[-21]) else None
            peak5 = float(closes.tail(5).max()) if len(closes) >= 5 else None
            drop5 = (peak5 / last - 1) * 100 if peak5 and last else None
            return {
                "last": last,
                "change": (last / prev - 1) * 100 if prev else None,
                "ma20": ma20,
                "ma50": ma50,
                "vs20": (last / ma20 - 1) * 100 if ma20 else None,
                "vs50": (last / ma50 - 1) * 100 if ma50 else None,
                "ret5": ret5,
                "ret20": ret20,
                "peak5": peak5,
                "drop5": drop5,
            }
        except Exception:
            return {"last": None, "change": None}

    @st.cache_data(ttl=60 * 60, show_spinner=False)
    def _fear_greed():
        try:
            req = urllib.request.Request("https://api.alternative.me/fng/", headers={"User-Agent": "TradingDesk/1.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            item = (payload.get("data") or [{}])[0]
            return {"value": int(item.get("value")), "label": item.get("value_classification") or "—"}
        except Exception:
            return {"value": None, "label": "—"}

    def _score_regime(d):
        cfg = {"ism_warn": 50, "t2_trigger": 4.2, "t2_approach": 4.0, "hy_alert": 450, "hy_fire": 600}
        s = {}
        ism = d.get("ism")
        unemp = d.get("unemp")
        unemp_prev = d.get("unemp_prev")
        hy_bps = d.get("hy_bps")
        yc_bps = d.get("yc_bps")
        if ism is not None and ism < cfg["ism_warn"]:
            s.update(t1="WARNING", t1_score=-2.5, t1_dist=ism - 50, t1_short=f"ISM Manufacturing {ism:.1f}% — BELOW 50, first miss", t1_detail=f"ISM {ism:.1f}% BELOW 50 — T1 WARNING. Reduce risk now.")
        else:
            s.update(t1="CLEAR", t1_score=1.0, t1_dist=(ism - 50 if ism is not None else None), t1_short=f"ISM Manufacturing {ism:.1f}% — above 50, no miss" if ism is not None else "ISM unavailable", t1_detail=f"ISM {ism:.1f}% expanding. T1 clear." if ism is not None else "ISM unavailable.")
        rising = unemp is not None and unemp_prev is not None and unemp > unemp_prev
        falling = unemp is not None and unemp_prev is not None and unemp < unemp_prev
        if unemp is not None and unemp >= cfg["t2_trigger"] and rising:
            s.update(t2="FIRING", t2_score=-2.5, t2_dist=unemp - cfg["t2_trigger"], t2_short=f"Unemployment {unemp:.1f}% — above 4.2% and rising", t2_detail=f"Unemployment {unemp:.1f}% above 4.2% and RISING. T2 active.")
        elif unemp is not None and unemp >= cfg["t2_trigger"] and falling:
            s.update(t2="RETREATING", t2_score=0.0, t2_dist=-(cfg["t2_trigger"] - unemp + 0.1), t2_short=f"Unemployment {unemp:.1f}% — was {unemp_prev:.1f}%, now falling", t2_detail=f"Unemployment {unemp:.1f}% above trigger but FALLING. Near-miss.")
        elif unemp is not None and unemp >= cfg["t2_approach"] and rising:
            s.update(t2="APPROACHING", t2_score=-0.75, t2_dist=unemp - cfg["t2_trigger"], t2_short=f"Unemployment {unemp:.1f}% — rising toward 4.2%", t2_detail=f"Unemployment {unemp:.1f}% approaching 4.2% and rising.")
        else:
            s.update(t2="CLEAR", t2_score=0.25, t2_dist=(unemp - cfg["t2_trigger"] if unemp is not None else None), t2_short=f"Unemployment {unemp:.1f}% — stable, below 4.2%" if unemp is not None else "Unemployment unavailable", t2_detail=f"Unemployment {unemp:.1f}% stable." if unemp is not None else "Unemployment unavailable.")
        if hy_bps is not None and hy_bps >= cfg["hy_fire"]:
            s.update(t3="FIRING", t3_score=-1.5, t3_detail=f"HY OAS {hy_bps:.0f}bps above 600bps crisis threshold.")
        elif hy_bps is not None and hy_bps >= cfg["hy_alert"]:
            s.update(t3="ELEVATED", t3_score=-1.0, t3_detail=f"HY OAS {hy_bps:.0f}bps elevated, {cfg['hy_fire'] - hy_bps:.0f}bps from trigger.")
        else:
            s.update(t3="CLEAR", t3_score=0.25, t3_detail=f"HY OAS {hy_bps:.0f}bps normal, {cfg['hy_fire'] - hy_bps:.0f}bps from trigger." if hy_bps is not None else "HY OAS unavailable.")
        if yc_bps is not None and yc_bps < 0:
            s.update(yc="INVERTED", yc_score=-2.0, yc_detail=f"Yield curve {yc_bps:.0f}bps inverted — leading bear signal.")
        elif yc_bps is not None and yc_bps < 50:
            s.update(yc="FLAT", yc_score=-0.5, yc_detail=f"Yield curve +{yc_bps:.0f}bps flat.")
        else:
            s.update(yc="STEEPENING", yc_score=0.5, yc_detail=f"Yield curve +{yc_bps:.0f}bps steepening — positive." if yc_bps is not None else "Yield curve unavailable.")
        score = s["t1_score"] + s["t2_score"] * 0.5 + s["t3_score"] * 0.25 + s["yc_score"] * 0.25
        s["score"] = score
        if s["t1"] == "WARNING":
            s.update(regime="LATE EXPANSION / REDUCE", action_label="REDUCE", action_detail="T1 fired below 50. Cut equity to 30–50%. Raise cash, trim cyclicals first.", action_short="REDUCE: Cut to 30–50% equity. Trim cyclicals first.")
        elif s["yc"] == "INVERTED" and s["t1"] == "CLEAR":
            s.update(regime="LATE CYCLE / INVERTED", action_label="REDUCE", action_detail="Yield curve inverted while T1 is still clear. Cut to 40–60% equity. Do not wait for T1 to confirm.", action_short="REDUCE: Inverted curve. Cut to 40–60% equity now.")
        elif s["t2"] == "RETREATING" and s["t1"] == "CLEAR":
            s.update(regime="FRAGILE / IMPROVING", action_label="ADD", action_detail="T2 retreating. Near-miss confirmed. Target 60–70% equity, deployed over 1–2 weeks on weakness.", action_short="ADD: Deploy gradually on weakness.")
        elif s["t2"] == "FIRING" and s["t1"] == "CLEAR":
            s.update(regime="FRAGILE / NEAR-MISS", action_label="HOLD", action_detail="T2 firing but T1 still clear. Hold current exposure. Do not add. One ISM miss below 50 triggers REDUCE.", action_short="HOLD: Do not add. One ISM miss = REDUCE.")
        elif s["t1"] == "CLEAR" and s["t2"] == "CLEAR":
            s.update(regime="EXPANSION", action_label="HOLD", action_detail="All primary signals clear. Hold full base positioning; stock-specific rules decide adds.", action_short="HOLD: All clear. Maintain base exposure.")
        else:
            s.update(regime="FRAGILE", action_label="HOLD", action_detail="Mixed signals. Hold current positioning and monitor T1 closely.", action_short="HOLD: Mixed signals.")
        vix = d.get("vix")
        fg = d.get("fg")
        pcr = d.get("pcr")
        cluster_n = int(vix is not None and vix > 35) + int(fg is not None and fg < 25) + int(pcr is not None and pcr > 0.9)

        # v11 hierarchy: macro regime is context; the 2-12 week opportunity window
        # drives the actionable top call.
        if s["t1"] == "WARNING":
            macro_regime = "Contraction Risk"
        elif s["yc"] == "INVERTED" and s["t1"] == "CLEAR":
            macro_regime = "Late Cycle"
        elif s["t2"] == "RETREATING" and s["t1"] == "CLEAR":
            macro_regime = "Fragile / Improving"
        elif s["t2"] == "FIRING" and s["t1"] == "CLEAR":
            macro_regime = "Fragile / Watch"
        elif s["t1"] == "CLEAR" and s["t2"] == "CLEAR":
            macro_regime = "Expansion"
        else:
            macro_regime = "Transition"

        opp_score = 0
        opp_drivers = []
        opp_risks = []
        spx_vs20 = d.get("spx_vs20")
        spx_vs50 = d.get("spx_vs50")
        spx_ret5 = d.get("spx_ret5")
        spx_ret20 = d.get("spx_ret20")
        vix_peak5 = d.get("vix_peak5")
        vix_drop5 = d.get("vix_drop5")

        if spx_vs20 is not None and spx_vs50 is not None:
            if spx_vs20 > 0 and spx_vs50 > 0:
                opp_score += 2
                opp_drivers.append("SPX above 20d/50d")
            elif spx_vs50 > 0:
                opp_score += 1
                opp_drivers.append("SPX above 50d")
            elif spx_vs20 < 0 and spx_vs50 < 0:
                opp_score -= 2
                opp_risks.append("SPX below 20d/50d")
            else:
                opp_score -= 1
                opp_risks.append("SPX trend mixed")

        if hy_bps is not None:
            if hy_bps < 350:
                opp_score += 1
                opp_drivers.append("credit calm")
            elif hy_bps >= 475:
                opp_score -= 2
                opp_risks.append("credit stress rising")
            elif hy_bps >= 400:
                opp_score -= 1
                opp_risks.append("credit spreads elevated")

        if vix_peak5 is not None and vix_drop5 is not None and vix_peak5 >= 25 and vix_drop5 >= 20:
            opp_score += 1
            opp_drivers.append("volatility compressed after stress")
        elif vix is not None and vix > 35:
            opp_score -= 2
            opp_risks.append("VIX panic active")
        elif vix is not None and vix > 25:
            opp_score -= 1
            opp_risks.append("VIX elevated")

        if fg is not None and fg <= 25 and spx_ret5 is not None and spx_ret5 > 0:
            opp_score += 1
            opp_drivers.append("fear despite price recovery")
        elif fg is not None and fg >= 75 and spx_ret20 is not None and spx_ret20 > 5:
            opp_score -= 1
            opp_risks.append("sentiment stretched")

        if s["t1"] == "WARNING":
            opp_score -= 3
            opp_risks.append("T1 macro trigger fired")
        elif s["yc"] == "INVERTED" and s["t1"] == "CLEAR":
            opp_score -= 1
            opp_risks.append("curve inverted")

        if opp_score >= 3:
            opportunity_window = "Favorable"
            opportunity_detail = "Conditions support initiating new longs over the next 2-12 weeks."
        elif opp_score >= 0:
            opportunity_window = "Mixed"
            opportunity_detail = "The 2-12 week entry window is unclear; wait for stronger confirmation."
        else:
            opportunity_window = "Unfavorable"
            opportunity_detail = "Risk/reward is poor for initiating broad new longs."

        if cluster_n >= 2:
            execution_window = "Add weakness"
            execution_detail = "Capitulation cluster active — stagger entries instead of waiting for perfect confirmation."
        elif fg is not None and fg >= 75:
            execution_window = "Wait"
            execution_detail = "Sentiment stretched — do not chase strength."
        elif spx_vs20 is not None and spx_vs20 > 3:
            execution_window = "Wait"
            execution_detail = "Price extended above the 20-day average — prefer pullbacks for new entries."
        elif spx_vs20 is not None and spx_vs20 < 0 and spx_vs50 is not None and spx_vs50 > 0:
            execution_window = "Pullback watch"
            execution_detail = "Short-term pullback within a broader trend — wait for stabilization."
        elif (vix_peak5 is not None and vix_drop5 is not None and vix_peak5 >= 25 and vix_drop5 >= 20) or (fg is not None and fg <= 35 and spx_ret5 is not None and spx_ret5 > 0):
            execution_window = "Acceptable"
            execution_detail = "Entry conditions are acceptable; use tranches, not a full-size chase."
        else:
            execution_window = "Neutral"
            execution_detail = "No tactical edge — let the opportunity window drive positioning."

        if opportunity_window == "Unfavorable":
            final_action = "Avoid"
        elif opportunity_window == "Mixed":
            final_action = "Hold Off"
        elif execution_window in {"Wait", "Pullback watch"}:
            final_action = "Wait"
        elif opportunity_window == "Favorable":
            final_action = "Enter"
        else:
            final_action = "Hold Off"

        driver_sentence = f"Support comes from {', '.join(opp_drivers[:3])}." if opp_drivers else "There is not enough positive market confirmation yet."
        risk_sentence = f"The main drag is {', '.join(opp_risks[:2])}." if opp_risks else "The main risk is that volatility, breadth, or credit reverses before a cleaner entry appears."
        if opportunity_window == "Favorable":
            opportunity_takeaway = "The 2-12 week opportunity window is favorable for new longs."
            opportunity_explanation = driver_sentence + (" Execution is the constraint today because price is extended, so staged entries are cleaner than chasing." if spx_vs20 is not None and spx_vs20 > 3 else " Execution is acceptable if entries are staged rather than rushed.")
        elif opportunity_window == "Mixed":
            opportunity_takeaway = "The 2-12 week opportunity window is mixed, so broad new longs should hold off."
            opportunity_explanation = f"{driver_sentence} {risk_sentence} Wait for trend, volatility, credit, liquidity, and leadership to line up."
        else:
            opportunity_takeaway = "The 2-12 week opportunity window is unfavorable for broad new longs."
            opportunity_explanation = f"{risk_sentence} New entries should wait until price trend, volatility, and credit conditions repair."

        s.update(
            macro_regime=macro_regime,
            opportunity_score=opp_score,
            opportunity_window=opportunity_window,
            opportunity_detail=opportunity_detail,
            opportunity_drivers=opp_drivers[:3],
            opportunity_risks=opp_risks[:2],
            opportunity_takeaway=opportunity_takeaway,
            opportunity_explanation=opportunity_explanation,
            opportunity_reason=f"{opportunity_takeaway} {opportunity_explanation}",
            execution_window=execution_window,
            execution_detail=execution_detail,
            final_action=final_action,
            key_risk=(opp_risks[0] if opp_risks else ("Chasing after a sharp recovery; prefer pullbacks for new longs." if final_action == "Wait" else "A reversal in volatility, breadth, or credit could weaken the window.")),
            regime_layer=macro_regime,
            portfolio_stance=opportunity_window,
            action_guidance=final_action,
            short_term_cond=execution_window,
        )
        s["why_today"] = []
        if s["t1"] == "WARNING":
            s["why_today"].append(f"ISM {ism:.1f}% below 50 → primary recession trigger fired")
        elif s["t2"] == "RETREATING":
            s["why_today"].append(f"Unemployment retreating {unemp_prev:.1f}%→{unemp:.1f}% → near-miss confirmed")
        elif s["t2"] == "FIRING":
            s["why_today"].append(f"Unemployment {unemp:.1f}% rising above 4.2% → labor stress building")
        elif s["t1"] == "CLEAR" and ism is not None:
            s["why_today"].append(f"ISM {ism:.1f}% above 50 → expansion intact, no T1 trigger")
        if s["yc"] == "INVERTED":
            s["why_today"].append(f"Yield curve {yc_bps:.0f}bps inverted → late-cycle risk elevated")
        elif s["t3"] == "FIRING":
            s["why_today"].append(f"HY spreads {hy_bps:.0f}bps above 600 → credit stress firing")
        elif s["t3"] == "ELEVATED":
            s["why_today"].append(f"HY spreads {hy_bps:.0f}bps elevated → credit caution rising")
        elif hy_bps is not None and hy_bps < 350 and len(s["why_today"]) < 2:
            s["why_today"].append(f"HY spreads {hy_bps:.0f}bps stable → no credit stress")
        s["why_today"] = s["why_today"][:2]
        s["change_if"] = []
        s["change_if"].append("ISM prints below 50 → shift to REDUCE" if s["t1"] == "CLEAR" else "ISM recovers above 50 → begin rebuilding")
        if s["t3"] != "FIRING" and hy_bps is not None and hy_bps < 475:
            s["change_if"].append("HY spreads above 475bps → tail-risk activates")
        elif s["t2"] not in {"FIRING", "RETREATING"}:
            s["change_if"].append("Unemployment re-accelerates above 4.4% rising → regime deteriorates")
        return s

    def _market_implications(d, s):
        stance = s.get("portfolio_stance", "Neutral")
        action = s.get("action_guidance", "Wait for Confirmation")
        short_term = s.get("short_term_cond", "Constructive")
        if action in {"Avoid", "Raise Cash", "Reduce Weak Exposure"}:
            exposure = "Pull gross exposure down before credit or earnings confirm the slowdown. Cash is a risk-control tool here, not a failed trade."
            selection = "Keep only the names with clear trend, liquidity, and durable earnings. Cyclicals, weak balance sheets, and broken charts get cut first."
            execution = "Do not average down into weak setups. New buys need unusually clean entries, defined stops, and smaller size."
            trim = "Trim extended winners, laggards below the 200-day, and positions where the thesis now depends on a macro rebound."
        elif action == "Enter":
            exposure = "Move risk up in tranches. The opportunity window supports new longs, but individual stock triggers still decide exact entries."
            selection = "Prioritize leadership that already held support during the stress window. Add to strength on pullbacks, not to lower-quality catch-up names."
            execution = "Use staged entries over 1-2 weeks. Let the watchlist find exact setups while the regime permits measured buying."
            trim = "Fund adds by reducing stale positions that failed to rebound while the regime improved."
        elif action == "Wait":
            exposure = "Keep current exposure but do not chase. The opportunity window is present, while execution timing still needs a cleaner entry."
            selection = "Prepare the highest-quality leaders and names near support. Avoid broad adds in extended names."
            execution = "Wait for pullbacks, stabilization, or volatility compression before adding risk."
            trim = "Trim only positions where the stock-level setup has broken, not just because the broad tape is pausing."
        elif action in {"Hold Off", "Maintain Full Positioning"}:
            exposure = "Hold current exposure. The opportunity window is not clean enough for broad new longs, but it is not a forced de-risk signal."
            selection = "Let the rules favor liquid leaders, constructive relative strength, and quality growth. Avoid forcing trades in names without clean triggers."
            execution = "Add only when the individual setup is actionable. A mixed opportunity window does not override bad entry price."
            trim = "Use trims for crowded or stretched names, not because the macro backdrop alone has turned."
        else:
            exposure = "Hold current exposure. The signals are mixed enough that doing less is the decision until the next trigger resolves."
            selection = "Keep the bar high: leadership, relative strength, and clear technical structure. Watchlist names can prepare, but not all deserve capital."
            execution = "Wait for confirmation rather than predicting the next macro print. Preserve optionality for cleaner setups."
            trim = "Clean up weak or duplicative exposure, especially if it does not fit the current regime."
        if short_term in {"Extended", "Momentum Acceleration"}:
            execution += " Short-term tape is extended, so avoid chasing gap-up moves."
        elif short_term in {"Oversold", "Healthy Pullback"}:
            execution += " Short-term weakness can be used, but only if the primary cycle signal stays intact."
        return [
            ("Exposure", exposure),
            ("Stock selection", selection),
            ("Execution", execution),
            ("Trim / avoid", trim),
            ("Decision rule", f"Macro stance is {stance}. Portfolio action is {action}. Individual stocks still need their own entry/exit signal."),
        ]

    def _scenario_map(d, s):
        ism = d.get("ism")
        unemp = d.get("unemp")
        hy_bps = d.get("hy_bps")
        base = s.get("action_detail", "Hold current exposure until the next primary trigger resolves.")
        if s.get("t1") == "CLEAR":
            bull = "ISM holds above 50, unemployment stabilizes or falls, and credit spreads stay contained. That keeps the risk budget open for high-quality entries."
            bear = "ISM breaks below 50. That is the first reduce signal in this framework and should override short-term dip-buying instincts."
        else:
            bull = "ISM recovers above 50 and credit does not confirm stress. That would allow a staged rebuild, not an instant full-risk reset."
            bear = "ISM remains below 50 while unemployment rises or credit spreads widen. That confirms the slowdown and keeps cash high."
        if s.get("t2") == "FIRING":
            bear += " Labor is already firing, so a weak ISM print would be more serious than a one-off data wobble."
        elif s.get("t2") == "RETREATING":
            bull += " The unemployment near-miss is already retreating, which is the cleanest path to a constructive regime."
        numbers = []
        if ism is not None:
            numbers.append(f"ISM {ism:.1f}")
        if unemp is not None:
            numbers.append(f"unemployment {unemp:.1f}%")
        if hy_bps is not None:
            numbers.append(f"HY {hy_bps:.0f}bps")
        return [
            ("Base case", base, "var(--color-blue)"),
            ("Bull case", bull, "var(--color-positive)"),
            ("Bear case", bear, "var(--color-negative)"),
            ("Current evidence", " · ".join(numbers) if numbers else "Macro inputs are partially unavailable; treat the read as lower confidence.", "var(--color-muted)"),
        ]

    def _forward_watch_items(d, s, crypto):
        items = []
        ism = d.get("ism")
        unemp = d.get("unemp")
        hy_bps = d.get("hy_bps")
        yc_bps = d.get("yc_bps")
        vix = d.get("vix")
        fg = d.get("fg")
        if s.get("t1") == "CLEAR":
            gap = 50 - ism if ism is not None else None
            items.append(("ISM Manufacturing", "First print below 50", f"That flips the primary cycle trigger from clear to reduce. Current buffer: {abs(gap):.1f} pts above 50." if gap is not None else "This is the most important macro exit trigger."))
        else:
            items.append(("ISM Manufacturing", "Reclaim above 50", "That would begin repairing the primary cycle signal. Until then, reduce/defensive posture stays in force."))
        if s.get("t2") in {"FIRING", "APPROACHING"}:
            items.append(("Labor market", "Unemployment stops rising", "A falling unemployment rate would turn labor stress from a threat into a near-miss."))
        else:
            distance = 4.2 - unemp if unemp is not None else None
            items.append(("Labor market", "Unemployment rises through 4.2%", f"That activates T2 if the move is rising month over month. Current distance: {distance:.1f} pts." if distance is not None else "This is the labor-market warning line."))
        if hy_bps is not None and hy_bps < 475:
            items.append(("Credit", "HY spreads above 475bps", f"Tail-risk warning starts before the 600bps crisis trigger. Current level: {hy_bps:.0f}bps."))
        elif hy_bps is not None:
            items.append(("Credit", "HY spreads reverse lower", f"Credit is already elevated at {hy_bps:.0f}bps. Improvement would reduce tail-risk pressure."))
        else:
            items.append(("Credit", "HY spreads", "Watch for widening credit stress; this is the confirmation layer after macro weakens."))
        if yc_bps is not None and yc_bps < 0:
            items.append(("Curve", "10Y-2Y turns positive", "A less inverted curve would remove one late-cycle warning, though it does not by itself create a buy signal."))
        elif yc_bps is not None and yc_bps < 50:
            items.append(("Curve", "Curve steepens above +50bps", "A steeper curve supports financial conditions and reduces late-cycle pressure."))
        else:
            items.append(("Curve", "Curve flattens sharply", "A sudden flattening would be an early warning that growth expectations are fading."))
        if vix is not None and fg is not None:
            items.append(("Sentiment", "VIX >35 and Fear & Greed <25", "That creates an oversold cluster. It can be a buying window only if T1 stays clear."))
        else:
            items.append(("Sentiment", "Panic or euphoria extremes", "Use sentiment as timing, not as the primary macro regime signal."))
        btc_vs_200 = crypto.get("btc_vs_200")
        if btc_vs_200 is not None:
            trigger = "BTC holds above 200d" if btc_vs_200 > 0 else "BTC reclaims 200d"
            why = "Crypto risk appetite is supportive while BTC stays above the long-term trend." if btc_vs_200 > 0 else "A reclaim would improve speculative risk appetite; failure keeps crypto secondary."
            items.append(("Crypto", trigger, why))
        return items[:6]

    @st.cache_data(ttl=60 * 60, show_spinner=False)
    def _crypto_snapshot():
        btc = _quote("BTC-USD", "260d")
        eth = _quote("ETH-USD", "260d")
        ethbtc_hist = yf.Ticker("ETH-BTC").history(period="35d", interval="1d", auto_adjust=True)
        btc_hist = yf.Ticker("BTC-USD").history(period="260d", interval="1d", auto_adjust=True)
        fg = _fear_greed()
        if btc_hist is None or btc_hist.empty:
            return {}
        closes = btc_hist["Close"].dropna()
        if len(closes) < 50:
            return {}
        btc_price = float(closes.iloc[-1])
        ma200 = float(closes.tail(200).mean())
        ma20 = float(closes.tail(20).mean())
        btc_vs_200 = (btc_price / ma200 - 1) * 100 if ma200 else None
        btc_vs_20 = (btc_price / ma20 - 1) * 100 if ma20 else None
        ethbtc_change = None
        ethbtc_now = None
        if ethbtc_hist is not None and not ethbtc_hist.empty:
            ethbtc = ethbtc_hist["Close"].dropna()
            if len(ethbtc):
                ethbtc_now = float(ethbtc.iloc[-1])
            if len(ethbtc) >= 20:
                ethbtc_change = (float(ethbtc.iloc[-1]) / float(ethbtc.tail(30).mean()) - 1) * 100
        halving = datetime(2024, 4, 20)
        now = datetime.now()
        months_since_halving = max(0, (now.year - halving.year) * 12 + now.month - halving.month)
        return {
            "price": btc_price,
            "change": btc.get("change"),
            "eth_price": eth.get("last"),
            "eth_change": eth.get("change"),
            "btc_vs_200": btc_vs_200,
            "btc_vs_20": btc_vs_20,
            "fg": fg,
            "ethbtc_change": ethbtc_change,
            "ethbtc_now": ethbtc_now,
            "btc_dom": 55.0,
            "stable_dom": None,
            "stable_chg": None,
            "months_since_halving": months_since_halving,
        }

    def _crypto_color(name):
        return {
            "green": "var(--color-positive)",
            "yellow": "var(--color-warning-text)",
            "red": "var(--color-negative)",
            "blue": "var(--color-blue)",
            "muted": "var(--color-muted)",
        }.get(name, "var(--color-muted)")

    def _crypto_class(name):
        return {
            "green": "good",
            "yellow": "warn",
            "red": "bad",
            "blue": "neutral",
            "muted": "neutral",
        }.get(name, "neutral")

    def _crypto_change_class(value, inverse=False):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "neutral"
        if inverse:
            value = -value
        if value > 0:
            return "good"
        if value < 0:
            return "bad"
        return "neutral"

    def _crypto_fear_class(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "neutral"
        if value < 25:
            return "bad"
        if value < 45:
            return "warn"
        if value <= 70:
            return "good"
        return "warn"

    def _crypto_phase_class(phase_num):
        return {
            1: "warn",
            2: "good",
            3: "warn",
            4: "bad",
        }.get(phase_num, "neutral")

    def _score_crypto(c):
        btc_vs_200 = c.get("btc_vs_200")
        btc_vs_20 = c.get("btc_vs_20")
        fg_value = (c.get("fg") or {}).get("value")
        ethbtc_change = c.get("ethbtc_change")
        btc_dom = c.get("btc_dom")

        above_200 = btc_vs_200 is not None and btc_vs_200 > 0
        above_20 = btc_vs_20 is not None and btc_vs_20 > 0
        high_dom = btc_dom is not None and btc_dom > 60
        low_dom = btc_dom is not None and btc_dom < 54
        extreme_fear = fg_value is not None and fg_value < 25
        deep_fear = fg_value is not None and fg_value < 35
        greed = fg_value is not None and fg_value >= 65
        euphoria = fg_value is not None and fg_value >= 80

        if above_200 and above_20:
            q1 = ("Bull", "Above 200d and 20d MA", "green")
        elif above_200 and not above_20:
            q1 = ("Mixed", "Structure intact, momentum fading", "yellow")
        elif not above_200 and above_20:
            q1 = ("Reclaim attempt", "200d broken, 20d holding", "yellow")
        else:
            q1 = ("Bear", "Below 200d and 20d MA", "red")

        if q1[0] == "Bull" and fg_value is not None and fg_value < 40:
            q2 = ("Yes", "Pro-trend dip; add selectively", "green")
        elif q1[0] == "Bull" and fg_value is not None and fg_value < 65:
            q2 = ("Selective", "Trend is healthy but no sentiment discount", "yellow")
        elif q1[0] == "Bull":
            q2 = ("Wait", "Chasing into greed; let it cool", "yellow")
        elif q1[0] == "Mixed" and deep_fear:
            q2 = ("Yes, small", "Counter-trend only while structure repairs", "yellow")
        elif q1[0] == "Bear" and extreme_fear:
            q2 = ("Yes, small", "Capitulation setup, not confirmation", "yellow")
        else:
            q2 = ("No", "No edge yet", "red")

        if ethbtc_change is not None and ethbtc_change > 5 and low_dom:
            q3 = ("Alts", "ETH/BTC rising and BTC dominance low", "green")
        elif ethbtc_change is not None and ethbtc_change > 5:
            q3 = ("Lean alts", "ETH/BTC improving, dominance not yet confirming", "yellow")
        elif ethbtc_change is not None and ethbtc_change < -5 and high_dom:
            q3 = ("BTC only", "ETH/BTC weak and BTC dominance high", "red")
        elif ethbtc_change is not None and ethbtc_change < -5:
            q3 = ("Lean BTC", "ETH/BTC weakening", "yellow")
        else:
            q3 = ("Neutral", "No clean BTC/alt rotation edge", "muted")

        if above_200 and (btc_vs_200 or 0) > 15 or (above_200 and (btc_vs_200 or 0) > 5 and ((btc_vs_20 or 0) > 5 or euphoria)):
            four = ("Phase 3", "Parabolic bull", "Trend is mature; manage greed and trailing risk.", 3)
        elif above_200:
            four = ("Phase 2", "Recovery / expansion", "Constructive cycle with room if macro stays supportive.", 2)
        elif not above_200 and (btc_vs_200 or 0) < -35 and not above_20 and extreme_fear:
            four = ("Phase 1", "Accumulation", "Deep-cycle stress; buys are long-duration and sized small.", 1)
        elif not above_200 and above_20:
            four = ("Phase 4", "Bear-market recovery attempt", "Repair attempt, but 200d is still the key line.", 4)
        else:
            four = ("Phase 4", "Bear market", "Defense until BTC reclaims the long-term trend.", 4)

        if above_20 and ((btc_vs_20 or 0) < 2 or greed):
            medium = ("Late", "Short-term risk/reward is less attractive.", "yellow")
        elif above_20:
            medium = ("Expansion", "Short-term momentum supports risk.", "green")
        elif extreme_fear:
            medium = ("Recovery watch", "Watch for a 20d reclaim from washout conditions.", "yellow")
        else:
            medium = ("Rolling over", "Short-term trend is losing support.", "red")

        alignment = "Aligned" if q1[0] == "Bull" and medium[0] == "Expansion" else ("Mixed" if q1[0] in {"Bull", "Mixed", "Reclaim attempt"} else "Defensive")
        return {"q1": q1, "q2": q2, "q3": q3, "four": four, "medium": medium, "alignment": alignment}

    def _crypto_narrative(c, scored, d, s):
        fg = c.get("fg") or {}
        fg_txt = f'{fg.get("value")} {fg.get("label")}' if fg.get("value") is not None else "unavailable"
        btc_200 = _signed_regime(c.get("btc_vs_200"))
        btc_20 = _signed_regime(c.get("btc_vs_20"))
        ethbtc = _signed_regime(c.get("ethbtc_change"))
        macro_gate = s.get("portfolio_stance", "Neutral")
        return [
            ("Trend", f'BTC is {btc_200} vs its 200d and {btc_20} vs its 20d, so the crypto tape is {scored["q1"][0].lower()}. The key regime line is still the 200d moving average.'),
            ("Opportunity", f'Fear & Greed is {fg_txt}. Add timing is {scored["q2"][0].lower()}: {scored["q2"][1]}. This is a sizing signal, not permission to ignore the macro risk budget.'),
            ("Positioning", f'ETH/BTC is {ethbtc} versus its recent baseline and BTC dominance is modeled near {c.get("btc_dom", 55):.1f}%. Rotation read: {scored["q3"][0]} — {scored["q3"][1]}.'),
            ("Conviction", f'Cycle read is {scored["four"][0]} ({scored["four"][1]}) and medium-term phase is {scored["medium"][0]}. Portfolio stance from the broader regime engine is {macro_gate}, so crypto risk should stay inside that envelope.'),
        ]

    def _crypto_section_html(c, d, s, memo_crypto=None):
        if not c or c.get("price") is None:
            return (
                '<div class="regime-panel regime-pad">'
                '<span class="regime-section-title">Crypto regime</span>'
                '<div class="crypto-note">Crypto data is unavailable right now. Refresh the regime page to retry BTC, ETH/BTC, and sentiment inputs.</div>'
                '</div></div>'
            )
        scored = _score_crypto(c)
        btc_price = f'${c.get("price"):,.0f}' if c.get("price") is not None else "—"
        eth_price = f'${c.get("eth_price"):,.0f}' if c.get("eth_price") is not None else "—"
        fg = c.get("fg") or {}
        fg_txt = f'{fg.get("value")} · {fg.get("label")}' if fg.get("value") is not None else "—"
        price_cells = [
            ("BTC", btc_price, _signed_regime(c.get("change")), _crypto_change_class(c.get("change"))),
            ("ETH", eth_price, _signed_regime(c.get("eth_change")), _crypto_change_class(c.get("eth_change"))),
            ("BTC dominance", f'{c.get("btc_dom", 55):.1f}%', "approximation", _crypto_class(scored["q3"][2])),
            ("Fear & Greed", fg_txt, f'{c.get("months_since_halving", 0)} mo post-halving', _crypto_fear_class(fg.get("value"))),
        ]
        price_html = "".join(
            f'<div class="crypto-price-cell {cls}"><div class="k">{html.escape(k)}</div><div class="v">{html.escape(v)}</div><div class="crypto-note sub {cls}">{html.escape(sub)}</div></div>'
            for k, v, sub, cls in price_cells
        )
        decision_html = "".join(
            f'<div class="crypto-card {_crypto_class(color)}"><div class="q">{html.escape(title)}</div>'
            f'<div class="answer" style="color:{_crypto_color(color)};"><span class="crypto-dot" style="background:{_crypto_color(color)};"></span>{html.escape(answer)}</div>'
            f'<div class="crypto-note">{html.escape(note)}</div></div>'
            for title, (answer, note, color) in [
                ("1 · Bull or bear?", scored["q1"]),
                ("2 · Good time to add?", scored["q2"]),
                ("3 · BTC or alts?", scored["q3"]),
            ]
        )
        phase_defs = [
            (1, "Phase 1", "🤔 Accumulation", "Post-crash, boring, sentiment terrible. Smart money buys quietly.", "2015, 2018–19, 2022–23", "Deeply below 200d, extreme fear, flat price action"),
            (2, "Phase 2", "📈 Recovery", "Price climbs back toward old highs. Halving occurs. Retail not yet paying attention.", "2016, 2020, 2023–24", "Reclaimed 200d MA, momentum building, sentiment improving"),
            (3, "Phase 3", "🚀 Parabolic Bull", "Euphoria. New ATH. Media explodes. Altcoins go parabolic.", "Q4 2013, Q4 2017, Q4 2021, Q4 2025", "Well above 200d, extreme greed, parabolic price action"),
            (4, "Phase 4", "🐻 Bear / Transition", "50–80% drawdown from peak. Bear rallies trap latecomers. Watch for accumulation signals before the next cycle begins.", "2014–15, 2018, 2022, 2025–26", "Below 200d, declining momentum, bear rallies mislead"),
        ]
        active_phase = scored["four"][3]
        phases = "".join(
            f'<div class="crypto-phase {_crypto_phase_class(num)} {"current" if num == active_phase else ""}"><div class="phase">{html.escape(label)}</div>'
            f'<div class="name">{html.escape(name)}</div><div class="desc">{html.escape(desc)}</div>'
            f'<div class="meta">Historical</div><div class="meta-text">{html.escape(historical)}</div>'
            f'<div class="meta">Signal</div><div class="meta-text"><em>{html.escape(signal)}</em></div></div>'
            for num, label, name, desc, historical, signal in phase_defs
        )
        memo_rows = (memo_crypto or {}).get("narrative") if isinstance(memo_crypto, dict) else None
        if isinstance(memo_rows, list) and len(memo_rows) >= 3:
            narrative_rows = [
                (
                    str((row or {}).get("title") or "Read"),
                    str((row or {}).get("body") or ""),
                )
                for row in memo_rows[:4]
                if isinstance(row, dict)
            ]
        else:
            narrative_rows = _crypto_narrative(c, scored, d, s)
        narrative = "".join(
            f'<div class="crypto-narrative-row"><div class="h">{html.escape(h)}</div><div class="b">{html.escape(b)}</div></div>'
            for h, b in narrative_rows
        )
        tags = [
            (f'Cycle: {scored["four"][0]} · {scored["four"][1]}', _crypto_phase_class(scored["four"][3])),
            (f'Medium: {scored["medium"][0]}', _crypto_class(scored["medium"][2])),
            (f'Alignment: {scored["alignment"]}', "good" if scored["alignment"] == "Aligned" else ("warn" if scored["alignment"] == "Mixed" else "bad")),
            (f'ETH/BTC: {_signed_regime(c.get("ethbtc_change"))}', _crypto_change_class(c.get("ethbtc_change"))),
        ]
        return (
            '<div class="regime-panel regime-pad crypto-wrap">'
            '<div class="crypto-header"><span class="crypto-accent"></span><span class="regime-section-title" style="margin:0;">Crypto regime</span>'
            + "".join(f'<span class="crypto-badge {cls}">{html.escape(tag)}</span>' for tag, cls in tags)
            + '</div>'
            f'<div class="crypto-price-strip">{price_html}</div>'
            f'<div class="crypto-decisions">{decision_html}</div>'
            '<span class="regime-section-title" style="margin-top:16px;">Behavioral cycle map</span>'
            '<div class="crypto-cycle-note">Contextual reference — not the primary driver of decisions</div>'
            f'<div class="crypto-cycle-grid">{phases}</div>'
            f'<div class="crypto-narrative">{narrative}</div>'
            '</div></div>'
        )

    @st.cache_data(ttl=60 * 60, show_spinner=False)
    def _regime_snapshot(refresh_key):
        ism = _fred_rows("NAPMPMI")
        unemp = _fred_rows("UNRATE")
        hy = _fred_rows("BAMLH0A0HYM2")
        yc = _fred_rows("T10Y2Y")
        fed = _fred_rows("WALCL")
        rrp = _fred_rows("WLRRAL")
        tga = _fred_rows("WTREGEN")
        fg = _fear_greed()
        spx = _quote("^GSPC")
        qqq = _quote("QQQ")
        vix = _quote("^VIX")
        d = {
            "ism": ism[-1]["value"] if ism else None,
            "unemp": unemp[-1]["value"] if unemp else None,
            "unemp_prev": unemp[-2]["value"] if len(unemp) >= 2 else None,
            "hy_bps": hy[-1]["value"] * 100 if hy else None,
            "yc_bps": yc[-1]["value"] * 100 if yc else None,
            "fed_now": fed[-1]["value"] if fed else None,
            "fed_prev": fed[-5]["value"] if len(fed) >= 5 else None,
            "rrp_now": rrp[-1]["value"] if rrp else None,
            "rrp_prev": rrp[-5]["value"] if len(rrp) >= 5 else None,
            "tga_now": tga[-1]["value"] if tga else None,
            "tga_prev": tga[-5]["value"] if len(tga) >= 5 else None,
            "spx": spx.get("last"),
            "spx_change": spx.get("change"),
            "spx_vs20": spx.get("vs20"),
            "spx_vs50": spx.get("vs50"),
            "spx_ret5": spx.get("ret5"),
            "spx_ret20": spx.get("ret20"),
            "qqq": qqq.get("last"),
            "qqq_change": qqq.get("change"),
            "vix": vix.get("last"),
            "vix_change": vix.get("change"),
            "vix_peak5": vix.get("peak5"),
            "vix_drop5": vix.get("drop5"),
            "fg": fg.get("value"),
            "fg_label": fg.get("label"),
            "pcr": None,
        }
        s = _score_regime(d)
        liq_signals = 0
        liq_count = 0
        if d["fed_now"] is not None and d["fed_prev"] is not None:
            liq_count += 1
            liq_signals += 1 if d["fed_now"] > d["fed_prev"] * 1.001 else (-1 if d["fed_now"] < d["fed_prev"] * 0.999 else 0)
        if d["rrp_now"] is not None and d["rrp_prev"] is not None:
            liq_count += 1
            liq_signals += 1 if d["rrp_now"] < d["rrp_prev"] * 0.99 else (-1 if d["rrp_now"] > d["rrp_prev"] * 1.01 else 0)
        if d["tga_now"] is not None and d["tga_prev"] is not None:
            liq_count += 1
            liq_signals += 1 if d["tga_now"] < d["tga_prev"] * 0.99 else (-1 if d["tga_now"] > d["tga_prev"] * 1.01 else 0)
        if liq_count == 0:
            s.update(liq_status="NEUTRAL", liq_detail="Liquidity data unavailable.", liq_color="var(--color-muted)")
        elif liq_signals >= 2:
            s.update(liq_status="IMPROVING", liq_detail="Net liquidity expanding — Fed balance sheet, RRP, and TGA skew positive.", liq_color="var(--color-positive)")
        elif liq_signals <= -2:
            s.update(liq_status="TIGHTENING", liq_detail="Net liquidity tightening — medium-term headwind for risk assets.", liq_color="var(--color-negative)")
        else:
            s.update(liq_status="NEUTRAL", liq_detail="Mixed liquidity signals — neither clear tailwind nor headwind.", liq_color="var(--color-warning-text)")
        def _bchg(now, prev):
            return round((now - prev) / 1000) if now is not None and prev is not None else None
        s["liq_numbers"] = {
            "Fed": _bchg(d["fed_now"], d["fed_prev"]),
            "RRP": _bchg(d["rrp_now"], d["rrp_prev"]),
            "TGA": _bchg(d["tga_now"], d["tga_prev"]),
        }
        s["alerts"] = []
        if s["t1"] == "WARNING":
            s["alerts"].append("T1 WARNING")
        if s["yc"] == "INVERTED" and s["t1"] == "CLEAR":
            s["alerts"].append("CURVE INVERTED")
        if s["t2"] == "FIRING":
            s["alerts"].append("T2 FIRING")
        if s["t2"] == "APPROACHING":
            s["alerts"].append("T2 APPROACHING")
        if s["t3"] == "ELEVATED":
            s["alerts"].append("HY ELEVATED")
        return {"data": d, "signals": s, "crypto": _crypto_snapshot(), "updated_at": now_market_time().isoformat(timespec="seconds")}

    if st.button("↻ Refresh market regime", key="refresh_market_regime", help="Refresh macro, crypto, and regenerate the daily Claude memo."):
        _fred_rows.clear()
        _quote.clear()
        _fear_greed.clear()
        _crypto_snapshot.clear()
        _regime_snapshot.clear()
        st.session_state["force_regime_daily_memo"] = True
        st.rerun()

    regime_refresh_key = regime_daily_key()
    snap = _regime_snapshot(regime_refresh_key)
    d, s, crypto = snap["data"], snap["signals"], snap.get("crypto") or {}
    color_map = {
        "Risk Off": "var(--color-negative)",
        "Defensive": "var(--color-warning-text)",
        "Neutral": "var(--color-blue)",
        "Moderately Risk On": "var(--color-positive)",
        "Risk On": "var(--color-positive)",
    }
    action_color = color_map.get(s["portfolio_stance"], "var(--color-text)")
    short_term_color = _condition_color(s.get("short_term_cond"))
    sev_color = {"CLEAR": "var(--color-positive)", "RETREATING": "var(--color-positive)", "APPROACHING": "var(--color-warning-text)", "ELEVATED": "var(--color-warning-text)", "WARNING": "var(--color-negative)", "FIRING": "var(--color-negative)", "INVERTED": "var(--color-warning-text)", "FLAT": "var(--color-warning-text)", "STEEPENING": "var(--color-positive)", "NEUTRAL": "var(--color-muted)", "IMPROVING": "var(--color-positive)", "TIGHTENING": "var(--color-negative)"}
    updated_label = format_market_time(snap["updated_at"], "%b %d · %-I:%M %p %Z")

    def _risk_status_class(label):
        label = str(label or "").lower()
        if any(word in label for word in ("expansion", "risk on", "maintain", "healthy", "constructive", "momentum", "acceleration", "tight", "calm", "favorable", "enter", "acceptable", "add weakness")):
            return "good"
        if any(word in label for word in ("reduce", "risk off", "warning", "stress", "extreme", "raise cash", "unfavorable", "avoid")):
            return "bad"
        if any(word in label for word in ("defensive", "neutral", "choppy", "extended", "fragile", "mixed", "wait", "hold off", "pullback")):
            return "warn"
        return ""

    def _change_html(value, inverse=False):
        color = _regime_value_color(value, inverse=inverse)
        return f'<span class="risk-change" style="color:{color};">{html.escape(_signed_regime(value))}</span>'

    def _vix_label(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "—"
        if value < 20:
            return "calm"
        if value < 28:
            return "elevated"
        return "stressed"

    def _hy_label(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "—"
        if value < 350:
            return "tight"
        if value < 475:
            return "normal"
        if value < 600:
            return "wide"
        return "stress"

    def _why_today_text(d, s):
        if s.get("opportunity_reason"):
            return s["opportunity_reason"]
        action = s.get("action_guidance")
        if action == "Enter":
            return "The 2-12 week opportunity window is favorable. Start or continue building exposure in tranches, while letting individual stock triggers decide exact entries."
        if action == "Wait":
            return "The opportunity window is open, but execution timing is not clean. Wait for pullbacks or stabilization rather than chasing the move."
        if action == "Avoid":
            return "The opportunity window is unfavorable. Do not initiate broad new longs until trend, volatility, and credit conditions repair."
        return "The opportunity window is mixed. Keep the watchlist ready, but do not force broad new longs until market-led inputs line up."

    def _forward_watch_static():
        return [
            ("University of Michigan Consumer Sentiment (Friday June 26)", "Final June print; recovery from May lows would ease recession fears."),
            ("Conference Board Consumer Confidence (Tuesday June 30)", "Broad measure of mood. Weakness would reinforce consumer distress narrative."),
            ("Jobs Report for June (Thursday July 2, 8:30 AM)", "Critical. Miss signals labor cracks; beat validates expansion. Moves rates and equities."),
            ("ISM Services PMI for June (Friday July 3)", "Services employ 85% of workforce. Weakness here suggests wage pressure and spending risk."),
        ]

    def _market_implication_static():
        headline = "PCE hits 3-year high at 4.1% YoY; Micron earnings crush AI demand fears; expansion regime intact but inflation timeline tightens."
        bullets = [
            "PCE May 4.1% YoY, highest since April 2023 → hawkish inflation signal → rate hike risk 2026 intact, pressures equity duration",
            "Micron Q3 revenue $41.46B, beat $35.86B forecast by 16% → AI capex cycle confirmed → tech momentum resumes, semis rally",
            "May retail sales +0.9% beat +0.5% forecast, consumer resilient → spending strength contradicts Fed concern → base positioning holds",
            "Oil prices down 1.7-2% on tanker releases from Persian Gulf → deflation tailwind for Q2/Q3 → energy CPI component risk easing",
        ]
        triggers = [
            "If PCE core tracks above 3.5% 3M-ahead → FOMC liftoff mid-2026 odds spike → rotate away from duration",
            "If Micron guidance falters or semis break below 20-day MA → AI boom narrative cracks → tech profit-taking accelerates",
            "If oil rallies back above $75 on supply shock → inflation re-acceleration → PCE peak claim invalidated",
        ]
        return headline, bullets, triggers

    def _default_forward_watch(d, s, crypto):
        items = _forward_watch_items(d, s, crypto)
        return [
            {
                "title": title,
                "body": body,
            }
            for title, trigger, body in items[:4]
        ]

    def _fallback_regime_daily_memo(d, s, crypto):
        drivers = s.get("opportunity_drivers") or []
        risks = s.get("opportunity_risks") or []
        impact_headline = s.get("opportunity_takeaway") or _why_today_text(d, s)
        impact_bullets = [
            f"Opportunity window is {s.get('opportunity_window', 'mixed')} with score {s.get('opportunity_score', '—')} → {s.get('action_guidance', 'Hold Off')} is the broad action",
            f"SPX versus trend: 20d {_signed_regime(d.get('spx_vs20'))}, 50d {_signed_regime(d.get('spx_vs50'))} → price trend drives entry timing",
            f"Credit and volatility: HY {_fmt_regime(d.get('hy_bps'), 'bps', 0)}, VIX {_fmt_regime(d.get('vix'), '', 1)} → risk appetite remains the key confirmation layer",
        ]
        if drivers:
            impact_bullets.append(f"Positive drivers: {', '.join(drivers)} → supports staged risk when stock-level triggers agree")
        if risks:
            impact_bullets.append(f"Primary risks: {', '.join(risks)} → wait for repair before forcing broad new longs")
        watch_triggers = [
            "If SPX loses both the 20d and 50d trend → opportunity window deteriorates → stop adding broad exposure",
            "If HY spreads move toward 475bps → credit stress rises → reduce marginal cyclicals",
            "If volatility compresses while price holds trend → execution improves → staged entries become cleaner",
        ]
        signal_explanations = {
            "T1 · ISM": s["t1_detail"],
            "T2 · Unemployment": s["t2_detail"],
            "T3 · HY OAS": s["t3_detail"],
            "Yield curve": s["yc_detail"],
            "Liquidity": f'{s["liq_detail"]} Fed {s["liq_numbers"]["Fed"] if s["liq_numbers"]["Fed"] is not None else "—"}B · RRP {s["liq_numbers"]["RRP"] if s["liq_numbers"]["RRP"] is not None else "—"}B · TGA {s["liq_numbers"]["TGA"] if s["liq_numbers"]["TGA"] is not None else "—"}B',
        }
        crypto_scored = _score_crypto(crypto) if crypto and crypto.get("price") is not None else None
        crypto_narrative = [
            {"title": title, "body": body}
            for title, body in (_crypto_narrative(crypto, crypto_scored, d, s) if crypto_scored else [])
        ]
        return {
            "schema_version": REGIME_DAILY_MEMO_SCHEMA_VERSION,
            "why_today": _why_today_text(d, s),
            "daily_context": {
                "headline": impact_headline,
                "bullets": impact_bullets,
                "change_status": "NO CHANGE",
                "watch_triggers": watch_triggers,
            },
            "forward_watch": _default_forward_watch(d, s, crypto),
            "signal_explanations": signal_explanations,
            "crypto": {
                "narrative": crypto_narrative,
            },
            "source_note": "Rule fallback — Claude daily memo unavailable.",
        }

    def _daily_brief_seed_memo(d, s, crypto, today_key):
        memo = _fallback_regime_daily_memo(d, s, crypto)
        if today_key == "2026-06-30":
            memo.update(
                {
                    "topline": {
                        "regime": "Expansion",
                        "portfolio_stance": "Risk On",
                        "action": "Maintain Full Positioning",
                        "short_term": "Momentum Acceleration",
                    },
                    "why_today": (
                        "Stay the course. You are fully invested and positioned for earnings season, "
                        "which carries both upside from beats and noise from guidance. Do not panic-sell "
                        "into any individual earnings miss or guidance cut; the trend and structure remain "
                        "constructive. If spreads spike above 600 basis points or unemployment jumps above "
                        "4.5% in a single report, reassess."
                    ),
                    "daily_context": {
                        "headline": (
                            "Expansion regime intact; momentum is accelerating while fear remains elevated, "
                            "so the portfolio call stays risk-on."
                        ),
                        "bullets": [
                            "SPX and QQQ are advancing while VIX remains below 20 → trend confirmation with contained volatility → maintain full positioning",
                            "Fear & Greed remains in Extreme Fear → sentiment is anxious rather than euphoric → do not reduce solely because headlines feel uncomfortable",
                            "HY spreads remain tight near normal levels → credit stress is not confirming a macro break → keep equity risk budget open",
                            "Earnings season is approaching → single-stock misses may create noise → use stock-level rules rather than changing the macro stance",
                        ],
                        "change_status": "NO CHANGE",
                        "watch_triggers": [
                            "If HY spreads widen above 600bps → credit stress confirms → reduce weak/cyclical exposure",
                            "If unemployment jumps above 4.5% in one report → labor break risk rises → reassess full positioning",
                            "If VIX spikes above 28 with SPX losing trend support → risk-off signal strengthens → tighten stops and cut marginal names",
                        ],
                    },
                    "forward_watch": [
                        {
                            "title": "June employment situation, Thursday July 2",
                            "body": "determines whether labor resilience still supports the expansion call.",
                        },
                        {
                            "title": "ISM Services PMI",
                            "body": "checks whether services strength offsets manufacturing caution and keeps earnings risk contained.",
                        },
                        {
                            "title": "Credit spreads and VIX",
                            "body": "the cleanest confirmation pair for whether fear is noise or the start of real de-risking.",
                        },
                        {
                            "title": "Earnings guidance tone",
                            "body": "watch whether misses stay idiosyncratic or start pointing to broad demand deterioration.",
                        },
                    ],
                    "source_note": "Imported 9:10 AM daily brief.",
                }
            )
            return memo
        if today_key != "2026-06-29":
            return None
        memo.update(
            {
                "topline": {
                    "regime": "Expansion",
                    "portfolio_stance": "Risk On",
                    "action": "Maintain Full Positioning",
                    "short_term": "Constructive",
                },
                "why_today": (
                    "Hold your full equity allocation. There is no signal to reduce here. "
                    "The combination of stable growth, a tight labor market, and financial system calm "
                    "argues for staying at your target allocation. Any pullback from fear-driven sentiment "
                    "will be noise, not a trading opportunity."
                ),
                "daily_context": {
                    "headline": (
                        "Michigan sentiment improved to 49.5; PCE inflation sticky at 4.1%; "
                        "jobs resilient last month. Expansion regime confirmed."
                    ),
                    "bullets": [
                        "Michigan Sentiment 49.5 vs 46 prelim → Boost to demand outlook → Consumers adapting to gas prices, rebalance favors consumer staples",
                        "PCE inflation 4.1% YoY (May) vs 3.8% → Inflation stickier, 2yr above Fed target → Watch for July FOMC hold stance",
                        "May NFP +172K beat 85K forecast → Labor resilience intact → Q2 earnings runway extends; wage pressures moderate",
                        "S&P futures +0.8% on Iran ceasefire; tech rotation accelerates → Oil risk premium fell → Asset allocation shift away from mega-cap concentrated positioning",
                    ],
                    "change_status": "NO CHANGE",
                    "watch_triggers": [
                        "If June jobs report (July 2) < 100K → labor softening signal; monitor consumer spending weakness in H2",
                        "If PCE stays above 4% in July → inflation persistence extends Fed pause; avoid duration-heavy risk",
                        "If S&P breaks below 7,300 on tech selloff → check VIX and credit spreads for risk-off confirmation",
                    ],
                },
                "forward_watch": [
                    {
                        "title": "June employment situation, Thursday July 2",
                        "body": "determines Fed pause duration; weak print triggers rate-cut repricing and volatility spike.",
                    },
                    {
                        "title": "ISM Services PMI (June data), July 1",
                        "body": "gauges whether services strength offsets manufacturing caution.",
                    },
                    {
                        "title": "Personal Income and Outlays (June), July 30",
                        "body": "tracks consumer spending resilience and whether energy costs are flowing through.",
                    },
                    {
                        "title": "Fed July 28-29 meeting and rate decision July 29",
                        "body": "market pricing for hold versus hike risk determines whether the expansion call remains clean.",
                    },
                ],
                "source_note": "Imported 9:10 AM daily brief.",
            }
        )
        return memo

    def _regime_ai_payload(d, s, crypto):
        fg = d.get("fg")
        crypto_scored = _score_crypto(crypto) if crypto and crypto.get("price") is not None else {}
        return {
            "schema_version": REGIME_DAILY_MEMO_SCHEMA_VERSION,
            "date": regime_daily_key(),
            "regime": s.get("regime_layer"),
            "portfolio_stance": s.get("portfolio_stance"),
            "action": s.get("action_guidance"),
            "short_term": s.get("short_term_cond"),
            "opportunity": {
                "window": s.get("opportunity_window"),
                "score": s.get("opportunity_score"),
                "drivers": s.get("opportunity_drivers"),
                "risks": s.get("opportunity_risks"),
                "reason": s.get("opportunity_reason"),
                "key_risk": s.get("key_risk"),
            },
            "execution": {
                "window": s.get("execution_window"),
                "detail": s.get("execution_detail"),
            },
            "signals": {
                "T1_ISM": {"state": s.get("t1"), "detail": s.get("t1_detail"), "value": d.get("ism")},
                "T2_unemployment": {"state": s.get("t2"), "detail": s.get("t2_detail"), "value": d.get("unemp"), "previous": d.get("unemp_prev")},
                "T3_HY_OAS": {"state": s.get("t3"), "detail": s.get("t3_detail"), "bps": d.get("hy_bps")},
                "yield_curve": {"state": s.get("yc"), "detail": s.get("yc_detail"), "bps": d.get("yc_bps")},
                "liquidity": {"state": s.get("liq_status"), "detail": s.get("liq_detail"), "numbers": s.get("liq_numbers")},
            },
            "market_highlights": {
                "spx": d.get("spx"),
                "spx_change": d.get("spx_change"),
                "spx_vs20": d.get("spx_vs20"),
                "spx_vs50": d.get("spx_vs50"),
                "spx_ret5": d.get("spx_ret5"),
                "spx_ret20": d.get("spx_ret20"),
                "qqq": d.get("qqq"),
                "qqq_change": d.get("qqq_change"),
                "vix": d.get("vix"),
                "vix_change": d.get("vix_change"),
                "vix_peak5": d.get("vix_peak5"),
                "vix_drop5": d.get("vix_drop5"),
                "fear_greed": fg,
                "fear_greed_label": d.get("fg_label"),
            },
            "crypto": {
                "btc_price": crypto.get("price"),
                "btc_change": crypto.get("change"),
                "btc_vs_200d": crypto.get("btc_vs_200"),
                "btc_vs_20d": crypto.get("btc_vs_20"),
                "eth_price": crypto.get("eth_price"),
                "eth_change": crypto.get("eth_change"),
                "ethbtc_change": crypto.get("ethbtc_change"),
                "fear_greed": (crypto.get("fg") or {}).get("value"),
                "fear_greed_label": (crypto.get("fg") or {}).get("label"),
                "score": crypto_scored,
            },
            "deterministic_watch_items": _forward_watch_items(d, s, crypto),
        }

    def _parse_regime_json(text):
        text = str(text or "").strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
        return json.loads(text)

    def _validate_regime_memo(memo):
        if not isinstance(memo, dict):
            return False
        if memo.get("schema_version") != REGIME_DAILY_MEMO_SCHEMA_VERSION:
            return False
        daily = memo.get("daily_context")
        forward = memo.get("forward_watch")
        signal_explanations = memo.get("signal_explanations")
        crypto_memo = memo.get("crypto")
        if not str(memo.get("why_today") or "").strip():
            return False
        if not isinstance(daily, dict) or not str(daily.get("headline") or "").strip():
            return False
        if not isinstance(daily.get("bullets"), list) or len(daily["bullets"]) < 3:
            return False
        if not isinstance(daily.get("watch_triggers"), list) or len(daily["watch_triggers"]) < 2:
            return False
        if not isinstance(forward, list) or len(forward) < 3:
            return False
        if not isinstance(signal_explanations, dict) or len(signal_explanations) < 5:
            return False
        if crypto_memo is not None and not isinstance(crypto_memo, dict):
            return False
        return True

    def _regime_daily_topline(s, daily_memo):
        memo_topline = (daily_memo or {}).get("topline") if isinstance(daily_memo, dict) else None
        memo_topline = memo_topline if isinstance(memo_topline, dict) else {}
        return {
            "regime": str(memo_topline.get("regime") or s.get("regime_layer") or ""),
            "portfolio_stance": str(memo_topline.get("portfolio_stance") or s.get("portfolio_stance") or ""),
            "action": str(memo_topline.get("action") or s.get("action_guidance") or ""),
            "short_term": str(memo_topline.get("short_term") or s.get("short_term_cond") or ""),
        }

    def _call_claude_regime_daily_memo(d, s, crypto, api_key):
        from anthropic import Anthropic
        from pm_view import _call_with_timeout
        client = Anthropic(api_key=api_key)
        payload = _regime_ai_payload(d, s, crypto)
        prompt = f"""
You are generating the daily memo for an institutional-style Market Regime & Risk Engine.

Use ONLY the provided dashboard data. Do not invent fresh news, dates, economic releases, earnings, geopolitical events, or price levels that are not in the data. If a specific event is not provided, write the implication from the signal itself.

Decision hierarchy:
- The 2-12 week Opportunity Window is the primary actionable layer for new broad long exposure.
- The 1-4 year macro regime is context only; do not let it override the opportunity window.
- The final action must be one of Enter, Wait, Hold Off, or Avoid.
- Execution timing can refine entries, but should not change the opportunity-window conclusion.

The app renders this in the exact dashboard sections:
1. Why Today — one dense paragraph for the hero card.
2. Today's Context — event -> market impact -> portfolio implication.
3. Watch Triggers — concrete conditions that would change the call.
4. Forward Watch — next 1-7 day monitoring checklist.
5. Signal Cards — short explanations for T1, T2, T3, Yield Curve, and Liquidity.
6. Crypto Regime — daily interpretation of BTC/ETH/BTC rotation using the provided crypto data.

Style:
- Specific, direct, PM-memo tone.
- Use arrows (→) inside the Today's Context bullets.
- Keep bullets concise but information-rich.
- Respect the rules engine's regime/action. Do not override it.
- Mention crypto only when the provided crypto data matters to risk appetite or speculative appetite.

Dashboard data:
{json.dumps(payload, indent=2, default=str)}

Return ONLY this JSON shape:
{{
  "schema_version": {REGIME_DAILY_MEMO_SCHEMA_VERSION},
  "topline": {{
    "regime": "{s.get("regime_layer")}",
    "portfolio_stance": "{s.get("portfolio_stance")}",
    "action": "{s.get("action_guidance")}",
    "short_term": "{s.get("short_term_cond")}"
  }},
  "why_today": "one paragraph, 60-95 words. Explain the opportunity window first, then the key risk.",
  "daily_context": {{
    "headline": "one sentence summarizing today's macro/market context",
    "bullets": [
      "signal/event → market impact → portfolio implication",
      "signal/event → market impact → portfolio implication",
      "signal/event → market impact → portfolio implication",
      "optional fourth bullet"
    ],
    "change_status": "NO CHANGE or WATCH or RISK UP or RISK DOWN",
    "watch_triggers": [
      "If ... → ... → ...",
      "If ... → ... → ...",
      "If ... → ... → ..."
    ]
  }},
  "forward_watch": [
    {{"title": "specific thing to watch", "body": "why it matters"}},
    {{"title": "specific thing to watch", "body": "why it matters"}},
    {{"title": "specific thing to watch", "body": "why it matters"}},
    {{"title": "specific thing to watch", "body": "why it matters"}}
  ],
  "signal_explanations": {{
    "T1 · ISM": "one sentence explaining the current state and portfolio implication",
    "T2 · Unemployment": "one sentence explaining the current state and portfolio implication",
    "T3 · HY OAS": "one sentence explaining the current state and portfolio implication",
    "Yield curve": "one sentence explaining the current state and portfolio implication",
    "Liquidity": "one sentence explaining the current state and portfolio implication"
  }},
  "crypto": {{
    "narrative": [
      {{"title": "Trend", "body": "daily interpretation of BTC trend"}},
      {{"title": "Opportunity", "body": "daily interpretation of add/risk timing"}},
      {{"title": "Positioning", "body": "daily interpretation of BTC versus ETH/alts"}},
      {{"title": "Conviction", "body": "daily interpretation of cycle and sizing"}}
    ]
  }}
}}
"""
        message = _call_with_timeout(
            lambda: anthropic_messages_create(
                client,
                max_tokens=1800,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            ),
            REGIME_CLAUDE_TIMEOUT_SECONDS,
            "Claude market regime daily memo",
        )
        text = message.content[0].text.strip()
        memo = _parse_regime_json(text)
        if not _validate_regime_memo(memo):
            raise ValueError("Claude regime memo did not include required sections.")
        memo["source_note"] = "Claude daily memo."
        return memo

    def _get_regime_daily_memo(d, s, crypto, api_key, force=False):
        today_key = regime_daily_key()
        cache = st.session_state.store.setdefault("regime_daily_cache", {})
        seeded_memo = _daily_brief_seed_memo(d, s, crypto, today_key)
        if seeded_memo and _validate_regime_memo(seeded_memo):
            seed_ts = regime_daily_anchor().isoformat(timespec="seconds")
            cache[today_key] = {
                "ts": seed_ts,
                "source": "9:10 daily brief",
                "memo": seeded_memo,
            }
            save_store(st.session_state.store)
            return {"memo": seeded_memo, "source": "9:10 daily brief", "ts": seed_ts, "date": today_key}
        entry = cache.get(today_key) if isinstance(cache, dict) else None
        if entry and not force:
            memo = entry.get("memo") or {}
            source = str(entry.get("source") or "")
            is_failed_rule_fallback = source.startswith("rule fallback") and "Claude failed" in source
            if _validate_regime_memo(memo) and not (api_key and is_failed_rule_fallback):
                return {
                    "memo": memo,
                    "source": source or "cached",
                    "ts": entry.get("ts"),
                    "date": today_key,
                }
        if not api_key:
            memo = _fallback_regime_daily_memo(d, s, crypto)
            cache[today_key] = {
                "ts": now_market_time().isoformat(timespec="seconds"),
                "source": "rule fallback · no Claude key",
                "memo": memo,
            }
            save_store(st.session_state.store)
            return {"memo": memo, "source": "rule fallback · no Claude key", "ts": cache[today_key]["ts"], "date": today_key}
        try:
            memo = _call_claude_regime_daily_memo(d, s, crypto, api_key)
            cache[today_key] = {
                "ts": now_market_time().isoformat(timespec="seconds"),
                "source": "claude daily",
                "memo": memo,
            }
            save_store(st.session_state.store)
            st.session_state["claude_calls_this_session"] = (
                st.session_state.get("claude_calls_this_session", 0) + 1
            )
            return {"memo": memo, "source": "claude daily", "ts": cache[today_key]["ts"], "date": today_key}
        except Exception as exc:
            if entry and _validate_regime_memo(entry.get("memo") or {}):
                return {
                    "memo": entry.get("memo") or {},
                    "source": f"{entry.get('source') or 'cached'} · Claude refresh failed",
                    "ts": entry.get("ts"),
                    "date": today_key,
                    "error": str(exc)[:160],
                }
            memo = _fallback_regime_daily_memo(d, s, crypto)
            cache[today_key] = {
                "ts": now_market_time().isoformat(timespec="seconds"),
                "source": f"rule fallback · Claude failed: {str(exc)[:80]}",
                "memo": memo,
            }
            save_store(st.session_state.store)
            return {"memo": memo, "source": cache[today_key]["source"], "ts": cache[today_key]["ts"], "date": today_key, "error": str(exc)[:160]}

    def _email_hex(status, default="#5B5BAD"):
        label = str(status or "").lower()
        if any(word in label for word in ("clear", "expansion", "risk on", "maintain", "constructive", "healthy", "tight", "calm", "improving", "favorable", "enter", "acceptable", "add weakness")):
            return "#2A6E46"
        if any(word in label for word in ("warning", "firing", "risk off", "raise", "reduce", "stress", "extreme", "broken", "unfavorable", "avoid")):
            return "#8B2A2A"
        if any(word in label for word in ("approaching", "defensive", "neutral", "mixed", "flat", "elevated", "watch", "wait", "hold off", "pullback")):
            return "#9B6214"
        return default

    def _email_badge(label, status=None):
        color = _email_hex(status or label)
        if color == "#2A6E46":
            bg, border = "#EAF4EE", "#A8D4B8"
        elif color == "#8B2A2A":
            bg, border = "#FAF0EE", "#D49090"
        else:
            bg, border = "#FDF6E3", "#D4B86A"
        return (
            f'<span style="display:inline-block;padding:4px 10px;border-radius:5px;'
            f'font-size:12px;font-weight:800;background:{bg};color:{color};border:1px solid {border};">{html.escape(str(label))}</span>'
        )

    def _email_context_status_style(status):
        color = _email_hex(status)
        if color == "#2A6E46":
            return "#EAF4EE", "#2A6E46", "#A8D4B8"
        if color == "#8B2A2A":
            return "#FAF0EE", "#8B2A2A", "#D49090"
        return "#FDF6E3", "#9B6214", "#D4B86A"

    def _email_change_hex(value, inverse=False):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "#8888A4"
        if inverse:
            value = -value
        if value > 0:
            return "#2A6E46"
        if value < 0:
            return "#8B2A2A"
        return "#8888A4"

    def _email_fear_greed_hex(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "#8888A4"
        if value < 25:
            return "#8B2A2A"
        if value < 45:
            return "#9B6214"
        if value <= 70:
            return "#2A6E46"
        return "#9B6214"

    def _email_forward_rows(items):
        rows = []
        for item in (items or [])[:5]:
            if isinstance(item, dict):
                title = str(item.get("title") or "Watch item")
                body = str(item.get("body") or "")
            else:
                raw = str(item or "")
                if "→" in raw:
                    title, body = [part.strip() for part in raw.split("→", 1)]
                else:
                    title, body = raw, ""
            rows.append(
                '<div style="padding:10px 0;border-bottom:1px solid #F0EEF8;font-size:15px;line-height:1.5;">'
                f'<strong style="color:#1A1A2E;">{html.escape(title)}</strong>'
                f' <span style="color:#9B7EC8;">→</span> <span style="color:#55556A;">{html.escape(body)}</span></div>'
            )
        return "".join(rows) or '<div style="font-size:13px;color:#AAA;padding:6px 0;">No forward watch available.</div>'

    def _build_regime_email_html(d, s, crypto, daily_memo, snapshot_ts, snapshot_label, memo_label):
        topline = _regime_daily_topline(s, daily_memo)
        daily_context = daily_memo.get("daily_context") or {}
        context_headline = str(daily_context.get("headline") or "")
        context_bullets = daily_context.get("bullets") or []
        context_triggers = daily_context.get("watch_triggers") or []
        change_status = str(daily_context.get("change_status") or "NO CHANGE").upper()
        status_bg, status_color, status_border = _email_context_status_style(change_status)
        forward_rows = _email_forward_rows(daily_memo.get("forward_watch") or _default_forward_watch(d, s, crypto))
        why_today = str(daily_memo.get("why_today") or _why_today_text(d, s))
        date_str = format_market_time(snapshot_ts, "%a, %b %d %Y")
        spx = _fmt_regime(d.get("spx"), "", 0)
        spx_chg = _signed_regime(d.get("spx_change"))
        qqq = _fmt_regime(d.get("qqq"), "", 2)
        qqq_chg = _signed_regime(d.get("qqq_change"))
        vix = _fmt_regime(d.get("vix"), "", 1)
        fg = f'{_fmt_regime(d.get("fg"), "", 0)} {d.get("fg_label") or ""}'.strip()
        alert_bar = (
            '<div style="background:#8B2A2A;padding:12px 22px;text-align:center;font-size:14px;font-weight:800;color:#fff;">'
            f'Alert: {html.escape(" · ".join(s.get("alerts") or []))}</div>'
            if s.get("alerts") else ""
        )

        bullet_html = "".join(
            '<div style="padding:8px 0;border-bottom:1px solid #F0EEF8;font-size:15px;color:#44445A;line-height:1.55;">'
            f'<span style="color:#6B61B3;">·</span> {html.escape(str(item))}</div>'
            for item in context_bullets[:5]
        )
        trigger_html = "".join(
            '<div style="padding:3px 0;font-size:13px;color:#9B6214;line-height:1.5;">'
            f'→ {html.escape(str(item))}</div>'
            for item in context_triggers[:4]
        )

        crypto_block = ""
        if crypto and crypto.get("price") is not None:
            scored = _score_crypto(crypto)
            memo_crypto = daily_memo.get("crypto") or {}
            memo_rows = memo_crypto.get("narrative") if isinstance(memo_crypto, dict) else None
            conviction = ""
            if isinstance(memo_rows, list) and memo_rows:
                conviction = str((memo_rows[-1] or {}).get("body") or "")
            btc_vs_200 = crypto.get("btc_vs_200")
            btc_color = "#2A6E46" if btc_vs_200 is not None and btc_vs_200 >= 0 else "#8B2A2A"
            def verdict_cell(label, verdict):
                answer, _note, color_key = verdict
                color = {"green": "#2A6E46", "red": "#8B2A2A", "yellow": "#9B6214"}.get(color_key, "#5B5BAD")
                bg = {"green": "#EAF4EE", "red": "#FAF0EE", "yellow": "#FDF6E3"}.get(color_key, "#F0ECF8")
                border = {"green": "#A8D4B8", "red": "#D4A8A8", "yellow": "#D4B86A"}.get(color_key, "#D9D2EA")
                return (
                    f'<td style="background:{bg};border:1px solid {border};border-radius:5px;padding:7px 8px;text-align:center;">'
                    f'<div style="font-size:10px;color:{color};font-weight:800;letter-spacing:1px;text-transform:uppercase;opacity:.75;">{html.escape(label)}</div>'
                    f'<div style="font-size:13px;color:{color};font-weight:900;">{html.escape(str(answer))}</div></td>'
                )
            crypto_block = (
                '<div class="card"><div class="card-pad">'
                '<div class="lbl">Crypto</div>'
                '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;margin-bottom:12px;width:100%;">'
                '<tr>'
                f'<td style="padding:0 14px 0 0;white-space:nowrap;"><span style="font-size:18px;font-weight:900;color:#1A1A2E;">BTC ${crypto.get("price"):,.0f}</span>'
                f' <span style="font-size:12px;font-weight:800;color:{btc_color};">{_signed_regime(crypto.get("btc_vs_200"))} vs 200d</span></td>'
                f'<td style="padding:0 14px;font-size:16px;color:#1A1A2E;"><strong>ETH {"$" + _fmt_regime(crypto.get("eth_price"), "", 0) if crypto.get("eth_price") else "—"}</strong></td>'
                f'<td style="text-align:right;"><span style="font-size:11px;padding:4px 9px;border-radius:4px;background:#F0ECF8;color:#5B3F8A;font-weight:900;">{html.escape(str(scored["four"][1]).upper())}</span></td>'
                '</tr></table>'
                '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:separate;border-spacing:6px 0;table-layout:fixed;margin-bottom:10px;"><tr>'
                + verdict_cell("Trend", scored["q1"])
                + verdict_cell("Add?", scored["q2"])
                + verdict_cell("Lean", scored["q3"])
                + '</tr></table>'
                f'<div style="font-size:12px;color:#8888A4;margin-top:8px;">Alignment: <strong style="color:#5B3F8A;">{html.escape(str(scored["alignment"]))}</strong></div>'
                + (f'<div style="font-size:15px;color:#333;line-height:1.65;font-style:italic;border-top:1px solid #F4F2FB;padding-top:10px;margin-top:10px;">{html.escape(conviction)}</div>' if conviction else "")
                + '</div></div>'
            )

        return (
            '<!DOCTYPE html><html><head><meta charset="UTF-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
            '<title>Market Decision Brief</title>'
            '<style>'
            'body{margin:0;padding:0;background:#F4F2FB;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;}'
            '.wrap{max-width:620px;margin:0 auto;background:#F4F2FB;}'
            '.card{background:#fff;border-radius:12px;margin:12px 16px;overflow:hidden;}'
            '.card-pad{padding:18px 22px;}'
            '.lbl{font-size:10px;font-weight:900;letter-spacing:2.5px;text-transform:uppercase;color:#9B7EC8;margin-bottom:12px;}'
            '@media(max-width:480px){.card{margin:9px 10px;}.card-pad{padding:15px 16px;}}'
            '</style></head><body><div class="wrap">'
            '<div style="background:#5B3F8A;padding:20px 22px 16px;text-align:center;">'
            '<div style="font-size:11px;font-weight:800;letter-spacing:3px;text-transform:uppercase;color:#D8CEEF;margin-bottom:5px;">Market Decision Brief</div>'
            f'<div style="font-size:14px;color:#D8CEEF;">{html.escape(date_str)}</div></div>'
            + alert_bar
            + '<div class="card"><div class="card-pad"><div class="lbl">Morning Briefing</div>'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;margin-bottom:14px;">'
            '<tr>'
            f'<td style="padding:0 14px 12px 0;width:50%;vertical-align:top;border-right:1px solid #F0EEF8;"><div style="font-size:10px;font-weight:900;letter-spacing:1.8px;text-transform:uppercase;color:#9B7EC8;margin-bottom:4px;">Regime</div><div style="font-size:21px;font-weight:900;color:{_email_hex(topline.get("regime"))};line-height:1.1;">{html.escape(str(topline.get("regime")).title())}</div></td>'
            f'<td style="padding:0 0 12px 14px;width:50%;vertical-align:top;"><div style="font-size:10px;font-weight:900;letter-spacing:1.8px;text-transform:uppercase;color:#9B7EC8;margin-bottom:4px;">Opportunity Window</div><div style="font-size:21px;font-weight:900;color:{_email_hex(topline.get("portfolio_stance"))};line-height:1.1;">{html.escape(str(topline.get("portfolio_stance")))}</div></td>'
            '</tr><tr>'
            f'<td style="padding:12px 14px 0 0;vertical-align:top;border-right:1px solid #F0EEF8;border-top:1px solid #F0EEF8;"><div style="font-size:10px;font-weight:900;letter-spacing:1.8px;text-transform:uppercase;color:#9B7EC8;margin-bottom:4px;">Action</div><div style="font-size:21px;font-weight:900;color:{_email_hex(topline.get("action"))};line-height:1.1;">{html.escape(str(topline.get("action")))}</div></td>'
            f'<td style="padding:12px 0 0 14px;vertical-align:top;border-top:1px solid #F0EEF8;"><div style="font-size:10px;font-weight:900;letter-spacing:1.8px;text-transform:uppercase;color:#9B7EC8;margin-bottom:4px;">Execution</div><div style="font-size:21px;font-weight:900;color:{_email_hex(topline.get("short_term"))};line-height:1.1;">{html.escape(str(topline.get("short_term")))}</div></td>'
            '</tr></table>'
            f'<div style="font-size:16px;color:#1A1A2E;line-height:1.75;border-top:1px solid #F4F2FB;padding-top:12px;">{html.escape(why_today)}</div>'
            '</div></div>'
            '<div class="card"><div class="card-pad"><div class="lbl">Today\'s Market</div>'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;margin-bottom:14px;"><tr>'
            f'<td style="padding:0 8px 8px 0;width:50%;font-size:15px;color:#666;"><span style="font-size:19px;font-weight:900;color:#1A1A2E;">SPX {html.escape(spx)}</span> <span style="font-size:13px;font-weight:800;color:{_email_change_hex(d.get("spx_change"))};">{html.escape(spx_chg)}</span></td>'
            f'<td style="padding:0 0 8px 8px;width:50%;font-size:15px;color:#666;text-align:right;">VIX <strong style="color:#1A1A2E;">{html.escape(vix)}</strong> &nbsp; F&amp;G <strong style="color:{_email_fear_greed_hex(d.get("fg"))};">{html.escape(fg)}</strong></td>'
            '</tr><tr>'
            f'<td style="font-size:14px;color:#666;">QQQ <strong style="color:#1A1A2E;">{html.escape(qqq)}</strong> <span style="color:{_email_change_hex(d.get("qqq_change"))};">{html.escape(qqq_chg)}</span></td>'
            f'<td style="font-size:12px;color:#888;text-align:right;">Data: {html.escape(snapshot_label)} · Memo: {html.escape(memo_label)}</td>'
            '</tr></table>'
            '<div style="border-top:1px solid #F0EEF8;margin-top:12px;padding-top:14px;">'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;margin-bottom:10px;"><tr>'
            '<td><div class="lbl" style="margin-bottom:0;">Today\'s Context</div></td>'
            f'<td style="text-align:right;"><span style="font-size:11px;font-weight:900;padding:4px 9px;border-radius:5px;background:{status_bg};color:{status_color};border:1px solid {status_border};white-space:nowrap;">{html.escape(change_status)}</span></td>'
            '</tr></table>'
            f'<div style="font-size:16px;font-weight:800;color:#1A1A2E;line-height:1.55;margin-bottom:10px;">{html.escape(context_headline)}</div>'
            f'{bullet_html}'
            + (f'<div style="margin-top:8px;">{trigger_html}</div>' if trigger_html else "")
            + '</div>'
            '<div style="border-top:1px solid #F0EEF8;margin-top:14px;padding-top:14px;">'
            '<div class="lbl">Forward Watch · next 1-7 days</div>'
            f'{forward_rows}</div></div></div>'
            '<div class="card" style="background:#1A1035;"><div class="card-pad">'
            '<div class="lbl" style="color:#A98DDB;">The Call</div>'
            f'<div style="font-size:21px;font-weight:950;color:#fff;line-height:1.3;margin-bottom:5px;">{html.escape(str(s.get("portfolio_stance")))} — {html.escape(str(s.get("action_guidance")))}</div>'
            f'<div style="font-size:14px;color:#BDB7D5;margin-bottom:14px;">{html.escape(str(s.get("regime_layer")).title())} regime</div>'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:separate;border-spacing:8px 0;border-top:1px solid #2A2550;padding-top:14px;margin-top:6px;width:100%;"><tr>'
            f'<td style="width:33%;">{_email_badge("T1 " + str(s.get("t1")), s.get("t1"))}</td>'
            f'<td style="width:33%;">{_email_badge("T2 " + str(s.get("t2")), s.get("t2"))}</td>'
            f'<td style="width:33%;">{_email_badge("T3 " + str(s.get("t3")), s.get("t3"))}</td>'
            '</tr></table></div></div>'
            + crypto_block
            + '<div style="text-align:center;padding:22px 16px 30px;">'
            '<a href="https://tradingdesk.streamlit.app/?view=regime" style="display:inline-block;background:#5B3F8A;color:#fff;text-decoration:none;font-size:15px;font-weight:800;padding:14px 30px;border-radius:9px;">Full Dashboard →</a>'
            '<div style="margin-top:14px;font-size:12px;color:#9B9BB4;">Trading Desk · Market Regime & Risk Engine · Not financial advice</div>'
            '</div></div></body></html>'
        )

    regime_daily = _get_regime_daily_memo(
        d,
        s,
        crypto,
        get_effective_api_key(),
        force=bool(st.session_state.pop("force_regime_daily_memo", False)),
    )
    daily_memo = regime_daily["memo"]
    daily_topline = _regime_daily_topline(s, daily_memo)
    memo_ts = regime_daily.get("ts") or snap["updated_at"]
    try:
        memo_label = format_market_time(memo_ts, "%b %d · %-I:%M %p %Z")
    except Exception:
        memo_label = "cached"

    if s["alerts"]:
        st.markdown(
            '<div class="regime-panel regime-pad" style="border-color:rgba(209,69,69,.45);margin-bottom:18px;">'
            f'<span class="regime-section-title" style="color:var(--color-negative);">Alert</span>{html.escape(" · ".join(s["alerts"]))}</div>',
            unsafe_allow_html=True,
        )
    snapshot_label = format_market_time(snap["updated_at"], "%a %b %d, %-I:%M %p %Z")
    email_html = _build_regime_email_html(d, s, crypto, daily_memo, snap["updated_at"], snapshot_label, memo_label)

    highlights = [
        ("S&P 500", _fmt_regime(d["spx"], "", 0), _change_html(d["spx_change"])),
        ("Nasdaq 100 (QQQ)", _fmt_regime(d["qqq"], "", 2), _change_html(d["qqq_change"])),
        ("VIX", _fmt_regime(d["vix"], "", 1), f'<span class="risk-change" style="color:{_regime_value_color(d["vix_change"], inverse=True)};">{html.escape(_vix_label(d["vix"]))}</span>'),
        ("Fear & Greed", _fmt_regime(d["fg"], "", 0), f'<span class="risk-change" style="color:{_fear_greed_color(d.get("fg"))};">{html.escape(d.get("fg_label") or "—")}</span>'),
        ("HY Spreads", _fmt_regime(d["hy_bps"], "bps", 0), f'<span class="risk-change" style="color:{sev_color.get(s["t3"], "var(--color-muted)")};">{html.escape(_hy_label(d["hy_bps"]))}</span>'),
    ]
    highlight_html = "".join(
        f'<div class="risk-market-item"><span class="name">{html.escape(k)}</span><strong>{html.escape(v)}{extra}</strong></div>'
        for k, v, extra in highlights
    )
    memo_forward = daily_memo.get("forward_watch") or _default_forward_watch(d, s, crypto)
    forward_html = "".join(
        f'<div class="forward-watch-row"><div class="idx">{idx}</div><div><strong>{html.escape(str((item or {}).get("title") or "Watch item"))}</strong> → {html.escape(str((item or {}).get("body") or ""))}</div></div>'
        for idx, item in enumerate(memo_forward[:4], 1)
    )
    daily_context = daily_memo.get("daily_context") or {}
    fallback_headline, fallback_bullets, fallback_triggers = _market_implication_static()
    impact_headline = daily_context.get("headline") or fallback_headline
    impact_bullets = daily_context.get("bullets") or fallback_bullets
    watch_triggers = daily_context.get("watch_triggers") or fallback_triggers
    change_status = daily_context.get("change_status") or "NO CHANGE"
    impact_bullets_html = "".join(
        f'<div class="market-imp-bullet"><span class="market-imp-dot"></span><span>{html.escape(item)}</span></div>'
        for item in impact_bullets
    )
    watch_triggers_html = "".join(
        f'<div class="watch-trigger"><span class="arrow">→</span><span>{html.escape(item)}</span></div>'
        for item in watch_triggers
    )
    change_display = {
        "NO CHANGE": "Unchanged",
        "WATCH": "Watch",
        "RISK UP": "Risk Up",
        "RISK DOWN": "Risk Down",
        "MINOR IMPROVEMENT": "Minor Improvement",
        "RISK INCREASING": "Risk Increasing",
        "REGIME SHIFT WARNING": "Regime Shift Warning",
    }.get(str(change_status).upper(), str(change_status).title())
    change_note = daily_context.get("change_note") or daily_context.get("summary") or impact_headline
    opportunity_takeaway = s.get("opportunity_takeaway") or daily_memo.get("why_today") or _why_today_text(d, s)
    opportunity_explanation = s.get("opportunity_explanation") or ""
    key_risk = s.get("key_risk") or "A reversal in volatility, breadth, or credit could weaken the window."
    dark_highlight_html = "".join(
        f'<div class="risk-op-highlight-row"><span>{html.escape(k)}</span><strong>{html.escape(v)}{extra}</strong></div>'
        for k, v, extra in highlights
    )
    st.markdown(
        '<div class="risk-engine-page">'
        f'<div class="risk-engine-title">Market Regime &amp; Risk Engine</div>'
        f'<div class="risk-engine-snapshot">Data snapshot: {html.escape(snapshot_label)} · Daily memo: {html.escape(regime_daily.get("source") or "cached")} · {html.escape(memo_label)}</div>'
        '<div class="risk-opportunity-card">'
        '<div class="risk-op-top">'
        '<div class="risk-op-cell">'
        '<div class="risk-op-label">Opportunity Decision · 2-12 weeks</div>'
        '<div style="display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;">'
        f'<span class="risk-op-main" style="color:{_email_hex(daily_topline["action"])};">{html.escape(str(daily_topline["action"]).upper())}</span>'
        f'<span style="font-size:15px;font-weight:900;color:{_email_hex(daily_topline["portfolio_stance"])};letter-spacing:.08em;text-transform:uppercase;">{html.escape(str(daily_topline["portfolio_stance"]).upper())}</span>'
        '</div>'
        '</div>'
        '<div class="risk-op-cell">'
        '<div class="risk-op-label">Entry Timing · days-2 weeks</div>'
        f'<div class="risk-op-main" style="color:{_email_hex(daily_topline["short_term"])};">{html.escape(str(daily_topline["short_term"]).upper())}</div>'
        '</div>'
        '<div class="risk-op-cell">'
        '<div class="risk-op-label">Change Since Yesterday</div>'
        f'<div class="risk-op-main" style="color:{_email_hex(change_display)};text-transform:capitalize;">{html.escape(change_display)}</div>'
        f'<div class="risk-op-sub">{html.escape(str(change_note)[:120])}</div>'
        '</div>'
        '<div class="risk-op-cell">'
        '<div class="risk-op-label">Background Context</div>'
        '<div class="risk-op-context">'
        f'<span style="color:{_email_hex(s.get("regime_layer"))};">Cyclical · 1-4y: {html.escape(str(s.get("regime_layer") or daily_topline["regime"]))}</span>'
        '<span style="color:var(--color-warning);">Secular · 10-20y: Late / Narrow</span>'
        '</div>'
        '</div>'
        '</div>'
        '<div class="risk-op-bottom">'
        '<div>'
        '<div class="risk-op-label">Why Today</div>'
        f'<div class="risk-op-why"><strong>{html.escape(str(opportunity_takeaway))}</strong> {html.escape(str(opportunity_explanation))} <span style="color:var(--color-muted);">Key risk: {html.escape(str(key_risk))}</span></div>'
        '</div>'
        f'<div class="risk-op-highlights"><div class="risk-op-label">Market Highlights</div>{dark_highlight_html}</div>'
        '</div>'
        '</div>'
        '<div class="risk-brief-card">'
        '<div class="risk-brief-pad">'
        '<div class="risk-brief-label">Today\'s Market</div>'
        f'<div class="risk-market-grid">{highlight_html}</div>'
        '<div class="risk-market-context">'
        '<div class="risk-card-head" style="padding:0 0 14px;background:transparent;border-bottom:1px solid #D7DFEA;">'
        f'<div><span class="risk-card-title">Today\'s Context</span><span class="risk-card-sub">Event → Market impact → Portfolio implication</span></div><span class="risk-badge">{html.escape(str(change_status).upper())}</span></div>'
        '<div class="market-imp-body" style="border-bottom:1px solid #D7DFEA;">'
        f'<div class="market-imp-main" style="padding-left:0;"><div class="market-imp-headline">{html.escape(impact_headline)}</div>{impact_bullets_html}</div>'
        f'<div class="market-imp-side"><div class="risk-card-title" style="margin-bottom:16px;">Watch Triggers</div>{watch_triggers_html}</div>'
        '</div>'
        '<div style="padding-top:18px;">'
        '<div class="risk-card-title">Forward Watch · next 1-7 days</div>'
        f'<div class="forward-watch-body" style="padding:10px 0 0;">{forward_html}</div>'
        '</div>'
        '</div></div></div>'
        '<div class="risk-secondary">',
        unsafe_allow_html=True,
    )

    rows = [
        ("T1 · ISM", "Manufacturing expansion vs contraction", s["t1"], s["t1_detail"]),
        ("T2 · Unemployment", "Labor-market stress confirmation", s["t2"], s["t2_detail"]),
        ("T3 · HY OAS", "Credit-spread tail-risk gauge", s["t3"], s["t3_detail"]),
        ("Yield curve", "Growth / recession pricing backdrop", s["yc"], s["yc_detail"]),
        ("Liquidity", "Fed, RRP, and Treasury cash impulse", s["liq_status"], f'{s["liq_detail"]} Fed {s["liq_numbers"]["Fed"] if s["liq_numbers"]["Fed"] is not None else "—"}B · RRP {s["liq_numbers"]["RRP"] if s["liq_numbers"]["RRP"] is not None else "—"}B · TGA {s["liq_numbers"]["TGA"] if s["liq_numbers"]["TGA"] is not None else "—"}B'),
    ]
    signal_explanations = daily_memo.get("signal_explanations") or {}
    st.markdown(
        '<div class="regime-signal-cards">'
        + "".join(
            f'<div class="regime-signal-card">'
            f'<div class="name">{html.escape(label)}</div>'
            f'<div class="metric-sub">{html.escape(subtitle)}</div>'
            f'<div class="state" style="color:{sev_color.get(status, "var(--color-muted)")};">{html.escape(status)}</div>'
            f'<div class="copy">{html.escape(str(signal_explanations.get(label) or text))}</div>'
            f'</div>'
            for label, subtitle, status, text in rows
        )
        + '</div>',
        unsafe_allow_html=True,
    )

    def _framework_state_class(name, current=False):
        label = str(name or "").lower()
        classes = []
        if any(x in label for x in ("risk on", "expansion", "recovery", "rebuild", "early", "clear")):
            classes.append("good")
        elif any(x in label for x in ("risk off", "rolling", "contraction", "secular bear", "reduce")):
            classes.append("bad")
        else:
            classes.append("warn")
        if current:
            classes.append("current")
        return " ".join(classes)

    def _state_box(title, copy, action, current=False):
        return (
            f'<div class="framework-state {_framework_state_class(title, current)}">'
            f'<div class="state-title">{html.escape(title)}{" ◂ current" if current else ""}</div>'
            f'<div class="state-copy">{html.escape(copy)}</div>'
            f'<div class="action">→ {html.escape(action)}</div>'
            '</div>'
        )

    t1_value = f'{_fmt_regime(d.get("ism"), "%", 1)}' if d.get("ism") is not None else "—"
    t2_value = f'{_fmt_regime(d.get("unemp"), "%", 1)}' if d.get("unemp") is not None else "—"
    t3_value = _fmt_regime(d.get("hy_bps"), "bps", 0)
    t1_buffer = f'{_fmt_regime(s.get("t1_dist"), " pts", 1)}' if s.get("t1_dist") is not None else "—"
    t2_buffer = f'{_fmt_regime(abs(s.get("t2_dist") or 0), " pts", 1)} from 4.2% trigger' if s.get("t2_dist") is not None else "—"
    t3_buffer = f'{_fmt_regime(max(0, 600 - float(d.get("hy_bps") or 0)), "bps", 0)} of buffer' if d.get("hy_bps") is not None else "—"
    framework_html = (
        '<details class="framework-details">'
        '<summary><span>Framework / metric guide</span><span style="font-family:var(--font-sans);font-size:14px;font-weight:780;letter-spacing:0;text-transform:none;">What T1, T2, T3, yield curve, liquidity, and the timeframe map mean</span></summary>'
        '<div class="framework-body">'
        '<div class="metric-guide-grid">'
        f'<div class="metric-guide-card good"><div class="label">T1 · Primary Trigger</div><div class="metric-name">ISM Manufacturing PMI — expansion vs contraction</div><div class="value">{html.escape(t1_value)} <span style="font-size:15px;">{html.escape(str(s.get("t1") or ""))}</span></div><div class="note">First miss below 50 is the main reduce-now signal. Current buffer: {html.escape(t1_buffer)}.</div></div>'
        f'<div class="metric-guide-card good"><div class="label">T2 · Confirmation</div><div class="metric-name">Unemployment rate — labor market stress</div><div class="value">{html.escape(t2_value)} <span style="font-size:15px;">{html.escape(str(s.get("t2") or ""))}</span></div><div class="note">T2 alone is a near-miss, not a sell signal. It matters most when rising and paired with T1 weakness. Distance: {html.escape(t2_buffer)}.</div></div>'
        f'<div class="metric-guide-card info"><div class="label">T3 · Tail Risk</div><div class="metric-name">High-yield credit spreads — credit market stress</div><div class="value">{html.escape(t3_value)} <span style="font-size:15px;">{html.escape(str(s.get("t3") or ""))}</span></div><div class="note">Stress trigger is roughly above 600bps. Credit confirms whether macro fear is becoming funding stress. Current cushion: {html.escape(t3_buffer)}.</div></div>'
        '</div>'
        '<div class="framework-grid">'
        '<div class="framework-col"><div class="title">⚡ Short · Tactical</div><div class="question">“Act now or wait?” · days to weeks</div>'
        + _state_box("Risk On", "VIX compressed, breadth broad, and no active cluster signals.", "Add on dips. Favor cyclicals and high beta.", False)
        + _state_box("Cautious", f'{s.get("short_term_cond") or "Current tape"} with VIX {_fmt_regime(d.get("vix"), "", 1)} and Fear & Greed {_fmt_regime(d.get("fg"), "", 0)}.', "Hold tactical exposure. Wait for the cluster to clear before forcing adds.", s.get("short_term_cond") in {"Constructive", "Momentum Acceleration", "Healthy Pullback"})
        + _state_box("Risk Off", "Two or more short-term stress signals fire together.", "Hedge or exit tactical risk. Watch for stabilization before adding.", False)
        + '<div class="framework-note">Action trigger: two of three stress inputs — VIX above 35, deeply negative breadth, or put/call panic.</div></div>'
        '<div class="framework-col"><div class="title">📈 Medium · Trend</div><div class="question">“Trend intact or rolling over?” · 2 to 12 months</div>'
        + _state_box("Expansion", f'EPS/market trend intact. ISM {t1_value}. No rollover signal.', "Hold full equity. No defensive rotation needed.", s.get("regime_layer") == "Expansion")
        + _state_box("Late Cycle", "EPS positive but slowing, ISM plateauing, curve flattening.", "Neutral-defensive. Do not add leverage.", False)
        + _state_box("Rolling Over", "T1 warning fired and trend is breaking down.", "Shift defensive and reduce equity exposure.", False)
        + _state_box("Recovery", "ISM recovered above 50 after firing.", "Rebuild to full exposure; contrarian accumulation phase.", False)
        + '<div class="framework-note">Flip trigger: T1 warning, or yield curve inversion while T1 is still clear.</div></div>'
        '<div class="framework-col"><div class="title">🌐 Cyclical · Primary</div><div class="question">“What regime? Take or reduce risk?” · 1 to 4 years</div>'
        + _state_box("Expansion", "T1 clear, T2 clear, credit spreads contained, and unemployment not breaking.", "Overweight equities. Lean cyclicals and quality growth.", s.get("regime_layer") == "Expansion")
        + _state_box("Late Expansion", "ISM expanding but 3-month trend declining; unemployment rising from lows.", "Stay long but do not add cyclical exposure. Quality bias.", False)
        + _state_box("Reduce / T1 Warning", "ISM first miss below 50. Historically the optimal reduce window.", "Begin reducing cyclicals. Do not wait for a second miss.", s.get("t1") == "WARNING")
        + _state_box("Contraction / Near Bottom", "T1 firing for multiple months; markets may be near the bottom.", "Stop reducing once recovery evidence appears. Hold for ISM recovery.", False)
        + '<div class="framework-note">T1 fires → downgrade. T1 + T2 → full cycle turn. T2 alone is a near-miss, not a sell signal.</div></div>'
        '<div class="framework-col"><div class="title">🌐 Secular · Background</div><div class="question">“How aggressively to size?” · 15 to 20 years</div>'
        + _state_box("Early / Mid Bull", "CAPE cheap or fair, real rates low, breadth broad.", "Max equity. React aggressively to cyclical signals.", False)
        + _state_box("Late / Narrow", "CAPE elevated or leadership narrow. Sizing modifier only.", "Same T1/T2 triggers, but cut harder when they fire and reload less when clear.", True)
        + _state_box("Topping / Distribution", "Multiple warning triggers, SPX below 200-month average, top-10 concentration elevated.", "Significantly reduce when cyclical signals fire. Build defensive allocation.", False)
        + _state_box("Secular Bear", "Long-term trend breaks and drawdown deepens.", "Drastically reduce equities. Favor real assets and gold.", False)
        + '<div class="framework-note">Secular layer does not create a standalone action signal; it calibrates how aggressively you respond to cyclical signals.</div></div>'
        '</div></div></details>'
    )
    st.markdown(framework_html, unsafe_allow_html=True)
    st.markdown('<div class="regime-framework-break"></div>', unsafe_allow_html=True)

    st.markdown(_crypto_section_html(crypto, d, s, daily_memo.get("crypto")), unsafe_allow_html=True)
    st.markdown('</div></div>', unsafe_allow_html=True)
    with st.expander("✉️ Email brief preview / export", expanded=False):
        st.download_button(
            "Download email HTML",
            data=email_html,
            file_name=f"market-decision-brief-{regime_daily_key()}.html",
            mime="text/html",
            key="download_regime_email_html",
        )
        st.components.v1.html(email_html, height=760, scrolling=True)


if view == "ideas":
    st.markdown("""
<style>
.ideas-shell {
    display:grid;
    grid-template-columns:260px minmax(0, 1fr);
    gap:22px;
    align-items:start;
}
.ideas-rail {
    border-right:1px solid var(--color-border);
    min-height:620px;
    padding-right:18px;
}
.ideas-rail-title {
    font-family:var(--font-sans);
    font-size:var(--fs-lg);
    font-weight:850;
    margin:2px 0 18px;
}
.ideas-rail-asset {
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:10px;
    font-family:var(--font-sans);
    font-size:var(--fs-sm);
    font-weight:800;
    color:#475569;
    margin-bottom:12px;
}
.ideas-rail-rule {
    border-left:1px solid #CBD5E1;
    padding-left:12px;
    margin:8px 0 14px;
    color:#64748B;
    font-family:var(--font-mono);
    font-size:var(--fs-xs);
    line-height:1.45;
}
.ideas-rail-chip {
    border:1px solid var(--color-border);
    border-radius:4px;
    padding:2px 5px;
    background:#F8FAFC;
    color:#64748B;
}
div[data-testid="stForm"] {
    border:1px solid var(--color-border) !important;
    border-radius:6px !important;
    background:#FFFFFF !important;
    padding:14px !important;
}
div[data-testid="stForm"] label {
    font-family:var(--font-sans);
    font-size:var(--fs-xs) !important;
    font-weight:650 !important;
    color:#64748B !important;
}
.ideas-main-panel {
    border:1px solid var(--color-border-soft);
    border-radius:6px;
    background:#F8FAFC;
    padding:24px 26px 26px;
    min-height:620px;
}
.ideas-tabs {
    display:flex;
    gap:24px;
    margin-bottom:14px;
    font-family:var(--font-sans);
    font-size:var(--fs-base);
    font-weight:700;
}
.ideas-tab-active {
    color:var(--color-text);
    border-bottom:1px solid var(--color-text);
    padding-bottom:6px;
}
.ideas-tab-muted {
    color:#CBD5E1;
}
.ideas-asset-mark {
    width:22px;
    height:22px;
    border-radius:50%;
    background:linear-gradient(135deg,#FDB5D5,#B8F76A);
    display:inline-block;
    margin-bottom:12px;
}
.ideas-builder-title {
    font-family:var(--font-sans);
    font-size:26px;
    font-weight:850;
    color:var(--color-text);
    margin-bottom:10px;
}
.ideas-builder-sub {
    font-size:var(--fs-base);
    color:var(--color-body);
    line-height:1.45;
    max-width:980px;
}
.ideas-examples {
    display:flex;
    flex-wrap:wrap;
    gap:6px;
    margin:10px 0 2px;
}
.ideas-example {
    border:1px solid var(--color-border);
    border-radius:4px;
    padding:5px 7px;
    font-family:var(--font-mono);
    font-size:var(--fs-xs);
    color:var(--color-muted);
    background:#F8FAFC;
}
.ideas-empty {
    border:1px dashed var(--color-border);
    border-radius:4px;
    padding:13px 14px;
    margin-top:12px;
    color:var(--color-muted);
    font-size:var(--fs-base);
    background:#FFFFFF;
    font-family:var(--font-sans);
}
.ideas-progress {
    display:flex;
    align-items:center;
    gap:12px;
    margin:26px 0 18px;
    font-family:var(--font-mono);
    font-size:var(--fs-xs);
    color:#64748B;
}
.ideas-progress-bar {
    flex:1;
    height:3px;
    background:#E2E8F0;
    position:relative;
}
.ideas-progress-bar:before {
    content:"";
    position:absolute;
    left:0;
    top:0;
    bottom:0;
    width:42%;
    background:#22C55E;
}
.ideas-table {
    border-top:1px solid var(--color-border);
    border-bottom:1px solid var(--color-border);
    border-radius:0;
    overflow:hidden;
    background:transparent;
}
.ideas-grid {
    display:grid;
    grid-template-columns:0.9fr 0.58fr 0.78fr 0.82fr 1fr 0.72fr 0.72fr 0.72fr 1.7fr 0.6fr;
    gap:14px;
    align-items:start;
}
.ideas-head {
    padding:10px 0;
    background:transparent;
    border-bottom:1px solid var(--color-border);
    font-family:var(--font-mono);
    font-size:var(--fs-xs);
    font-weight:700;
    letter-spacing:var(--ls-caps-lg);
    text-transform:uppercase;
    color:var(--color-muted);
}
.ideas-row {
    padding:12px 0;
    border-bottom:1px solid var(--color-border-soft);
    font-family:var(--font-sans);
    font-size:var(--fs-sm);
    line-height:1.35;
}
.ideas-row:last-child { border-bottom:0; }
.ideas-ticker {
    font-size:var(--fs-md);
    font-weight:850;
    color:var(--color-text) !important;
    text-decoration:none !important;
}
.ideas-company {
    display:block;
    margin-top:2px;
    font-size:var(--fs-xs);
    color:var(--color-muted);
    font-weight:600;
}
.ideas-num {
    font-family:var(--font-mono);
    font-variant-numeric:tabular-nums;
}
.ideas-action {
    font-weight:800;
    white-space:nowrap;
}
.ideas-weight {
    height:8px;
    width:100%;
    margin-top:4px;
    border-radius:999px;
    background:#E2E8F0;
    overflow:hidden;
}
.ideas-weight > span {
    display:block;
    height:100%;
    background:#94A3B8;
}
.ideas-link {
    display:inline-block;
    border:1px solid var(--color-border);
    border-radius:4px;
    padding:5px 7px;
    color:var(--color-text) !important;
    text-decoration:none !important;
    font-family:var(--font-mono);
    font-size:var(--fs-xs);
    font-weight:700;
}
@media (max-width: 900px) {
    .ideas-shell { display:block; }
    .ideas-rail {
        border-right:0;
        min-height:0;
        padding-right:0;
        margin-bottom:16px;
    }
    .ideas-main-panel { padding:16px; min-height:0; }
    .ideas-table { border:0; background:transparent; }
    .ideas-head { display:none; }
    .ideas-grid {
        display:block;
        border:1px solid var(--color-border);
        border-radius:4px;
        margin-bottom:10px;
        background:#FFFFFF;
    }
    .ideas-row { border-bottom:0; }
    .ideas-row > span,
    .ideas-row > div,
    .ideas-row > a {
        display:block;
        margin-bottom:7px;
    }
}
</style>
""", unsafe_allow_html=True)
    st.markdown(
        '<div style="font-family:var(--font-sans);font-size:var(--fs-xs);font-weight:700;'
        'letter-spacing:var(--ls-caps-lg);text-transform:uppercase;color:var(--color-muted);'
        'margin:4px 0 10px;">Ideas · thematic discovery</div>',
        unsafe_allow_html=True,
    )
    default_prompt = ""
    idea_prompt_seed = ""
    runs = st.session_state.store.get("idea_discovery_runs", [])
    latest = runs[0] if runs else None
    latest_result = (latest or {}).get("result") or {}
    latest_query = (latest or {}).get("query") or "New thematic screen"
    latest_criteria = latest_result.get("criteria") or []
    latest_candidates = latest_result.get("candidates") or []
    display_candidate_count = len(latest_candidates) if latest_candidates else 0

    left_col, right_col = st.columns([1.05, 4.7], gap="large")
    with left_col:
        st.markdown(
            '<div class="ideas-rail-title">Generated Assets</div>'
            '<div class="ideas-rail-asset">'
            f'<span>{html.escape(str(latest_query)[:42])}</span><span>{display_candidate_count}</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        rail_rules = latest_criteria[:4] if latest_criteria else [
            "Describe the kind of companies you want to find",
            "The AI ranks candidate stocks by relevance and evidence",
            "Trading Desk overlays action, financials, and risk",
        ]
        st.markdown(
            '<div class="ideas-rail-rule">'
            + "".join(f'<div style="margin-bottom:10px;">{html.escape(str(rule))}</div>' for rule in rail_rules)
            + '<div>Weighting <span class="ideas-rail-chip">based on relevance</span></div>'
            + '</div>',
            unsafe_allow_html=True,
        )
        with st.form("idea_discovery_form"):
            idea_prompt = st.text_area(
                "Your thematic idea",
                value=idea_prompt_seed,
                height=130,
                placeholder="Type your thematic investment idea to get stock ideas.",
            )
            submit_idea = st.form_submit_button("Run screen →", use_container_width=True)
        refresh_candidate_metrics = st.button(
            "Refresh candidate metrics",
            key="refresh_idea_candidate_metrics",
            help="Optional heavier refresh: pulls live market/fundamental data for the generated candidate table.",
            use_container_width=True,
            disabled=not bool(latest_candidates),
        )

    if submit_idea:
        if not idea_prompt.strip():
            st.warning("Type a thematic investment idea first.")
        else:
            st.session_state["last_idea_prompt"] = idea_prompt
            universe_text = st.session_state.get("last_idea_universe", DEFAULT_DISCOVERY_UNIVERSE)
            st.session_state["last_idea_universe"] = universe_text
            try:
                with st.spinner("Researching theme and ranking candidates…"):
                    result = generate_theme_discovery(idea_prompt, universe_text, api_key)
                run = {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "query": idea_prompt.strip(),
                    "universe": universe_text.strip(),
                    "result": result,
                }
                runs = st.session_state.store.setdefault("idea_discovery_runs", [])
                runs.insert(0, run)
                st.session_state.store["idea_discovery_runs"] = runs[:8]
                save_store(st.session_state.store)
                st.rerun()
            except ValueError as exc:
                st.warning(str(exc))
            except Exception as exc:
                st.error(f"Could not generate ideas: {str(exc)[:160]}")

    with right_col:
        result = latest_result
        asset_title = latest_query if latest else "New thematic screen"
        asset_summary = result.get("summary") if latest else (
            "Type a thematic investment idea on the left and run the screen to generate candidate stocks."
        )
        found_count = len(latest_candidates) if latest_candidates else 0
        raw_candidates = result.get("candidates") or []
        candidates = []
        if raw_candidates:
            if refresh_candidate_metrics:
                with st.spinner("Refreshing candidate metrics…"):
                    bench = fetch_bench()
                    candidates = [enrich_discovery_candidate(c, bench) for c in raw_candidates]
                if latest is not None:
                    latest.setdefault("result", {})["candidates"] = candidates
                    latest["metrics_refreshed_at"] = datetime.now().isoformat(timespec="seconds")
                    save_store(st.session_state.store)
                    st.rerun()
            else:
                candidates = [cached_discovery_candidate(c) for c in raw_candidates]
        else:
            found_count = 0
        header_html = (
            '<div class="ideas-table">'
            '<div class="ideas-grid ideas-head">'
            '<span>Assets</span><span>Relevance</span><span>Market cap</span><span>Sector</span>'
            '<span>Industry</span><span>Action</span><span>Growth</span><span>Debt</span>'
            '<span>Theme proof</span><span>Weight</span>'
            '</div>'
        )
        row_html = []
        evidence_blocks = []
        watchlist_set = set(st.session_state.store.get("watchlist", []))
        for idx, cand in enumerate(candidates):
            tkr = str(cand.get("ticker", "")).upper().strip()
            score = cand.get("score", "—")
            action = cand.get("_action")
            action_style = STATE_STYLES.get(action or "", {})
            action_label = action_style.get("label", "Not scored")
            action_emoji = action_style.get("emoji", "•")
            price = cand.get("_price")
            chg = cand.get("_change")
            price_txt = f"${price:,.2f}" if price is not None else "—"
            chg_txt = f"{chg:+.2f}%" if chg is not None else "—"
            chg_color = "var(--color-positive)" if (chg or 0) >= 0 else "var(--color-negative)"
            rs_value = cand.get("_rs")
            rs_txt = f"{rs_value:.2f}" if isinstance(rs_value, (int, float)) else "—"
            add_or_open = (
                f'<a class="ideas-link" href="?open={html.escape(tkr)}" target="_self">Open</a>'
                if tkr in watchlist_set else
                f'<a class="ideas-link" href="?idea_watch={html.escape(tkr)}" target="_self">+ Watch</a>'
            )
            try:
                weight_pct = max(8, min(100, float(score)))
            except (TypeError, ValueError):
                weight_pct = 35
            market_cap = cand.get("_market_cap") or "—"
            sector = cand.get("_sector") or "—"
            industry = cand.get("_industry") or "—"
            growth = cand.get("_revenue_growth") or "—"
            debt = cand.get("_debt_equity") or "—"
            theme_proof = cand.get("theme_fit") or cand.get("why_it_matters") or "—"
            if cand.get("_starter"):
                action_label = "Run screen"
                action_emoji = "↗"
                price_txt = "—"
                chg_txt = ""
                rs_txt = "—"
                chg_color = "var(--color-muted)"
            row_html.append(
                '<div class="ideas-grid ideas-row">'
                f'<div><a class="ideas-ticker" href="?open={html.escape(tkr)}" target="_self">{html.escape(tkr)}</a>'
                f'<span class="ideas-company">{html.escape(str(cand.get("_name") or cand.get("company") or ""))}</span></div>'
                f'<span class="ideas-num">{html.escape(str(score))}%</span>'
                f'<span class="ideas-num">{html.escape(str(market_cap))}</span>'
                f'<span>{html.escape(str(sector))}</span>'
                f'<span>{html.escape(str(industry))}</span>'
                f'<span class="ideas-action">{html.escape(action_emoji)} {html.escape(action_label)}'
                f'<span class="ideas-company">{html.escape(price_txt)}'
                f'{" · " if chg_txt else ""}<span style="color:{chg_color};">{html.escape(chg_txt)}</span>'
                f' · RS {html.escape(rs_txt)}</span></span>'
                f'<span>{html.escape(str(growth))}</span>'
                f'<span>{html.escape(str(debt))}</span>'
                f'<span>{html.escape(str(theme_proof))}</span>'
                f'<span><div class="ideas-weight"><span style="width:{weight_pct:.0f}%;"></span></div>'
                f'<span class="ideas-company">{add_or_open}</span></span>'
                '</div>'
            )
            evidence = cand.get("evidence") or []
            verify_next = cand.get("verify_next") or []
            if evidence or verify_next:
                evidence_blocks.append((tkr, evidence, verify_next))
        empty_html = (
            '<div class="ideas-empty">Describe a theme and run the screen to rank candidate assets.</div>'
            if not row_html else ""
        )
        table_html = header_html + "".join(row_html) + "</div>" + empty_html
        panel_html = (
            '<div class="ideas-tabs"><span class="ideas-tab-active">Positions</span><span class="ideas-tab-muted">Backtest</span></div>'
            '<div class="ideas-main-panel">'
            '<span class="ideas-asset-mark"></span>'
            f'<div class="ideas-builder-title">{html.escape(str(asset_title))}</div>'
            f'<div class="ideas-builder-sub">{html.escape(str(asset_summary or ""))}</div>'
            '<div class="ideas-progress">'
            '<span>Screening for potential matches...</span>'
            '<span class="ideas-progress-bar"></span>'
            f'<span>{found_count or "—"} found</span>'
            '</div>'
            f'<div style="font-family:var(--font-sans);font-size:var(--fs-md);font-weight:800;margin:0 0 12px;">{html.escape(str(asset_title))}</div>'
            + table_html +
            '</div>'
        )
        st.markdown(panel_html, unsafe_allow_html=True)
        if evidence_blocks:
            with st.expander("Evidence / verify next", expanded=False):
                for tkr, evidence, verify_next in evidence_blocks:
                    st.markdown(f"**{tkr}**")
                    if evidence:
                        st.markdown("Evidence:")
                        for item in evidence[:4]:
                            st.markdown(f"- {item}")
                    if verify_next:
                        st.markdown("Verify next:")
                        for item in verify_next[:4]:
                            st.markdown(f"- {item}")


# ─────────────────────────────────────────────────────────────────────
# WATCHLIST — Pro view
# ─────────────────────────────────────────────────────────────────────
if view == "holdings":
    holdings = st.session_state.store.setdefault("holdings", {})
    removed_auto_holdings = cleanup_tracker_synced_holdings()
    if removed_auto_holdings:
        st.info(
            "Removed tracker-imported holdings: "
            + ", ".join(sorted(removed_auto_holdings))
            + ". Holdings now only shows positions you explicitly add."
        )
    with st.expander("Add holding directly", expanded=not bool(holdings)):
        st.caption("Add anything you already own. This feeds the trim/sell read on Analyze and Holdings.")
        with st.form("direct_add_holding_form", clear_on_submit=True):
            a1, a2, a3, a4, a5 = st.columns([0.9, 0.9, 0.75, 0.9, 0.9])
            with a1:
                add_ticker = st.text_input("Ticker", placeholder="COHR")
            with a2:
                add_entry = st.text_input("Entry", placeholder="123.45")
            with a3:
                add_shares = st.text_input("Shares", placeholder="Optional")
            with a4:
                add_target = st.text_input("Target", placeholder="Optional")
            with a5:
                add_stop = st.text_input("Stop", placeholder="Optional")
            add_note = st.text_input("Note", placeholder="Optional")
            submitted_holding = st.form_submit_button("Add holding", use_container_width=True)
        if submitted_holding:
            try:
                saved = add_or_update_holding(
                    add_ticker,
                    add_entry,
                    shares=add_shares,
                    target1_price=add_target,
                    stop_price=add_stop,
                    user_note=add_note,
                )
                save_store(st.session_state.store)
                st.session_state.current_ticker = saved["ticker"]
                st.session_state["ticker_input"] = saved["ticker"]
                st.session_state["_last_synced_ticker"] = saved["ticker"]
                st.session_state.store["last_ticker"] = saved["ticker"]
                try:
                    st.query_params["ticker"] = saved["ticker"]
                except Exception:
                    pass
                save_store(st.session_state.store)
                st.success(f"Added {saved['ticker']} to Holdings.")
                st.rerun()
            except ValueError as e:
                st.warning(str(e))

    if not holdings:
        st.markdown(
            '<div style="color:var(--color-faintest);font-style:italic;font-size:var(--fs-base);'
            'padding:14px 0;">No holdings yet. Add one above, or open any ticker on Analyze and use “I own this / add position.”</div>',
            unsafe_allow_html=True,
        )
    else:
        bench = fetch_bench()

        def _fmt_hold_px(value):
            try:
                if value is None:
                    return "—"
                return f"${float(value):,.2f}"
            except (TypeError, ValueError):
                return "—"

        def _fmt_hold_pct(value):
            try:
                if value is None:
                    return "—"
                return f"{float(value):+.1f}%"
            except (TypeError, ValueError):
                return "—"

        rows = []
        price_age_kinds = []
        meta_sparse = 0
        with st.spinner("Checking holdings…"):
            for tkr, holding in sorted(holdings.items()):
                hist, name, _err = fetch_history(tkr)
                if hist is None or bench is None:
                    continue
                _price_label, _price_kind = format_market_data_age(hist)
                price_age_kinds.append(_price_kind)
                t_state = tactical.compute(hist, bench)
                if not t_state:
                    continue
                meta = fetch_quote_meta(tkr)
                remember_quote_meta(tkr, meta)
                if metadata_status_label(meta)[1] != "fresh":
                    meta_sparse += 1
                t_state = apply_earnings_event_gate(t_state, meta.get("earnings_days") if meta else None)
                entry = holding_to_position_entry(tkr, holding)
                read = position_management_read(entry, t_state)
                entry_px = entry.get("entry_price")
                price = t_state.get("price")
                shares = entry.get("shares")
                pnl_pct = None
                pnl_dollars = None
                try:
                    if entry_px and price:
                        pnl_pct = (float(price) / float(entry_px) - 1) * 100
                        if shares:
                            pnl_dollars = (float(price) - float(entry_px)) * float(shares)
                except (TypeError, ValueError):
                    pass
                target = entry.get("target1_price")
                stop = entry.get("stop_price")
                target_gap = ((target / price - 1) * 100) if target and price else None
                stop_room = ((price / stop - 1) * 100) if stop and price else None
                rows.append({
                    "ticker": tkr,
                    "name": name or tkr,
                    "price": price,
                    "entry": entry_px,
                    "shares": shares,
                    "pnl_pct": pnl_pct,
                    "pnl_dollars": pnl_dollars,
                    "target": target,
                    "target_gap": target_gap,
                    "stop": stop,
                    "stop_room": stop_room,
                    "read": read,
                    "holding": holding,
                })

        st.markdown(
            f'<div style="font-family:var(--font-sans);font-size:var(--fs-xs);font-weight:700;'
            f'letter-spacing:var(--ls-caps-lg);text-transform:uppercase;color:var(--color-muted);'
            f'margin:4px 0 10px;">Holdings · {len(rows)} positions</div>',
            unsafe_allow_html=True,
        )
        bench_label, bench_kind = benchmark_source_label(bench)
        holdings_price_kind = "fresh" if price_age_kinds and all(k == "fresh" for k in price_age_kinds) else ("warn" if price_age_kinds else "stale")
        st.markdown(data_status_html([
            ("Source", "explicit holdings only", "info"),
            ("Prices", f"{len(rows)} checked · last close data", holdings_price_kind),
            ("Benchmark", bench_label, bench_kind),
            ("Fundamentals", f"{meta_sparse} sparse" if meta_sparse else "cached ≤1h", "warn" if meta_sparse else "fresh"),
        ]), unsafe_allow_html=True)

        total_pl = sum((r.get("pnl_dollars") or 0) for r in rows)
        trim_count = sum(
            1 for r in rows
            if (r.get("read") or {}).get("action") in ("Trim", "Take profit", "Exit", "Respect stop", "Review after earnings")
        )
        h1, h2, h3 = st.columns(3)
        h1.metric("Positions", len(rows))
        h2.metric("Needs review", trim_count)
        h3.metric("Known $ P/L", f"${total_pl:,.0f}" if total_pl else "—")

        st.markdown("""
<style>
.holdings-grid {
    display:grid;
    grid-template-columns:0.75fr 0.9fr 0.75fr 0.75fr 0.65fr 0.75fr 0.75fr 0.8fr 1.05fr;
    gap:10px;
    align-items:center;
}
.holdings-head {
    padding:9px 10px;
    border:1px solid var(--color-border);
    border-radius:4px 4px 0 0;
    background:#F8FAFC;
    font-family:var(--font-mono);
    font-size:var(--fs-xs);
    font-weight:700;
    letter-spacing:var(--ls-caps-lg);
    text-transform:uppercase;
    color:var(--color-muted);
}
.holdings-row {
    padding:9px 10px;
    border-left:1px solid var(--color-border);
    border-right:1px solid var(--color-border);
    border-bottom:1px solid var(--color-border-soft);
    font-family:var(--font-mono);
    font-size:var(--fs-sm);
    font-variant-numeric:tabular-nums;
}
.holdings-row:last-child {
    border-bottom:1px solid var(--color-border);
    border-radius:0 0 4px 4px;
}
.holdings-ticker {
    font-family:var(--font-sans);
    font-weight:800;
    color:var(--color-text) !important;
    text-decoration:none !important;
}
.holdings-action {
    font-family:var(--font-sans);
    font-weight:800;
    white-space:nowrap;
}
</style>
""", unsafe_allow_html=True)
        st.markdown(
            '<div class="holdings-grid holdings-head">'
            '<span>Ticker</span><span>Now</span><span>Entry</span><span>Shares</span>'
            '<span>P/L</span><span>Target</span><span>Stop</span><span>Room</span><span>Read</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        for row in rows:
            read = row.get("read") or {}
            color = read.get("color", "var(--color-muted)")
            action = read.get("action", "Review")
            emoji = read.get("emoji", "📌")
            pl_color = "var(--color-positive)" if (row.get("pnl_pct") or 0) >= 0 else "var(--color-negative)"
            room_text = f"Tgt {_fmt_hold_pct(row.get('target_gap'))} · Stop {(_fmt_hold_pct(row.get('stop_room'))).replace('+', '')}"
            st.markdown(
                f'<div class="holdings-grid holdings-row">'
                f'<a class="holdings-ticker" href="?open={html.escape(row["ticker"])}" target="_self">{html.escape(row["ticker"])}</a>'
                f'<span>{_fmt_hold_px(row.get("price"))}</span>'
                f'<span>{_fmt_hold_px(row.get("entry"))}</span>'
                f'<span>{html.escape(str(row.get("shares") or "—"))}</span>'
                f'<span style="color:{pl_color};">{_fmt_hold_pct(row.get("pnl_pct"))}</span>'
                f'<span>{_fmt_hold_px(row.get("target"))}</span>'
                f'<span>{_fmt_hold_px(row.get("stop"))}</span>'
                f'<span style="color:var(--color-muted);">{html.escape(room_text)}</span>'
                f'<span class="holdings-action" style="color:{color};">{emoji} {html.escape(action)}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
        for row in rows:
            tkr = row["ticker"]
            holding = holdings.get(tkr, {})
            with st.expander(f"Edit {tkr} holding", expanded=False):
                e1, e2, e3, e4 = st.columns(4)
                with e1:
                    entry_val = st.text_input("Entry", value=_fmt_hold_px(holding.get("entry_price")).replace("$", "").replace(",", "") if holding.get("entry_price") is not None else "", key=f"hold_entry_{tkr}")
                with e2:
                    shares_val = st.text_input("Shares", value=str(holding.get("shares") or ""), key=f"hold_shares_{tkr}")
                with e3:
                    target_val = st.text_input("Target", value=_fmt_hold_px(holding.get("target1_price")).replace("$", "").replace(",", "") if holding.get("target1_price") is not None else "", key=f"hold_target_{tkr}")
                with e4:
                    stop_val = st.text_input("Stop", value=_fmt_hold_px(holding.get("stop_price")).replace("$", "").replace(",", "") if holding.get("stop_price") is not None else "", key=f"hold_stop_{tkr}")
                note_val = st.text_input("Note", value=str(holding.get("user_note") or ""), key=f"hold_note_{tkr}")
                c1, c2 = st.columns([3, 1])
                with c1:
                    if st.button("Save holding", key=f"save_hold_{tkr}", use_container_width=True):
                        try:
                            holdings[tkr].update({
                                "entry_price": parse_optional_float(entry_val),
                                "shares": parse_optional_float(shares_val),
                                "target1_price": parse_optional_float(target_val),
                                "stop_price": parse_optional_float(stop_val),
                                "user_note": note_val.strip(),
                                "updated_at": datetime.now().isoformat(timespec="seconds"),
                            })
                            save_store(st.session_state.store)
                            st.rerun()
                        except ValueError:
                            st.warning("One of the edited values is not a valid number.")
                with c2:
                    if st.button("Remove", key=f"delete_hold_{tkr}", use_container_width=True):
                        holdings.pop(tkr, None)
                        save_store(st.session_state.store)
                        st.rerun()


if view == "watchlist":
    if not st.session_state.store["watchlist"]:
        st.info("Your watchlist is empty. Type a ticker in the sidebar and add it.")
    else:
        def _run_watchlist_market_scan():
            """Refresh watchlist market inputs only when explicitly requested."""
            fetch_history.clear()
            watchlist_tickers = [
                str(scan_tkr).upper().strip()
                for scan_tkr in st.session_state.store.get("watchlist", [])
                if str(scan_tkr or "").strip()
            ]
            snapshots = st.session_state.store.setdefault("ticker_snapshots", {})
            for scan_tkr in watchlist_tickers:
                _delete_history_cache(scan_tkr)
                snapshot_entry = snapshots.get(scan_tkr)
                if isinstance(snapshot_entry, dict):
                    snapshot_entry.pop("market", None)
                    snapshot_entry.pop("market_updated_at", None)
            fetch_quote_meta.clear()
            sidebar_watchlist_snapshot.clear()
            st.session_state.store["watchlist_sidebar_cache"] = {}
            update_sidebar_watchlist_cache(watchlist_tickers)

        scan_c1, scan_c2 = st.columns([1.3, 4])
        with scan_c1:
            if st.button(
                "↻ Refresh prices/actions",
                key="refresh_watchlist_scan",
                help="Refresh prices, fundamentals, sidebar rows, and rule/scan metrics for the full watchlist. PM memos refresh per ticker from the PM memo column or Analyze page.",
                use_container_width=True,
            ):
                _run_watchlist_market_scan()
                st.rerun()
        with scan_c2:
            st.markdown(
                '<div class="watchlist-control-note">'
                'Default view opens from saved setup data for speed. Refresh prices/actions updates market data. PM memo age is separate; use the PM memo links to refresh research for one ticker.</div>',
                unsafe_allow_html=True,
            )
        watchlist_layout_pref = st.session_state.get("watchlist_layout", "Decision queue")
        fast_watchlist = watchlist_layout_pref == "Decision queue"
        bench = None if fast_watchlist else fetch_bench()
        dossier_cache = st.session_state.store.get("dossier_cache", {})
        owned_ticker_set = active_position_tickers()

        def _watchlist_attention(tkr, t_state, trig_dist, earnings_days, cached):
            """Compact attention label + priority for the watchlist table."""
            ticker_key = str(tkr).upper()
            action = t_state.get("action")
            cached_call = ((cached.get("result") or {}).get("tactical_call") or {})
            cached_action = normalize_action_key(cached_call.get("action"))
            try:
                cached_confidence = int(cached_call.get("confidence", 0) or 0)
            except (TypeError, ValueError):
                cached_confidence = 0
            dissent = claude_dissent_signal(
                action,
                cached_action,
                cached_confidence,
                cached_call.get("reasoning") or cached_call.get("trigger") or "",
            )
            if dissent.get("flag"):
                return "★ Claude dissent", 0, "var(--color-blue)"
            if action == "enter_now":
                return "🚀 Enter", 1, "var(--color-positive)"
            if ticker_key in owned_ticker_set:
                return "🟢 Position", 2, "var(--color-blue)"
            if trig_dist is not None and abs(trig_dist) <= 3:
                return "🎯 Near trigger", 3, "var(--color-warning-text)"
            if earnings_days is not None and -1 <= earnings_days <= 7:
                return "📅 Earnings", 4, "var(--color-warning-text)"
            if action == "watch":
                return "👀 Watch", 6, "var(--color-warning-text)"
            return "—", 99, "var(--color-faint)"

        def _watchlist_pm_cell(cached):
            """Return PM memo state for the row without implying scan refresh regenerates it."""
            if not ((cached.get("result") or {}).get("tactical_call") or {}):
                return "No memo ↻", "var(--color-faint)", True
            try:
                cache_ts = cached.get("ts")
                if cache_ts:
                    age = datetime.now() - datetime.fromisoformat(cache_ts)
                    if age.days >= 3:
                        return f"Memo {age.days}d ↻", "var(--color-warning-text)", True
            except Exception:
                pass
            return "Memo ready", "var(--color-positive)", False

        def _safe_float(value, default=0.0):
            try:
                if value is None:
                    return default
                return float(value)
            except (TypeError, ValueError):
                return default

        def _cached_watchlist_row(tkr):
            """Fast watchlist row from the canonical per-ticker snapshot."""
            key = str(tkr).upper().strip()
            canonical = ticker_snapshot(key)
            snapshot = canonical.get("market") or {}
            cached = dossier_cache.get(key, {}) if isinstance(dossier_cache, dict) else {}
            meta = canonical.get("meta") or cached_quote_meta_snapshot(key)
            cached_call = ((cached.get("result") or {}).get("tactical_call") or {})
            cached_action = normalize_action_key(cached_call.get("action"))
            try:
                cached_confidence = int(cached_call.get("confidence", 0) or 0)
            except (TypeError, ValueError):
                cached_confidence = 0
            final_cached = canonical.get("final_action") or {}
            action = (
                normalize_action_key(final_cached.get("action"))
                or normalize_action_key(snapshot.get("action"))
                or cached_action
                or "watch"
            )
            fallback_profile = infer_security_profile(key, meta, key)
            if (
                fallback_profile.get("sector") in {"ETF", "Fund"}
                or str(meta.get("quote_type") or "").upper() in {"ETF", "MUTUALFUND", "FUND"}
            ):
                sector = (
                    fallback_profile.get("category")
                    or meta.get("category")
                    or fallback_profile.get("sector")
                    or "ETF"
                )
            else:
                sector = meta.get("sector") or fallback_profile.get("sector") or "—"
            quality_tier = (
                ((cached.get("result") or {}).get("quality") or {}).get("tier", "")
            )
            cached_dissent_note = cached_call.get("reasoning") or cached_call.get("trigger") or ""
            try:
                from pm_view import substitute_live_values as _sub_live_values
                cached_dissent_note = _sub_live_values(
                    cached_dissent_note,
                    {**snapshot, "price": snapshot.get("price")},
                )
            except Exception:
                pass
            dissent_signal = claude_dissent_signal(
                action,
                cached_action,
                cached_confidence,
                cached_dissent_note,
            )
            t_stub = {
                "action": action,
                "state": snapshot.get("state") or "CACHED",
                "price": _safe_float(snapshot.get("last")),
                "change": _safe_float(snapshot.get("change_pct")),
                "rs": _safe_float(snapshot.get("rs"), 1.0),
                "tech_delta": 0,
            }
            attention_label, attention_rank, attention_color = _watchlist_attention(
                key, t_stub, None, None, cached
            )
            pm_row_label, pm_row_color, pm_needs_refresh = _watchlist_pm_cell(cached)
            personality = {
                "label": "Saved setup",
                "emoji": STATE_STYLES.get(action, STATE_STYLES["watch"]).get("emoji", "👀"),
                "rank": 99,
            }
            if cached_call.get("setup_type"):
                personality["label"] = str(cached_call.get("setup_type"))
            return {
                "ticker": key,
                "name": display_security_name(key, key, meta, fallback_profile) or key,
                "price": t_stub["price"],
                "change": t_stub["change"],
                "action": action,
                "state": t_stub["state"],
                "rs": t_stub["rs"],
                "rs_delta": 0,
                "pct_ma50": _safe_float(snapshot.get("pct_ma50"), 0),
                "trig_dist": None,
                "earnings_days": meta.get("earnings_days") if isinstance(meta, dict) else None,
                "quality": quality_tier,
                "sector": sector,
                "high_52w": snapshot.get("high_52w"),
                "low_52w": snapshot.get("low_52w"),
                "pct_52w_range": _safe_float(snapshot.get("pct_52w_range"), 50),
                "vol_ratio": _safe_float(snapshot.get("vol_ratio"), 1.0),
                "tech_delta": 0,
                "attention": attention_label,
                "attention_rank": attention_rank,
                "attention_color": attention_color,
                "pm_status": pm_row_label,
                "pm_status_color": pm_row_color,
                "pm_needs_refresh": pm_needs_refresh,
                "claude_dissent": dissent_signal,
                "personality": personality["label"],
                "personality_emoji": personality["emoji"],
                "personality_rank": personality["rank"],
                "_t": t_stub,
            }

        # ── Compute everything we'll need for every ticker, in one pass ──
        # Each row: dict with action/state/RS/etc. + tactical engine output.
        rows = []
        price_age_kinds = []
        meta_sparse = 0
        if fast_watchlist:
            for tkr in st.session_state.store["watchlist"]:
                row = _cached_watchlist_row(tkr)
                rows.append(row)
                cached_age_kind = (
                    (ticker_snapshot(row["ticker"]).get("market") or {}).get("price_age_kind")
                )
                price_age_kinds.append(cached_age_kind or "stale")
                meta = ticker_snapshot(row["ticker"]).get("meta") or cached_quote_meta_snapshot(row["ticker"])
                if metadata_status_label(meta)[1] != "fresh":
                    meta_sparse += 1
        else:
            with st.spinner("Analyzing full metrics…"):
                for tkr in st.session_state.store["watchlist"]:
                    hist, name, _err = fetch_history(tkr)
                    if hist is None or bench is None:
                        continue
                    _price_label, _price_kind = format_market_data_age(hist)
                    price_age_kinds.append(_price_kind)
                    t = tactical.compute(hist, bench)
                    if t is None:
                        continue

                    # Apply accumulation override using cached quality
                    if t.get("is_accumulation_eligible") and t["action"] == "avoid":
                        cached = dossier_cache.get(tkr.upper(), {})
                        cached_quality = ((cached.get("result") or {}).get("quality") or {})
                        q_tier = cached_quality.get("tier", "")
                        new_action = tactical.apply_accumulation_override(
                            t["action"], True, q_tier
                        )
                        if new_action != t["action"]:
                            t = {**t, "action": new_action}

                    # Quality tier from dossier cache (if available)
                    cached = dossier_cache.get(tkr.upper(), {})
                    quality_tier = (
                        ((cached.get("result") or {}).get("quality") or {}).get("tier", "")
                    )

                    # Earnings days from quote meta — fetch separately, cached
                    meta = fetch_quote_meta(tkr)
                    remember_quote_meta(tkr, meta)
                    if metadata_status_label(meta)[1] != "fresh":
                        meta_sparse += 1
                    earnings_days = meta.get("earnings_days") if meta else None
                    t = apply_earnings_event_gate(t, earnings_days)
                    cached_call = ((cached.get("result") or {}).get("tactical_call") or {})
                    cached_action = normalize_action_key(cached_call.get("action"))
                    try:
                        cached_confidence = int(cached_call.get("confidence", 0) or 0)
                    except (TypeError, ValueError):
                        cached_confidence = 0
                    cached_dissent_note = cached_call.get("reasoning") or cached_call.get("trigger") or ""
                    try:
                        from pm_view import substitute_live_values as _sub_live_values
                        cached_dissent_note = _sub_live_values(
                            cached_dissent_note,
                            {**t, "price": t.get("price")},
                        )
                    except Exception:
                        pass
                    dissent_signal = claude_dissent_signal(
                        t.get("action"),
                        cached_action,
                        cached_confidence,
                        cached_dissent_note,
                    )

                    # Trigger distance % — how close to a logged trigger?
                    trig = t.get("trigger") or {}
                    buy_above = trig.get("levels", {}).get("buy_above") if trig else None
                    trig_dist = (
                        (buy_above - t["price"]) / t["price"] * 100
                        if buy_above and t["price"] else None
                    )

                    # % from MA50
                    pct_ma50 = (
                        (t["price"] - t["ma50"]) / t["ma50"] * 100
                        if t["ma50"] else 0
                    )

                    # Sector/category from yfinance + display fallback — used for grouping.
                    fallback_profile = infer_security_profile(tkr, meta, name)
                    if (fallback_profile.get("sector") in {"ETF", "Fund"} or str(meta.get("quote_type") or "").upper() in {"ETF", "MUTUALFUND", "FUND"}):
                        sector = fallback_profile.get("category") or meta.get("category") or fallback_profile.get("sector") or "ETF"
                    else:
                        sector = (meta.get("sector") if meta else None) or fallback_profile.get("sector") or "—"
                    attention_label, attention_rank, attention_color = _watchlist_attention(
                        tkr, t, trig_dist, earnings_days, cached
                    )
                    pm_row_label, pm_row_color, pm_needs_refresh = _watchlist_pm_cell(cached)
                    personality = classify_setup_personality(t, quality_tier)

                    rows.append({
                        "ticker": tkr,
                        "name": display_security_name(tkr, name, meta, fallback_profile) or tkr,
                        "price": t["price"],
                        "change": t["change"],
                        "action": t["action"],
                        "state": t.get("state", "TRENDING"),
                        "rs": t.get("rs", 1.0),
                        "rs_delta": t.get("rs_delta", 0),
                        "pct_ma50": pct_ma50,
                        "trig_dist": trig_dist,
                        "earnings_days": earnings_days,
                        "quality": quality_tier,
                        "sector": sector,
                        "high_52w": t.get("high_52w"),
                        "low_52w": t.get("low_52w"),
                        "pct_52w_range": t.get("pct_of_52w_range", 50),
                        "vol_ratio": t.get("vol_ratio", 1.0),
                        "tech_delta": t.get("tech_delta", 0),
                        "attention": attention_label,
                        "attention_rank": attention_rank,
                        "attention_color": attention_color,
                        "pm_status": pm_row_label,
                        "pm_status_color": pm_row_color,
                        "pm_needs_refresh": pm_needs_refresh,
                        "claude_dissent": dissent_signal,
                        "personality": personality["label"],
                        "personality_emoji": personality["emoji"],
                        "personality_rank": personality["rank"],
                        "_t": t,
                    })

        def _watch_queue_bucket(row):
            attention = str(row.get("attention") or "")
            action = row.get("action")
            if action == "enter_now":
                return "Actionable now"
            if attention.startswith("🎯"):
                return "Near trigger"
            if str(row.get("ticker")).upper() in owned_ticker_set:
                return "Owned positions"
            if attention.startswith("📅"):
                return "Earnings risk"
            if action == "avoid":
                return "Broken / avoid"
            if row.get("pm_needs_refresh"):
                return "PM refresh"
            if attention.startswith("🧠"):
                return "PM refresh"
            return "Monitor"

        queue_order = [
            ("Actionable now", "🚀", "var(--color-positive)"),
            ("Near trigger", "🎯", "var(--color-warning-text)"),
            ("Owned positions", "🟢", "var(--color-blue)"),
            ("Earnings risk", "📅", "var(--color-warning-text)"),
            ("Broken / avoid", "⛔", "var(--color-negative)"),
            ("PM refresh", "🧠", "var(--color-faint)"),
            ("Monitor", "👀", "var(--color-muted)"),
        ]
        queue_map = {label: [] for label, _emoji, _color in queue_order}
        for row in rows:
            queue_map.setdefault(_watch_queue_bucket(row), []).append(row)

        queue_html = []
        for label, emoji, color in queue_order:
            bucket_rows = queue_map.get(label, [])
            preview = " · ".join(r["ticker"] for r in bucket_rows[:4])
            if len(bucket_rows) > 4:
                preview += f" +{len(bucket_rows) - 4}"
            if not preview:
                preview = "—"
            queue_html.append(
                f'<div class="watch-queue-card">'
                f'<div class="watch-queue-label" style="color:{color};">{emoji} {label}</div>'
                f'<div class="watch-queue-count">{len(bucket_rows)}</div>'
                f'<div class="watch-queue-preview">{html.escape(preview)}</div>'
                f'</div>'
            )

        # ── Sort selector ──
        st.markdown(
            '<div style="font-family: var(--font-sans);'
            'font-size: var(--fs-xs);font-weight:600;'
            'letter-spacing: var(--ls-caps-lg);text-transform:uppercase;'
            'color: var(--color-muted);margin: 4px 0 8px;">'
            'Watchlist · ' + str(len(rows)) + ' names</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="watch-queue-grid">' + "".join(queue_html) + '</div>',
            unsafe_allow_html=True,
        )
        pm_label, pm_kind = watchlist_pm_status(dossier_cache, [r["ticker"] for r in rows])
        watch_price_kind = "fresh" if price_age_kinds and all(k == "fresh" for k in price_age_kinds) else ("warn" if price_age_kinds else "stale")
        st.markdown(canonical_freshness_html([
            ("Price", "last close data", watch_price_kind),
            ("Fundamentals", f"{meta_sparse} sparse" if meta_sparse else "cached ≤1h", "warn" if meta_sparse else "fresh"),
            ("PM memo", pm_label.replace("PM ", ""), pm_kind),
            ("Sidebar", f"{len(rows)}/{len(st.session_state.store['watchlist'])} rows updated", "fresh" if rows else "stale"),
        ]), unsafe_allow_html=True)
        if fast_watchlist:
            refresh_note_c1, refresh_note_c2 = st.columns([1.35, 4])
            with refresh_note_c1:
                if st.button(
                    "↻ Refresh saved queue",
                    key="refresh_watchlist_scan_inline",
                    help="Updates saved prices/actions without regenerating every ticker's PM memo.",
                    use_container_width=True,
                ):
                    _run_watchlist_market_scan()
                    st.rerun()
            with refresh_note_c2:
                st.markdown(
                    '<div class="watchlist-control-note">'
                    'Showing saved setup rows so the page opens quickly. Refresh saved queue updates prices and rule actions; PM memo links refresh one ticker at a time.</div>',
                    unsafe_allow_html=True,
                )

        sort_c1, sort_c2, sort_c3 = st.columns([2, 2, 2])
        with sort_c1:
            sort_by = st.selectbox(
                "Sort by",
                options=[
                    "Needs attention",
                    "Action priority",
                    "Ticker (A→Z)",
                    "Change % (high→low)",
                    "Change % (low→high)",
                    "% from MA50 (extended first)",
                    "% from MA50 (closest to MA50)",
                    "RS (leaders first)",
                    "Trigger distance (closest)",
                    "Earnings (soonest)",
                    "52w range (closest to high)",
                    "52w range (closest to low)",
                    "Volume ratio (highest)",
                    "Setup type",
                ],
                key="watchlist_sort",
                label_visibility="collapsed",
            )
        with sort_c2:
            group_by = st.selectbox(
                "Group by",
                options=["No grouping", "Action", "Setup type", "Sector", "Quality tier"],
                key="watchlist_group",
                label_visibility="collapsed",
            )
        with sort_c3:
            watchlist_layout = st.selectbox(
                "View",
                options=["Decision queue", "Full metrics"],
                key="watchlist_layout",
                label_visibility="collapsed",
            )

        # Action priority order
        action_priority = {
            "enter_now": 0, "accumulate": 1, "watch": 2,
            "hold_off": 3, "avoid": 4,
        }

        if sort_by == "Needs attention":
            rows.sort(key=lambda r: (r["attention_rank"], action_priority.get(r["action"], 99), r["ticker"]))
        elif sort_by == "Action priority":
            rows.sort(key=lambda r: (action_priority.get(r["action"], 99), r["ticker"]))
        elif sort_by == "Ticker (A→Z)":
            rows.sort(key=lambda r: r["ticker"])
        elif sort_by == "Change % (high→low)":
            rows.sort(key=lambda r: -r["change"])
        elif sort_by == "Change % (low→high)":
            rows.sort(key=lambda r: r["change"])
        elif sort_by == "% from MA50 (extended first)":
            rows.sort(key=lambda r: -r["pct_ma50"])
        elif sort_by == "% from MA50 (closest to MA50)":
            rows.sort(key=lambda r: abs(r["pct_ma50"]))
        elif sort_by == "RS (leaders first)":
            rows.sort(key=lambda r: -r["rs"])
        elif sort_by == "Trigger distance (closest)":
            rows.sort(key=lambda r: abs(r["trig_dist"]) if r["trig_dist"] is not None else 999)
        elif sort_by == "Earnings (soonest)":
            rows.sort(key=lambda r: r["earnings_days"] if r["earnings_days"] is not None else 999)
        elif sort_by == "52w range (closest to high)":
            rows.sort(key=lambda r: -r["pct_52w_range"])
        elif sort_by == "52w range (closest to low)":
            rows.sort(key=lambda r: r["pct_52w_range"])
        elif sort_by == "Volume ratio (highest)":
            rows.sort(key=lambda r: -r["vol_ratio"])
        elif sort_by == "Setup type":
            rows.sort(key=lambda r: (r["personality_rank"], r["ticker"]))

        # ── Build groups ──
        if group_by == "Action":
            from collections import OrderedDict
            groups = OrderedDict()
            order = ["enter_now", "watch", "accumulate", "hold_off", "avoid"]
            for k in order:
                groups[k] = []
            for r in rows:
                groups.setdefault(r["action"], []).append(r)
            # Format: (group_label, group_color, rows)
            grouped = []
            for k in order:
                if groups[k]:
                    sty = STATE_STYLES[k]
                    grouped.append((f"{sty['emoji']} {sty['label']}", sty["color"], groups[k]))
        elif group_by == "Sector":
            from collections import defaultdict
            sectors = defaultdict(list)
            for r in rows:
                sectors[r["sector"]].append(r)
            grouped = [(sec, "var(--color-muted)", srows)
                       for sec, srows in sorted(sectors.items())]
        elif group_by == "Quality tier":
            from collections import OrderedDict
            tiers = OrderedDict([("A", []), ("B", []), ("Speculative", []),
                                ("Avoid", []), ("(no quality data)", [])])
            for r in rows:
                key = r["quality"] if r["quality"] in ("A", "B", "Speculative", "Avoid") else "(no quality data)"
                tiers[key].append(r)
            tier_colors = {"A": "var(--color-accent)", "B": "var(--color-faint)",
                           "Speculative": "var(--color-purple)", "Avoid": "var(--color-negative)",
                           "(no quality data)": "var(--color-faintest)"}
            grouped = [(k, tier_colors[k], v) for k, v in tiers.items() if v]
        elif group_by == "Setup type":
            from collections import OrderedDict
            setup_groups = OrderedDict()
            for r in sorted(rows, key=lambda row: (row["personality_rank"], row["ticker"])):
                setup_groups.setdefault(f'{r["personality_emoji"]} {r["personality"]}', []).append(r)
            grouped = [(k, "var(--color-muted)", v) for k, v in setup_groups.items()]
        else:
            grouped = [(None, None, rows)]

        # ── Header row + all data rows render as ONE consistent HTML grid ──
        # Ticker cells are clickable anchor tags using ?open=TICKER query
        # params. The global handler at the top of the app picks up the
        # click and switches the active ticker — works universally without
        # needing Streamlit columns.

        compact_watchlist = watchlist_layout == "Decision queue"
        if compact_watchlist:
            # Default: decision queue, but with enough market context to scan
            # without switching into the heavier full-metrics pass.
            grid_cols = (
                'grid-template-columns: '
                'repeat(11, minmax(0, 1fr));'
            )
        else:
            # Dense research view: all metrics for sorting/comparison.
            grid_cols = (
                'grid-template-columns: '
                'minmax(0,0.76fr) '
                'minmax(0,1.10fr) '
                'minmax(0,1.15fr) '
                'minmax(0,0.78fr) '
                'minmax(0,0.72fr) '
                'minmax(0,0.96fr) '
                'minmax(0,0.78fr) '
                'minmax(0,0.92fr) '
                'minmax(0,0.58fr) '
                'minmax(0,0.58fr) '
                'minmax(0,0.72fr) '
                'minmax(0,0.72fr) '
                'minmax(0,0.58fr) '
                'minmax(0,0.58fr) '
                'minmax(0,0.48fr);'
            )

        dissent_rows = [
            r for r in rows
            if (r.get("claude_dissent") or {}).get("flag")
        ]
        if dissent_rows:
            dissent_items = []
            for row in dissent_rows[:8]:
                dissent = row.get("claude_dissent") or {}
                reason = dissent.get("reason") or "Claude disagrees with rules."
                note = dissent.get("note") or ""
                note_html = (
                    f'<div class="watchlist-dissent-note">{html.escape(note)}</div>'
                    if note else ""
                )
                dissent_items.append(
                    f'<div class="watchlist-dissent-item">'
                    f'<a href="?open={html.escape(row["ticker"])}" target="_self">'
                    f'{html.escape(row["ticker"])}</a> · {html.escape(reason)}'
                    f'{note_html}</div>'
                )
            if len(dissent_rows) > 8:
                dissent_items.append(
                    f'<div class="watchlist-dissent-item">'
                    f'+{len(dissent_rows) - 8} more in the table</div>'
                )
            st.markdown(
                '<div class="watchlist-dissent-panel">'
                '<div class="watchlist-dissent-title">★ Claude dissent review</div>'
                '<div class="watchlist-dissent-copy">'
                'Rules still drive the action. These are the names where Claude '
                'materially disagrees, so they deserve a manual second look.'
                '</div>'
                '<div class="watchlist-dissent-list">'
                + "".join(dissent_items) +
                '</div></div>',
                unsafe_allow_html=True,
            )

        if compact_watchlist:
            column_key_html = """
<details class="watchlist-column-key">
  <summary>Column key</summary>
  <div>
    <b>Setup</b> descriptive setup type, separate from the action call ·
    <b>Attention</b> highest-signal reason to look now ·
    <b>New action</b> what the app would do for a fresh position ·
    <b>52w high/low</b> saved during the last watchlist refresh ·
    <b>52w pos</b> position in the 52-week range ·
    <b>Trigger</b> percent to the trigger, if defined ·
    <b>PM memo</b> memo/dossier age; old/no-memo links refresh research for that ticker. <b>Review ★</b> opens Analyze when Claude materially disagrees with rules.
  </div>
</details>
"""
        else:
            column_key_html = """
<details class="watchlist-column-key">
  <summary>Column key</summary>
  <div>
    <b>Setup</b> descriptive setup type, separate from the action call ·
    <b>Attention</b> highest-signal reason to look now ·
    <b>New action</b> what the app would do for a fresh position, whether or not you already own it ·
    <b>PM memo</b> memo/dossier age; ★ means Claude strongly dissents from rules ·
    <b>Quality</b> long-term PM tier ·
    <b>RS</b> relative strength vs SPY, above 1.0 leads ·
    <b>vs MA50</b> percent above/below the 50-day ·
    <b>52w pos</b> position in the 52-week range ·
    <b>Vol ×</b> today vs 20-day average ·
    <b>Trig</b> percent to trigger ·
    <b>Earn</b> days to earnings.
  </div>
</details>
"""
        st.markdown(column_key_html, unsafe_allow_html=True)

        # Header
        if compact_watchlist:
            header_cells = (
                '<span>Ticker</span>'
                '<span style="text-align:right;">Price</span>'
                '<span style="text-align:right;">Chg</span>'
                '<span>Action</span>'
                '<span>Attention</span>'
                '<span style="text-align:right;">52w high</span>'
                '<span style="text-align:right;">52w low</span>'
                '<span style="text-align:right;">52w pos</span>'
                '<span style="text-align:right;">Vol ×</span>'
                '<span style="text-align:right;">Trigger</span>'
                '<span>PM memo</span>'
            )
        else:
            header_cells = (
                '<span>Ticker</span>'
                '<span>Setup</span>'
                '<span>Attention</span>'
                '<span style="text-align:right;">Last</span>'
                '<span style="text-align:right;">Chg (1D)</span>'
                '<span>New action</span>'
                '<span>PM memo</span>'
                '<span>State</span>'
                '<span>Quality</span>'
                '<span style="text-align:right;">RS</span>'
                '<span style="text-align:right;">vs MA50</span>'
                '<span style="text-align:right;">52w pos</span>'
                '<span style="text-align:right;">Vol ×</span>'
                '<span style="text-align:right;">Trig</span>'
                '<span style="text-align:right;">Earn</span>'
            )
        st.markdown(
            f'<div class="watchlist-grid-row watchlist-grid-head" style="display:grid; {grid_cols} '
            f'column-gap: 10px; row-gap: 0; padding: 8px 6px; margin-top: 16px; '
            f'border-bottom: 1px solid var(--color-border); '
            f'font-family: var(--font-sans); '
            f'font-size: var(--fs-xs); font-weight: 600; '
            f'letter-spacing: var(--ls-caps-md); text-transform: uppercase; '
            f'color: var(--color-muted);">{header_cells}</div>',
            unsafe_allow_html=True,
        )

        # ── Rows, possibly grouped ──
        for group_label, group_color, group_rows in grouped:
            # Group header (only if grouping is active)
            if group_label is not None:
                st.markdown(
                    f'<div style="margin-top:14px; padding: 6px 8px 4px; '
                    f'font-family: var(--font-sans); '
                    f'font-size: var(--fs-xs); font-weight: 600; '
                    f'letter-spacing: var(--ls-caps-lg); text-transform: uppercase; '
                    f'color: {group_color};">'
                    f'{group_label} · {len(group_rows)}'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            for row in group_rows:
                sty = STATE_STYLES[row["action"]]
                chg_color = "var(--color-positive)" if row["change"] >= 0 else "var(--color-negative)"
                rs_color = "var(--color-positive)" if row["rs"] >= 1.0 else "var(--color-faint)"
                ma_color = (
                    "var(--color-negative)" if row["pct_ma50"] > 12
                    else ("var(--color-positive)" if row["pct_ma50"] > 2
                          else "var(--color-faint)")
                )
                pct_52w = row["pct_52w_range"]
                pos_color = (
                    "var(--color-negative)" if pct_52w >= 90
                    else ("var(--color-positive)" if pct_52w <= 25
                          else "var(--color-faint)")
                )
                vol_color = (
                    "var(--color-text)" if row["vol_ratio"] >= 1.5
                    else ("var(--color-fainter)" if row["vol_ratio"] < 0.7
                          else "var(--color-faint)")
                )
                state_key = str(row.get("state") or "").upper()
                state_color = {
                    "TRENDING": "var(--color-positive)",
                    "TRANSITION": "var(--color-warning-text)",
                    "BROKEN": "var(--color-negative)",
                }.get(state_key, "var(--color-faint)")
                state_bg = {
                    "TRENDING": "rgba(0, 168, 112, 0.08)",
                    "TRANSITION": "rgba(209, 135, 0, 0.10)",
                    "BROKEN": "rgba(209, 69, 69, 0.09)",
                }.get(state_key, "rgba(100, 116, 139, 0.08)")
                trig_str = (
                    f'{row["trig_dist"]:+.1f}%' if row["trig_dist"] is not None else "—"
                )
                earn_str = (
                    f'{row["earnings_days"]}d' if row["earnings_days"] is not None
                    else "—"
                )
                price_value = row.get("price")
                price_str = f'${price_value:,.2f}' if price_value else "—"
                high_52w = row.get("high_52w")
                low_52w = row.get("low_52w")
                high_52w_str = f'${float(high_52w):,.2f}' if high_52w else "—"
                low_52w_str = f'${float(low_52w):,.2f}' if low_52w else "—"

                # Quality tier styling
                q_tier = row.get("quality") or ""
                if q_tier == "A":
                    q_html = '<span style="font-family:var(--font-sans);font-size:var(--fs-sm);font-weight:600;color:var(--color-accent);">A</span>'
                elif q_tier == "B":
                    q_html = '<span style="font-family:var(--font-sans);font-size:var(--fs-sm);font-weight:600;color:var(--color-faint);">B</span>'
                elif q_tier == "Speculative":
                    q_html = '<span style="font-family:var(--font-sans);font-size:var(--fs-sm);font-weight:600;color:var(--color-purple);">Spec</span>'
                elif q_tier == "Avoid":
                    q_html = '<span style="font-family:var(--font-sans);font-size:var(--fs-sm);font-weight:600;color:var(--color-negative);">Avoid</span>'
                else:
                    q_html = '<span style="color:var(--color-fainter);">—</span>'

                # Ticker cell is a clickable <a> with ?open=TICKER param.
                # Streamlit picks up the param on rerun (handler at top of
                # this block) and switches the active ticker.
                action_emoji = sty.get("emoji", "")
                ticker_link = (
                    f'<a href="?open={row["ticker"]}" target="_self" '
                    f'style="font-weight:600;color:var(--color-text);'
                    f'text-decoration:none;cursor:pointer;">'
                    f'{row["ticker"]}'
                    f'<span style="margin-left:5px;font-size:11px;vertical-align:1px;">{html.escape(action_emoji)}</span>'
                    f'</a>'
                )
                personality_html = (
                    f'<span style="font-family:var(--font-sans);font-size:var(--fs-xs);'
                    f'font-weight:700;letter-spacing:var(--ls-caps);text-transform:uppercase;'
                    f'color:var(--color-faint);">{row["personality_emoji"]} {row["personality"]}</span>'
                )
                dissent = row.get("claude_dissent") or {}
                dissent_star = (
                    f'<span title="{html.escape(dissent.get("reason", ""), quote=True)}" '
                    f'style="margin-left:6px;color:var(--color-blue);font-weight:900;">★</span>'
                    if dissent.get("flag") else ""
                )
                if dissent.get("flag"):
                    pm_cell_html = (
                        f'<a class="watchlist-review-link" href="?open={html.escape(row["ticker"])}" '
                        f'target="_self" title="{html.escape(dissent.get("reason", ""), quote=True)}">'
                        f'Review ★</a>'
                    )
                elif row.get("pm_needs_refresh"):
                    pm_cell_html = (
                        f'<a class="watchlist-review-link" href="?pm_refresh={html.escape(row["ticker"])}" '
                        f'target="_self" title="Refresh PM memo/dossier for {html.escape(row["ticker"])}">'
                        f'{html.escape(str(row["pm_status"]))}</a>'
                    )
                else:
                    pm_cell_html = html.escape(str(row["pm_status"]))
                if compact_watchlist:
                    row_cells = (
                        f'{ticker_link}'
                        f'<span style="text-align:right;color:var(--color-text);">{price_str}</span>'
                        f'<span style="text-align:right;color:{chg_color};">{row["change"]:+.2f}%</span>'
                        f'<span style="font-family:var(--font-sans);font-size:var(--fs-sm);font-weight:600;color:{sty["color"]};">{sty["emoji"]} {sty["label"]}</span>'
                        f'<span style="font-family:var(--font-sans);font-size:var(--fs-xs);font-weight:700;'
                        f'letter-spacing:var(--ls-caps);text-transform:uppercase;color:{row["attention_color"]};">'
                        f'{row["attention"]}</span>'
                        f'<span style="text-align:right;color:var(--color-faint);">{high_52w_str}</span>'
                        f'<span style="text-align:right;color:var(--color-faint);">{low_52w_str}</span>'
                        f'<span style="text-align:right;color:{pos_color};">{pct_52w:.0f}%</span>'
                        f'<span style="text-align:right;color:{vol_color};">{row["vol_ratio"]:.1f}×</span>'
                        f'<span style="text-align:right;color:var(--color-faint);">{trig_str}</span>'
                        f'<span style="font-family:var(--font-sans);font-size:var(--fs-xs);font-weight:800;'
                        f'letter-spacing:var(--ls-caps);text-transform:uppercase;color:{row["pm_status_color"]};">'
                        f'{pm_cell_html}</span>'
                    )
                else:
                    row_cells = (
                        f'{ticker_link}'
                        f'{personality_html}'
                        f'<span style="font-family:var(--font-sans);font-size:var(--fs-xs);font-weight:700;'
                        f'letter-spacing:var(--ls-caps);text-transform:uppercase;color:{row["attention_color"]};">'
                        f'{row["attention"]}</span>'
                        f'<span style="text-align:right;color:var(--color-text);">{price_str}</span>'
                        f'<span style="text-align:right;color:{chg_color};">{row["change"]:+.2f}%</span>'
                        f'<span style="font-family:var(--font-sans);font-size:var(--fs-sm);font-weight:600;color:{sty["color"]};">{sty["emoji"]} {sty["label"]}</span>'
                        f'<span style="font-family:var(--font-sans);font-size:var(--fs-xs);font-weight:700;'
                        f'letter-spacing:var(--ls-caps);text-transform:uppercase;color:{row["pm_status_color"]};">'
                        f'{pm_cell_html}{dissent_star}</span>'
                        f'<span class="state-pill" style="display:inline-flex;width:max-content;align-items:center;'
                        f'border:1px solid {state_color};border-radius:4px;background:{state_bg};'
                        f'padding:2px 6px;font-family:var(--font-sans);font-size:var(--fs-xs);'
                        f'letter-spacing:var(--ls-caps);text-transform:uppercase;color:{state_color};'
                        f'font-weight:700;">{row["state"]}</span>'
                        f'{q_html}'
                        f'<span style="text-align:right;color:{rs_color};">{row["rs"]:.2f}</span>'
                        f'<span style="text-align:right;color:{ma_color};">{row["pct_ma50"]:+.1f}%</span>'
                        f'<span style="text-align:right;color:{pos_color};">{pct_52w:.0f}%</span>'
                        f'<span style="text-align:right;color:{vol_color};">{row["vol_ratio"]:.1f}×</span>'
                        f'<span style="text-align:right;color:var(--color-faint);">{trig_str}</span>'
                        f'<span style="text-align:right;color:var(--color-faint);">{earn_str}</span>'
                    )

                st.markdown(
                    f'<div class="watchlist-grid-row" style="display:grid; {grid_cols} '
                    f'column-gap: 10px; row-gap: 0; padding: 8px 6px; '
                    f'border-bottom: 1px dashed var(--color-border-soft); '
                    f'font-family: var(--font-mono); font-variant-numeric: tabular-nums; '
                    f'font-size: var(--fs-base); align-items: baseline;">{row_cells}</div>',
                    unsafe_allow_html=True,
                )

        # Note: navigation to Analyze view happens by clicking a ticker name
        # in the table (uses ?open=TICKER query param) or via the sidebar
        # watchlist (always visible).


# ─────────────────────────────────────────────────────────────────────
# TRACKER
# ─────────────────────────────────────────────────────────────────────
if view == "tracker":
    # Decision comparison log
    # Trial-period banner — explicit timeline + progress.
    # Date math: trial starts when the FIRST entry was logged. Target
    # is N days from there. Goal is volume of comparisons to evaluate.
    decisions_log_for_banner = st.session_state.store.get("decisions_log", [])
    trial_snapshot = tracker_trial_snapshot()
    needs_rescore = any(
        (d.get("outcome") or {}).get("auto_scored")
        and (d.get("outcome") or {}).get("score_version", 0) < AUTO_SCORE_VERSION
        for d in trial_snapshot.get("decisions", [])
    )
    if trial_snapshot.get("overdue") or needs_rescore:
        auto_scored = auto_close_tracker_outcomes(force_all=True)
        if auto_scored:
            st.success(f"Auto-scored {auto_scored} overdue tracker rows.")
            decisions_log_for_banner = st.session_state.store.get("decisions_log", [])

    if decisions_log_for_banner:
        try:
            first_ts = min(
                datetime.fromisoformat(d["ts"])
                for d in decisions_log_for_banner if d.get("ts")
            )
            trial_end = first_ts + timedelta(days=TRIAL_DAYS)
            days_in = (datetime.now() - first_ts).days
            days_remaining = max(0, (trial_end - datetime.now()).days)
            progress_pct = min(100, round(100 * len(decisions_log_for_banner) / TARGET_COMPARISONS))
            trial_status = (
                f"Day {days_in} of {TRIAL_DAYS} · "
                f"{len(decisions_log_for_banner)}/{TARGET_COMPARISONS} comparisons logged · "
                f"{days_remaining}d remaining"
            )
            if days_remaining == 0:
                trial_status += " · trial complete — evaluate below"
        except Exception:
            trial_status = f"{len(decisions_log_for_banner)} comparisons logged"
            progress_pct = 0
    else:
        trial_status = (
            f"Trial period: {TRIAL_DAYS} days from first log · "
            f"target {TARGET_COMPARISONS} comparisons · "
            f"not started yet"
        )
        progress_pct = 0

    scored_count = sum(1 for d in decisions_log_for_banner if d.get("outcome") is not None)
    entered_open_count = sum(
        1 for d in decisions_log_for_banner
        if d.get("outcome") is None and d.get("position_status") == "entered"
    )
    st.markdown(data_status_html([
        ("Source", "saved decision log", "info"),
        ("Rows", f"{len(decisions_log_for_banner)} logged", "fresh" if decisions_log_for_banner else "warn"),
        ("Outcomes", f"{scored_count} scored", "fresh" if scored_count else "warn"),
        ("Open tracker positions", f"{entered_open_count} tracked", "info" if entered_open_count else "neutral"),
        ("Ownership", "Holdings is explicit-only", "info"),
    ]), unsafe_allow_html=True)

    st.markdown(f"""
<div style="background:linear-gradient(90deg,#F8FAFC 0%, var(--desk-bg) {progress_pct}%, var(--desk-bg) 100%);
        border:1px solid var(--desk-border);border-radius:4px;padding:10px 14px;
        margin-bottom:14px;">
  <div style="font-family: var(--font-sans);font-size:var(--fs-xs);font-weight:600;
          letter-spacing: var(--ls-caps-xl);text-transform:uppercase;color:var(--color-purple);
          margin-bottom:4px;">Calibration trial</div>
  <div style="font-family: var(--font-mono);font-size:var(--fs-sm);color:var(--color-body);">
    {trial_status}
  </div>
</div>
""", unsafe_allow_html=True)

    st.markdown(
        '<div style="font-size:var(--fs-base);color:var(--color-body);line-height:1.55;'
        'margin-bottom:18px;max-width:720px;">'
        'Side-by-side log of rule engine action vs Claude\'s tactical_call vs '
        'your call at the time of viewing each ticker. Use this to evaluate '
        'which decision source produces better calls over a 2-week trial. '
        'Add an outcome to a logged entry once the setup plays out (right / '
        'wrong / unclear) — that\'s the data we use for the final eval.</div>',
        unsafe_allow_html=True,
    )

    decisions_log = st.session_state.store.get("decisions_log", [])
    # Open tracker rows represent active decisions. Keep one active row per
    # ticker, preserving the earliest logged decision so later accidental
    # re-logs do not create duplicate NVDA / PLTR rows.
    try:
        kept_open_tickers = set()
        normalized_log = []
        for entry in sorted(
            decisions_log,
            key=lambda d: d.get("ts") or "",
        ):
            ticker_key = str(entry.get("ticker") or "").upper()
            if entry.get("outcome") is None and ticker_key:
                if ticker_key in kept_open_tickers:
                    continue
                kept_open_tickers.add(ticker_key)
            normalized_log.append(entry)
        if len(normalized_log) != len(decisions_log):
            st.session_state.store["decisions_log"] = normalized_log
            save_store(st.session_state.store)
            decisions_log = normalized_log
    except Exception:
        pass
    unscored = [d for d in decisions_log if d.get("outcome") is None]
    scored = [d for d in decisions_log if d.get("outcome") is not None]

    # ─── Summary metrics (only meaningful once we have scored entries) ──
    def _agreement(entries, source_a, source_b):
        """Return (agree_count, total) for two action source keys.
        Normalizes 'enter_now' (rules) vs 'ENTER' (Claude) so they match."""
        n = 0
        agree = 0
        for d in entries:
            a = (d.get(source_a) or "").upper().replace("_NOW", "")
            b = (d.get(source_b) or "").upper().replace("_NOW", "")
            if a and b:
                n += 1
                if a == b:
                    agree += 1
        return agree, n

    m1, m2, m3, m4 = st.columns(4)
    m1.markdown(f"""
<div style="border:1px solid var(--color-border);border-radius:4px;padding:10px 12px;">
  <div style="font-size:var(--fs-xs);font-weight:600;letter-spacing: var(--ls-caps-lg);text-transform:uppercase;color:var(--color-muted);">Total logged</div>
  <div style="font-family: var(--font-mono);font-size:var(--fs-xl);font-weight:500;margin-top:2px;">{len(decisions_log)}</div>
</div>
""", unsafe_allow_html=True)
    m2.markdown(f"""
<div style="border:1px solid var(--color-border);border-radius:4px;padding:10px 12px;">
  <div style="font-size:var(--fs-xs);font-weight:600;letter-spacing: var(--ls-caps-lg);text-transform:uppercase;color:var(--color-muted);">Outcome scored</div>
  <div style="font-family: var(--font-mono);font-size:var(--fs-xl);font-weight:500;margin-top:2px;">{len(scored)}</div>
</div>
""", unsafe_allow_html=True)

    # Agreement: rules vs claude — does not require outcomes
    rc_agree, rc_total = _agreement(decisions_log, "rule_action", "claude_action")
    rc_pct = round(100 * rc_agree / rc_total) if rc_total else 0
    m3.markdown(f"""
<div style="border:1px solid var(--color-border);border-radius:4px;padding:10px 12px;">
  <div style="font-size:var(--fs-xs);font-weight:600;letter-spacing: var(--ls-caps-lg);text-transform:uppercase;color:var(--color-muted);">Rules ≡ Claude</div>
  <div style="font-family: var(--font-mono);font-size:var(--fs-xl);font-weight:500;margin-top:2px;">{rc_pct}%</div>
  <div style="font-size:var(--fs-xs);color:var(--color-faint);margin-top:1px;">{rc_agree}/{rc_total} agree</div>
</div>
""", unsafe_allow_html=True)

    # Agreement: claude vs user
    cu_agree, cu_total = _agreement(decisions_log, "claude_action", "user_action")
    cu_pct = round(100 * cu_agree / cu_total) if cu_total else 0
    m4.markdown(f"""
<div style="border:1px solid var(--color-border);border-radius:4px;padding:10px 12px;">
  <div style="font-size:var(--fs-xs);font-weight:600;letter-spacing: var(--ls-caps-lg);text-transform:uppercase;color:var(--color-muted);">Claude ≡ You</div>
  <div style="font-family: var(--font-mono);font-size:var(--fs-xl);font-weight:500;margin-top:2px;">{cu_pct}%</div>
  <div style="font-size:var(--fs-xs);color:var(--color-faint);margin-top:1px;">{cu_agree}/{cu_total} agree</div>
</div>
""", unsafe_allow_html=True)

    # ─── Plain-English calibration read ──────────────────────────────
    # The metric cards tell the count; this tells the user what to do
    # with that count. Outcomes are the only true accuracy signal, so when
    # none are scored, keep the language honest: directional read only.
    def _norm_action(value):
        return (value or "").upper().replace("_NOW", "").replace("_", " ")

    def _pair_disagreements(entries, left_key, right_key):
        rows = []
        for d in entries:
            left = _norm_action(d.get(left_key))
            right = _norm_action(d.get(right_key))
            if left and right and left != right:
                rows.append(d)
        return rows

    rules_claude_disagree = _pair_disagreements(decisions_log, "rule_action", "claude_action")
    claude_user_disagree = _pair_disagreements(decisions_log, "claude_action", "user_action")
    entered_open = [
        d for d in unscored
        if d.get("position_status") == "entered"
    ]

    if scored:
        def _right_count_for_summary(source_key):
            return sum(
                1 for d in scored
                if source_key in (d.get("outcome") or {}).get("right_sources", [])
            )
        source_counts = {
            "Rules": _right_count_for_summary("rules"),
            "Claude": _right_count_for_summary("claude"),
            "You": _right_count_for_summary("user"),
        }
        leader, leader_count = max(source_counts.items(), key=lambda kv: kv[1])
        total_scored = len(scored)
        calibration_headline = f"{leader} is leading on scored outcomes so far."
        calibration_body = (
            f"{leader} has been right on {leader_count}/{total_scored} scored rows. "
            f"Rules and Claude agree on {rc_agree}/{rc_total or 0} comparable rows; "
            f"you and Claude agree on {cu_agree}/{cu_total or 0}."
        )
    else:
        calibration_headline = "No accuracy winner yet."
        calibration_body = (
            "You have logged decisions, but none are outcome-scored yet. "
            "The next job is not logging more inputs — it is forcing an outcome call on the open rows."
        )

    if entered_open:
        position_names = [str(d.get("ticker", "")).upper() for d in entered_open if d.get("ticker")]
        position_note = (
            f"{len(entered_open)} open positions are being monitored: "
            + ", ".join(position_names[:6])
            + (" and more." if len(position_names) > 6 else ".")
        )
    else:
        position_note = "No entered positions are active yet."

    disagreement_note = (
        f"Rules vs Claude disagree on {len(rules_claude_disagree)} rows. "
        f"Claude vs your call disagrees on {len(claude_user_disagree)} rows."
    )

    st.markdown(f"""
<div style="border:1px solid var(--color-border);border-left:3px solid var(--color-blue);
        border-radius:4px;background:#FFFFFF;padding:12px 14px;margin:14px 0 16px;">
  <div style="font-family:var(--font-sans);font-size:var(--fs-xs);font-weight:750;
          letter-spacing:var(--ls-caps-lg);text-transform:uppercase;color:var(--color-blue);
          margin-bottom:5px;">Calibration read</div>
  <div style="font-family:var(--font-sans);font-size:var(--fs-md);font-weight:750;
          color:var(--color-text);margin-bottom:4px;">{html.escape(calibration_headline)}</div>
  <div style="font-size:var(--fs-base);line-height:1.5;color:var(--color-body);">
    {html.escape(calibration_body)}
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px;
          border-top:1px dashed var(--color-border-soft);padding-top:9px;
          font-size:var(--fs-sm);line-height:1.45;color:var(--color-muted);">
    <div><b style="color:var(--color-text);">Disagreement:</b> {html.escape(disagreement_note)}</div>
    <div><b style="color:var(--color-text);">Positions:</b> {html.escape(position_note)}</div>
  </div>
</div>
""", unsafe_allow_html=True)

    if unscored:
        st.markdown(f"""
<div style="border:1px solid var(--color-border);border-left:3px solid var(--color-warning-text);
        border-radius:4px;background:#FFFFFF;padding:10px 12px;margin:0 0 14px;">
  <div style="font-family:var(--font-sans);font-size:var(--fs-xs);font-weight:750;
          letter-spacing:var(--ls-caps-lg);text-transform:uppercase;color:var(--color-warning-text);
          margin-bottom:4px;">Decision required</div>
  <div style="font-size:var(--fs-base);line-height:1.45;color:var(--color-body);">
    {len(unscored)} open row{"s" if len(unscored) != 1 else ""} still need a forced outcome.
    Expand a row and choose who was right. Avoid adding more inputs until those are scored.
  </div>
</div>
""", unsafe_allow_html=True)

    # ─── Per-source accuracy (only shows once outcomes are scored) ──
    # For each source (rules / claude / user), count how many times they
    # were marked "right" out of total scored entries. This is the
    # actual evaluation metric the trial period is designed to produce.
    if scored:
        def _right_count(source_key):
            return sum(
                1 for d in scored
                if source_key in (d.get("outcome") or {}).get("right_sources", [])
            )
        rules_right = _right_count("rules")
        claude_right = _right_count("claude")
        user_right = _right_count("user")
        total_scored = len(scored)

        st.markdown(
            '<div style="margin-top:14px;"></div>'
            '<div style="font-family: var(--font-sans);font-size:var(--fs-xs);'
            'font-weight:600;letter-spacing: var(--ls-caps-lg);text-transform:uppercase;'
            'color:var(--color-muted);margin-bottom:6px;">Accuracy by source — scored entries only</div>',
            unsafe_allow_html=True,
        )
        a1, a2, a3 = st.columns(3)
        for col, label, count in [
            (a1, "Rules", rules_right),
            (a2, "Claude", claude_right),
            (a3, "You", user_right),
        ]:
            pct = round(100 * count / total_scored) if total_scored else 0
            col.markdown(f"""
<div style="border:1px solid var(--color-border);border-radius:4px;padding:10px 12px;">
  <div style="font-size:var(--fs-xs);font-weight:600;letter-spacing: var(--ls-caps-lg);text-transform:uppercase;color:var(--color-muted);">{label}</div>
  <div style="font-family: var(--font-mono);font-size:var(--fs-xl);font-weight:500;margin-top:2px;">{pct}%</div>
  <div style="font-size:var(--fs-xs);color:var(--color-faint);margin-top:1px;">{count}/{total_scored} right</div>
</div>
""", unsafe_allow_html=True)

    st.markdown("&nbsp;", unsafe_allow_html=True)

    if not decisions_log:
        st.markdown(
            '<div style="color:var(--color-faintest);font-style:italic;font-size:var(--fs-base);'
            'padding:20px 0;">No decisions logged yet. Open a ticker on '
            'the Analyze tab and click "Log" in Decision comparison to '
            'start collecting data.</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown("""
<style>
.tracker-table {
    border: 1px solid var(--color-border);
    border-radius: 6px;
    overflow: hidden;
    background: #FFFFFF;
}
.tracker-grid {
    display: grid;
    grid-template-columns: 0.75fr 0.78fr 0.95fr 0.95fr 0.95fr 0.75fr 0.85fr 0.85fr 0.75fr 1fr 1.05fr 0.75fr;
    gap: 8px;
    align-items: center;
}
.tracker-head {
    padding: 9px 10px;
    background: #F8FAFC;
    border-bottom: 1px solid var(--color-border);
    font-family: var(--font-mono);
    font-size: var(--fs-xs);
    font-weight: 650;
    letter-spacing: var(--ls-caps-lg);
    text-transform: uppercase;
    color: var(--color-muted);
}
.tracker-row {
    padding: 9px 10px;
    border-bottom: 1px solid var(--color-border-soft);
    font-family: var(--font-mono);
    font-size: var(--fs-sm);
    font-variant-numeric: tabular-nums;
}
.tracker-row:last-child { border-bottom: 0; }
.tracker-ticker {
    font-family: var(--font-sans);
    font-weight: 750;
    color: var(--color-text) !important;
    text-decoration: none !important;
}
.tracker-note {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    margin-left: 5px;
    border: 1px solid var(--color-border);
    border-radius: 4px;
    color: var(--color-muted);
    background: #FFFFFF;
    font-family: var(--font-sans);
    font-size: 10px;
    font-weight: 700;
    cursor: help;
    vertical-align: 1px;
}
.tracker-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-family: var(--font-sans);
    font-size: var(--fs-sm);
    font-weight: 650;
    white-space: nowrap;
}
.tracker-muted { color: var(--color-muted); }
.tracker-faint { color: var(--color-faint); }
.tracker-status {
    font-family: var(--font-sans);
    font-size: var(--fs-xs);
    font-weight: 650;
    letter-spacing: var(--ls-caps);
    text-transform: uppercase;
}
.tracker-position {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-family: var(--font-sans);
    font-size: var(--fs-xs);
    font-weight: 750;
    letter-spacing: var(--ls-caps);
    text-transform: uppercase;
    white-space: nowrap;
}
</style>
""", unsafe_allow_html=True)

        _act_map = {
            "ENTER": "enter_now", "WATCH": "watch", "HOLD_OFF": "hold_off",
            "AVOID": "avoid", "ACCUMULATE": "accumulate",
        }

        def _fmt_px(value):
            try:
                if value is None:
                    return "—"
                return f"${float(value):,.2f}"
            except (TypeError, ValueError):
                return "—"

        def _fmt_date(value):
            return value.strftime("%Y-%m-%d") if value else "—"

        def _action_chip(action, source="rule", confidence=None):
            if source == "claude":
                key = _act_map.get(action or "", "")
                label = STATE_STYLES.get(key, {}).get("label", (action or "—").replace("_", " ").title())
            elif source == "user":
                key = _act_map.get(action or "", "")
                label = STATE_STYLES.get(key, {}).get("label", (action or "—").replace("_", " ").title())
            else:
                key = action or ""
                label = STATE_STYLES.get(key, {}).get("label", (action or "—").replace("_", " ").title())
            sty = STATE_STYLES.get(key, {})
            suffix = f' <span class="tracker-faint">{confidence}/10</span>' if confidence else ""
            return (
                f'<span class="tracker-chip" style="color:{sty.get("color", "var(--color-text)")};">'
                f'{sty.get("emoji", "")} {html.escape(str(label))}{suffix}</span>'
            )

        def _first_hit_after(ticker_value, logged_ts, level, direction):
            if level is None:
                return None
            try:
                level = float(level)
                start_dt = datetime.fromisoformat(str(logged_ts)).date()
            except Exception:
                return None
            hist, _, _ = fetch_history(ticker_value)
            if hist is None or len(hist) == 0:
                return None
            for idx, bar in hist.iterrows():
                try:
                    bar_date = idx.date() if hasattr(idx, "date") else idx.to_pydatetime().date()
                    if bar_date < start_dt:
                        continue
                    high = float(bar.get("High", bar.get("Close")))
                    low = float(bar.get("Low", bar.get("Close")))
                except Exception:
                    continue
                if direction == "down" and low <= level:
                    return bar_date
                if direction == "up" and high >= level:
                    return bar_date
            return None

        def _levels_for(entry):
            logged_price = entry.get("price")
            entry_px = entry.get("entry_price")
            target_px = entry.get("target1_price")
            stop_px = entry.get("stop_price")
            avoid_px = entry.get("avoid_price")
            if entry_px is None and entry.get("user_action") == "AVOID":
                avoid_px = logged_price
            if entry_px is None and avoid_px is None:
                entry_px = logged_price
            return logged_price, entry_px, avoid_px, target_px, stop_px

        def _hit_status(entry):
            ticker_value = entry.get("ticker", "")
            logged_ts = entry.get("ts", "")
            logged_price, entry_px, avoid_px, target_px, stop_px = _levels_for(entry)
            if avoid_px is not None and entry_px is None:
                return "Avoid logged", "tracker-faint"
            entry_hit_at = entry.get("entry_hit_at")
            entry_hit = None
            try:
                direction = "down" if entry_px is not None and logged_price is not None and float(entry_px) < float(logged_price) else "up"
            except Exception:
                direction = "up"
            if entry_hit_at:
                try:
                    entry_hit = datetime.fromisoformat(str(entry_hit_at)).date()
                except Exception:
                    entry_hit = None
            elif entry_px is not None:
                entry_hit = _first_hit_after(ticker_value, logged_ts, entry_px, direction)
                if entry_hit:
                    entry["entry_hit_at"] = entry_hit.isoformat()
                    entry["entry_hit_price"] = round(float(entry_px), 2)
                    entry["position_status"] = "entered"
                    entry["auto_entry_logged"] = True
                    save_store(st.session_state.store)

            target_start = entry.get("entry_hit_at") or logged_ts
            target_hit = _first_hit_after(ticker_value, target_start, target_px, "up") if entry_hit else None
            stop_hit = _first_hit_after(ticker_value, target_start, stop_px, "down") if entry_hit else None
            if entry.get("outcome"):
                return "Scored", "tracker-status"
            if target_hit:
                return f"Target {_fmt_date(target_hit)}", "tracker-status"
            if stop_hit:
                return f"Stop {_fmt_date(stop_hit)}", "tracker-status"
            if entry_hit:
                return f"Entered {_fmt_date(entry_hit)}", "tracker-status"
            if entry_px is not None:
                return f"Waiting {_fmt_px(entry_px)}", "tracker-faint"
            return "Waiting", "tracker-faint"

        def _position_read_for_tracker(entry):
            ticker_value = str(entry.get("ticker", "")).upper()
            if entry.get("position_status") != "entered":
                return None
            hist, _, _ = fetch_history(ticker_value)
            bench = fetch_bench()
            if hist is None or bench is None:
                return None
            t_state = tactical.compute(hist, bench)
            if not t_state:
                return None
            meta = fetch_quote_meta(ticker_value)
            remember_quote_meta(ticker_value, meta)
            t_state = apply_earnings_event_gate(t_state, meta.get("earnings_days") if meta else None)
            return position_management_read(entry, t_state)

        def _position_chip(entry):
            if entry.get("position_status") != "entered":
                if entry.get("entry_price") is not None and entry.get("outcome") is None:
                    return '<span class="tracker-faint">Await entry</span>'
                return '<span class="tracker-faint">—</span>'
            read = _position_read_for_tracker(entry)
            if not read:
                return '<span class="tracker-faint">Review</span>'
            title = (
                f'{read.get("summary", "")} '
                + " · ".join(f"{label}: {value}" for label, value in read.get("stats", []))
            )
            return (
                f'<span class="tracker-position" title="{html.escape(title, quote=True)}" '
                f'style="color:{read["color"]};">'
                f'{read["emoji"]} {html.escape(read["action"])}</span>'
            )

        def _outcome_label(entry):
            outcome = entry.get("outcome") or {}
            if not outcome:
                return "Open"
            srcs = outcome.get("right_sources") or []
            if not srcs:
                family_label = {
                    "long": "Long worked",
                    "avoid": "Avoid right",
                    "wait": "Wait right",
                }.get(outcome.get("winning_family"), "No source right")
                return family_label
            return ", ".join(s.title() for s in srcs)

        def _refresh_claude_for_tracker_entry(entry):
            ticker_value = str(entry.get("ticker", "")).upper()
            hist, company_name, _ = fetch_history(ticker_value)
            bench = fetch_bench()
            if hist is None or len(hist) < 50 or bench is None:
                return False, "Could not load enough price history."
            meta = fetch_quote_meta(ticker_value)
            remember_quote_meta(ticker_value, meta)
            t_state = tactical.compute(hist, bench)
            if t_state is None:
                return False, "Could not compute rule-engine state."
            earnings_days = meta.get("earnings_days") if meta else None
            t_state = apply_earnings_event_gate(t_state, earnings_days)
            modifiers = tactical.decision_modifiers(
                t_state, meta, t_state.get("market_regime", "unknown")
            )
            pm_data = get_cached_pm(
                ticker_value, t_state,
                api_key=api_key if api_key else None,
                company_name=company_name,
            )
            dossier = get_cached_dossier(
                ticker_value, t_state, modifiers, meta, pm_data,
                api_key=api_key if api_key else None,
                company_name=company_name,
            )
            tactical_call = (dossier or {}).get("tactical_call") or {}
            claude_action = (tactical_call.get("action") or "").upper()
            if not claude_action:
                return False, "Claude did not return a tactical call."
            try:
                confidence = int(tactical_call.get("confidence", 0) or 0)
            except (TypeError, ValueError):
                confidence = 0
            entry["claude_action"] = claude_action
            entry["claude_confidence"] = confidence
            entry["claude_reasoning"] = (tactical_call.get("reasoning") or dossier.get("dossier") or "").strip()
            entry["claude_trigger"] = (tactical_call.get("trigger") or "").strip()
            entry.setdefault("entry_price", round(float(t_state.get("entry")), 2) if t_state.get("entry") is not None else None)
            entry.setdefault("stop_price", round(float(t_state.get("stop")), 2) if t_state.get("stop") is not None else None)
            entry.setdefault("target1_price", round(float(t_state.get("t1")), 2) if t_state.get("t1") is not None else None)
            entry.setdefault("target2_price", round(float(t_state.get("t2")), 2) if t_state.get("t2") is not None else None)
            entry["claude_refreshed_at"] = datetime.now().isoformat(timespec="seconds")
            save_store(st.session_state.store)
            return True, f"Claude refreshed for {ticker_value}."

        def _render_tracker_table(entries):
            st.markdown(
                '<div class="tracker-table">'
                '<div class="tracker-grid tracker-head">'
                '<span>Ticker</span><span>Logged</span><span>Claude</span><span>Rules</span><span>You</span>'
                '<span style="text-align:right;">Ref px</span><span style="text-align:right;">Entry / avoid</span>'
                '<span style="text-align:right;">Target</span><span style="text-align:right;">Stop</span>'
                '<span>When hit</span><span>Position</span><span>Outcome</span>'
                '</div>',
                unsafe_allow_html=True,
            )
            for entry in entries:
                ticker_value = str(entry.get("ticker", "")).upper()
                logged_price, entry_px, avoid_px, target_px, stop_px = _levels_for(entry)
                setup_px = avoid_px if avoid_px is not None else entry_px
                setup_label = _fmt_px(setup_px)
                if avoid_px is not None:
                    setup_label = f"Avoid {_fmt_px(avoid_px)}"
                hit_text, hit_class = _hit_status(entry)
                raw_note = str(entry.get("user_note") or "").strip()
                note_icon = (
                    f'<span class="tracker-note" title="{html.escape(raw_note, quote=True)}">N</span>'
                    if raw_note else ""
                )
                st.markdown(
                    f'<div class="tracker-grid tracker-row">'
                    f'<span><a class="tracker-ticker" href="?open={html.escape(ticker_value)}" target="_self">{html.escape(ticker_value)}</a>{note_icon}</span>'
                    f'<span class="tracker-muted">{html.escape(str(entry.get("ts", "")[:10]))}</span>'
                    f'<span>{_action_chip(entry.get("claude_action"), "claude", entry.get("claude_confidence"))}</span>'
                    f'<span>{_action_chip(entry.get("rule_action"), "rule")}</span>'
                    f'<span>{_action_chip(entry.get("user_action"), "user")}</span>'
                    f'<span style="text-align:right;">{_fmt_px(logged_price)}</span>'
                    f'<span style="text-align:right;">{setup_label}</span>'
                    f'<span style="text-align:right;">{_fmt_px(target_px)}</span>'
                    f'<span style="text-align:right;">{_fmt_px(stop_px)}</span>'
                    f'<span class="{hit_class}">{html.escape(hit_text)}</span>'
                    f'<span>{_position_chip(entry)}</span>'
                    f'<span class="tracker-muted">{html.escape(_outcome_label(entry))}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            st.markdown('</div>', unsafe_allow_html=True)

        def _render_row_actions(entries, scored_view):
            _act_map = {
                "ENTER": "enter_now", "WATCH": "watch", "HOLD_OFF": "hold_off",
                "AVOID": "avoid", "ACCUMULATE": "accumulate",
            }
            def _input_value(value):
                try:
                    if value is None:
                        return ""
                    return f"{float(value):.2f}"
                except (TypeError, ValueError):
                    return ""

            def _parse_optional_price(value):
                value = str(value or "").strip().replace("$", "").replace(",", "")
                if not value:
                    return None
                try:
                    return round(float(value), 2)
                except ValueError:
                    return "invalid"

            for entry in entries:
                entry_id = entry.get("id", "")
                ticker_value = str(entry.get("ticker", "")).upper()
                expander_title = (
                    f"Resolve now · {ticker_value} · {entry.get('ts', '')[:10]}"
                    if not scored_view
                    else f"Scored · {ticker_value} · {entry.get('ts', '')[:10]}"
                )
                with st.expander(expander_title, expanded=False):
                    if not scored_view:
                        st.markdown(
                            '<div style="font-size:var(--fs-xs);font-weight:750;'
                            'letter-spacing:var(--ls-caps-lg);text-transform:uppercase;'
                            'color:var(--color-blue);margin:2px 0 6px;">Final evaluation</div>'
                            '<div style="font-size:var(--fs-sm);color:var(--color-muted);'
                            'line-height:1.4;margin-bottom:8px;">'
                            'Make the call now: which source was most right for this trade/setup?</div>',
                            unsafe_allow_html=True,
                        )
                        outcome_choice = st.selectbox(
                            "Forced outcome",
                            options=["Choose outcome", "Rules", "Claude", "You", "All three", "None / unclear"],
                            key=f"outcome_choice_{entry_id}",
                            label_visibility="collapsed",
                        )
                        if st.button("Score final outcome", key=f"save_outcome_{entry_id}", use_container_width=True):
                            if outcome_choice == "Choose outcome":
                                st.warning("Pick an outcome first. This is the point of the tracker.")
                            else:
                                right_sources = {"Rules":["rules"],"Claude":["claude"],"You":["user"],"All three":["rules","claude","user"]}.get(outcome_choice,[])
                                entry["outcome"] = {
                                    "ts": datetime.now().isoformat(timespec="seconds"),
                                    "result": "right" if right_sources else "unclear",
                                    "right_sources": right_sources,
                                    "result_pct": None,
                                    "note": "",
                                }
                                save_store(st.session_state.store)
                                st.rerun()
                        st.markdown('<div style="border-top:1px dashed var(--color-border-soft);margin:12px 0;"></div>', unsafe_allow_html=True)
                    if entry.get("user_note"):
                        st.caption(f"Your note: {entry.get('user_note')}")
                    if entry.get("position_status") == "entered" and entry.get("entry_hit_at"):
                        st.caption(
                            f"Auto-entry logged: {_fmt_px(entry.get('entry_hit_price') or entry.get('entry_price'))} "
                            f"on {entry.get('entry_hit_at')}"
                        )
                        tracker_position_read = _position_read_for_tracker(entry)
                        if tracker_position_read:
                            stat_line = " · ".join(
                                f"{label}: {value}"
                                for label, value in tracker_position_read["stats"][:5]
                            )
                            st.markdown(
                                f'<div style="border:1px solid var(--color-border);'
                                f'border-left:3px solid {tracker_position_read["color"]};'
                                f'border-radius:4px;padding:8px 10px;margin:6px 0 8px;'
                                f'background:#FFFFFF;font-size:var(--fs-sm);line-height:1.45;">'
                                f'<b style="color:{tracker_position_read["color"]};">'
                                f'{tracker_position_read["emoji"]} {html.escape(tracker_position_read["action"])}</b>'
                                f' — {html.escape(tracker_position_read["summary"])}'
                                f'<div style="font-family:var(--font-mono);color:var(--color-muted);margin-top:4px;">'
                                f'{html.escape(stat_line)}</div>'
                                f'</div>',
                                unsafe_allow_html=True,
                            )
                    elif entry.get("entry_price") is not None and entry.get("user_action") in ("WATCH", "ENTER", "ACCUMULATE"):
                        st.caption(
                            f"Waiting for entry trigger: {_fmt_px(entry.get('entry_price'))}. "
                            "Once hit, this row automatically becomes an active entry."
                        )
                        if st.button("Mark as in position", key=f"tracker_mark_entered_{entry_id}", use_container_width=True):
                            entry["position_status"] = "entered"
                            entry["entry_hit_at"] = datetime.now().date().isoformat()
                            entry["entry_hit_price"] = entry.get("entry_price") or entry.get("price")
                            entry["manual_entry_logged"] = True
                            save_store(st.session_state.store)
                            st.rerun()

                    st.markdown(
                        '<div style="font-size:var(--fs-xs);font-weight:700;'
                        'letter-spacing:var(--ls-caps-lg);text-transform:uppercase;'
                        'color:var(--color-muted);margin:10px 0 4px;">Edit levels</div>',
                        unsafe_allow_html=True,
                    )
                    e_col, t_col, s_col = st.columns(3)
                    current_entry_px = entry.get("entry_hit_price") or entry.get("entry_price")
                    with e_col:
                        edit_entry_px = st.text_input(
                            "Entry",
                            value=_input_value(current_entry_px),
                            key=f"edit_entry_px_{entry_id}",
                            placeholder="Entry",
                        )
                    with t_col:
                        edit_target_px = st.text_input(
                            "Target",
                            value=_input_value(entry.get("target1_price")),
                            key=f"edit_target_px_{entry_id}",
                            placeholder="Target",
                        )
                    with s_col:
                        edit_stop_px = st.text_input(
                            "Stop",
                            value=_input_value(entry.get("stop_price")),
                            key=f"edit_stop_px_{entry_id}",
                            placeholder="Stop",
                        )
                    edit_note = st.text_input(
                        "Note",
                        value=str(entry.get("user_note") or ""),
                        key=f"edit_note_{entry_id}",
                        placeholder="Optional note",
                    )
                    if st.button("Save edited levels", key=f"save_levels_{entry_id}", use_container_width=True):
                        parsed_entry = _parse_optional_price(edit_entry_px)
                        parsed_target = _parse_optional_price(edit_target_px)
                        parsed_stop = _parse_optional_price(edit_stop_px)
                        if "invalid" in (parsed_entry, parsed_target, parsed_stop):
                            st.warning("One of the edited prices is not a valid number.")
                        else:
                            if parsed_entry is not None:
                                entry["entry_price"] = parsed_entry
                                if entry.get("position_status") == "entered":
                                    entry["entry_hit_price"] = parsed_entry
                            entry["target1_price"] = parsed_target
                            entry["stop_price"] = parsed_stop
                            entry["user_note"] = edit_note.strip()
                            entry["levels_edited_at"] = datetime.now().isoformat(timespec="seconds")
                            save_store(st.session_state.store)
                            st.success("Levels updated.")
                            st.rerun()

                    if entry.get("agreement_read"):
                        st.caption(f"Comparison read: {entry.get('agreement_read')}")
                    if not entry.get("claude_action"):
                        st.caption("Claude is missing for this saved row.")
                        if st.button("Refresh Claude", key=f"refresh_claude_{entry_id}", use_container_width=True):
                            ok, msg = _refresh_claude_for_tracker_entry(entry)
                            if ok:
                                st.success(msg)
                                st.rerun()
                            else:
                                st.warning(msg)
                    if entry.get("claude_trigger"):
                        st.caption(f"Trigger: {entry.get('claude_trigger')}")
                    if entry.get("claude_reasoning"):
                        st.caption(str(entry.get("claude_reasoning"))[:350] + ("…" if len(str(entry.get("claude_reasoning"))) > 350 else ""))
                    if st.button("Delete", key=f"del_decision_{entry_id}"):
                        st.session_state.store["decisions_log"] = [
                            d for d in st.session_state.store["decisions_log"] if d.get("id") != entry_id
                        ]
                        save_store(st.session_state.store)
                        st.rerun()

        # Table-first tracker: same scan pattern as Watchlist, with the
        # scoring controls tucked behind per-row expanders.
        sub_open, sub_resolved = st.tabs([f"Open ({len(unscored)})", f"Resolved ({len(scored)})"])
        with sub_open:
            if not unscored:
                st.markdown(
                    '<div style="color:var(--color-faintest);font-style:italic;font-size:var(--fs-base);'
                    'padding:14px 0;">No open decisions.</div>',
                    unsafe_allow_html=True,
                )
            else:
                _render_tracker_table(unscored)
                _render_row_actions(unscored, scored_view=False)

        with sub_resolved:
            if not scored:
                st.markdown(
                    '<div style="color:var(--color-faintest);font-style:italic;font-size:var(--fs-base);'
                    'padding:14px 0;">No outcomes scored yet.</div>',
                    unsafe_allow_html=True,
                )
            else:
                # Per-source accuracy
                sources = ["rules", "claude", "user"]
                sac1, sac2, sac3 = st.columns(3)
                for col, src in zip([sac1, sac2, sac3], sources):
                    right_n = sum(
                        1 for d in scored
                        if src in (d.get("outcome") or {}).get("right_sources", [])
                    )
                    total = sum(
                        1 for d in scored
                        if (d.get("outcome") or {}).get("right_sources")
                    )
                    pct = round(100 * right_n / total) if total else 0
                    col.markdown(f"""
<div style="border:1px solid var(--color-border);border-radius:4px;padding:10px 12px;">
  <div style="font-size:var(--fs-xs);font-weight:600;letter-spacing: var(--ls-caps-lg);text-transform:uppercase;color:var(--color-muted);">{src.title()} accuracy</div>
  <div style="font-family: var(--font-mono);font-size:var(--fs-xl);font-weight:500;margin-top:2px;">{pct}%</div>
  <div style="font-size:var(--fs-xs);color:var(--color-faint);margin-top:1px;">{right_n}/{total}</div>
</div>
""", unsafe_allow_html=True)

                st.markdown("&nbsp;", unsafe_allow_html=True)
                _render_tracker_table(scored)
                _render_row_actions(scored, scored_view=True)
