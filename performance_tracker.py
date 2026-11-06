"""Read-only tracking views: prospective evidence and exploratory backtests stay separate."""
from __future__ import annotations
import copy
import csv
import io
import json
from datetime import datetime,timedelta,timezone

import validation_engine as engine


def calendar_rows(curves,initial,frequency='month',allowed=None):
    if not curves:return []
    arms=sorted(set(curves[0])-{'session'})
    if allowed is not None:arms=[a for a in arms if a in allowed]
    rows=[];groups={}
    width=7 if frequency=='month' else 4
    for index,day in enumerate(curves):groups.setdefault(day['session'][:width],[]).append(index)
    for period,indexes in groups.items():
        for arm in arms:
            opening=curves[indexes[0]-1][arm] if indexes[0] else initial
            values=[curves[i][arm] for i in indexes]
            metrics=engine.metrics(values,opening)
            rows.append({'period':period,'arm':arm,'sessions':len(values),
                'return_pct':metrics['return_pct'],'max_drawdown_pct':metrics['max_drawdown_pct'],
                'period_start':curves[indexes[0]]['session'],'period_end':curves[indexes[-1]]['session']})
    return rows


def summarize_backtest(report):
    if report.get('evidence_type')!='retrospective_validation_engine_simulation' or report.get('new_forward_results'):
        raise ValueError('Only an explicitly retrospective validation-engine replay belongs here')
    result={k:copy.deepcopy(report[k]) for k in ('study_id','generated_at','evidence_type','universe','universe_basis',
        'source_sha256','assumptions','limitations','actual_account_performance','new_forward_results')}
    result['input_sha256']={s:v['sha256'] for s,v in report['input_manifest']['files'].items()}
    result['periods']={}
    for name,period in report['periods'].items():
        summary=copy.deepcopy(period['summary']);curves={}
        for cost in report['assumptions']['cost_scenarios_bps']:
            curves[str(cost)]=[{'session':d['session'],**{a:p['equity'] for a,p in d['books'][str(cost)].items() if a not in engine.EXTERNAL}} for d in period['curve']]
        result['periods'][name]={**summary,'equity_curves':curves,
            'annual_rows':{cost:calendar_rows(values,summary['initial_capital'],'year') for cost,values in curves.items()},
            'monthly_rows':{cost:calendar_rows(values,summary['initial_capital']) for cost,values in curves.items()}}
    result['summary_sha256']=engine.digest({k:v for k,v in result.items() if k!='generated_at'})
    return result


def publish_backtest(report,backend):
    summary=summarize_backtest(report);backend.ensure_backend_schema()
    key='validation_suite_replay:'+report['study_id']
    with backend.db_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute('SELECT payload FROM engine_review_status WHERE key=%s',(key,));old=cur.fetchone()
                if old:
                    if old[0].get('summary_sha256')!=summary['summary_sha256']:raise ValueError('Immutable replay identity conflict')
                    summary=old[0]
                else:
                    cur.execute('INSERT INTO engine_review_status(key,payload,updated_at) VALUES (%s,%s::jsonb,NOW())',(key,backend.json_dumps(summary)))
                cur.execute("INSERT INTO engine_review_status(key,payload,updated_at) VALUES ('validation_suite_replay',%s::jsonb,NOW()) ON CONFLICT(key) DO UPDATE SET payload=EXCLUDED.payload,updated_at=NOW()",(backend.json_dumps(summary),))


def freshness(live,now):
    if not live or live.get('status')=='not_registered':return {'status':'not_registered'}
    dt=engine.timestamp(now);today=dt.astimezone(engine.ET).date()
    start=live.get('starts_on') or live.get('registration',{}).get('starts_on')
    if start and str(today)<start:return {'status':'future_start','starts_on':start,'as_of':live.get('as_of')}
    import exchange_calendars as xcals
    cal=xcals.get_calendar('XNYS')
    expected=[s for s in cal.sessions_in_range(str(today-timedelta(days=12)),str(today)) if cal.session_close(s).to_pydatetime()+timedelta(hours=2)<=dt]
    last=str(expected[-1].date()) if expected else None
    if not last or start and last<start:return {'status':'waiting_first_settled_close','starts_on':start}
    actual=live.get('as_of')
    return {'status':'up_to_date' if actual and actual>=last else 'awaiting_complete_collection',
        'expected_through':last,'as_of':actual,'source_status':live.get('status')}


def compose(live=None,historical=None,*,now=None):
    now=now or datetime.now(timezone.utc).isoformat()
    live=copy.deepcopy(live or {'status':'not_registered','rows':[],'completed_sessions':0})
    if historical and (historical.get('evidence_type')!='retrospective_validation_engine_simulation' or historical.get('new_forward_results')):
        raise ValueError('Historical and prospective evidence cannot be interchanged')
    count=live.get('completed_sessions',0)
    available={r['arm'] for r in live.get('rows',[]) if r.get('status')!='incomplete' and r.get('return_pct') is not None}
    live['monthly_scorecard']=calendar_rows(live.get('equity_curves',[]),live.get('initial_capital',100000),allowed=available)
    return {'generated_at':now,'version':'performance-tracker-v1',
        'forward':live,'historical':copy.deepcopy(historical),'freshness':freshness(live,now),
        'milestones':{'completed_sessions':count,'preliminary_at':252,'confirmatory_at':504,
            'sessions_to_preliminary':max(0,252-count),'sessions_to_confirmatory':max(0,504-count)},
        'actual_account_performance':False,
        'comparison_policy':'Forward and historical periods differ. No return spread is calculated between them; benchmarks are matched within each period.',
        'links':{'report':'/v1/operator/performance/tracker','csv':'/v1/operator/performance/tracker.csv'}}


def read(backend,*,now=None):
    with backend.db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT key,payload FROM engine_review_status WHERE key IN ('validation_performance','validation_suite_replay')")
            saved=dict(cur.fetchall())
    return compose(saved.get('validation_performance'),saved.get('validation_suite_replay'),now=now)


def csv_export(tracker):
    output=io.StringIO();fields=['evidence','study_or_protocol','period','start','end','arm','cost_bps','status',
        'return_pct','excess_vs_spy_pp','excess_vs_qqq_pp','max_drawdown_pct','annualized_volatility_pct','costs_dollars']
    writer=csv.DictWriter(output,fieldnames=fields,extrasaction='ignore');writer.writeheader()
    live=tracker['forward']
    for r in live.get('rows',[]):writer.writerow({'evidence':'prospective_paper','study_or_protocol':live.get('protocol_id'),
        'period':'since_inception','start':live.get('starts_on'),'end':live.get('as_of'),**r})
    historical=tracker.get('historical') or {}
    for name,period in historical.get('periods',{}).items():
        for r in period['rows']:writer.writerow({'evidence':'retrospective_simulation','study_or_protocol':historical['study_id'],
            'period':name,'start':period['start'],'end':period['end'],**r})
    return output.getvalue()
