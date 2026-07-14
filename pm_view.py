"""Portfolio Manager view — two-layer model.

Layer 1 — Snapshot: thesis, drivers, risks, valuation. Always visible.
Layer 2 — Deep dive: expanded thesis, variant perception, catalysts,
  risk scenarios, valuation context, what-must-be-true, what-would-change-my-mind.
  Revealed on demand.

Both layers generated in a single Claude call to keep cost at one request per ticker.
"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

CLAUDE_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6-20260217").strip()
CLAUDE_MODEL_FALLBACKS = [
    CLAUDE_MODEL,
    "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5-20251015",
]
CLAUDE_PM_TIMEOUT_SECONDS = max(8, int(os.environ.get("CLAUDE_PM_TIMEOUT_SECONDS", "18")))
CLAUDE_DOSSIER_TIMEOUT_SECONDS = max(8, int(os.environ.get("CLAUDE_DOSSIER_TIMEOUT_SECONDS", "18")))


def _call_with_timeout(fn, timeout_seconds, label):
    """Run a blocking API call with a hard UI timeout."""
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeout as exc:
        raise TimeoutError(f"{label} timed out after {timeout_seconds}s") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _model_candidates():
    seen = set()
    for model in CLAUDE_MODEL_FALLBACKS:
        model = str(model or "").strip()
        if model and model not in seen:
            seen.add(model)
            yield model


def _is_model_not_found(exc):
    text = str(exc).lower()
    return "not_found_error" in text or "model" in text and "not found" in text


def _messages_create(client, **kwargs):
    """Create an Anthropic message with model fallback on 404/model rename."""
    last_exc = None
    for model in _model_candidates():
        try:
            return client.messages.create(model=model, **kwargs)
        except Exception as exc:
            if _is_model_not_found(exc):
                last_exc = exc
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("No Claude model configured.")


def _parse_json_response(text):
    """Parse Claude JSON even if it adds fences or surrounding prose."""
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
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return json.loads(text)


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

    # Some fast watchlist/sidebar snapshots store already-computed percent
    # fields instead of the raw moving averages. Use those as a fallback so
    # cached Claude dissent notes do not leak {pct_ma200}/{rs} placeholders.
    def _fallback_pct(name, current):
        if current is not None:
            return current
        raw = t.get(name)
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    pct_ma50 = _fallback_pct("pct_ma50", pct_ma50)
    pct_ma100 = _fallback_pct("pct_ma100", pct_ma100)
    pct_ma200 = _fallback_pct("pct_ma200", pct_ma200)
    pct_52w_high = _fallback_pct("pct_52w_high", pct_52w_high)
    pct_52w_low_v = _fallback_pct("pct_52w_low", pct_52w_low_v)

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
    "SATS": {
        "thesis": "EchoStar is no longer just a levered satellite/telecom asset; the active debate is whether its spectrum monetization and SpaceX equity exposure make it a liquid public proxy for Starlink/SpaceX direct-to-cell upside.",
        "drivers": ["SpaceX equity received in spectrum deals", "Starlink direct-to-cell commercial link", "Spectrum monetization de-risks balance sheet"],
        "risks": ["Deal closing and regulatory mechanics", "Legacy DISH/Hughes cash-flow drag", "SpaceX proxy premium can unwind quickly"],
        "valuation": "Valuation should be framed as a sum-of-parts: SpaceX stock value, remaining spectrum/telecom assets, net debt, and legacy operating burn.",
    },
}

# Keep the older name so existing imports don't break.
STATIC_VIEWS = STATIC_SNAPSHOTS


TICKER_RESEARCH_CONTEXT = {
    "NVDA": [
        "Frame NVDA as the AI infrastructure compute standard, not just a chip cycle. The debate is whether Blackwell/rack-scale systems and CUDA networking lock-in extend the cycle or whether hyperscaler capex/custom silicon compress margins and multiples.",
        "Separate long-term platform quality from entry timing; a great business can still be a bad fresh entry if the chart is extended.",
    ],
    "AVGO": [
        "Broadcom is a custom silicon plus infrastructure software compounder after VMware, not only a semiconductor beta. Discuss AI ASIC demand, VMware margin/cash extraction, debt paydown, and customer concentration risk.",
    ],
    "AMD": [
        "The bull case is MI-series accelerator share gain and CPU recovery; the bear case is that AMD remains a second-source AI supplier with weaker software lock-in versus NVDA. Do not write a generic semiconductor memo.",
    ],
    "MU": [
        "Micron is a memory-cycle and HBM pricing story. The PM read must discuss DRAM/NAND supply discipline, HBM mix, cycle timing, and the risk that low multiples can signal peak-cycle earnings.",
    ],
    "MRVL": [
        "Marvell is an AI networking/custom silicon and optical connectivity story. Separate true AI revenue durability from cyclical storage/networking recovery and valuation risk.",
    ],
    "COHR": [
        "Coherent is an optical components/datacenter interconnect recovery story with balance-sheet and execution risk. Discuss AI optical demand, telecom/industrial cyclicality, and whether margin recovery is real.",
    ],
    "CRDO": [
        "Credo is a high-beta AI connectivity/SerDes and active electrical cable story. Emphasize customer concentration, design-win durability, gross margin, and whether demand is structural or inventory-cycle driven.",
    ],
    "ANET": [
        "Arista is a cloud networking compounder tied to AI data-center scale-out. Discuss Ethernet AI fabrics, hyperscaler concentration, Cisco/white-box risk, and whether AI networking keeps growth above normal enterprise cycles.",
    ],
    "TER": [
        "Teradyne is a test-equipment cycle plus robotics optionality story. Discuss semiconductor test recovery, AI/advanced packaging demand, auto/industrial cyclicality, and Universal Robots recovery.",
    ],
    "VRT": [
        "Vertiv is an AI data-center power/cooling infrastructure beneficiary. The debate is whether backlog and pricing power make it a durable compounder or whether it is a capex-cycle winner trading at peak expectations.",
    ],
    "PLTR": [
        "Palantir is an ontology/AIP operating-system story, not just government analytics. Discuss commercial AIP adoption, government durability, usage-to-revenue conversion, valuation, and whether it becomes core enterprise infrastructure.",
    ],
    "DASH": [
        "DoorDash is a local-commerce network and logistics platform, not merely food delivery. Discuss marketplace liquidity, international and grocery expansion, ad/commerce monetization, gig-worker regulation, and Uber Eats competition.",
    ],
    "COIN": [
        "Coinbase is operating leverage to crypto activity plus custody/staking/stablecoin/regulatory optionality. Separate crypto beta from the durable exchange/custody franchise and discuss regulatory and fee-compression risk.",
    ],
    "RKLB": [
        "Rocket Lab is a space infrastructure company, not only a small-launch provider. Discuss Neutron, spacecraft/components revenue, launch cadence, funding runway, and whether it can become a scaled alternative to SpaceX in selected missions.",
    ],
    "ASTS": [
        "AST SpaceMobile is a direct-to-device cellular satellite special situation. Discuss satellite deployment cadence, carrier/MNO partnerships, spectrum/regulatory execution, funding needs, and whether technical validation converts into commercial service.",
    ],
    "SATS": [
        "EchoStar/SATS has become a SpaceX/Starlink-adjacent special situation, not a plain satellite TV or telecom comp.",
        "The key thesis is the sum-of-parts and proxy debate: SpaceX equity received or expected from spectrum transactions, cash proceeds/debt relief, Starlink Direct-to-Cell commercial linkage, and remaining DISH/Hughes/Boost operating assets.",
        "The PM narrative MUST discuss whether SATS is a credible liquid public proxy for pre-IPO SpaceX/Starlink exposure, and must separate that optionality from the legacy operating business.",
        "Do not present the SpaceX angle as a guaranteed IPO payoff. Frame it as optionality with deal-closing, valuation, liquidity, regulatory, tax, and proxy-premium unwind risk.",
    ],
    "IREN": [
        "IREN is not only bitcoin mining; it is a BTC mining plus AI/HPC power-infrastructure optionality story. Discuss power access, data-center conversion credibility, BTC price sensitivity, dilution/capex, and contract quality.",
    ],
    "VST": [
        "Vistra is a power-generation and load-growth story tied to electricity scarcity, nuclear/gas fleet value, and AI/data-center demand. Discuss power prices, capacity markets, hedging, leverage, and regulatory risk.",
    ],
    "NVO": [
        "Novo Nordisk is an obesity/GLP-1 category leader. Discuss Wegovy/Ozempic supply, pricing/reimbursement, competition from Lilly and next-gen incretins, and whether growth durability supports the multiple.",
    ],
    "SKM": [
        "SK Telecom is not just a defensive Korean telecom ADR; the key variant debate is whether its Anthropic equity exposure and AI data-center ambitions create hidden asset value relative to the market cap.",
        "Known context to verify and update with live research: SKT announced an additional $100M Anthropic investment in 2023 plus a telco-LLM partnership; analyst/news estimates have framed SKT's Anthropic ownership anywhere from roughly 0.1-0.7% depending on dilution assumptions, with some estimates putting the stake value around 3-20%+ of SKM market cap. Treat this as an estimated range, not a hard fact.",
        "The PM memo MUST explicitly include this math: estimated Anthropic stake value / SKM market cap. Use live market cap from the data context and live/analyst Anthropic valuation or stake estimates from research. If the inputs conflict, show the range and label confirmed vs estimated.",
        "Also discuss dilution uncertainty, foreign-ownership/ADR constraints, Korea discount, FX risk, governance/chaebol restructuring risk, and whether the core telecom dividend/cash flow supports downside.",
        "Treat the Anthropic stake as a hidden-asset/proxy thesis, not as guaranteed upside. Separate confirmed company disclosures from analyst estimates and retail narrative math.",
    ],
    "ZM": [
        "Zoom can screen as an Anthropic proxy because of prior strategic investment, but the operating debate is still core enterprise communications durability, AI monetization, cash balance, and growth reacceleration. Estimate any Anthropic stake value as % of market cap only if live research supports it.",
    ],
    "ICOP": [
        "Treat ICOP as an ETF/fund exposure, not an operating company. The thesis should focus on copper/critical metals cycle, China/global capex demand, mine supply constraints, holdings concentration, expense ratio, and ETF liquidity.",
    ],
    "CQQQ": [
        "Treat CQQQ as an ETF/fund exposure, not an operating company. Discuss China technology beta, policy/regulatory risk, ADR/geopolitical risk, holdings concentration, FX, and whether China internet sentiment is repairing.",
    ],
    "EWY": [
        "Treat EWY as an ETF/fund exposure, not an operating company. Discuss Korea equity beta, Samsung/SK Hynix memory cycle exposure, won/FX risk, China/export sensitivity, and fund concentration.",
    ],
    "BTC-USD": [
        "Treat BTC as a macro/liquidity and digital scarcity asset, not a company. Discuss ETF flows, real rates/liquidity, halving/supply dynamics, leverage/funding, and regulatory/custody risk.",
    ],
}

RESEARCH_CONTEXT_TICKERS = set(TICKER_RESEARCH_CONTEXT)


def _special_context_for(ticker):
    lines = TICKER_RESEARCH_CONTEXT.get((ticker or "").upper())
    if not lines:
        return """
