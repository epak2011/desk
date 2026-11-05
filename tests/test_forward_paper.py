import unittest
from copy import deepcopy
import forward_paper as f

class ForwardPaperTests(unittest.TestCase):
    def test_next_open_only_and_both_costs(self):
        bar={'Open':100.,'Close':100.}
        s={'action':'enter_now','stop':95.,'observed_at':'2026-11-06T23:15:00+00:00'}
        first=f.advance(f.wallet(),bar,s,session='2026-11-06')
        self.assertEqual(first['shares'],0)
        second=f.advance(first,bar,None,session='2026-11-09')
        self.assertAlmostEqual(second['equity'],1/1.001)
        self.assertIsNotNone(second['stop'])
        second['pending']={'kind':'exit','observed_at':'2026-11-09T23:15:00+00:00'}
        out=f.advance(second,bar,None,session='2026-11-10')
        self.assertAlmostEqual(out['cash'],.999/1.001)

    def test_observed_after_open_cannot_fill_it(self):
        w=f.wallet();w['pending']={'kind':'entry','stop':90.,'observed_at':'2026-11-09T15:00:00+00:00'}
        out=f.advance(w,{'Open':100.,'Close':100.},None,session='2026-11-09')
        self.assertEqual(out['shares'],0.)
        self.assertEqual(out['late_orders_skipped'],1)
        self.assertIsNone(out['pending'])

    def test_before_close_signal_is_rejected(self):
        with self.assertRaises(ValueError):
            f.advance(f.wallet(),{'Open':100.,'Close':100.},{'action':'hold_off','observed_at':'2026-11-06T19:00:00+00:00'},session='2026-11-06')

    def test_split_and_dividend_accounting(self):
        w=f.wallet();w.update(cash=0.,shares=1.,stop=80.)
        out=f.advance(w,{'Open':50.,'Close':50.,'Stock Splits':2.,'Dividends':1.},None,session='2026-11-09',cost_bps=0)
        self.assertEqual(out['shares'],2.)
        self.assertEqual(out['stop'],40.)
        self.assertEqual(out['equity'],102.)
        self.assertEqual(w['shares'],1.,'must not mutate prior ledger')

    def test_same_cash_yield_for_idle_strategy_and_passive_dividend_cash(self):
        w=f.wallet();w['cash']=2.
        out=f.advance(w,{'Open':100.,'Close':100.},None,session='2026-11-09',cash_gap=1.01,cash_day=1.02)
        self.assertAlmostEqual(out['cash'],2*1.01*1.02)

    def test_no_calendar_sale_or_terminal_forced_fill(self):
        w=f.wallet();w.update(cash=0.,shares=.01,stop=90.)
        for _ in range(40):
            w=f.advance(w,{'Open':100.,'Close':100.},None,session='2026-11-09')
        self.assertEqual(w['shares'],.01)
        self.assertEqual(w['fills'],0)

    def test_coverage_gap_suppresses_strategy_return_not_benchmark(self):
        spec={'protocol_id':'test','starts_on':'2026-11-06','universe':['X'],'cost_bps_per_side':10}
        day={'session':'2026-11-06','equity':dict(strategy=1.,SPY=1.1,QQQ=1.2,same_universe=1.3,cash=1.),'exposure':0.,'turnover':0.}
        r=f.summary(spec,[day],missing_observations=1)
        self.assertEqual(r['status'],'incomplete')
        self.assertIsNone(r['rows'][0]['return_pct'])
        self.assertAlmostEqual(r['rows'][1]['return_pct'],10.)

    def test_source_drift_blocks_without_rewriting_registration(self):
        from pathlib import Path
        import json
        root=Path(__file__).parents[1]
        spec=json.loads((root/'research/forward-paper-110-v4.json').read_text())
        f.verify(spec,root)
        bad=deepcopy(spec);bad['cost_bps_per_side']=5
        with self.assertRaises(ValueError):f.verify(bad,root)

    def test_holiday_weekend_does_not_falsely_expire_friday_bar(self):
        from datetime import date
        self.assertFalse(f.latest_bar_is_stale(date(2027,1,15),date(2027,1,18)))
        self.assertTrue(f.latest_bar_is_stale(date(2027,1,15),date(2027,1,21)))

    def test_late_order_suppresses_paper_alpha_headline(self):
        spec={'protocol_id':'test','starts_on':'2026-11-06','universe':['X'],'cost_bps_per_side':10}
        d={'session':'2026-11-09','equity':dict(strategy=1.,SPY=1.1,QQQ=1.2,same_universe=1.3,cash=1.),'exposure':0.,'turnover':0.,'late_orders_skipped':1}
        r=f.summary(spec,[d])
        self.assertEqual(r['status'],'incomplete');self.assertIsNone(r['rows'][0]['return_pct'])

    def test_start_cannot_be_backdated_even_with_recomputed_identity(self):
        import json
        from pathlib import Path
        root=Path(__file__).parents[1]
        s=json.loads((root/'research/forward-paper-110-v4.json').read_text())
        s['starts_on']='2026-01-01';s['protocol_id']=f.identity(s)
        with self.assertRaisesRegex(ValueError,'Prospective start'):f.verify(s,root)
