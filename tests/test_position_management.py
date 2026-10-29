import unittest
from unittest import mock
from datetime import datetime, timezone
import api_repository
import api_service
from fastapi.testclient import TestClient
from position_management import position_management_read


class PositionManagementTests(unittest.TestCase):
    def test_avoid_does_not_imply_sell(self):
        self.assertEqual(position_management_read({"entry_price": 100},
            {"price": 100, "ma50": 90, "ma200": 80, "action": "avoid"})["action"], "Hold")

    def test_broken_stop_exits(self):
        self.assertEqual(position_management_read({"entry_price": 100, "stop_price": 95},
            {"price": 94, "action": "watch"})["action"], "Exit")

    def test_private_route_requires_auth(self):
        self.assertEqual(TestClient(api_service.app).get("/v1/positions/DASH/assessment").status_code, 401)

    def test_saved_snapshot_drives_actual_holding(self):
        receipt = {"action": "avoid", "price": 100,
                   "captured_at": datetime.now(timezone.utc).isoformat(),
                   "input_snapshot": {"inputs": {"ma50": 90, "ma200": 80}}}
        with mock.patch.object(api_repository, "_workspace_state", return_value={
            "holdings": {"DASH": {"entry_price": 100}}}), mock.patch.object(
            api_repository.backend_layer, "read_json_table",
            side_effect=[{"DASH": {"decision_receipt": receipt}}, {}]):
            result = api_repository.position_assessment("DASH", "user-a")
        self.assertEqual(result["assessment"]["action"], "Hold")
        self.assertEqual(result["entry_action"], "avoid")
        self.assertFalse(result["calendar_sale"])

    def test_no_holding_does_not_invent_hold(self):
        with mock.patch.object(api_repository, "_workspace_state", return_value={"holdings": {}}):
            self.assertEqual(api_repository.position_assessment("DASH", "u")["status"], "needs_position")

    def test_incomplete_snapshot_does_not_invent_hold(self):
        with mock.patch.object(api_repository, "_workspace_state", return_value={
            "holdings": {"DASH": {"entry_price": 100}}}), mock.patch.object(
            api_repository.backend_layer, "read_json_table", side_effect=[
                {"DASH": {"decision_receipt": {"action": "avoid"}}}, {}]):
            self.assertEqual(api_repository.position_assessment("DASH", "u")["status"], "refresh_required")
