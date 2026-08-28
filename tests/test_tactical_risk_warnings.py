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

    def test_moderate_extension_keeps_enter_and_caps_size(self):
        result = tactical.apply_extension_execution_overlay({
            "action": "enter_now", "entry_size": "full",
            "extension_warning": {"severity": "med"},
        })
        self.assertEqual(result["action"], "enter_now")
        self.assertEqual(result["entry_size"], "starter")
        self.assertEqual(result["entry_status"], "Staged entry")

    def test_extreme_extension_with_good_confirmation_becomes_accumulate(self):
        result = tactical.apply_extension_execution_overlay({
            "action": "enter_now", "entry_size": "full", "reward_risk": 1.8,
            "vol_ratio": 1.1, "rs_delta": 0.02, "tech_delta": 0.5,
            "extension_warning": {"severity": "high"},
        })
        self.assertEqual(result["action"], "accumulate")
        self.assertEqual(result["entry_size"], "starter")
        self.assertEqual(result["extension_pre_overlay_action"], "enter_now")

    def test_extreme_extension_with_thin_reward_risk_becomes_watch(self):
        result = tactical.apply_extension_execution_overlay({
            "action": "enter_now", "reward_risk": 1.4, "vol_ratio": 1.1,
            "rs_delta": 0.02, "tech_delta": 0.5,
            "extension_warning": {"severity": "high"},
        })
        self.assertEqual(result["action"], "watch")
        self.assertEqual(result["entry_size"], "none")
        self.assertIn("below 1.5:1", result["extension_overlay_reason"])

    def test_extreme_extension_with_weak_confirmation_becomes_watch(self):
        result = tactical.apply_extension_execution_overlay({
            "action": "enter_now", "reward_risk": 2.0, "vol_ratio": 0.8,
            "rs_delta": -0.01, "tech_delta": -0.5,
            "extension_warning": {"severity": "high"},
        })
        self.assertEqual(result["action"], "watch")
        self.assertIn("volume confirmation is weak", result["extension_overlay_reason"])

    def test_extension_overlay_does_not_change_non_entry_calls(self):
        result = tactical.apply_extension_execution_overlay({
            "action": "avoid", "entry_size": "none",
            "extension_warning": {"severity": "high"},
        })
        self.assertEqual(result["action"], "avoid")
        self.assertFalse(result.get("extension_overlay_applied", False))


if __name__ == "__main__":
    unittest.main()
