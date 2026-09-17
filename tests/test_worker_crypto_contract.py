import unittest

import pandas as pd

import worker


class WorkerCryptoContractTests(unittest.TestCase):
    def test_deep_drawdown_recovery_is_not_phase_two(self):
        prices = [120.0] * 100 + [65.0] * 100 + [float(65 + i * 0.06) for i in range(200)]
        frame = pd.DataFrame({"Close": prices})

        result = worker._crypto_regime_snapshot(frame)

        self.assertEqual(result["cycle"]["phase"], "Phase 1")
        self.assertEqual(result["cycle"]["label"], "Accumulation / repair")
        self.assertIn("medium_term_trend", result)
        self.assertIn("tactical_timing", result)
        self.assertEqual(len(result["phases"]), 4)
        self.assertEqual([row["heading"] for row in result["narrative"]],
                         ["Trend", "Opportunity", "Positioning", "Conviction"])
        self.assertIn("opportunity", result)
        self.assertIn("positioning", result)
        self.assertIn("btc_return_20d_pct", result["metrics"])

    def test_missing_history_returns_no_assessment(self):
        self.assertEqual(worker._crypto_regime_snapshot(pd.DataFrame()), {})


if __name__ == "__main__":
    unittest.main()
