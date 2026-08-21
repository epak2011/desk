import unittest
from datetime import date

import pandas as pd

import engine_evaluation


def history(rows):
    return pd.DataFrame(
        rows,
        index=pd.to_datetime([row.pop("date") for row in rows]),
    )


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

    def test_scores_first_close_at_or_after_target_date(self):
        bars = history([
            {"date": "2026-01-02", "Close": 100, "High": 101, "Low": 99},
            {"date": "2026-01-15", "Close": 105, "High": 108, "Low": 97},
            {"date": "2026-01-16", "Close": 110, "High": 112, "Low": 96},
            {"date": "2026-01-20", "Close": 150, "High": 151, "Low": 109},
        ])
        spy = history([
            {"date": "2026-01-02", "Close": 500, "High": 501, "Low": 499},
            {"date": "2026-01-16", "Close": 525, "High": 526, "Low": 498},
        ])
        entry = {"ts": "2026-01-02T10:00:00", "price": 100, "rule_action": "enter_now"}
        outcome = engine_evaluation.score_forward_outcome(
            entry,
            bars,
            benchmark_history=spy,
            as_of=date(2026, 1, 20),
        )
        self.assertEqual(outcome["scored_date"], "2026-01-16")
        self.assertEqual(outcome["forward_return_pct"], 10.0)
        self.assertEqual(outcome["benchmark_return_pct"], 5.0)
        self.assertEqual(outcome["excess_return_pct"], 5.0)
        self.assertEqual(outcome["decision_return_pct"], 10.0)
        self.assertEqual(outcome["decision_excess_pct"], 5.0)
        self.assertEqual(outcome["mfe_pct"], 12.0)
        self.assertEqual(outcome["mae_pct"], -4.0)
        self.assertTrue(outcome["credited"])

    def test_avoid_decision_edge_inverts_underlying_return(self):
        bars = history([
            {"date": "2026-01-02", "Close": 100, "High": 101, "Low": 99},
            {"date": "2026-01-16", "Close": 110, "High": 112, "Low": 98},
        ])
        entry = {"ts": "2026-01-02T10:00:00", "price": 100, "rule_action": "avoid"}
        outcome = engine_evaluation.score_forward_outcome(
            entry, bars, as_of=date(2026, 1, 16)
        )
        self.assertEqual(outcome["forward_return_pct"], 10.0)
        self.assertEqual(outcome["decision_return_pct"], -10.0)
        self.assertFalse(outcome["credited"])

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
