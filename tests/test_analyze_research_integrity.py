import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import pm_view
import public_contract
from public_contract import analyze_page_payload, research_payload


class AnalyzeIntegrityTests(unittest.TestCase):
    def test_technical_picture_matches_streamlit_classifications(self):
        cases = [
            ({"price": 110, "ma50": 100, "ma200": 90, "tech_delta": 1.5, "rs": 1.06, "rs_delta": .01, "vol_ratio": 1.25, "pct_of_52w_range": 90},
             ["Strong", "Accelerating", "Leading", "Confirming", "Upper range"]),
            ({"price": 100, "ma50": 110, "ma200": 90, "tech_delta": -.5, "rs": .97, "vol_ratio": 1.1, "pct_of_52w_range": 70},
             ["Repairing", "Flat", "Neutral", "Normal", "Upper half"]),
            ({"price": 100, "ma50": 90, "ma200": 110, "tech_delta": -1.5, "rs": 1.02, "vol_ratio": .75, "pct_of_52w_range": 10},
             ["Transition", "Fading", "Positive", "Light", "Near lows"]),
        ]
        for rule, expected in cases:
            with self.subTest(rule=rule):
                page = analyze_page_payload("TEST", rule=rule)
                self.assertEqual([row["status"] for row in page["technical_picture"]], expected)

    def test_unknown_technicals_do_not_turn_into_negative_readings(self):
        page = analyze_page_payload("TEST", rule={"rs": float("nan"), "pct_of_52w_range": float("inf")})
        self.assertEqual([row["status"] for row in page["technical_picture"]],
                         ["Pending", "Unavailable", "Unavailable", "Unavailable", "Unavailable"])

    def test_checks_match_streamlit_thresholds_without_changing_action(self):
        rule = {"action": "watch", "price": 110, "ma50": 100, "ma200": 120,
                "rs": 1, "reward_risk": 1.25, "vol_ratio": .7}
        page = analyze_page_payload("TEST", rule=rule)
        checks = {row["key"]: row["passed"] for row in page["decision_evidence"]["checks"]}
        self.assertEqual(checks, {"trend": False, "relative_strength": True, "reward_risk": False, "trigger": False, "volume": False})
        self.assertEqual(page["hero"]["action"], "watch")
        self.assertEqual(page["technical_evidence"]["ma200"], 120)
        self.assertIsNone(page["execution_plan"]["target_1"])

    def test_fallback_never_claims_business_quality_or_leaks_prompt(self):
        pm = pm_view._rule_backed_pm_snapshot("DASH", {"action": "avoid"})
        self.assertEqual(pm["quality"], {})
        self.assertNotIn("Discuss", " ".join(pm["drivers"]))
        pm["quality"] = {"tier": "B"}  # saved legacy payload
        pm["drivers"].append("DoorDash is a platform. Discuss marketplace liquidity,")
        pm["_source"] = "rules fallback (provider account/request detail)"
        research = research_payload("DASH", report={"pm": pm, "_worker_generated_at": datetime.now(timezone.utc).isoformat()})
        self.assertEqual(research["quality"], {})
        self.assertIsNone(research["valuation"])
        self.assertNotIn("Discuss", " ".join(research["drivers"]))
        self.assertEqual(research["source"], "rules fallback")
        self.assertTrue(research["retry_required"])
        page = analyze_page_payload("DASH", research=research)
        self.assertEqual(page["portfolio_manager"]["research_completeness"], "rules_only")

    def test_company_profile_is_not_completed_claude_research(self):
        result = research_payload("TEST", report={"meta": {"long_business_summary": "Company description"}, "_worker_generated_at": datetime.now(timezone.utc).isoformat()})
        self.assertEqual(result["company_overview"], "Company description")
        self.assertEqual(result["completeness"], "unavailable")
        self.assertTrue(result["retry_required"])
        self.assertFalse(result["full_report_available"])

    def test_valid_compact_claude_summary_survives_failed_full_dossier(self):
        result = research_payload("TEST", report={
            "pm": {"_source": "claude", "thesis": "Actual business thesis", "quality": {"tier": "A"}},
            "dossier": {"_source": "rules fallback", "thesis": "Fallback"},
            "_worker_generated_at": datetime.now(timezone.utc).isoformat(),
        })
        self.assertEqual(result["provenance"], "claude")
        self.assertEqual(result["completeness"], "summary_only")
        self.assertEqual(result["quality"]["tier"], "A")

    def test_completed_dossier_overrides_fallback_provenance(self):
        research = research_payload("TEST", report={
            "pm": {"_source": "rules fallback", "quality": {"tier": "B"}},
            "dossier": {"_source": "claude · fast refresh", "dossier": "Full memo", "bullets": {"thesis": "Business thesis"}, "quality": {"tier": "A"}},
            "_worker_generated_at": datetime.now(timezone.utc).isoformat(),
        })
        self.assertEqual(research["provenance"], "claude")
        self.assertEqual(research["generation_status"], "complete")
        self.assertEqual(research["quality"]["tier"], "A")
        self.assertFalse(research["retry_required"])


