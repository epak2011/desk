import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import validation_engine as e
import validation_protocol as protocol
from validation_capture import market_window
from validation_ingest import record_plan
from validation_stats import bootstrap_interval, candidate_outcomes, assess
from validation_store import Store, build_report
from validation_execution import validate_statement,validate_fill_identity,reconcile

ROOT=Path(__file__).resolve().parents[1]


def spec():
    return {'protocol_id':'test','universe':['X','Y'],'initial_capital':100000.,
        'cost_scenarios_bps':[10,25],'primary_cost_bps':10,'participation_limit':.01,
        'selection_count':2,'registered_at':'2026-11-05T21:05:00+00:00',
        'starts_on':'2026-11-06','complete_policy_id':'complete-v1','universe_basis':'fixture',
        'success_gates':{'annualized_excess_pp':2,'risk_ratio':1.1},'bootstrap':{'draws':200,'seed':1}}


def snapshot(session='2026-11-05',full=True):
    window=market_window(session)
    received=session+'T23:15:00+00:00'
    candidates={s:{'action':'enter_now','score':9.,'price':100.,'ma50':95.,'ma200':90.,'stop':80.,
        'selected':True,'reason':'fixture rule','sector':'Test'} for s in ('X','Y')}
    result={**window,'snapshot_id':'snapshot-'+session,'received_at':received,'candidates':candidates}
    if full:
        result.update(full_plan={'policy_id':'complete-v1','session':session,'observed_at':session+'T22:00:00+00:00',
            'selection':{'version':'one'},'sizing':{'version':'one'},'risk':{'version':'one'},'execution':{'version':'one'},
            'target_weights':{'X':.5,'Y':.5},'stops':{'X':80.,'Y':80.},
            'candidates':[{'ticker':s,**r} for s,r in candidates.items()]},
            full_plan_received_at=session+'T22:05:00+00:00')
    return result


def bars(price=100):
    return {s:{'Open':float(price),'Close':float(price),'split':1.,'dividend':0.,
        'prior_median_dollar_volume':100_000_000.} for s in ('X','Y','SPY','QQQ')}


