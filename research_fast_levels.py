"""Research-only equivalent support-level computation; production source untouched.

Only repeated scalar window min/max reductions are vectorized. Pruning, ties,
clusters, rounding and score ordering are copied from the frozen implementation.
Full technical-state equivalence is verified separately before using this path.
"""
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
    # The same inclusive 11-point windows and exact comparisons as the
    # frozen implementation, evaluated once instead of thousands of reductions.
    values = series.to_numpy()
    maxima = series.rolling(2 * window + 1, center=True, min_periods=1).max().to_numpy()
    minima = series.rolling(2 * window + 1, center=True, min_periods=1).min().to_numpy()
    for i in range(window, n - window):
        center = values[i]
        if center == maxima[i] and center > values[i - 1]:
            extrema.append((i, float(center), "high"))
        elif center == minima[i] and center < values[i - 1]:
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
