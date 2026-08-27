import unittest
from datetime import date

import notification_engine


class NotificationEngineTests(unittest.TestCase):
    def test_digest_only_includes_high_and_critical_events(self):
        digest = notification_engine.build_digest(
            "u1",
            [
                {"event_id": "a", "priority": "high", "ticker": "AAPL"},
                {"event_id": "b", "priority": "medium", "ticker": "MSFT"},
            ],
            day=date(2026, 8, 27),
        )
        self.assertEqual(digest["count"], 1)
        self.assertEqual(digest["events"][0]["ticker"], "AAPL")

    def test_digest_key_is_idempotent(self):
        events = [{"event_id": "a", "priority": "critical"}]
        first = notification_engine.build_digest("u1", events, day=date(2026, 8, 27))
        second = notification_engine.build_digest("u1", events, day=date(2026, 8, 27))
        self.assertEqual(first["digest_key"], second["digest_key"])

    def test_empty_digest_is_not_sent(self):
        self.assertFalse(notification_engine.build_digest("u1", [])["should_send"])


if __name__ == "__main__":
    unittest.main()
