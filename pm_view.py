"""Portfolio Manager view — two-layer model.

Layer 1 — Snapshot: thesis, drivers, risks, valuation. Always visible.
Layer 2 — Deep dive: expanded thesis, variant perception, catalysts,
  risk scenarios, valuation context, what-must-be-true, what-would-change-my-mind.
  Revealed on demand.

Both layers generated in a single Claude call to keep cost at one request per ticker.
"""

import json
import re


# ─────────────────────────────────────────────────────────────────────
# Live-value substitution
# ─────────────────────────────────────────────────────────────────────
# Claude generates prose with placeholder tokens ({{price}}, {{rs}}, etc.)
# instead of hardcoded numbers. At render time, we substitute the live
# tactical engine output. This means narrative numbers stay current
# without regenerating the whole prose. Cost: zero per refresh.
#
# Tokens are defined in the dossier prompt (see the LIVE-VALUE TOKENS
# section). Add new tokens here and in the prompt simultaneously.
def substitute_live_values(text, tactical_output):
    """Replace {{token}} placeholders with current values from tactical engine.

    Args:
      text: prose string that may contain {{token}} placeholders
      tactical_output: dict from tactical.compute() with current price,
        RS, MA values, etc.

    Returns the prose with all known tokens substituted. Unknown tokens
    are left as-is so they're visible (and we can debug them).

    Free operation — no Claude call.
    """
    if not text or not tactical_output:
        return text or ""

    t = tactical_output
    price = t.get("price")
    ma50 = t.get("ma50")
    ma100 = t.get("ma100")
    ma200 = t.get("ma200")
    high_52w = t.get("high_52w")
    low_52w = t.get("low_52w")
    rs = t.get("rs")
    rsi = t.get("rsi14")

    def _pct_signed(curr, ref):
        if curr is None or ref is None or ref == 0:
            return None
        return (curr - ref) / ref * 100

    def _pct_unsigned(curr, ref):
        v = _pct_signed(curr, ref)
        return abs(v) if v is not None else None

    pct_ma50 = _pct_signed(price, ma50)
    pct_ma100 = _pct_signed(price, ma100)
    pct_ma200 = _pct_signed(price, ma200)
    pct_52w_high = _pct_unsigned(price, high_52w)
    pct_52w_low_v = _pct_signed(price, low_52w)  # signed for "x% above low"

    # Build substitution map. Every value formatted to its display form.
    substitutions = {
        "price":         f"${price:,.2f}" if price is not None else "—",
        "pct_ma50":      f"{pct_ma50:+.1f}%" if pct_ma50 is not None else "—",
        "pct_ma100":     f"{pct_ma100:+.1f}%" if pct_ma100 is not None else "—",
        "pct_ma200":     f"{pct_ma200:+.1f}%" if pct_ma200 is not None else "—",
        "pct_52w_high":  f"{pct_52w_high:.1f}%" if pct_52w_high is not None else "—",
        "pct_52w_low":   f"{pct_52w_low_v:+.1f}%" if pct_52w_low_v is not None else "—",
        "rs":            f"{rs:.2f}" if rs is not None else "—",
        "rsi":           f"{int(rsi)}" if rsi is not None else "—",
    }

    # Substitute via regex — pattern matches {{token}} with optional
    # whitespace inside the braces (Claude is sometimes inconsistent).
    def _replacer(match):
        token = match.group(1).strip().lower()
        return substitutions.get(token, match.group(0))

    # Match both {{token}} and {token} — Claude sometimes generates single braces
    text = re.sub(r"\{\{\s*(\w+)\s*\}\}", _replacer, text)
    text = re.sub(r"\{(\w+)\}", _replacer, text)
    return text


