"""Portfolio Manager view — two-layer model.

Layer 1 — Snapshot: thesis, drivers, risks, valuation. Always visible.
Layer 2 — Deep dive: expanded thesis, variant perception, catalysts,
  risk scenarios, valuation context, what-must-be-true, what-would-change-my-mind.
  Revealed on demand.

Both layers generated in a single Claude call to keep cost at one request per ticker.
"""

import json

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
        from anthropic import Anthropic

        client = Anthropic(api_key=api_key)
        t = tactical_output or {}
        prompt = f"""You are a senior portfolio manager at a long-biased hedge fund. Write a full investment note on {ticker}{' (' + company_name + ')' if company_name else ''}.

Current tactical state from the system (for context, not the focus):
- Directional bias: {t.get('bias') or 'unclear'}
- Action: {t.get('action', 'unknown')}
- Technical score: {t.get('setup_score', 0):.1f} / 10

Return ONLY JSON in exactly this shape. No preamble, no code fences.

{{
  "thesis": "1-2 sentences, the core investment rationale",
  "drivers": ["3 short items, no period at end"],
  "risks": ["3 short items, no period at end"],
  "valuation": "1 sentence on valuation context",
  "deep_dive": {{
    "expanded_thesis": "4-6 sentences. Frame what the market currently believes vs what you believe. Include the variant view clearly.",
    "business": "2-3 sentences on key segments, where growth comes from, durability of the franchise",
    "variant_bull": "1-2 sentences on the specific bull case that is NOT consensus",
    "variant_bear": "1-2 sentences on the specific bear case that is NOT consensus",
    "variant_needs": "1 sentence on what specifically has to happen for the variant to play out",
    "catalysts": ["3 time-bound catalysts over the next 1-2 quarters, each one line, concrete and dated when possible"],
    "risk_scenarios": ["3 specific failure modes, not generic risks. Each one line."],
    "valuation_context": "2-3 sentences comparing to historical multiples, peers, and what is priced in today",
    "must_be_true": ["3 things that must hold for the thesis to work. Each phrased as a specific condition."],
    "would_change_mind": ["3 things that would invalidate the thesis. Each phrased as a specific trigger."]
  }}
}}

Voice: senior PM, confident, specific, opinionated. No hedging, no consultantese, no corporate-speak. Write in complete sentences.
Return ONLY the JSON, nothing else."""

        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
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
    }
    if not api_key:
        return {**empty, "_source": "unavailable"}

    try:
        from anthropic import Anthropic
        import json as _json
        client = Anthropic(api_key=api_key)

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

DATA YOU HAVE:

Tactical state:
- Decision: {action.replace('_', ' ')}
- Bias: {bias}
- Setup score: {t_state.get('setup_score', 0):.1f}/10 (bias_score {t_state.get('bias_score', 0):+d}/±10)
- Price: ${t_state.get('price', 0):.2f}; 50-day MA ${t_state.get('ma50', 0):.2f}; 200-day MA ${t_state.get('ma200', 0):.2f}
- RSI: {t_state.get('rsi14', 50):.0f}; 52-week range position {t_state.get('pct_of_52w_range', 50):.0f}%
- Relative strength vs SPX: {t_state.get('rs', 1):.2f} (10d delta {t_state.get('rs_delta', 0):+.3f})
- ATR: {t_state.get('atr_pct', 0)*100:.2f}%; Volume vs 20d avg: {t_state.get('vol_ratio', 1):.2f}×
- Tech score 10d delta: {t_state.get('tech_delta', 0):+.1f}
- Structure quality: {t_state.get('structure_quality', 5):.1f}/10
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

DELIVERABLES — return ONLY a JSON object with these five keys:

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
  }}
}}

Each field's content rules:

dossier: 4-6 sentences. Single paragraph. Top-of-page brief tying tactical + fundamental into one decision. Reference exact prices. End with the action condition.

technical_narrative: 2-4 paragraphs (4 only if needed). Senior-trader voice. Walk through:
- Para 1: trend posture and chart setup. Where price sits relative to 50d/200d in dollar terms, what the MA stack signals, recent price action character.
- Para 2: momentum + volume + relative strength as a connected read. Is the tape supporting or fading the move? What's the volume saying about conviction? Is RSI extended or coiled? Reference the 10-day deltas and vol ratio.
- Para 3 (optional): historical pattern context if useful — how this stock typically behaves at this kind of level. Use the ma50_history line if it adds signal.
- Para 4 (optional, if relevant): how the broader regime ({regime} SPY) affects this read.

pm_narrative: 2-4 paragraphs. Senior PM voice. Walk through:
- Para 1: what the business actually does and how it makes money. Specific to this name, not boilerplate.
- Para 2: variant view — what consensus believes vs what the bull/bear case actually requires. Be specific about which view you find more convincing and why.
- Para 3: valuation context — what's priced in at current multiples, how the math compares to the growth rate, what would have to be true for this to work from current levels.
- Para 4 (optional): the dominant near-term catalyst or risk and what to watch for.

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

Style across all three:
- Confident, opinionated, specific. No hedging, no consultantese.
- Reference exact prices, multiples, percentages. Numbers anchor every claim.
- Write like a memo, not a marketing brochure.
- For pm_narrative, do NOT just restate the thesis snippet above — extend it with judgment, framing, and specific reasoning.

Return ONLY the JSON object. No markdown fencing, no preamble, no commentary."""

        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=2500,
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
            "_source": "claude",
        }

    except Exception as e:
        return {**empty, "_source": f"error: {str(e)[:120]}"}
