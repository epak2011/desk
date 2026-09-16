import unittest
from datetime import date
from unittest import mock

import pm_view


class _Messages:
    def __init__(self, missing=None):
        self.models = []
        self.missing = set(missing or [])

    def create(self, model, **kwargs):
        self.models.append(model)
        if model in self.missing:
            raise RuntimeError(f"model {model} not found")
        return {"model": model, "kwargs": kwargs}


class _Client:
    def __init__(self, missing=None):
        self.messages = _Messages(missing=missing)


class FastModelTests(unittest.TestCase):
    def _complete_dossier(self):
        return {
            "dossier": "Decision memo.",
            "technical_narrative": "Technical read.",
            "pm_narrative": "Portfolio manager view.",
            "bullets": {
                "thesis": "Company-specific thesis.",
                "drivers": ["Driver one", "Driver two", "Driver three"],
                "risks": ["Risk one", "Risk two", "Risk three"],
                "valuation": "Valuation anchor.",
                "timing_watchpoint": "Watch the confirmed October 2026 earnings update.",
            },
            "quality": {"tier": "A", "rationale": "Durable franchise."},
            "tactical_call": {"action": "WATCH", "reasoning": "Trigger has not fired."},
        }

    def test_nested_live_values_resolve_for_frontend_contract(self):
        state = {"price": 212.17, "ma50": 213.02, "rs": 1.0}
        payload = {
            "memo": "Price {price}; again {{price}}; RS {rs}.",
            "bullets": ["Trigger {pct_ma50}", "No token"],
        }

        resolved = pm_view.substitute_live_values_nested(payload, state)

        self.assertEqual(resolved["memo"], "Price $212.17; again $212.17; RS 1.00.")
        self.assertIn("Trigger", resolved["bullets"][0])
        self.assertNotIn("{", resolved["bullets"][0])

    def test_fast_messages_prefer_fast_model(self):
        client = _Client()
        with mock.patch.object(
            pm_view,
            "CLAUDE_FAST_MODEL_FALLBACKS",
            ["fast-model", "full-model"],
        ):
            result = pm_view._messages_create_fast(client, max_tokens=100)

        self.assertEqual(client.messages.models, ["fast-model"])
        self.assertEqual(result["model"], "fast-model")

    def test_fast_messages_fall_back_when_fast_model_is_unavailable(self):
        client = _Client(missing={"missing-fast-model"})
        with mock.patch.object(
            pm_view,
            "CLAUDE_FAST_MODEL_FALLBACKS",
            ["missing-fast-model", "full-model"],
        ):
            result = pm_view._messages_create_fast(client, max_tokens=100)

        self.assertEqual(
            client.messages.models,
            ["missing-fast-model", "full-model"],
        )
        self.assertEqual(result["model"], "full-model")

    def test_dossier_contract_rejects_missing_required_sections(self):
        payload = self._complete_dossier()
        payload["bullets"]["risks"] = ["Only one risk"]

        issues = pm_view._dossier_contract_issues(
            payload, as_of=date(2026, 9, 16)
        )

        self.assertIn("bullets.risks must contain exactly 3 items", issues)

    def test_dossier_contract_rejects_past_catalyst_year(self):
        payload = self._complete_dossier()
        payload["bullets"]["timing_watchpoint"] = "Watch Q1 2025 earnings."

        issues = pm_view._dossier_contract_issues(
            payload, as_of=date(2026, 9, 16)
        )

        self.assertTrue(any("past year" in issue for issue in issues))

    def test_dossier_contract_accepts_complete_current_memo(self):
        issues = pm_view._dossier_contract_issues(
            self._complete_dossier(), as_of=date(2026, 9, 16)
        )

        self.assertEqual(issues, [])

    def test_date_label_normalizes_iso_timestamp(self):
        self.assertEqual(
            pm_view._date_label("2026-11-17T13:30:00+00:00"),
            "2026-11-17",
        )


if __name__ == "__main__":
    unittest.main()