GENERAL PM CONTEXT DISCIPLINE - mandatory:
- Before writing, identify the actual reason this ticker matters now: product cycle, capital structure, regulatory/event path, asset value, category leadership, hidden optionality, or ETF/factor exposure.
- Do not write a generic company summary. Name the dominant debate and the variant view in plain English.
- If a ticker has an obvious non-financial thesis hook that would matter to a real PM, include it even if it is not present in the tactical data.
"""
    bullets = "\n".join(f"- {line}" for line in lines)
    return f"""
TICKER-SPECIFIC PM CONTEXT - mandatory to incorporate:
{bullets}
"""


def _empty_deep_dive(ticker):
    return {
        "expanded_thesis": None,
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
        "thesis": f"No generated PM thesis yet for {ticker}. Use Refresh PM memo to generate current PM research, drivers, risks, and valuation.",
        "drivers": ["Not yet analyzed"],
        "risks": ["Not yet analyzed"],
        "valuation": "Not yet analyzed",
    }


def _fetch_recent_news(client, ticker, company_name):
    """Web-search call to get recent PM-relevant research context.
    Handles the multi-turn tool-use loop that web_search requires.
    Returns a formatted block ready to inject into any prompt, or empty string."""
    # Keep the live app responsive. The PM prompt is already instructed to
    # surface off-statement / special-situation facts, and blocking every
    # refresh on web search made new tickers feel frozen.
    return ""
    name = company_name or ticker
    tools = [{"type": "web_search_20250305", "name": "web_search"}]
    identity = f"{ticker} {name}".strip()
    # Keep this deliberately short. The refresh button should improve the PM
    # memo, not trap the page in a research crawl.
    search_queries = [
        (
            f"US-listed {identity} stock investment thesis variant view bull case bear case "
            f"special situation strategic partnership acquisition hidden asset value proxy regulatory catalyst "
            f"key risks short interest customer concentration insider institutional ownership 2025 2026"
        ),
        (
            f"US-listed {identity} most recent earnings revenue EPS guidance margins backlog cash flow "
            f"analyst call transcript 2025 2026"
        ),
    ]
    all_results = []

    try:
        def _run_search(query_text):
            """Run one search query through the agentic loop, return text."""
            msgs = [{"role": "user", "content": query_text}]
            for _ in range(5):
                try:
                    resp = _call_with_timeout(
                        lambda: _messages_create(client,
                            max_tokens=800,
                            tools=tools,
                            messages=msgs,
                            betas=["web-search-2025-03-05"],
                        ),
                        12,
                        "Claude web search",
                    )
                except TypeError as err:
                    if "betas" not in str(err):
                        raise
                    resp = _call_with_timeout(
                        lambda: _messages_create(client,
                            max_tokens=800,
                            messages=msgs,
                        ),
                        12,
                        "Claude fallback search",
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
                f"\n\nLIVE RESEARCH CONTEXT (web search — more current than training data; "
                f"you MUST incorporate the PM-relevant facts, catalysts, risks, and variant-thesis angles found here):\n{combined}\n"
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
        special_context = _special_context_for(ticker)
        prompt = f"""You are a senior portfolio manager at a long-biased hedge fund. Write a full investment note on {ticker}{' (' + company_name + ')' if company_name else ''}.

