import json
import sys
import types
import unittest
from unittest import mock

import idea_discovery


class IdeaDiscoveryTests(unittest.TestCase):
    def test_message_helper_prefers_streaming(self):
        response = types.SimpleNamespace(content=[], stop_reason="end_turn")

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get_final_message(self):
                return response

        messages = types.SimpleNamespace(
            stream=lambda **_kwargs: Stream(),
            create=mock.Mock(side_effect=AssertionError("non-streaming path used")),
        )
        result = idea_discovery._create_message(types.SimpleNamespace(messages=messages), model="demo")
        self.assertIs(result, response)
        messages.create.assert_not_called()

    def test_paused_web_search_is_resumed_before_parsing(self):
        payload = {"candidates": [{"ticker": "VRT", "score": 90}]}
        paused_content = [types.SimpleNamespace(
            text=None, citations=[], content=None, type="server_tool_use",
        )]
        responses = [
            types.SimpleNamespace(content=paused_content, stop_reason="pause_turn"),
            types.SimpleNamespace(content=[types.SimpleNamespace(
                text="VRT is exposed to data-center power demand.", citations=[], content=None,
            )], stop_reason="end_turn"),
            types.SimpleNamespace(content=[types.SimpleNamespace(
                text=json.dumps(payload), citations=[], content=None,
            )], stop_reason="end_turn"),
        ]
        calls = []

        class Messages:
            def create(self, **kwargs):
                calls.append(kwargs)
                return responses.pop(0)

        fake_module = types.SimpleNamespace(
            Anthropic=lambda **_kwargs: types.SimpleNamespace(messages=Messages())
        )
        with mock.patch.dict(sys.modules, {"anthropic": fake_module}):
            result = idea_discovery.generate("AI data-center power", None, "key")

        self.assertEqual(result["candidates"][0]["ticker"], "VRT")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1]["messages"][1]["role"], "assistant")
        self.assertIs(calls[1]["messages"][1]["content"], paused_content)
        self.assertEqual(calls[1]["tools"][0]["type"], "web_search_20250305")
        self.assertNotIn("tools", calls[2])

    def test_default_universe_is_broad_and_web_researched(self):
        calls = []
        payload = {
            "criteria": ["Grid demand"],
            "summary": "Current opportunity set.",
            "candidates": [{
                "ticker": "VRT", "company": "Vertiv", "score": 90,
                "theme_fit": "Cooling", "financial_fit": "Growing",
                "risks": "Valuation", "evidence": ["Demand"],
                "verify_next": ["Orders"], "sources": [{"url": "https://example.com"}],
            }],
        }
        response = types.SimpleNamespace(content=[types.SimpleNamespace(
            text=json.dumps(payload), citations=[types.SimpleNamespace(
                url="https://example.com", title="Primary source"
            )], content=None,
        )])

        class Messages:
            def create(self, **kwargs):
                calls.append(kwargs)
                return response

        fake_module = types.SimpleNamespace(
            Anthropic=lambda **_kwargs: types.SimpleNamespace(messages=Messages())
        )
        with mock.patch.dict(sys.modules, {"anthropic": fake_module}):
            result = idea_discovery.generate("AI data-center power", None, "key")

        self.assertGreater(len(idea_discovery.DEFAULT_UNIVERSE.split(",")), 100)
        self.assertEqual(calls[0]["tools"][0]["type"], "web_search_20250305")
        self.assertNotIn("tools", calls[1])
        self.assertTrue(result["web_researched"])
        self.assertEqual(result["universe_mode"], "broad_default")
        self.assertEqual(result["candidates"][0]["financial_fit"], "Growing")

    def test_explicit_universe_excludes_out_of_scope_candidates(self):
        payload = {"candidates": [
            {"ticker": "VRT", "score": 90},
            {"ticker": "NVDA", "score": 80},
        ]}
        response = types.SimpleNamespace(content=[types.SimpleNamespace(
            text=json.dumps(payload), citations=[], content=None,
        )])

        class Messages:
            def create(self, **_kwargs):
                return response

        fake_module = types.SimpleNamespace(
            Anthropic=lambda **_kwargs: types.SimpleNamespace(messages=Messages())
        )
        with mock.patch.dict(sys.modules, {"anthropic": fake_module}):
            result = idea_discovery.generate("Power infrastructure", "VRT, ETN", "key")
        self.assertEqual([row["ticker"] for row in result["candidates"]], ["VRT"])
        self.assertEqual(result["universe_mode"], "explicit")


if __name__ == "__main__":
    unittest.main()
