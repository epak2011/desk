import unittest

from public_contract import (
    PUBLIC_CONTRACT_VERSION,
    attention_payload,
    decision_payload,
    error_payload,
    regime_payload,
    user_workspace_payload,
    watchlist_payload,
)


class PublicContractTests(unittest.TestCase):
    def test_decision_payload_uses_receipt_and_blocks_untrusted_execution(self):
        payload = decision_payload(
            {
                "receipt_id": "abc",
                "ticker": "AAPL",
                "action": "Enter",
                "data_trust": {"status": "blocked", "executable": False},
                "private_note": "never expose",
            },
            portfolio_context={"suggested_size_pct": 2.5},
        )
        self.assertEqual(payload["decision"]["action"], "Enter")
        self.assertFalse(payload["executable"])
        self.assertNotIn("private_note", payload["decision"])
        self.assertEqual(payload["meta"]["contract_version"], PUBLIC_CONTRACT_VERSION)

    def test_attention_payload_has_stable_shape_without_private_fields(self):
        payload = attention_payload(
            [{"event_id": "x", "ticker": "MSFT", "priority": "high", "private": 1}]
        )
        self.assertEqual(payload["count"], 1)
        self.assertNotIn("private", payload["events"][0])

    def test_regime_payload_allowlists_fields(self):
        payload = regime_payload({"why_today": "Mixed tape.", "database_url": "secret"})
        self.assertEqual(payload["regime"]["why_today"], "Mixed tape.")
        self.assertNotIn("database_url", payload["regime"])

    def test_watchlist_payload_blocks_private_notes(self):
        payload = watchlist_payload([{"ticker": "NVDA", "action": "watch", "private_note": "x"}])
        self.assertEqual(payload["count"], 1)
        self.assertNotIn("private_note", payload["items"][0])

    def test_workspace_payload_exposes_only_user_owned_sections(self):
        payload = user_workspace_payload({"watchlist": ["AAPL"], "api_key": "secret"})
        self.assertEqual(payload["workspace"]["watchlist"], ["AAPL"])
        self.assertNotIn("api_key", payload["workspace"])

    def test_error_payload_is_stable_and_safe(self):
        payload = error_payload("data_stale", "Refresh required.", retryable=True, request_id="req-1")
        self.assertEqual(payload["error"]["code"], "data_stale")
        self.assertTrue(payload["error"]["retryable"])
        self.assertEqual(payload["meta"]["request_id"], "req-1")


if __name__ == "__main__":
    unittest.main()