CRITICAL: The ticker {ticker} refers to the US-listed security "{company_name if company_name else ticker}" trading on US stock exchanges (NYSE/NASDAQ). Do NOT confuse it with any foreign company that may share the same ticker symbol on another exchange (e.g. a Singapore, London, or Hong Kong-listed company). If the yfinance name seems wrong, trust the US stock market context — {ticker} is a US-listed security. All analysis must be about the US-listed {ticker} only.{recent_news}
{special_context}
Current tactical state from the system (for context, not the focus):
- Directional bias: {t.get('bias') or 'unclear'}
- Action: {t.get('action', 'unknown')}
- Technical score: {t.get('setup_score', 0):.1f} / 10
- Projected reward/risk: {f"{t.get('reward_risk'):.2f}:1" if t.get('reward_risk') is not None else 'n/a'}

PM MEMO QUALITY BAR:
- Your job is judgment, not data transcription. Do not merely restate the tactical inputs.
- Every paragraph must answer one of these questions: why own it, why avoid it, why now, what is priced in, what would change the call.
- Separate business underwriting from trading timing. A great business can be a bad fresh entry; a broken chart can still be a real company.
- Avoid generic phrases like "strong fundamentals", "growth opportunity", "competitive landscape", or "execution risk" unless immediately tied to a specific mechanism.
- Use crisp investor language: moat, unit economics, revenue durability, margin structure, balance-sheet risk, multiple support, catalyst path.
- Research completeness test: before writing, identify the one or two critical facts a real PM would be embarrassed to miss. These may be strategic partnerships, pending transactions, hidden asset value, regulatory decisions, financing/dilution risk, customer concentration, product-cycle inflection, short interest, insider/institutional behavior, or ETF/factor exposure.
- If live research shows a special situation, proxy exposure, major partnership, litigation/regulatory overhang, acquisition, restructuring, balance-sheet event, or upcoming product cycle, it MUST appear in thesis, risks, and valuation context.
- Hidden asset math: if the thesis depends on a stake in another company, private-company exposure, venture holding, cash/investment portfolio, spectrum, real estate, patents, or other non-core asset, estimate value as a percentage of the current market cap whenever enough information exists. Show the range and state what is confirmed vs estimated.
- Never stop at "partnership" if ownership economics matter. For proxy trades, the PM note must answer: how big is the stake/exposure, what is it worth, what percent of the public company's market cap does that represent, what can dilute or trap the value, and what catalyst unlocks it.
- If you cannot verify a suspected critical fact from live research, say the thesis depends on an unverified market narrative rather than treating it as fact.

