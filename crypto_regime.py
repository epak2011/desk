"""Deterministic crypto cycle classification for the Market Regime page."""


PHASE_DEFINITIONS = [
    {"number": 1, "phase": "Phase 1", "name": "Accumulation / repair", "icon": "🤔",
     "description": "Post-crash, boring, sentiment terrible. Smart money buys quietly.",
     "historical": "2015, 2018–19, 2022–23",
     "signal": "Deeply below the 200-day average, extreme fear, and flat price action."},
    {"number": 2, "phase": "Phase 2", "name": "Recovery", "icon": "📈",
     "description": "Price climbs back toward old highs. Excitement slowly builds, but retail is not yet fully engaged.",
     "historical": "2016, 2020, 2023–24",
     "signal": "The 200-day average is reclaimed, momentum is building, and sentiment is improving."},
    {"number": 3, "phase": "Phase 3", "name": "Parabolic bull", "icon": "🚀",
     "description": "Euphoria, new highs, intense media attention, and broad speculative participation.",
     "historical": "Q4 2013, Q4 2017, Q4 2021, Q4 2025",
     "signal": "Well above the 200-day average, extreme greed, and parabolic price action."},
    {"number": 4, "phase": "Phase 4", "name": "Bear / transition", "icon": "🐻",
     "description": "A 50–80% drawdown from the peak. Bear rallies trap latecomers while a new base begins to form.",
     "historical": "2014–15, 2018, 2022, 2025–26",
     "signal": "Below the 200-day average, declining momentum, and misleading bear-market rallies."},
]


def classify_cycle(*, btc_vs_200, btc_vs_20, drawdown_cycle, return_90, fear_greed):
    above_200 = btc_vs_200 is not None and btc_vs_200 > 0
    above_20 = btc_vs_20 is not None and btc_vs_20 > 0
    if drawdown_cycle is None:
        return ("Unconfirmed", "Cycle data pending", "Waiting for the full two-year cycle range; no phase is asserted.", 0)
    deep_drawdown = drawdown_cycle is not None and drawdown_cycle <= -30
    parabolic = (
        above_200
        and drawdown_cycle is not None and drawdown_cycle >= -8
        and return_90 is not None and return_90 >= 25
        and (btc_vs_200 or 0) >= 20
        and (btc_vs_20 or 0) >= 8
        and fear_greed is not None and fear_greed >= 65
    )
    if parabolic:
        return ("Phase 3", "Parabolic bull", "Trend is mature; manage greed and trailing risk.", 3)
    if deep_drawdown and (above_200 or above_20):
        return ("Phase 1", "Accumulation / repair", "Post-drawdown base building; trend repair is not a new bull cycle yet.", 1)
    if above_200:
        return ("Phase 2", "Recovery / expansion", "Constructive cycle with room if macro stays supportive.", 2)
    if not above_200 and above_20:
        return ("Phase 4", "Bear-market recovery attempt", "Repair attempt, but 200d is still the key line.", 4)
    return ("Phase 4", "Bear market", "Defense until BTC reclaims the long-term trend.", 4)


