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
            "status": "ok", "contract_version": 2, "engine_version": "saved", "deployment_revision": "abc123",
        }):
            response = self.client.get("/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["contract_version"], 2)
        self.assertEqual(response.json()["deployment_revision"], "abc123")
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

    def test_analysis_request_requires_sign_in(self):
        response = self.client.post("/v1/decisions/GOOG/requests")
        self.assertEqual(response.status_code, 401)

    def test_analysis_request_queues_for_verified_user(self):
        identity = VerifiedIdentity("trusted-user", "demo@example.invalid", "Demo")
        api_service.app.dependency_overrides[api_service.current_identity] = lambda: identity
        with mock.patch.object(
            api_service.api_repository,
            "request_decision",
            return_value={"status": "queued", "ticker": "GOOG", "request_id": "job-1"},
        ) as request_decision:
            response = self.client.post("/v1/decisions/GOOG/requests")
        self.assertEqual(response.status_code, 202)
        request_decision.assert_called_once_with("GOOG", "trusted-user")

    def test_research_request_queues_for_verified_user(self):
        identity = VerifiedIdentity("trusted-user", "demo@example.invalid", "Demo")
        api_service.app.dependency_overrides[api_service.current_identity] = lambda: identity
        with mock.patch.object(
            api_service.api_repository,
            "request_research",
            return_value={"status": "queued", "ticker": "DASH", "request_id": "job-2"},
        ) as request_research:
            response = self.client.post("/v1/decisions/DASH/research/requests")
        self.assertEqual(response.status_code, 202)
        request_research.assert_called_once_with("DASH", "trusted-user")

    def test_cors_does_not_allow_arbitrary_origin(self):
        response = self.client.options(
            "/v1/regime",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertNotEqual(response.headers.get("access-control-allow-origin"), "https://untrusted.example")

    def test_engine_updates_requires_sign_in(self):
        response = self.client.get("/v1/operator/engine-updates")
        self.assertEqual(response.status_code, 401)

    def test_methodology_is_public(self):
        expected = {"contract_version": 2, "actions": [{"key": "watch"}]}
        with mock.patch.object(api_service.api_repository, "methodology", return_value=expected):
            response = self.client.get("/v1/methodology")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)

    def test_ideas_and_system_health_require_sign_in(self):
        self.assertEqual(self.client.get("/v1/ideas").status_code, 401)
        self.assertEqual(self.client.get("/v1/system-health").status_code, 401)

    def test_engine_updates_denies_non_owner(self):
        identity = VerifiedIdentity("trusted-user", "someone@example.invalid", "Someone")
        api_service.app.dependency_overrides[api_service.current_identity] = lambda: identity
        with mock.patch.dict(api_service.os.environ, {"TRADING_DESK_OWNER_EMAIL": "owner@example.invalid"}):
            response = self.client.get("/v1/operator/engine-updates")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "forbidden")

    def test_engine_updates_returns_canonical_feed_to_owner(self):
        identity = VerifiedIdentity("owner-user", "OWNER@example.invalid", "Owner")
        api_service.app.dependency_overrides[api_service.current_identity] = lambda: identity
        expected = {"contract_version": 1, "updates": [], "count": 0}
        with mock.patch.dict(api_service.os.environ, {"TRADING_DESK_OWNER_EMAIL": "owner@example.invalid"}):
            with mock.patch.object(api_service.api_repository, "engine_updates", return_value=expected) as updates:
                response = self.client.get("/v1/operator/engine-updates")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        updates.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
