import unittest

import data_trust


class DataTrustTests(unittest.TestCase):
    def complete_state(self):
        return {"price": 100, "action": "watch", "ma50": 95, "ma200": 80, "setup_score": 7, "rs": 1.1, "vol_ratio": 1, "reward_risk": 1.5}

    def test_complete_live_state_is_trusted(self):
        self.assertEqual(data_trust.assess_decision_data(self.complete_state())["status"], "trusted")

    def test_stale_price_blocks_execution(self):
        result = data_trust.assess_decision_data(self.complete_state(), price_age_kind="stale")
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["executable"])

    def test_missing_secondary_context_degrades(self):
        state = self.complete_state()
        state["vol_ratio"] = None
        result = data_trust.assess_decision_data(state)
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["executable"])


if __name__ == "__main__":
    unittest.main()