class WorkerResearchIntegrityTests(unittest.TestCase):
    def worker_function(self, name, namespace):
        # Execute the real function in isolation; provider/database work is mocked.
        source = Path(__file__).resolve().parents[1] / "worker.py"
        tree = ast.parse(source.read_text())
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), "exec"), namespace)
        return namespace[name]

    def test_failed_claude_report_does_not_overwrite_saved_report(self):
        backend = Mock()
        namespace = {"_api_key": lambda: "test-key", "_fresh_tactical_state": lambda ticker: ({}, {}),
                     "get_pm_view": lambda *a, **k: {},
                     "get_decision_dossier": lambda *a, **k: {"_source": "rules fallback"},
                     "_dossier_contract_issues": pm_view._dossier_contract_issues, "backend": backend}
        fn = self.worker_function("refresh_full_report", namespace)
        with self.assertRaisesRegex(RuntimeError, "previous report preserved"):
            fn("DASH")
        backend.upsert_json_table.assert_not_called()

    def test_incomplete_claude_json_is_not_success(self):
        backend = Mock()
        namespace = {"_api_key": lambda: "test-key", "_fresh_tactical_state": lambda ticker: ({}, {}),
                     "get_pm_view": lambda *a, **k: {},
                     "get_decision_dossier": lambda *a, **k: {"_source": "claude", "dossier": "partial"},
                     "_dossier_contract_issues": pm_view._dossier_contract_issues, "backend": backend}
        fn = self.worker_function("refresh_full_report", namespace)
        with self.assertRaises(RuntimeError):
            fn("DASH")
        backend.upsert_json_table.assert_not_called()

    def test_complete_report_is_persisted_even_if_compact_pm_failed(self):
        backend = Mock()
        dossier = {"_source": "claude", "dossier": "Memo", "technical_narrative": "Tape", "pm_narrative": "Business",
                   "bullets": {"thesis": "Thesis", "valuation": "Valuation", "timing_watchpoint": "No confirmed near-term catalyst", "drivers": ["a", "b", "c"], "risks": ["x", "y", "z"]},
                   "quality": {"tier": "B", "rationale": "Rationale"}, "tactical_call": {"action": "WATCH", "reasoning": "Reason"}}
        namespace = {"_api_key": lambda: "test-key", "_fresh_tactical_state": lambda ticker: ({}, {}),
                     "get_pm_view": lambda *a, **k: {"_source": "rules fallback"},
                     "get_decision_dossier": lambda *a, **k: dossier,
                     "_dossier_contract_issues": pm_view._dossier_contract_issues, "backend": backend,
                     "datetime": datetime, "timezone": timezone, "_fundamentals_fingerprint": lambda meta: None}
        self.worker_function("refresh_full_report", namespace)("DASH")
        backend.upsert_json_table.assert_called_once()

    def test_fresh_rules_fallback_is_queued_for_repair(self):
        backend = Mock()
        backend.enabled_watchlist_tickers.return_value = ["DASH"]
        backend.read_json_table_many.side_effect = [
            {"DASH": {"pm": {"_source": "rules fallback"}, "_worker_generated_at": datetime.now(timezone.utc).isoformat()}}, {}, {},
        ]
        backend.enqueue_job.return_value = "job-1"
        fn = self.worker_function("queue_stale_research_refresh", {"_api_key": lambda: "key", "backend": backend, "datetime": datetime, "timezone": timezone, "_fundamentals_fingerprint": lambda profile: None})
        result = fn()
        self.assertEqual(result["tickers"], ["DASH"])
        self.assertEqual(result["jobs"][0]["reason"], "research_incomplete")


class ApiResearchIntegrityTests(unittest.TestCase):
    def api_function(self, name, namespace):
        source = Path(__file__).resolve().parents[1] / "api_repository.py"
        tree = ast.parse(source.read_text())
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
        namespace["Any"] = object
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), "exec"), namespace)
        return namespace[name]

    def test_legacy_succeeded_job_with_fallback_is_not_ready(self):
        backend = Mock()
        backend.get_job.return_value = {"id": "one", "requested_by": "api:user", "job_type": "full_report", "status": "succeeded", "ticker": "DASH"}
        fn = self.api_function("analysis_request", {"backend_layer": backend, "normalize_ticker": str,
            "NotFoundError": LookupError, "decision": lambda ticker: {"research": {"generation_status": "incomplete", "full_report_available": False}}})
        result = fn("one", "user")
        self.assertEqual(result["status"], "failed")
        self.assertIn("retry research", result["message"])


if __name__ == "__main__":
    unittest.main()
