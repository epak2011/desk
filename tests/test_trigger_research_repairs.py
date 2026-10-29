import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import pm_view
import public_contract
from trigger_presentation import entry_trigger_display
from research_reliability import growth_percent_label


class TriggerResearchRepairTests(unittest.TestCase):
    def test_be_missing_level_is_not_current_price_trigger(self):
        receipt = {"action": "hold_off", "price": 228.08,
                   "trigger": {"text": "Watch trigger near $228.08.", "price": None}}
        shown = entry_trigger_display(receipt)
        self.assertEqual(shown["status"], "undefined")
        self.assertIsNone(shown["price"])
        self.assertNotIn("228", shown["text"])
        page = public_contract.analyze_page_payload("BE", rule={"decision_receipt": receipt})
        self.assertFalse(any(r["kind"] == "upgrade" for r in page["call_changes"]))

    def test_crossed_breakout_is_not_automatic_buy(self):
        r = {"action": "hold_off", "price": 228,
             "trigger": {"price": 220, "text": "Break above 220"},
             "primary_risk": "Reward/risk too low"}
        d = entry_trigger_display(r, {"trigger": {"kind": "breakout"}})
        self.assertEqual(d["status"], "level_reached")
        self.assertIn("blocked", d["text"])
        self.assertEqual(r["action"], "hold_off")

    def test_pullback_is_not_breakout(self):
        d = entry_trigger_display({"price": 228, "action": "watch",
            "trigger": {"price": 210, "text": "Pullback to support"}},
            {"trigger": {"kind": "pullback"}})
        self.assertEqual(d["status"], "defined")

    def test_market_entry_does_not_require_a_breakout_level(self):
        d = entry_trigger_display({"action": "enter_now", "price": 228,
                                  "trigger": {"text": "Enter at market", "price": None}})
        self.assertEqual(d["text"], "Enter at market")
        self.assertFalse(d["entry_blocked"])

    def test_growth_fraction_units_zero_and_missing(self):
        for value, expected in [(0.10, "+10.00%"), (-0.30, "-30.00%"),
                                (0, "+0.00%"), (None, "unavailable"), (float("nan"), "unavailable")]:
            self.assertEqual(growth_percent_label(value), expected)

    def test_repair_receives_schema_evidence_and_uses_second_model(self):
        valid = {"dossier": "Memo", "technical_narrative": "Technical",
                 "pm_narrative": "Business", "bullets": {"thesis": "Thesis",
                 "valuation": "Valuation", "timing_watchpoint": "Next date unconfirmed",
                 "drivers": ["a", "b", "c"], "risks": ["a", "b", "c"]},
                 "quality": {"tier": "B", "rationale": "Reason"},
                 "tactical_call": {"action": "WATCH", "reasoning": "Reason"}}
        def message(payload):
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload))], stop_reason="end_turn")
        with patch.object(pm_view, "_generate_independent_assessment", return_value={}), \
             patch.object(pm_view, "_messages_create_fast", return_value=message({})) as fast, \
             patch.object(pm_view, "_messages_create", return_value=message(valid)) as repair:
            result = pm_view.get_decision_dossier("BE", {"price": 228, "action": "hold_off"},
                [], {"revenue_growth": .10}, {}, api_key="synthetic-not-sent", fast=True)
        self.assertEqual(pm_view._dossier_contract_issues(result), [])
        self.assertEqual(fast.call_count, 1)
        self.assertEqual(repair.call_count, 1)
        prompt = repair.call_args.kwargs["messages"][0]["content"]
        self.assertIn('"timing_watchpoint"', prompt)
        self.assertIn("+10.00%", prompt)
        self.assertIn("$228.00", prompt)
