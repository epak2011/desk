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


def detect_key_levels(prices, lookback_days=504, min_touches=3,
                      cluster_tolerance=0.02, min_separation_days=5):
    """Scan price history for significant support / resistance levels.

    Algorithm:
      1. Find local extrema (highs and lows) where price reverses
         direction with at least `min_separation_days` between picks
      2. Cluster nearby extrema within `cluster_tolerance` (2% default)
      3. A cluster with ≥ `min_touches` reversals = a key level
      4. Level price = mean of cluster members; importance scales with
         touch count and how recent the most recent touch was

    Returns list of dicts:
      [{"level": float, "touches": int, "kind": "support"|"resistance",
        "last_touch_idx": int, "first_touch_idx": int}]
    Sorted by importance (touches × recency).
    """
    if prices is None or len(prices) < 60:
        return []

    series = prices.iloc[-min(lookback_days, len(prices)):].reset_index(drop=True)
    n = len(series)

    # Find local extrema using a 5-day window: a point is a local high
    # if it's the max of [-5, +5] around it; symmetric for lows.
    window = 5
    extrema = []  # (index, price, kind)
    for i in range(window, n - window):
        slice_ = series.iloc[i - window:i + window + 1]
        center = series.iloc[i]
        if center == slice_.max() and center > series.iloc[i - 1]:
            extrema.append((i, float(center), "high"))
        elif center == slice_.min() and center < series.iloc[i - 1]:
            extrema.append((i, float(center), "low"))

    # Enforce min_separation_days
    pruned = []
    for e in extrema:
        if pruned and (e[0] - pruned[-1][0]) < min_separation_days:
            # Keep the more extreme of the two
            if e[2] == pruned[-1][2]:
                if (e[2] == "high" and e[1] > pruned[-1][1]) or \
                   (e[2] == "low" and e[1] < pruned[-1][1]):
                    pruned[-1] = e
                continue
        pruned.append(e)
    extrema = pruned

    # Cluster nearby extrema within cluster_tolerance
    # Sort by price first to make clustering linear
    by_price = sorted(extrema, key=lambda e: e[1])
    clusters = []
    current_cluster = []
    for e in by_price:
        if not current_cluster:
            current_cluster = [e]
            continue
        cluster_avg = sum(c[1] for c in current_cluster) / len(current_cluster)
        if abs(e[1] - cluster_avg) / cluster_avg <= cluster_tolerance:
            current_cluster.append(e)
        else:
            clusters.append(current_cluster)
            current_cluster = [e]
    if current_cluster:
        clusters.append(current_cluster)

    # Filter to meaningful clusters
    levels = []
    for cluster in clusters:
        if len(cluster) < min_touches:
            continue
        # A cluster can contain both highs and lows (S/R flip). Tag by
        # majority — but note that flipped levels (resistance becoming
        # support) are particularly valuable.
        highs = sum(1 for c in cluster if c[2] == "high")
        lows = sum(1 for c in cluster if c[2] == "low")
        kind = "resistance" if highs >= lows else "support"

        level = sum(c[1] for c in cluster) / len(cluster)
        last_idx = max(c[0] for c in cluster)
        first_idx = min(c[0] for c in cluster)
        levels.append({
            "level": round(level, 2),
            "touches": len(cluster),
            "kind": kind,
            "last_touch_idx": last_idx,
            "first_touch_idx": first_idx,
            "is_flip": highs > 0 and lows > 0,  # tested both ways
        })

    # Sort by importance: touches × recency_weight
    # recency_weight = (last_touch_idx / n) — more recent = higher weight
    for lv in levels:
        recency = lv["last_touch_idx"] / n
        lv["_score"] = lv["touches"] * (0.5 + 0.5 * recency)
        # Flip levels (tested both as S and R) get a boost — they're
        # the strongest setups
        if lv["is_flip"]:
            lv["_score"] *= 1.4

    levels.sort(key=lambda lv: -lv["_score"])

    # Collapse nearly identical levels after scoring. Without this pass,
    # volatile names can show several clusters that are only 1-2% apart,
    # which reads as noise instead of a usable support/resistance map.
    min_gap = max(cluster_tolerance * 1.75, 0.035)
    cleaned = []
    for lv in levels:
        if any(abs(lv["level"] - kept["level"]) / kept["level"] < min_gap for kept in cleaned):
            continue
        cleaned.append(lv)
    return cleaned[:12]


def _rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    delta = prices.diff().iloc[-period:]
    gains = delta.clip(lower=0).sum()
    losses = -delta.clip(upper=0).sum()
    rs = gains / (losses if losses > 0 else 0.0001)
    return 100 - 100 / (1 + rs)


def _nearest_level(levels, price, *, above=False, max_distance=0.08):
    """Return nearest detected key level above/below price within a band."""
    if not levels or price <= 0:
        return None
    candidates = []
    for level in levels:
        try:
            lv = float(level.get("level"))
        except (TypeError, ValueError):
            continue
        if lv <= 0:
            continue
        if above:
            if lv <= price:
                continue
            dist = (lv - price) / price
        else:
            if lv > price * 1.02:
                continue
            dist = abs(price - lv) / price
        if dist <= max_distance:
            candidates.append((dist, level))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _structural_targets_above(entry, atr_pct, *, key_levels=None,
                              resistance=None, swing_high_60d=None,
                              prior_high_52w=None, high_52w=None):
    """Return meaningful technical objectives above entry, nearest first.

    ATR is deliberately not used to generate these levels. It only filters
    tiny/noisy objectives that sit too close to entry to be useful.
    """
    if entry <= 0:
        return []

    key_levels = key_levels or []
    min_gap_pct = max(0.01, min(float(atr_pct or 0) * 0.5, 0.03))
    min_level = entry * (1 + min_gap_pct)
    raw = []

    def add(value, label, rank):
        try:
            level = float(value)
        except (TypeError, ValueError):
            return
        if level <= min_level:
            return
        raw.append({
            "level": level,
            "label": label,
            "rank": rank,
            "distance": (level - entry) / entry,
        })

    for level in key_levels:
        try:
            value = float(level.get("level"))
        except (TypeError, ValueError, AttributeError):
            continue
        touches = int(level.get("touches") or 0)
        kind = str(level.get("kind") or "level")
        if level.get("is_flip"):
            label = "support/resistance flip"
        elif kind == "resistance":
            label = f"{touches}x tested resistance" if touches else "detected resistance"
        else:
            label = f"{touches}x tested level" if touches else "detected technical level"
        add(value, label, 1)

    add(resistance, "10-day range high", 2)
    add(swing_high_60d, "60-day swing high", 3)
    add(prior_high_52w, "prior 52-week high", 4)
    add(high_52w, "52-week high", 5)

    deduped = []
    for item in sorted(raw, key=lambda x: (x["level"], x["rank"])):
        if deduped and abs(item["level"] - deduped[-1]["level"]) / deduped[-1]["level"] < 0.01:
            # Keep the stronger/more structural label for near-identical levels.
            if item["rank"] < deduped[-1]["rank"]:
                deduped[-1] = item
            continue
        deduped.append(item)

    return sorted(deduped, key=lambda x: (x["distance"], x["rank"]))


