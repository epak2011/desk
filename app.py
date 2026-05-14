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
import html
from pathlib import Path
from datetime import datetime, timedelta

import streamlit as st
import yfinance as yf

import tactical
from pm_view import get_pm_view, get_decision_dossier, STATIC_SNAPSHOTS


st.set_page_config(
    page_title="Trading Desk",
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

# Display-only fallbacks for common watchlist names when Yahoo omits profile
# metadata during rate-limit windows. Live quote/math still comes from data.
FALLBACK_PROFILE_META = {
    "ASTS": {"name": "AST SpaceMobile", "sector": "Communication Services"},
    "SATS": {"name": "EchoStar Corporation", "sector": "Communication Services"},
    "NVDA": {"name": "NVIDIA Corporation", "sector": "Technology"},
    "AVGO": {"name": "Broadcom Inc.", "sector": "Technology"},
    "PLTR": {"name": "Palantir Technologies", "sector": "Technology"},
    "DASH": {"name": "DoorDash", "sector": "Consumer Internet"},
    "COIN": {"name": "Coinbase Global", "sector": "Financial Services"},
    "BTC-USD": {"name": "Bitcoin", "sector": "Crypto"},
}

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


def _pg_connect():
    """Lazy-import psycopg only if we're using Postgres."""
    import psycopg
    return psycopg.connect(_get_database_url(), autocommit=True)


def _pg_init():
    """Ensure the kv_store table exists. Single-row store for now —
    one row, key='default', value=jsonb. Simple is fine for this scale."""
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY,
                    value JSONB NOT NULL,
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
    }


def load_store():
    if USE_POSTGRES:
        try:
            _pg_init()
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM kv_store WHERE key = 'default'")
                    row = cur.fetchone()
                    if row:
                        return row[0]
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


