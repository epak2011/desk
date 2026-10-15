import unittest
from unittest.mock import MagicMock, patch
import json
import backtest_store
import backend_layer


class BacktestStoreTests(unittest.TestCase):
    def test_summary_excludes_trades_and_frozen_source_code(self):
        backend = MagicMock()
        backend.json_dumps.side_effect = json.dumps
        report = dict(assumptions={}, versions=["original"], validation_start="2026-01-01",
                      limitations=[], frozen_sources={"secret": "source"},
                      results={"validation": {"original": {"total_return_pct": 1,
                                                            "trades": [{"ticker": "PRIVATE"}]}}})
        backtest_store.publish(report, backend)
        sql, params = backend.db_connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.execute.call_args.args
        self.assertIn("'historical_replay'", sql)
        self.assertNotIn("'current'", sql)
        self.assertNotIn("PRIVATE", params[0])
        self.assertNotIn("frozen_sources", params[0])

    def test_nonowner_does_not_read_replay(self):
        with patch.object(backend_layer, "read_decision_logs", return_value=[]), \
             patch.object(backend_layer, "read_engine_review_status", return_value={}), \
             patch.object(backtest_store, "read") as read:
            result = backend_layer.read_rule_evidence(include_cases=False)
        read.assert_not_called()
        self.assertNotIn("historical_replay", result)