class Accounting(unittest.TestCase):
    def test_same_day_observation_never_fills_same_open(self):
        s=spec();day=e.process_day(e.initial_state(s),s,'2026-11-06',bars(),snapshot('2026-11-06'))
        self.assertEqual(day['books']['10']['full_system']['shares'],{})
        following=e.process_day(day,s,'2026-11-09',bars(110),snapshot('2026-11-09'))
        fills=following['books']['10']['full_system']['fills']
        self.assertEqual({f['price'] for f in fills},{110.})

    def test_seed_fills_at_registered_open_with_same_benchmark_cost(self):
        s=spec();initial=e.initial_state(s,snapshot());saved=copy.deepcopy(initial)
        day=e.process_day(initial,s,'2026-11-06',bars(),snapshot('2026-11-06'))
        a=day['books']['10']['full_system'];b=day['books']['10']['SPY']
        self.assertAlmostEqual(a['equity'],b['equity'])
        self.assertAlmostEqual(a['equity'],100000/1.001)
        self.assertEqual(initial,saved)
        self.assertLess(day['books']['25']['full_system']['equity'],a['equity'])

    def test_sell_before_buy_and_no_leverage(self):
        p=e.new_portfolio(100000);p.update(cash=0.,shares={'X':1000.},
            pending={'weights':{'Y':1.},'stops':{},'recorded_at':'2026-11-05T23:00:00+00:00'})
        out=e.advance_portfolio(p,bars(),'2026-11-06')
        self.assertEqual([f['side'] for f in out['fills']],['sell','buy'])
        self.assertNotIn('X',out['shares']);self.assertGreaterEqual(out['cash'],0)
        self.assertAlmostEqual(out['equity'],100000*.999/1.001)

    def test_split_cash_dividend_and_frozen_stop(self):
        p=e.new_portfolio(100000);p.update(cash=0.,shares={'X':1000.},stops={'X':80.})
        b=bars();b['X'].update(Open=50.,Close=50.,split=2.,dividend=1.)
        out=e.advance_portfolio(p,b,'2026-11-06')
        self.assertEqual(out['shares']['X'],2000.)
        self.assertEqual(out['stops']['X'],40.)
        self.assertEqual(out['equity'],102000.)

    def test_liquidity_limit_uses_prior_volume_and_expires_partial_order(self):
        p=e.new_portfolio(100000);p['pending']={'weights':{'X':1.},'recorded_at':'2026-11-05T23:00:00+00:00'}
        b=bars();b['X']['prior_median_dollar_volume']=100000.
        out=e.advance_portfolio(p,b,'2026-11-06')
        self.assertEqual(out['shares']['X'],10.)
        self.assertTrue(out['unfilled']);self.assertIsNone(out['pending'])

    def test_late_receipt_cannot_replay_open(self):
        p=e.new_portfolio(100000);p['pending']={'weights':{'X':1.},'recorded_at':'2026-11-06T15:00:00+00:00'}
        out=e.advance_portfolio(p,bars(),'2026-11-06')
        self.assertFalse(out['shares']);self.assertEqual(out['issues'][0]['kind'],'late_order_cancelled')

    def test_open_gap_stop_executes_at_actual_open_not_stop_price(self):
        s=spec();p=e.initial_state(s,snapshot());d=e.process_day(p,s,'2026-11-06',bars(),snapshot('2026-11-06'))
        obs=snapshot('2026-11-09');obs['candidates']['X'].update(price=70.,action='avoid',ma50=90.,ma200=85.)
        b=bars();b['X'].update(Open=75.,Close=70.)
        d=e.process_day(d,s,'2026-11-09',b,obs)
        b=bars();b['X'].update(Open=60.,Close=65.)
        d=e.process_day(d,s,'2026-11-10',b,snapshot('2026-11-10'))
        fills=[f for f in d['books']['10']['technical_timing']['fills'] if f['ticker']=='X' and f['side']=='sell']
        self.assertEqual(fills[0]['price'],60.)

    def test_missing_snapshot_suppresses_active_returns_not_spy(self):
        s=spec();d=e.process_day(e.initial_state(s,snapshot()),s,'2026-11-06',bars())
        rows=e.scoreboard(s,[d])['rows'];a=next(r for r in rows if r['arm']=='technical_timing')
        spy=next(r for r in rows if r['arm']=='SPY')
        self.assertIsNone(a['return_pct']);self.assertIsNotNone(spy['return_pct'])

    def test_missing_complete_policy_does_not_poison_technical_controls(self):
        s=spec();d=e.process_day(e.initial_state(s,snapshot(full=False)),s,'2026-11-06',bars(),snapshot('2026-11-06',full=False))
        rows=e.scoreboard(s,[d])['rows']
        self.assertIsNone(next(r for r in rows if r['arm']=='full_system')['return_pct'])
        self.assertIsNotNone(next(r for r in rows if r['arm']=='technical_timing')['return_pct'])

    def test_held_security_disappearance_cannot_be_dropped(self):
        p=e.new_portfolio(100000);p.update(shares={'DELISTED':100.})
        with self.assertRaisesRegex(ValueError,'Held security missing'):e.advance_portfolio(p,bars(),'2026-11-06')

    def test_complete_universe_and_time_order_are_mandatory(self):
        s=spec();b=bars();b.pop('Y')
        with self.assertRaises(ValueError):e.process_day(None,s,'2026-11-06',b)
        d=e.process_day(None,s,'2026-11-06',bars())
        with self.assertRaises(ValueError):e.process_day(d,s,'2026-11-06',bars())

    def test_simple_trend_and_two_variants_are_different_frozen_rules(self):
        c=snapshot()['candidates'];p=e.new_portfolio(100000)
        c['X']['action']='watch'
        self.assertNotIn('X',e.technical_weights(p,c,'technical_timing')[0])
        self.assertIn('X',e.technical_weights(p,c,'no_entry_confirmation')[0])
        self.assertIn('X',e.technical_weights(p,c,'simple_trend')[0])
        p.update(shares={'X':1.},stops={'X':70.});c['X'].update(action='avoid',price=80.,ma50=95.,ma200=90.)
        self.assertNotIn('X',e.technical_weights(p,c,'technical_timing')[0])
        self.assertIn('X',e.technical_weights(p,c,'no_structural_exit')[0])

    def test_monthly_selection_holds_and_keeps_unused_slots_cash(self):
        s=spec();p=e.new_portfolio(100000);obs=snapshot();obs['full_plan']['candidates'][1]['selected']=False
        p,issue=e.schedule(p,'selected_passive',obs,s)
        self.assertIsNone(issue);self.assertEqual(p['pending']['weights'],{'X':.5})
        p['pending']=None;obs.pop('full_plan')
        p,issue=e.schedule(p,'selected_passive',obs,s)
        self.assertIsNone(issue);self.assertIsNone(p['pending'])

    def test_invalid_numeric_inputs_rejected(self):
        for value in (float('nan'),float('inf'),-1,True):
            with self.assertRaises(ValueError):e.number(value)


