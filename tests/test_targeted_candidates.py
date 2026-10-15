import engine_candidates as candidates
import engine_evaluation as evaluation
import unittest
import pandas as pd
import tactical
import decision_contract


def candidate(state, name):
    return next(row for row in candidates.shadow_evaluations(state) if row["candidate"] == name)


def test_weak_trend_is_wait_not_avoid_without_mutating_live():
    state = dict(action="avoid", price=97, ma50=99, ma200=100,
                 rs=.85, rs_delta=0, tech_delta=-1)
    row = candidate(state, "confirmed_structural_avoid")
    assert row["action"] == "hold_off"
    assert state["action"] == "avoid"
    result = evaluation.shadow_outcomes({"shadow_evaluations": [row]},
                                       {"forward_return_pct": 10})[0]
    assert result["decision_return_pct"] == 0
    assert result["directional_success"] is None


def test_missing_confirmation_is_not_backfilled_as_false():
    row = candidate(dict(action="enter"), "confirmed_entry_hold")
    assert row["eligible"] is False
    assert evaluation.shadow_outcomes({"shadow_evaluations": [row]},
                                      {"forward_return_pct": -10}) == []


def test_confirmation_requires_both_trigger_and_hold():
    row = candidate(dict(action="enter", entry_trigger_fired=True,
                         entry_level_held=True), "confirmed_entry_hold")
    assert row["action"] == "enter_now"
    assert row["eligible"] is True


def test_paired_cash_comparison_includes_filtered_losses_and_winners():
    entries = []
    for forward in (-10, 6):
        row = candidate(dict(action="enter", entry_trigger_fired=False,
                             entry_level_held=False), "confirmed_entry_hold")
        entries.append(dict(rule_action="enter", shadow_evaluations=[row],
                            outcome=dict(forward_return_pct=forward)))
    result = evaluation.paired_shadow_comparison(entries)[0]
    assert result["eligible_count"] == 2
    assert result["original_avg_policy_return_pct"] == -2
    assert result["candidate_avg_policy_return_pct"] == 0
    assert result["paired_lift_pct"] == 2


def test_avoid_relabel_does_not_create_short_trade_profit():
    row = candidate(dict(action="avoid", price=97, ma50=99, ma200=100,
                         rs=.85, rs_delta=0, tech_delta=-1), "confirmed_structural_avoid")
    result = evaluation.paired_shadow_comparison([dict(rule_action="avoid",
        shadow_evaluations=[row], outcome=dict(forward_return_pct=40))])[0]
    assert result["paired_lift_pct"] == 0


class TargetedCandidateTests(unittest.TestCase):
    test_weak_trend = staticmethod(test_weak_trend_is_wait_not_avoid_without_mutating_live)
    test_unknown = staticmethod(test_missing_confirmation_is_not_backfilled_as_false)
    test_confirmed = staticmethod(test_confirmation_requires_both_trigger_and_hold)
    test_paired = staticmethod(test_paired_cash_comparison_includes_filtered_losses_and_winners)
    test_no_synthetic_short = staticmethod(test_avoid_relabel_does_not_create_short_trade_profit)

    def test_completed_closes_exclude_current_bar(self):
        history = pd.DataFrame({"Close": [101, 102, 90]},
                               index=pd.to_datetime(["2026-10-12", "2026-10-13", "2026-10-14"]))
        state = tactical.shadow_entry_confirmation(history, {"levels": {"buy_above": 100}},
                                                    True, as_of="2026-10-14")
        self.assertTrue(state["entry_level_held"])
        snapshot = decision_contract.build_input_snapshot(state)
        replay = candidates.decision_state_from_log({"decision_inputs": snapshot})
        self.assertIs(replay["entry_level_held"], True)

    def test_unknown_coverage_has_no_returns(self):
        row = candidate(dict(action="enter"), "confirmed_entry_hold")
        result = evaluation.paired_shadow_comparison([dict(rule_action="enter",
            shadow_evaluations=[row], outcome=dict(forward_return_pct=-20))])[0]
        self.assertEqual(result["unknown_count"], 1)
        self.assertIsNone(result["paired_lift_pct"])
        self.assertNotIn("original", result)
