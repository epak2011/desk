import unittest

from public_recovery import regime_confidence


COMPLETE = {
    "spx": 7000,
    "spx_vs20": 1.2,
    "spx_vs50": 3.1,
    "vix": 17,
    "qqq": 600,
    "hy_bps": 310,
    "fg": 58,
    "ism": 51,
    "unemp": 4.1,
    "yc_bps": 42,
}


class RegimeConfidenceTests(unittest.TestCase):
    def test_complete_inputs_are_trusted(self):
        self.assertEqual(regime_confidence(COMPLETE)["state"], "trusted")

    def test_missing_confirmation_is_degraded(self):
        data = {**COMPLETE, "hy_bps": None}
        result = regime_confidence(data)
        self.assertEqual(result["state"], "degraded")
        self.assertIn("high-yield spreads", result["missing"])

    def test_missing_core_input_is_blocked(self):
        data = {**COMPLETE, "vix": None}
        result = regime_confidence(data)
        self.assertEqual(result["state"], "blocked")
        self.assertIn("VIX", result["missing"])


if __name__ == "__main__":
    unittest.main()