Return ONLY JSON in exactly this shape. No preamble, no code fences.

{{
  "quality": {{
    "tier": "A | B | Speculative | Avoid",
    "rationale": "1 sentence. Long-term ownership quality, independent of the tactical entry."
  }},
  "thesis": "1-2 sentences. State the actual underwriting view and what the market may be mispricing.",
  "drivers": ["3 short items, no period at end"],
  "risks": ["3 short items, no period at end"],
  "valuation": "1 sentence on valuation context",
  "deep_dive": {{
    "expanded_thesis": "4-6 sentences. Frame consensus, your variant view, what is priced in, timing, and what would prove the view wrong.",
    "business": "2-3 sentences on revenue model, key segments, margin structure, and durability of the franchise",
    "variant_bull": "1-2 sentences on the specific bull case that is NOT consensus",
    "variant_bear": "1-2 sentences on the specific bear case that is NOT consensus",
    "variant_needs": "1 sentence on what specifically has to happen for the variant to play out",
    "catalysts": ["3 time-bound catalysts over the next 1-2 quarters, each one line, concrete and dated when possible"],
    "risk_scenarios": ["3 specific failure modes, not generic risks. Each one line."],
    "valuation_context": "2-3 sentences comparing multiple to growth durability, peer/history context if known, and the earnings/cash-flow path required to defend today's price",
    "must_be_true": ["3 things that must hold for the thesis to work. Each phrased as a specific measurable condition."],
    "would_change_mind": ["3 things that would invalidate the thesis. Each phrased as a specific observable trigger."]
  }}
}}

