import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import research_reliability as rr
import pm_view
import worker
import backtest_store
import api_repository


def valid_dossier():
    return {"dossier": "Decision memo", "technical_narrative": "Trend evidence",
            "pm_narrative": "Business evidence", "bullets": {"thesis": "Thesis",
                "valuation": "Valuation", "timing_watchpoint": "No confirmed near-term catalyst",
                "drivers": ["Driver one", "Driver two", "Driver three"],
                "risks": ["Risk one", "Risk two", "Risk three"]},
            "quality": {"tier": "B", "rationale": "Evidence"},
            "tactical_call": {"action": "WATCH", "reasoning": "Wait for evidence"}}


class ResearchReliabilityTests(TestCase):
    def test_provider_quota_circuit_stops_all_scheduled_tickers_before_large_reads(self):
        pause = {"failures": 1, "last_failed_at": datetime.now(timezone.utc).isoformat(), "error_code": "provider_quota"}
        with patch.object(worker, "_api_key", return_value="test"), \
             patch.object(worker.backend, "enabled_watchlist_tickers", return_value=["DASH", "NVDA"]), \
             patch.object(worker.backend, "research_retry_states", return_value={"__provider__": pause}), \
             patch.object(worker.backend, "read_json_table_many") as read, patch.object(worker.backend, "enqueue_job") as enqueue:
            result = worker.queue_stale_research_refresh()
        self.assertFalse(result["queued"])
        self.assertEqual(result["provider_error_code"], "provider_quota")
        read.assert_not_called()
        enqueue.assert_not_called()

    def test_system_health_uses_selected_memo_and_exposes_trial_block(self):
        research = {"generated_at": datetime.now(timezone.utc).isoformat(), "status": "ready",
                    "completeness": "summary_only", "selection": {"saved_successful_research": True}}
        trial = {"status": "blocked", "reason": "frozen_sources_changed", "eligible": False}
        with patch.object(api_repository, "_workspace_state", return_value={"watchlist": ["DASH"]}), \
             patch.object(api_repository.backend_layer, "read_json_table_many", side_effect=[
                 {"DASH": {"updated_at": datetime.now(timezone.utc).isoformat()}},
                 {"DASH": {"decision_receipt": {"action": "watch"}}}, {}, {"DASH": {"thesis": "Saved"}}]), \
             patch.object(api_repository.public_contract, "research_payload", return_value=research), \
             patch.object(api_repository.backend_layer, "latest_jobs", return_value=[]), \
             patch.object(api_repository.backend_layer, "research_retry_states", return_value={}), \
             patch.object(api_repository.backend_layer, "has_database", return_value=True), \
             patch.object(rr, "experiment_health", return_value=trial):
            result = api_repository.system_health("user")
        self.assertFalse(any(issue["kind"] in {"research_missing", "research_stale"} for issue in result["issues"]))
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["prospective_trial"], trial)

    def test_optional_market_metrics_do_not_abort_research_or_become_fake_numbers(self):
        message = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(text=json.dumps(valid_dossier()))])
        state = {key: None for key in ("price", "ma50", "ma200", "rsi14", "rs", "rs_delta", "vol_ratio", "reward_risk")}
        state["action"] = "watch"
        with patch("anthropic.Anthropic"), patch.object(pm_view, "_messages_create_fast", return_value=message) as create, \
             patch.object(pm_view, "_call_with_timeout", side_effect=lambda fn, *args: fn()), \
             patch.object(pm_view, "pm_identity_mismatch", return_value=False):
            result = pm_view.get_decision_dossier("TEST", state, [], {"earnings_date": "2020-01-01", "earnings_days": -49}, {}, api_key="test", fast=True)
        self.assertEqual(result["_source"], "claude · fast refresh")
        prompt = create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("200d MA: unavailable", prompt)
        self.assertIn("No confirmed future earnings date", prompt)
        self.assertNotIn("in -49 days", prompt)
        self.assertNotIn("Confirmed next earnings date: 2020", prompt)

    def test_failure_codes_never_echo_provider_secrets(self):
        for source, expected in (("401 invalid api key sk-ant-SECRET", "provider_auth"),
                                 ("credit balance too low SECRET", "provider_quota"),
                                 ("request timed out SECRET", "provider_timeout"),
                                 ("research_response_truncated", "response_truncated"),
                                 ("research_invalid_json", "invalid_json")):
            error = rr.ResearchFailure(source, ["missing dossier"])
            self.assertEqual(error.diagnostics["error_code"], expected)
            self.assertNotIn("SECRET", str(error) + json.dumps(error.diagnostics))
            self.assertTrue(error.diagnostics["previous_report_preserved"])

    def test_persistent_backoff_grows_and_eventually_expires(self):
        now = datetime.now(timezone.utc)
        row = {"last_failed_at": now.isoformat(), "failures": 1}
        self.assertEqual(rr.retry_window(row, now)["cooldown_minutes"], 30)
        self.assertEqual(rr.retry_window({**row, "failures": 3}, now)["cooldown_minutes"], 120)
        self.assertFalse(rr.retry_window(row, now + timedelta(minutes=31))["cooling_down"])
        self.assertEqual(rr.retry_window({**row, "error_code": "provider_quota"}, now)["cooldown_minutes"], 1440)

    def test_recent_saved_memo_is_not_missing_because_full_report_failed(self):
        research = {"generated_at": datetime.now(timezone.utc).isoformat(), "status": "ready",
                    "completeness": "summary_only", "selection": {"saved_successful_research": True},
                    "generation_status": "incomplete"}
        self.assertIsNone(rr.research_issue(research))
        self.assertEqual(rr.research_issue({**research, "status": "stale"})["kind"], "research_stale")

    def test_missing_date_is_not_fresh(self):
        self.assertEqual(rr.research_issue({"completeness": "summary_only"})["kind"], "research_stale")
        self.assertEqual(rr.performance_context({})["freshness"], "unknown")
        self.assertEqual(rr.performance_context({"generated_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()})["freshness"], "unknown")

    def test_api_image_includes_reliability_module_and_trial_hash_sources(self):
        root = Path(rr.__file__).parent
        docker = (root / "Dockerfile.api").read_text()
        self.assertIn("COPY research_reliability.py ./", docker)
        self.assertIn("COPY experiments/ ./experiments/", docker)
        spec = json.loads((root / "experiments/entry-events-v3.json").read_text())
        for name in spec["source_hashes"]:
            self.assertIn(name, docker)

    def test_performance_context_never_claims_aggregate_is_current_strategy_pnl(self):
        context = rr.performance_context({"generated_at": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
                                         "engine_version_families": [{"engine_version": "old"}]})
        self.assertEqual(context["freshness"], "stale")
        self.assertFalse(context["calendar_exit_enabled"])
        self.assertFalse(context["current_rules_performance_verified"])
        self.assertEqual(context["version_breakdown"][0]["engine_version"], "old")

    def test_actual_registration_mismatch_is_visible_not_silently_rewritten(self):
        root = Path(rr.__file__).parent
        path = root / "experiments/entry-events-v3.json"
        before = path.read_bytes()
        result = rr.experiment_health(registration="entry-events-v3.json")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "frozen_sources_changed")
        self.assertIn("worker.py", result["changed_sources"])
        self.assertFalse(result["collection_verified"])
        self.assertEqual(before, path.read_bytes())

    def test_new_trial_is_explicit_future_registration_without_rewriting_old_trial(self):
        from datetime import date
        import research_protocol
        root = Path(rr.__file__).parent
        old = (root / "experiments/entry-events-v3.json").read_bytes()
        spec = research_protocol.active_registration(root)
        self.assertEqual(spec["starts_on"], "2026-10-29")
        self.assertEqual(spec["assumptions"]["benchmarks"], ["SPY", "QQQ"])
        self.assertEqual(rr.experiment_health(today=date(2026, 10, 28))["status"], "scheduled")
        self.assertTrue(rr.experiment_health(today=date(2026, 10, 29))["eligible"])
        self.assertFalse(rr.experiment_health(today=date(2026, 10, 29))["collection_verified"])
        self.assertEqual(old, (root / "experiments/entry-events-v3.json").read_bytes())

    def test_comparison_keeps_same_period_benchmarks_and_open_positions(self):
        result = backtest_store.comparison_summary({"validation_start": "2026-01-02", "sample_end": "2026-10-14",
            "results": {"validation": {"original": {"total_return_pct": 18.12, "closed_trade_count": 11,
                "sample_boundary_valuation_count": 6}, "benchmarks": {"spy_buy_hold_return_pct": 12.88}}}})
        self.assertEqual(result["period_end"], "2026-10-14")
        self.assertEqual(result["rows"][0]["open_position_valuations"], 6)
        self.assertEqual(result["benchmarks"]["spy_buy_hold_return_pct"], 12.88)
        self.assertFalse(result["new_forward_results"])

    def test_malformed_or_truncated_fast_dossier_gets_one_repair(self):
        for first in (SimpleNamespace(stop_reason="max_tokens", content=[SimpleNamespace(text='{"dossier":')]),
                      SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(text='not JSON')])):
            second = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(text=json.dumps(valid_dossier()))])
            with patch("anthropic.Anthropic"), patch.object(pm_view, "_messages_create_fast", return_value=first) as create, \
                 patch.object(pm_view, "_messages_create", return_value=second) as repair, \
                 patch.object(pm_view, "_generate_independent_assessment", return_value={}), \
                 patch.object(pm_view, "_call_with_timeout", side_effect=lambda fn, *args: fn()), \
                 patch.object(pm_view, "pm_identity_mismatch", return_value=False):
                result = pm_view.get_decision_dossier("TEST", {"action": "watch"}, [], {}, {}, api_key="test", fast=True)
            self.assertEqual(result["_source"], "claude · fast refresh")
            self.assertEqual(create.call_count, 1)
            self.assertEqual(repair.call_count, 1)
            self.assertEqual([create.call_args.kwargs["max_tokens"], repair.call_args.kwargs["max_tokens"]], [2600, 3600])

    def test_worker_records_safe_research_failure_diagnostic(self):
        error = rr.ResearchFailure("429 rate limit SECRET")
        job = {"id": "job-1", "job_type": "full_report", "ticker": "DASH"}
        with patch.object(worker.backend, "claim_next_job", return_value=job), \
             patch.object(worker, "process_job", side_effect=error), patch.object(worker.backend, "fail_job") as fail:
            self.assertEqual(worker.run_once(), (True, False))
        self.assertEqual(fail.call_args.kwargs["result"]["research_diagnostics"]["error_code"], "provider_quota")
        self.assertNotIn("SECRET", str(fail.call_args))

    def test_cooling_ticker_does_not_starve_next_candidate(self):
        recent = datetime.now(timezone.utc).isoformat()
        with patch.object(worker, "_api_key", return_value="test"), \
             patch.object(worker.backend, "enabled_watchlist_tickers", return_value=["AAA", "BBB"]), \
             patch.object(worker.backend, "read_json_table_many", return_value={}), \
             patch.object(worker.backend, "research_retry_states", return_value={"AAA": {"failures": 1, "last_failed_at": recent}}), \
             patch.object(worker.backend, "enqueue_job", return_value="job") as enqueue:
            result = worker.queue_stale_research_refresh(limit=1)
        self.assertEqual(enqueue.call_args.kwargs["ticker"], "BBB")
        self.assertEqual(result["cooldown_tickers"], ["AAA"])
