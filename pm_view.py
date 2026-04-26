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
    """Generate a single-paragraph dossier synthesizing tactical state +
    modifiers + history + fundamentals into PM-style prose.

    Returns dict: { "dossier": str, "_source": "claude"|"unavailable" }
    Returns None for the dossier text when no API key is set; the UI hides
    the block in that case.
    """
    if not api_key:
        return {"dossier": None, "_source": "unavailable"}

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)

        # Build a compact, dense brief for Claude. We deliberately pack a
        # lot of facts in so the output can be specific without us having
        # to babysit each prompt parameter.
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

        prompt = f"""You are a senior PM. Write a SINGLE PARAGRAPH (4-6 sentences) decision dossier for {ticker}{f' ({company_name})' if company_name else ''}.

This is the top-of-page brief that ties everything together. The user reads it in 15 seconds and walks away knowing what to do.

TACTICAL STATE
- Decision: {action.replace('_', ' ')}
- Bias: {bias}
- Setup score: {t_state.get('setup_score', 0):.1f}/10
- Price: ${t_state.get('price', 0):.2f}; 50-day MA ${t_state.get('ma50', 0):.2f}; 200-day MA ${t_state.get('ma200', 0):.2f}
- RSI: {t_state.get('rsi14', 50):.0f}
- Relative strength vs SPX: {t_state.get('rs', 1):.2f} (10d delta {t_state.get('rs_delta', 0):+.3f})
- ATR: {t_state.get('atr_pct', 0)*100:.2f}%
- Trigger: {trig_summary}
- Buy above: {f'${buy_above:.2f}' if buy_above else 'n/a'}
- Invalidation below: {f'${abort_below:.2f}' if abort_below else 'n/a'}

CONTEXT
- Market regime (SPY): {regime}
- Sector: {(meta or {}).get('sector') or 'unknown'}
- Earnings: {f'in {meta.get("earnings_days")} days' if meta and meta.get('earnings_days') is not None else 'no near-term'}
- Forward P/E: {(meta or {}).get('forward_pe') or 'n/a'}, PEG: {(meta or {}).get('peg') or 'n/a'}

HISTORICAL
{history_line if history_line else 'No useful 50-day test history.'}

DECISION MODIFIERS
{modifiers_block}

PM THESIS
{thesis or 'No thesis on file.'}

Write the dossier in this voice: senior trader/PM, confident, specific. Reference exact prices and levels. No hedging, no boilerplate. Cover what's happening tactically, what fundamentals say, and what the user should DO. End with the action condition (buy at X / wait for Y / pass entirely).

Return ONLY the paragraph text. No JSON, no markdown headers, no preamble."""

        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        # Strip stray code fences if Claude adds them
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            text = text.strip()
        return {"dossier": text, "_source": "claude"}

    except Exception as e:
        return {"dossier": None, "_source": f"error: {str(e)[:80]}"}
