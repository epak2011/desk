"""Retrospective adapter for the unchanged forward portfolio engine.

Hypothetical historical timestamps are simulation mechanics, never live evidence.
Yahoo's split-normalized OHLCV is restored to contemporaneous share units before
explicit split/dividend accounting. Exact provider files are retained separately.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import pickle
from concurrent.futures import ProcessPoolExecutor,as_completed
from datetime import timedelta
from pathlib import Path

import pandas as pd
import validation_engine as engine
from validation_capture import market_window

SOURCES=('validation_backtest.py','validation_engine.py','tactical.py','position_policy.py','research_fast_levels.py')
LIMITATIONS=[
    'Retrospective exploratory simulation on a present-day survivor basket, not untouched validation or actual account performance.',
    'Complete system and system-selected passive arms cannot be reconstructed without dated complete-policy receipts. Their returns are unavailable, not zero.',
    'Technical decisions use only each historical price prefix, but downloaded histories may contain provider revisions. Point-in-time security-master and fundamental/macro vintages are not reconstructed.',
    'Yahoo OHLCV is split-normalized; nominal share units are reconstructed using returned split events before accounting for shares/dividends. These are reconstructed prices, not an original exchange tape.',
    'Dividends become cash on the ex date, not actual pay date; personal taxes, settlement and market impact beyond the stated participation limit are not modeled.',
    'Complex spin-offs and reorganizations follow provider adjustment events; separate distributed securities and actual corporate-action settlement are not reconstructed.',
    'No leverage, discretionary targets, intraday stop fills or automatic strategy promotion. Stops are close-confirmed and execute at the next open.',
    'Full-period calendar returns carry capital. The separately labeled 2026 slice resets capital and seeds using the preceding completed close.',
]


def source_hashes(root):
    return {s:hashlib.sha256((Path(root)/s).read_bytes()).hexdigest() for s in SOURCES}


def prepare_frame(provider):
    from backtest_runner import validate_bars
    p=provider.copy().sort_index()
    required=['Open','High','Low','Close','Adj Close','Volume','Dividends','Stock Splits']
    if not set(required)<=set(p):raise ValueError('Provider OHLCV, adjusted close and actions required')
    p[required]=p[required].apply(pd.to_numeric,errors='raise')
    if not p[required].map(math.isfinite).all().all():raise ValueError('Missing or non-finite provider input')
    if (p['Adj Close']<=0).any() or (p.Dividends<0).any() or (p['Stock Splits']<0).any():raise ValueError('Invalid adjustment or action')
    if 'Capital Gains' in p and p['Capital Gains'].fillna(0).ne(0).any():raise ValueError('Capital-gain distribution needs an explicitly reviewed accounting extension')
    p=validate_bars(p)
    split=p['Stock Splits'].replace(0.,1.)
    future=split.iloc[::-1].cumprod().iloc[::-1]/split
    nominal=p.copy();adjusted=p.copy()
    ratio=p['Adj Close']/p.Close
    for col in ('Open','High','Low','Close'):
        nominal[col]=p[col]*future
        adjusted[col]=p[col]*ratio
    nominal['Dividends']=p.Dividends*future
    nominal['Volume']=p.Volume/future
    nominal['future_split_factor']=future
    nominal['prior_liquidity']=(nominal.Close*nominal.Volume).rolling(60,min_periods=60).median().shift(1)
    return nominal,adjusted


def load_provider(path):
    return pd.read_csv(path,parse_dates=['Date']).set_index('Date')


def historical_prefix(nominal,adjusted,day):
    """Only known-through-day bars, expressed in that day's nominal price/share units."""
    prefix=adjusted.loc[:day].copy()
    factor=float(nominal.loc[day,'Close']/prefix.Close.iloc[-1])
    for col in ('Open','High','Low','Close'):prefix[col]*=factor
    prefix['Volume']/=float(nominal.loc[day,'future_split_factor'])
    return prefix


