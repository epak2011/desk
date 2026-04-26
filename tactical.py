"""Tactical engine — pure functions, no UI.

Ports the JS version and extends the trigger system to return explicit
price levels and volume conditions for Buy / Abort, not just prose.
"""

import pandas as pd


def _sma(series, window):
    return series.rolling(window).mean()


def _ma_slope(prices, period, lookback):
    if len(prices) < period + lookback:
        return 0.0
    ma_today = prices.iloc[-period:].mean()
    ma_past = prices.iloc[-period - lookback:-lookback].mean()
    return (ma_today - ma_past) / lookback


def _rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    delta = prices.diff().iloc[-period:]
    gains = delta.clip(lower=0).sum()
    losses = -delta.clip(upper=0).sum()
    rs = gains / (losses if losses > 0 else 0.0001)
    return 100 - 100 / (1 + rs)


def tech_score(hist):
    prices = hist["Close"]
    price = prices.iloc[-1]
    ma50 = prices.iloc[-50:].mean() if len(prices) >= 50 else price
    ma200 = prices.iloc[-200:].mean() if len(prices) >= 200 else price
    rsi = _rsi(prices)
    avg_vol = hist["Volume"].iloc[-20:].mean()
    vol_ratio = hist["Volume"].iloc[-1] / avg_vol if avg_vol > 0 else 1.0
    slope = _ma_slope(prices, 50, 20)

    score = 5.0
    if price > ma50: score += 1.5
    if price > ma200: score += 1.0
    if 50 <= rsi <= 70: score += 1.0
    if rsi > 75: score -= 0.5
    if vol_ratio > 1.2: score += 1.0
    if vol_ratio < 0.8: score -= 0.5
    if slope > 0: score += 0.5
    return max(0.0, min(10.0, score))


def relative_strength(ticker_hist, bench_hist):
    n = min(len(ticker_hist), len(bench_hist), 60)
    if n < 2:
        return 1.0
    t_ret = ticker_hist["Close"].iloc[-1] / ticker_hist["Close"].iloc[-n]
    b_ret = bench_hist["Close"].iloc[-1] / bench_hist["Close"].iloc[-n]
    return float(t_ret / b_ret)


def structure_quality(hist):
    prices = hist["Close"].iloc[-30:].to_numpy()
    highs, lows = [], []
    for i in range(1, len(prices) - 1):
        if prices[i] > prices[i-1] and prices[i] > prices[i+1]:
            highs.append(prices[i])
        if prices[i] < prices[i-1] and prices[i] < prices[i+1]:
            lows.append(prices[i])
    score = 5.0
    if len(highs) >= 2 and highs[-1] > highs[0]: score += 2
    if len(lows) >= 2 and lows[-1] > lows[0]: score += 2
    if len(highs) >= 2 and highs[-1] < highs[0]: score -= 2
    return max(0.0, min(10.0, score))


def tactical_bias(price, ma50, ma200, ma50_slope, ma200_slope, sq, rs):
    score = 0
    score += 2 if price > ma50 else -2
    score += 2 if price > ma200 else -2
    score += 1 if ma50_slope > 0 else -1
    score += 1 if ma200_slope > 0 else -1
    if sq >= 6: score += 1
    elif sq <= 4: score -= 1
    if rs > 1.0: score += 1
    elif rs < 0.95: score -= 1

    if score >= 4: bias = "bullish"
    elif score <= -4: bias = "bearish"
    else: bias = "neutral"
    return bias, score


def tactical_action(bias, bias_score, setup_score, atr_ok, price, ma50):
    """Return one of: 'enter_now', 'watch', 'hold_off', 'avoid'.

    Hold_off is for the gray zone — directionally bullish-leaning (bias score
    +2 or +3) and structurally not broken (price still above the 50-day) but
    not strong enough to be an actionable Watch. These stocks aren't wrong-side
    trades, they're 'not enough edge yet' situations."""
    if not atr_ok:
        return "avoid"
    if bias == "bullish":
        if setup_score >= 9:
            return "enter_now"
        return "watch"
    # Bias is neutral or bearish. Look for the gray zone: marginal bullishness
    # that's structurally intact but score-light.
    if bias == "neutral" and bias_score >= 2 and price > ma50:
        return "hold_off"
    return "avoid"