# Static snapshot views for common tickers — used when no API key is set.
STATIC_SNAPSHOTS = {
    "NVDA": {
        "thesis": "Dominant compute platform for AI infrastructure. Multi-year revenue tailwind intact while data center capex expands.",
        "drivers": ["Hyperscaler and sovereign buildouts", "CUDA moat — real switching costs", "Blackwell driving the next cycle"],
        "risks": ["Further China export restrictions", "Customer concentration at the hyperscalers", "Custom silicon from Google, Amazon, Meta"],
        "valuation": "32× forward. Rich, but defensible if the growth rate holds.",
    },
    "META": {
        "thesis": "Margin expansion post the Year of Efficiency. Reels monetization closing the gap to Feed.",
        "drivers": ["Advertising revenue reaccelerating", "Cost discipline held through AI capex", "Better targeting from generative AI"],
        "risks": ["Reality Labs still burning cash", "Regulatory pressure in EU and US", "TikTok clawing back younger users"],
        "valuation": "22× forward. Fair for a mid-teens grower with best-in-class margins.",
    },
    "AAPL": {
        "thesis": "Installed base monetization via Services offsetting hardware maturity.",
        "drivers": ["Services margin expansion", "Installed base growing in emerging markets", "Apple Intelligence driving replacement"],
        "risks": ["China revenue exposure", "iPhone cycle elongating", "Antitrust on the App Store"],
        "valuation": "28× forward. Premium for a slow grower — sensitive to multiple compression.",
    },
    "MSFT": {
        "thesis": "Enterprise AI winner via Azure and Copilot. Multi-year seat monetization ahead across productivity.",
        "drivers": ["Azure AI revenue ramping", "Copilot attach across Office 365", "Pricing power on productivity suite"],
        "risks": ["OpenAI relationship economics", "AI capex digestion", "Commercial O365 saturation"],
        "valuation": "30× forward. Premium, but defensible given the moat.",
    },
    "TSLA": {
        "thesis": "Transitioning from EV manufacturer to AI/robotics platform. Bull case needs you to believe the latter.",
        "drivers": ["Energy storage growing rapidly", "FSD progress toward robotaxi", "Cost reduction on next-gen vehicles"],
        "risks": ["Auto demand soft, margins compressing", "Robotaxi timeline keeps slipping", "Distracted leadership"],
        "valuation": "Rich on auto metrics. Only justified if AI optionality delivers.",
    },
}

# Keep the older name so existing imports don't break.
STATIC_VIEWS = STATIC_SNAPSHOTS


def _empty_deep_dive(ticker):
    return {
        "expanded_thesis": f"Deep dive on {ticker} not available without an Anthropic API key. Paste one in the sidebar to unlock this layer.",
        "business": None,
        "variant_bull": None,
        "variant_bear": None,
        "variant_needs": None,
        "catalysts": [],
        "risk_scenarios": [],
        "valuation_context": None,
        "must_be_true": [],
        "would_change_mind": [],
    }


def _generic_snapshot(ticker):
    return {
        "thesis": f"No thesis on file for {ticker}. Paste an Anthropic API key in the sidebar to generate one, or edit pm_view.py to add a static thesis.",
        "drivers": ["Not yet analyzed"],
        "risks": ["Not yet analyzed"],
        "valuation": "Not yet analyzed",
    }