Voice: senior PM, confident, specific, opinionated. No hedging, no consultantese, no corporate-speak. Include both the upside case and the kill criteria; do not write a one-sided bull pitch. Write in complete sentences.
Return ONLY the JSON, nothing else."""

        for _attempt in range(2):
            try:
                message = _call_with_timeout(
                    lambda: _messages_create(client,
                        max_tokens=2400,
                        temperature=0,
                        messages=[{"role": "user", "content": prompt}],
                    ),
                    CLAUDE_PM_TIMEOUT_SECONDS,
                    "Claude PM note",
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
        parsed = _parse_json_response(text)
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
                         api_key=None, company_name=None, fast=False):
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

        special_context = _special_context_for(ticker)
        recent_news_block = ""

        bias = t_state.get("bias") or t_state.get("raw_bias") or "unclear"
        action = t_state.get("action", "unknown")
        trigger = t_state.get("trigger") or {}
        trig_summary = trigger.get("summary", "n/a") if trigger else "n/a"
        buy_above = trigger.get("levels", {}).get("buy_above") if trigger else None
        abort_below = trigger.get("levels", {}).get("abort_below") if trigger else None
        ma50_hist = t_state.get("ma50_history")
        regime = t_state.get("market_regime", "unknown")
        tech_ctx = t_state.get("technical_prompt_context") or {}
        td_daily = tech_ctx.get("td_daily") or {}
        td_weekly = tech_ctx.get("td_weekly") or {}

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

        if fast:
            fast_prompt = f"""You are a senior portfolio manager and trader. Refresh the on-page PM research for {ticker}{f' ({company_name})' if company_name else ''}.

