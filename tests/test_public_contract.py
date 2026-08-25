import unittest

from public_contract import attention_payload, decision_payload


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

    def test_attention_payload_has_stable_shape_without_private_fields(self):
        payload = attention_payload(
            [{"event_id": "x", "ticker": "MSFT", "priority": "high", "private": 1}]
        )
        self.assertEqual(payload["count"], 1)
        self.assertNotIn("private", payload["events"][0])


if __name__ == "__main__":
    unittest.main()
