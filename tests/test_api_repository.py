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


if __name__ == "__main__":
    unittest.main()