def _recent_local_lows(prices, lookback=126):
    if prices is None or len(prices) < 15:
        return []
    series = prices.iloc[-min(lookback, len(prices)):].reset_index(drop=True)
    lows = []
    for i in range(3, len(series) - 3):
        center = float(series.iloc[i])
        window = series.iloc[i - 3:i + 4]
        if center == float(window.min()) and center < float(series.iloc[i - 1]):
            lows.append((i, center))
    return lows


def tech_score_breakdown(hist):
    """Score technical opportunity quality today.

    The score intentionally combines two different ideas:
      1. Trend / momentum: whether the tape is already strong
      2. Market structure: whether price is sitting at an attractive,
         rule-defined location such as multi-touch support, a breakout
         retest, a failed breakdown/reclaim, or a compressed base

    The output remains deterministic and fully explainable. No language
    model judgement is used here.
    """
    prices = hist["Close"]
    price = float(prices.iloc[-1])
    ma50 = prices.iloc[-50:].mean() if len(prices) >= 50 else price
    ma200 = prices.iloc[-200:].mean() if len(prices) >= 200 else price
    rsi = _rsi(prices)
    avg_vol = hist["Volume"].iloc[-20:].mean()
    vol_ratio = hist["Volume"].iloc[-1] / avg_vol if avg_vol > 0 else 1.0
    ma50_slope = _ma_slope(prices, 50, 20)
    ma200_slope = _ma_slope(prices, 200, 50)
    key_levels = detect_key_levels(prices)
    support_level = _nearest_level(key_levels, price, above=False, max_distance=0.06)
    resistance_level = _nearest_level(key_levels, price, above=True, max_distance=0.18)

    def _component(label, points, max_points, note):
        return {
            "label": label,
            "points": float(points),
            "max_points": float(max_points),
            "note": note,
        }

    trend_50_points = 0.8 if price > ma50 else (-0.6 if price < ma50 * 0.97 else 0.0)
    trend_200_points = 0.7 if price > ma200 else (-0.8 if price < ma200 * 0.95 else 0.0)
    slope_points = 0.0
    if ma50_slope > 0 and ma200_slope >= 0:
        slope_points = 0.5
    elif ma50_slope > 0 or ma200_slope > 0:
        slope_points = 0.25
    elif ma50_slope < 0 and ma200_slope < 0:
        slope_points = -0.5

    rsi_points = 0.0
    if 45 <= rsi <= 70:
        rsi_points = 0.6
    elif rsi > 75:
        rsi_points = -0.4
    elif rsi < 35:
        rsi_points = -0.2

    volume_points = 0.0
    if vol_ratio > 1.2:
        volume_points = 0.6
    elif vol_ratio < 0.6:
        volume_points = -0.3

    components = [
        _component("Baseline", 5.0, 5.0, "neutral opportunity starting point"),
        _component("50d trend", trend_50_points, 0.8, f"price is {(price / ma50 - 1) * 100:+.1f}% vs 50-day MA"),
        _component("200d trend", trend_200_points, 0.7, f"price is {(price / ma200 - 1) * 100:+.1f}% vs 200-day MA"),
        _component("MA slope", slope_points, 0.5, "50-day and 200-day slope direction"),
        _component("RSI setup", rsi_points, 0.6, f"RSI {rsi:.0f}; ideal opportunity zone is roughly 45-70"),
        _component("Volume", volume_points, 0.6, f"{vol_ratio:.2f}x 20-day average"),
    ]

    structure_points = 0.0
    structure_notes = []

    if support_level:
        level = float(support_level["level"])
        dist = abs(price - level) / price
        touches = int(support_level.get("touches") or 0)
        is_flip = bool(support_level.get("is_flip"))
        points = 1.4 if is_flip else 1.0
        if touches >= 5:
            points += 0.3
        if dist <= 0.025:
            points += 0.3
        structure_points += min(points, 2.0)
        label = "former resistance/support flip" if is_flip else f"{touches}x tested support"
        structure_notes.append(f"{label} near ${level:.2f}")

    prior_breakout_level = None
    if len(prices) >= 180:
        pre_recent = prices.iloc[:-20]
        if len(pre_recent) >= 80:
            prior_breakout_level = float(pre_recent.iloc[-252:].max())
            recent_high = float(prices.iloc[-63:].max())
            retest_band = prior_breakout_level > 0 and abs(price - prior_breakout_level) / prior_breakout_level <= 0.06
            has_broken_out = prior_breakout_level > 0 and recent_high >= prior_breakout_level * 1.03
            still_holding = prior_breakout_level > 0 and price >= prior_breakout_level * 0.97
            if retest_band and has_broken_out and still_holding:
                structure_points += 1.0
                structure_notes.append(f"prior 52w breakout retest near ${prior_breakout_level:.2f}")

    if support_level:
        level = float(support_level["level"])
        recent_low_20 = float(prices.iloc[-20:].min()) if len(prices) >= 20 else price
        ma20 = prices.iloc[-20:].mean() if len(prices) >= 20 else price
        reclaim_ok = (
            recent_low_20 <= level * 0.98 and
            price >= level * 1.01 and
            ma20 > 0 and
            price > ma20
        )
        if reclaim_ok:
            structure_points += 1.0
            structure_notes.append(f"failed breakdown/reclaim above ${level:.2f}")

    if len(prices) >= 60:
        last_20 = prices.iloc[-20:]
        last_60 = prices.iloc[-60:]
        range_20 = (float(last_20.max()) - float(last_20.min())) / price if price > 0 else 0
        range_60 = (float(last_60.max()) - float(last_60.min())) / price if price > 0 else 0
        in_upper_half = price >= float(last_20.min()) + (float(last_20.max()) - float(last_20.min())) * 0.55
        if range_20 <= 0.12 and range_60 <= 0.30 and in_upper_half:
            structure_points += 0.7
            structure_notes.append(f"tight base/compression ({range_20 * 100:.1f}% 20d range)")

    recent_lows = _recent_local_lows(prices)
    if len(recent_lows) >= 2:
        prev_low = recent_lows[-2][1]
        latest_low = recent_lows[-1][1]
        high_52w = float(prices.iloc[-min(252, len(prices)):].max())
        drawdown = (price / high_52w - 1) if high_52w > 0 else 0
        if latest_low >= prev_low * 1.03 and drawdown <= -0.15 and price >= latest_low * 1.03:
            structure_points += 0.7
            structure_notes.append(f"higher low after correction (${latest_low:.2f} vs ${prev_low:.2f})")

    asymmetry_points = 0.0
    asymmetry_note = "no nearby rule-defined support/resistance pair"
    if support_level and resistance_level:
        sup = float(support_level["level"])
        res = float(resistance_level["level"])
        risk = price - sup
        reward = res - price
        if risk > 0 and reward > 0:
            rr = reward / risk
            asymmetry_note = f"nearest support ${sup:.2f}, resistance ${res:.2f}; approx {rr:.2f}:1"
            if rr >= 2.0:
                asymmetry_points = 0.75
            elif rr >= 1.5:
                asymmetry_points = 0.4
            elif rr < 1.0:
                asymmetry_points = -0.5

    # Broken-chart guardrail: location credit requires proof. If a name is
    # deeply below the 200d, lagging, and not reclaiming any detected level,
    # cap the positive structure credit so the model does not reward blind
    # falling-knife attempts.
    below_200_deep = ma200 > 0 and price < ma200 * 0.90
    no_reclaim_note = not any("reclaim" in note for note in structure_notes)
    if below_200_deep and no_reclaim_note:
        structure_points = min(structure_points, 0.7)
        if structure_notes:
            structure_notes.append("structure credit capped until a reclaim confirms")

    structure_points = min(structure_points, 3.0)
    structure_note = "; ".join(structure_notes) if structure_notes else "no objective support/retest/reclaim setup detected"
    components.append(_component("Market structure", structure_points, 3.0, structure_note))
    components.append(_component("Location / asymmetry", asymmetry_points, 0.75, asymmetry_note))

    raw_score = sum(component["points"] for component in components)
    score = max(0.0, min(10.0, raw_score))
    return {
        "score": float(score),
        "raw_score": float(raw_score),
        "components": components,
        "method": (
            "Technical opportunity score: 5.0 baseline plus trend/momentum inputs "
            "and deterministic market-structure/location bonuses; capped at 0-10."
        ),
    }


