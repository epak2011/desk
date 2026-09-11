import unittest

from rules_updates import ENGINE_UPDATES_CONTRACT_VERSION, RULES_UPDATES, engine_updates_payload


class RulesUpdatesTests(unittest.TestCase):
    def test_payload_is_versioned_and_serializable_shape(self):
        payload = engine_updates_payload()
        self.assertEqual(payload["contract_version"], ENGINE_UPDATES_CONTRACT_VERSION)
        self.assertEqual(payload["count"], len(RULES_UPDATES))
        self.assertTrue(payload["generated_at"])
        self.assertIsInstance(payload["updates"], list)
        self.assertIsInstance(payload["updates"][0]["changes"], list)

    def test_proposed_work_is_not_presented_as_deployed(self):
        proposed = next(item for item in engine_updates_payload()["updates"] if item["status"] == "Proposed")
        self.assertIn("required before production", proposed["validation"])


if __name__ == "__main__":
    unittest.main()
