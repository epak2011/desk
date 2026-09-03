import unittest

import attention_engine


class AttentionEngineTests(unittest.TestCase):
    def test_prioritizes_breach_over_near_trigger(self):
        rows = [
            {"ticker": "A", "action": "watch", "price": 90, "invalidation_price": 95},
            {"ticker": "B", "action": "watch", "trigger_status": "near", "distance_pct": 2},
        ]
        events = attention_engine.build_attention_events(rows)
        self.assertEqual(events[0]["kind"], "invalidation")
        self.assertEqual(events[1]["kind"], "near_trigger")

    def test_owned_avoid_creates_position_review(self):
        events = attention_engine.build_attention_events(
            [{"ticker": "AAPL", "action": "avoid"}], holdings=["AAPL"]
        )
        self.assertIn("position_review", {event["kind"] for event in events})

    def test_logic_review_is_critical(self):
        events = attention_engine.build_attention_events([], logic_alerts=[{
            "status": "review_logic", "label": "Avoid", "count": 20,
            "success_rate_pct": 20, "avg_decision_return_pct": -5,
        }])
        self.assertEqual(events[0]["priority"], "critical")

    def test_old_fired_trigger_is_removed_from_inbox(self):
        events = attention_engine.build_attention_events([{
            "ticker": "PLTR", "action": "watch", "market_fresh": True,
            "trigger_status": "fired", "trigger_sessions_ago": 4,
            "trigger_detail": "Prior breakout fired 4 sessions ago.",
        }])
        self.assertNotIn("trigger_fired", {event["kind"] for event in events})

    def test_recent_fired_trigger_remains_actionable(self):
        events = attention_engine.build_attention_events([{
            "ticker": "PLTR", "action": "watch", "market_fresh": True,
            "trigger_status": "fired", "trigger_sessions_ago": 1,
        }])
        self.assertIn("trigger_fired", {event["kind"] for event in events})

    def test_stale_market_does_not_emit_price_events(self):
        events = attention_engine.build_attention_events([{
            "ticker": "AAPL", "action": "watch", "market_fresh": False,
            "price": 90, "invalidation_price": 95, "trigger_status": "near",
        }])
        self.assertEqual(events, [])

    def test_old_action_change_is_removed(self):
        events = attention_engine.build_attention_events([{
            "ticker": "PLTR", "action": "watch", "receipt_age_days": 3,
            "receipt": {"change_summary": ["Action changed from enter now to watch."]},
        }])
        self.assertNotIn("action_change", {event["kind"] for event in events})

    def test_contract_mismatches_are_collapsed_per_ticker(self):
        mismatches = [
            {"ticker": "PLTR", "surface": "sidebar", "observed": "enter_now", "expected": "watch"},
            {"ticker": "PLTR", "surface": "market", "observed": "enter_now", "expected": "watch"},
        ]
        events = attention_engine.build_attention_events([], contract_mismatches=mismatches)
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
