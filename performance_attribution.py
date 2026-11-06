"""Mechanical entry/exit diagnostics; never optimizer inputs or trading rules."""
import math
from statistics import mean
import pandas as pd


def attribute(bars, result, cost_bps=10):
    """Exact daily log-return bridge versus holding the same stock.

    These are path accounting buckets, not causal proof that changing a rule
    would improve returns. All forward event windows include exited trades.
    """
    curve, trades = result['curve'], result['trades']
    cost = cost_bps / 10000
    passive = bars.Close / (float(bars.Open.iloc[0]) * (1 + cost))
    if result.get('terminal_liquidation', True):
        passive.iloc[-1] *= 1 - cost
    system_returns = curve.equity / curve.equity.shift(fill_value=1)
    passive_returns = passive / passive.shift(fill_value=1)
    entries = {t['entry_date'] for t in trades}
    exits = {t['exit_date'] for t in trades if not t.get('exit_is_sample_boundary')}
    buckets = {k: 0.0 for k in ('initial_wait', 'entry_days', 'invested_days', 'exit_days', 'between_trades_cash')}
    seen_entry = False
    for day in bars.index:
        stamp = str(day.date())
        if stamp in entries:
            key, seen_entry = 'entry_days', True
        elif stamp in exits:
            key = 'exit_days'
        elif bool(curve.loc[day, 'exposed']):
            key = 'invested_days'
        else:
            key = 'between_trades_cash' if seen_entry else 'initial_wait'
        buckets[key] += 100 * math.log(float(system_returns.loc[day]) / float(passive_returns.loc[day]))
    windows = {f'{kind}_{h}': [] for kind in ('entry', 'exit') for h in (5, 14, 30)}
    eligible = {k: 0 for k in windows}
    for trade in trades:
        for kind in ('entry', 'exit'):
            if kind == 'exit' and trade.get('exit_is_sample_boundary'):
                continue
            day = pd.Timestamp(trade[kind + '_date'])
            if day not in bars.index:
                continue
            i = bars.index.get_loc(day)
            price = trade[kind + '_price']
            for horizon in (5, 14, 30):
                key = f'{kind}_{horizon}'
                eligible[key] += 1
                # The fill session is session 1. Entry/exit reference is its open.
                if i + horizon - 1 < len(bars):
                    windows[key].append((float(bars.Close.iloc[i + horizon - 1]) / price - 1) * 100)
    return {'log_excess_contributions_pct': buckets,
            'log_excess_total_pct': sum(buckets.values()),
            'system_return_pct': (float(curve.equity.iloc[-1]) - 1) * 100,
            'same_stock_buy_hold_return_pct': (float(passive.iloc[-1]) - 1) * 100,
            'windows': {k: {'eligible_events': eligible[k], 'mature_events': len(v),
                            'mean_stock_return_pct': mean(v) if v else None,
                            'positive_fraction_pct': 100 * sum(x > 0 for x in v) / len(v) if v else None}
                        for k, v in windows.items()},
            'basis': 'Exact same-stock log-return bridge; log points sum, not arithmetic percentage-point alpha. Exit windows are counterfactual stock returns after selling; positive means foregone upside, negative means avoided loss. Overlapping descriptive windows are not independent tests.'}


def summarize(rows):
    """Equal-name descriptive diagnostics; portfolio bridge is reported separately."""
    if not rows:
        return {'status': 'unavailable', 'ticker_count': 0}
    keys = rows[0]['log_excess_contributions_pct']
    windows = {}
    for key in rows[0]['windows']:
        records = [r['windows'][key] for r in rows]
        n = sum(r['mature_events'] for r in records)
        windows[key] = {'eligible_events': sum(r['eligible_events'] for r in records), 'mature_events': n,
                        'mean_stock_return_pct': sum((r['mean_stock_return_pct'] or 0) * r['mature_events'] for r in records) / n if n else None}
    return {'status': 'available', 'ticker_count': len(rows),
            'mean_ticker_log_excess_contributions_pct': {k: mean(r['log_excess_contributions_pct'][k] for r in rows) for k in keys},
            'event_windows': windows, 'basis': rows[0]['basis'],
            'aggregation': 'Mean of per-ticker log bridges; not the arithmetic portfolio excess return and not a rule-change recommendation.'}
