import unittest
from unittest.mock import patch

import pandas as pd

import worker


class WorkerMarketScheduleTests(unittest.TestCase):
    @patch("worker.backend.enqueue_job")
    @patch("worker.backend.stale_watchlist_market_tickers", return_value=["NVDA", "BTC-USD"])
    @patch("worker.market_freshness.worker_should_refresh", side_effect=lambda ticker: ticker == "BTC-USD")
    def test_closed_market_filters_equities_but_keeps_crypto(self, _refresh, _stale, enqueue):
        enqueue.return_value = "job-1"
        result = worker.queue_stale_watchlist_market_scan()

        self.assertTrue(result["queued"])
        payload = enqueue.call_args.kwargs["payload"]
        self.assertEqual(payload["tickers"], ["BTC-USD"])

    @patch("worker.backend.enqueue_job")
    @patch("worker.backend.stale_watchlist_market_tickers", return_value=["NVDA"])
    @patch("worker.market_freshness.worker_should_refresh", return_value=False)
    def test_closed_market_does_not_enqueue_equity_scan(self, _refresh, _stale, enqueue):
        result = worker.queue_stale_watchlist_market_scan()

        self.assertFalse(result["queued"])
        enqueue.assert_not_called()

    @patch("worker.backend.upsert_json_table")
    @patch("worker._download_history")
    def test_market_regime_job_persists_deterministic_snapshot(self, download, upsert):
        dates = pd.bdate_range("2025-01-01", periods=220)
        download.return_value = pd.DataFrame({"Close": range(100, 320)}, index=dates)
        result = worker.process_job({"job_type": "market_regime_daily", "payload": {}, "ticker": None})
        self.assertIn(result["portfolio_stance"], {"Risk On", "Moderately Risk On", "Neutral", "Defensive", "Risk Off"})
        upsert.assert_called_once()
        self.assertEqual(upsert.call_args.args[0], "market_regime_daily")

    @patch("worker.refresh_market_regime_daily", return_value={"day": "2026-09-04"})
    @patch("worker.refresh_market_snapshot", side_effect=lambda ticker, bench=None: {"ticker": ticker})
    @patch("worker._download_benchmark", return_value=pd.DataFrame({"Close": [1, 2]}))
    @patch("worker.backend.read_json_table_many", return_value={})
    @patch("worker.backend.enabled_watchlist_tickers", return_value=["NVDA", "AAPL"])
    def test_repair_job_refreshes_durable_watchlist_rows(self, _tickers, _rows, _bench, _refresh, _regime):
        result = worker.process_job({"job_type": "repair_missing_data", "payload": {}, "ticker": None})
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["missing"], 2)
        self.assertEqual(result["repaired"], 2)
        self.assertTrue(result["regime_updated"])


if __name__ == "__main__":
    unittest.main()
