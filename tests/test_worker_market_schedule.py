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
    @patch("worker._macro_regime_inputs", return_value={"hy_oas_bps": 265, "fear_greed": 69})
    @patch("worker._download_history")
    def test_market_regime_job_persists_deterministic_snapshot(self, download, _macro, upsert):
        dates = pd.bdate_range("2025-01-01", periods=220)
        download.return_value = pd.DataFrame({"Close": range(100, 320)}, index=dates)
        result = worker.process_job({"job_type": "market_regime_daily", "payload": {}, "ticker": None})
        self.assertIn(result["portfolio_stance"], {"Favorable", "Mixed", "Unfavorable"})
        upsert.assert_called_once()
        self.assertEqual(upsert.call_args.args[0], "market_regime_daily")
        self.assertIn("why_today", result)
        self.assertGreaterEqual(len(result["watch_triggers"]), 3)
        self.assertEqual(len(result["market_highlights"]), 6)

    def test_streamlit_parity_pullback_is_mixed_hold_off(self):
        assets = {
            "SPY": {"vs_20d_pct": -0.1, "vs_50d_pct": 1.3, "return_5d_pct": 0.1, "return_20d_pct": -0.7},
            "^VIX": {"last": 15.3, "peak_5d": 15.5, "drop_from_5d_peak_pct": 1.3},
        }
        macro = {"ism": None, "unemployment": 4.1, "unemployment_previous": 4.1,
                 "hy_oas_bps": 265, "yield_curve_bps": 41, "fear_greed": 69}
        result = worker._streamlit_regime_score(assets=assets, macro=macro)
        self.assertEqual(result["score"], 2)
        self.assertEqual(result["window"], "Mixed")
        self.assertEqual(result["timing"], "Pullback watch")
        self.assertEqual(result["action"], "Hold Off")

    def test_regime_context_changes_with_current_market_inputs(self):
        constructive_assets = {
            "SPY": {"last": 600, "vs_20d_pct": 2, "vs_50d_pct": 5, "return_20d_pct": 4},
            "RSP": {"last": 180, "return_20d_pct": 4},
            "HYG": {"last": 80, "return_20d_pct": 1},
            "^VIX": {"last": 16},
        }
        weak_assets = {
            "SPY": {"last": 520, "vs_20d_pct": -4, "vs_50d_pct": -7, "return_20d_pct": -8},
            "RSP": {"last": 150, "return_20d_pct": -12},
            "HYG": {"last": 70, "return_20d_pct": -5},
            "^VIX": {"last": 36},
        }
        strong = worker._regime_decision_context(stance="Risk On", score=6, assets=constructive_assets, errors={})
        weak = worker._regime_decision_context(stance="Risk Off", score=-6, assets=weak_assets, errors={})
        self.assertEqual(strong["opportunity_action"], "enter")
        self.assertEqual(weak["opportunity_action"], "avoid")
        self.assertNotEqual(strong["why_today"], weak["why_today"])
        self.assertTrue(weak["risks"])

    @patch("worker.backend.enqueue_job", return_value="job-1")
    @patch("worker.backend.read_json_table")
    @patch("worker.backend.latest_jobs")
    def test_schema_upgrade_forces_same_day_regime_refresh(self, latest_jobs, read_regime, enqueue):
        latest_jobs.return_value = [{
            "job_type": "market_regime_daily", "status": "succeeded",
            "created_at": pd.Timestamp.today(),
        }]
        read_regime.return_value = {"today": {
            "schema_version": worker.MARKET_REGIME_SCHEMA_VERSION - 1,
            "crypto_regime": {"model_version": worker.CRYPTO_REGIME_MODEL_VERSION},
        }}
        result = worker.queue_scheduled_backend_maintenance()
        self.assertIn("market_regime_daily", result["queued"])
        self.assertTrue(any(call.args[0] == "market_regime_daily" for call in enqueue.call_args_list))

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
