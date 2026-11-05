import unittest
import math
import pandas as pd
import performance_attribution as pa
from backtest_runner import simulate, Assumptions, matched_exposure_benchmark, replay
from test_backtest_runner import bars, enter

class PerformanceTests(unittest.TestCase):
    def test_log_bridge_reconciles_exactly_even_with_costs(self):
        b=bars(12)
        for i,day in enumerate(b.index):
            b.loc[day,['Open','High','Low','Close']]=[100+i,103+i,99+i,102+i]
        r=simulate(b,{b.index[3]:enter()},'original')
        a=pa.attribute(b,r)
        expected=100*math.log((1+a['system_return_pct']/100)/(1+a['same_stock_buy_hold_return_pct']/100))
        self.assertAlmostEqual(a['log_excess_total_pct'],expected)
        self.assertLess(a['log_excess_contributions_pct']['initial_wait'],0)

    def test_cash_yield_compounds_identically_for_idle_strategy_and_benchmark(self):
        b=bars(8)
        cash=bars(8)
        for i,day in enumerate(cash.index):
            cash.loc[day,['Open','High','Low','Close']]=[100+i,102+i,99+i,101+i]
        r=simulate(b,{},'original',cash_bars=cash)
        expected=float(cash.Close.iloc[-1]/cash.Open.iloc[0])
        self.assertAlmostEqual(r['curve'].equity.iloc[-1],expected)
        matched=matched_exposure_benchmark(b,pd.Series(0.,index=b.index),10,cash)
        self.assertAlmostEqual(matched['total_return_pct'],(expected-1)*100)

    def test_missing_cash_sessions_fail(self):
        b=bars()
        with self.assertRaises(ValueError):
            simulate(b,{},'original',cash_bars=b.drop(b.index[2]))

    def test_exit_window_includes_sold_trades_not_only_survivors(self):
        b=bars(40)
        b.loc[b.index[3],['Open','High','Low','Close']]=[90,91,89,90]
        b.loc[b.index[5]:,['Open','High','Low','Close']]=[105,106,104,105]
        r=simulate(b,{b.index[0]:enter()},'original',Assumptions(0))
        a=pa.attribute(b,r)
        self.assertEqual(a['windows']['exit_14']['mature_events'],1)
        self.assertGreater(a['windows']['exit_14']['mean_stock_return_pct'],0)

    def test_open_paper_position_not_liquidated(self):
        b=bars()
        r=simulate(b,{b.index[0]:enter()},'original',terminal_liquidation=False)
        self.assertIsNotNone(r['open_position'])
        self.assertEqual(r['trades'][0]['reason'],'open_valuation')
        self.assertTrue(r['trades'][0]['exit_is_sample_boundary'])

    def test_broad_preset_has_110_unique_names_before_results(self):
        import json
        from pathlib import Path
        p=json.loads((Path(__file__).parents[1]/'research/broad-universe-2026-11-05.json').read_text())
        names=[t for v in p['sectors'].values() for t in v]
        self.assertEqual(len(names),110)
        self.assertEqual(len(set(names)),110)
        self.assertEqual(len(p['sectors']),11)

    def test_real_cash_assumption_and_qqq_matched_exposure_are_reported(self):
        b=bars(240)
        report=replay({'TEST':b},b,start=b.index[210],end=b.index[230],validation_start=b.index[220],
                      comparison_benchmarks={'QQQ':b},cash_bars=b,compute=lambda a,c:{'action':'hold_off'})
        r=report['results']['validation']['original']
        self.assertEqual(r['entry_exit_attribution']['ticker_count'],1)
        self.assertIsNotNone(r['matched_exposure_qqq'])
        self.assertIn('SGOV',report['cash_basis'])

    def test_only_machine_precision_ohlc_noise_is_normalized(self):
        from backtest_runner import validate_bars
        b=bars();b.loc[b.index[0],'Low']=100.+1e-12;b.loc[b.index[0],'High']=101.
        self.assertEqual(validate_bars(b).Low.iloc[0],100.)
        b.loc[b.index[0],'Low']=100.001
        with self.assertRaises(ValueError):validate_bars(b)

    def test_full_universe_is_validated_before_any_engine_call(self):
        b=bars(240);calls=[]
        with self.assertRaises(ValueError):
            replay({'VALID':b,'MISSING':b.drop(b.index[225])},b,start=b.index[210],end=b.index[230],
                   validation_start=b.index[220],compute=lambda *a:calls.append(1))
        self.assertEqual(calls,[])