Return ONLY valid JSON. No markdown.

Context:
- Rule action: {action.replace('_', ' ')}
- State: {t_state.get('state', 'unknown')}
- Bias: {bias}
- Price: ${t_state.get('price', 0):.2f}
- 50d MA: ${t_state.get('ma50', 0):.2f}; 200d MA: ${t_state.get('ma200', 0):.2f}
- RSI: {t_state.get('rsi14', 50):.0f}; RS vs SPY: {t_state.get('rs', 1):.2f}; RS delta: {t_state.get('rs_delta', 0):+.3f}
- Volume vs 20d avg: {t_state.get('vol_ratio', 1):.2f}x
- Reward/risk: {f"{t_state.get('reward_risk'):.2f}:1" if t_state.get('reward_risk') is not None else 'n/a'}
- Trigger: {trig_summary}; buy above {f'${buy_above:.2f}' if buy_above else 'n/a'}; invalidation {f'${abort_below:.2f}' if abort_below else 'n/a'}
- Sector: {(meta or {}).get('sector') or 'unknown'}; industry: {(meta or {}).get('industry') or 'unknown'}
- Market cap: {(meta or {}).get('market_cap') or 'unknown'}
- Earnings: {f'in {meta.get("earnings_days")} days' if meta and meta.get('earnings_days') is not None else 'no near-term'}
- Forward P/E: {(meta or {}).get('forward_pe') or 'n/a'}; PEG: {(meta or {}).get('peg') or 'n/a'}; EV/EBITDA: {(meta or {}).get('ev_ebitda') or 'n/a'}
- Earnings growth: {(meta or {}).get('earnings_growth') or 'n/a'}%; Revenue growth: {(meta or {}).get('revenue_growth') or 'n/a'}%
- Existing thesis: {thesis or 'none'}
- Existing drivers: {', '.join(drivers) if drivers else 'none'}
- Existing risks: {', '.join(risks) if risks else 'none'}
{special_context}

Use live-value tokens in prose where current values appear: {{price}}, {{pct_ma50}}, {{pct_ma200}}, {{rs}}, {{rsi}}.

JSON shape:
{{
  "dossier": "3-4 sentence decision memo. Mention the rule action and what changes it.",
  "technical_narrative": "1-2 concise paragraphs on trend, momentum, RS, volume, and trigger.",
  "pm_narrative": "2 concise paragraphs on business thesis, variant view, valuation, and what would change your mind.",
  "bullets": {{
    "thesis": "1-2 sentences",
    "drivers": ["exactly 3 short specific drivers"],
    "risks": ["exactly 3 short specific risks"],
    "valuation": "1 sentence"
  }},
  "quality": {{
    "tier": "A | B | Speculative | Avoid",
    "rationale": "1-2 sentences"
  }},
  "tactical_call": {{
    "action": "ENTER | WATCH | HOLD_OFF | AVOID | ACCUMULATE",
    "confidence": "integer 1-10",
    "reasoning": "2 sentences",
    "trigger": "specific condition or null",
    "invalidation": "specific condition or null",
    "notes": "one sentence or empty"
  }}
}}