def _fetch_recent_news(client, ticker, company_name):
    """Web-search call to get recent earnings + news.
    Handles the multi-turn tool-use loop that web_search requires.
    Returns a formatted block ready to inject into any prompt, or empty string."""
    name = company_name or ticker
    tools = [{"type": "web_search_20250305", "name": "web_search"}]
    # Run two targeted searches: recent earnings + analyst/news
    search_queries = [
        (f"US stock {ticker} most recent quarterly earnings EPS revenue guidance beat miss 2025 2026 "
         f"site:bloomberg.com OR site:reuters.com OR site:seekingalpha.com OR site:cnbc.com"),
        (f"{ticker} stock analyst upgrade downgrade price target news 2025 2026"),
    ]
    all_results = []

    try:
        def _run_search(query_text):
            """Run one search query through the agentic loop, return text."""
            msgs = [{"role": "user", "content": query_text}]
            for _ in range(5):
                resp = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=800,
                    tools=tools,
                    messages=msgs,
                    betas=["web-search-2025-03-05"],
                )
                text_parts = [b.text for b in resp.content if hasattr(b, "text") and b.text]
                if resp.stop_reason == "end_turn":
                    return " ".join(text_parts).strip()
                if resp.stop_reason == "tool_use":
                    msgs.append({"role": "assistant", "content": resp.content})
                    tool_results = []
                    for block in resp.content:
                        if block.type == "tool_use":
                            rc = getattr(block, "content", "") or ""
                            if isinstance(rc, list):
                                rc = " ".join(c.get("text","") if isinstance(c,dict) else str(c) for c in rc)
                            tool_results.append({"type":"tool_result","tool_use_id":block.id,"content":str(rc)})
                    msgs.append({"role": "user", "content": tool_results})
                else:
                    return " ".join(text_parts).strip()
            return ""

        for q in search_queries:
            result = _run_search(q)
            if result:
                all_results.append(result)

        if all_results:
            combined = "\n\n".join(all_results)
            return (
                f"\n\nRECENT NEWS & EARNINGS (live web search — more current than training data; "
                f"you MUST incorporate these specific facts into your analysis):\n{combined}\n"
            )
    except Exception:
        pass
    return ""

def get_pm_view(ticker, tactical_output, api_key=None, company_name=None):
    """Return a dict with BOTH snapshot fields and deep_dive nested.

    Shape:
      {
        thesis, drivers[], risks[], valuation,      # snapshot (layer 1)
        deep_dive: {                                 # layer 2
          expanded_thesis, business,
          variant_bull, variant_bear, variant_needs,
          catalysts[], risk_scenarios[],
          valuation_context,
          must_be_true[], would_change_mind[],
        },
        _source: "claude" | "static" | ...
      }
    """
    ticker = ticker.upper()

    if not api_key:
        snap = STATIC_SNAPSHOTS.get(ticker, _generic_snapshot(ticker))
        return {**snap, "deep_dive": _empty_deep_dive(ticker), "_source": "static"}

    try:
        import time
        from anthropic import Anthropic

        client = Anthropic(api_key=api_key)
        t = tactical_output or {}
        recent_news = _fetch_recent_news(client, ticker, company_name)
        time.sleep(1)  # brief pause to avoid rate-limiting the main call
        prompt = f"""You are a senior portfolio manager at a long-biased hedge fund. Write a full investment note on {ticker}{' (' + company_name + ')' if company_name else ''}.

CRITICAL: The ticker {ticker} refers to the US-listed security "{company_name if company_name else ticker}" trading on US stock exchanges (NYSE/NASDAQ). Do NOT confuse it with any foreign company that may share the same ticker symbol on another exchange (e.g. a Singapore, London, or Hong Kong-listed company). If the yfinance name seems wrong, trust the US stock market context — {ticker} is a US-listed security. All analysis must be about the US-listed {ticker} only.{recent_news}
Current tactical state from the system (for context, not the focus):
- Directional bias: {t.get('bias') or 'unclear'}
- Action: {t.get('action', 'unknown')}
- Technical score: {t.get('setup_score', 0):.1f} / 10
- Projected reward/risk: {f"{t.get('reward_risk'):.2f}:1" if t.get('reward_risk') is not None else 'n/a'}

Return ONLY JSON in exactly this shape. No preamble, no code fences.

{{
  "thesis": "1-2 sentences, the core investment rationale",
  "drivers": ["3 short items, no period at end"],
  "risks": ["3 short items, no period at end"],
  "valuation": "1 sentence on valuation context",
  "deep_dive": {{
    "expanded_thesis": "4-6 sentences. Frame consensus, variant view, what is priced in, and what would prove the view wrong.",
    "business": "2-3 sentences on key segments, where growth comes from, durability of the franchise",
    "variant_bull": "1-2 sentences on the specific bull case that is NOT consensus",
    "variant_bear": "1-2 sentences on the specific bear case that is NOT consensus",
    "variant_needs": "1 sentence on what specifically has to happen for the variant to play out",
    "catalysts": ["3 time-bound catalysts over the next 1-2 quarters, each one line, concrete and dated when possible"],
    "risk_scenarios": ["3 specific failure modes, not generic risks. Each one line."],
    "valuation_context": "2-3 sentences comparing to history, peers, growth durability, and what is priced in today",
    "must_be_true": ["3 things that must hold for the thesis to work. Each phrased as a specific measurable condition."],
    "would_change_mind": ["3 things that would invalidate the thesis. Each phrased as a specific observable trigger."]
  }}
}}

Voice: senior PM, confident, specific, opinionated. No hedging, no consultantese, no corporate-speak. Include both the upside case and the kill criteria; do not write a one-sided bull pitch. Write in complete sentences.
Return ONLY the JSON, nothing else."""

        for _attempt in range(2):
            try:
                message = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=2000,
                    messages=[{"role": "user", "content": prompt}],
                )
                break
            except Exception as _e:
                if '429' in str(_e) and _attempt == 0:
                    time.sleep(8)
                    continue
                raise
        text = message.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        parsed = json.loads(text)
        parsed["_source"] = "claude"
        return parsed
    except Exception as e:
        # Graceful fallback — static snapshot + empty deep dive
        snap = STATIC_SNAPSHOTS.get(ticker, _generic_snapshot(ticker))
        return {
            **snap,
            "deep_dive": _empty_deep_dive(ticker),
            "_source": f"static (claude call failed: {str(e)[:80]})",
        }


