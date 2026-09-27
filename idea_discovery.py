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
    prompt = f"""You are an equity idea-discovery analyst inside Trading Desk.

User request: {cleaned}
{universe_note}: {', '.join(names)}

Research the theme on the web using current, attributable information. Find up
to 12 US-listed stocks or ETFs that plausibly match. This is a research screen,
not a buy list and not an investment recommendation. The thematic score must
measure theme fit only; never infer or fabricate a Trading Desk action.

Requirements:
- Translate the request into 4-6 explicit screening criteria.
- Use current evidence and state caveats and disconfirming facts.
- Distinguish theme fit from financial fit.
- Prefer primary sources and include source URL, title, and publication date
  when known. Never fabricate a citation or metric.
- Mark unknown facts as unknown and put them in verify_next.

Return ONLY JSON with this shape:
{{"criteria":["..."],"summary":"...","research_as_of":"ISO timestamp",
"sources":[{{"url":"https://...","title":"...","published_at":"ISO date or null"}}],
"candidates":[{{"ticker":"TICKER","company":"Name","score":1,
"theme_fit":"...","financial_fit":"...","why_it_matters":"...",
"risks":"...","caveats":["..."],"evidence":["..."],
"verify_next":["..."],"sources":[{{"url":"https://...","title":"...",
"published_at":"ISO date or null"}}]}}]}}"""

    client = Anthropic(api_key=api_key)
    messages = [{"role": "user", "content": prompt}]
    kwargs = {
        "model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6").strip(),
        "max_tokens": 8000,
        "messages": messages,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
    }
    try:
        response = client.messages.create(**kwargs)
    except TypeError:
        kwargs.pop("tools", None)
        response = client.messages.create(**kwargs)

    # Anthropic can pause a long-running server-side web-search loop before it
    # has produced the final text. Resume with the assistant content unchanged,
    # preserving the tool definition, until the turn is actually complete.
    # Without this, a legitimate research run is misreported as "no candidates".
    for _ in range(3):
        if getattr(response, "stop_reason", None) != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})
        kwargs["messages"] = messages
        response = client.messages.create(**kwargs)

    final_text, sdk_sources = _response_text_and_sources(response)
    parsed = _json_object(final_text)
    candidates = [
        row for row in (parsed.get("candidates") or [])
        if isinstance(row, dict) and str(row.get("ticker") or "").strip()
    ][:12]
    if not candidates:
        raise ValueError("Claude did not return usable candidates. Try a narrower prompt.")

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