Be specific. Do not return placeholders. If the business has a special-situation angle, hidden asset, major strategic relationship, financing risk, customer concentration, or regulatory catalyst, include it."""

            message = _call_with_timeout(
                lambda: _messages_create(client,
                    max_tokens=1400,
                    temperature=0,
                    messages=[{"role": "user", "content": fast_prompt}],
                ),
                min(CLAUDE_DOSSIER_TIMEOUT_SECONDS, 45),
                "Claude fast PM refresh",
            )
            text = message.content[0].text.strip()
            if text.startswith("```"):
                parts = text.split("```")
                text = parts[1] if len(parts) > 1 else text
                if text.lower().startswith("json"):
                    text = text[4:]
                text = text.strip()
            parsed = _parse_json_response(text)
            return {
                "dossier": parsed.get("dossier"),
                "technical_narrative": parsed.get("technical_narrative"),
                "pm_narrative": parsed.get("pm_narrative"),
                "bullets": parsed.get("bullets") or {},
                "quality": parsed.get("quality") or {},
                "tactical_call": parsed.get("tactical_call") or {},
                "_source": "claude · fast refresh",
            }

        prompt = f"""You are a senior portfolio manager and trader. Generate THREE pieces of analysis on {ticker}{f' ({company_name})' if company_name else ''}.

CRITICAL: The ticker {ticker} refers to the US-listed security "{company_name if company_name else ticker}" trading on US stock exchanges (NYSE/NASDAQ). Do NOT confuse it with any foreign company sharing the same ticker on another exchange. If the company name seems unfamiliar or foreign, use your knowledge of US-listed stocks to identify the correct company for ticker {ticker}. All analysis must be about the US-listed {ticker} only.{recent_news_block}
{special_context}
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
- MA stack read: {tech_ctx.get('stack_read') or 'n/a'}
- MACD: {f"{tech_ctx.get('macd'):.2f} vs signal {tech_ctx.get('macd_signal'):.2f}, histogram {tech_ctx.get('macd_hist'):+.2f}" if tech_ctx.get('macd') is not None and tech_ctx.get('macd_signal') is not None and tech_ctx.get('macd_hist') is not None else 'n/a'}; MACD read: {tech_ctx.get('macd_read') or 'n/a'}
- TD daily setup: {td_daily.get('side', '—')} {td_daily.get('count', 0)}/9 · {td_daily.get('status', 'n/a')}
- TD weekly setup: {td_weekly.get('side', '—')} {td_weekly.get('count', 0)}/9 · {td_weekly.get('status', 'n/a')}
- Daily/weekly trend: daily {tech_ctx.get('stack_read') or 'n/a'}; weekly {tech_ctx.get('weekly_read') or 'n/a'}; weekly RSI {f"{tech_ctx.get('weekly_rsi'):.0f}" if tech_ctx.get('weekly_rsi') is not None else 'n/a'}
- 20-day alpha vs SPY: {f"{tech_ctx.get('bench_rs_20'):+.1f}%" if tech_ctx.get('bench_rs_20') is not None else 'n/a'}; 20-day realized vol: {f"{tech_ctx.get('realized_vol_20'):.1f}%" if tech_ctx.get('realized_vol_20') is not None else 'n/a'}
- 20-day range: {f"${tech_ctx.get('low_20'):.2f}–${tech_ctx.get('high_20'):.2f}" if tech_ctx.get('low_20') is not None and tech_ctx.get('high_20') is not None else 'n/a'}

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

