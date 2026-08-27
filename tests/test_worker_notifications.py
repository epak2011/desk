import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import email_delivery
import worker


class WorkerNotificationTests(unittest.TestCase):
    @patch("worker.email_delivery.config_from_env")
    def test_disabled_delivery_does_not_claim_rows(self, config):
        config.return_value = email_delivery.DeliveryConfig(False, "", "")
        with patch("worker.backend.claim_notifications") as claim:
            result = worker.drain_notification_outbox()
        claim.assert_not_called()
        self.assertFalse(result["enabled"])

    @patch("worker.email_delivery.send_email", return_value="provider-1")
    @patch("worker.email_delivery.config_from_env")
    @patch("worker.backend.complete_notification")
    @patch("worker.backend.claim_notifications")
    def test_enabled_delivery_marks_claimed_row_sent(self, claim, complete, config, send):
        config.return_value = email_delivery.DeliveryConfig(True, "key", "desk@example.com")
        claim.return_value = [{
            "id": "row-1", "recipient": "user@example.com", "subject": "Daily",
            "html": "<p>One alert</p><a href='https://desk.example.com/unsubscribe'>Unsubscribe</a>", "attempts": 1,
        }]
        result = worker.drain_notification_outbox()
        complete.assert_called_once_with("row-1", "provider-1")
        self.assertEqual(result["sent"], 1)

    @patch.dict("os.environ", {
        "NOTIFICATIONS_ENABLED": "true",
        "RESEND_API_KEY": "key",
        "NOTIFICATION_FROM_EMAIL": "desk@example.com",
        "UNSUBSCRIBE_SECRET": "secret",
        "APP_BASE_URL": "https://desk.example.com",
        "DIGEST_SEND_HOUR_ET": "17",
    }, clear=False)
    @patch("worker.backend.enqueue_notification", return_value="row-1")
    @patch("worker.backend.notification_users")
    @patch("worker.backend.read_engine_review_status", return_value={})
    def test_daily_digest_queues_one_personalized_message(self, _review, users, enqueue):
        users.return_value = [{
            "user_id": "00000000-0000-0000-0000-000000000001",
            "state": {
                "watchlist": ["AAPL"],
                "holdings": {},
                "notification_preferences": {
                    "email": "user@example.com", "daily_digest": True, "delivery_enabled": True,
                },
                "ticker_snapshots": {
                    "AAPL": {
                        "market": {
                            "last": 200, "action": "enter_now",
                            "trigger_monitor": {"status": "fired", "detail": "Breakout held."},
                        },
                        "meta": {},
                        "decision_receipt": {},
                    }
                },
            },
        }]
        result = worker.queue_daily_user_digests(
            now=datetime(2026, 8, 27, 22, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(result["queued"], 1)
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(kwargs["recipient"], "user@example.com")
        self.assertIn("Unsubscribe", kwargs["html"])


if __name__ == "__main__":
    unittest.main()