def next_trigger(bias, action, price, ma50, high_52w, vol_ratio,
                 range_10d_pct, support, resistance,
                 tech_delta, rs_delta, rs, avg_vol_20d):
    """Return a rich trigger dict with explicit levels and conditions.

    Shape:
      {
        "kind": "reclaim_ma50" | "fast_momentum" | "breakout" |
                "coil_break"   | "pullback"       | "rs_catchup",
        "summary":      "one-line human description",
        "buy_rule":     "concrete condition that triggers entry",
        "abort_rule":   "concrete condition that invalidates the thesis",
        "levels": {"buy_above": float|None, "abort_below": float|None,
                   "volume_min": float|None, ...}
      }
    Returns None when no trigger applies (including enter_now).
    """
    if action == "enter_now" or bias != "bullish":
        return None

    # 1. Below MA50 — waiting for reclaim
    if price < ma50:
        return {
            "kind": "reclaim_ma50",
            "summary": f"reclaim of the 50-day moving average at ${ma50:.2f}",
            "buy_rule": f"Buy if price closes above ${ma50:.2f} (the 50-day moving average).",
            "abort_rule": f"Abandon the setup if price makes a lower low below ${support:.2f}.",
            "levels": {
                "buy_above": round(ma50, 2),
                "abort_below": round(support, 2),
                "volume_min": None,
            },
        }

    near_resistance = (high_52w - price) / high_52w <= 0.03

    # 2. Fast momentum — accelerating into resistance
    if tech_delta >= 1.5 and rs_delta >= 0.03 and near_resistance:
        vol_target = round(avg_vol_20d * 1.2)
        return {
            "kind": "fast_momentum",
            "summary": f"early momentum confirmation above ${high_52w:.2f}",
            "buy_rule": (
                f"Buy if price closes above ${high_52w:.2f} on volume "
                f"≥ {vol_target:,} (1.2× 20-day average)."
            ),
            "abort_rule": (
                f"Abandon the setup if price closes back below ${ma50:.2f} "
                f"(the 50-day moving average)."
            ),
            "levels": {
                "buy_above": round(high_52w, 2),
                "abort_below": round(ma50, 2),
                "volume_min": vol_target,
            },
        }

    # 3. Generic breakout — near 52w high but volume not confirming
    if near_resistance and vol_ratio < 1.0:
        vol_target = round(avg_vol_20d * 1.2)
        return {
            "kind": "breakout",
            "summary": f"breakout above ${high_52w:.2f} on rising volume",
            "buy_rule": (
                f"Buy if price closes above ${high_52w:.2f} on volume "
                f"≥ {vol_target:,} (1.2× 20-day average). A move above "
                f"${high_52w:.2f} on light volume is a fakeout — do not chase."
            ),
            "abort_rule": (
                f"Abandon the setup if price closes back below ${ma50:.2f}."
            ),
            "levels": {
                "buy_above": round(high_52w, 2),
                "abort_below": round(ma50, 2),
                "volume_min": vol_target,
            },
        }

    # 4. Coil — tight 10-day range
    if range_10d_pct < 0.02:
        return {
            "kind": "coil_break",
            "summary": f"break above ${resistance:.2f} or hold ${support:.2f}",
            "buy_rule": (
                f"Buy if price closes above ${resistance:.2f} (top of the "
                f"10-day range) with volume expanding."
            ),
            "abort_rule": (
                f"Abandon the setup if price closes below ${support:.2f} "
                f"(bottom of the 10-day range)."
            ),
            "levels": {
                "buy_above": round(resistance, 2),
                "abort_below": round(support, 2),
                "volume_min": round(avg_vol_20d * 1.1),
            },
        }

    # 5. Extended — wait for pullback
    if (price - ma50) / ma50 > 0.08:
        return {
            "kind": "pullback",
            "summary": f"pullback to the 50-day moving average at ${ma50:.2f}",
            "buy_rule": (
                f"Buy on a pullback to ${ma50:.2f} that holds, with price "
                f"closing back up from the test."
            ),
            "abort_rule": (
                f"Abandon the setup if price closes decisively below ${ma50:.2f}."
            ),
            "levels": {
                "buy_above": round(ma50, 2),
                "abort_below": round(ma50 * 0.98, 2),
                "volume_min": None,
            },
        }

    # 6. RS weakness
    if rs < 1.0:
        return {
            "kind": "rs_catchup",
            "summary": "improvement in relative strength vs the S&P 500",
            "buy_rule": (
                "Wait for relative strength to push above 1.0 and a close at "
                "a new 20-day high before entering."
            ),
            "abort_rule": (
                f"Abandon the setup if relative strength falls further "
                f"(currently {rs:.3f}) or price closes below ${ma50:.2f}."
            ),
            "levels": {
                "buy_above": None,
                "abort_below": round(ma50, 2),
                "volume_min": None,
            },
        }

    return None


def market_regime(bench_hist):
    """Read SPY's regime: bullish (above both MAs, 50>200), bearish (below
    both, 50<200), or neutral. Used as a decision modifier — trading
    longs in a bear market is a different trade than in a bull."""
    if bench_hist is None or len(bench_hist) < 200:
        return "unknown"
    prices = bench_hist["Close"]
    price = float(prices.iloc[-1])
    ma50 = float(prices.iloc[-50:].mean())
    ma200 = float(prices.iloc[-200:].mean())
    if price > ma50 and price > ma200 and ma50 > ma200:
        return "bullish"
    if price < ma50 and price < ma200 and ma50 < ma200:
        return "bearish"
    return "neutral"


