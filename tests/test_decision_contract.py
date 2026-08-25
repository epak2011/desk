import unittest

import decision_contract


class DecisionContractTests(unittest.TestCase):
    def test_receipt_captures_action_evidence_and_levels(self):
        receipt = decision_contract.build_decision_receipt(
            "nvda",
            {
                "action": "enter_now",
                "price": 100,
                "setup_score": 8.4,
                "market_regime": "bullish",
                "_rule_trace": [
                    {"label": "Base rules", "detail": "Strong trend"},
                    {"label": "Decision matrix", "detail": "Trigger fired and held"},
                ],
                "trigger": {"summary": "Breakout", "levels": {"buy_above": 99, "abort_below": 94}},
            },
            engine_version="rules-test",
            captured_at="2026-01-01T12:00:00Z",
        )
        self.assertEqual(receipt["ticker"], "NVDA")
        self.assertEqual(receipt["action"], "enter_now")
        self.assertEqual(receipt["trigger"]["price"], 99)
        self.assertEqual(receipt["invalidation"]["price"], 94)
        self.assertEqual(receipt["top_factors"], ["Strong trend", "Trigger fired and held"])
        self.assertEqual(len(receipt["receipt_id"]), 16)

    def test_receipt_reports_material_change(self):
        prior = {"action": "watch", "trigger": {"price": 105}}
        receipt = decision_contract.build_decision_receipt(
            "AAPL",
            {"action": "enter_now", "price": 106, "trigger_price": 106},
            engine_version="rules-test",
            previous=prior,
        )
        self.assertEqual(len(receipt["change_summary"]), 2)

    def test_consistency_audit_checks_action_and_version(self):
        mismatches = decision_contract.receipt_consistency("AAPL", {
            "receipt": {"action": "watch", "engine_version": "v2"},
            "sidebar": {"action": "avoid", "engine_version": "v2"},
            "watchlist": {"action": "watch", "engine_version": "v1"},
        })
        self.assertEqual({row["kind"] for row in mismatches}, {"action", "engine_version"})


if __name__ == "__main__":
    unittest.main()