def build_signals(task):
    ticker,folder,date_strings,cache,hashes=task
    folder=Path(folder)
    identity=engine.digest({'ticker':ticker,'files':{s:hashlib.sha256((folder/'provider'/(s+'.csv')).read_bytes()).hexdigest() for s in (ticker,'SPY')},
        'dates':date_strings,'sources':hashes})
    target=Path(cache)/(identity+'.pickle')
    if target.exists():
        # This cache is generated locally by this script, never accepted as a
        # remote/untrusted artifact. CSV/hash archives remain authoritative.
        with target.open('rb') as f:return ticker,pickle.load(f)
    import tactical,research_fast_levels
    nominal,adjusted=prepare_frame(load_provider(folder/'provider'/(ticker+'.csv')))
    bn,ba=prepare_frame(load_provider(folder/'provider/SPY.csv'))
    original=tactical.detect_key_levels
    states={}
    try:
        tactical.detect_key_levels=research_fast_levels.detect_key_levels
        for stamp in date_strings:
            day=pd.Timestamp(stamp)
            state=tactical.compute(historical_prefix(nominal,adjusted,day),historical_prefix(bn,ba,day))
            state=tactical.apply_extension_execution_overlay(state) if state else None
            if not state or not state.get('action'):raise ValueError('Unusable historical state: '+ticker+'/'+stamp)
            states[stamp]={k:state.get(k) for k in ('action','setup_score','price','ma50','ma200','stop')}
    finally:tactical.detect_key_levels=original
    target.parent.mkdir(parents=True,exist_ok=True)
    tmp=target.with_suffix('.tmp')
    with tmp.open('wb') as f:pickle.dump(states,f,protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(target)
    return ticker,states


def precompute(folder,names,dates,cache,root,workers=4):
    hashes=source_hashes(root)
    tasks=[(s,str(folder),list(dates),str(cache),hashes) for s in names]
    results={}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(build_signals,t) for t in tasks]):
            name,states=future.result();results[name]=states
            print(f'Historical signals ready {len(results)}/{len(names)} ({name})',flush=True)
    return results


def snapshot_for(day,states,nominal,spec):
    window=market_window(day)
    candidates={}
    for symbol in spec['universe']:
        state=states[symbol][day]
        row={'action':state['action'],'price':float(nominal[symbol].loc[day,'Close']),
             'score':float(state['setup_score']),'reason':'Retrospective frozen technical rule',
             'selected':str(state['action']).lower() in {'enter','enter_now','accumulate'},
             'sector':spec.get('sectors',{}).get(symbol)}
        for k in ('ma50','ma200','stop'):
            v=state.get(k);row[k]=float(v) if v is not None and math.isfinite(float(v)) else None
        candidates[symbol]=row
    snapshot={**window,'received_at':(engine.timestamp(window['market_close_at'])+timedelta(hours=2,minutes=15)).isoformat(),
        'candidates':candidates,'source':'Retrospective calculation; hypothetical timestamps, not recorded observations'}
    snapshot['snapshot_id']=engine.digest(snapshot)
    engine.validate_snapshot(snapshot,spec)
    return snapshot


def compact_day(state):
    return {'session':state['session'],'books':{cost:{arm:{k:p[k] for k in ('equity','exposure','costs','turnover','fills_count')}|
        {'open_positions':len(p['shares']),'unfilled_orders_today':len(p['unfilled'])} for arm,p in books.items()} for cost,books in state['books'].items()}}


def period_summary(spec,curve):
    rows=[];costs=spec['cost_scenarios_bps'];initial=spec['initial_capital']
    for cost in costs:
        spy=engine.metrics([d['books'][str(cost)]['SPY']['equity'] for d in curve],initial)
        qqq=engine.metrics([d['books'][str(cost)]['QQQ']['equity'] for d in curve],initial)
        for arm in engine.ARMS:
            p=curve[-1]['books'][str(cost)][arm]
            m=engine.metrics([d['books'][str(cost)][arm]['equity'] for d in curve],initial)
            available=arm not in engine.EXTERNAL
            rows.append({'arm':arm,'label':engine.LABELS[arm],'cost_bps':cost,
                'status':'retrospective_simulation' if available else 'unavailable_missing_historical_policy',
                **{k:v if available else None for k,v in m.items()},
                'excess_vs_spy_pp':m['return_pct']-spy['return_pct'] if available else None,
                'excess_vs_qqq_pp':m['return_pct']-qqq['return_pct'] if available else None,
                'average_exposure_pct':sum(d['books'][str(cost)][arm]['exposure'] for d in curve)/len(curve)*100 if available else None,
                'costs_dollars':p['costs'] if available else None,'turnover_pct':p['turnover']/initial*100 if available else None,
                'fills_count':p['fills_count'] if available else None,'open_positions':p['open_positions'] if available else None})
    return {'start':curve[0]['session'],'end':curve[-1]['session'],'sessions':len(curve),'initial_capital':initial,'rows':rows}


