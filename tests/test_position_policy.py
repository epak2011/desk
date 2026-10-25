import ast
from pathlib import Path
import unittest
import subprocess
import pandas as pd
import position_policy
import tactical
from backtest_runner import replay, simulate, metrics, Assumptions


class PositionPolicyTests(unittest.TestCase):
    def test_active_workflow_cannot_select_legacy_calendar_exit(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/rules-replay.yml").read_text()
        self.assertIn("REPLAY_EXIT_POLICY: signal_invalidation", workflow)
        self.assertNotIn("inputs.exit_policy", workflow)
        self.assertNotIn("- legacy_14_session", workflow)

    def test_position_age_never_creates_a_live_exit(self):
        for age in (0, 13, 14, 15, 30, 365):
            self.assertIsNone(position_policy.full_exit_reason(
                dict(price=100, action="hold", ma50=99, ma200=98,
                     age=age, holding_sessions=age, sessions_held=age), 95))

    def test_worker_workflow_preserves_success_and_failure_status(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/desk-worker.yml").read_text()
        tail = workflow.split("          worker_status=$?",1)[1].split("\n      - name:",1)[0]
        tail = "worker_status=$?\n" + "\n".join(line[10:] if line.startswith("          ") else line for line in tail.splitlines())
        for command, expected in (("true",0),("false",1)):
            result = subprocess.run(["bash","-c","set +e\n"+command+"\n"+tail],capture_output=True,text=True)
            self.assertEqual(result.returncode,expected)
        if "Re-dispatch this workflow" in workflow:
            redispatch = workflow.split("Re-dispatch this workflow",1)[1]
            self.assertIn("if: always()",redispatch)

    def test_stop_and_structural_exit_are_shared(self):
        self.assertEqual(position_policy.full_exit_reason(dict(price=90), 95), "invalidation")
        self.assertIsNone(position_policy.full_exit_reason(dict(price=100, action="avoid"), 95))
        self.assertEqual(position_policy.full_exit_reason(dict(price=100, action="avoid", ma50=101, ma200=102),95), "structural")

    def test_nonfinite_inputs_are_not_exit_evidence(self):
        for bad in (float("nan"), float("inf"), None):
            self.assertIsNone(position_policy.full_exit_reason(dict(price=bad),95))

    def test_streamlit_stop_takes_precedence_over_earnings(self):
        # Load only the pure PM function, never import or run Streamlit's app.
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "app.py").read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "position_management_read")
        scope = {"position_policy": position_policy}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "app.py", "exec"), scope)
        result = scope[fn.name](dict(entry_price=100,stop_price=95),dict(price=90,event_risk_days=1))
        self.assertEqual(result["action"], "Exit")

    def test_catchup_cannot_bypass_atr_gate(self):
        dates = pd.bdate_range("2024-01-01",periods=320)
        prices = pd.Series([100 * 1.004**n for n in range(320)],index=dates)
        stock = pd.DataFrame(dict(Open=prices,High=prices*1.001,Low=prices*.999,Close=prices,Volume=1e6))
        bench = stock.copy()
        bench["Close"] = [100*1.001**n for n in range(320)]
        result = tactical.compute(stock,bench)
        self.assertFalse(result["atr_ok"])
        self.assertEqual(result["action"],"hold_off")

    def test_boundary_value_is_not_closed_trade(self):
        dates = pd.bdate_range("2026-01-01",periods=4)
        frame = pd.DataFrame(dict(Open=100.,High=101.,Low=99.,Close=100.,Volume=1000.),index=dates)
        result = simulate(frame,{dates[0]:dict(action="enter_now",stop=95)},"original")
        report = metrics(result["curve"],result["trades"])
        self.assertEqual(report["round_trips"],0)
        self.assertEqual(report["sample_boundary_valuation_count"],1)
        self.assertIsNone(report["win_rate_pct"])

    def test_unknown_trigger_flag_remains_unknown(self):
        history = pd.DataFrame({"Close":[101,102]},index=pd.to_datetime(["2026-10-12","2026-10-13"]))
        result = tactical.shadow_entry_confirmation(history,{"levels":{"buy_above":100}},None,as_of="2026-10-14")
        self.assertIsNone(result["entry_trigger_fired"])

    def test_candidate_nan_is_not_complete(self):
        import engine_candidates
        result = engine_candidates.shadow_evaluations(dict(action="avoid",price=float("nan"),ma50=99,ma200=100,rs=.8,rs_delta=0,tech_delta=-1))
        row = next(r for r in result if r["candidate"]=="confirmed_structural_avoid")
        self.assertFalse(row["eligible"])

    def test_manifests_and_alias_are_explicit(self):
        dates = pd.bdate_range("2024-01-01",periods=240)
        frame = pd.DataFrame(dict(Open=100.,High=101.,Low=99.,Close=100.,Volume=1000.),index=dates)
        kwargs = dict(start=dates[210],end=dates[230],validation_start=dates[220],compute=lambda a,b:dict(action="hold_off"))
        report = replay({"TEST":frame},frame,**kwargs)
        self.assertNotIn("v3_execution",report["versions"])
        self.assertEqual(report["version_aliases"],{"v3_execution":"v2_combined"})
        control = report["strategy_manifests"]["original"]
        self.assertFalse(report["assumptions"]["calendar_exit_enabled"])
        self.assertNotIn("holding_sessions", report["assumptions"])
        self.assertNotIn("target_r", report["assumptions"])
        self.assertEqual(control["label"],"Technical control")
        self.assertEqual(len(control["strategy_id"]),64)
        second = replay({"TEST":frame},frame,assumptions=Assumptions(cost_bps_per_side=20),**kwargs)
        self.assertNotEqual(control["strategy_id"],second["strategy_manifests"]["original"]["strategy_id"])


if __name__ == "__main__":
    unittest.main()