class Evidence(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.store=Store(self.conn.cursor(),sqlite=True);self.store.initialize()

    def tearDown(self):self.conn.close()

    def test_append_idempotence_conflict_and_tamper_detection(self):
        self.assertTrue(self.store.append('p','day','one',{'cash':1}))
        self.assertFalse(self.store.append('p','day','one',{'cash':1}))
        with self.assertRaises(ValueError):self.store.append('p','day','one',{'cash':2})
        self.assertTrue(self.store.audit('p')['verified'])
        self.conn.execute("UPDATE validation_records SET payload='{}'")
        with self.assertRaises(ValueError):self.store.audit('p')

    def test_duplicate_activation_cannot_reset_bad_history(self):
        self.store.activate(spec())
        with self.assertRaises(ValueError):self.store.activate(spec())

    def test_complete_plan_arrival_is_server_time_and_immutable(self):
        s=spec();plan=snapshot()['full_plan'];window=market_window('2026-11-05')
        out=record_plan(self.store,s,plan,'2026-11-05T22:05:00+00:00',window)
        self.assertEqual(out['status'],'receipt_recorded')
        self.assertEqual(record_plan(self.store,s,plan,'2026-11-05T22:06:00+00:00',window)['status'],'already_recorded')
        changed=copy.deepcopy(plan);changed['target_weights']={'X':1.}
        with self.assertRaises(ValueError):record_plan(self.store,s,changed,'2026-11-05T22:06:00+00:00',window)
        with self.assertRaises(ValueError):record_plan(self.store,s,plan,'2026-11-06T15:00:00+00:00',window)

    def test_missing_or_duplicated_rejections_and_leverage_rejected(self):
        plan=snapshot()['full_plan'];plan['candidates'].pop()
        with self.assertRaises(ValueError):e.validate_plan(plan,['X','Y'],'complete-v1')
        plan=snapshot()['full_plan'];plan['target_weights']={'X':2.}
        with self.assertRaises(ValueError):e.validate_plan(plan,['X','Y'],'complete-v1')

    def test_sealed_snapshot_cannot_gain_a_later_plan(self):
        self.store.append('test','snapshot','2026-11-05',snapshot(full=False))
        with self.assertRaises(ValueError):record_plan(self.store,spec(),snapshot()['full_plan'],'2026-11-05T23:20:00+00:00',market_window('2026-11-05'))

    def test_owner_summary_excludes_candidate_events_and_private_plan(self):
        s=spec();self.store.activate(s)
        self.store.append('test','snapshot','2026-11-05',snapshot())
        d=e.process_day(e.initial_state(s,snapshot()),s,'2026-11-06',bars(),snapshot('2026-11-06'))
        self.store.append('test','day','2026-11-06',d)
        report=build_report(self.store,'test')
        self.assertEqual(len(report['rows']),18)
        self.assertNotIn('events',report['candidate_comparison']);self.assertNotIn('full_plan',json.dumps(report))

    def test_future_registration_source_lock_and_protocol_drift(self):
        template=json.loads((ROOT/'research/validation-template.json').read_text())
        s=protocol.register(template,'2026-11-06','2026-11-05T23:00:00+00:00',ROOT)
        protocol.verify(s,ROOT)
        altered=copy.deepcopy(s);altered['initial_capital']=1
        with self.assertRaises(ValueError):protocol.verify(altered,ROOT)
        with self.assertRaises(ValueError):protocol.register(template,'2026-11-05','2026-11-05T23:00:00+00:00',ROOT)

    def test_objective_universe_deduplicates_issuers_and_rejects_future_data(self):
        def row(ticker,cap,issuer):
            return dict(ticker=ticker,float_market_cap=cap,issuer_id=issuer,as_of='2026-11-05T20:00:00+00:00',
                price=10.,median_dollar_volume_60=30_000_000,security_type='common_stock',country='US',primary_listing=True)
        records=[row('A',100,'one'),row('AA',90,'one'),row('B',80,'two')]
        out=protocol.objective_universe(records,observed_at='2026-11-05T23:00:00+00:00',source='fixture',limit=2)
        self.assertEqual(out['universe'],['A','B'])
        records[0]['as_of']='2026-11-06T23:00:00+00:00'
        with self.assertRaises(ValueError):protocol.objective_universe(records,observed_at='2026-11-05T23:00:00+00:00',source='fixture',limit=2)

    def test_exchange_calendar_holiday_early_close_and_weekend(self):
        with self.assertRaises(ValueError):market_window('2026-11-26')
        window=market_window('2026-11-27')
        self.assertEqual(e.timestamp(window['market_close_at']).astimezone(e.ET).hour,13)
        self.assertEqual(window['next_session'],'2026-11-30')

    def test_seed_daily_collection_rerun_and_missing_day_end_to_end(self):
        from scripts.run_validation import collect
        s=spec();self.store.activate(s)
        def run(obs,price_days,stamp):
            with mock.patch('validation_capture.capture',return_value=(obs,price_days,
                    {d:{'cash_gap':1.,'cash_day':1.} for d in price_days},{'format':'fixture','frames':{}})):
                return collect(self.store,s,stamp)
        run(snapshot(),{},'2026-11-05T23:15:00+00:00')
        first=run(snapshot('2026-11-06'),{'2026-11-06':bars()},'2026-11-06T23:15:00+00:00')
        self.assertEqual(first['completed_sessions'],1)
        head=self.store.audit('test')['head_hash']
        run(snapshot('2026-11-06'),{'2026-11-06':bars()},'2026-11-06T23:20:00+00:00')
        self.assertEqual(self.store.audit('test')['head_hash'],head)
        result=run(snapshot('2026-11-10'),{'2026-11-06':bars(),'2026-11-09':bars(),'2026-11-10':bars()},'2026-11-10T23:15:00+00:00')
        self.assertEqual(result['completed_sessions'],3)
        report=build_report(self.store,'test')
        self.assertTrue(any(i['kind']=='missing_snapshot' for i in report['incidents']))
        self.assertIsNone(next(r for r in report['rows'] if r['arm']=='full_system')['return_pct'])
        self.assertEqual(len(self.store.read('test','inputs')),3)

    def test_registration_runtime_drift_is_explicitly_blocked(self):
        template=json.loads((ROOT/'research/validation-template.json').read_text())
        s=protocol.register(template,'2026-11-06','2026-11-05T23:00:00+00:00',ROOT)
        with mock.patch('importlib.metadata.version',return_value='changed'):
            with self.assertRaisesRegex(ValueError,'dependency changed'):protocol.verify(s,ROOT,check_runtime=True)

    def test_runtime_lock_only_emits_whitelisted_exact_packages(self):
        s={'runtime_versions':{p:'1.2.3' for p in ('numpy','pandas','yfinance','exchange-calendars')}}
        self.assertEqual(len(protocol.runtime_requirements(s)),4)
        s['runtime_versions']['numpy']='1.2.3 --extra-index-url https://invalid'
        with self.assertRaises(ValueError):protocol.runtime_requirements(s)

    def test_api_image_contains_every_frozen_source_needed_by_intake(self):
        import shlex
        copied=[]
        for line in (ROOT/'Dockerfile.api').read_text().splitlines():
            if line.startswith('COPY '):copied.extend(shlex.split(line)[1:-1])
        for source in protocol.SOURCES:
            self.assertTrue(any(source==p or p.endswith('/') and source.startswith(p) for p in copied),source+' missing from API image')

    def test_complete_manifest_sections_cannot_change_under_same_policy_id(self):
        s=spec();s['complete_policy_manifest']={k:{'version':'one'} for k in ('selection','sizing','risk','execution')}
        plan=snapshot()['full_plan']
        with self.assertRaisesRegex(ValueError,'section changed'):
            record_plan(self.store,s,plan,'2026-11-05T22:05:00+00:00',market_window('2026-11-05'))
        for k in s['complete_policy_manifest']:plan[k]['policy_sha256']=e.digest(s['complete_policy_manifest'][k])
        self.assertEqual(record_plan(self.store,s,plan,'2026-11-05T22:05:00+00:00',market_window('2026-11-05'))['status'],'receipt_recorded')


class ExecutionQuality(unittest.TestCase):
    def statement(self):
        return {'source':'fixture broker export','statement_id':'one','fills':[
            {'fill_id':'a','ticker':'X','side':'buy','quantity':4.,'price':101.,'fee':1.,
             'decision_id':'decision','executed_at':'2026-11-06T14:30:00+00:00'},
            {'fill_id':'b','ticker':'X','side':'buy','quantity':6.,'price':102.,'fee':1.,
             'decision_id':'decision','executed_at':'2026-11-06T14:31:00+00:00'}]}

    def test_partial_fills_aggregate_vwap_and_preserve_adverse_slippage(self):
        statement=validate_statement(self.statement())
        day={'session':'2026-11-06','books':{'10':{'full_system':{'fills':[
            {'ticker':'X','side':'buy','shares':10.,'price':100.,'cost':1.,'decision_id':'decision'}]}}}}
        out=reconcile([statement],[day],10)
        self.assertAlmostEqual(out['rows'][0]['actual_vwap'],101.6)
        self.assertAlmostEqual(out['rows'][0]['adverse_price_slippage_bps'],160.)
        self.assertFalse(out['actual_account_performance'])

    def test_ambiguous_actual_fills_are_unmatched_not_inferred(self):
        statement=self.statement();statement['fills'][0].pop('decision_id')
        self.assertEqual(reconcile([statement],[],10)['unmatched_actual_fills'],2)

    def test_conflicting_fill_ids_cannot_rewrite_actual_prices(self):
        statement=self.statement();changed=copy.deepcopy(statement);changed['fills'][0]['price']=1
        with self.assertRaises(ValueError):validate_fill_identity(changed,[statement])

    def test_duplicate_actual_fill_ids_and_invalid_quantity_rejected(self):
        statement=self.statement();statement['fills'][1]['fill_id']='a'
        with self.assertRaises(ValueError):validate_statement(statement)
        statement=self.statement();statement['fills'][0]['quantity']=-1
        with self.assertRaises(ValueError):validate_statement(statement)


class Statistics(unittest.TestCase):
    def test_bootstrap_preserves_pairing_and_is_reproducible(self):
        r=[.01,-.01,.005,-.002]*20
        interval=bootstrap_interval(r,r,draws=200)
        self.assertEqual(interval['low_pct'],0);self.assertEqual(interval['high_pct'],0)
        self.assertEqual(interval,bootstrap_interval(r,r,draws=200))

    def test_daily_peeking_cannot_produce_formal_verdict(self):
        self.assertEqual(assess(spec(),[{}]*251)['status'],'not_due')
        self.assertEqual(assess(spec(),[{}]*503)['status'],'not_due')

    def test_incomplete_504_session_record_cannot_pass(self):
        days=[{'incidents':[{'arms':['full_system']}]}]*504
        self.assertEqual(assess(spec(),days)['status'],'incomplete')

    def test_candidate_outcomes_start_next_open_and_show_maturity(self):
        obs=snapshot();days=[]
        for session in ('2026-11-06','2026-11-09'):
            days.append({'session':session,'raw_bars':bars()})
        days[0]['raw_bars']['X'].update(Close=105.,dividend=50.)
        days[1]['raw_bars']['X'].update(Open=52.5,Close=55.,split=2.,dividend=1.)
        out=candidate_outcomes([obs],days,horizons=(1,2,5))
        one=next(r for r in out['events'] if r['ticker']=='X' and r['horizon']==1)
        two=next(r for r in out['events'] if r['ticker']=='X' and r['horizon']==2)
        self.assertAlmostEqual(one['return_pct'],5.) # no ex-dividend windfall on purchase day
        self.assertAlmostEqual(two['return_pct'],12.)
        self.assertEqual(next(r for r in out['rows'] if r['horizon']==5)['mature'],0)

    def test_prestart_snapshot_cannot_consume_the_first_eligible_month(self):
        before=snapshot('2026-11-05');eligible=snapshot('2026-11-06')
        days=[{'session':'2026-11-09','raw_bars':bars()}]
        out=candidate_outcomes([before,eligible],days,horizons=(1,))
        self.assertEqual(len(out['events']),2)
        self.assertTrue(all(r['snapshot_id']==eligible['snapshot_id'] for r in out['events']))

    def test_confirmatory_gate_uses_net_growth_and_risk_not_win_rate(self):
        a=b=100000.;days=[]
        for i in range(504):
            ret=.005 if i%2==0 else -.005
            a*=1+ret+.0002;b*=1+ret
            days.append({'incidents':[],'books':{'10':{'full_system':{'equity':a},'SPY':{'equity':b}}}})
        preliminary=assess(spec(),days[:252]);final=assess(spec(),days)
        self.assertEqual(preliminary['status'],'preliminary')
        self.assertEqual(final['status'],'confirmatory')
        self.assertTrue(final['economic_gate_passed'])
        self.assertGreater(final['primary_interval']['low_pct'],0)
        self.assertTrue(final['no_automatic_promotion'])


if __name__=='__main__':unittest.main()