def assess(*, btc_vs_200, btc_vs_20, drawdown_cycle, return_90, fear_greed,
           ethbtc_change=None, btc_dominance=None, portfolio_stance="Neutral"):
    """Build the shared, display-ready crypto decision contract for every client."""
    above_200 = btc_vs_200 is not None and btc_vs_200 > 0
    above_20 = btc_vs_20 is not None and btc_vs_20 > 0
    deep_drawdown = drawdown_cycle is not None and drawdown_cycle <= -30
    extreme_fear = fear_greed is not None and fear_greed < 25
    deep_fear = fear_greed is not None and fear_greed < 35
    greed = fear_greed is not None and fear_greed >= 65

    if deep_drawdown and (above_200 or above_20):
        trend = ("Recovery", "Deep drawdown; trend repair, not a new bull cycle.", "caution")
    elif above_200 and above_20:
        trend = ("Bull", "Above the 200-day and 20-day averages.", "positive")
    elif above_200:
        trend = ("Mixed", "Long-term structure is intact, but short-term momentum is fading.", "caution")
    elif above_20:
        trend = ("Reclaim attempt", "The 200-day average is broken, while the 20-day average is holding.", "caution")
    else:
        trend = ("Bear", "Below the 200-day and 20-day averages.", "negative")

    if trend[0] == "Bull" and fear_greed is not None and fear_greed < 40:
        opportunity = ("Yes", "A pro-trend dip; add selectively.", "positive")
    elif trend[0] == "Bull" and fear_greed is not None and fear_greed < 65:
        opportunity = ("Selective", "The trend is healthy, but sentiment offers no discount.", "caution")
    elif trend[0] == "Bull":
        opportunity = ("Wait", "Do not chase greed; let the market cool.", "caution")
    elif trend[0] == "Mixed" and deep_fear:
        opportunity = ("Yes, small", "Counter-trend sizing only while structure repairs.", "caution")
    elif trend[0] == "Bear" and extreme_fear:
        opportunity = ("Yes, small", "Capitulation setup, not confirmation.", "caution")
    else:
        opportunity = ("No", "There is no reliable entry edge yet.", "negative")

    high_dom = btc_dominance is not None and btc_dominance > 60
    low_dom = btc_dominance is not None and btc_dominance < 54
    if ethbtc_change is not None and ethbtc_change > 5 and low_dom:
        positioning = ("Alts", "ETH/BTC is rising and Bitcoin dominance is low.", "positive")
    elif ethbtc_change is not None and ethbtc_change > 5:
        positioning = ("Lean alts", "ETH/BTC is improving, but dominance has not confirmed.", "caution")
    elif ethbtc_change is not None and ethbtc_change < -5 and high_dom:
        positioning = ("BTC only", "ETH/BTC is weak and Bitcoin dominance is high.", "negative")
    elif ethbtc_change is not None and ethbtc_change < -5:
        positioning = ("Lean BTC", "ETH/BTC is weakening.", "caution")
    else:
        positioning = ("Neutral", "There is no clean Bitcoin-versus-altcoin rotation edge.", "neutral")

    cycle = classify_cycle(btc_vs_200=btc_vs_200, btc_vs_20=btc_vs_20,
                           drawdown_cycle=drawdown_cycle, return_90=return_90,
                           fear_greed=fear_greed)
    if above_20 and ((btc_vs_20 or 0) < 2 or greed):
        medium = ("Late", "Short-term risk/reward is less attractive.", "caution")
    elif above_20:
        medium = ("Expansion", "Short-term momentum supports risk.", "positive")
    elif extreme_fear:
        medium = ("Recovery watch", "Watch for a 20-day reclaim after the washout.", "caution")
    else:
        medium = ("Rolling over", "Short-term trend support is deteriorating.", "negative")
    alignment = "Aligned" if trend[0] == "Bull" and medium[0] == "Expansion" else (
        "Mixed" if trend[0] in {"Bull", "Mixed", "Reclaim attempt", "Recovery"} else "Defensive")

    def signed(value):
        return "unavailable" if value is None else f"{value:+.1f}%"
    fg_text = "unavailable" if fear_greed is None else str(int(fear_greed))
    dom_text = "unavailable" if btc_dominance is None else f"{btc_dominance:.1f}%"
    narrative = [
        {"heading": "Trend", "body": f"Bitcoin is {signed(btc_vs_200)} versus its 200-day average and {signed(btc_vs_20)} versus its 20-day average, so the tape is {trend[0].lower()}. The 200-day average remains the key regime line."},
        {"heading": "Opportunity", "body": f"Fear & Greed is {fg_text}. The current add signal is {opportunity[0].lower()}: {opportunity[1]} This affects sizing; it does not override the broader market risk budget."},
        {"heading": "Positioning", "body": f"ETH/BTC is {signed(ethbtc_change)} versus its recent baseline and Bitcoin dominance is {dom_text}. The rotation read is {positioning[0]}: {positioning[1]}"},
        {"heading": "Conviction", "body": f"The cycle read is {cycle[0]} ({cycle[1]}) and the tactical phase is {medium[0]}. Alignment is {alignment.lower()}, so conviction should remain inside the broader {portfolio_stance} portfolio stance."},
    ]
    phases = [{**item, "current": item["number"] == cycle[3]} for item in PHASE_DEFINITIONS]
    return {
        "cycle": {"phase": cycle[0], "phase_number": cycle[3], "label": cycle[1], "detail": cycle[2]},
        "trend": {"label": trend[0], "detail": trend[1], "tone": trend[2]},
        "opportunity": {"label": opportunity[0], "detail": opportunity[1], "tone": opportunity[2]},
        "positioning": {"label": positioning[0], "detail": positioning[1], "tone": positioning[2]},
        "tactical_phase": {"label": medium[0], "detail": medium[1], "tone": medium[2]},
        "alignment": alignment,
        "phases": phases,
        "narrative": narrative,
    }