def ma_test_history(ticker_hist, ma_period=50, lookback_days=180,
                    test_tolerance=0.02, follow_through_days=20):
    """Find recent times price tested a given moving average, then report
    what happened in the N days after each test. Returns a dict like:
       { 'level': 'MA50', 'tests': 3, 'held': 2, 'avg_bounce_pct': 8.4 }
    or None if there isn't enough history.

    A "test" = price came within `test_tolerance` of the MA from above and
    the MA was the relevant support (price held above the MA at the test).
    A test "held" if price closed higher `follow_through_days` later.
    """
    if ticker_hist is None or len(ticker_hist) < ma_period + lookback_days:
        return None

    prices = ticker_hist["Close"].values
    n = len(prices)

    # Compute the MA series
    ma_series = []
    for i in range(n):
        if i < ma_period - 1:
            ma_series.append(None)
        else:
            window = prices[i - ma_period + 1:i + 1]
            ma_series.append(window.mean())

    tests = []
    last_test_idx = -100  # cooldown so we don't double-count consecutive days
    cooldown = 10

    start = max(ma_period, n - lookback_days)
    end = n - follow_through_days
    for i in range(start, end):
        ma_now = ma_series[i]
        if ma_now is None:
            continue
        price_now = prices[i]
        # Test condition: price within tolerance of the MA, and was above
        # the MA recently (so this is a pullback to support, not a level
        # being broken from below)
        if abs(price_now - ma_now) / ma_now <= test_tolerance:
            # Was price above the MA in the prior 5 days?
            recent = prices[max(0, i - 5):i]
            recent_mas = [m for m in ma_series[max(0, i - 5):i] if m is not None]
            if len(recent_mas) >= 3 and any(p > m for p, m in zip(recent, recent_mas)):
                if i - last_test_idx >= cooldown:
                    tests.append(i)
                    last_test_idx = i

    if not tests:
        return None

    # For each test, did price close higher follow_through_days later?
    held = 0
    bounces = []
    for idx in tests:
        future_idx = min(idx + follow_through_days, n - 1)
        bounce_pct = (prices[future_idx] / prices[idx] - 1) * 100
        bounces.append(bounce_pct)
        if bounce_pct > 0:
            held += 1

    return {
        "level": f"MA{ma_period}",
        "tests": len(tests),
        "held": held,
        "avg_bounce_pct": round(sum(bounces) / len(bounces), 1),
        "lookback_months": round(lookback_days / 21),
    }


def decision_modifiers(t_state, meta, market_reg):
    """Compute decision modifiers — earnings proximity, sector RS, market
    regime. These nudge the conviction up or down on the same nominal
    decision. Returns list of {kind, severity, text} dicts."""
    mods = []

    # Earnings proximity
    days = meta.get("earnings_days") if meta else None
    if days is not None and 0 <= days <= 7:
        if days == 0:
            text = "Earnings today — wait for the print before sizing in."
            severity = "high"
        elif days == 1:
            text = "Earnings in 1 day — wait for the print before sizing in."
            severity = "high"
        elif days == 2:
            text = "Earnings in 2 days — wait for the print before sizing in."
            severity = "high"
        else:
            text = f"Earnings in {days} days — setup may reset after the print."
            severity = "med"
        mods.append({"kind": "earnings", "severity": severity, "text": text})

    # Market regime
    if market_reg == "bearish":
        mods.append({
            "kind": "regime", "severity": "high",
            "text": "Market regime is bearish — long setups have lower base rates here.",
        })
    elif market_reg == "neutral":
        mods.append({
            "kind": "regime", "severity": "low",
            "text": "Market regime is mixed — neither a tailwind nor a headwind.",
        })

    # Relative strength tells us if the stock is leading vs the market
    rs = t_state.get("rs", 1.0)
    rs_delta = t_state.get("rs_delta", 0.0)
    if rs > 1.10 and rs_delta > 0:
        mods.append({
            "kind": "rs", "severity": "low",
            "text": "Strong leadership — outpacing the S&P 500 and the lead is widening.",
        })
    elif rs < 0.90:
        mods.append({
            "kind": "rs", "severity": "med",
            "text": "Significant lag versus the S&P 500 — tape is not supporting this name.",
        })

    return mods