def tech_score(hist):
    return tech_score_breakdown(hist)["score"]


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


def _ma_score(price, ma, *, tight=False):
    """Gradient distance score for price vs a moving average.

    Replaces the old binary ±2 with a graduated scale that distinguishes
    'just below' (transition) from 'deeply below' (broken).

    For ma200 (`tight=False`), the bands are:
      > +10%  → +2  strong trend
      0..+10% → +1  above, stable
      -5..0%  →  0  neutral / transition zone
      -15..-5%→ -1  weakening
      < -15%  → -2  broken

    For ma50 (`tight=True`), the bands are tighter to reflect that the
    50-day moves more with price:
      > +5%   → +2
      0..+5%  → +1
      -3..0%  →  0  transition
      -8..-3% → -1
      < -8%   → -2
    """
    if ma <= 0:
        return 0
    pct = (price - ma) / ma
    if tight:
        if pct >  0.05:  return  2
        if pct >  0.0:   return  1
        if pct > -0.03:  return  0
        if pct > -0.08:  return -1
        return -2
    # ma200
    if pct >  0.10:  return  2
    if pct >  0.0:   return  1
    if pct > -0.05:  return  0
    if pct > -0.15:  return -1
    return -2


def tactical_bias(price, ma50, ma200, ma50_slope, ma200_slope, sq, rs):
    """Compute directional bias with GRADIENT MA scoring.

    Score range stays ±8 nominal; bullish/bearish thresholds unchanged at
    ±4. The change is in the middle: names that were previously slammed
    to a strong negative just for being slightly below the 200d now land
    in the neutral / transition zone, which lets `tactical_action`
    correctly classify them as Hold off (recovering) instead of Avoid.
    """
    score = 0
    score += _ma_score(price, ma50, tight=True)    # was ±2 binary
    score += _ma_score(price, ma200, tight=False)  # was ±2 binary
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


def classify_state(price, ma50, ma200, rs, rs_delta, tech_delta):
    """Classify structural state BEFORE tactical_action runs.

    Returns one of: "TRENDING", "TRANSITION", "BROKEN".

    Design principles:
    - TRANSITION is broad but requires confirmation (momentum or RS improvement)
    - BROKEN is strict (ALL conditions must be true)
    - When ambiguous, bias toward TRANSITION (Hold off) over BROKEN (Avoid)

    See SPEC dated 2026-04-28 for full logic. The state is exposed in the
    UI alongside the action so the user always sees why a name landed
    where it did.
    """
    # Guard against zero/negative MAs from bad data
    if ma50 <= 0 or ma200 <= 0:
        return "TRENDING"

    # ─── TRANSITION conditions ────────────────────────────────────────
    # (a) Partial recovery WITH confirmation:
    #     above MA50 AND below MA200 AND (tech_delta > 0 OR rs_delta >= 0.02)
    #     The confirmation requirement is critical — without it, weak
    #     bounces would get protected as TRANSITION.
    cond_a = (
        price > ma50 and
        price < ma200 and
        (tech_delta > 0 or rs_delta >= 0.02)
    )

    # (b) Near-MA200 zone:
    #     Price -20% to -5% of MA200. Captures weakening but not fully
    #     broken structure regardless of where MA50 sits.
    pct_vs_ma200 = (price - ma200) / ma200
    cond_b = -0.20 <= pct_vs_ma200 <= -0.05

    # (c) RS improving from weakness:
    #     RS < 1.0 AND rs_delta >= 0.02
    cond_c = rs < 1.0 and rs_delta >= 0.02

    if cond_a or cond_b or cond_c:
        return "TRANSITION"

    # ─── BROKEN conditions (ALL must be true) ──────────────────────────
    cond_broken = (
        price < ma200 * 0.85 and    # >15% below MA200
        rs < 0.9 and                 # weak RS
        rs_delta < 0.01 and          # not improving
        tech_delta <= 0              # no momentum recovery
    )

    if cond_broken:
        return "BROKEN"

    # ─── Default ───────────────────────────────────────────────────────
    return "TRENDING"


def classify_accumulation(price, high_52w, low_52w, ma20, ret_5d,
                          rs_delta, made_new_30d_low_recently):
    """Classify whether a name is in an Accumulation Watch setup.

    Returns True if ALL spec conditions met (per 2026-04-28 Accumulation
    Watch spec), regardless of quality. Quality gating happens later in
    apply_accumulation_override() because it requires the dossier call.

    Conditions (ALL must be true):
      - Drawdown from 52w high >= 35%
      - Price within 20% of 52w low
      - Stabilization: rs_delta >= 0.02 OR no new 30d low in last 5 sessions
      - No active breakdown: price > ma20 OR 5-day return > 0
    """
    # Guard against degenerate inputs
    if high_52w <= 0 or low_52w <= 0 or price <= 0:
        return False

    # Drawdown from 52-week high
    drawdown = (price - high_52w) / high_52w   # negative number
    cond_deep_drawdown = drawdown <= -0.35

    # Within 20% of 52-week low
    pct_above_low = (price - low_52w) / low_52w if low_52w > 0 else 0
    cond_near_low = pct_above_low <= 0.20

    # Stabilization
    cond_stabilizing = (rs_delta >= 0.02) or (not made_new_30d_low_recently)

    # No active breakdown
    cond_not_breaking_down = (price > ma20) or (ret_5d > 0)

    return (cond_deep_drawdown and cond_near_low
            and cond_stabilizing and cond_not_breaking_down)


