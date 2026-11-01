"""Release-aware diagnostic comparison; not a backtest of the full Opportunity engine.

Usage: python3 compare_macro_rules.py historical_inputs.csv --output comparison.json
Required CSV columns:
date,total_return_index,cash_return,ism,ism_period,ism_available_at,
unemp,unemp_period,unemp_available_at,hy_bps,hy_available_at

date is a month-end decision date. *_available_at must be the actual release date.
total_return_index must include reinvested distributions; cash_return is a monthly
decimal return (0.002 means 0.2%). No numeric defaults or release-date guesses.
The same underlying observation appearing in several rows must retain its value
and release date. Revised-vintage data must be disclosed separately.
"""
import argparse
import csv
import datetime as dt
import json
import math
from pathlib import Path
import statistics

REQUIRED='date total_return_index cash_return ism ism_period ism_available_at unemp unemp_period unemp_available_at hy_bps hy_available_at'.split()

def date(v):
    return dt.date.fromisoformat(v)

def month_number(v):
    d=date(v)
    return d.year*12+d.month

def states(ism, prior_ism, unemp, prev_unemp, unemp2, hy, variant):
    if variant=='current_v12_1':
        t1='WARNING' if ism<50 else 'CLEAR'
        t2='FIRING' if unemp>=4.2 and unemp>prev_unemp else 'WATCH'
    else:
        t1='CLEAR' if ism>=50 else 'FIRING' if prior_ism<50 else 'WARNING'
        delta=round(unemp-prev_unemp,3)
        prev_delta=round(prev_unemp-unemp2,3)
        magnitude=delta>0.10 if variant=='candidate_gt_0_10' else delta>=0.15
        t2='FIRING' if unemp>=4.2 and delta>prev_delta and magnitude else 'WATCH'
    credit='CRISIS' if hy>=600 else 'TAIL_RISK' if hy>=475 else 'EARLY_WARNING' if hy>=450 else 'CLEAR'
    return {'t1':t1,'t2':t2,'credit':credit}

def load_rows(path):
    with open(path,newline='') as f:
        reader=csv.DictReader(f)
        missing=set(REQUIRED)-set(reader.fieldnames or [])
        if missing:
            raise ValueError('Missing columns: '+', '.join(sorted(missing)))
        rows=list(reader)
    if len(rows)<15:
        raise ValueError('At least 15 monthly records are required; synthetic inputs are tests only.')
    for i,r in enumerate(rows):
        date(r['date'])
        for key in ['total_return_index','cash_return','ism','unemp','hy_bps']:
            r[key]=float(r[key])
            if not math.isfinite(r[key]): raise ValueError('Nonfinite '+key)
        if r['total_return_index']<=0 or not 0<r['ism']<=100 or not 0<=r['unemp']<=100 or r['hy_bps']<0:
            raise ValueError('Invalid market value')
        for key in ['ism','unemp']:
            if date(r[key+'_available_at'])<date(r[key+'_period']):
                raise ValueError('Release precedes observation period: '+key)
        date(r['hy_available_at'])
        if i and month_number(r['date'])-month_number(rows[i-1]['date'])!=1:
            raise ValueError('Decision dates must be sorted consecutive months')
    return rows

def available(rows, key, at):
    period_key=key+'_period' if key in ['ism','unemp'] else 'date'
    release_key=key+'_available_at' if key in ['ism','unemp'] else 'hy_available_at'
    observations={}
    for r in rows:
        period=r[period_key]
        # Duplicated records cannot silently overwrite a prior vintage.
        value=(r[key],r[release_key])
        if period in observations and observations[period]!=value:
            raise ValueError('Conflicting observation/vintage for '+key+' '+period)
        observations[period]=value
    result=[(p,v[0]) for p,v in observations.items() if date(v[1])<=at]
    return sorted(result,reverse=True)