def save_store(store):
    if USE_POSTGRES:
        try:
            with _pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO kv_store (key, value, updated_at)
                        VALUES ('default', %s::jsonb, NOW())
                        ON CONFLICT (key) DO UPDATE
                            SET value = EXCLUDED.value, updated_at = NOW()
                    """, (json.dumps(store),))
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
    STORE_PATH.write_text(json.dumps(store, indent=2))


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
    if "decisions_log" not in st.session_state.store:
        st.session_state.store["decisions_log"] = []
        _needs_save = True
    if _needs_save:
        save_store(st.session_state.store)
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
    # Count this fresh Claude call if it actually hit the API. Source is
    # "claude" on success, "static" or "static (claude call failed: ...)"
    # on fallback — only the first counts toward session cost.
    if pm.get("_source") == "claude":
        st.session_state["claude_calls_this_session"] = (
            st.session_state.get("claude_calls_this_session", 0) + 1
        )
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
    if changed:
        save_store(st.session_state.store)


def get_cached_dossier(ticker, t_state, modifiers, meta, pm_data, api_key, company_name):
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
    if not api_key:
        return {"dossier": None, "technical_narrative": None,
                "pm_narrative": None, "bullets": {}, "quality": {},
                "_source": "unavailable"}
    ticker = ticker.upper()
    cache = st.session_state.store.setdefault("dossier_cache", {})
    entry = cache.get(ticker)
    current_price = t_state.get("price") if isinstance(t_state, dict) else None
    current_action = t_state.get("action") if isinstance(t_state, dict) else None

    if entry:
        try:
            age = datetime.now() - datetime.fromisoformat(entry.get("ts"))
            cached_price = entry.get("price_at_generation")
            cached_action = entry.get("action_at_generation")

            staleness_failed = age >= timedelta(days=PM_CACHE_TTL_DAYS)

            if not staleness_failed and cached_price and current_price:
                pct_moved = abs(current_price - cached_price) / cached_price
                if pct_moved >= 0.10:
                    staleness_failed = True

            if not staleness_failed and cached_action and current_action:
                if cached_action != current_action:
                    staleness_failed = True

            if not staleness_failed:
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

    # Cache miss or staleness failed — regenerate via Claude.
    result = get_decision_dossier(
        ticker, t_state, modifiers, meta, pm_data,
        api_key=api_key, company_name=company_name,
    )
    if result.get("dossier"):
        st.session_state["claude_calls_this_session"] = (
            st.session_state.get("claude_calls_this_session", 0) + 1
        )
        cache[ticker] = {
            "ts": datetime.now().isoformat(timespec="seconds"),
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
        save_store(st.session_state.store)

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
    --color-surface-trigger: #FFF7DF;
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
    gap: 14px;
    align-items: stretch;
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
    margin-bottom: 12px;
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
    font-family: var(--font-serif);
    font-size: var(--fs-xl) !important;
    font-weight: 600;
    line-height: 1.2;
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
    margin-top: 14px;
    padding-top: 12px;
    border-top: 1px dashed var(--color-border-soft);
    font-size: var(--fs-md) !important;
    line-height: 1.65;
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
    margin-top: 8px;
    font-size: var(--fs-base) !important;
    color: var(--color-body);
}
.desk-cmp-trigger-label {
    font-size: var(--fs-xs) !important;
    font-weight: 600;
    letter-spacing: var(--ls-caps-md);
    text-transform: uppercase;
    color: var(--color-faint);
}
.desk-cmp-yourcall-label {
    margin-top: 14px;
    padding-top: 12px;
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
.desk-cmp-badge-disagree { background: #FEF3C7; color: #92400E; }
.desk-cmp-badge-agree    { background: #D1FAE5; color: #065F46; }
.desk-cmp-badge-unknown  { background: var(--color-surface-soft); color: var(--color-muted); }

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
.desk-mod-med  { background: var(--color-surface-trigger); border-color: #F5D88A; color: var(--color-warning-text); }
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
    background: rgba(255, 255, 255, 0.92);
    color: var(--color-text);
    padding: 8px 0 9px;
    display: flex; justify-content: space-between; align-items: center;
    margin: 0 0 calc(1.2rem + 52px);
    position: relative;
    z-index: 999;
    border-bottom: 1px solid var(--color-border);
    box-shadow: 0 8px 24px rgba(15, 23, 42, 0.055);
    backdrop-filter: blur(12px);
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
    background: #F0EDE5;
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
    background: #FFF1C4; padding: 0 6px; border-radius: 2px;
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
    border: 1px solid #F5D88A;
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
    border: 1px solid var(--color-border);
    border-radius: 12px;
    padding: 20px 22px 22px;
    margin-bottom: 24px;
    background: rgba(255, 255, 255, 0.88);
    box-shadow: 0 18px 38px rgba(15, 23, 42, 0.055);
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
    font-family: var(--font-serif);
    font-size: 54px;
    line-height: 1;
    font-weight: 500;
    margin: 8px 0 8px;
}
.research-page .deck {
    font-family: var(--font-serif);
    font-size: var(--fs-xl);
    line-height: 1.45;
    color: var(--color-body);
    max-width: 840px;
}
.research-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
    margin: 18px 0 8px;
}
.research-kpi {
    border: 1px solid var(--color-border);
    border-radius: 8px;
    padding: 12px 13px;
    background: rgba(255, 255, 255, 0.94);
    box-shadow: 0 10px 22px rgba(15, 23, 42, 0.04);
}
.research-kpi .k {
    font-family: var(--font-mono);
    font-size: var(--fs-xs);
    letter-spacing: var(--ls-caps-lg);
    text-transform: uppercase;
    color: var(--color-muted);
    font-weight: 600;
}
.research-kpi .v {
    font-family: var(--font-mono);
    font-size: var(--fs-xl);
    font-weight: 500;
    margin-top: 4px;
}
.research-layout {
    display: grid;
    grid-template-columns: minmax(0, 1.1fr) minmax(320px, 0.9fr);
    gap: 28px;
}
.research-section {
    border-top: 1px solid var(--color-border);
    padding-top: 18px;
    margin-top: 18px;
}
.research-section h2 {
    font-family: var(--font-serif);
    font-size: 28px;
    line-height: 1.1;
    font-weight: 500;
    margin: 8px 0 10px;
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
@media (max-width: 900px) {
    .research-grid,
    .research-layout { grid-template-columns: 1fr; }
    .research-page h1 { font-size: 42px; }
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
    background: rgba(255, 255, 255, 0.82) !important;
    border: 1px solid rgba(17, 24, 39, 0.08) !important;
    border-top: 3px solid var(--color-accent) !important;
    box-shadow: 0 16px 36px rgba(37, 99, 235, 0.08) !important;
}
.desk-bar .wordmark {
    color: #0B1020 !important;
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

    /* Ticker row: shrink sym, hide secondary meta line so the row fits */
    .desk-ticker-row .sym { font-size: var(--fs-xl) !important; }
    .desk-ticker-row .name { font-size: var(--fs-sm) !important; margin-left: 6px !important; }
    .desk-ticker-row .price { font-size: var(--fs-md) !important; }
    .desk-ticker-row .chg { font-size: var(--fs-sm) !important; margin-left: 6px !important; }
    .desk-ticker-row .meta-inline { font-size: var(--fs-xs) !important; }

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
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_history(ticker):
    """Fetch 2y daily history + name. Returns (hist, name, err_reason).

    err_reason is None on success; otherwise a short string explaining why
    the fetch failed (rate limit, 404, JSON decode error, etc.). The UI
    surfaces this so the user sees WHY data didn't load instead of a
    generic 'couldn't find data' message.
    """
    try:
        yf_ticker = yf.Ticker(ticker)
        hist = yf_ticker.history(period="2y", interval="1d", auto_adjust=True)
        if hist is None or len(hist) == 0:
            return None, None, "Yahoo returned no rows for this ticker (possibly delisted, wrong symbol, or yfinance API drift)"
        info = {}
        try:
            info = yf_ticker.info or {}
        except Exception:
            pass
        name = info.get("longName") or info.get("shortName") or ticker
        return hist, name, None
    except Exception as e:
        # yfinance 1.x can raise AttributeError on internal None/Response
        # mishandling when Yahoo's API drifts. Treat all exceptions the
        # same: surface the reason so the user can debug, not just shrug.
        return None, None, f"{type(e).__name__}: {str(e)[:160]}"


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_bench():
    try:
        hist = yf.Ticker("SPY").history(period="2y", interval="1d", auto_adjust=True)
        if hist is None or len(hist) == 0:
            return None
        return hist
    except Exception:
        return None


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_quote_meta(ticker):
    """Pull sector, market cap, short interest, earnings date, valuation ratios,
    analyst rating + target, dividend yield, growth rate, debt/equity.
    All optional. Cached 1 hour.
    """
    out = {
        "long_name": None,
        "short_name": None,
        "sector": None,
        "industry": None,
        "market_cap": None,
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

        out["long_name"] = info.get("longName")
        out["short_name"] = info.get("shortName")
        out["sector"] = info.get("sector")
        out["industry"] = info.get("industry")
        out["market_cap"] = info.get("marketCap")
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
    meta = fetch_quote_meta(ticker)
    fin = fetch_financial_snapshot(ticker)
    fallback_profile = FALLBACK_PROFILE_META.get(ticker, {})
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

    t = tactical.compute(hist, bench)
    api_key = get_effective_api_key()
    pm = get_cached_pm(ticker, t, api_key=api_key if api_key else None, company_name=company)
    modifiers = tactical.decision_modifiers(t, meta, t.get("market_regime", "unknown"))
    dossier = get_cached_dossier(
        ticker, t, modifiers, meta, pm,
        api_key=api_key if api_key else None, company_name=company,
    )
    quality = (dossier or {}).get("quality") or {}
    q_label = quality.get("tier") or "Unrated"
    q_text = quality.get("rationale") or pm.get("thesis") or "No long-form quality note is available yet."

    market_cap = format_market_cap(meta.get("market_cap")) or "—"
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

    if t.get("action") == "enter_now":
        timing = "Technicals are supportive enough for immediate action."
    elif t.get("action") == "watch":
        timing = "The business may be interesting, but the entry still depends on a cleaner trigger."
    elif t.get("action") == "hold_off":
        timing = "The report can support watchlist work, but the trade setup is not clean enough yet."
    else:
        timing = "The current setup does not justify fresh exposure."

    kpis = [
        ("Price", f"${t['price']:,.2f}"),
        ("Market cap", market_cap or "—"),
        ("Enterprise value", enterprise_value),
        ("Revenue YoY", fmt_pct(revenue_yoy)),
        ("Gross margin", fmt_pct(gross_margin)),
        ("FCF margin", fmt_pct(fcf_margin)),
        ("EV/Sales", fmt_mult(meta.get("enterprise_to_revenue"))),
        ("EV/EBITDA", fmt_mult(ev_ebitda)),
        ("Quality", q_label),
    ]

    def kpi_html():
        return "".join(
            f'<div class="research-kpi"><div class="k">{html.escape(k)}</div>'
            f'<div class="v">{html.escape(str(v))}</div></div>'
            for k, v in kpis
        )

    def table_html(title, rows):
        body = "".join(
            f"<tr><td>{html.escape(label)}</td><td>{html.escape(value)}</td></tr>"
            for label, value in rows
        )
        return (
            f'<div class="research-section"><div class="eyebrow">{html.escape(title)}</div>'
            f'<table class="research-table"><tbody>{body}</tbody></table></div>'
        )

    earnings_label = "Next earnings"
    if meta.get("earnings_date") and meta.get("earnings_days") is not None:
        d = meta["earnings_days"]
        earnings_value = meta["earnings_date"].strftime("%b %d") + (f" · in {d}d" if d >= 0 else f" · {abs(d)}d ago")
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
    growth_rows = [
        ("Trailing revenue", fmt_big_number(meta.get("total_revenue"))),
        ("Revenue growth YoY", fmt_pct(revenue_yoy)),
        ("Earnings growth YoY", fmt_pct(meta.get("earnings_growth"))),
        ("52-week position", f"{t.get('pct_of_52w_range'):.0f}% of range" if t.get("pct_of_52w_range") is not None else "—"),
        ("Relative strength", f"{t.get('rs', 1):.2f} vs SPX"),
        ("Volume", f"{t.get('vol_ratio', 1):.2f}x 20d avg"),
    ]
    balance_rows = [
        ("Cash & investments", fmt_big_number(cash)),
        ("Total debt", fmt_big_number(debt)),
        ("Net cash / debt", fmt_big_number(net_cash)),
        ("Debt / equity", fmt_pct(meta.get("debt_to_equity"))),
    ]
    valuation_rows = [
        ("Market cap", market_cap),
        ("Enterprise value", enterprise_value),
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
        ("Dividend yield", fmt_pct(meta.get("dividend_yield")) if meta.get("dividend_yield") is not None else "—"),
        ("Quality tier", q_label),
        ("Tactical state", STATE_STYLES.get(t.get("action"), {}).get("label", t.get("action", "—"))),
    ]
    watch_items = [
        f"Reclaim or lose the 50-day moving average at ${t.get('ma50', 0):,.2f}.",
        f"Hold the 200-day moving average near ${t.get('ma200', 0):,.2f}.",
        "Watch whether revenue growth is translating into cash flow rather than only headline scale.",
        "Track whether valuation multiples compress because fundamentals disappoint or because the stock de-risks into the numbers.",
    ]

    st.markdown(f"""
