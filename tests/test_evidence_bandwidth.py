import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

import backend_layer as backend
import api_repository


class EvidenceBandwidthTests(unittest.TestCase):
    def test_calibration_does_not_load_legacy_workspace_blob(self):
        cursor = Mock()
        cursor.fetchone.return_value = (["DASH"],)
        @contextmanager
        def connection():
            conn = Mock()
            conn.cursor.return_value.__enter__ = Mock(return_value=cursor)
            conn.cursor.return_value.__exit__ = Mock(return_value=False)
            yield conn
        with patch.object(backend, "db_connection", connection), patch.object(api_repository, "_workspace_state", side_effect=AssertionError("large workspace")), patch.object(backend, "read_json_table_many", return_value={}), patch.object(backend, "read_rule_evidence", return_value={}):
            api_repository.calibration("00000000-0000-0000-0000-000000000001")
        self.assertIn("value->'watchlist'", cursor.execute.call_args.args[0])

    def test_summary_never_downloads_logs(self):
        saved = {"evidence_id": "frozen", "summary": {"count": 12}, "weakest_cases": [{"ticker": "PRIVATE"}]}
        with patch.object(backend, "read_engine_review_status", return_value=saved), patch.object(backend, "read_decision_logs", side_effect=AssertionError("full scan")):
            result = backend.read_rule_evidence()
        self.assertEqual(result["summary"], {"count": 12})
        self.assertNotIn("weakest_cases", result)
        self.assertNotIn("review_cases", result)
        self.assertEqual(saved["weakest_cases"][0]["ticker"], "PRIVATE")

    def test_missing_publication_does_not_fall_back_to_full_scan(self):
        with patch.object(backend, "read_engine_review_status", return_value={}), patch.object(backend, "read_decision_logs", side_effect=AssertionError("full scan")):
            self.assertEqual(backend.read_rule_evidence()["publication_status"], "pending")

    def test_cases_are_bounded_in_sql(self):
        cursor = Mock()
        cursor.fetchall.return_value = [({"id": "case51"},)]
        @contextmanager
        def connection():
            conn = Mock()
            conn.cursor.return_value.__enter__ = Mock(return_value=cursor)
            conn.cursor.return_value.__exit__ = Mock(return_value=False)
            yield conn
        saved = {"evidence_id": "frozen", "case_publication_id": "revision", "case_count": 90}
        with patch.object(backend, "read_engine_review_status", return_value=saved), patch.object(backend, "db_connection", connection), patch("backtest_store.read", return_value={}):
            result = backend.read_rule_evidence(include_cases=True, case_offset=50, case_limit=10000)
        self.assertEqual(cursor.execute.call_args.args[1], ("cases:revision", 50, 100))
        self.assertEqual(result["case_pagination"]["limit"], 50)
        self.assertEqual(result["review_cases"], [{"id": "case51"}])


if __name__ == "__main__":
    unittest.main()
