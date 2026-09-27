"""Backend-owned, web-researched thematic idea discovery."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any


DEFAULT_UNIVERSE = (
    "AAPL, ABBV, ABNB, ABT, ACN, ADBE, ADI, ADP, AMAT, AMD, AMGN, AMZN, ANET, "
    "ARM, ASML, ASTS, AVGO, AXP, BA, BAC, BABA, BDX, BKNG, BLK, BMY, BSX, CAT, "
    "CEG, CMI, COIN, COST, CRM, CRWD, CSCO, CVX, DASH, DE, DELL, DIS, DLR, DUOL, "
    "ELF, ETN, F, FCX, GE, GEV, GILD, GM, GOOGL, GS, HD, HON, HOOD, IBM, INTC, "
    "INTU, ISRG, JNJ, JPM, KLAC, LLY, LMT, LOW, LRCX, MA, MAR, MCD, MDT, MELI, "
    "META, MMM, MRK, MRVL, MS, MSFT, MU, NEE, NFLX, NKE, NOW, NVDA, NVO, NXPI, "
    "ORCL, PANW, PEP, PFE, PLTR, QCOM, RBLX, RDDT, REGN, ROK, RTX, SBUX, SCHW, "
    "SHOP, SMCI, SNOW, SOFI, SPGI, SPOT, TGT, TJX, TMO, TSM, TSLA, UBER, UNH, "
    "V, VRT, WMT, XOM, ZS, SPY, QQQ, IWM, RSP, XLK, XLF, XLE, XLI, XLY, XLP, "
    "XLV, XLU, XLB, SMH, IGV, XBI, ICLN, TAN, BOTZ, ARKK, IBIT, ETHA"
)


def _json_object(text: str) -> dict:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1].removeprefix("json").strip()
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    return {}


def _citation_dict(value: Any) -> dict[str, Any] | None:
    get = value.get if isinstance(value, dict) else lambda key, default=None: getattr(value, key, default)
    url = get("url")
    if not url:
        return None
    return {
        "url": str(url),
        "title": str(get("title") or url),
        "accessed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _response_text_and_sources(response: Any) -> tuple[str, list[dict[str, Any]]]:
    text_parts: list[str] = []
    sources: list[dict[str, Any]] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "text", None):
            text_parts.append(str(block.text))
        for citation in getattr(block, "citations", None) or []:
            normalized = _citation_dict(citation)
            if normalized:
                sources.append(normalized)
        for item in getattr(block, "content", None) or []:
            normalized = _citation_dict(item)
            if normalized:
                sources.append(normalized)
    unique = {row["url"]: row for row in sources}
    return "\n".join(text_parts).strip(), list(unique.values())


def generate(query: str, universe: str | None, api_key: str) -> dict:
    cleaned = str(query or "").strip()
    if len(cleaned) < 8:
        raise ValueError("Write a little more about the theme you want.")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured for the worker")

    from anthropic import Anthropic

    explicit = bool(str(universe or "").strip())
    raw_universe = str(universe or DEFAULT_UNIVERSE)
    names = list(dict.fromkeys(
        item.strip().upper()
        for item in raw_universe.replace("\n", ",").split(",")
        if item.strip()
    ))[:200]
    universe_note = (
        "Use only these securities" if explicit else
        "Use these liquid securities as the primary screen; add another liquid US-listed stock or ETF only when web evidence shows it is a substantially better fit"
    )
    research_prompt = f"""You are researching an equity theme for Trading Desk.

User request: {cleaned}
{universe_note}: {', '.join(names)}

Run one focused web search for current, attributable evidence. Identify up to 8
US-listed stocks or ETFs that plausibly match. Prefer primary sources. Return
concise research notes naming the tickers, relevant facts, caveats, dates, and
source URLs. Do not make a buy/sell recommendation and do not return JSON."""

    synthesis_prompt = """Using only the web research above, create the saved
Trading Desk idea screen. This is a research screen, not a buy list or an
investment recommendation. The thematic score measures theme fit only; never
infer or fabricate a Trading Desk action.

Requirements:
- Translate the request into 4-6 explicit screening criteria.
- Use current evidence and state caveats and disconfirming facts.
- Distinguish theme fit from financial fit.
- Prefer primary sources and include at most 2 sources per candidate, with URL,
  title, and publication date when known. Never fabricate a citation or metric.
- Mark unknown facts as unknown and put them in verify_next.

Return ONLY JSON with this shape:
{{"criteria":["..."],"summary":"...","research_as_of":"ISO timestamp",
"sources":[{{"url":"https://...","title":"...","published_at":"ISO date or null"}}],
"candidates":[{{"ticker":"TICKER","company":"Name","score":1,
"theme_fit":"...","financial_fit":"...","why_it_matters":"...",
"risks":"...","caveats":["..."],"evidence":["..."],
"verify_next":["..."],"sources":[{{"url":"https://...","title":"...",
"published_at":"ISO date or null"}}]}}]}}"""

    # Bound each network round trip so a provider-side stall cannot consume an
    # entire scheduled worker run. A paused search may make one continuation.
    client = Anthropic(api_key=api_key, timeout=75.0)
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6").strip()
    research_messages = [{"role": "user", "content": research_prompt}]
    research_kwargs = {
        "model": model,
        "max_tokens": 3000,
        "messages": research_messages,
        # A single direct search returns several results without allowing an
        # open-ended server-side research loop to consume the worker runtime.
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
    }
    try:
        research_response = client.messages.create(**research_kwargs)
    except TypeError:
        research_kwargs.pop("tools", None)
        research_response = client.messages.create(**research_kwargs)

    if getattr(research_response, "stop_reason", None) == "pause_turn":
        research_messages.append({"role": "assistant", "content": research_response.content})
        research_kwargs["messages"] = research_messages
        research_response = client.messages.create(**research_kwargs)

    _research_text, sdk_sources = _response_text_and_sources(research_response)
    synthesis_messages = [
        {"role": "user", "content": research_prompt},
        {"role": "assistant", "content": research_response.content},
        {"role": "user", "content": synthesis_prompt},
    ]
    response = client.messages.create(
        model=model,
        max_tokens=8000,
        messages=synthesis_messages,
    )

    final_text, final_sources = _response_text_and_sources(response)
    sdk_sources = list({row["url"]: row for row in [*sdk_sources, *final_sources]}.values())
    parsed = _json_object(final_text)
    candidates = [
        row for row in (parsed.get("candidates") or [])
        if isinstance(row, dict) and str(row.get("ticker") or "").strip()
    ][:8]
    if not candidates:
        stop_reason = str(getattr(response, "stop_reason", "unknown") or "unknown")
        raise ValueError(
            "Claude research ended without a complete candidate payload "
            f"(stop_reason={stop_reason}, response_chars={len(final_text)}). "
            "Try the screen again."
        )

    allowed = set(names)
    if explicit:
        candidates = [row for row in candidates if str(row.get("ticker") or "").upper().strip() in allowed]
    if not candidates:
        raise ValueError("No researched candidates matched the supplied ticker universe.")

    parsed["candidates"] = candidates
    parsed["research_as_of"] = parsed.get("research_as_of") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    parsed["sources"] = parsed.get("sources") or sdk_sources
    parsed["web_researched"] = bool(parsed.get("sources"))
    parsed["universe_mode"] = "explicit" if explicit else "broad_default"
    parsed["universe_size"] = len(names)
    return parsed
