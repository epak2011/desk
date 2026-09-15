import unittest
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


if __name__ == "__main__":
    unittest.main()
