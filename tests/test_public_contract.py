import unittest

from public_contract import (
    PUBLIC_CONTRACT_VERSION,
    analyze_page_payload,
    app_manifest_payload,
    attention_payload,
    decision_payload,
    error_payload,
    regime_payload,
    research_payload,
    security_profile_payload,
    user_workspace_payload,
    watchlist_payload,
    ideas_payload,
    methodology_payload,
)


class PublicContractTests(unittest.TestCase):
    def test_app_manifest_identifies_shared_and_missing_page_contracts(self):
        payload = app_manifest_payload()
        pages = {page["key"]: page for page in payload["pages"]}
        self.assertEqual(pages["analyze"]["endpoint"], "/v1/decisions/{ticker}")
        self.assertEqual(pages["ideas"]["endpoint"], "/v1/ideas")
        self.assertEqual(pages["ideas"]["status"], "shared")
        self.assertEqual(pages["health"]["endpoint"], "/v1/system-health")
        self.assertEqual(pages["methodology"]["status"], "shared")
        self.assertTrue(all(page["status"] == "shared" for page in pages.values()))
        self.assertNotIn("missing", pages["today"])
        self.assertTrue(payload["rules"]["backend_is_authoritative"])
        self.assertTrue(payload["rules"]["render_endpoint_payload_directly"])
        self.assertEqual(
            pages["market"]["sections"],
            ["outlook", "entry_timing", "todays_context", "market_highlights", "market_implications", "forward_watch", "framework_gauges", "market_news", "crypto_regime", "metric_guide"],
        )

    def test_ideas_payload_only_exposes_renderable_saved_screen_fields(self):
        payload = ideas_payload([{"query": "AI power", "secret": "no", "result": {
            "summary": "Grid beneficiaries", "criteria": ["Power demand"],
            "candidates": [{"ticker": "VRT", "score": 91, "theme_fit": "Cooling", "secret": "no"}],
        }}])
        self.assertEqual(payload["ideas"][0]["candidates"][0]["ticker"], "VRT")
        self.assertNotIn("secret", payload["ideas"][0])
        self.assertNotIn("secret", payload["ideas"][0]["candidates"][0])

    def test_methodology_publishes_actions_and_disclaimer(self):
        payload = methodology_payload()
        self.assertEqual({row["key"] for row in payload["actions"]}, {"enter", "accumulate", "watch", "hold_off", "avoid"})
        self.assertIn("does not provide personalized investment advice", payload["disclaimer"])

    def test_analyze_page_payload_is_complete_and_ready_to_render(self):
        rule = {
            "action": "watch", "price": 100, "ma20": 98, "ma50": 90, "ma200": 80,
            "rs": 1.1, "tech_delta": -0.2, "vol_ratio": 0.5, "pct_of_52w_range": 80,
            "reward_risk": 0.9, "entry_status": "Waiting for pullback",
            "matrix_reason": "Setup needs the trigger to fire.",
            "setup_score_breakdown": {"score": 6.5, "components": [{"label": "Baseline", "points": 5, "max_points": 5, "note": "neutral"}]},
            "decision_receipt": {
                "action": "watch", "entry_size": None, "confidence": "Low",
                "trigger": {"text": "Pull back to $95 and hold.", "price": 95},
                "invalidation": {"text": "Invalid below $88.", "price": 88},
                "top_factors": ["Trend is intact."], "data_trust": {"status": "trusted"},
            },
        }
        research = {
            "status": "ready", "thesis": "A durable platform.", "drivers": ["Growth"],
            "risks": ["Valuation"], "valuation": "Rich", "quality": {"tier": "B", "rationale": "Execution risk"},
            "decision_memo": "Full memo", "technical_narrative": "Technical read", "pm_narrative": "PM view",
        }
        payload = analyze_page_payload("DEMO", rule=rule, research=research)
        self.assertEqual(payload["hero"]["size_now"], "0% — wait for an actionable call")
        self.assertEqual(payload["decision_evidence"]["total_count"], 5)
        self.assertEqual(payload["why_action"]["title"], "Why WATCH")
        self.assertEqual(len(payload["portfolio_manager"]["quality_classifications"]), 4)
        self.assertTrue(payload["full_research_report"]["available"])

    def test_decision_payload_uses_receipt_and_blocks_untrusted_execution(self):
        payload = decision_payload(
            {
                "receipt_id": "abc",
                "ticker": "AAPL",
                "action": "Enter",
                "captured_at": "2026-09-23T18:27:41+00:00",
                "data_trust": {"status": "blocked", "executable": False},
                "private_note": "never expose",
            },
            portfolio_context={"suggested_size_pct": 2.5},
        )
        self.assertEqual(payload["decision"]["action"], "Enter")
        self.assertFalse(payload["executable"])
        self.assertNotIn("private_note", payload["decision"])
        self.assertEqual(payload["meta"]["contract_version"], PUBLIC_CONTRACT_VERSION)
        self.assertEqual(payload["meta"]["data_as_of"], "2026-09-23T18:27:41+00:00")

    def test_decision_payload_prefers_market_source_timestamp(self):
        payload = decision_payload({
            "ticker": "AVGO",
            "captured_at": "2026-09-23T18:27:41+00:00",
            "input_snapshot": {"source_as_of": "2026-09-23T18:25:00+00:00"},
            "data_trust": {"as_of": "2026-09-23T18:26:00+00:00"},
        })
        self.assertEqual(payload["meta"]["data_as_of"], "2026-09-23T18:26:00+00:00")

    def test_attention_payload_has_stable_shape_without_private_fields(self):
        payload = attention_payload(
            [{"event_id": "x", "ticker": "MSFT", "priority": "high", "private": 1}]
        )
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["daily_workflow_summary"]["urgent_items"], 1)
        self.assertEqual(payload["daily_workflow_summary"]["affected_tickers"], ["MSFT"])
        self.assertNotIn("private", payload["events"][0])

    def test_empty_attention_payload_publishes_clear_daily_workflow(self):
        payload = attention_payload([])
        self.assertEqual(payload["daily_workflow_summary"]["status"], "clear")
        self.assertEqual(payload["daily_workflow_summary"]["next_step"], "No action is required.")

    def test_regime_payload_allowlists_fields(self):
        payload = regime_payload({
            "why_today": "Mixed tape.",
            "portfolio_stance": "Neutral",
            "assets": {"SPY": {"last": 100}},
            "news": [{"title": "Crypto policy update", "category": "crypto_policy"}],
            "database_url": "secret",
        })
        self.assertEqual(payload["regime"]["why_today"], "Mixed tape.")
        self.assertEqual(payload["regime"]["portfolio_stance"], "Neutral")
        self.assertEqual(payload["regime"]["assets"]["SPY"]["last"], 100)
        self.assertEqual(payload["regime"]["news"][0]["category"], "crypto_policy")
        self.assertNotIn("database_url", payload["regime"])

    def test_watchlist_payload_blocks_private_notes(self):
        payload = watchlist_payload([{"ticker": "NVDA", "action": "watch", "private_note": "x"}])
        self.assertEqual(payload["count"], 1)
        self.assertNotIn("private_note", payload["items"][0])

    def test_workspace_payload_exposes_only_user_owned_sections(self):
        payload = user_workspace_payload({"watchlist": ["AAPL"], "api_key": "secret"})
        self.assertEqual(payload["workspace"]["watchlist"], ["AAPL"])
        self.assertNotIn("api_key", payload["workspace"])

    def test_error_payload_is_stable_and_safe(self):
        payload = error_payload("data_stale", "Refresh required.", retryable=True, request_id="req-1")
        self.assertEqual(payload["error"]["code"], "data_stale")
        self.assertTrue(payload["error"]["retryable"])
        self.assertEqual(payload["meta"]["request_id"], "req-1")
        self.assertIn("display", payload["meta"])

    def test_old_or_price_dislocated_research_is_stale(self):
        payload = research_payload(
            "DEMO",
            report={
                "pm": {"thesis": "Saved thesis."},
                "_worker_generated_at": "2026-01-01T12:00:00+00:00",
                "_market_price": 100,
            },
            market={"price": 115},
        )
        self.assertEqual(payload["status"], "stale")
        self.assertGreaterEqual(payload["age_days"], 7)
        self.assertTrue(any("Price has moved" in reason for reason in payload["stale_reasons"]))

    def test_completed_dossier_precedes_compact_rules_fallback(self):
        payload = research_payload(
            "NVDA",
            report={
                "pm": {
                    "thesis": "Rules fallback thesis.",
                    "drivers": ["Rules fallback driver."],
                    "risks": ["Claude PM research did not complete."],
                    "valuation": "Valuation unavailable.",
                    "_source": "rules fallback",
                },
                "dossier": {
                    "bullets": {
                        "thesis": "Claude dossier thesis.",
                        "drivers": ["Blackwell demand."],
                        "risks": ["Custom silicon adoption."],
                        "valuation": "Forward P/E provides the anchor.",
                    },
                    "_source": "claude · fast refresh",
                },
                "_worker_generated_at": "2026-09-15T22:00:00+00:00",
            },
        )

        self.assertEqual(payload["thesis"], "Claude dossier thesis.")
        self.assertEqual(payload["drivers"], ["Blackwell demand."])
        self.assertEqual(payload["risks"], ["Custom silicon adoption."])
        self.assertEqual(payload["valuation"], "Forward P/E provides the anchor.")

    def test_completed_dossier_never_splices_in_rules_fallback(self):
        payload = research_payload(
            "NVDA",
            report={
                "pm": {
                    "drivers": ["Rules fallback driver."],
                    "risks": ["Claude PM research did not complete."],
                    "valuation": "Valuation requires a successful PM memo refresh.",
                },
                "dossier": {
                    "bullets": {"thesis": "Completed Claude thesis."},
                    "_source": "claude · fast refresh",
                },
            },
        )

        self.assertEqual(payload["drivers"], [])
        self.assertEqual(payload["risks"], [])
        self.assertIsNone(payload["valuation"])

    def test_security_profile_allowlists_company_metadata(self):
        payload = security_profile_payload(
            "demo",
            report={"meta": {"company_name": "Demo Inc.", "sector": "Technology", "private": "secret"}},
            market={"security_profile": {"market_cap": 1000, "short_pct_float": 0.03}},
        )
        self.assertEqual(payload["ticker"], "DEMO")
        self.assertEqual(payload["company_name"], "Demo Inc.")
        self.assertEqual(payload["market_cap"], 1000)
        self.assertNotIn("private", payload)


if __name__ == "__main__":
    unittest.main()
