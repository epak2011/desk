import unittest
from unittest.mock import MagicMock, patch

import email_delivery


class EmailDeliveryTests(unittest.TestCase):
    def test_delivery_fails_closed_without_complete_configuration(self):
        config = email_delivery.DeliveryConfig(enabled=True, api_key="", from_email="desk@example.com")
        with self.assertRaises(email_delivery.DeliveryError):
            email_delivery.send_email(
                recipient="user@example.com", subject="Test", html="<p>Test</p>", config=config
            )

    @patch("email_delivery.urllib.request.urlopen")
    def test_provider_id_is_required_and_returned(self, urlopen):
        response = MagicMock()
        response.read.return_value = b'{"id":"provider-1"}'
        urlopen.return_value.__enter__.return_value = response
        config = email_delivery.DeliveryConfig(enabled=True, api_key="key", from_email="desk@example.com")
        result = email_delivery.send_email(
            recipient="user@example.com", subject="Test", html="<p>Test</p>", config=config
        )
        self.assertEqual(result, "provider-1")


if __name__ == "__main__":
    unittest.main()
