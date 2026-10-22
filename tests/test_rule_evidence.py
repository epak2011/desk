import ast
import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import backend_layer
import engine_evaluation as evaluation


def case(ticker="DEMO", day="2026-08-03", value=-4, action="enter_now", **updates):
    forward = value if action != "avoid" else -value
    row = {
        "id": f"{ticker}-{day}-{action}", "ts": day + "T10:00:00+00:00",
        "ticker": ticker, "rule_action": action, "auto_logged": True,
        "rule_engine_version": "test-engine", "setup_score": 8,
        "private_note": "Do not publish", "user_action": "private",
        "decision_context": {"market_regime": "bullish"},
        "outcome": {
            "evaluation_version": evaluation.EVALUATION_VERSION, "auto_scored": True,
            "decision_return_pct": value, "forward_return_pct": forward,
            "benchmark_return_pct": 2, "excess_return_pct": forward - 2,
            "decision_excess_pct": (forward - 2) * (-1 if action == "avoid" else 1),
            "directional_success": value > 0, "credited": value > 0,
            "integrity": {"calibration_eligible": True, "issues": []},
            "horizons": {"14": {"scored_date": "2026-08-21", "return_pct": forward}},
        },
    }
    row.update(updates)
    return row


class RuleEvidenceTests(unittest.TestCase):
    def test_reversed_input_order_has_identical_population_and_fingerprint(self):
        early = case(day="2026-08-03", value=-4)
        later = case(day="2026-08-03", value=12)
        later["id"] = "later"
        later["ts"] = "2026-08-03T12:00:00+00:00"
        a = evaluation.rules_performance_snapshot([early, later])
        b = evaluation.rules_performance_snapshot([later, early])
        self.assertEqual(a["evidence_id"], b["evidence_id"])
        self.assertEqual(a["directional_families"][0]["avg_return_pct"], -4)
        self.assertEqual(a["flags"][0]["count"], 1)

    def test_current_version_integrity_and_rules_source_define_population(self):
        good = case()
        old = case("OLD")
        old["outcome"]["evaluation_version"] -= 1
        corrupt = case("CORRUPT")
        corrupt["outcome"]["integrity"] = {"calibration_eligible": False, "issues": ["reference_price_discontinuity"]}
        manual = case("MANUAL", auto_logged=False)
        pending = case("PENDING", outcome=None)
        snap = evaluation.rules_performance_snapshot([good, old, corrupt, manual, pending])
        self.assertEqual([row["ticker"] for row in snap["review_cases"]], ["DEMO"])
        self.assertEqual(snap["evidence_quality"]["ignored_non_rule_log_count"], 1)
        self.assertEqual(snap["evidence_quality"]["excluded_directional_reasons"], {
            "old_evaluation_version": 1, "reference_price_discontinuity": 1, "unscored": 1,
        })

    def test_positive_underperformer_is_distinguished_from_absolute_loss(self):
        winner = case("A", value=1)
        winner["outcome"]["directional_success"] = False
        winner["outcome"]["credited"] = False
        loser = case("B", value=-5)
        snap = evaluation.rules_performance_snapshot([winner, loser])
        row = snap["directional_families"][0]
        self.assertEqual(row["positive_direction_rate_pct"], 50)
        self.assertEqual(row["success_rate_pct"], 0)
        self.assertEqual(row["avg_decision_return_pct"], -2)
        self.assertEqual(row["avg_benchmark_return_pct"], 2)
        self.assertEqual(row["avg_decision_excess_pct"], -4)

    def test_diagnostics_do_not_mix_long_and_avoid_families(self):
        rows = [case(str(i), value=-4) for i in range(3)]
        rows += [case(str(i + 3), value=4, action="avoid") for i in range(3)]
        snap = evaluation.rules_performance_snapshot(rows)
        regimes = [row for row in snap["performance_slices"] if row["dimension"] == "Regime"]
        self.assertEqual(len(regimes), 2)
        self.assertEqual({row["family"]: row["avg_decision_return_pct"] for row in regimes}, {"long": -4, "avoid": 4})

    def test_overlapping_paths_are_exposed_without_redefining_live_gate(self):
        rows = [case(day="2026-08-03"), case(day="2026-08-11")]
        snap = evaluation.rules_performance_snapshot(rows)
        self.assertEqual(snap["flags"][0]["count"], 2)
        self.assertEqual(snap["non_overlapping_families"][0]["count"], 1)

    def test_case_payload_is_allowlisted_and_ordinary_accounts_get_no_cases(self):
        snap = evaluation.rules_performance_snapshot([case()])
        public = evaluation.evidence_payload(snap)
        private = evaluation.evidence_payload(snap, include_cases=True)
        self.assertNotIn("review_cases", public)
        self.assertNotIn("weakest_cases", public)
        self.assertNotIn("decisions", private)
        self.assertNotIn("private_note", json.dumps(private))
        self.assertNotIn("user_action", json.dumps(private))

    def test_owner_streamlit_reads_worker_outcome_and_never_uses_stale_copy(self):
        source = Path("app.py").read_text()
        tree = ast.parse(source)
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in {"_rules_evidence_entries", "rules_performance_snapshot"}]
        canonical = case()
        local = copy.deepcopy(canonical)
        local["outcome"] = None
        backend = MagicMock()
        backend.has_database.return_value = True
        backend.read_decision_logs.return_value = [canonical]
        namespace = {
            "st": SimpleNamespace(session_state=SimpleNamespace(store={"decisions_log": [local]})),
            "_is_owner_account": lambda: True, "backend_layer": backend, "engine_evaluation": evaluation,
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), "app.py", "exec"), namespace)
        snap = namespace["rules_performance_snapshot"]()
        self.assertEqual(snap["flags"][0]["count"], 1)
        self.assertEqual(snap["flags"][0]["avg_decision_return_pct"], -4)
        backend.import_rule_decision_logs.assert_not_called()
        namespace["_is_owner_account"] = lambda: False
        self.assertEqual(namespace["rules_performance_snapshot"]()["flags"][0]["count"], 0)

    def test_canonical_read_failure_does_not_fall_back_to_zero_case_history(self):
        with patch.object(backend_layer, "read_decision_logs", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                backend_layer.read_rule_evidence(include_cases=True)

    def test_ordinary_calibration_reads_the_persisted_snapshot_not_decisions_log(self):
        """Guards the fix for the dominant Supabase egress driver: /v1/calibration
        (include_cases=False) must not re-run the unbounded decisions_log scan when
        the worker has already persisted a snapshot to engine_review_status.
        """
        persisted = {
            "score_version": "v9", "evidence_id": "abc123", "summary": {"n": 3},
            "flags": [{"count": 3}], "alerting": [], "directional_families": [],
            "non_overlapping_families": [], "engine_version_families": [], "patience_summary": {},
            "evidence_quality": {}, "shadow_performance": {}, "shadow_promotion": {},
            "paired_shadow_comparison": {}, "paired_shadow_by_fingerprint": {},
            "non_overlapping_logic_review_flags": [], "confidence_calibration": {},
            "confidence_calibration_by_family": {},
            "generated_at": "2026-10-22T00:00:00+00:00", "scored_now": 3, "remaining_due": 0,
        }
        with (
            patch.object(backend_layer, "read_engine_review_status", return_value=persisted),
            patch.object(backend_layer, "read_decision_logs", side_effect=AssertionError(
                "read_rule_evidence must not fall back to the unbounded decisions_log "
                "scan when a persisted engine_review_status snapshot is available"
            )),
        ):
            report = backend_layer.read_rule_evidence(include_cases=False)
        self.assertEqual(report["evidence_id"], "abc123")
        self.assertEqual(report["flags"], [{"count": 3}])
        self.assertFalse(report["case_review_available"])
        self.assertEqual(report["generated_at"], "2026-10-22T00:00:00+00:00")

    def test_calibration_is_pending_before_the_worker_has_ever_run(self):
        with (
            patch.object(backend_layer, "read_engine_review_status", return_value={}),
            patch.object(backend_layer, "read_decision_logs", side_effect=AssertionError("unbounded fallback")),
        ):
            report = backend_layer.read_rule_evidence(include_cases=False)
        self.assertEqual(report["publication_status"], "pending")
        self.assertNotIn("summary", report)

    def test_append_import_cannot_overwrite_a_scored_canonical_record(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 1
        with (
            patch.object(backend_layer, "ensure_backend_schema"),
            patch.object(backend_layer, "db_connection") as db,
        ):
            db.return_value.__enter__.return_value = conn
            count = backend_layer.import_rule_decision_logs([case(), case("MANUAL", auto_logged=False)])
        sql, args = cur.execute.call_args.args
        self.assertIn("ON CONFLICT (id) DO NOTHING", sql)
        rows = json.loads(args[0])
        self.assertEqual(len(rows), 1)
        self.assertNotIn("private_note", rows[0])
        self.assertEqual(count, 1)

    def test_missing_old_inputs_are_unknown_not_clean_entry_evidence(self):
        entry = case(rule_engine_version=None)
        snap = evaluation.rules_performance_snapshot([entry])
        self.assertEqual(snap["engine_version_families"][0]["engine_version"], "Unrecorded")
        self.assertIsNone(snap["review_cases"][0]["extension_warning"])
        self.assertIsNone(snap["review_cases"][0]["entry_is_projected"])
        self.assertEqual(snap["evidence_quality"]["recorded_input_coverage"]["code_fingerprint"], 0)
        self.assertEqual(snap["evidence_quality"]["recorded_input_coverage"]["regime"], 1)

    def test_provenance_distinguishes_implementations_without_relabelling_old_calls(self):
        worker = evaluation.engine_provenance("worker")
        streamlit = evaluation.engine_provenance("streamlit")
        self.assertNotEqual(worker["rule_code_fingerprint"], streamlit["rule_code_fingerprint"])
        self.assertEqual(worker, evaluation.engine_provenance("worker"))
        snap = evaluation.rules_performance_snapshot([case()])
        self.assertIsNone(snap["review_cases"][0]["rule_code_fingerprint"])


if __name__ == "__main__":
    unittest.main()

