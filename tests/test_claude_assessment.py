import json
import unittest
from datetime import date, datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
import pandas as pd
import claude_assessment as c


def answer(action="BUY_NOW"):
    return {"entry":{"action":action,"reasoning":"Evidence", "counterargument":"Risk", "what_changes_call":"Reversal"},
            "ownership":{"action":"HOLD","reasoning":"Conditional", "what_changes_call":"Thesis breaks"},
            "confidence":6,"missing_information":["News"],"evidence_notes":["Price above MA"]}


def pair(action="BUY_NOW", rule="watch"):
    return c.freeze(answer(action), c.evidence("TEST",{"price":100},{}),model="test-model",rule_action=rule,rule_fingerprint="rules-v1")


class IndependentTests(unittest.TestCase):
    def test_generation_retains_actual_model_and_freezes_rule_only_after_prompt(self):
        import pm_view
        msg=SimpleNamespace(stop_reason="end_turn",model="actual-model",content=[SimpleNamespace(text=json.dumps(answer()))])
        with patch.object(pm_view,"_messages_create",return_value=msg) as create, patch.object(pm_view,"_call_with_timeout",side_effect=lambda fn,*args:fn()):
            p=pm_view._generate_independent_assessment(MagicMock(),"TEST",{"action":"RULE_SECRET","price":100},{})
        self.assertEqual(p["status"],"ready")
        self.assertEqual(p["model"],"actual-model")
        self.assertEqual(p["paired_rule"]["action"],"RULE_SECRET")
        self.assertNotIn("RULE_SECRET",create.call_args.kwargs["messages"][0]["content"])
        self.assertTrue(p["paired_rule"]["code_fingerprint"])

    def test_public_contract_preserves_independent_call_without_changing_rules(self):
        import public_contract
        now=datetime.now(timezone.utc).isoformat()
        p=pair()
        dossier={"dossier":"Memo","pm_narrative":"Business","bullets":{"thesis":"A business"},"_source":"claude","independent_assessment":p}
        rule={"action":"watch"}
        read=public_contract.research_payload("TEST",report={"dossier":dossier,"_worker_generated_at":now},rule=rule)
        self.assertEqual(read["claude_assessment"]["status"],"ready")
        self.assertTrue(read["claude_assessment"]["disagrees"])
        self.assertEqual(rule,{"action":"watch"})

    def test_prompt_excludes_rule_decision_and_prior_prose(self):
        inputs=c.evidence("TEST",{"price":100,"action":"SECRET_RULE","bias":"SECRET_BIAS","setup_score":9},{"thesis":"OLD_AI"})
        text=c.prompt(inputs)
        for secret in ("SECRET_RULE","SECRET_BIAS","OLD_AI","setup_score"):
            self.assertNotIn(secret,text)
        self.assertIn("0.10 means 10%",text)

    def test_debt_equity_units_are_not_a_thirty_three_times_leverage_claim(self):
        inputs=c.evidence("TEST",{}, {"debt_to_equity":33.276})
        self.assertEqual(inputs["business"]["debt_to_equity_pct"],33.276)
        self.assertNotIn("debt_to_equity",inputs["business"])
        self.assertIn("0.33276",inputs["units"]["debt_to_equity_pct"])

    def test_ready_legacy_stale_and_unknown_rule_badges(self):
        p=pair()
        self.assertTrue(c.display(p,{},rule_action="watch")["disagrees"])
        self.assertFalse(c.display(p,{},rule_action="unknown")["disagrees"])
        self.assertFalse(c.display(p,{},rule_action="enter_now")["disagrees"])
        self.assertNotIn("inputs",c.display(p,{},rule_action="watch"))
        self.assertFalse(c.display(p,{},rule_action="watch",blocked=True)["disagrees"])
        p["generated_at"]=(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
        self.assertEqual(c.display(p,{},rule_action="watch")["status"],"stale")
        legacy=c.display(None,{"action":"ENTER","reasoning":"Legacy"},rule_action="watch")
        self.assertFalse(legacy["independent"])
        self.assertFalse(legacy["disagrees"])

    def test_invalid_action_and_confidence_rejected(self):
        for value in (float("nan"),0,True,11):
            a=answer();a["confidence"]=value
            with self.assertRaises(ValueError):c.validate(a)
        with self.assertRaises(ValueError):c.validate(answer("SELL"))

    def test_append_only_snapshot_freezes_both_calls(self):
        p=pair();cur=MagicMock()
        c.record(cur,"TEST",{"dossier":{"independent_assessment":p}},json.dumps)
        sql,args=cur.execute.call_args.args
        self.assertIn("DO NOTHING",sql)
        self.assertEqual(json.loads(args[1])["paired_rule"]["action"],"watch")
        self.assertIn("prompt_sha256",json.loads(args[1]))

    def test_checkpoint_dates_costs_cash_and_no_sale(self):
        p=pair();p.update(ticker="TEST",generated_at="2026-10-29T20:01:00+00:00")
        frame=pd.DataFrame({"Open":100.,"Close":110.},index=pd.bdate_range("2026-10-29",periods=35))
        rows=c.score_checkpoints(p,{s:frame for s in ("TEST","SPY","QQQ")},completed_before=date(2027,1,1))
        self.assertEqual([r["horizon_sessions"] for r in rows],[5,14,30])
        self.assertEqual(rows[0]["start"],"2026-10-30")
        self.assertAlmostEqual(rows[0]["claude_return_pct"],(110*.999/(100*1.001)-1)*100)
        self.assertEqual(rows[0]["rules_return_pct"],0)
        self.assertFalse(rows[1]["calendar_sale"])
        self.assertFalse(c.summarize(rows)["ownership_scored"])
        missing=frame.drop(frame.index[3])
        self.assertEqual(c.score_checkpoints(p,{"TEST":frame,"SPY":frame,"QQQ":missing},completed_before=date(2027,1,1)),[])

    def test_nonoverlap_all_calls_disagreements_and_model_strata(self):
        row={"assessment_id":"a","version":c.VERSION,"model":"m","rule_fingerprint":"r","prompt_template_sha256":"p","ticker":"TEST","horizon_sessions":5,"start":"2026-10-01","end":"2026-10-07","disagrees":False,"claude_return_pct":10,"rules_return_pct":10,"spy_return_pct":2,"qqq_return_pct":3}
        overlap={**row,"assessment_id":"b","start":"2026-10-02","disagrees":True}
        rows=[{**r,"measurement_protocol_sha256":c.PROTOCOL_HASH} for r in (row,overlap,{**row,"model":"m2"})]
        report=c.summarize(rows)
        self.assertEqual(report["groups"][0]["sample_count"],1)
        self.assertEqual(report["groups"][1]["sample_count"],0)
        self.assertEqual({r["model"] for r in report["groups"]},{"m","m2"})
        self.assertFalse(report["automatic_winner_or_promotion"])


if __name__=="__main__":unittest.main()
