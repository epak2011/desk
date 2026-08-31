"""Public-facing confidence states for partially available decision inputs."""


CORE_REGIME_INPUTS = (
    ("spx", "S&P 500 price"),
    ("spx_vs20", "S&P 500 vs 20-day trend"),
    ("spx_vs50", "S&P 500 vs 50-day trend"),
    ("vix", "VIX"),
)

CONFIRMING_REGIME_INPUTS = (
    ("qqq", "Nasdaq 100"),
    ("hy_bps", "high-yield spreads"),
    ("fg", "Fear & Greed"),
    ("ism", "ISM manufacturing"),
    ("unemp", "unemployment"),
    ("yc_bps", "yield curve"),
)


def regime_confidence(data):
    """Classify a regime read without changing the underlying decision logic."""
    data = data if isinstance(data, dict) else {}
    missing_core = [label for key, label in CORE_REGIME_INPUTS if data.get(key) is None]
    missing_confirmation = [
        label for key, label in CONFIRMING_REGIME_INPUTS if data.get(key) is None
    ]
    if missing_core:
        return {
            "state": "blocked",
            "label": "Decision confidence blocked",
            "summary": "Core market inputs are incomplete. Do not act on the broad regime call yet.",
            "missing": missing_core + missing_confirmation,
        }
    if missing_confirmation:
        return {
            "state": "degraded",
            "label": "Decision confidence reduced",
            "summary": "The broad read is usable, but confirmation is incomplete. Keep sizing conservative.",
            "missing": missing_confirmation,
        }
    return {
        "state": "trusted",
        "label": "Decision inputs confirmed",
        "summary": "Core trend, volatility, credit, macro, and sentiment inputs are available.",
        "missing": [],
    }
