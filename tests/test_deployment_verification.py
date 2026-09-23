import unittest
from unittest import mock

from scripts import verify_deployment


class DeploymentVerificationTests(unittest.TestCase):
    @mock.patch("scripts.verify_deployment.fetch_json")
    def test_verify_requires_matching_revision_and_complete_pages(self, fetch):
        fetch.side_effect = [
            {"deployment_revision": "abcdef123456"},
            {
                "contract_version": 2,
                "contract_fingerprint": "fingerprint",
                "pages": [{"key": "market", "status": "shared", "sections": ["outlook"], "response_keys": ["regime"]}],
            },
            {"regime": {}},
            {"decision": {}, "security_profile": {}, "research": {}, "analyze_page": {}},
        ]
        result = verify_deployment.verify("https://example.com", "abcdef123456789")
        self.assertEqual(result["revision"], "abcdef123456")

    @mock.patch("scripts.verify_deployment.fetch_json")
    def test_verify_rejects_partial_page_contract(self, fetch):
        fetch.side_effect = [
            {"deployment_revision": "abcdef123456"},
            {"contract_fingerprint": "x", "pages": [{"key": "today", "status": "partial", "sections": ["x"], "response_keys": ["x"]}]},
        ]
        with self.assertRaises(RuntimeError):
            verify_deployment.verify("https://example.com", "abcdef123456")


if __name__ == "__main__":
    unittest.main()
