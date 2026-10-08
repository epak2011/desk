"""Geographic relevance rules for the Market Regime news slate."""

from __future__ import annotations

import re
from typing import Any, Mapping


_REGIONAL_YAHOO_SOURCES = re.compile(
    r"\byahoo finance\s+(australia|canada|hong kong|india|new zealand|singapore|uk)\b",
    re.I,
)
_US_MARKET_TERMS = re.compile(
    r"\b(united states|u\.s\.|us economy|american economy|federal reserve|the fed|"
    r"treasury(?: yields?)?|wall street|s&p\s*500|nasdaq|dow jones|sec|cftc|"
    r"white house|congress|senate|house of representatives|nonfarm payrolls?|"
    r"pce|cpi|jobs report)\b",
    re.I,
)
_GLOBAL_MARKET_TERMS = re.compile(
    r"\b(global (?:financial )?markets?|world markets?|global stocks?|global bonds?|"
    r"risk assets?|financial contagion|systemic risk|banking crisis|credit crisis|"
    r"bond yields?|currency markets?|foreign exchange|forex|dollar index|oil prices?|"
    r"crude oil|gold prices?|opec\+?|trade war|global trade|tariffs?|sanctions|"
    r"supply disruption|geopolitical risk|bitcoin|ethereum|crypto etf|stablecoin|"
    r"digital asset markets?)\b",
    re.I,
)
_LOCAL_FOREIGN_TERMS = re.compile(
    r"\b(australia|australian|asx|rba|sydney|new zealand|nzx|canada|tsx|"
    r"britain|british|ftse|india|sensex|nifty|singapore|sti|hong kong|hang seng)\b",
    re.I,
)


def is_us_or_global_market_story(story: Mapping[str, Any]) -> bool:
    """Keep U.S. news plus international events with explicit global transmission."""
    title = str(story.get("title") or "").strip()
    source = str(story.get("source") or "").strip()
    category = str(story.get("category") or "").strip().lower()
    if not title or _REGIONAL_YAHOO_SOURCES.search(source):
        return False
    if _US_MARKET_TERMS.search(title) or _GLOBAL_MARKET_TERMS.search(title):
        return True
    if category.startswith("crypto") and re.search(
        r"\b(crypto|bitcoin|ethereum|stablecoin|digital asset|tokenization)\b", title, re.I
    ):
        return True
    if _LOCAL_FOREIGN_TERMS.search(title):
        return False
    return bool(re.search(
        r"\b(inflation|unemployment|payrolls?|gdp|credit spreads?|vix|"
        r"earnings outlook|rate cuts?|rate hikes?)\b",
        title,
        re.I,
    ))
