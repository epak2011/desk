import unittest
from datetime import date

import pandas as pd

import engine_evaluation


def history(rows):
    return pd.DataFrame(
        rows,
        index=pd.to_datetime([row.pop("date") for row in rows]),
    )


def path_history(start="2026-01-02", sessions=30, start_price=100, step=1):
    dates = pd.bdate_range(start=start, periods=sessions + 1)
    rows = []
    for index, day in enumerate(dates):
        close = start_price + index * step
        rows.append({"date": str(day.date()), "Close": close, "High": close + 2, "Low": close - 2})
    return history(rows)


class EngineEvaluationTests(unittest.TestCase):
    def test_waits_until_fixed_horizon_matures(self):
        bars = history([
            {"date": "2026-01-02", "Close": 100, "High": 101, "Low": 99},
            {"date": "2026-01-16", "Close": 110, "High": 111, "Low": 98},
        ])
        entry = {"ts": "2026-01-02T10:00:00", "price": 100, "rule_action": "enter_now"}
        self.assertIsNone(
            engine_evaluation.score_forward_outcome(
                entry, bars, as_of=date(2026, 1, 15)
            )
        )

    def test_scores_fixed_trading_session_paths(self):
        bars = path_history(step=1)
        spy = path_history(start_price=500, step=1)
        entry = {"ts": "2026-01-02T10:00:00", "price": 100, "rule_action": "enter_now"}
        outcome = engine_evaluation.score_forward_outcome(
            entry,
            bars,
            benchmark_history=spy,
            as_of=date(2026, 3, 1),
        )
        self.assertEqual(outcome["horizons"]["5"]["return_pct"], 5.0)
        self.assertEqual(outcome["forward_return_pct"], 14.0)
        self.assertEqual(outcome["horizons"]["30"]["return_pct"], 30.0)
        self.assertEqual(outcome["decision_return_pct"], 14.0)
        self.assertTrue(outcome["evaluation_complete"])
        self.assertTrue(outcome["credited"])

    def test_avoid_decision_edge_inverts_underlying_return(self):
        bars = path_history(sessions=14, step=1)
        entry = {"ts": "2026-01-02T10:00:00", "price": 100, "rule_action": "avoid"}
        outcome = engine_evaluation.score_forward_outcome(
            entry, bars, as_of=date(2026, 2, 1)
        )
        self.assertEqual(outcome["forward_return_pct"], 14.0)
        self.assertEqual(outcome["decision_return_pct"], -14.0)
        self.assertFalse(outcome["credited"])

    def test_watch_scores_trigger_lifecycle_not_direction(self):
        bars = path_history(sessions=30, step=1)
        entry = {
            "ts": "2026-01-02T10:00:00",
            "price": 100,
            "rule_action": "watch",
            "trigger_price": 104,
            "invalidation_price": 95,
        }
        outcome = engine_evaluation.score_forward_outcome(entry, bars, as_of=date(2026, 3, 1))
        self.assertTrue(outcome["trigger_fired"])
        self.assertEqual(outcome["event_order"], "trigger_first")
        self.assertEqual(outcome["patience_status"], "triggered_then_matured")
        self.assertTrue(outcome["patience_success"])
        self.assertIsNone(outcome["directional_success"])

    def test_unresolved_watch_remains_waiting(self):
        bars = path_history(sessions=8, step=0)
        entry = {
            "ts": "2026-01-02T10:00:00",
            "price": 100,
            "rule_action": "hold_off",
            "trigger_price": 110,
            "invalidation_price": 90,
        }
        outcome = engine_evaluation.score_forward_outcome(entry, bars, as_of=date(2026, 2, 1))
        self.assertEqual(outcome["patience_status"], "waiting")
        self.assertIsNone(outcome["patience_success"])

    def test_summarizes_only_forward_scored_rows(self):
        rows = [
            {"outcome": {"forward_return_pct": 6, "excess_return_pct": 2, "mfe_pct": 8, "mae_pct": -2, "credited": True}},
            {"outcome": {"forward_return_pct": -2, "excess_return_pct": -3, "mfe_pct": 1, "mae_pct": -5, "credited": False}},
            {"outcome": None},
        ]
        summary = engine_evaluation.summarize_outcomes(rows)
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["hit_rate_pct"], 50.0)
        self.assertEqual(summary["avg_return_pct"], 2.0)
        self.assertEqual(summary["avg_excess_return_pct"], -0.5)

    def test_independent_cohorts_reduce_correlated_repeat_logs(self):
        rows = [
            {"ticker": "NVDA", "ts": "2026-01-01T09:00:00", "rule_action": "enter_now"},
            {"ticker": "NVDA", "ts": "2026-01-03T09:00:00", "rule_action": "enter_now"},
            {"ticker": "NVDA", "ts": "2026-01-04T09:00:00", "rule_action": "avoid"},
            {"ticker": "NVDA", "ts": "2026-01-09T09:00:00", "rule_action": "enter_now"},
            {"ticker": "AAPL", "ts": "2026-01-03T09:00:00", "rule_action": "enter_now"},
        ]
        cohorts = engine_evaluation.independent_cohorts(rows, spacing_days=7)
        self.assertEqual(cohorts, [rows[0], rows[4], rows[2], rows[3]])


if __name__ == "__main__":
    unittest.main()
