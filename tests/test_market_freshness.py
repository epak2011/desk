import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import market_freshness


ET = ZoneInfo("America/New_York")


class MarketFreshnessTests(unittest.TestCase):
    def test_utc_timestamp_preserves_instant(self):
        stamp = market_freshness.parse_timestamp("2026-08-28T19:50:00Z")
        self.assertEqual(market_freshness.market_now(stamp).hour, 15)

    def test_friday_close_is_fresh_on_weekend(self):
        result = market_freshness.snapshot_freshness(
            "NVDA",
            datetime(2026, 8, 28, 15, 50, tzinfo=ET),
            now=datetime(2026, 8, 30, 12, 0, tzinfo=ET),
        )
        self.assertTrue(result["fresh"])
        self.assertIn("last completed", result["reason"])

    def test_old_equity_row_is_stale_on_weekend(self):
        result = market_freshness.snapshot_freshness(
            "NVDA",
            datetime(2026, 8, 27, 16, 0, tzinfo=ET),
            now=datetime(2026, 8, 30, 12, 0, tzinfo=ET),
        )
        self.assertFalse(result["fresh"])

    def test_open_market_uses_intraday_age(self):
        result = market_freshness.snapshot_freshness(
            "NVDA",
            datetime(2026, 8, 28, 11, 0, tzinfo=ET),
            now=datetime(2026, 8, 28, 11, 45, tzinfo=ET),
        )
        self.assertFalse(result["fresh"])

    def test_crypto_remains_continuous_on_weekend(self):
        now = datetime(2026, 8, 30, 12, 0, tzinfo=ET)
        self.assertTrue(market_freshness.worker_should_refresh("BTC-USD", now))
        self.assertFalse(market_freshness.worker_should_refresh("NVDA", now))

    def test_worker_refreshes_equities_during_session(self):
        now = datetime(2026, 8, 28, 10, 0, tzinfo=ET)
        self.assertTrue(market_freshness.worker_should_refresh("NVDA", now))


if __name__ == "__main__":
    unittest.main()
