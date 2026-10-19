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
    def test_historical_shadow_candidates_backfill_from_frozen_snapshot(self):
        entry = {
            "id": "rules-auto-BACKFILL",
            "rule_action": "enter_now",
            "decision_inputs": {
                "action": "enter_now", "state": "TRENDING", "market_regime": "Mixed",
                "inputs": {"price": 110, "ma50": 100, "ma100": 95, "ma200": 90, "setup_score": 7, "reward_risk": 1.5},
            },
            "decision_context": {"market_regime": "Mixed", "extension_warning": True, "extension_warning_severity": "med"},
            "shadow_evaluations": [],
            "outcome": {
                "forward_return_pct": -6, "excess_return_pct": -4,
                "directional_success": False, "decision_return_pct": -6,
            },
        }
        saved = []
        with patch.object(worker.backend, "upsert_decision_log", side_effect=lambda row: saved.append(row.copy())):
            count = worker._backfill_shadow_candidates([entry])
        self.assertEqual(count, 1)
        self.assertEqual(
            {row["candidate"] for row in entry["shadow_evaluations"]},
            {"entry_timing_guard", "regime_quality_gate", "confirmed_entry_hold"},
        )
        self.assertTrue(all(row["directional_success"] for row in entry["outcome"]["shadow_results"]))
        self.assertEqual(len(saved), 1)

    def test_scheduled_scoring_persists_outcome_and_status(self):
        logged = datetime.now(timezone.utc) - timedelta(days=35)
        entry = {
            "id": "rules-auto-TEST",
            "ts": logged.isoformat(),
            "ticker": "TEST",
            "price": 100,
            "rule_action": "enter_now",
            "outcome": None,
            "auto_logged": True,
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
        self.assertEqual(result["flags"][0]["count"], 1)

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
            "auto_logged": True,
        }
        with (
            patch.object(worker.backend, "read_decision_logs", return_value=[entry]),
            patch.object(worker.backend, "write_engine_review_status"),
            patch.object(worker, "_download_history") as download,
        ):
            result = worker.score_due_rule_outcomes(max_entries=12)

        self.assertEqual(result["scored"], 0)
        download.assert_not_called()

    def test_skips_the_unbounded_read_when_scored_recently(self):
        """Guards the fix for the worker's share of the Supabase egress spike: the
        worker is a fresh process every ~5-minute maintenance pass, so scoring must
        gate on the cheap engine_review_status timestamp instead of re-reading the
        entire decisions_log table every pass.
        """
        recent_status = {
            "generated_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            "flags": [{"label": "x"}],
            "alerting": [{"label": "x"}],
            "remaining_due": 3,
        }
        with (
            patch.object(worker.backend, "read_engine_review_status", return_value=recent_status),
            patch.object(worker.backend, "read_decision_logs") as read_logs,
        ):
            result = worker.score_due_rule_outcomes(max_entries=12)

        read_logs.assert_not_called()
        self.assertEqual(result["scored"], 0)
        self.assertEqual(result["skipped"], "scored_recently")
        self.assertEqual(result["alerts"], 1)
        self.assertEqual(result["remaining_due"], 3)

    def test_runs_normally_when_last_score_is_stale(self):
        stale_status = {
            "generated_at": (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(),
        }
        with (
            patch.object(worker.backend, "read_engine_review_status", return_value=stale_status),
            patch.object(worker.backend, "read_decision_logs", return_value=[]) as read_logs,
            patch.object(worker.backend, "write_engine_review_status"),
        ):
            result = worker.score_due_rule_outcomes(max_entries=12)

        read_logs.assert_called()
        self.assertNotIn("skipped", result)

    def test_min_interval_hours_zero_disables_the_gate(self):
        recent_status = {"generated_at": datetime.now(timezone.utc).isoformat()}
        with (
            patch.object(worker.backend, "read_engine_review_status", return_value=recent_status),
            patch.object(worker.backend, "read_decision_logs", return_value=[]) as read_logs,
            patch.object(worker.backend, "write_engine_review_status"),
        ):
            result = worker.score_due_rule_outcomes(max_entries=12, min_interval_hours=0)

        read_logs.assert_called()
        self.assertNotIn("skipped", result)

    def test_unscored_mature_call_precedes_old_30_session_followup(self):
        old = {
            "id": "old-followup", "ticker": "OLD", "auto_logged": True,
            "ts": (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(),
            "price": 100, "rule_action": "enter_now",
            "outcome": {"evaluation_version": worker.OUTCOME_SCORE_VERSION,
                        "forward_return_pct": -4, "evaluation_complete": False},
        }
        primary = {
            "id": "new-primary", "ticker": "NEW", "auto_logged": True,
            "ts": (datetime.now(timezone.utc) - timedelta(days=35)).isoformat(),
            "price": 100, "rule_action": "enter_now", "outcome": None,
        }
        saved = []
        with (
            patch.object(worker.backend, "read_decision_logs", return_value=[old, primary]),
            patch.object(worker.backend, "upsert_decision_log", side_effect=lambda row: saved.append(row)),
            patch.object(worker.backend, "write_engine_review_status"),
            patch.object(worker, "_download_history", return_value=market_history()),
            patch.object(worker, "_download_benchmark", return_value=market_history(500)),
        ):
            worker.score_due_rule_outcomes(max_entries=1)
        self.assertEqual([row["id"] for row in saved], ["new-primary"])


if __name__ == "__main__":
    unittest.main()
