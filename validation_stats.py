"""Predeclared assessments. Daily readings never select a statistical stopping date."""
import math
import random
import statistics


def bootstrap_interval(strategy, benchmark, *, block=20, draws=10000, seed=728081):
    if len(strategy) != len(benchmark) or len(strategy) < block or draws < 100:
        raise ValueError('Insufficient paired observations')
    log_excess = [math.log1p(s)-math.log1p(b) for s,b in zip(strategy, benchmark)]
    n = len(log_excess); rng = random.Random(seed)
    # Moving blocks sample only observed contiguous sequences, never stitch a
    # fictitious end-to-start market block. Pairing is retained within each day.
    full = [sum(log_excess[i:i+block]) for i in range(n-block+1)]
    count, remainder = divmod(n, block)
    partial = [sum(log_excess[i:i+remainder]) for i in range(n-remainder+1)] if remainder else [0.]
    values = sorted(math.expm1((sum(rng.choice(full) for _ in range(count)) + rng.choice(partial))*252/n)*100 for _ in range(draws))
    return {'low_pct': values[int(.025*(draws-1))], 'high_pct': values[int(.975*(draws-1))],
            'block_sessions': block, 'draws': draws, 'seed': seed,
            'estimand': 'annualized compounded relative wealth growth', 'model_dependent': True}


def daily_returns(values, initial):
    return [v/(values[i-1] if i else initial)-1 for i,v in enumerate(values)]


def market_diagnostics(strategy,benchmark,initial):
    """Descriptive market exposure; not a multiple-factor claim of manager skill."""
    a=daily_returns(strategy,initial);b=daily_returns(benchmark,initial)
    if len(a)<20:return {'status':'insufficient_history','minimum_sessions':20}
    vb=statistics.variance(b)
    beta=statistics.covariance(a,b)/vb if vb>0 else None
    alpha=(statistics.mean(a)-beta*statistics.mean(b))*252*100 if beta is not None else None
    return {'status':'descriptive','beta_vs_spy':beta,'annualized_daily_intercept_pct':alpha,
        'tracking_error_pct':statistics.stdev(x-y for x,y in zip(a,b))*math.sqrt(252)*100,
        'basis':'Market-only daily-return regression; intercept is not proof of skill or sector/style-adjusted alpha.'}


def assess(spec, days):
    from validation_engine import metrics
    n = len(days)
    if n not in (252,504):
        return {'status': 'not_due', 'sessions': n, 'primary_confirmatory_sessions': 504}
    cost = str(spec['primary_cost_bps']); initial = spec['initial_capital']
    if any('full_system' in i['arms'] for i in days[-1]['incidents']):
        return {'status': 'incomplete', 'sessions': n, 'verdict': 'No demonstrated edge: incomplete record'}
    a = [d['books'][cost]['full_system']['equity'] for d in days]
    b = [d['books'][cost]['SPY']['equity'] for d in days]
    am, bm = metrics(a,initial), metrics(b,initial)
    excess = am['annualized_return_pct']-bm['annualized_return_pct']
    thresholds = spec['success_gates']
    risk = (am['annualized_volatility_pct'] <= bm['annualized_volatility_pct']*thresholds['risk_ratio']
            and abs(am['max_drawdown_pct']) <= abs(bm['max_drawdown_pct'])*thresholds['risk_ratio'])
    economic = excess >= thresholds['annualized_excess_pp'] and risk
    intervals = [bootstrap_interval(daily_returns(a,initial), daily_returns(b,initial), block=w,
        draws=spec['bootstrap']['draws'], seed=spec['bootstrap']['seed']) for w in (20,10,40)]
    if n == 252:
        verdict = 'Promising; preliminary only' if economic else 'No demonstrated edge'
    else:
        verdict = 'Evidence of an edge for this version and period' if economic and intervals[0]['low_pct']>0 else 'No demonstrated edge'
    return {'status': 'preliminary' if n==252 else 'confirmatory', 'sessions': n,
        'verdict': verdict, 'annualized_excess_pp': excess, 'economic_gate_passed': economic,
        'risk_gate_passed': risk, 'primary_interval': intervals[0], 'sensitivity_intervals': intervals[1:],
        'no_automatic_promotion': True}


def candidate_outcomes(snapshots, days, *, horizons=(5,14,30)):
    """Fixed monthly cohorts reduce duplicate signals; no price-dependent selection.

    Outcomes start at the first subsequent open and include dividends and splits.
    They are descriptive overlapping observations, never independent trade samples.
    """
    indexes = {d['session']: i for i,d in enumerate(days)}
    grouped = {}; seen_months = set(); events = []
    for snapshot in sorted(snapshots, key=lambda s:s['session']):
        month = snapshot['next_session'][:7]
        if month in seen_months or not snapshot.get('full_plan'):
            continue
        seen_months.add(month)
        start = indexes.get(snapshot['next_session'])
        if start is None: continue
        records = snapshot['full_plan']['candidates']
        for horizon in horizons:
            end = start+horizon-1
            for record in records:
                key = ('accepted' if record['selected'] else 'rejected', horizon)
                bucket = grouped.setdefault(key, {'eligible':0,'returns':[],'spy_excess':[],'sector_excess':[]})
                bucket['eligible'] += 1
                if end >= len(days): continue
                returns = {}
                # Sector peers are the contemporaneous candidate set, not a
                # future sector classification fetched at scoring time.
                peers = [r['ticker'] for r in records if r.get('sector') and r.get('sector')==record.get('sector')]
                for symbol in set(peers+[record['ticker'],'SPY']):
                    shares=1.;cash=0.;opening=days[start]['raw_bars'][symbol]['Open']
                    for j in range(start,end+1):
                        bar=days[j]['raw_bars'][symbol]
                        if j>start: shares*=bar.get('split',1.)
                        # Purchases at this open do not earn today's ex dividend.
                        if j>start: cash+=shares*bar.get('dividend',0.)
                    returns[symbol]=(shares*days[end]['raw_bars'][symbol]['Close']+cash)/opening-1
                value=returns[record['ticker']]*100
                bucket['returns'].append(value);bucket['spy_excess'].append(value-returns['SPY']*100)
                if peers:bucket['sector_excess'].append(value-statistics.mean(returns[s]*100 for s in peers))
                events.append({'snapshot_id':snapshot['snapshot_id'],'ticker':record['ticker'],
                    'selected':record['selected'],'reason':record['reason'],'horizon':horizon,
                    'return_pct':value,'entry_session':days[start]['session'],'end_session':days[end]['session']})
    rows = [{'group':g,'horizon':h,'eligible':v['eligible'],'mature':len(v['returns']),
        'mean_gross_return_pct':statistics.mean(v['returns']) if v['returns'] else None,
        'mean_excess_vs_spy_pp':statistics.mean(v['spy_excess']) if v['spy_excess'] else None,
        'mean_sector_peer_excess_pp':statistics.mean(v['sector_excess']) if v['sector_excess'] else None,
        'sector_matched_observations':len(v['sector_excess'])} for (g,h),v in sorted(grouped.items())]
    return {'rows':rows,'events':events,'basis':'First available complete-policy snapshot per next-session month; gross stock outcomes, not portfolio alpha. Overlapping outcomes are dependent. Sector peer controls do not match size or beta.'}
