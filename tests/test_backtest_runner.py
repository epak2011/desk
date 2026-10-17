import unittest
import pandas as pd
from backtest_runner import Assumptions, simulate, replay, policy, validate_bars


def bars(n=8, start="2026-01-01"):
    return pd.DataFrame({"Open": [100.] * n, "High": [101.] * n,
                         "Low": [99.] * n, "Close": [100.] * n,
                         "Volume": [1000.] * n}, index=pd.bdate_range(start, periods=n))


def enter(**extra):
    return dict(action="enter_now", stop=95., entry_trigger_fired=True,
                entry_level_held=True, **extra)


class ReplayTests(unittest.TestCase):
    def test_no_same_day_fill_and_costs_both_sides(self):
        frame = bars()
        frame.loc[frame.index[0], ["High", "Close"]] = [150, 150]
        result = simulate(frame, {frame.index[0]: enter()}, "original", Assumptions(10, 2))
        trade = result["trades"][0]
        self.assertEqual(trade["entry_date"], str(frame.index[1].date()))
        self.assertAlmostEqual(trade["net_return_pct"], ((1-.001)/(1+.001)-1)*100)

    def test_stop_wins_ambiguous_bar(self):
        frame = bars()
        frame.loc[frame.index[1], ["High", "Low"]] = [115, 90]
        result = simulate(frame, {frame.index[0]: enter()}, "v3_execution", Assumptions(0, exit_policy="legacy_14_session"))
        self.assertEqual(result["trades"][0]["exit_price"], 95)

    def test_gap_stop_fills_at_open_not_stop(self):
        frame = bars()
        frame.loc[frame.index[2], ["Open", "Low", "Close"]] = [85, 84, 90]
        result = simulate(frame, {frame.index[0]: enter()}, "v3_execution", Assumptions(0, exit_policy="legacy_14_session"))
        self.assertEqual(result["trades"][0]["exit_price"], 85)
        self.assertEqual(result["trades"][0]["reason"], "gap_stop")

    def test_missing_confirmation_stays_unknown(self):
        self.assertEqual(policy({"action": "enter_now"}, "v2_entry"), "unknown")

    def test_known_gap_target_precedes_later_intraday_low(self):
        frame = bars()
        frame.loc[frame.index[2], ["Open", "High", "Low", "Close"]] = [115, 120, 90, 100]
        result = simulate(frame, {frame.index[0]: enter()}, "v3_execution", Assumptions(0, exit_policy="legacy_14_session"))
        self.assertEqual(result["trades"][0]["exit_price"], 115)
        self.assertEqual(result["trades"][0]["reason"], "gap_target")

    def test_control_does_not_require_candidate_confirmation(self):
        self.assertEqual(policy({"action": "enter_now"}, "original"), "enter_now")

    def test_default_holds_past_14_and_30_without_target_sale(self):
        frame = bars(40)
        frame.loc[frame.index[2]:, ["Open", "High", "Low", "Close"]] = [120, 121, 119, 120]
        for version in ("original", "v2_combined", "v3_execution"):
            trade = simulate(frame, {frame.index[0]: enter()}, version)["trades"][0]
            self.assertEqual(trade["reason"], "end_of_sample")
            self.assertTrue(trade["exit_is_sample_boundary"])
            self.assertEqual(set(trade["checkpoint_gross_returns_pct"]), {"5", "14", "30"})

    def test_close_invalidation_exits_next_open_at_actual_gap(self):
        frame = bars()
        frame.loc[frame.index[2], ["Low", "Close"]] = [90, 94]
        frame.loc[frame.index[3], ["Open", "High", "Low", "Close"]] = [85, 90, 84, 89]
        trade = simulate(frame, {frame.index[0]: enter()}, "original")["trades"][0]
        self.assertEqual(trade["exit_price"], 85)
        self.assertEqual(trade["exit_date"], str(frame.index[3].date()))
        self.assertEqual(trade["reason"], "invalidation_exit_next_open")

    def test_avoid_alone_does_not_sell_but_broken_trend_does(self):
        frame = bars()
        signals = {frame.index[0]: enter(),
                   frame.index[2]: dict(action="avoid", ma50=90, ma200=90),
                   frame.index[4]: dict(action="avoid", ma50=110, ma200=110)}
        trade = simulate(frame, signals, "original")["trades"][0]
        self.assertEqual(trade["exit_date"], str(frame.index[5].date()))
        self.assertEqual(trade["reason"], "structural_exit_next_open")

    def test_invalid_initial_stop_does_not_create_unprotected_position(self):
        self.assertEqual(simulate(bars(), {bars().index[0]: dict(action="enter_now")}, "original")["trades"], [])

    def test_legacy_forced_horizon_remains_reproducible(self):
        trade = simulate(bars(30), {bars(30).index[0]: enter()}, "original",
                         Assumptions(0, exit_policy="legacy_14_session"))["trades"][0]
        self.assertEqual(trade["reason"], "max_horizon")
        self.assertEqual(trade["exit_date"], str(bars(30).index[14].date()))

    def test_no_fill_after_final_signal(self):
        frame = bars()
        self.assertEqual(simulate(frame, {frame.index[-1]: enter()}, "original")["trades"], [])

    def test_bad_data_rejected(self):
        frame = bars()
        frame.loc[frame.index[0], "Low"] = 110
        with self.assertRaises(ValueError):
            validate_bars(frame)

    def test_replay_compute_never_receives_future_bars(self):
        frame = bars(240)
        seen = []
        def compute(stock, bench):
            self.assertEqual(stock.index[-1], bench.index[-1])
            seen.append(stock.index[-1])
            return dict(action="hold_off")
        start, split, end = frame.index[210], frame.index[220], frame.index[230]
        report = replay({"TEST": frame}, frame, start=start, end=end,
                        validation_start=split, compute=compute)
        self.assertEqual(seen, list(frame.loc[start:end].index))
        self.assertEqual(report["results"]["validation"]["original"]["total_return_pct"], 0)

    def test_validation_starts_with_fresh_capital_and_no_training_position(self):
        frame = bars(240)
        start, split, end = frame.index[210], frame.index[220], frame.index[230]
        def compute(stock, bench):
            return enter() if stock.index[-1] == split - pd.offsets.BDay(1) else dict(action="hold_off")
        report = replay({"TEST": frame}, frame, start=start, end=end,
                        validation_start=split, compute=compute)
        self.assertEqual(report["results"]["validation"]["original"]["round_trips"], 0)


if __name__ == "__main__":
    unittest.main()