GLOBAL MEMO QUALITY RULES:
- Do not duplicate the same observation across dossier, technical_narrative, and pm_narrative. Each field has a distinct job.
- dossier = decision synthesis. technical_narrative = chart/tape evidence. pm_narrative = business underwriting and portfolio judgment.
- PM narrative must NOT be a second technical recap. Mention current price only when discussing valuation, sizing, or entry discipline.
- If the company is high quality but the entry is bad, say that plainly. If the setup is good but the business is speculative, say that plainly.
- Name the dominant debate in one sentence: "The debate is whether ___ or ___." Work that debate into pm_narrative.
- Include an explicit "what would change my mind" idea in pm_narrative paragraph 4, not only in bullets.
- Avoid generic filler: "monitor execution", "competitive pressures", "macro uncertainty", "growth potential" are banned unless made specific.
- Research completeness test: do not finalize until you have checked whether the ticker has a special-situation angle, strategic relationship, hidden asset/liability, regulatory catalyst, financing risk, customer concentration, product-cycle inflection, short-interest squeeze risk, or ETF/factor exposure. If any exists, incorporate it directly.
- The most important non-obvious fact should appear in the dossier and the PM narrative. Do not bury it in bullets.
- Prefer current live research over stale training knowledge. If live research conflicts with the existing PM thesis snapshot, trust the newer research and call out the change.
- Hidden asset / proxy math is mandatory when relevant: if live research identifies a private-company stake, strategic investment, spectrum asset, investment portfolio, cash pile, venture book, or other non-core asset, estimate the asset value as % of the company's market cap using available market cap from the data context. If ownership or valuation is uncertain, present a range and label it as estimated. This belongs in dossier, pm_narrative, and valuation.
- A memo that mentions a hidden asset or private stake without sizing it against market cap is incomplete. Include the math, even if it is a range.

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
- Para 2: momentum + volume + relative strength as a connected read. RS at {{rs}}, RSI at {{rsi}}, MACD read, volume ratio, and 20-day alpha vs SPY. Say whether participation confirms or undercuts the move.
- Para 3: daily-vs-weekly confirmation and exhaustion risk. Explicitly mention TD daily/weekly setup counts when they are active, and explain whether the daily and weekly tapes agree or diverge.
- Para 4 (optional, if useful): historical pattern context, 20-day range, realized volatility, or how the broader regime ({regime} SPY) changes the risk/reward.

pm_narrative: 3-4 paragraphs. Senior PM voice. Do NOT recap MA50/MA200/RS/RSI unless directly tied to sizing or timing. Walk through:
- Para 1: business underwriting — what the business actually does, how it makes money, and why the moat/margin structure is or is not durable.
- Para 2: variant view — "The debate is whether ___ or ___." Explain consensus, the bull case requirement, the bear case requirement, and which side currently has better evidence.
- Para 3: valuation context — what's priced in at current multiples, how the math compares to growth/cash conversion, and the earnings path required to defend the stock from here.
- Para 4: portfolio implementation — fresh-entry posture, owned-position posture if different, sizing posture, dominant catalyst, dominant risk, and the concrete evidence that would change your mind.

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
- tech_score ≥ 8.5
- either the rule-engine action is ENTER, or the trigger has already fired / market entry is actionable now
- NOT extended (price within ~12% above MA50 AND within ~8% above MA100)
- projected reward/risk is at least ~1.2:1, or the invalidation is tight and obvious from the current level
- earnings are not within the next 7 calendar days

WATCH if:
- bullish bias but tech_score < 8.5 (waiting on confirmation), OR
- valid trigger exists and is approaching but not fired, OR
- bullish bias but extended → wait for pullback target
- clean setup but earnings are 3-7 days away → wait for post-print reset

HOLD_OFF (universal default for ambiguity) if:
- earnings are within 0-2 days for a fresh entry
- projected reward/risk is below ~1.0:1
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

FINAL RULE: if uncertain or the setup is not clearly actionable with favorable risk/reward, default to WATCH when the trend is bullish and a concrete trigger exists; default to HOLD_OFF only when the setup lacks a near-term trigger or has unfavorable risk/reward. Do NOT force trades. Do NOT default to optimism. Your job is to identify clean setups without chasing bad entries.

Style across all three:
- Confident, opinionated, specific. No hedging, no consultantese.
- Reference exact prices, multiples, percentages. Numbers anchor every claim.
- Write like a memo, not a marketing brochure.
- For pm_narrative, do NOT just restate the thesis snippet above — extend it with judgment, framing, and specific reasoning.

Return ONLY the JSON object. No markdown fencing, no preamble, no commentary."""

        message = _call_with_timeout(
            lambda: _messages_create(client,
                    max_tokens=2400,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            ),
            CLAUDE_DOSSIER_TIMEOUT_SECONDS,
            "Claude decision dossier",
        )
        text = message.content[0].text.strip()
        # Strip stray code fences if Claude adds them
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        parsed = _parse_json_response(text)
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
