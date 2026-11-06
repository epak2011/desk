"""Versioned prospective comparison. Pure accounting; no broker, network or LLM calls."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
ARMS = ('full_system', 'selected_passive', 'technical_timing', 'broad_passive',
        'SPY', 'QQQ', 'simple_trend', 'no_entry_confirmation', 'no_structural_exit')
EXTERNAL = {'full_system', 'selected_passive'}
LABELS = dict(zip(ARMS, ('Complete system', 'System selection, monthly holding',
    'Technical timing', 'Broad passive, monthly rebalance', 'SPY buy and hold',
    'QQQ buy and hold', 'Simple 200-day trend', 'Shadow: MA-only entry gates',
    'Shadow: no structural exit')))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def number(value, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < minimum:
        raise ValueError('Invalid finite numeric value')
    return float(value)


def timestamp(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Timezone required')
    return dt


def validate_plan(plan, universe, policy_id):
    """External complete policy receipt. Server arrival time is authoritative for execution."""
    if not isinstance(plan,dict) or plan.get('policy_id') != policy_id or not policy_id:
        raise ValueError('Complete policy identity required')
    if not all(isinstance(plan.get(k), dict) and plan[k] for k in ('selection', 'sizing', 'risk', 'execution')):
        raise ValueError('Selection, sizing, risk and execution provenance required')
    records = plan.get('candidates', [])
    if not isinstance(records,list) or not all(isinstance(r,dict) for r in records):
        raise ValueError('Candidate records must be a list of objects')
    if len(records) != len(universe) or {r.get('ticker') for r in records} != set(universe):
        raise ValueError('Every eligible candidate must be recorded exactly once')
    for r in records:
        number(r.get('score'), minimum=-1e12)
        if type(r.get('selected')) is not bool or not str(r.get('reason', '')).strip():
            raise ValueError('Candidate selection and rejection reason required')
    weights = plan.get('target_weights')
    if not isinstance(weights, dict) or set(weights) - set(universe):
        raise ValueError('Invalid target universe')
    if sum(number(v) for v in weights.values()) > 1 + 1e-10:
        raise ValueError('Leverage prohibited')
    if not isinstance(plan.get('stops',{}),dict):raise ValueError('Stops must be an object')
    for ticker, stop in plan.get('stops', {}).items():
        if ticker not in universe:
            raise ValueError('Invalid stop universe')
        number(stop, minimum=1e-12)
    if not isinstance(plan.get('observed_at'),str) or not isinstance(plan.get('session'),str):
        raise ValueError('Observation timestamp and session required')
    timestamp(plan['observed_at'])
    return copy.deepcopy(plan)


def new_portfolio(capital):
    return {'cash': number(capital, minimum=1), 'shares': {}, 'stops': {}, 'pending': None,
            'turnover': 0., 'costs': 0., 'fills_count': 0, 'issues': [], 'last_selection_month': None}


def mark(portfolio, bars, field='Close'):
    return portfolio['cash'] + sum(q * bars[s][field] for s, q in portfolio['shares'].items())


def advance_portfolio(prior, bars, session, *, cost_bps=10, cash_gap=1., cash_day=1., participation=.01):
    """Target-weight orders execute at the next open; participation/cash caps, sells first.

    Stops are close-confirmed and execute next open, never at a fictitious intraday stop.
    Partial fills expire and are visible; a later recorded target is required to retry.
    """
    p = copy.deepcopy(prior)
    cost = number(cost_bps) / 10000
    if cost >= .1 or not 0 < participation <= 1:
        raise ValueError('Invalid execution assumptions')
    for s in p['shares']:
        if s not in bars:
            raise ValueError('Held security missing: ' + s)
    for s, bar in bars.items():
        for field in ('Open', 'Close'):
            number(bar.get(field), minimum=1e-12)
        split = number(bar.get('split', 1), minimum=1e-12)
        dividend = number(bar.get('dividend', 0))
        if s in p['shares']:
            p['shares'][s] *= split
            p['cash'] += p['shares'][s] * dividend
        if s in p['stops']:
            p['stops'][s] /= split
    p['cash'] *= number(cash_gap, minimum=1e-12)
    opening = datetime.fromisoformat(session + 'T09:30:00').replace(tzinfo=ET)
    fills, unfilled = [], []
    order = p.pop('pending', None)
    p['pending'] = None
    if order:
        if timestamp(order['recorded_at']) >= opening:
            p['issues'].append({'session': session, 'kind': 'late_order_cancelled'})
        else:
            weights = order['weights']
            if set(weights) - set(bars) or sum(number(w) for w in weights.values()) > 1 + 1e-10:
                raise ValueError('Invalid weights or price coverage')
            equity = mark(p, bars, 'Open')
            symbols = sorted(set(weights) | set(p['shares']))
            desired = {s: equity * weights.get(s, 0) / bars[s]['Open'] - p['shares'].get(s, 0) for s in symbols}
            for buying in (False, True):
                for s in symbols:
                    change = desired[s]
                    if (change > 0) != buying or abs(change) < 1e-10:
                        continue
                    bar = bars[s]
                    # Only trailing liquidity known before this open is admissible.
                    capacity = number(bar.get('prior_median_dollar_volume', 0)) * participation / bar['Open']
                    qty = min(abs(change), capacity)
                    if buying:
                        qty = min(qty, max(0, p['cash']) / (bar['Open'] * (1 + cost)))
                    else:
                        qty = min(qty, p['shares'].get(s, 0))
                    if qty > 1e-10:
                        notional = qty * bar['Open']
                        fee = notional * cost
                        p['cash'] += -notional-fee if buying else notional-fee
                        p['shares'][s] = p['shares'].get(s, 0) + (qty if buying else -qty)
                        if p['shares'][s] < 1e-10:
                            p['shares'].pop(s, None); p['stops'].pop(s, None)
                        if buying and s in order.get('stops', {}):
                            # A stop above the opening price cannot create an impossible fill.
                            stop = order['stops'][s] / number(bar.get('split', 1), minimum=1e-12)
                            if 0 < stop < bar['Open']:
                                p['stops'][s] = stop
                        p['costs'] += fee; p['turnover'] += notional; p['fills_count'] += 1
                        fills.append({'ticker': s, 'side': 'buy' if buying else 'sell', 'shares': qty,
                                      'price': bar['Open'], 'cost': fee, 'decision_id': order.get('decision_id')})
                    if abs(change) - qty > 1e-8:
                        unfilled.append({'ticker': s, 'side': 'buy' if buying else 'sell',
                                         'shares': abs(change)-qty, 'reason': 'cash_or_participation_limit'})
    p['cash'] *= number(cash_day, minimum=1e-12)
    p['equity'] = mark(p, bars)
    if p['cash'] < -1e-7 or p['equity'] <= 0:
        raise ValueError('Portfolio accounting invariant failed')
    p['cash'] = max(0., p['cash'])
    p['exposure'] = (p['equity']-p['cash'])/p['equity']
    p['fills'], p['unfilled'] = fills, unfilled
    return p


def technical_weights(p, candidates, arm):
    universe = sorted(candidates)
    weights, stops = {}, {}
    for s in universe:
        r = candidates[s]
        price, ma200, ma50 = r['price'], r['ma200'], r['ma50']
        held = p['shares'].get(s, 0) > 0
        action = str(r['action']).lower()
        frozen_stop = p['stops'].get(s)
        stop_exit = frozen_stop is not None and price <= frozen_stop
        structural_exit = action == 'avoid' and price < min(ma50, ma200)
        if arm == 'simple_trend':
            active = price > ma200
        elif arm == 'no_entry_confirmation':
            active = not stop_exit and not structural_exit if held else price > ma200 and price > ma50
        else:
            active = not stop_exit and (arm == 'no_structural_exit' or not structural_exit) if held else action in {'enter', 'enter_now', 'accumulate'}
        if active:
            if not held and arm != 'simple_trend' and not 0 < (r.get('stop') or 0) < price:
                continue
            weights[s] = 1 / len(universe)
            if held and frozen_stop is not None:
                stops[s] = frozen_stop
            elif arm != 'simple_trend':
                stops[s] = r['stop']
    return weights, stops


def schedule(p, arm, snapshot, spec):
    """Complete-close observations schedule a future open. No retrospective observations."""
    p = copy.deepcopy(p)
    # Rebalance selection at the first executable open in a new month. The collector
    # supplies the exchange calendar's next session to avoid weekend-month mistakes.
    next_month = snapshot['next_session'][:7]
    receipt = snapshot.get('full_plan')
    if arm in EXTERNAL:
        if arm == 'selected_passive' and p['last_selection_month'] == next_month:
            return p, None
        if receipt is None:
            return p, 'missing_complete_policy_receipt'
        if arm == 'full_system':
            weights, stops = receipt['target_weights'], receipt.get('stops', {})
        else:
            chosen = sorted((r for r in receipt['candidates'] if r['selected']), key=lambda r: (-r['score'], r['ticker']))[:spec['selection_count']]
            weights = {r['ticker']: 1 / spec['selection_count'] for r in chosen}
            stops = {}
    elif arm in {'SPY', 'QQQ'}:
        return p, None  # Initial benchmark orders are registered before collection.
    elif arm == 'broad_passive':
        if p['last_selection_month'] == next_month:
            return p, None
        weights = {s: 1 / len(spec['universe']) for s in spec['universe']}; stops = {}
    else:
        weights, stops = technical_weights(p, snapshot['candidates'], arm)
        # No mechanical daily rebalance of unchanged active positions. New target
        # membership causes an explicit rebalance; a failed fill is retried next close.
        if set(weights) == set(p['shares']) and not p.get('unfilled'):
            return p, None
    recorded = snapshot.get('full_plan_received_at') if arm in EXTERNAL else snapshot['received_at']
    p['pending'] = {'weights': weights, 'stops': stops, 'recorded_at': recorded,
                    'decision_id': snapshot['snapshot_id']}
    if arm in {'broad_passive', 'selected_passive'}:
        p['last_selection_month'] = next_month
    return p, None


def initial_state(spec, seed=None):
    books = {}
    for cost in spec['cost_scenarios_bps']:
        books[str(cost)] = {a: new_portfolio(spec['initial_capital']) for a in ARMS}
        for s in ('SPY', 'QQQ'):
            books[str(cost)][s]['pending'] = {'weights': {s: 1.}, 'stops': {},
                'recorded_at': spec['registered_at'], 'decision_id': 'registered-benchmark-order'}
        books[str(cost)]['broad_passive']['pending'] = {
            'weights': {s: 1/len(spec['universe']) for s in spec['universe']},
            'stops': {}, 'recorded_at': spec['registered_at'], 'decision_id': 'registered-broad-order'}
        books[str(cost)]['broad_passive']['last_selection_month'] = spec['starts_on'][:7]
    incidents = []
    if seed:
        validate_snapshot(seed, spec)
        if seed['next_session'] != spec['starts_on']:
            raise ValueError('Seed must target the first registered session')
    for cost in spec['cost_scenarios_bps']:
        for arm in ARMS:
            if arm in {'SPY', 'QQQ', 'broad_passive'}:
                continue
            issue = 'missing_initial_decision'
            if seed:
                books[str(cost)][arm], issue = schedule(books[str(cost)][arm], arm, seed, spec)
            if issue:
                item = {'session': spec['starts_on'], 'kind': issue, 'arms': [arm]}
                if item not in incidents: incidents.append(item)
    return {'books': books, 'incidents': incidents, 'session': None}


def validate_snapshot(snapshot, spec):
    if set(snapshot['candidates']) != set(spec['universe']):
        raise ValueError('Complete eligible set required; no dropped candidates')
    received = timestamp(snapshot['received_at'])
    close = timestamp(snapshot['market_close_at'])
    next_open = timestamp(snapshot['next_open_at'])
    if received < close or received >= next_open or snapshot['next_session'] <= snapshot['session']:
        raise ValueError('Snapshot must arrive after its close and before the next open')
    if close.astimezone(ET).date().isoformat() != snapshot['session']:
        raise ValueError('Snapshot session/close mismatch')
    for r in snapshot['candidates'].values():
        for k in ('price', 'ma50', 'ma200'):
            number(r.get(k), minimum=1e-12)
        number(r.get('score'), minimum=-1e12)
        if not r.get('action') or not r.get('reason'):
            raise ValueError('Candidate action and reason required')
        if r.get('stop') is not None:
            number(r['stop'], minimum=1e-12)
    if snapshot.get('full_plan'):
        plan = validate_plan(snapshot['full_plan'], spec['universe'], spec.get('complete_policy_id'))
        arrival = timestamp(snapshot['full_plan_received_at'])
        if not close <= arrival < next_open or timestamp(plan['observed_at']) > arrival:
            raise ValueError('Complete policy arrival outside admissible window')
        if plan.get('session') != snapshot['session']:
            raise ValueError('Complete policy session mismatch')
    return snapshot


def process_day(prior, spec, session, bars, snapshot=None, *, cash_gap=1., cash_day=1.):
    state = copy.deepcopy(prior or initial_state(spec))
    if state['session'] is not None and session <= state['session']:
        raise ValueError('Sessions must be append-only')
    if session < spec['starts_on']:
        raise ValueError('Session precedes registered start')
    if set(bars) != set(spec['universe']) | {'SPY', 'QQQ'}:
        raise ValueError('Complete price coverage required')
    if snapshot:
        validate_snapshot(snapshot, spec)
        if snapshot['session'] != session:
            raise ValueError('Never backfill earlier decisions')
    elif not any(i['kind'] == 'missing_snapshot' and i['session'] == session for i in state['incidents']):
        state['incidents'].append({'session': session, 'kind': 'missing_snapshot', 'arms': [a for a in ARMS if a not in {'SPY', 'QQQ'}]})
    state['session'] = session; state['raw_bars'] = bars
    state['cash_factors'] = {'overnight': cash_gap, 'intraday': cash_day}
    for cost in spec['cost_scenarios_bps']:
        for arm in ARMS:
            p = advance_portfolio(state['books'][str(cost)][arm], bars, session, cost_bps=cost,
                cash_gap=cash_gap, cash_day=cash_day, participation=spec['participation_limit'])
            if p['issues']:
                # Retain each incident once across reruns and cost scenarios.
                for issue in p['issues']:
                    item = {**issue, 'arms': [arm]}
                    if item not in state['incidents']: state['incidents'].append(item)
                p['issues'] = []
            if snapshot:
                p, issue = schedule(p, arm, snapshot, spec)
                if issue:
                    item = {'session': session, 'kind': issue, 'arms': [arm]}
                    if item not in state['incidents']: state['incidents'].append(item)
            state['books'][str(cost)][arm] = p
    state['snapshot_id'] = snapshot['snapshot_id'] if snapshot else None
    return state


def metrics(equity, initial):
    peak, dd, previous, returns = initial, 0., initial, []
    for value in equity:
        peak = max(peak, value); dd = min(dd, value / peak - 1)
        returns.append(value / previous - 1); previous = value
    return {'equity': equity[-1], 'return_pct': (equity[-1]/initial-1)*100,
            'max_drawdown_pct': dd*100,
            'annualized_volatility_pct': statistics.stdev(returns)*math.sqrt(252)*100 if len(returns)>1 else None,
            'annualized_return_pct': ((equity[-1]/initial)**(252/len(equity))-1)*100 if len(equity)>=252 else None}


def scoreboard(spec, days):
    if not days:
        return {'status': 'registered', 'protocol_id': spec['protocol_id'], 'completed_sessions': 0,
                'starts_on':spec['starts_on'],'universe_count':len(spec['universe']),
                'universe_basis':spec['universe_basis'],'complete_policy_id':spec.get('complete_policy_id'),
                'rows': [], 'actual_account_performance': False, 'verdict': 'No observations yet'}
    initial = spec['initial_capital']; rows = []; n = len(days)
    bad = {a for i in days[-1]['incidents'] for a in i['arms']}
    for cost in spec['cost_scenarios_bps']:
        books = days[-1]['books'][str(cost)]
        spy = metrics([d['books'][str(cost)]['SPY']['equity'] for d in days], initial)
        qqq = metrics([d['books'][str(cost)]['QQQ']['equity'] for d in days], initial)
        for arm in ARMS:
            m = metrics([d['books'][str(cost)][arm]['equity'] for d in days], initial)
            p = books[arm]
            rows.append({'arm': arm, 'label': LABELS[arm], 'cost_bps': cost,
                'status': 'incomplete' if arm in bad else 'collecting',
                **{k: v if arm not in bad else None for k, v in m.items()},
                'excess_vs_spy_pp': m['return_pct']-spy['return_pct'] if arm not in bad else None,
                'excess_vs_qqq_pp': m['return_pct']-qqq['return_pct'] if arm not in bad else None,
                'exposure_pct': p['exposure']*100, 'costs_dollars': p['costs'],
                'turnover_pct': p['turnover']/initial*100, 'open_positions': len(p['shares']),
                'unfilled_orders_today': len(p['unfilled']),
                'rolling_excess_vs_spy_pp': {str(w): (
                    ((p['equity']/days[-w-1]['books'][str(cost)][arm]['equity'])-
                     (books['SPY']['equity']/days[-w-1]['books'][str(cost)]['SPY']['equity']))*100
                    if n>w and arm not in bad else None) for w in (63,126,252)}})
    return {'status': 'collecting' if not bad else 'partial_coverage', 'protocol_id': spec['protocol_id'],
        'as_of': days[-1]['session'], 'starts_on': spec['starts_on'], 'completed_sessions': n,
        'universe_count': len(spec['universe']), 'universe_basis': spec['universe_basis'],
        'rows': rows, 'incidents': days[-1]['incidents'], 'primary_benchmark': 'SPY',
        'verdict': 'No demonstrated edge: ' + ('incomplete full-system record' if 'full_system' in bad else 'formal assessment pending'),
        'actual_account_performance': False, 'mature_performance_claim': False,
        'basis': 'Prospective paper portfolios, recorded decisions and next-open executions. Close-confirmed stops, not intraday stop fills.',
        'assessment_sessions': [252,504], 'complete_policy_id': spec.get('complete_policy_id')}
