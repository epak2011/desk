"""Prospective hypothetical ledger. Recorded orders only; no broker or LLM calls.

Raw prices, cash dividends, splits, next-open fills and actual cash balances are
accounted for. Existing entry-event trials are neither modified nor promoted.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import position_policy

ET = ZoneInfo('America/New_York')


def identity(spec):
    return hashlib.sha256(json.dumps({k:v for k,v in spec.items() if k!='protocol_id'},sort_keys=True,separators=(',',':')).encode()).hexdigest()


def verify(spec, root):
    if spec.get('protocol_id') != identity(spec):
        raise ValueError('Protocol identity mismatch; do not rewrite the original registration.')
    for name, expected in spec['source_sha256'].items():
        if hashlib.sha256((Path(root)/name).read_bytes()).hexdigest() != expected:
            raise ValueError('Frozen source changed: '+name)


def wallet():
    return {'cash':1., 'shares':0., 'stop':None, 'pending':None, 'fills':0, 'turnover':0.}


def advance(prior, bar, signal, *, session, cash_gap=1., cash_day=1., cost_bps=10, passive=False, initial=False):
    """One immutable session; an after-open observation cannot fill that open."""
    w=copy.deepcopy(prior)
    cost=cost_bps/10000
    if not math.isfinite(cost) or not 0<=cost<.1:
        raise ValueError('Invalid cost.')
    for key in ('Open','Close'):
        if not isinstance(bar.get(key),(int,float)) or not math.isfinite(bar[key]) or bar[key]<=0:
            raise ValueError('Missing or invalid raw bar.')
    for factor in (cash_gap,cash_day):
        if not math.isfinite(factor) or factor<=0:
            raise ValueError('Invalid cash return.')
    w['cash']*=cash_gap
    split=float(bar.get('Stock Splits') or 0) or 1.
    if not math.isfinite(split) or split<=0:
        raise ValueError('Invalid split.')
    w['shares']*=split
    if w['stop'] is not None:
        w['stop']/=split
    pending=w.get('pending')
    if pending and pending.get('stop') is not None:
        pending['stop']/=split
    dividend=float(bar.get('Dividends') or 0)
    if not math.isfinite(dividend) or dividend<0:
        raise ValueError('Invalid cash dividend.')
    w['cash']+=w['shares']*dividend
    opening=datetime.combine(datetime.fromisoformat(session).date(),time(9,30),ET)
    if passive and initial:
        pending={'kind':'entry','stop':None,'observed_at': '2000-01-01T00:00:00+00:00'}
    if pending:
        observed=datetime.fromisoformat(pending['observed_at'].replace('Z','+00:00'))
        if observed.tzinfo is None:
            raise ValueError('Observation requires timezone.')
        if observed>=opening:
            raise ValueError('Lookahead: order was not recorded before this open.')
        if pending['kind']=='entry' and not w['shares']:
            stop=pending.get('stop')
            if passive or isinstance(stop,(float,int)) and math.isfinite(stop) and 0<stop<bar['Open']:
                qty=w['cash']/(bar['Open']*(1+cost))
                w.update(shares=qty,cash=0.,stop=stop)
                w['turnover']+=qty*bar['Open'];w['fills']+=1
        elif pending['kind']=='exit' and w['shares']:
            w['turnover']+=w['shares']*bar['Open'];w['fills']+=1
            w.update(cash=w['cash']+w['shares']*bar['Open']*(1-cost),shares=0.,stop=None)
    w['pending']=None
    w['cash']*=cash_day
    if not passive and signal:
        observed=datetime.fromisoformat(signal['observed_at'].replace('Z','+00:00'))
        close=datetime.combine(datetime.fromisoformat(session).date(),time(16),ET)
        if observed<close:
            raise ValueError('A closing signal cannot be observed before the close.')
        state={**signal,'price':bar['Close']}
        reason=position_policy.full_exit_reason(state,w['stop']) if w['shares'] else None
        if reason:
            w['pending']={'kind':'exit','observed_at':signal['observed_at'],'reason':reason}
        elif not w['shares'] and str(signal.get('action','')).lower() in {'enter','enter_now','accumulate'}:
            w['pending']={'kind':'entry','stop':signal.get('stop'),'observed_at':signal['observed_at']}
    # Even with no engine observation, a previously frozen price stop is observable.
    elif not passive and w['shares'] and w['stop'] is not None and bar['Close']<=w['stop']:
        w['pending']={'kind':'exit','reason':'frozen_stop','observed_at':datetime.combine(datetime.fromisoformat(session).date(),time(16,1),ET).isoformat()}
    w['equity']=w['cash']+w['shares']*bar['Close']
    w['exposure']=w['shares']*bar['Close']/w['equity'] if w['equity'] else 0.
    return w


def summary(spec, days, *, missing_observations=0):
    if not days:
        return {'status':'scheduled','starts_on':spec['starts_on'],'protocol_id':spec['protocol_id'],
                'universe_count':len(spec['universe']),'completed_sessions':0,'rows':[],
                'basis':'Prospective paper ledger; not actual account performance. No backdated decisions.'}
    rows=[]
    for name in ('strategy','SPY','QQQ','same_universe','cash'):
        curve=[d['equity'][name] for d in days]
        peak=1.;dd=0.
        for v in curve:
            peak=max(peak,v);dd=min(dd,(v/peak-1)*100)
        value=(curve[-1]-1)*100
        rows.append({'name':name,'return_pct':value if not missing_observations or name!='strategy' else None,
                     'max_drawdown_pct':dd if not missing_observations or name!='strategy' else None})
    return {'status':'incomplete' if missing_observations else 'collecting',
            'protocol_id':spec['protocol_id'],'starts_on':spec['starts_on'],'as_of':days[-1]['session'],
            'completed_sessions':len(days),'universe_count':len(spec['universe']),
            'missing_observations':missing_observations,'rows':rows,
            'average_exposure_pct':sum(d['exposure'] for d in days)/len(days)*100 if not missing_observations else None,
            'gross_turnover_pct':days[-1]['turnover']*100 if not missing_observations else None,
            'cost_bps_per_side':spec['cost_bps_per_side'],'cash_basis':'SGOV total-return proxy on idle cash; raw cash dividends retained and earn the same cash proxy.',
            'basis':'Prospective hypothetical equal-capital sleeves. Actual recorded close decisions, next-open fills, raw prices, split/dividend accounting; no calendar exits, automatic targets or discretionary PM fills.',
            'mature_performance_claim':False,'actual_account_performance':False}