def apply_accumulation_override(action, is_accumulation_eligible, quality_tier):
    """Upgrade 'avoid' to 'accumulate' if accumulation criteria + quality A/B.

    Called from the app layer after the dossier (and quality tier) is
    fetched. Only overrides Avoid — never overrides hold_off, watch, or
    enter_now (those are already actionable; accumulation isn't a
    promotion path from those states).

    Quality gate is HARD: only "A" or "B" tiers are eligible. Speculative
    and Avoid quality tiers do NOT get the accumulation upgrade — that's
    the value-trap protection.
    """
    if action != "avoid":
        return action
    if not is_accumulation_eligible:
        return action
    if quality_tier not in ("A", "B"):
        return action
    return "accumulate"



def tactical_action(bias, bias_score, setup_score, atr_ok, price, ma50,
                    ma200=None, ma100=None, ma20=None,
                    rs=1.0, rs_delta=0.0, tech_delta=0,
                    vol_ratio=1.0,
                    state="TRENDING", is_accumulation_eligible=False):
    """Return one of: 'enter_now', 'watch', 'hold_off', 'avoid'.

    Per the 2026-04-28 strict-Avoid spec PLUS the user's "decision
    precedence" framework (Tier 1 hard gates → Tier 2 disqualifiers →
    Tier 3 modifiers), the rules are now applied in explicit order:

    TIER 1 — HARD GATES (always evaluated first; Tier 1 fires regardless
    of quality, regime, or trend alignment):
      1.0  ATR fail → HOLD OFF (not enough range for this tactical system)

    TIER 2 — DISQUALIFIERS:
      2.1  BROKEN structure WITHOUT stabilization → AVOID
           Five strict conditions, all required:
             - price < ma200
             - rs < 0.9
             - rs_delta < 0.01
             - tech_delta <= 0
             - NOT accumulation-eligible
      2.2  Extension disqualifier → DOWNGRADE bullish-Enter to bullish-
           Watch when price is materially extended above MA50 AND MA100
           (when MA100 is available). Forces a base before entry.

    TIER 3 — MODIFIERS (quality, regime, trend) are applied through:
      - bias and setup_score (computed upstream with quality/regime
        signal embedded via RS, structure_quality, etc.)
      - is_accumulation_eligible flag (which itself depends on Quality
        A/B at the app layer, but we just see the boolean here)

    The ENTER / WATCH / HOLD OFF / AVOID outputs:
      - ENTER:    bullish bias + setup_score >= 8.5 + NOT extended
      - WATCH:    bullish bias + setup_score < 8.5 (or extended)
      - AVOID:    Tier 2.1 fires
      - HOLD OFF: universal fall-through (every other case)

    Key principle: Tier 1 / 2 fires regardless of quality. Quality
    cannot rescue a structurally broken name. Low ATR is not treated as
    bad business quality; it simply means the setup is not active enough
    for this tactical system today. Quality CAN earn an upgrade from avoid to accumulate,
    but that's applied as a separate override at the app layer after
    Quality is known from the dossier.
    """
    # ─── TIER 1: HARD GATES ─────────────────────────────────────────
    if not atr_ok:
        return "hold_off"

    # ─── TIER 2.1: STRUCTURAL BREAKDOWN ────────────────────────────
    # Strict Avoid: ALL five conditions required. Reserved for genuinely
    # broken names with no stabilization signs.
    is_broken = (
        ma200 is not None and
        price < ma200 and
        rs < 0.9 and
        rs_delta < 0.01 and
        tech_delta <= 0 and
        not is_accumulation_eligible
    )
    if is_broken:
        return "avoid"

    # ─── ENTER / WATCH (bullish path) ──────────────────────────────
    if bias == "bullish":
        # ─── TIER 2.2: EXTENSION DISQUALIFIER ──────────────────────
        # If MA100 is available, "extended above BOTH MA50 AND MA100"
        # is the trigger. Without MA100, fall back to a stricter MA50-
        # only check (since we have no second MA to confirm extension).
        ext_ma50 = (price - ma50) / ma50 if ma50 > 0 else 0
        if ma100 is not None and ma100 > 0:
            ext_ma100 = (price - ma100) / ma100
            # "Significantly extended" = >12% above MA50 AND >8% above MA100.
            # The dual-MA check is more discriminating than MA50 alone —
            # a name +15% above MA50 but only +6% above MA100 is in a
            # normal pullback-to-MA100 zone, not chasing.
            extended = ext_ma50 > 0.12 and ext_ma100 > 0.08
        else:
            # Without MA100, use MA50 alone with a tighter band
            extended = ext_ma50 > 0.15

        # Momentum exception: strong leaders can remain actionable even
        # when extended, but only if the tape is still proving sponsorship.
        # This prevents old "too extended" logic from missing legitimate
        # continuation names while still blocking ordinary chase entries.
        momentum_exception = (
            extended and
            setup_score >= 9.0 and
            rs >= 1.10 and
            (
                rs_delta >= 0.01 or
                tech_delta >= 1.0 or
                vol_ratio >= 1.2
            )
        )

        if setup_score >= 8.5 and (not extended or momentum_exception):
            return "enter_now"
        # Either setup_score < 8.5, OR extended → watch (with a
        # pullback-style trigger generated by next_trigger)
        return "watch"

    # ─── HOLD OFF (universal fall-through) ─────────────────────────
    # Any non-bullish, non-broken case lands here. Includes:
    #   - Pullbacks in uptrends
    #   - Leadership names below MA200 with strong RS
    #   - Transitioning structures
    #   - Ambiguous setups with no clear edge
    return "hold_off"


