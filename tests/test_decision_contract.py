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

    def test_consistency_audit_checks_trigger_and_invalidation_levels(self):
        mismatches = decision_contract.receipt_consistency("NVDA", {
            "receipt": {
                "action": "watch", "engine_version": "v2",
                "trigger": {"price": 110}, "invalidation": {"price": 95},
            },
            "watchlist": {
                "action": "watch", "engine_version": "v2",
                "trigger_price": 111, "invalidation_price": 94,
            },
        })
        self.assertEqual({row["kind"] for row in mismatches}, {"trigger_price", "invalidation_price"})

    def test_attribution_records_gates_and_decisive_step(self):
        result = decision_contract.build_rule_attribution({
            "action": "watch",
            "extension_overlay_applied": True,
            "reward_risk_gate": True,
            "_rule_trace": [{"label": "Extension", "detail": "Wait for a base", "action": "watch"}],
        })
        self.assertEqual(result["final_action"], "watch")
        self.assertEqual(result["decisive_step"]["label"], "Extension")
        self.assertEqual(result["active_gates"], ["reward_risk", "stretched_momentum"])


if __name__ == "__main__":
    unittest.main()