def get_decision_dossier(ticker, t_state, modifiers, meta, pm_data,
                         api_key=None, company_name=None):
    """Generate three pieces in one Claude call:
      - dossier: 4-6 sentence top-of-page brief
      - technical_narrative: 2-4 paragraph technical read
      - pm_narrative: 2-4 paragraph investment thesis prose

    Returns dict with all three fields plus _source. Returns None values
    when no API key; the UI hides the blocks in that case.
    """
    empty = {
        "dossier": None,
        "technical_narrative": None,
        "pm_narrative": None,
        "bullets": {},
        "quality": {},
        "tactical_call": {},
    }
    if not api_key:
        return {**empty, "_source": "unavailable"}

    try:
        import time
        from anthropic import Anthropic
        import json as _json
        client = Anthropic(api_key=api_key)

        recent_news_block = _fetch_recent_news(client, ticker, company_name)
        time.sleep(1)  # brief pause to avoid rate-limiting the main call

        bias = t_state.get("bias") or t_state.get("raw_bias") or "unclear"
        action = t_state.get("action", "unknown")
        trigger = t_state.get("trigger") or {}
        trig_summary = trigger.get("summary", "n/a") if trigger else "n/a"
        buy_above = trigger.get("levels", {}).get("buy_above") if trigger else None
        abort_below = trigger.get("levels", {}).get("abort_below") if trigger else None
        ma50_hist = t_state.get("ma50_history")
        regime = t_state.get("market_regime", "unknown")

        history_line = ""
        if ma50_hist:
            history_line = (
                f"Tested the 50-day {ma50_hist['tests']} times over "
                f"~{ma50_hist['lookback_months']} months; held "
                f"{ma50_hist['held']}/{ma50_hist['tests']}, average "
                f"{ma50_hist['avg_bounce_pct']:+.1f}% over the next 20 days."
            )

        modifiers_block = "\n".join(f"- {m['text']}" for m in (modifiers or []))
        if not modifiers_block:
            modifiers_block = "- (none active)"

        thesis = (pm_data or {}).get("thesis", "")
        drivers = (pm_data or {}).get("drivers", [])
        risks = (pm_data or {}).get("risks", [])
        valuation = (pm_data or {}).get("valuation", "")

        prompt = f"""You are a senior portfolio manager and trader. Generate THREE pieces of analysis on {ticker}{f' ({company_name})' if company_name else ''}.

CRITICAL: The ticker {ticker} refers to the US-listed security "{company_name if company_name else ticker}" trading on US stock exchanges (NYSE/NASDAQ). Do NOT confuse it with any foreign company sharing the same ticker on another exchange. If the company name seems unfamiliar or foreign, use your knowledge of US-listed stocks to identify the correct company for ticker {ticker}. All analysis must be about the US-listed {ticker} only.{recent_news_block}
DATA YOU HAVE:

Tactical state:
- Decision (rule engine): {action.replace('_', ' ')}
- Structural state: {t_state.get('state', 'TRENDING')}
- Bias: {bias}
- Setup score: {t_state.get('setup_score', 0):.1f}/10 (bias_score {t_state.get('bias_score', 0):+d}/±10)
- Price: ${t_state.get('price', 0):.2f}; 20-day MA ${t_state.get('ma20', 0):.2f}; 50-day MA ${t_state.get('ma50', 0):.2f}; 100-day MA ${t_state.get('ma100', 0):.2f}; 200-day MA ${t_state.get('ma200', 0):.2f}
- RSI: {t_state.get('rsi14', 50):.0f}; 52-week range position {t_state.get('pct_of_52w_range', 50):.0f}%
- Relative strength vs SPX: {t_state.get('rs', 1):.2f} (10d delta {t_state.get('rs_delta', 0):+.3f})
- ATR: {t_state.get('atr_pct', 0)*100:.2f}%; Volume vs 20d avg: {t_state.get('vol_ratio', 1):.2f}×
- Projected reward/risk to Target 1: {f"{t_state.get('reward_risk'):.2f}:1" if t_state.get('reward_risk') is not None else 'n/a'}
- Tech score 10d delta: {t_state.get('tech_delta', 0):+.1f}
- Structure quality: {t_state.get('structure_quality', 5):.1f}/10
- Accumulation eligible: {t_state.get('is_accumulation_eligible', False)}
- Trigger: {trig_summary}
- Buy above: {f'${buy_above:.2f}' if buy_above else 'n/a'}
- Invalidation below: {f'${abort_below:.2f}' if abort_below else 'n/a'}

Context:
- Market regime (SPY): {regime}
- Sector: {(meta or {}).get('sector') or 'unknown'}
- Industry: {(meta or {}).get('industry') or 'unknown'}
- Market cap: {(meta or {}).get('market_cap') or 'unknown'}
- Earnings: {f'in {meta.get("earnings_days")} days' if meta and meta.get('earnings_days') is not None else 'no near-term'}
- Forward P/E: {(meta or {}).get('forward_pe') or 'n/a'}, PEG: {(meta or {}).get('peg') or 'n/a'}, EV/EBITDA: {(meta or {}).get('ev_ebitda') or 'n/a'}
- Earnings growth YoY: {(meta or {}).get('earnings_growth') or 'n/a'}%, Revenue growth: {(meta or {}).get('revenue_growth') or 'n/a'}%
- Debt/equity: {(meta or {}).get('debt_to_equity') or 'n/a'}%
- Analyst consensus: {(meta or {}).get('analyst_rec') or 'n/a'}, target ${(meta or {}).get('analyst_target') or 'n/a'}

Historical:
{history_line if history_line else 'No useful 50-day test history.'}

Decision modifiers:
{modifiers_block}

Existing PM thesis snapshot:
{thesis or 'No thesis on file.'}
Drivers: {', '.join(drivers) if drivers else 'n/a'}
Risks: {', '.join(risks) if risks else 'n/a'}
Valuation: {valuation or 'n/a'}

DELIVERABLES — return ONLY a JSON object with these six keys:

{{
  "dossier": "...",
  "technical_narrative": "...",
  "pm_narrative": "...",
  "bullets": {{
    "thesis": "...",
    "drivers": ["...", "...", "..."],
    "risks": ["...", "...", "..."],
    "valuation": "..."
  }},
  "quality": {{
    "tier": "A" | "B" | "Speculative" | "Avoid",
    "rationale": "1-2 sentences"
  }},
  "tactical_call": {{
    "action": "ENTER" | "WATCH" | "HOLD_OFF" | "AVOID" | "ACCUMULATE",
    "confidence": 1-10,
    "reasoning": "2-3 sentences",
    "trigger": "specific price/condition or null",
    "invalidation": "what breaks the setup or null",
    "notes": "optional nuance"
  }}
}}

Each field's content rules:

LIVE-VALUE TOKENS (CRITICAL):
The dossier, technical_narrative, and pm_narrative are cached for up to 7 days, but PRICES MOVE DAILY. To keep narrative numbers current without regenerating the whole prose, you MUST use these literal token strings instead of hardcoding the values:

- {{price}}                — current price in dollars (e.g., "$192.34")
- {{pct_ma50}}             — % above/below 50-day MA, signed (e.g., "+4.2%" or "-3.1%")
- {{pct_ma200}}            — % above/below 200-day MA, signed
- {{pct_52w_high}}         — % below 52-week high, unsigned (e.g., "12.4%")
- {{pct_52w_low}}          — % above 52-week low, unsigned
- {{rs}}                   — relative strength vs SPY, two decimals (e.g., "0.81")
- {{rsi}}                  — RSI(14) value, integer (e.g., "47")

Examples of CORRECT usage:
- "DASH at {{price}} sits {{pct_ma200}} from its 200-day MA, with RS at {{rs}}..."
- "NVDA trading near {{price}} has pulled back {{pct_52w_high}} from highs..."
- "PLTR at {{price}} ({{pct_ma50}} from MA50) shows momentum..."

Examples of INCORRECT usage (DO NOT do this):
- "DASH at $171.97" — hardcoded; will be stale tomorrow
- "RS at 0.81" — hardcoded; should be "{{rs}}"
- "12% below MA200" — hardcoded; should be "{{pct_ma200}}"

Use tokens ONLY for the headline current values. Historical anchors ("rallied from $80 in March", "broke out above $150 in October") should remain as literal numbers — those refer to past events, not live state.

dossier: 4-6 sentences. Single paragraph. Top-of-page brief tying tactical + fundamental into one decision. Use {{price}} for the headline price. End with the action condition.

technical_narrative: 2-4 paragraphs (4 only if needed). Senior-trader voice. Use tokens for ALL current-state numbers. Walk through:
- Para 1: trend posture and chart setup. Where price ({{price}}) sits relative to 50d/200d ({{pct_ma50}}, {{pct_ma200}}), what the MA stack signals, recent price action character.
- Para 2: momentum + volume + relative strength as a connected read. RS at {{rs}}, RSI at {{rsi}}. Is the tape supporting or fading the move? What's the volume saying about conviction? Reference the 10-day deltas and vol ratio.
- Para 3 (optional): historical pattern context if useful — how this stock typically behaves at this kind of level. Use the ma50_history line if it adds signal.
- Para 4 (optional, if relevant): how the broader regime ({regime} SPY) affects this read.

pm_narrative: 3-4 paragraphs. Senior PM voice. Use {{price}} for current-price references. Walk through:
- Para 1: what the business actually does and how it makes money. Specific to this name, not boilerplate.
- Para 2: variant view — what consensus believes vs what the bull/bear case actually requires. Be specific about which view you find more convincing and why.
- Para 3: valuation context — what's priced in at current multiples, how the math compares to the growth rate, what would have to be true for this to work from current levels.
- Para 4: portfolio implementation — sizing posture, dominant catalyst, dominant risk, and the concrete evidence that would make you change your mind.

bullets: compact summary that powers the right-hand snapshot panel. Used when the static template doesn't have a thesis for this ticker (DASH, PLTR, COIN, etc.). Rules:
- thesis: 1-2 sentences. Core investment rationale. Specific to this name, no boilerplate.
- drivers: exactly 3 items, each 4-8 words, no trailing period. The structural reasons this works.
- risks: exactly 3 items, each 4-8 words, no trailing period. Specific failure modes, not generic market risk.
- valuation: 1 sentence. What's priced in vs what the math implies.
- Generate REAL content for any ticker — never placeholder text like "Not yet analyzed".

quality: Long-term ownership tier. INDEPENDENT of the tactical action and INDEPENDENT of current financial metrics. Quality is informational only; it does NOT change the tactical decision. Assess based on long-term industry leadership, moat durability, and structural opportunity — NOT on whether current revenue is positive, current P/E is reasonable, or current chart is clean.

Tiers:

- **A** — Durable category leader with structural moat: dominant market share + durable competitive advantage (network effects, scale, brand, regulatory moat, switching costs) + secular tailwind. Examples: NVDA, MSFT, META in the AI era; COST in retail; V/MA in payments. Worth owning and accumulating at proper setups.

- **B** — Real business with real moat but with timing, cyclicality, or execution risk that makes ownership conditional. Examples: NFLX through password-sharing transition; TSLA at extreme multiple but real EV/AI leadership; quality cyclicals at the wrong point in cycle. Tactical + selective ownership.

- **Speculative** — Real long-term upside with binary or pre-revenue risk. NOT a disqualifier — this includes leaders in emerging categories (space economy, gene therapy, frontier AI). Examples: ASTS (orbital cellular leader, technical risk retiring), RKLB (small-launch leader), pre-profit category creators with credible moats. CRITICAL: only act on Speculative names with strong technical setups and clear momentum. Do NOT treat Speculative names as long-term accumulation candidates unless they are explicitly stabilized (basing pattern, RS turning, no new lows). Position-size accordingly — this tier requires cleaner setups than Quality A/B.

- **Avoid** — Genuinely broken business: declining moat, secular headwinds, melting ice cube. NOT just a name with a broken chart. Reserve this for businesses that probably shouldn't exist in their current form (distressed retail, dying media, obsolete hardware).

Critical: Pre-revenue is NOT Avoid. Negative earnings in a cyclical leader is NOT Avoid. High forward P/E is NOT Avoid. Ask: "Will this company still exist and be relevant in 10 years, and will it likely be a leader in its category?" If yes → A or B or Speculative. Reserve Avoid for the genuine melting-ice-cube cases.

rationale: 1-2 sentences. State the moat and category leadership specifically. Not boilerplate.

tactical_call: YOUR independent action call on a 1-8 week trading horizon. This is timing, not long-term ownership — Quality is informational, not a gate (with one specific exception below). The rule engine's decision is shown above for reference, but you must make your OWN call based on the underlying data. Where the rules and your judgment disagree, output your judgment — that's the whole point of having both. The downstream system will surface disagreements explicitly. Apply this decision framework strictly:

CORE PRINCIPLE — separate timing (action) from long-term quality (already given). Answer ONLY: "Is this a good entry or setup right now, on a 1-8 week horizon?"

HARD CONSTRAINTS (must follow; if your output conflicts with these, the constraints win):

AVOID if ALL of:
- price < ma200 (below long-term structure)
- relative_strength < 0.9 (tape rejecting)
- RS_delta < 0.01 (no improvement)
- tech_delta ≤ 0 (no momentum)
- accumulation_flag is false
(This matches the rules engine's strict 5-condition Avoid. Mirror it.)

ENTER only if ALL of:
- bullish bias (price above ma50 AND ma200, momentum positive)
- tech_score ≥ 9
- valid trigger exists AND is actionable now
- NOT extended (price within ~12% above MA50 AND within ~8% above MA100)
- projected reward/risk is at least ~1.5:1
- earnings are not within the next 7 calendar days

WATCH if:
- bullish bias but tech_score < 9 (waiting on confirmation), OR
- valid trigger exists and is approaching but not fired, OR
- bullish bias but extended → wait for pullback target
- clean setup but earnings are 3-7 days away → wait for post-print reset

HOLD_OFF (universal default for ambiguity) if:
- earnings are within 0-2 days for a fresh entry
- projected reward/risk is below ~1.2:1
- pullback in uptrend (above ma200, below ma50)
- transitioning structure (recovering, no clean confirmation)
- below ma200 but tape still loyal (RS ≥ 0.95)
- mixed signals where edge isn't clear

ACCUMULATE if:
- accumulation_flag is true AND quality is A or B
This is the ONLY case where quality affects action. Otherwise quality is informational.

INTERPRETATION RULES:

Trend vs Transition vs Broken — use the trend_state input directly:
- TRENDING: above MA50 and MA200, structure intact
- TRANSITION: conflicting signals (e.g. above MA50, below MA200) with momentum recovering — default HOLD_OFF unless very strong confluence
- BROKEN: below key MAs, weak RS, no structure

RS interpretation: RS > 1.0 = leadership; 0.9-1.0 = neutral; <0.9 = lagging. Improving RS (positive RS_delta) matters more than absolute level — a name with RS 0.92 and rs_delta +0.04 is repairing.

Extension: price >12% above MA50 AND >8% above MA100 → extended → do NOT ENTER → downgrade to WATCH with pullback target.

Triggers: prefer proximate, actionable levels. A reclaim of MA200 that's 35% away is NOT a trigger — it's a long-horizon target. The trigger must be reachable on a 1-8 week timeframe (typically within 10% of current price).

Reward/risk discipline: Do not recommend a trade just because direction is plausible. If the invalidation is too far from entry relative to Target 1, call it HOLD_OFF and explain that the entry is bad even if the stock is interesting.

Event discipline: Do not recommend a fresh ENTER immediately ahead of earnings. Earnings can gap through both trigger and stop; prefer WATCH for 3-7 days away and HOLD_OFF for 0-2 days away.

QUALITY USAGE — strictly limited:
- DO use quality to inform reasoning tone and risk framing
- DO use quality to gate the ACCUMULATE upgrade (A/B only)
- DO NOT upgrade non-accumulation AVOIDs to ENTER because of quality
- DO NOT downgrade clean ENTERs because of low quality
- Speculative quality + WATCH → "setup valid, size smaller" framing
- Quality A + HOLD_OFF → "wait for better entry" framing

CONFIDENCE SCALE (anchor your number to these):
- 1-3: I don't trust this — signals are mixed, low conviction
- 4-6: reasonable read but not high conviction — could go either way
- 7-9: clean setup, signals aligning
- 10: if this fails, my framework is wrong

OUTPUT for tactical_call:
- action: one of ENTER/WATCH/HOLD_OFF/AVOID/ACCUMULATE
- confidence: integer 1-10 anchored to scale above
- reasoning: 2-3 sentences focused on structure + RS + trigger + reward/risk/event risk. Reference specific numbers from inputs. Identify the dominant principle that drove the call.
- trigger: concrete price/condition (e.g. "Hold of $145 with volume confirmation") or null if no clear trigger
- invalidation: specific price level that breaks the setup, or null if action is HOLD_OFF/AVOID
- notes: optional one-line nuance, including sizing if Speculative or event risk is near

FINAL RULE: if uncertain or the setup is not clearly actionable with favorable risk/reward, default to HOLD_OFF. Do NOT force trades. Do NOT default to optimism. Your job is to avoid bad trades and identify clean setups — not to justify interest.

Style across all three:
- Confident, opinionated, specific. No hedging, no consultantese.
- Reference exact prices, multiples, percentages. Numbers anchor every claim.
- Write like a memo, not a marketing brochure.
- For pm_narrative, do NOT just restate the thesis snippet above — extend it with judgment, framing, and specific reasoning.

Return ONLY the JSON object. No markdown fencing, no preamble, no commentary."""

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        # Strip stray code fences if Claude adds them
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        parsed = _json.loads(text)
        return {
            "dossier": parsed.get("dossier"),
            "technical_narrative": parsed.get("technical_narrative"),
            "pm_narrative": parsed.get("pm_narrative"),
            "bullets": parsed.get("bullets") or {},
            "quality": parsed.get("quality") or {},
            "tactical_call": parsed.get("tactical_call") or {},
            "_source": "claude",
        }

    except Exception as e:
        return {**empty, "_source": f"error: {str(e)[:120]}"}
