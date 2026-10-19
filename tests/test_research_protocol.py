import unittest
import json
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
import pandas as pd
import research_protocol as p
from backtest_runner import matched_exposure_benchmark, policy, simulate
from portfolio_ledger import replay_instructions
import research_event_store


class ProtocolTests(unittest.TestCase):
    def test_event_store_restarts_without_rewriting_observations(self):
        class Cursor:
            journal = {}
            observations = {}
            def execute(self, sql, params):
                if "entry_event_observation:" in str(params[0]):
                    self.observations.setdefault(params[0], json.loads(params[1]))
                elif sql.startswith("UPDATE"):
                    self.journal = json.loads(params[0])
            def fetchone(self):
                return [self.journal]
            def __enter__(self): return self
            def __exit__(self, *args): pass
        cur = Cursor()
        @contextmanager
        def connection():
            yield SimpleNamespace(cursor=lambda: cur)
        backend = SimpleNamespace(has_database=lambda: True, ensure_backend_schema=lambda: None,
                                  db_connection=connection, json_dumps=json.dumps)
        idx = pd.bdate_range("2026-10-05", periods=4)
        bars = pd.DataFrame({"Close": [99, 101, 102, 103]}, index=idx)
        args = dict(backend=backend, ticker="TEST", benchmark=bars, fingerprint="code",
            compute=lambda a,b: {"trigger": {"levels": {"buy_above": 100, "abort_below": 90}}})
        first = research_event_store.capture(history=bars, completed_before=date(2026,10,6), **args)
        self.assertNotIn("event", first)
        final = research_event_store.capture(history=bars, completed_before=date(2026,10,9), **args)
        self.assertEqual(final["event"]["confirmed_session"], "2026-10-08")
        n = len(cur.observations)
        research_event_store.capture(history=bars, completed_before=date(2026,10,9), **args)
        self.assertEqual(len(cur.observations), n)

    def advance(self, previous, day, price, level=100):
        return p.advance_event(previous, ticker="TEST", session=f"2026-10-{day:02}",
            close=price, state={"trigger": {"levels": {"buy_above": level, "abort_below": 90}}},
            fingerprint="frozen-code")

    def test_level_frozen_and_two_closes_after_event(self):
        j = self.advance({}, 5, 99)
        j = self.advance(j, 6, 101, level=110)
        event_id = j["event"]["event_id"]
        self.assertEqual(j["event"]["level"], 100)
        self.assertEqual(j["event"]["held_closes"], 0)
        self.assertIsNone(j["event"]["confirmed_session"])
        same = self.advance(j, 6, 105)
        self.assertEqual(same, j)
        j = self.advance(j, 7, 102)
        self.assertIsNone(j["event"]["confirmed_session"])
        j = self.advance(j, 8, 103)
        self.assertEqual(j["event"]["confirmed_session"], "2026-10-08")
        self.assertEqual(j["event"]["event_id"], event_id)

    def test_retrospective_snapshot_cannot_invent_event(self):
        j = self.advance({}, 5, 105)
        self.assertIsNone(j.get("event"))
        self.assertEqual(policy({"action": "enter_now"}, "event_early"), "unknown")

    def test_crossing_requires_prior_observation_and_stop_invalidates(self):
        j = self.advance({}, 5, 99)
        j = self.advance(j, 6, 101)
        j = self.advance(j, 7, 89)
        self.assertEqual(j["event"]["status"], "invalidated")

    def test_no_confirmation_before_cross_and_no_same_close_fill(self):
        j = self.advance({}, 5, 99)
        j = self.advance(j, 6, 101)
        state = {"action": "enter_now", "stop": 90, "entry_event": j["event"]}
        self.assertEqual(policy(state, "event_early"), "enter_now")
        self.assertEqual(policy(state, "event_confirmed"), "watch")
        idx = pd.bdate_range("2026-10-06", periods=3)
        bars = pd.DataFrame({"Open": 102., "High": 103., "Low": 100., "Close": 102., "Volume": 100}, index=idx)
        trade = simulate(bars, {idx[0]: state}, "event_early")["trades"][0]
        self.assertEqual(trade["entry_date"], "2026-10-07")

    def test_explicit_discretionary_choices_required(self):
        self.assertFalse(p.execution_instruction("trim")["executable"])
        self.assertFalse(p.execution_instruction("add", fraction=float("nan"))["executable"])
        self.assertTrue(p.execution_instruction("trim", fraction=1/3)["executable"])
        self.assertFalse(p.execution_instruction("raise_stop")["executable"])

    def test_registration_cannot_backdate_or_change_identity(self):
        args = dict(source_hashes={"source": "hash"}, universe=["B", "A"], assumptions={"cost": 10},
                    starts_on="2026-10-20", registered_at="2026-10-19T00:00:00+00:00")
        a = p.register_experiment(**args)
        b = p.register_experiment(**{**args, "assumptions": {"cost": 20}})
        self.assertNotEqual(a["experiment_id"], b["experiment_id"])
        with self.assertRaises(ValueError):
            p.register_experiment(**{**args, "starts_on": "2026-10-19"})
        self.assertTrue(p.registration_status(a, source_hashes=args["source_hashes"], ticker="A", session="2026-10-20")["eligible"])
        self.assertFalse(p.registration_status(a, source_hashes={"source": "different"}, ticker="A", session="2026-10-20")["eligible"])
        self.assertFalse(p.registration_status(a, source_hashes=args["source_hashes"], ticker="A", session="2026-10-19")["eligible"])

    def test_ledger_keeps_cash_and_requires_explicit_trim(self):
        idx = pd.bdate_range("2026-10-05", periods=6)
        bars = pd.DataFrame({"Open": 100., "High": 101., "Low": 99., "Close": 100., "Volume": 100}, index=idx)
        result = replay_instructions(bars, {idx[0]: [{"kind": "entry", "fraction": .25, "stop": 90}],
            idx[1]: [{"kind": "add", "fraction": .5}], idx[2]: [{"kind": "trim"}],
            idx[3]: [{"kind": "trim", "fraction": 1/3}]}, cost_bps=0)
        self.assertEqual(len(result["unresolved"]), 1)
        self.assertEqual([r["fill_date"] for r in result["fills"]], ["2026-10-06", "2026-10-07", "2026-10-09"])
        self.assertAlmostEqual(result["curve"].iloc[-1].equity, 1)
        self.assertAlmostEqual(result["curve"].iloc[-1].shares, .625/100*2/3)
        self.assertTrue(result["open_position_is_valuation_not_sale"])

    def test_ledger_raised_stop_is_not_lowered_and_invalidation_fills_next_open(self):
        idx = pd.bdate_range("2026-10-05", periods=6)
        bars = pd.DataFrame({"Open": 100., "High": 101., "Low": 99., "Close": 100., "Volume": 100}, index=idx)
        bars.loc[idx[3], ["Low", "Close"]] = [94, 95]
        bars.loc[idx[4], ["Open", "Low", "Close"]] = [85, 84, 85]
        result = replay_instructions(bars, {idx[0]: [{"kind": "entry", "fraction": 1, "stop": 90}],
             idx[1]: [{"kind": "raise_stop", "stop": 96}], idx[2]: [{"kind": "raise_stop", "stop": 92}]}, cost_bps=0)
        self.assertEqual(len(result["unresolved"]), 1)
        self.assertEqual(result["fills"][-1]["kind"], "exit")
        self.assertEqual(result["fills"][-1]["price"], 85)

    def test_fingerprint_strata_do_not_pool_controls(self):
        import engine_evaluation as e
        entries = [{"rule_action": "enter_now", "rule_code_fingerprint": fingerprint,
            "shadow_evaluations": [{"candidate": "confirmed_entry_hold", "version": "v1", "eligible": True,
                                    "action": "watch", "differs_from_live": True}],
            "outcome": {"forward_return_pct": value}} for fingerprint, value in [("old", -10), ("new", 10)]]
        results = e.paired_shadow_comparison(entries, separate_fingerprints=True)
        self.assertEqual(len(results), 2)
        self.assertEqual({r["original_code_fingerprint"] for r in results}, {"old", "new"})

    def test_matched_spy_keeps_gap_with_previous_exposure(self):
        idx = pd.bdate_range("2026-10-05", periods=3)
        bars = pd.DataFrame({"Open": [100, 200, 100], "Close": [100, 200, 100]}, index=idx)
        # Enter after first gap; own the subsequent loss even if exiting at open.
        result = matched_exposure_benchmark(bars, pd.Series([0, 1, 0], index=idx), 0)
        self.assertAlmostEqual(result["total_return_pct"], -50)
        self.assertEqual(matched_exposure_benchmark(bars, pd.Series(0, index=idx), 10)["total_return_pct"], 0)

    def test_partial_spy_exposure_drifts_before_next_gap(self):
        idx = pd.bdate_range("2026-10-05", periods=2)
        bars = pd.DataFrame({"Open": [100, 100], "Close": [200, 100]}, index=idx)
        self.assertAlmostEqual(matched_exposure_benchmark(bars, pd.Series([.5, 0], index=idx), 0)["total_return_pct"], 0)


if __name__ == "__main__":
    unittest.main()