def historical_support_trigger(price, ma50, atr_pct, support_levels,
                               approach_tolerance=0.04,
                               wick_tolerance=0.003):
    """Generate a Watch trigger if price is approaching a meaningful
    historical support level.

    Args:
      price: current price (close)
      ma50: 50-day MA (for context)
      atr_pct: average true range as % (for sizing the abort buffer)
      support_levels: list of dicts with at minimum {"level": float,
        "touches": int, "is_flip": bool, "_score": float, "source":
        "auto"|"manual"}.
      approach_tolerance: how close (above) does price need to be? Default
        4% — close enough that the test is imminent, far enough that we
        get notice before the bounce.
      wick_tolerance: small allowance for closes that print just below
        the level (default 0.3%) — handles intraday wicks below support.
        NOT a "broke through" allowance.

    Returns a trigger dict (same shape as next_trigger) or None.

    Fires only when the support level is being TESTED FROM ABOVE:
      - 0 ≤ pct_above ≤ approach_tolerance, OR
      - just barely below within wick_tolerance (intraday wick scenarios)

    Does NOT fire when price has closed meaningfully below the level —
    that's a BROKEN support, not an approach. In that case the level
    becomes resistance and the right action is Hold off until reclaim.
    """
    if not support_levels or price <= 0:
        return None

    candidates = []
    for s in support_levels:
        level = s.get("level", 0)
        if level <= 0:
            continue
        # pct_above > 0: price above level (approaching from above) ✓
        # pct_above ≈ 0: price at level                              ✓
        # pct_above < -wick_tolerance: price BELOW — support broken  ✗
        pct_above = (price - level) / level
        if -wick_tolerance <= pct_above <= approach_tolerance:
            candidates.append((s, pct_above))

    if not candidates:
        return None

    # Pick the strongest candidate by score; tie-break by closest to price
    candidates.sort(key=lambda x: (-x[0].get("_score", 0), abs(x[1])))
    best, pct_above = candidates[0]
    level = best["level"]
    touches = best.get("touches", 0)
    is_flip = best.get("is_flip", False)
    source = best.get("source", "auto")

    support_status = "testing"
    # If price is already comfortably above the level, stop presenting this
    # as "approaching" support. ATR can be very large in volatile names, so
    # do not let it keep stale support-test copy alive for days.
    if pct_above >= 0.015:
        support_status = "held_above"
    elif pct_above < 0:
        support_status = "wick_test"

    # Build the trigger dict
    # Buy rule: hold of the level on volume confirmation
    # Abort: clean break below the level (allow a small buffer)
    abort_buffer = max(0.5 * atr_pct * level, 0.01 * level)
    abort_below = round(level - abort_buffer, 2)

    if source == "manual":
        descriptor = "user-marked support"
    elif is_flip:
        descriptor = "support level (former resistance, now flipped)"
    else:
        descriptor = f"support level ({touches}× tested)"

    return {
        "kind": "historical_support_test",
        "summary": f"hold of ${level:.2f} {descriptor}",
        "buy_rule": (
            f"Buy only after ${level:.2f} proves support — either a tap-and-bounce "
            f"with confirming volume, or continued closes above the level."
        ),
        "abort_rule": (
            f"Abandon if price closes below ${abort_below:.2f} on volume — "
            f"a clean break of {descriptor} invalidates the thesis."
        ),
        "levels": {
            "buy_above": round(level, 2),
            "abort_below": abort_below,
            "volume_min": None,
        },
        "support_meta": {
            "touches": touches,
            "is_flip": is_flip,
            "source": source,
            "status": support_status,
            "pct_above_currently": round(pct_above * 100, 2),
        },
    }



