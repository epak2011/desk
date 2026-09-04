import unittest
from unittest import mock

import api_repository


class ApiRepositoryTests(unittest.TestCase):
    def test_decision_returns_only_saved_canonical_receipt(self):
        rows = {"DEMO": {"decision_receipt": {
            "receipt_id": "r1", "ticker": "DEMO", "engine_version": "v1",
            "action": "watch", "trigger": {"price": 105}, "invalidation": {"price": 92},
            "private_note": "do not expose",
        }}}
        with mock.patch.object(api_repository.backend_layer, "read_json_table", return_value=rows):
            payload = api_repository.decision("demo")
        self.assertEqual(payload["decision"]["ticker"], "DEMO")
        self.assertNotIn("private_note", payload["decision"])

    def test_decision_never_fabricates_missing_receipt(self):
        with (
            mock.patch.object(api_repository.backend_layer, "read_json_table", return_value={"DEMO": {"action": "watch"}}),
            self.assertRaises(api_repository.NotFoundError),
        ):
            api_repository.decision("DEMO")

    def test_invalid_ticker_is_rejected(self):
        with self.assertRaises(ValueError):
            api_repository.normalize_ticker("../../secret")

    def test_watchlist_uses_user_state_and_saved_rows(self):
        state = {"watchlist": ["DEMO"], "_revision": "abc"}
        with (
            mock.patch.object(api_repository, "_workspace_state", return_value=state),
            mock.patch.object(api_repository.backend_layer, "read_json_table_many", side_effect=[
                {"DEMO": {"decision_receipt": {"action": "watch", "trigger": {"price": 105}, "invalidation": {"price": 92}}}},
                {"DEMO": {"price": 100, "change_pct": 1.2}},
            ]),
        ):
            payload = api_repository.watchlist("trusted-user-id")
        self.assertEqual(payload["items"][0]["ticker"], "DEMO")
        self.assertEqual(payload["items"][0]["trigger_price"], 105)

    def test_missing_decision_is_queued_for_authenticated_user(self):
        with (
            mock.patch.object(api_repository, "decision", side_effect=api_repository.NotFoundError("missing")),
            mock.patch.object(api_repository.backend_layer, "enqueue_job", return_value="job-1") as enqueue,
        ):
            payload = api_repository.request_decision("goog", "user-1")
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["ticker"], "GOOG")
        enqueue.assert_called_once_with(
            "market_snapshot", ticker="GOOG", priority=20, requested_by="api:user-1",
            dedupe_active=False,
        )

    def test_request_status_is_scoped_to_requesting_user(self):
        job = {
            "id": "job-1", "job_type": "market_snapshot", "ticker": "GOOG",
            "status": "queued", "requested_by": "api:another-user",
        }
        with (
            mock.patch.object(api_repository.backend_layer, "get_job", return_value=job),
            self.assertRaises(api_repository.NotFoundError),
        ):
            api_repository.analysis_request("job-1", "user-1")


if __name__ == "__main__":
    unittest.main()
