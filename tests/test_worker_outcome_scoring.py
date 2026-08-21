import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd

import worker


def market_history(start_price=100, sessions=40):
    dates = pd.bdate_range(end=date.today(), periods=sessions)
    return pd.DataFrame(
        {
            "Close": [start_price + index for index in range(sessions)],
            "High": [start_price + index + 1 for index in range(sessions)],
            "Low": [start_price + index - 1 for index in range(sessions)],
        },
        index=dates,
    )


class WorkerOutcomeScoringTests(unittest.TestCase):
    def test_scheduled_scoring_persists_outcome_and_status(self):
        logged = datetime.now(timezone.utc) - timedelta(days=35)
        entry = {
            "id": "rules-auto-TEST",
            "ts": logged.isoformat(),
            "ticker": "TEST",
            "price": 100,
            "rule_action": "enter_now",
            "outcome": None,
        }
        stored = [entry]
        saved_entries = []
        saved_statuses = []

        def save_entry(updated):
            saved_entries.append(updated.copy())
            stored[0] = updated.copy()

        with (
            patch.object(worker.backend, "read_decision_logs", side_effect=lambda: [row.copy() for row in stored]),
            patch.object(worker.backend, "upsert_decision_log", side_effect=save_entry),
            patch.object(worker.backend, "write_engine_review_status", side_effect=lambda payload: saved_statuses.append(payload)),
            patch.object(worker, "_download_history", return_value=market_history()),
            patch.object(worker, "_download_benchmark", return_value=market_history(500)),
        ):
            result = worker.score_due_rule_outcomes(max_entries=12)

        self.assertEqual(result["scored"], 1)
        self.assertTrue(saved_entries[0]["outcome"]["auto_scored"])
        self.assertEqual(saved_statuses[0]["score_version"], worker.OUTCOME_SCORE_VERSION)
        self.assertEqual(saved_statuses[0]["flags"][0]["count"], 1)

    def test_same_day_partial_score_is_not_downloaded_again(self):
        entry = {
            "id": "rules-auto-TEST",
            "ts": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
            "ticker": "TEST",
            "price": 100,
            "rule_action": "enter_now",
            "outcome": {
                "ts": datetime.now(timezone.utc).isoformat(),
                "auto_scored": True,
                "score_version": worker.OUTCOME_SCORE_VERSION,
                "evaluation_complete": False,
            },
        }
        with (
            patch.object(worker.backend, "read_decision_logs", return_value=[entry]),
            patch.object(worker.backend, "write_engine_review_status"),
            patch.object(worker, "_download_history") as download,
        ):
            result = worker.score_due_rule_outcomes(max_entries=12)

        self.assertEqual(result["scored"], 0)
        download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
