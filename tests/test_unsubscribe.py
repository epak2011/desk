import unittest
from unittest.mock import patch

import unsubscribe


class UnsubscribeTests(unittest.TestCase):
    @patch("unsubscribe.time.time", return_value=1000)
    def test_signed_token_round_trip(self, _time):
        token = unsubscribe.create_token("user-1", "User@Example.com", "secret", expires_in_days=1)
        payload = unsubscribe.verify_token(token, "secret", now=1001)
        self.assertEqual(payload["email"], "user@example.com")

    @patch("unsubscribe.time.time", return_value=1000)
    def test_tampered_or_expired_token_fails(self, _time):
        token = unsubscribe.create_token("user-1", "user@example.com", "secret", expires_in_days=1)
        with self.assertRaises(ValueError):
            unsubscribe.verify_token(token + "x", "secret", now=1001)
        with self.assertRaises(ValueError):
            unsubscribe.verify_token(token, "secret", now=1000 + 86401)


if __name__ == "__main__":
    unittest.main()
