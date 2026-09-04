import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendContractAssetTests(unittest.TestCase):
    def test_all_examples_are_valid_json_and_version_two(self):
        examples = sorted((ROOT / "contracts" / "examples").glob("*.json"))
        self.assertEqual({path.name for path in examples}, {"decision.json", "regime.json", "workspace.json"})
        for path in examples:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["contract_version"], 2, path.name)
            self.assertEqual(payload["meta"]["contract_version"], 2, path.name)

    def test_openapi_declares_required_frontend_routes_and_auth(self):
        spec = (ROOT / "contracts" / "openapi.yaml").read_text(encoding="utf-8")
        for route in (
            "/v1/health:",
            "/v1/regime:",
            "/v1/decisions/{ticker}:",
            "/v1/workspace:",
            "/v1/watchlist:",
            "/v1/attention:",
            "/v1/portfolio:",
            "/v1/calibration:",
        ):
            self.assertIn(route, spec)
        self.assertIn("supabaseBearer", spec)
        self.assertIn("const: 2", spec)

    def test_handoff_contains_safety_and_build_boundaries(self):
        handoff = (ROOT / "LOVABLE_HANDOFF.md").read_text(encoding="utf-8")
        self.assertIn("frontend must never calculate", handoff)
        self.assertIn("No beige", handoff)
        self.assertIn("row-level security", handoff)
        self.assertIn("Copy-paste prompt for Lovable", handoff)


if __name__ == "__main__":
    unittest.main()