def next_trigger(bias, action, price, ma50, high_52w, vol_ratio,
                 range_10d_pct, support, resistance,
                 tech_delta, rs_delta, rs, avg_vol_20d,
                 ma20=None, recent_pullback_anchor=None,
                 prior_high_52w=None):
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

    Pullback branch is now extension-aware: the target depends on how far
    above ma50 price is sitting. A name +25% above ma50 is not going to
    pull back to ma50 in any actionable timeframe — that's a -20% move,
    by which point the trade is no longer the trade we're entering. The
    new logic uses ma20 or a recent local low as the proximate target
    when the stock is materially extended.

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

    breakout_pivot = prior_high_52w if prior_high_52w and prior_high_52w > 0 else high_52w
    near_resistance = breakout_pivot and (breakout_pivot - price) / breakout_pivot <= 0.03

    # 2. Fast momentum — accelerating into resistance
    if tech_delta >= 1.5 and rs_delta >= 0.03 and near_resistance:
        vol_target = round(avg_vol_20d * 1.2)
        return {
            "kind": "fast_momentum",
            "summary": f"early momentum confirmation above ${breakout_pivot:.2f}",
            "buy_rule": (
                f"Buy if price closes above ${breakout_pivot:.2f} on volume "
                f"≥ {vol_target:,} (1.2× 20-day average)."
            ),
            "abort_rule": (
                f"Abandon the setup if price closes back below ${ma50:.2f} "
                f"(the 50-day moving average)."
            ),
            "levels": {
                "buy_above": round(breakout_pivot, 2),
                "abort_below": round(ma50, 2),
                "volume_min": vol_target,
            },
        }

    # 3. Generic breakout — near 52w high but volume not confirming
    if near_resistance and vol_ratio < 1.0:
        vol_target = round(avg_vol_20d * 1.2)
        return {
            "kind": "breakout",
            "summary": f"breakout above ${breakout_pivot:.2f} on rising volume",
            "buy_rule": (
                f"Buy if price closes above ${breakout_pivot:.2f} on volume "
                f"≥ {vol_target:,} (1.2× 20-day average). A move above "
                f"${breakout_pivot:.2f} on light volume is a fakeout — do not chase."
            ),
            "abort_rule": (
                f"Abandon the setup if price closes back below ${ma50:.2f}."
            ),
            "levels": {
                "buy_above": round(breakout_pivot, 2),
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

    # 5. Extended — wait for pullback. Branch by how extended.
    extension_pct = (price - ma50) / ma50
    if extension_pct > 0.08:
        # Build candidate pullback targets, nearest first.
        # Each candidate = (target, abort, label, summary_descriptor)
        candidates = []

        # Recent local low (last ~20 sessions): the most actionable target
        # for a name in a strong uptrend. This is the "first dip" buyers
        # are watching for.
        if recent_pullback_anchor is not None and price > recent_pullback_anchor > 0:
            pull_pct = (price - recent_pullback_anchor) / price
            # Only use it if it's meaningfully below price (≥3%) but not
            # so deep that it's basically the ma50 anyway (≥40% of the
            # gap to ma50)
            if 0.03 <= pull_pct <= 0.15:
                candidates.append((
                    recent_pullback_anchor,
                    round(recent_pullback_anchor * 0.97, 2),
                    "recent support level",
                    f"pullback to ${recent_pullback_anchor:.2f} "
                    f"(recent support, -{pull_pct*100:.1f}% from here)",
                ))

        # ma20: usually 3-8% below price in trending names; the textbook
        # "first pullback" target.
        if ma20 is not None and ma20 > 0 and ma20 < price:
            pull_pct = (price - ma20) / price
            if 0.02 <= pull_pct <= 0.12:
                candidates.append((
                    ma20,
                    round(ma20 * 0.97, 2),
                    "20-day moving average",
                    f"pullback to the 20-day moving average at ${ma20:.2f} "
                    f"(-{pull_pct*100:.1f}% from here)",
                ))

        # ma50: only useful when extension is moderate (<15%). Above
        # that, ma50 is too far to be actionable.
        if extension_pct < 0.15:
            ma50_pct = (price - ma50) / price
            candidates.append((
                ma50,
                round(ma50 * 0.97, 2),
                "50-day moving average",
                f"pullback to the 50-day moving average at ${ma50:.2f} "
                f"(-{ma50_pct*100:.1f}% from here)",
            ))

        # Pick the nearest target above 3% pullback. If we have nothing
        # workable (rare — happens when stock is very extended AND no
        # recent low AND ma20 is very close), fall through to a generic
        # "wait for any meaningful pullback" message keyed off ma20.
        if candidates:
            # Sort by distance — nearest target wins (smallest pullback)
            candidates.sort(key=lambda c: -c[0])  # higher target = smaller pullback
            target, abort, label, summary = candidates[0]
            return {
                "kind": "pullback",
                "summary": summary,
                "buy_rule": (
                    f"Buy on a pullback to ${target:.2f} ({label}) that holds "
                    f"with price closing back up from the test."
                ),
                "abort_rule": (
                    f"Abandon the setup if price closes decisively below "
                    f"${abort:.2f}."
                ),
                "levels": {
                    "buy_above": round(target, 2),
                    "abort_below": abort,
                    "volume_min": None,
                },
            }
        else:
            # Fallback — name is so extended that nothing reasonable pulls
            # back to. Suggest watching for any 3-5% dip as the entry.
            target_band = round(price * 0.96, 2)
            return {
                "kind": "pullback",
                "summary": (
                    f"a 3-5% pullback (around ${target_band:.2f}) that "
                    f"holds — the stock is too extended for a clean MA test"
                ),
                "buy_rule": (
                    f"Buy on a 3-5% pullback that holds — first sign of "
                    f"meaningful selling pressure that the trend absorbs."
                ),
                "abort_rule": (
                    f"Abandon the setup if price closes below ${ma50:.2f} "
                    f"(the 50-day moving average) — that's a real character change."
                ),
                "levels": {
                    "buy_above": target_band,
                    "abort_below": round(ma50, 2),
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


def extension_momentum_warning(t_state):
    """Describe unusually stretched momentum without changing the action.

    The warning deliberately requires both price extension and a hot RSI so a
    healthy trend is not mislabeled simply for trading above a moving average.
    Keeping this separate from ``tactical_action`` lets calibration determine
    whether it eventually deserves to become a hard entry gate.
    """
    try:
        price = float(t_state.get("price"))
        ma20 = float(t_state.get("ma20"))
        ma50 = float(t_state.get("ma50"))
        rsi = float(t_state.get("rsi14"))
    except (TypeError, ValueError):
        return None
    if price <= 0 or ma20 <= 0 or ma50 <= 0:
        return None

    extension_20_pct = (price / ma20 - 1) * 100
    extension_50_pct = (price / ma50 - 1) * 100
    stretched = rsi >= 75 and (extension_20_pct >= 8 or extension_50_pct >= 12)
    if not stretched:
        return None

    extreme = (
        (rsi >= 80 and (extension_20_pct >= 10 or extension_50_pct >= 15))
        or (rsi >= 75 and extension_20_pct >= 12 and extension_50_pct >= 18)
    )
    severity = "high" if extreme else "med"
    label = "Extreme chase risk" if extreme else "Stretched momentum"
    return {
        "kind": "extension_momentum",
        "severity": severity,
        "label": label,
        "rsi14": round(rsi, 1),
        "extension_20_pct": round(extension_20_pct, 1),
        "extension_50_pct": round(extension_50_pct, 1),
        "text": (
            f"{label}: RSI {rsi:.0f}, {extension_20_pct:+.1f}% vs 20-day and "
            f"{extension_50_pct:+.1f}% vs 50-day. Avoid chasing full size; "
            "prefer a staged entry or wait for a pullback/base."
        ),
    }


def apply_extension_execution_overlay(t_state):
    """Constrain only an Enter that survived the base extension gate.

    Precedence is intentional and deterministic:
      1. ``tactical_action`` blocks ordinary MA extension with Watch.
      2. Exceptional sponsored momentum may survive that gate as Enter.
      3. This RSI-aware overlay then caps that surviving Enter to a starter,
         Accumulate, or Watch. It never upgrades a non-entry decision.
    """
    if not isinstance(t_state, dict):
        return t_state
    warning = t_state.get("extension_warning")
    if not isinstance(warning, dict):
        warning = extension_momentum_warning(t_state)
    if not warning:
        return t_state

    updated = dict(t_state)
    action = str(updated.get("action") or "").strip().lower()
    if action != "enter_now":
        return updated

    updated["extension_pre_overlay_action"] = action
    updated["extension_overlay_applied"] = True
    updated["extension_policy"] = "base_gate_then_rsi_execution_overlay"
    severity = str(warning.get("severity") or "med").lower()
    if severity != "high":
        reason = (
            "Momentum is stretched, so the bullish call remains Enter but execution "
            "is capped at starter size and should be staged."
        )
        updated.update({
            "entry_size": "starter",
            "entry_status": "Staged entry",
            "extension_overlay_reason": reason,
            "matrix_reason": reason,
        })
        return updated

    def _number(value, default=None):
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    reward_risk = _number(updated.get("reward_risk"))
    vol_ratio = _number(updated.get("vol_ratio"), 0) or 0
    rs_delta = _number(updated.get("rs_delta"), 0) or 0
    tech_delta = _number(updated.get("tech_delta"), 0) or 0
    weak_confirmation = vol_ratio < 0.85 or rs_delta < 0 or tech_delta < 0
    thin_trade_math = reward_risk is None or reward_risk < 1.5

    if weak_confirmation or thin_trade_math:
        reasons = []
        if thin_trade_math:
            reasons.append("reward/risk is below 1.5:1")
        if vol_ratio < 0.85:
            reasons.append("volume confirmation is weak")
        if rs_delta < 0:
            reasons.append("relative strength is fading")
        if tech_delta < 0:
            reasons.append("technical momentum is cooling")
        reason = (
            "Extreme chase risk plus " + ", ".join(reasons)
            + "; wait for a pullback or base before adding exposure."
        )
        updated.update({
            "action": "watch",
            "matrix_action": "watch",
            "matrix_changed_action": "watch",
            "entry_size": "none",
            "entry_status": "Waiting for pullback",
        })
    else:
        reason = (
            "The bullish thesis remains intact, but extreme extension makes a full "
            "market entry inappropriate; accumulate only with a staged starter."
        )
        updated.update({
            "action": "accumulate",
            "matrix_action": "accumulate",
            "matrix_changed_action": "accumulate",
            "entry_size": "starter",
            "entry_status": "Staged accumulation",
        })

    updated["extension_overlay_reason"] = reason
    updated["matrix_reason"] = reason
    return updated


def decision_modifiers(t_state, meta, market_reg):
    """Compute decision modifiers — earnings proximity, sector RS, market
    regime. These nudge the conviction up or down on the same nominal
    decision. Returns list of {kind, severity, text} dicts."""
    mods = []

    extension_warning = extension_momentum_warning(t_state)
    if extension_warning:
        mods.append(extension_warning)

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

    rr = t_state.get("reward_risk")
    if rr is not None:
        if rr < 1.2:
            mods.append({
                "kind": "reward_risk", "severity": "high",
                "text": f"Poor projected reward/risk ({rr:.2f}:1) — wait for a better entry or tighter invalidation.",
            })
        elif rr < 1.5:
            mods.append({
                "kind": "reward_risk", "severity": "med",
                "text": f"Thin projected reward/risk ({rr:.2f}:1) — setup needs extra confirmation.",
            })

    return mods


def compute(ticker_hist, bench_hist, atr_threshold=0.015):
    if ticker_hist is None or len(ticker_hist) < 50:
        return None

    prices = ticker_hist["Close"]
    price = float(prices.iloc[-1])
    ma20 = float(prices.iloc[-20:].mean()) if len(prices) >= 20 else price
    ma50 = float(prices.iloc[-50:].mean()) if len(prices) >= 50 else price
    ma100 = float(prices.iloc[-100:].mean()) if len(prices) >= 100 else price
    ma200 = float(prices.iloc[-200:].mean()) if len(prices) >= 200 else price
    ma50_slope = _ma_slope(prices, 50, 20)
    ma200_slope = _ma_slope(prices, 200, 50)

    rs = relative_strength(ticker_hist, bench_hist)
    sq = structure_quality(ticker_hist)
    setup_breakdown = tech_score_breakdown(ticker_hist)
    setup = setup_breakdown["score"]

    atr_pct = float(
        ((ticker_hist["High"] - ticker_hist["Low"]) / ticker_hist["Close"])
        .iloc[-20:].mean()
    )

    # Compute tech_delta first — tactical_action needs it for transition
    # recognition (improving-momentum names default to Hold off).
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

    bias, bias_score = tactical_bias(price, ma50, ma200, ma50_slope, ma200_slope, sq, rs)
    atr_ok = atr_pct >= atr_threshold

    # Compute 52-week extremes + accumulation inputs BEFORE classify_state
    # and tactical_action. tactical_action needs is_accumulation_eligible
    # to block premature Avoid on names that might earn the override.
    last_252 = prices.iloc[-min(252, len(prices)):]
    high_52w = float(last_252.max())
    low_52w = float(last_252.min())
    prior_252 = prices.iloc[-min(253, len(prices)):-1] if len(prices) > 1 else last_252
    prior_high_52w = float(prior_252.max()) if len(prior_252) else high_52w
    rng_52w = high_52w - low_52w
    pct_of_52w_range = float((price - low_52w) / rng_52w * 100) if rng_52w > 0 else 50.0

    # ret_5d: 5-session close return (positive = price recovering)
    if len(prices) >= 6:
        ret_5d = float(prices.iloc[-1] / prices.iloc[-6] - 1)
    else:
        ret_5d = 0.0
    # New 30-day low check — used to gate the stabilization signal
    if len(prices) >= 30:
        last_30 = prices.iloc[-30:]
        recent_5 = prices.iloc[-5:]
        rolling_30_low = float(last_30.min())
        recent_5_low = float(recent_5.min())
        made_new_30d_low_recently = bool(recent_5_low <= rolling_30_low + 1e-9)
    else:
        made_new_30d_low_recently = True

    is_accumulation_eligible = classify_accumulation(
        price, high_52w, low_52w, ma20, ret_5d, rs_delta,
        made_new_30d_low_recently,
    )

    # Classify structural state. This still drives UI copy ("transitioning
    # structure" etc.), but the action gate itself is now driven by the
    # strict-Avoid rule below, not by state.
    state = classify_state(price, ma50, ma200, rs, rs_delta, tech_delta)

    avg_vol_20d = float(ticker_hist["Volume"].iloc[-20:].mean())
    vol_ratio = float(ticker_hist["Volume"].iloc[-1] / avg_vol_20d) if avg_vol_20d > 0 else 1.0

    # Action: tier-1 hard gates → tier-2 disqualifiers → tier-3 modifiers
    # per the 2026-04-28 decision-precedence spec. Avoid is strict (5
    # conditions). Extension downgrade prevents Enter when extended above
    # both MA50 AND MA100 (when MA100 available).
    action = tactical_action(
        bias, bias_score, setup, atr_ok, price, ma50,
        ma200=ma200, ma100=ma100, ma20=ma20,
        rs=rs, rs_delta=rs_delta, tech_delta=tech_delta,
        vol_ratio=vol_ratio,
        state=state, is_accumulation_eligible=is_accumulation_eligible,
    )

    rsi14 = _rsi(prices)
    last_10 = prices.iloc[-10:]
    prior_10 = prices.iloc[-11:-1] if len(prices) >= 11 else prices.iloc[:-1]
    if len(prior_10) == 0:
        prior_10 = last_10
    range_10d_pct = float((last_10.max() - last_10.min()) / price)
    support = float(prior_10.min())
    resistance = float(prior_10.max())

    # Recent swing high — highest close in the last 60 sessions (3 months).
    # Used by reconsider_when as a "Primary" candidate level when it sits
    # closer than the MAs. For a name chopping below ma200, the recent
    # swing high reclaim is the actionable level, not the ma200.
    if len(prices) >= 60:
        swing_high_60d = float(prices.iloc[-60:].max())
    else:
        swing_high_60d = float(prices.max())

    # Auto-detected key support/resistance levels from full price history.
    # Used by historical_support_trigger to fire a Watch when price is
    # approaching a meaningful support level — even when bias isn't bullish.
    key_levels = detect_key_levels(prices)

    # Recent pullback anchor — the most meaningful local low in the last
    # ~25 sessions. Used by next_trigger's pullback branch when price is
    # materially extended above ma50 (so ma50 itself is too far away to
    # be actionable). A "local low" requires a 3-day swing window so we
    # don't pick a single-bar outlier wick.
    recent_pullback_anchor = None
    if len(prices) >= 30:
        lookback_window = prices.iloc[-25:]
        local_lows = []
        for i in range(2, len(lookback_window) - 2):
            ctr = lookback_window.iloc[i]
            if (ctr <= lookback_window.iloc[i - 1] and
                    ctr <= lookback_window.iloc[i - 2] and
                    ctr <= lookback_window.iloc[i + 1] and
                    ctr <= lookback_window.iloc[i + 2]):
                local_lows.append(float(ctr))
        if local_lows:
            # Pick the highest local low — the most recent meaningful
            # pullback level the stock has bounced from. Higher = closer
            # to current price = more actionable.
            recent_pullback_anchor = max(local_lows)

    trigger = next_trigger(
        bias, action, price, ma50, high_52w, vol_ratio,
        range_10d_pct, support, resistance, tech_delta, rs_delta, rs,
        avg_vol_20d,
        ma20=ma20,
        recent_pullback_anchor=recent_pullback_anchor,
        prior_high_52w=prior_high_52w,
    )

    trigger_fired = False
    trigger_fired_reason = ""
    if action == "watch" and trigger:
        buy_level = (trigger.get("levels") or {}).get("buy_above")
        kind = trigger.get("kind")
        try:
            buy_level = float(buy_level) if buy_level is not None else None
        except (TypeError, ValueError):
            buy_level = None
        if buy_level and price >= buy_level * 1.003:
            if kind in ("fast_momentum", "breakout", "coil_break"):
                confirmation_ok = (
                    vol_ratio >= 0.80 or
                    tech_delta >= 0.75 or
                    rs_delta >= 0.01
                )
                if confirmation_ok:
                    trigger_fired = True
                    trigger_fired_reason = (
                        f"Price cleared the prior trigger at ${buy_level:.2f}; "
                        "volume/momentum confirmation is acceptable."
                    )
            elif kind == "historical_support_test":
                meta = trigger.get("support_meta") or {}
                confirmation_ok = (
                    meta.get("status") == "held_above" and
                    (vol_ratio >= 0.80 or tech_delta > 0 or rs_delta >= 0)
                )
                if confirmation_ok:
                    trigger_fired = True
                    trigger_fired_reason = (
                        f"Support at ${buy_level:.2f} already held; "
                        "continuation is sufficient for an entry signal."
                    )
        if trigger_fired:
            trigger = {
                **trigger,
                "fired": True,
                "fired_reason": trigger_fired_reason,
            }
            action = "enter_now"

    # Retrospective trigger catch-up. The UI may have shown a "buy above X"
    # level on a prior day, then recomputed a higher resistance level after
    # price cleared X. That is moving the goalpost. If recent price action
    # already crossed and held a prior 20-day resistance pivot, treat it as
    # a fired trigger instead of asking for a fresh breakout.
    if action in ("watch", "hold_off") and price > ma50 and rs >= 1.0 and setup >= 6:
        fired_level = None
        sessions_ago = None
        max_scan = min(15, len(prices) - 22)
        for offset in range(1, max_scan + 1):
            close_idx = len(prices) - offset
            prior_window = prices.iloc[max(0, close_idx - 21):close_idx - 1]
            if len(prior_window) < 10:
                continue
            pivot = float(prior_window.max())
            close_at_test = float(prices.iloc[close_idx - 1])
            if close_at_test >= pivot * 1.003 and price >= pivot * 1.003:
                fired_level = pivot
                sessions_ago = offset - 1
                break
        if fired_level:
            trigger_fired = True
            if sessions_ago and sessions_ago > 0:
                timing = f"{sessions_ago} sessions ago"
            else:
                timing = "today"
            trigger_fired_reason = (
                f"Prior breakout trigger at ${fired_level:.2f} fired {timing} "
                "and price is still holding above it."
            )
            trigger = {
                "kind": "retrospective_breakout",
                "sessions_ago": sessions_ago or 0,
                "summary": f"prior breakout above ${fired_level:.2f} already fired",
                "buy_rule": (
                    f"Buy while price holds above the fired trigger at ${fired_level:.2f}; "
                    "do not reset the trigger to the next resistance level."
                ),
                "abort_rule": (
                    f"Abandon the setup if price closes back below ${ma50:.2f} "
                    "or loses relative strength."
                ),
                "levels": {
                    "buy_above": round(fired_level, 2),
                    "abort_below": round(ma50, 2),
                    "volume_min": None,
                },
                "fired": True,
                "fired_reason": trigger_fired_reason,
            }
            action = "enter_now"

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
    volatility_t1 = anchor * (1 + max(atr_pct * 3, 0.05))
    volatility_t2 = anchor * (1 + max(atr_pct * 6, 0.10))
    structural_targets = _structural_targets_above(
        anchor,
        atr_pct,
        key_levels=key_levels,
        resistance=resistance,
        swing_high_60d=swing_high_60d,
        prior_high_52w=prior_high_52w,
        high_52w=high_52w,
    )
    target_meta = []
    for target in structural_targets[:2]:
        target_meta.append({
            "source": "Structural target",
            "detail": target["label"],
            "level": target["level"],
        })
    if len(target_meta) == 0:
        target_meta.append({
            "source": "Volatility-derived target",
            "detail": "3x ATR / 5% minimum fallback",
            "level": volatility_t1,
        })
    if len(target_meta) == 1:
        fallback_level = max(volatility_t2, target_meta[0]["level"] * 1.03)
        fallback_detail = (
            "6x ATR / 10% minimum fallback"
            if fallback_level == volatility_t2
            else "fallback beyond first structural objective"
        )
        target_meta.append({
            "source": "Volatility-derived target",
            "detail": fallback_detail,
            "level": fallback_level,
        })

    t1 = target_meta[0]["level"]
    t2 = target_meta[1]["level"]
    risk_per_share = entry - stop
    reward_per_share = t1 - entry
    reward_risk = (
        float(reward_per_share / risk_per_share)
        if risk_per_share > 0 and reward_per_share > 0
        else None
    )

    # Final risk/reward sanity check. This is intentionally a sizing /
    # cleanliness overlay, not a hard "no" by itself. The old version
    # turned every thin reward/risk read into Watch/Hold off, which made
    # the system miss real fired triggers. The app-level decision matrix
    # now decides whether thin math means starter size, Watch, or Hold off.
    reward_risk_gate = False
    reward_risk_gate_reason = ""
    if reward_risk is not None and reward_risk < 1.0:
        reward_risk_gate = True
        reward_risk_gate_reason = (
            f"Reward/risk is {reward_risk:.2f}:1 to Target 1 "
            f"({target_meta[0]['detail']}), so the setup "
            "is starter-size at best unless the trigger has already fired and the tape confirms."
        )
    elif reward_risk is not None and reward_risk < 1.2:
        reward_risk_gate = True
        reward_risk_gate_reason = (
            f"Reward/risk is thin at {reward_risk:.2f}:1 to Target 1 "
            f"({target_meta[0]['detail']}); "
            "position size should stay conservative unless the setup improves."
        )
    change = float((prices.iloc[-1] / prices.iloc[-2] - 1) * 100) if len(prices) >= 2 else 0.0

    # ── Historical context: how often has price tested the 50-day, and
    #    what happened? Used in technical read + dossier prompt. ──
    ma50_history = ma_test_history(ticker_hist, ma_period=50)

    # ── Market regime from the benchmark (SPY) ──
    market_reg = market_regime(bench_hist)

    result = {
        "bias": display_bias,
        "raw_bias": bias,
        "action": action,
        "state": state,          # TRENDING / TRANSITION / BROKEN
        "is_accumulation_eligible": is_accumulation_eligible,
        "trigger": trigger,      # now a dict or None
        "trigger_fired": trigger_fired,
        "trigger_fired_reason": trigger_fired_reason,
        "bias_score": bias_score,
        "setup_score": setup,
        "setup_score_breakdown": setup_breakdown,
        "atr_pct": atr_pct,
        "atr_ok": atr_ok,
        "price": price,
        "ma50": ma50,
        "ma100": ma100,
        "ma200": ma200,
        "ma20": ma20,
        "rs": rs,
        "rs_delta": rs_delta,
        "tech_delta": tech_delta,
        "high_52w": high_52w,
        "low_52w": low_52w,
        "swing_high_60d": swing_high_60d,
        "key_levels": key_levels,
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
        "t1_source": target_meta[0]["source"],
        "t1_detail": target_meta[0]["detail"],
        "t2_source": target_meta[1]["source"],
        "t2_detail": target_meta[1]["detail"],
        "structural_targets": structural_targets,
        "reward_risk": reward_risk,
        "reward_risk_gate": reward_risk_gate,
        "reward_risk_gate_reason": reward_risk_gate_reason,
        "change": change,
    }
    result["price_vs_20_pct"] = round((price / ma20 - 1) * 100, 2) if ma20 > 0 else None
    result["price_vs_50_pct"] = round((price / ma50 - 1) * 100, 2) if ma50 > 0 else None
    result["price_vs_200_pct"] = round((price / ma200 - 1) * 100, 2) if ma200 > 0 else None
    result["extension_warning"] = extension_momentum_warning(result)
    return result
