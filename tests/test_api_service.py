import unittest
from unittest import mock

from fastapi.testclient import TestClient

import api_service
from api_auth import VerifiedIdentity


class ApiServiceTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api_service.app)

    def tearDown(self):
        api_service.app.dependency_overrides.clear()

    def test_health_is_public(self):
        with mock.patch.object(api_service.api_repository, "health", return_value={
            "status": "ok", "contract_version": 2, "engine_version": "saved",
        }):
            response = self.client.get("/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["contract_version"], 2)
        self.assertTrue(response.headers.get("X-Request-ID"))
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_public_decision_maps_not_found_to_contract_error(self):
        with mock.patch.object(
            api_service.api_repository,
            "decision",
            side_effect=api_service.api_repository.NotFoundError("No receipt."),
        ):
            response = self.client.get("/v1/decisions/DEMO")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_private_workspace_requires_bearer_token(self):
        response = self.client.get("/v1/workspace")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_private_workspace_uses_verified_identity(self):
        identity = VerifiedIdentity("trusted-user", "demo@example.invalid", "Demo")
        api_service.app.dependency_overrides[api_service.current_identity] = lambda: identity
        with mock.patch.object(
            api_service.api_repository,
            "workspace",
            return_value={"contract_version": 2, "workspace": {"watchlist": []}},
        ) as workspace:
            response = self.client.get("/v1/workspace")
        self.assertEqual(response.status_code, 200)
        workspace.assert_called_once_with("trusted-user")

    def test_cors_does_not_allow_arbitrary_origin(self):
        response = self.client.options(
            "/v1/regime",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertNotEqual(response.headers.get("access-control-allow-origin"), "https://untrusted.example")


if __name__ == "__main__":
    unittest.main()
