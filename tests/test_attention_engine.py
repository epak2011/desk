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


if __name__ == "__main__":
    unittest.main()
