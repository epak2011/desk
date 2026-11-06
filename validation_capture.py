"""Market adapter. All ranking decisions are captured now, never recomputed for missed dates."""
import math
from datetime import timedelta

from validation_engine import digest, timestamp, ET


def market_window(session):
    import exchange_calendars as xcals
    calendar=xcals.get_calendar('XNYS')
    if not calendar.is_session(session):raise ValueError('Not a US equity market session')
    nxt=calendar.next_session(session)
    return {'session':str(session)[:10], 'market_close_at':calendar.session_close(session).isoformat(),
            'next_session':str(nxt.date()), 'next_open_at':calendar.session_open(nxt).isoformat()}


def capture(spec, now):
    import pandas as pd
    import yfinance as yf
    import tactical
    eastern=timestamp(now).astimezone(ET)
    frames={}; adjusted={}
    for symbol in sorted(set(spec['universe'])|{'SPY','QQQ','SGOV'}):
        frame=yf.Ticker(symbol).history(start=str(__import__('datetime').date.fromisoformat(spec['starts_on'])-timedelta(days=450)),
            end=str(eastern.date()+timedelta(days=1)),auto_adjust=False,actions=True,raise_errors=True)
        if frame.empty:raise ValueError('Missing security history: '+symbol)
        frame.index=pd.DatetimeIndex([pd.Timestamp(d.date()) for d in frame.index])
        factor=frame['Adj Close']/frame['Close']
        adj=frame.copy()
        for k in ('Open','High','Low','Close'):adj[k]=frame[k]*factor
        # Shift ensures liquidity at today's open uses strictly earlier sessions.
        frame['prior_liquidity']=(frame.Close*frame.Volume).rolling(60,min_periods=60).median().shift(1)
        frames[symbol]=frame;adjusted[symbol]=adj
    latest=frames['SPY'].index[-1];window=market_window(str(latest.date()))
    if timestamp(now)<timestamp(window['market_close_at'])+timedelta(hours=2):
        raise ValueError('Wait for settled close; do not capture intraday or stale signals')
    if timestamp(now)>=timestamp(window['next_open_at']):
        raise ValueError('Source close is stale for prospective execution')
    candidates={}
    for symbol in spec['universe']:
        frame=adjusted[symbol].loc[:latest]
        if frame.index[-1]!=latest or len(frame)<200:raise ValueError('Incomplete latest technical history: '+symbol)
        state=tactical.apply_extension_execution_overlay(tactical.compute(frame,adjusted['SPY'].loc[:latest]) or {})
        ratio=float(frames[symbol].loc[latest,'Close']/frame.Close.iloc[-1])
        action=state.get('action');score=state.get('setup_score')
        if not action or not isinstance(score,(int,float)) or not math.isfinite(score):raise ValueError('Unusable rule output: '+symbol)
        candidates[symbol]={'action':action,'score':float(score),'price':float(frames[symbol].loc[latest,'Close']),
            **{k:float(state[k])*ratio if isinstance(state.get(k),(int,float)) and math.isfinite(state[k]) else None for k in ('ma50','ma200','stop')},
            'sector':spec.get('sectors',{}).get(symbol),
            'reason':str(state.get('trigger_fired_reason') or state.get('state') or action),
            'selected':str(action).lower() in {'enter','enter_now','accumulate'}}
    snapshot={**window,'received_at':now,'candidates':candidates,'source':'Yahoo adjusted OHLCV; current frozen technical rules',
        'input_sha256':{s:__import__('hashlib').sha256(frames[s].to_csv().encode()).hexdigest() for s in frames}}
    snapshot['snapshot_id']=digest(snapshot)
    # Exact archived input frames make the recorded indicators reproducible;
    # hashes alone would not recover a provider's later revised history.
    import base64,zlib
    archive={'format':'zlib+base64 CSV','captured_at':now,
        'frames':{s:base64.b64encode(zlib.compress(f.to_csv().encode(),9)).decode() for s,f in frames.items()}}
    sessions=[str(s.date()) for s in frames['SPY'].loc[spec['starts_on']:].index if s<=latest]
    bars={};cash={}
    for session in sessions:
        day=pd.Timestamp(session);bars[session]={}
        for symbol in set(spec['universe'])|{'SPY','QQQ'}:
            f=frames[symbol]
            if day not in f.index:raise ValueError('Coverage gap or corporate action needs reconciliation: '+symbol+'/'+session)
            row=f.loc[day]
            bars[session][symbol]={'Open':float(row.Open),'Close':float(row.Close),
                'dividend':float(row.get('Dividends',0)), 'split':float(row.get('Stock Splits',0)) or 1.,
                'prior_median_dollar_volume':float(row.prior_liquidity)}
        f=frames['SGOV'];i=f.index.get_loc(day);r=f.loc[day]
        split=float(r.get('Stock Splits',0)) or 1.
        cash[session]={'cash_gap':(float(r.Open)+float(r.get('Dividends',0)))*split/float(f.Close.iloc[i-1]),
                       'cash_day':float(r.Close/r.Open)}
    return snapshot,bars,cash,archive
