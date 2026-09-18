import unittest

import pandas as pd

import engine_candidates
import tactical


class EngineCandidateTests(unittest.TestCase):
    def test_lower_lows_reduce_candidate_structure_quality(self):
        closes = [10, 12, 9, 12, 8, 12, 7, 12, 6, 12, 10]
        hist = pd.DataFrame({"Close": closes})
        self.assertEqual(tactical.structure_quality(hist), 5.0)
        self.assertEqual(tactical.structure_quality_candidate(hist), 3.0)

    def test_strict_broken_precedes_transition_zone(self):
        result = engine_candidates.structural_state({
            "price": 82, "ma50": 90, "ma200": 100,
            "rs": 0.80, "rs_delta": 0.0, "tech_delta": -1,
        })
        self.assertEqual(result["state"], "BROKEN")

    def test_near_ma200_without_confirmation_is_weakening_not_recovering(self):
        result = engine_candidates.structural_state({
            "price": 90, "ma50": 95, "ma200": 100,
            "rs": 0.92, "rs_delta": 0.0, "tech_delta": -1,
        })
        self.assertEqual(result, {
            "state": "TRANSITION",
            "phase": "weakening",
            "reason": "Structure is below its long-term trend without enough deterioration to qualify as broken.",
        })

    def test_structural_candidate_removes_trending_avoid_contradiction(self):
        rows = engine_candidates.shadow_evaluations({
            "action": "avoid", "state": "TRENDING",
            "price": 97, "ma50": 99, "ma100": 100, "ma200": 100,
            "rs": 0.88, "rs_delta": 0.0, "tech_delta": -1,
        })
        candidate = next(row for row in rows if row["candidate"] == "unified_structural_state")
        self.assertEqual(candidate["state"], "TRANSITION")
        self.assertEqual(candidate["action"], "hold_off")
        self.assertTrue(candidate["differs_from_live"])

    def test_candidates_never_mutate_live_state(self):
        state = {
            "action": "enter_now", "price": 125, "ma50": 100, "ma100": 105,
            "ma200": 90, "rs": 1.2, "rs_delta": 0.02, "tech_delta": 1,
            "extension_warning": {"severity": "high"},
        }
        original = dict(state)
        rows = engine_candidates.shadow_evaluations(state)
        self.assertEqual(state, original)
        self.assertEqual({row["candidate"] for row in rows}, {
            "strict_extreme_extension", "unified_structural_state", "no_momentum_exception",
            "entry_timing_guard", "regime_quality_gate",
        })

    def test_entry_timing_guard_shadows_medium_extension_without_changing_live_action(self):
        state = {
            "action": "enter_now", "market_regime": "Favorable",
            "setup_score": 9, "reward_risk": 2.5,
            "extension_warning": {"severity": "med"},
        }
        candidate = next(
            row for row in engine_candidates.shadow_evaluations(state)
            if row["candidate"] == "entry_timing_guard"
        )
        self.assertEqual(candidate["action"], "watch")
        self.assertEqual(candidate["evaluation_mode"], "avoided_long_exposure")
        self.assertEqual(state["action"], "enter_now")

    def test_regime_quality_gate_requires_both_quality_thresholds(self):
        weak = next(
            row for row in engine_candidates.shadow_evaluations({
                "action": "accumulate", "market_regime": "Mixed",
                "setup_score": 8.5, "reward_risk": 1.6,
            }) if row["candidate"] == "regime_quality_gate"
        )
        strong = next(
            row for row in engine_candidates.shadow_evaluations({
                "action": "accumulate", "market_regime": "Mixed",
                "setup_score": 8.5, "reward_risk": 2.1,
            }) if row["candidate"] == "regime_quality_gate"
        )
        self.assertEqual(weak["action"], "watch")
        self.assertEqual(strong["action"], "accumulate")

    def test_historical_state_uses_frozen_inputs_not_current_values(self):
        state = engine_candidates.decision_state_from_log({
            "rule_action": "avoid",
            "setup_score": 2,
            "decision_inputs": {
                "action": "enter_now", "state": "TRENDING", "market_regime": "Mixed",
                "inputs": {"price": 110, "ma50": 100, "ma100": 95, "ma200": 90, "setup_score": 8.5, "reward_risk": 1.5},
            },
            "decision_context": {"extension_warning": True, "extension_warning_severity": "med"},
        })
        self.assertEqual(state["action"], "enter_now")
        self.assertEqual(state["setup_score"], 8.5)
        self.assertEqual(state["extension_warning"]["severity"], "med")


if __name__ == "__main__":
    unittest.main()