def run_period(template,nominal,states,dates):
    spec=copy.deepcopy(template);spec.update(starts_on=dates[0],complete_policy_id=None)
    calendar=nominal['SPY'].index
    first_index=calendar.get_loc(pd.Timestamp(dates[0]));seed=str(calendar[first_index-1].date())
    spec['registered_at']=(engine.timestamp(market_window(seed)['market_close_at'])+timedelta(minutes=1)).isoformat()
    spec['protocol_id']='retrospective-'+engine.digest({'universe':spec['universe'],'start':dates[0],'end':dates[-1]})
    prior=engine.initial_state(spec,snapshot_for(seed,states,nominal,spec))
    curve=[];fills=[];snapshots=[]
    for i,stamp in enumerate(dates):
        bars={}
        for symbol in set(spec['universe'])|{'SPY','QQQ'}:
            r=nominal[symbol].loc[stamp]
            bars[symbol]={'Open':float(r.Open),'Close':float(r.Close),'split':float(r['Stock Splits']) or 1.,
                'dividend':float(r.Dividends),'prior_median_dollar_volume':float(r.prior_liquidity)}
        cash=nominal['SGOV'];j=cash.index.get_loc(pd.Timestamp(stamp));r=cash.loc[stamp]
        gap=(float(r.Open)+float(r.Dividends))*(float(r['Stock Splits']) or 1.)/float(cash.Close.iloc[j-1]) if i else 1.
        snap=snapshot_for(stamp,states,nominal,spec)
        prior=engine.process_day(prior,spec,stamp,bars,snap,cash_gap=gap,cash_day=float(r.Close/r.Open))
        if any(set(issue['arms'])-engine.EXTERNAL for issue in prior['incidents']):
            raise ValueError('Unexpected technical/benchmark coverage incident; no clean replay headline')
        # External-policy absence is already permanent. Retain its first incident
        # rather than quadratic copies of the same unavailable daily arms.
        seen=set();incidents=[]
        for issue in prior['incidents']:
            key=(issue['kind'],tuple(issue['arms']))
            if key not in seen:seen.add(key);incidents.append(issue)
        prior['incidents']=incidents
        curve.append(compact_day(prior))
        for cost,books in prior['books'].items():
            for arm,p in books.items():
                fills.extend({'session':stamp,'cost_bps':int(cost),'arm':arm,**f} for f in p['fills'])
        if i%63==0:print('Portfolio accounting '+stamp,flush=True)
    return {'summary':period_summary(spec,curve),'curve':curve,'fills':fills}


def validate_dataset(folder,spec,start,end):
    manifest=json.loads((Path(folder)/'manifest.json').read_text())
    names=sorted(set(spec['universe'])|{'SPY','QQQ','SGOV'})
    if manifest.get('failed') or set(manifest['files'])!=set(names):raise ValueError('Incomplete requested cohort')
    nominal={};adjusted={}
    for symbol in names:
        path=Path(folder)/'provider'/(symbol+'.csv')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=manifest['files'][symbol]['sha256']:raise ValueError('Input hash mismatch: '+symbol)
        nominal[symbol],adjusted[symbol]=prepare_frame(load_provider(path))
    sessions=nominal['SPY'].loc[start:end].index
    if sessions.empty or str(sessions[0].date())!=start or str(sessions[-1].date())!=end:raise ValueError('Requested sample endpoints unavailable')
    previous=nominal['SPY'].index[nominal['SPY'].index.get_loc(sessions[0])-1]
    for symbol,f in nominal.items():
        if not sessions.union(pd.DatetimeIndex([previous])).isin(f.index).all():raise ValueError('Missing session: '+symbol)
        if symbol!='SGOV' and len(f.loc[:previous])<200:raise ValueError('Insufficient warmup: '+symbol)
        if f.loc[sessions,'prior_liquidity'].isna().any() and symbol!='SGOV':raise ValueError('Missing ex-ante liquidity: '+symbol)
    return nominal,manifest,[str(d.date()) for d in sessions],str(previous.date())