def compute(ticker_hist, bench_hist, atr_threshold=0.015):
    if ticker_hist is None or len(ticker_hist) < 50:
        return None

    prices = ticker_hist["Close"]
    price = float(prices.iloc[-1])
    ma50 = float(prices.iloc[-50:].mean()) if len(prices) >= 50 else price
    ma200 = float(prices.iloc[-200:].mean()) if len(prices) >= 200 else price
    ma50_slope = _ma_slope(prices, 50, 20)
    ma200_slope = _ma_slope(prices, 200, 50)

    rs = relative_strength(ticker_hist, bench_hist)
    sq = structure_quality(ticker_hist)
    setup = tech_score(ticker_hist)

    atr_pct = float(
        ((ticker_hist["High"] - ticker_hist["Low"]) / ticker_hist["Close"])
        .iloc[-20:].mean()
    )

    bias, bias_score = tactical_bias(price, ma50, ma200, ma50_slope, ma200_slope, sq, rs)
    atr_ok = atr_pct >= atr_threshold
    action = tactical_action(bias, bias_score, setup, atr_ok, price, ma50)

    if len(ticker_hist) >= 11:
        past_hist = ticker_hist.iloc[:-10]
        setup_t10 = tech_score(past_hist)
        bench_past = bench_hist.iloc[:len(past_hist)]
        rs_t10 = relative_strength(past_hist, bench_past)
        tech_delta = setup - setup_t10
        rs_delta = rs - rs_t10
    else:
        tech_delta = 0
        rs_delta = 0

    last_252 = prices.iloc[-min(252, len(prices)):]
    high_52w = float(last_252.max())
    low_52w = float(last_252.min())
    # Position as % of 52-week range: 0% = at low, 100% = at high
    rng_52w = high_52w - low_52w
    pct_of_52w_range = float((price - low_52w) / rng_52w * 100) if rng_52w > 0 else 50.0
    rsi14 = _rsi(prices)
    last_10 = prices.iloc[-10:]
    range_10d_pct = float((last_10.max() - last_10.min()) / price)
    support = float(last_10.min())
    resistance = float(last_10.max())
    avg_vol_20d = float(ticker_hist["Volume"].iloc[-20:].mean())
    vol_ratio = float(ticker_hist["Volume"].iloc[-1] / avg_vol_20d) if avg_vol_20d > 0 else 1.0

    trigger = next_trigger(
        bias, action, price, ma50, high_52w, vol_ratio,
        range_10d_pct, support, resistance, tech_delta, rs_delta, rs,
        avg_vol_20d,
    )

    display_bias = None if (action == "avoid" and bias == "bearish") else bias

    # Entry/Stop/Targets anchor differently based on action:
    #   - enter_now: off current price (you're buying at market)
    #   - watch with concrete buy_above: off the trigger level (projected entry)
    #   - watch without buy_above (e.g. rs_catchup, pullback with vague zone): off current price
    #   - avoid: off current price (for reference / override use only)
    anchor = price
    entry_is_projected = False
    if (
        action == "watch"
        and trigger
        and trigger.get("levels", {}).get("buy_above") is not None
    ):
        anchor = float(trigger["levels"]["buy_above"])
        entry_is_projected = True

    entry = anchor
    # Stop anchors to the invalidation level (abort_below) when we have one —
    # that way "stop" and "invalidation" tell the user the same story.
    abort_level = (trigger or {}).get("levels", {}).get("abort_below") if trigger else None
    if abort_level is not None:
        stop = float(abort_level)
    else:
        stop = anchor * (1 - max(atr_pct * 2, 0.03))
    t1 = anchor * (1 + max(atr_pct * 3, 0.05))
    t2 = anchor * (1 + max(atr_pct * 6, 0.10))
    change = float((prices.iloc[-1] / prices.iloc[-2] - 1) * 100) if len(prices) >= 2 else 0.0

    # ── Historical context: how often has price tested the 50-day, and
    #    what happened? Used in technical read + dossier prompt. ──
    ma50_history = ma_test_history(ticker_hist, ma_period=50)

    # ── Market regime from the benchmark (SPY) ──
    market_reg = market_regime(bench_hist)

    return {
        "bias": display_bias,
        "raw_bias": bias,
        "action": action,
        "trigger": trigger,      # now a dict or None
        "bias_score": bias_score,
        "setup_score": setup,
        "atr_pct": atr_pct,
        "atr_ok": atr_ok,
        "price": price,
        "ma50": ma50,
        "ma200": ma200,
        "rs": rs,
        "rs_delta": rs_delta,
        "tech_delta": tech_delta,
        "high_52w": high_52w,
        "low_52w": low_52w,
        "pct_of_52w_range": pct_of_52w_range,
        "rsi14": rsi14,
        "structure_quality": sq,
        "avg_vol_20d": avg_vol_20d,
        "vol_ratio": vol_ratio,
        "ma50_history": ma50_history,    # dict or None
        "market_regime": market_reg,     # 'bullish' | 'bearish' | 'neutral' | 'unknown'
        "entry": entry,
        "entry_is_projected": entry_is_projected,
        "stop": stop,
        "t1": t1,
        "t2": t2,
        "change": change,
    }
