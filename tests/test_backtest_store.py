import unittest
from unittest.mock import MagicMock, patch
import json
import backtest_store
import backend_layer


class BacktestStoreTests(unittest.TestCase):
    def test_old_corrected_report_hides_inactive_calendar_defaults_without_mutation(self):
        backend = MagicMock()
        original = {"assumptions": {"exit_policy": "signal_invalidation", "holding_sessions": 14, "target_r": 2}, "results": {"validation": {"return": 18.12}}}
        cursor = backend.db_connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (original,)
        result = backtest_store.read(backend)
        self.assertNotIn("holding_sessions", result["assumptions"])
        self.assertFalse(result["assumptions"]["calendar_exit_enabled"])
        self.assertEqual(result["results"], original["results"])
        self.assertEqual(original["assumptions"]["holding_sessions"], 14)
        self.assertEqual(cursor.execute.call_count, 1)

    def test_read_does_not_relabel_actual_legacy_experiment(self):
        backend = MagicMock()
        legacy = {"assumptions": {"exit_policy": "legacy_14_session", "holding_sessions": 14}}
        backend.db_connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value = (legacy,)
        result = backtest_store.read(backend)
        self.assertEqual(result["assumptions"], legacy["assumptions"])
        self.assertEqual(result["comparison_summary"]["exit_policy"], "legacy_14_session")

    def test_legacy_can_be_archived_but_cannot_replace_active_report(self):
        backend = MagicMock()
        backend.json_dumps.side_effect = json.dumps
        report = dict(assumptions={"exit_policy": "legacy_14_session"}, versions=["original"],
                      validation_start="2026-01-01", limitations=[], results={})
        backtest_store.publish(report, backend)
        cursor = backend.db_connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        self.assertEqual(cursor.execute.call_count, 1)
        sql, params = cursor.execute.call_args.args
        self.assertTrue(params[0].startswith("historical_replay:"))
        self.assertNotIn("VALUES ('historical_replay'", sql)

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
