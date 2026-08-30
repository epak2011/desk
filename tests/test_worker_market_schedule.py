import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
