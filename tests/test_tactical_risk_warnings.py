import unittest

import tactical


class ExtensionMomentumWarningTests(unittest.TestCase):
    def test_extreme_extension_and_hot_rsi_raise_high_warning(self):
        warning = tactical.extension_momentum_warning(
            {"price": 125, "ma20": 110, "ma50": 104, "rsi14": 82}
        )

        self.assertEqual(warning["kind"], "extension_momentum")
        self.assertEqual(warning["severity"], "high")
        self.assertIn("Avoid chasing full size", warning["text"])

    def test_moderate_extension_and_hot_rsi_raise_medium_warning(self):
        warning = tactical.extension_momentum_warning(
            {"price": 115, "ma20": 106, "ma50": 101, "rsi14": 76}
        )

        self.assertEqual(warning["severity"], "med")

    def test_extension_without_hot_rsi_does_not_warn(self):
        warning = tactical.extension_momentum_warning(
            {"price": 125, "ma20": 110, "ma50": 104, "rsi14": 68}
        )

        self.assertIsNone(warning)

    def test_hot_rsi_without_extension_does_not_warn(self):
        warning = tactical.extension_momentum_warning(
            {"price": 105, "ma20": 101, "ma50": 100, "rsi14": 81}
        )

        self.assertIsNone(warning)


if __name__ == "__main__":
    unittest.main()
