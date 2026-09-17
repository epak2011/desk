"""Backend-owned thematic idea generation used by every frontend."""

from __future__ import annotations

import json
import os


DEFAULT_UNIVERSE = (
    "AAPL, ABNB, AMZN, ANF, BIRK, BRZE, BROS, CAVA, CELH, CMG, COIN, CROX, "
    "DASH, DECK, DUOL, ELF, ETSY, HIMS, HOOD, LULU, META, NFLX, NKE, ONON, "
    "PINS, PLNT, RBLX, RDDT, RVLV, SE, SHOP, SOFI, SPOT, SQ, TOST, UBER, ULTA, VFC, WING, ZM"
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


def generate(query: str, universe: str, api_key: str) -> dict:
    cleaned = str(query or "").strip()
    if len(cleaned) < 8:
        raise ValueError("Write a little more about the theme you want.")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured for the worker")
    from anthropic import Anthropic
    names = list(dict.fromkeys(x.strip().upper() for x in str(universe or DEFAULT_UNIVERSE).replace("\n", ",").split(",") if x.strip()))[:80]
    prompt = f"""You are an equity idea discovery analyst. The user asks: {cleaned}
Candidate universe: {', '.join(names)}
Find up to 12 US-listed stocks or ETFs that plausibly match. This is not a buy list. Translate the theme into 4-6 criteria, cite concrete evidence, state caveats, and identify facts to verify. Never invent unknown metrics.
Return ONLY JSON with this shape: {{"criteria":["..."],"summary":"...","candidates":[{{"ticker":"TICKER","company":"Name","score":1,"theme_fit":"...","financial_fit":"...","why_it_matters":"...","risks":"...","evidence":["..."],"verify_next":["..."]}}]}}"""
    response = Anthropic(api_key=api_key).messages.create(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6").strip(),
        max_tokens=3500, messages=[{"role": "user", "content": prompt}]
    )
    parsed = _json_object("\n".join(block.text for block in response.content if getattr(block, "text", None)))
    candidates = [c for c in parsed.get("candidates", []) if isinstance(c, dict) and str(c.get("ticker") or "").strip()][:12]
    if not candidates:
        raise ValueError("Claude did not return usable candidates. Try a narrower prompt.")
    parsed["candidates"] = candidates
    return parsed
