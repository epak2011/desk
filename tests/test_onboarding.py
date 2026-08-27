import unittest

import onboarding


class OnboardingTests(unittest.TestCase):
    def test_tickers_are_normalized_deduplicated_and_bounded(self):
        self.assertEqual(onboarding.parse_tickers("aapl, msft AAPL bad/$", limit=3), ["AAPL", "MSFT"])

    def test_delivery_starts_disabled(self):
        prefs = onboarding.notification_preferences(
            "User@Example.com ", daily_digest=True, high_priority=True
        )
        self.assertEqual(prefs["email"], "user@example.com")
        self.assertFalse(prefs["delivery_enabled"])


if __name__ == "__main__":
    unittest.main()
