import unittest

from crypto_regime import classify_cycle


class CryptoRegimeTests(unittest.TestCase):
    def test_deep_drawdown_rebound_is_accumulation_not_parabolic(self):
        cycle = classify_cycle(btc_vs_200=11.7, btc_vs_20=6.1, drawdown_cycle=-38.0, return_90=9.0, fear_greed=69)
        self.assertEqual(cycle[:2], ("Phase 1", "Accumulation / repair"))

    def test_parabolic_requires_high_proximity_and_acceleration(self):
        cycle = classify_cycle(btc_vs_200=28.0, btc_vs_20=12.0, drawdown_cycle=-3.0, return_90=42.0, fear_greed=82)
        self.assertEqual(cycle[:2], ("Phase 3", "Parabolic bull"))

    def test_above_200_without_confirmation_is_recovery(self):
        cycle = classify_cycle(btc_vs_200=9.0, btc_vs_20=3.0, drawdown_cycle=-14.0, return_90=12.0, fear_greed=60)
        self.assertEqual(cycle[:2], ("Phase 2", "Recovery / expansion"))


if __name__ == "__main__":
    unittest.main()
