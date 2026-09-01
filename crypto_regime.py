"""Deterministic crypto cycle classification for the Market Regime page."""


def classify_cycle(*, btc_vs_200, btc_vs_20, drawdown_cycle, return_90, fear_greed):
    above_200 = btc_vs_200 is not None and btc_vs_200 > 0
    above_20 = btc_vs_20 is not None and btc_vs_20 > 0
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
