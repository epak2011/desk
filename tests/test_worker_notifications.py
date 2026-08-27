import unittest
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
            "html": "<p>One alert</p>", "attempts": 1,
        }]
        result = worker.drain_notification_outbox()
        complete.assert_called_once_with("row-1", "provider-1")
        self.assertEqual(result["sent"], 1)


if __name__ == "__main__":
    unittest.main()