<div class="research-page">
  <div class="hero">
    <div class="eyebrow">Full research report · {html.escape(ticker)}</div>
    <h1>{html.escape(company)}</h1>
    <div class="deck">{html.escape(timing)} This report focuses on the business, earnings quality,
    financial trajectory, balance sheet, valuation, and the main debate; the trading trigger stays on the desk.</div>
    <div class="research-grid">{kpi_html()}</div>
  </div>
  <div class="research-layout">
    <div>
      <div class="research-section">
        <div class="eyebrow">Research read-through</div>
        <h2>What matters most</h2>
        <p>{html.escape(q_text)}</p>
        <p>{html.escape(pm.get("valuation") or "Valuation context is not available yet.")}</p>
      </div>
      <div class="research-section">
        <div class="eyebrow">Business model</div>
        <h2>How the company makes money</h2>
        <p>{html.escape(pm.get("thesis") or "Business summary is generated from the PM view and financial profile.")}</p>
      </div>
      <div class="research-section">
        <div class="eyebrow">Financial trajectory</div>
        <h2>Scale, growth, and quality of revenue</h2>
        <p>{html.escape(company)} is currently showing {fmt_pct(revenue_yoy)} revenue growth with
        {fmt_pct(gross_margin)} gross margins and {fmt_pct(fcf_margin)} free-cash-flow margins. The key question
        is whether growth is being converted into durable earnings power, or whether the market is paying for scale
        before the model has proved normalized profitability.</p>
      </div>
      <div class="research-section">
        <div class="eyebrow">Bull / bear debate</div>
        <h2>The variant perception</h2>
        <p><b>Bull case:</b> {html.escape((pm.get("drivers") or ["Execution improves and the market assigns a higher-quality multiple."])[0])}</p>
        <p><b>Bear case:</b> {html.escape((pm.get("risks") or ["Valuation is already discounting too much good news."])[0])}</p>
      </div>
      <div class="research-section">
        <div class="eyebrow">Drivers</div>
        <h2>What could make the stock work</h2>
        <ul>{''.join(f'<li>{html.escape(str(d))}</li>' for d in pm.get('drivers', []))}</ul>
      </div>
      <div class="research-section">
        <div class="eyebrow">Risks</div>
        <h2>What could break the thesis</h2>
        <ul>{''.join(f'<li>{html.escape(str(r))}</li>' for r in pm.get('risks', []))}</ul>
      </div>
    </div>
    <div>
      {table_html("Earnings highlights", earnings_rows)}
      {table_html("Growth and trading context", growth_rows)}
      {table_html("Balance sheet", balance_rows)}
      {table_html("Valuation", valuation_rows)}
      {table_html("Ownership and setup", ownership_rows)}
      <div class="research-section">
        <div class="eyebrow">Tactical overlay</div>
        <h2>{html.escape(STATE_STYLES.get(t.get("action"), {}).get("label", t.get("action", "Watch")))} now</h2>
        <p>{html.escape(decision_context(t))}</p>
      </div>
      <div class="research-section">
        <div class="eyebrow">What to watch next</div>
        <h2>Next proof points</h2>
        <ul>{''.join(f'<li>{html.escape(str(item))}</li>' for item in watch_items)}</ul>
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
        "color": "#00A870", "label": "Enter", "emoji": "⚡",
        "tagline": "High-conviction setup — buy without waiting on a condition",
        "criteria": [
            "Bullish bias — above both 50d and 200d MAs, both rising, leading the index",
            "Setup score ≥ 9/10 — trend, structure, and volume all aligned",
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
        return f"Enter long at market — ${t['price']:,.2f}."
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
            return f"Hold of ${buy:,.2f} — {descriptor}, wait for tap-and-bounce."
        if buy:
            return f"Close above ${buy:,.2f}."
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
        return f"Below ${t['stop']:,.2f}, setup is invalid."
    if t["action"] == "watch" and t.get("trigger"):
        abort = t["trigger"].get("levels", {}).get("abort_below")
        if abort:
            return f"Below ${abort:,.2f}, setup is invalid."
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
    if "open" in qp_global:
        tkr_to_open = qp_global.get("open")
        del qp_global["open"]
        if tkr_to_open and tkr_to_open != st.session_state.current_ticker:
            st.session_state.current_ticker = tkr_to_open
            st.session_state.view = "analyze"
            st.rerun()
    if "wldel" in qp_global:
        tkr_to_del = qp_global.get("wldel")
        del qp_global["wldel"]
        if tkr_to_del and tkr_to_del in st.session_state.store.get("watchlist", []):
            st.session_state.store["watchlist"].remove(tkr_to_del)
            save_store(st.session_state.store)
            if tkr_to_del == st.session_state.current_ticker and st.session_state.store["watchlist"]:
                st.session_state.current_ticker = st.session_state.store["watchlist"][0]
            st.rerun()
    if "pm_refresh" in qp_global:
        tkr_to_refresh = qp_global.get("pm_refresh")
        del qp_global["pm_refresh"]
        if tkr_to_refresh:
            clear_pm_cache(tkr_to_refresh)
            clear_dossier_cache(tkr_to_refresh)
            fetch_quote_meta.clear()
            fetch_history.clear()
            st.rerun()
except Exception:
    pass


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
        st.session_state.current_ticker = input_ticker
        st.session_state["_last_synced_ticker"] = input_ticker

    st.markdown("---")
    st.markdown(
        '<div style="font-family: var(--font-mono);font-size:var(--fs-xs);'
        'font-weight:600;letter-spacing: var(--ls-caps-xl);text-transform:uppercase;'
        'color:var(--color-muted);margin:6px 0 8px;">Watchlist</div>',
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
    background: #EDE8DD !important;
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
    background: #101114 !important;
    color: #FFFFFF !important;
    border: none !important;
    border-radius: 0 !important;
    border-top: none !important;
    box-shadow: 0 12px 28px rgba(16, 17, 20, 0.12) !important;
}
.desk-bar .wordmark,
.desk-bar .meta {
    color: #FFFFFF !important;
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
</style>""",
            unsafe_allow_html=True,
        )

        current = st.session_state.current_ticker

        # Pre-fetch all watchlist prices in one pass. fetch_history is
        # cached at the function level so this is cheap on repeat views.
        wl_data = {}
        for tkr in watchlist:
            cached_hist, _, _ = fetch_history(tkr)
            if cached_hist is not None and len(cached_hist) >= 2:
                last = float(cached_hist["Close"].iloc[-1])
                prev = float(cached_hist["Close"].iloc[-2])
                chg_pct = (last / prev - 1) * 100 if prev else 0
                wl_data[tkr] = (last, chg_pct)
            else:
                wl_data[tkr] = (None, None)

        # Each watchlist row rendered as ONE HTML markdown block.
        # No st.columns — flex layout in pure HTML, fully aligned, no
        # Streamlit padding interference. Ticker click → ?open=TICKER,
        # ✕ click → ?wldel=TICKER, both handled by the global handler.
        rows_html = []
        for tkr in watchlist:
            last, chg_pct = wl_data[tkr]
            is_active = (tkr == current)
            chg_color = (
                "var(--color-positive)" if (chg_pct or 0) >= 0
                else "var(--color-negative)"
            )
            chg_str = f"{chg_pct:+.2f}%" if chg_pct is not None else "—"
            px_str = f"{last:,.2f}" if last is not None else "—"

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
                f'{tkr}</a>'
                # Price + change — right aligned in middle area
                f'<div style="flex: 1 1 auto; min-width: 0;'
                f'display: flex; flex-direction: column; align-items: flex-end;'
                f'font-family: var(--font-mono); font-variant-numeric: tabular-nums;'
                f'line-height: 1.15; padding: 0 4px;">'
                f'<span style="font-size: var(--fs-base); color: var(--color-text); font-weight: 500;">${px_str}</span>'
                f'<span style="font-size: var(--fs-sm); color: {chg_color};">{chg_str}</span>'
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

    if st.session_state.current_ticker and st.session_state.current_ticker not in watchlist:
        st.markdown('<div style="margin-top:8px;">', unsafe_allow_html=True)
        if st.button(f"+ Add {st.session_state.current_ticker} to watchlist",
                     use_container_width=True, key="add_to_watchlist_btn"):
            st.session_state.store["watchlist"].append(st.session_state.current_ticker)
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
        masked = effective_key[:7] + "…" + effective_key[-4:] if len(effective_key) > 12 else "saved"
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
            f'✓ key {source_note} <span style="color:var(--color-faint);">({masked})</span>'
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
            'border:1px solid #F5D88A;border-radius:3px;'
            'font-family:Geist Mono,monospace;font-size:var(--fs-xs);color:var(--color-warning-text);">'
            '⚠ Session-only · data resets on refresh'
            '</div>',
            unsafe_allow_html=True,
        )


# ─────────────────────────────────────────────────────────────────────
# Navbar
# ─────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="desk-bar">
  <span class="wordmark"><span class="arrow">▸</span> Trading Desk</span>
  <span class="meta">{st.session_state.current_ticker}</span>
</div>
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
        meta = fetch_quote_meta(ticker)

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

    t = tactical.compute(hist, bench)
    if t is None:
        st.error(f"Insufficient history for {ticker}.")
        st.stop()

    earnings_days = meta.get("earnings_days") if meta else None
    if (
        earnings_days is not None and
        0 <= earnings_days <= 2 and
        t.get("action") in ("enter_now", "watch")
    ):
        t = {
            **t,
            "action": "hold_off",
            "trigger": None,
            "event_risk_hold": True,
        }
    elif (
        earnings_days is not None and
        3 <= earnings_days <= 7 and
        t.get("action") == "enter_now"
    ):
        t = {
            **t,
            "action": "watch",
            "event_risk_watch": True,
        }

    # Compute decision modifiers — earnings proximity, market regime, RS
    modifiers = tactical.decision_modifiers(t, meta, t.get("market_regime", "unknown"))

    # ── Single full-width header row ──────────────────────────────────
    # Render this before Claude/PM work so the page anchors immediately.
    chg_color  = "#2E7D4F" if t["change"] >= 0 else "#D14545"
    fallback_profile = FALLBACK_PROFILE_META.get(ticker.upper(), {})
    mcap       = format_market_cap(meta.get("market_cap"))
    spf        = meta.get("short_pct_float")
    earn_banner, earn_footer = format_earnings(meta)
    meta_bits  = []
    industry_line = meta.get("sector") or meta.get("industry") or fallback_profile.get("sector")
    if industry_line:           meta_bits.append(industry_line)
    if mcap:                    meta_bits.append(mcap)
    if spf is not None:         meta_bits.append(f"{spf:.1f}% short")
    dy = meta.get("dividend_yield")
    if dy is not None and dy > 0.05: meta_bits.append(f"{dy:.2f}% yield")
    if earn_footer and not earn_banner: meta_bits.append(f"Earnings {earn_footer}")
    meta_line  = " · ".join(meta_bits)
    chg_sign   = "+" if t["change"] >= 0 else ""
    company_label = (
        name
        if name and name.strip().upper() != ticker.upper()
        else (meta.get("long_name") or meta.get("short_name") or fallback_profile.get("name") or "")
    )
    company_html = (
        f'<span class="name">{html.escape(str(company_label))}</span>'
        if company_label else ""
    )
    meta_html = html.escape(meta_line)

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

    # Live PM bullets: when the dossier call returned bullets, prefer those
    # over the static template. This is what makes non-hardcoded tickers
    # (DASH, PLTR, COIN, etc.) show real thesis/drivers/risks/valuation
    # instead of "Not yet analyzed". Bullets come from the SAME Claude call
    # as the dossier, so there's no extra cost.
    live_bullets = (dossier_result or {}).get("bullets") or {}
    if live_bullets.get("thesis"):
        pm = {
            **pm,
            "thesis": live_bullets.get("thesis", pm.get("thesis", "")),
            "drivers": live_bullets.get("drivers") or pm.get("drivers", []),
            "risks": live_bullets.get("risks") or pm.get("risks", []),
            "valuation": live_bullets.get("valuation", pm.get("valuation", "")),
        }

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
    auto_levels = t.get("key_levels") or []
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
                # Promote to watch and inject the trigger
                t = {
                    **t,
                    "action": "watch",
                    "trigger": support_trigger_override,
                    # Recompute entry levels from the trigger
                    "entry": support_trigger_override["levels"]["buy_above"],
                }
            else:
                support_trigger_override = None  # don't render banner

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

        # 1a. TRIGGER — sits directly under the decision word so it's the
        # first actionable thing the eye lands on. Also Invalidation when
        # there's a watch/enter trigger; Accumulate's "stabilization
        # rationale + position discipline" panels; Hold off / Avoid get
        # "What's missing" + "Reconsider when" instead. All rendered HERE
        # so they're above the modifier badges, dossier, and tape read.
        if t["action"] in ("enter_now", "watch"):
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
        elif t["action"] == "accumulate":
            # Accumulation Watch: high-quality name stabilizing after deep
            # drawdown. Surface the key data points that earned this and
            # the explicit position-sizing guidance.
            drawdown_pct = (t["price"] / t["high_52w"] - 1) * 100
            above_low_pct = (t["price"] / t["low_52w"] - 1) * 100
            stabilizing_reasons = []
            if t.get("rs_delta", 0) >= 0.02:
                stabilizing_reasons.append(
                    f"relative strength improving ({t['rs_delta']:+.3f} over 10 days)"
                )
            stabilizing_reasons.append(
                f"price ${t['price']:,.2f} sits {drawdown_pct:.0f}% below the 52-week high "
                f"${t['high_52w']:,.2f} and only {above_low_pct:.0f}% above the 52-week low "
                f"${t['low_52w']:,.2f}"
            )
            if t["price"] > t.get("ma20", t["price"]):
                stabilizing_reasons.append(
                    f"holding above the 20-day MA at ${t.get('ma20', 0):,.2f}"
                )

            reasons_html = "".join(f'<li>{bold_numbers(r)}</li>' for r in stabilizing_reasons)
            st.markdown(f"""
<div class="desk-avoid-reasons" style="border-left-color:var(--color-purple); background:#F4F0FB;">
  <div class="label" style="color:var(--color-purple);">
    <span class="em">🌱</span>Why accumulate
  </div>
  <ul>{reasons_html}</ul>
</div>
""", unsafe_allow_html=True)

            # Sizing guidance — explicit, not a full allocation
            st.markdown("""
<div class="desk-reconsider" style="border-left-color:var(--color-purple); background:#F4F0FB;">
  <div class="label" style="color:var(--color-purple);">
    <span class="em">📊</span>Position discipline
  </div>
  <ul>
    <li><strong>Small starter only</strong> — this is early-entry logic, not a full allocation</li>
    <li><strong>Stagger entries</strong> — add on confirmation (50d reclaim, RS catchup) rather than averaging into weakness</li>
    <li><strong>Cut if it breaks</strong> — close below the 52-week low invalidates the stabilization read</li>
  </ul>
</div>
""", unsafe_allow_html=True)
        else:
            # Hold off / Avoid — "What's missing" + "Reconsider when"
            reasons = why_avoid_reasons(t)
            reversals = reconsider_when(t)

            if t["action"] == "hold_off":
                why_emoji = "🤔"
                why_label = "What's missing"
            else:
                why_emoji = "⛔"
                why_label = "Why avoid"

            reasons_html = "".join(
                f'<li>{bold_numbers(r)}</li>' for r in reasons
            )
            st.markdown(f"""
<div class="desk-avoid-reasons">
  <div class="label"><span class="em">{why_emoji}</span>{why_label}</div>
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
        # Map Claude's vocabulary to engine's keys for comparison
        _claude_to_engine = {
            "ENTER": "enter_now", "WATCH": "watch", "HOLD_OFF": "hold_off",
            "AVOID": "avoid", "ACCUMULATE": "accumulate",
        }
        claude_action_key = _claude_to_engine.get(claude_action_raw, "")
        rule_action = t["action"]
        # Three states: agree, disagree, unknown.
        if not claude_action_key:
            agreement_state = "unknown"
        elif claude_action_key == rule_action:
            agreement_state = "agree"
        else:
            agreement_state = "disagree"

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
            f'<span class="desk-cmp-trigger-label">Trigger:</span> {claude_trigger}</div>'
            if claude_trigger else ''
        )

        # Render comparison + logging UI inside a single bordered Streamlit
        # container. Single-line HTML strings (no leading whitespace inside
        # any string content — Streamlit's markdown processor treats
        # indented lines as code blocks). Use Streamlit's native columns
        # for the side-by-side rather than CSS grid, since the HTML
        # markdown block doesn't share a DOM with the widget block.
        with st.container(border=True):
            # Header row: "Decision comparison" + agree/disagree badge
            st.markdown(
                f'<div class="desk-cmp-header"><span>Decision comparison</span>{disagree_marker}</div>',
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
                st.markdown(
                    f'<div style="background:var(--color-surface);border:1px solid var(--color-border);'
                    f'border-left:3px solid var(--color-positive);border-radius:4px;'
                    f'padding:8px 12px;margin-bottom:8px;font-size:var(--fs-base);">'
                    f'<span style="font-weight:600;color:var(--color-positive);">✓ Logged</span>'
                    f' — {logged_action} at <span style="font-family:var(--font-mono);">'
                    f'${logged_price:,.2f}</span> on {logged_ts}'
                    + (f' · <span style="color:var(--color-muted);font-style:italic;">{logged_note}</span>' if logged_note else '')
                    + '</div>',
                    unsafe_allow_html=True,
                )

            # "Your call" header with info-icon hover
            _action_help = (
                "Enter — high-conviction setup, buy now (bullish + setup ≥ 9, no extension). "
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
                        a_preview = item["answer"].replace("\n", " ").strip()
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
                        st.markdown(
                            f'<div style="margin-bottom:16px;padding:12px 14px;'
                            f'background:var(--color-surface);border-radius:4px;'
                            f'border-left:2px solid var(--color-border);'
                            f'font-size:var(--fs-md);line-height:1.65;color:var(--color-body);">'
                            f'{_html.escape(msg["content"])}</div>',
                            unsafe_allow_html=True,
                        )

                # Centered input module. Keep the text area and action button
                # visually grouped so the control reads as one ask surface.
                _, chat_col, _ = st.columns([1, 10, 1])
                with chat_col:
                    user_q = st.text_area(
                        "Ask anything about this ticker",
                        key=f"chat_input_{ticker}",
                        label_visibility="collapsed",
                        placeholder=f"Ask anything about {ticker}…",
                        height=90,
                    )
                _, ask_col, _ = st.columns([5, 2, 5])
                with ask_col:
                    send = st.button("Ask", key=f"chat_send_{ticker}", use_container_width=True)

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
                            for _ in range(6):
                                _resp = _client.messages.create(
                                    model="claude-sonnet-4-6",
                                    max_tokens=600,
                                    system=sys_prompt,
                                    tools=_tools,
                                    messages=_msgs,
                                    betas=["web-search-2025-03-05"],
                                )
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
        color_map = {"pos": "#2E7D4F", "neg": "#D14545", "": "#3F3B34"}
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

        # Technical narrative — 2-4 paragraphs, behind an expander.
        # Defaults closed so the page stays scannable; one click reveals.
        # The expander now ALSO contains the technical-read commentary
        # lines (previously rendered separately on the right column) so
        # all detailed technical content lives in one place.
        tech_narrative = dossier_result.get("technical_narrative") if dossier_result else None
        commentary_lines = technical_commentary(t)
        has_detailed_tech = bool(tech_narrative) or bool(commentary_lines)
        if has_detailed_tech:
            with st.expander("Detailed technical view ↓", expanded=False):
                if commentary_lines:
                    commentary_html = "".join(
                        f'<p style="margin: 0 0 8px; font-size: var(--fs-md); line-height: 1.65; '
                        f'color: var(--color-body); font-family: Geist, sans-serif;">'
                        f'{bold_numbers(line)}</p>'
                        for line in commentary_lines
                    )
                    st.markdown(
                        f'<div style="font-family:Geist,sans-serif;font-size:var(--fs-xs);'
                        f'font-weight:600;letter-spacing: var(--ls-caps-lg);text-transform:uppercase;'
                        f'color:var(--color-muted);margin-bottom:8px;">Tape detail</div>'
                        f'<div style="padding: 0 2px 8px;">{commentary_html}</div>',
                        unsafe_allow_html=True,
                    )

                if tech_narrative:
                    if commentary_lines:
                        st.markdown(
                            '<div style="border-top:1px dashed var(--color-border);margin:12px 0 14px;"></div>'
                            '<div style="font-family:Geist,sans-serif;font-size:var(--fs-xs);'
                            'font-weight:600;letter-spacing: var(--ls-caps-lg);text-transform:uppercase;'
                            'color:var(--color-muted);margin-bottom:8px;">Narrative</div>',
                            unsafe_allow_html=True,
                        )
                    paragraphs = [p.strip() for p in tech_narrative.split("\n\n") if p.strip()]
                    paras_html = "".join(
                        f'<p style="margin: 0 0 12px; font-size: var(--fs-md); line-height: 1.65; '
                        f'color: var(--color-body); font-family: Geist, sans-serif;">{p}</p>'
                        for p in paragraphs
                    )
                    st.markdown(
                        f'<div style="padding: 0 2px;">{paras_html}</div>',
                        unsafe_allow_html=True,
                    )

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
    <div id="lwchart_{ticker}" style="width:100%;height:480px;background:#FBFAF7;"></div>
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
      background: {{ type: 'solid', color: '#FBFAF7' }},
      textColor: '#3F3B34',
      fontFamily: 'Geist Mono, monospace',
      fontSize: 11,
    }},
    grid: {{
      vertLines: {{ color: '#EFEDE7' }},
      horzLines: {{ color: '#EFEDE7' }},
    }},
    rightPriceScale: {{
      borderColor: '#E5E3DE',
      scaleMargins: {{ top: 0.08, bottom: 0.28 }},
    }},
    timeScale: {{
      borderColor: '#E5E3DE',
      timeVisible: false,
      secondsVisible: false,
    }},
    crosshair: {{
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: {{ color: '#8A857C', width: 1, style: 2 }},
      horzLine: {{ color: '#8A857C', width: 1, style: 2 }},
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
    priceLineColor: '#8A857C',
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
            auto_lvls = t.get("key_levels") or []
            user_lvls = st.session_state.store.setdefault("manual_levels", {}).setdefault(
                ticker.upper(), {"support": [], "resistance": []}
            )
            current_price = t["price"]

            # Helper: format one auto level as a readable row
            def _fmt_level_row(lv):
                level = lv["level"]
                pct = (level - current_price) / current_price * 100
                # Override kind based on position — support must be below, resistance above
                kind = "support" if level <= current_price else "resistance"
                color = "#00A870" if kind == "support" else "#D14545"
                # Direction language matters more than the +/- sign
                if abs(pct) < 0.5:
                    distance = "at current price"
                elif pct > 0:
                    distance = f"{pct:.1f}% above"
                else:
                    distance = f"{abs(pct):.1f}% below"
                # Touches and flip context, in plain English
                touches_text = f"{lv['touches']}× tested"
                flip_text = " · also tested as resistance" if (kind == "support" and lv["is_flip"]) else (
                    " · former support" if (kind == "resistance" and lv["is_flip"]) else ""
                )
                return (
                    f'<div style="display:flex;justify-content:space-between;align-items:baseline;'
                    f'font-family:Geist,sans-serif;font-size:var(--fs-md);color:var(--color-text);'
                    f'padding:8px 0;border-bottom:1px solid var(--color-border-soft);">'
                    f'<span><span style="font-family: var(--font-mono);font-weight:600;'
                    f'color:{color};">${level:,.2f}</span> '
                    f'<span style="color:var(--color-muted);font-size:var(--fs-base);margin-left:8px;">{kind}</span></span>'
                    f'<span style="color:var(--color-muted);font-size:var(--fs-base);">'
                    f'{distance} · {touches_text}{flip_text}</span>'
                    f'</div>'
                )

            # Split into proximate (within 15%) vs distant
            proximate = [
                lv for lv in auto_lvls
                if abs((lv["level"] - current_price) / current_price) <= 0.15
            ]
            distant = [lv for lv in auto_lvls if lv not in proximate]

            if proximate:
                st.markdown(
                    '<div style="font-family:Geist,sans-serif;font-size:var(--fs-xs);'
                    'font-weight:600;letter-spacing: var(--ls-caps-lg);text-transform:uppercase;'
                    'color:var(--color-muted);margin:4px 0 6px;">Nearby — within 15% of current price</div>'
                    + "".join(_fmt_level_row(lv) for lv in proximate[:6]),
                    unsafe_allow_html=True,
                )
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
                st.markdown(
                    "".join(_fmt_level_row(lv) for lv in distant[:8]),
                    unsafe_allow_html=True,
                )

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
        src_note = format_source_note(pm.get("_source", "the thesis"))

        # PM header + refresh button
        st.markdown(f"""
<div class="desk-pm-header">
  <div>
    <div><span class="em">🧠</span>Portfolio manager</div>
    <div class="src">{src_note}</div>
  </div>
  <a class="pm-refresh-link" href="?pm_refresh={html.escape(ticker.upper())}" title="Regenerate PM view">↻</a>
</div>
""", unsafe_allow_html=True)
        st.markdown(
            f'<a class="research-link" href="?report={html.escape(ticker.upper())}" '
            f'target="_blank" rel="noopener">✨ Full research report ↗</a>',
            unsafe_allow_html=True,
        )

        # Quality tier badge — informational, NOT a gate. Sourced from the
        # dossier Claude call (5th field). Shows long-term ownership read
        # alongside the tactical action: e.g. "Avoid · Quality A" means the
        # chart is broken right now but it's a name worth owning at a
        # better entry. Hidden when no quality data (no API key or pre-
        # cache miss).
        quality = (dossier_result or {}).get("quality") or {}
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
            rationale_html = f'<div style="font-size:var(--fs-sm); color:#4A453E; margin-top:4px; line-height:1.4;">{q_rationale}</div>' if q_rationale else ""
            st.markdown(f"""
<div style="background:{ts['bg']}; border-left:3px solid {ts['color']};
            padding:8px 12px; margin:6px 0 14px 0; border-radius:4px;">
  <div style="font-family: var(--font-mono); font-size:var(--fs-sm); font-weight:600;
              letter-spacing: var(--ls-caps-xs); text-transform:uppercase; color:{ts['color']};">
    {ts['label']}
  </div>
  <div style="font-size:var(--fs-sm); color:var(--color-muted); margin-top:2px;">{ts['sub']}</div>
  {rationale_html}
</div>
""", unsafe_allow_html=True)

        # Layer 1 — snapshot
        st.markdown(f"""
<div>
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
            peg_row = f'<div class="row"><span>PEG</span><span class="v"><span class="num">{peg:.2f}</span> · {peg_verdict} {pass_fail(peg_pass)}</span></div>'

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
            pe_row = f'<div class="row"><span>Forward P/E</span><span class="v"><span class="num">{fpe:.1f}</span> · {pe_context} {pass_fail(pe_pass)}</span></div>'

        de_row = ""
        if de is not None:
            de_pass = de < 30
            de_note = "healthy" if de_pass else "leveraged"
            de_row = f'<div class="row"><span>Debt / Equity</span><span class="v"><span class="num">{de:.0f}%</span> · {de_note} {pass_fail(de_pass)}</span></div>'

        growth_row = ""
        if eg is not None:
            growth_row = f'<div class="row"><span>Earnings growth</span><span class="v"><span class="num">{eg:+.1f}%</span> YoY</span></div>'

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

        # Investment thesis is visible by default. The extra structured
        # deep-dive stays behind one compact button so the PM panel remains
        # readable on first load.
        pm_narrative = dossier_result.get("pm_narrative") if dossier_result else None
        deep = pm.get("deep_dive") or {}
        has_deep = (deep.get("expanded_thesis") and pm.get("_source", "").startswith("claude")) or deep.get("catalysts")

        if pm_narrative:
            paragraphs = [p.strip() for p in pm_narrative.split("\n\n") if p.strip()]
            narrative_html = "".join(f"<p>{p}</p>" for p in paragraphs)
            st.markdown(
                f'<div class="desk-pm-thesis">'
                f'<div class="desk-pm-block"><div class="lb">Investment thesis</div></div>'
                f'{narrative_html}'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            fallback_thesis = pm.get("thesis") or "Investment thesis appears here after the PM dossier is generated."
            st.markdown(
                f'<div class="desk-pm-thesis">'
                f'<div class="desk-pm-block"><div class="lb">Investment thesis</div></div>'
                f'<p>{fallback_thesis}</p>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # Track expanded deep-dive state per ticker.
        ticker_key = ticker.upper()
        expanded = st.session_state.pm_expanded.get(ticker_key, False)

        show_deep_button = has_deep or not pm.get("_source", "").startswith("claude")
        if show_deep_button:
            btn_label = "Hide expanded thesis ↑" if expanded else "Expanded thesis ↓"
            if st.button(btn_label, key=f"pm_expand_{ticker_key}", use_container_width=False):
                st.session_state.pm_expanded[ticker_key] = not expanded
                st.rerun()

        if expanded and not has_deep:
            st.markdown(f"""
<div class="desk-pm-deep">
  <div class="sub-body" style="color:var(--color-faint);">
    Expanded thesis is generated when an Anthropic API key is configured in the sidebar.
    Paste a key, then click ↻ next to the Portfolio manager header to regenerate.
  </div>
</div>
""", unsafe_allow_html=True)

        if expanded and has_deep:
            # Structured deep-dive: variant perception, catalysts, risks,
            # what-must-be-true, what-would-change-my-mind.
            if deep.get("expanded_thesis"):
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
            elif deep.get("catalysts"):
                html_parts = ['<div class="desk-pm-deep">']
                html_parts.append(f'<div class="sub-lb">Catalysts · next 1–2 quarters</div>')
                html_parts.extend(f'<div class="desk-pm-item">{c}</div>' for c in deep["catalysts"])
                html_parts.append('</div>')
                st.markdown("".join(html_parts), unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────
# WATCHLIST — Pro view
# ─────────────────────────────────────────────────────────────────────
if view == "watchlist":
    if not st.session_state.store["watchlist"]:
        st.info("Your watchlist is empty. Type a ticker in the sidebar and add it.")
    else:
        bench = fetch_bench()
        dossier_cache = st.session_state.store.get("dossier_cache", {})

        # ── Compute everything we'll need for every ticker, in one pass ──
        # Each row: dict with action/state/RS/etc. + tactical engine output.
        rows = []
        with st.spinner("Analyzing watchlist…"):
            for tkr in st.session_state.store["watchlist"]:
                hist, name, _err = fetch_history(tkr)
                if hist is None or bench is None:
                    continue
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
                earnings_days = meta.get("earnings_days") if meta else None

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

                # Sector from yfinance .info — used for grouping and as a column
                sector = (meta.get("sector") if meta else None) or "—"

                rows.append({
                    "ticker": tkr,
                    "name": name or tkr,
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
                    "pct_52w_range": t.get("pct_of_52w_range", 50),
                    "vol_ratio": t.get("vol_ratio", 1.0),
                    "tech_delta": t.get("tech_delta", 0),
                    "_t": t,
                })

        # ── Sort selector ──
        st.markdown(
            '<div style="font-family: var(--font-sans);'
            'font-size: var(--fs-xs);font-weight:600;'
            'letter-spacing: var(--ls-caps-lg);text-transform:uppercase;'
            'color: var(--color-muted);margin: 4px 0 8px;">'
            'Watchlist · ' + str(len(rows)) + ' names</div>',
            unsafe_allow_html=True,
        )

        sort_c1, sort_c2, sort_c3 = st.columns([2, 2, 4])
        with sort_c1:
            sort_by = st.selectbox(
                "Sort by",
                options=[
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
                ],
                key="watchlist_sort",
                label_visibility="collapsed",
            )
        with sort_c2:
            group_by = st.selectbox(
                "Group by",
                options=["No grouping", "Action", "Sector", "Quality tier"],
                key="watchlist_group",
                label_visibility="collapsed",
            )
        with sort_c3:
            st.empty()

        # Action priority order
        action_priority = {
            "enter_now": 0, "accumulate": 1, "watch": 2,
            "hold_off": 3, "avoid": 4,
        }

        if sort_by == "Action priority":
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
        else:
            grouped = [(None, None, rows)]

        # ── Header row + all data rows render as ONE consistent HTML grid ──
        # Ticker cells are clickable anchor tags using ?open=TICKER query
        # params. The global handler at the top of the app picks up the
        # click and switches the active ticker — works universally without
        # needing Streamlit columns.

        # 12-column grid: ticker / last / chg / action / state / quality
        #                / RS / vsMA50 / 52w / vol / trig / earn
        grid_cols = (
            'grid-template-columns: 0.9fr 1fr 0.8fr 1.4fr 1.3fr 0.9fr 0.7fr 0.9fr 0.8fr 0.8fr 0.9fr 0.7fr;'
        )

        # Header
        st.markdown(
            f'<div style="display:grid; {grid_cols} '
            f'gap: 6px; padding: 8px; margin-top: 16px; '
            f'border-bottom: 1px solid var(--color-border); '
            f'font-family: var(--font-sans); '
            f'font-size: var(--fs-xs); font-weight: 600; '
            f'letter-spacing: var(--ls-caps-md); text-transform: uppercase; '
            f'color: var(--color-muted);">'
            f'<span>Ticker</span>'
            f'<span style="text-align:right;">Last</span>'
            f'<span style="text-align:right;">Chg (1D)</span>'
            f'<span>Action</span>'
            f'<span>State</span>'
            f'<span title="Long-term ownership tier from Claude (A / B / Speculative / Avoid)">Quality ⓘ</span>'
            f'<span style="text-align:right;" title="Relative strength vs SPY (>1.0 = leader)">RS ⓘ</span>'
            f'<span style="text-align:right;" title="% above/below 50-day MA">vs MA50 ⓘ</span>'
            f'<span style="text-align:right;" title="Position in 52-week range (0% = at low, 100% = at high)">52w pos ⓘ</span>'
            f'<span style="text-align:right;" title="Today\'s volume vs 20-day average">Vol × ⓘ</span>'
            f'<span style="text-align:right;" title="% to logged trigger price (— if no trigger)">Trig ⓘ</span>'
            f'<span style="text-align:right;" title="Days to next earnings">Earn ⓘ</span>'
            f'</div>',
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
                trig_str = (
                    f'{row["trig_dist"]:+.1f}%' if row["trig_dist"] is not None else "—"
                )
                earn_str = (
                    f'{row["earnings_days"]}d' if row["earnings_days"] is not None
                    else "—"
                )

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
                ticker_link = (
                    f'<a href="?open={row["ticker"]}" target="_self" '
                    f'style="font-weight:600;color:var(--color-text);'
                    f'text-decoration:none;cursor:pointer;">'
                    f'{row["ticker"]}</a>'
                )

                st.markdown(
                    f'<div style="display:grid; {grid_cols} '
                    f'gap: 6px; padding: 8px; '
                    f'border-bottom: 1px dashed var(--color-border-soft); '
                    f'font-family: var(--font-mono); font-variant-numeric: tabular-nums; '
                    f'font-size: var(--fs-base); align-items: baseline;">'
                    f'{ticker_link}'
                    f'<span style="text-align:right;color:var(--color-text);">${row["price"]:,.2f}</span>'
                    f'<span style="text-align:right;color:{chg_color};">{row["change"]:+.2f}%</span>'
                    f'<span style="font-family:var(--font-sans);font-size:var(--fs-sm);font-weight:600;color:{sty["color"]};">{sty["emoji"]} {sty["label"]}</span>'
                    f'<span style="font-family:var(--font-sans);font-size:var(--fs-xs);letter-spacing:var(--ls-caps);text-transform:uppercase;color:var(--color-faint);">{row["state"]}</span>'
                    f'{q_html}'
                    f'<span style="text-align:right;color:{rs_color};">{row["rs"]:.2f}</span>'
                    f'<span style="text-align:right;color:{ma_color};">{row["pct_ma50"]:+.1f}%</span>'
                    f'<span style="text-align:right;color:{pos_color};">{pct_52w:.0f}%</span>'
                    f'<span style="text-align:right;color:{vol_color};">{row["vol_ratio"]:.1f}×</span>'
                    f'<span style="text-align:right;color:var(--color-faint);">{trig_str}</span>'
                    f'<span style="text-align:right;color:var(--color-faint);">{earn_str}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        # Note: navigation to Analyze view happens by clicking a ticker name
        # in the table (uses ?open=TICKER query param) or via the sidebar
        # watchlist (always visible).

        # ── Legend / column key ──
        st.markdown(
            '<div style="margin-top:24px;padding-top:16px;'
            'border-top:1px solid var(--color-border);'
            'font-family:var(--font-sans);font-size:var(--fs-sm);'
            'color:var(--color-muted);line-height:1.6;">'
            '<strong>Column key:</strong> '
            '<span style="font-family:var(--font-mono);">Quality</span> = long-term ownership tier from Claude (— if not yet generated) · '
            '<span style="font-family:var(--font-mono);">vs MA50</span> = % above/below 50-day MA · '
            '<span style="font-family:var(--font-mono);">RS</span> = relative strength vs SPY (>1.0 = leader) · '
            '<span style="font-family:var(--font-mono);">52w pos</span> = position in 52-week range (0% = at low, 100% = at high) · '
            '<span style="font-family:var(--font-mono);">Vol ×</span> = today\'s volume vs 20-day average · '
            '<span style="font-family:var(--font-mono);">Trig</span> = % to logged trigger price · '
            '<span style="font-family:var(--font-mono);">Earn</span> = days to next earnings'
            '</div>',
            unsafe_allow_html=True,
        )


# ─────────────────────────────────────────────────────────────────────
# TRACKER
# ─────────────────────────────────────────────────────────────────────
if view == "tracker":
    # Decision comparison log
    # Trial-period banner — explicit timeline + progress.
    # Date math: trial starts when the FIRST entry was logged. Target
    # is N days from there. Goal is volume of comparisons to evaluate.
    decisions_log_for_banner = st.session_state.store.get("decisions_log", [])
    TRIAL_DAYS = 14
    TARGET_COMPARISONS = 15

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

    st.markdown(f"""
<div style="background:linear-gradient(90deg,#F4F0FB 0%, var(--color-bg) {progress_pct}%, var(--color-bg) 100%);
        border:1px solid #D9D5CC;border-radius:4px;padding:10px 14px;
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
        # Sub-tabs: Open (no outcome) vs Resolved (outcome scored)
        sub_open, sub_resolved = st.tabs([
            f"Open ({len(unscored)})", f"Resolved ({len(scored)})"
        ])

        def _render_decision_row(entry, idx, scored_view):
            import html as _html
            _act_map = {
                "ENTER": "enter_now", "WATCH": "watch", "HOLD_OFF": "hold_off",
                "AVOID": "avoid", "ACCUMULATE": "accumulate",
            }
            rule_sty   = STATE_STYLES.get(entry.get("rule_action"), {})
            claude_sty = STATE_STYLES.get(_act_map.get(entry.get("claude_action") or "", ""), {})
            user_sty   = STATE_STYLES.get(_act_map.get(entry.get("user_action") or "", ""), {})

            ts_short   = entry.get("ts", "")[:10]
            tkr        = _html.escape(str(entry.get("ticker", "")))
            price      = entry.get("price", 0)
            entry_id   = entry.get("id", "")
            user_note  = entry.get("user_note", "")
            reasoning  = entry.get("claude_reasoning", "")

            rule_color   = rule_sty.get("color", "var(--color-text)")
            rule_emoji   = rule_sty.get("emoji", "")
            rule_label   = rule_sty.get("label", (entry.get("rule_action") or "—").replace("_"," ").title())
            claude_color = claude_sty.get("color", "var(--color-text)")
            claude_emoji = claude_sty.get("emoji", "")
            claude_label = (entry.get("claude_action") or "—").replace("_"," ").title()
            claude_conf  = entry.get("claude_confidence", 0)
            user_color   = user_sty.get("color", "var(--color-text)")
            user_emoji   = user_sty.get("emoji", "")
            user_label   = user_sty.get("label", (entry.get("user_action") or "—").replace("_"," ").title())
            reasoning_preview = ""
            if reasoning:
                reasoning_preview = _html.escape(str(reasoning[:200]))
                if len(reasoning) > 200:
                    reasoning_preview += "…"

            # ── Compact row ──────────────────────────────────────────
            st.markdown(
                f'<div style="padding:10px 0 4px;border-top:1px solid var(--color-border);">'
                f'<div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:4px;">'
                f'<div style="display:flex;align-items:baseline;gap:10px;">'
                f'<span style="font-weight:700;font-size:var(--fs-md);">{tkr}</span>'
                f'<span style="font-family:var(--font-mono);font-size:var(--fs-sm);color:var(--color-muted);">${price:,.2f}</span>'
                + (f'<span style="font-size:var(--fs-sm);color:var(--color-faint);font-style:italic;">{_html.escape(user_note)}</span>' if user_note else '')
                + f'</div>'
                f'<span style="font-family:var(--font-mono);font-size:var(--fs-xs);color:var(--color-fainter);">{ts_short}</span>'
                f'</div>'
                f'<div style="display:flex;gap:28px;padding:2px 0 4px;">'
                f'<div><div style="font-size:var(--fs-xs);font-weight:600;text-transform:uppercase;letter-spacing:0.06em;color:var(--color-faint);margin-bottom:1px;">Rules</div>'
                f'<div style="font-size:var(--fs-base);font-weight:600;color:{rule_color};">{rule_emoji} {rule_label}</div></div>'
                f'<div><div style="font-size:var(--fs-xs);font-weight:600;text-transform:uppercase;letter-spacing:0.06em;color:var(--color-faint);margin-bottom:1px;">Claude <span style="font-weight:400;opacity:0.7;">{claude_conf}/10</span></div>'
                f'<div style="font-size:var(--fs-base);font-weight:600;color:{claude_color};">{claude_emoji} {claude_label}</div></div>'
                f'<div><div style="font-size:var(--fs-xs);font-weight:600;text-transform:uppercase;letter-spacing:0.06em;color:var(--color-faint);margin-bottom:1px;">You</div>'
                f'<div style="font-size:var(--fs-base);font-weight:600;color:{user_color};">{user_emoji} {user_label}</div></div>'
                f'</div>'
                + (f'<div style="font-size:var(--fs-sm);color:var(--color-faint);font-style:italic;padding:0 0 4px;line-height:1.4;">&ldquo;{reasoning_preview}&rdquo;</div>' if reasoning_preview else '')
                + '</div>',
                unsafe_allow_html=True,
            )

            # ── Inline actions ───────────────────────────────────────
            if not scored_view:
                act_cols = st.columns([3, 1])
                with act_cols[0]:
                    with st.expander("Score outcome", expanded=False):
                        outcome_choice = st.radio(
                            "Who was right?",
                            options=["Rules", "Claude", "You", "All three", "None / unclear"],
                            horizontal=True,
                            key=f"outcome_choice_{entry_id}",
                            label_visibility="collapsed",
                        )
                        c1, c2 = st.columns(2)
                        outcome_pct  = c1.text_input("Result %", placeholder="+5.2", key=f"outcome_pct_{entry_id}", label_visibility="collapsed")
                        outcome_note = c2.text_input("Note", placeholder="Optional", key=f"outcome_note_{entry_id}", label_visibility="collapsed")
                        if st.button("Save", key=f"save_outcome_{entry_id}", use_container_width=True):
                            right_sources = {"Rules":["rules"],"Claude":["claude"],"You":["user"],"All three":["rules","claude","user"]}.get(outcome_choice,[])
                            try: pct_val = float(outcome_pct) if outcome_pct else None
                            except ValueError: pct_val = None
                            entry["outcome"] = {
                                "ts": datetime.now().isoformat(timespec="seconds"),
                                "result": "right" if right_sources else "unclear",
                                "right_sources": right_sources,
                                "result_pct": pct_val,
                                "note": outcome_note.strip() if outcome_note else "",
                            }
                            save_store(st.session_state.store)
                            st.rerun()
                if act_cols[1].button("Delete", key=f"del_decision_{entry_id}"):
                    st.session_state.store["decisions_log"] = [
                        d for d in st.session_state.store["decisions_log"] if d.get("id") != entry_id
                    ]
                    save_store(st.session_state.store)
                    st.rerun()
            else:
                if st.button("Delete", key=f"del_decision_{entry_id}"):
                    st.session_state.store["decisions_log"] = [
                        d for d in st.session_state.store["decisions_log"] if d.get("id") != entry_id
                    ]
                    save_store(st.session_state.store)
                    st.rerun()

        with sub_open:
            if not unscored:
                st.markdown(
                    '<div style="color:var(--color-faintest);font-style:italic;font-size:var(--fs-base);'
                    'padding:14px 0;">No open decisions.</div>',
                    unsafe_allow_html=True,
                )
            else:
                for idx, entry in enumerate(unscored):
                    _render_decision_row(entry, idx, scored_view=False)

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
                for idx, entry in enumerate(scored):
                    _render_decision_row(entry, idx, scored_view=True)