def metrics(returns, exposures, turnover, cash):
    equity=1.0
    peak=1.0
    drawdown=0.0
    for r in returns:
        if r<=-1: raise ValueError('Invalid return <= -100%')
        equity*=1+r
        peak=max(peak,equity)
        drawdown=min(drawdown,equity/peak-1)
    n=len(returns)
    excess=[r-c for r,c in zip(returns,cash)]
    vol=statistics.stdev(returns)*math.sqrt(12) if n>1 else 0
    excess_vol=statistics.stdev(excess) if n>1 else 0
    return {'months':n,'total_return':equity-1,'cagr':equity**(12/n)-1,
            'max_drawdown':drawdown,'annualized_volatility':vol,
            'sharpe_vs_cash':statistics.mean(excess)/excess_vol*math.sqrt(12) if excess_vol else None,
            'annualized_one_way_turnover':sum(turnover)*12/n,
            'average_equity_exposure':statistics.mean(exposures),
            'fraction_months_below_full_equity':sum(x<1 for x in exposures)/n}

def compare(rows,cost_bps=10):
    if not math.isfinite(cost_bps) or cost_bps<0: raise ValueError('Invalid trading cost')
    variants=['buy_and_hold','current_v12_1','candidate_gt_0_10','candidate_ge_0_15']
    eligible=[]
    for i in range(len(rows)-1):
        at=date(rows[i]['date'])
        ism=available(rows,'ism',at)
        unemp=available(rows,'unemp',at)
        hy=available(rows,'hy_bps',at)
        if len(ism)<2 or len(unemp)<3 or not hy: continue
        if month_number(ism[0][0])-month_number(ism[1][0])!=1: continue
        if any(month_number(unemp[j][0])-month_number(unemp[j+1][0])!=1 for j in [0,1]): continue
        if month_number(rows[i]['date'])-month_number(ism[0][0])>2: continue
        if month_number(rows[i]['date'])-month_number(unemp[0][0])>2: continue
        if month_number(rows[i]['date'])-month_number(hy[0][0])>1: continue
        eligible.append((i,ism,unemp,hy))
    if len(eligible)<12: raise ValueError('Fewer than 12 comparable months after release-aware warmup')
    if any(b[0]!=a[0]+1 for a,b in zip(eligible,eligible[1:])):
        raise ValueError('Missing or stale observations create gaps; refusing to compound across gaps')
    result={}
    events=[]
    for reduced in [0.3,0.5,0.7]:
        for variant in variants:
            if variant=='buy_and_hold' and reduced!=0.5: continue
            returns=[]; exposure=[]; turnover=[]; cash=[]; previous=0.0
            for i,ism,u,hy in eligible:
                status=states(ism[0][1],ism[1][1],u[0][1],u[1][1],u[2][1],hy[0][1],variant)
                # Diagnostic sensitivity only. Credit levels are logged, not mapped to automatic exits.
                weight=1.0 if variant=='buy_and_hold' else reduced if status['t1']=='WARNING' or status['t2']=='FIRING' else 1.0
                market=rows[i+1]['total_return_index']/rows[i]['total_return_index']-1
                cash_return=rows[i+1]['cash_return']
                traded=abs(weight-previous)
                returns.append(weight*market+(1-weight)*cash_return-traded*cost_bps/10000)
                exposure.append(weight);turnover.append(traded);cash.append(cash_return)
                previous=weight
                if reduced==0.5:
                    events.append({'decision_date':rows[i]['date'],'variant':variant,**status,'equity_weight':weight})
            name=variant if variant=='buy_and_hold' else variant+'_risk_weight_'+str(reduced)
            result[name]=metrics(returns,exposure,turnover,cash)
    return {'status':'diagnostic_only','period_start':rows[eligible[0][0]]['date'],
            'period_end':rows[eligible[-1][0]+1]['date'],'one_way_cost_bps':cost_bps,
            'limitations':['No threshold optimization performed.','Not a replay of the full daily Opportunity engine.',
                          '30/50/70% risk weights are sensitivity scenarios, not approved allocations.',
                          'Actual publication dates required; latest-revised data still carries revision bias.',
                          'Monthly samples and overlapping economic episodes are not independent cycles.'],
            'metrics':result,'monthly_state_audit':events}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv',type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--cost-bps',type=float,default=10)
    args=parser.parse_args()
    try:
        report=compare(load_rows(args.csv),args.cost_bps)
    except (ValueError,OSError) as e:
        parser.exit(2,'Comparison not run: '+str(e)+'\n')
    args.output.write_text(json.dumps(report,indent=2))
    print('Wrote diagnostic comparison:',args.output)
