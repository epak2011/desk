"""Independent daily paper ledger; no dependency on research generation."""
import json
import sys
from datetime import datetime, timedelta, timezone, time
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import backend_layer as backend
import forward_paper as paper
import tactical

SPEC=ROOT/'research/forward-paper-110.json'


def publish(cur,report):
    report['generated_at']=datetime.now(timezone.utc).isoformat()
    cur.execute("INSERT INTO engine_review_status (key,payload,updated_at) VALUES ('forward_paper_performance',%s::jsonb,NOW()) ON CONFLICT (key) DO UPDATE SET payload=EXCLUDED.payload,updated_at=NOW()",(backend.json_dumps(report),))


def main():
    spec=json.loads(SPEC.read_text());paper.verify(spec,ROOT)
    now=datetime.now(timezone.utc);eastern=now.astimezone(paper.ET)
    # Give the cash market at least two hours to settle its final daily bars.
    cutoff=eastern.date()+timedelta(days=1) if eastern.hour>=18 else eastern.date()
    if cutoff.isoformat()<=spec['starts_on']:
        with backend.db_connection() as conn:
            with conn.cursor() as cur:publish(cur,paper.summary(spec,[]))
        print(json.dumps({'status':'scheduled','starts_on':spec['starts_on'],'universe_count':len(spec['universe'])}));return
    import yfinance as yf
    import pandas as pd
    raw={};adjusted={}
    for symbol in sorted(set(spec['universe'])|{'SPY','QQQ','SGOV'}):
        frame=yf.Ticker(symbol).history(start=str(datetime.fromisoformat(spec['starts_on']).date()-timedelta(days=450)),end=cutoff.isoformat(),auto_adjust=False,actions=True,raise_errors=True)
        if frame.empty:raise ValueError('Required raw history unavailable: '+symbol)
        frame.index=pd.DatetimeIndex([pd.Timestamp(d.date()) for d in frame.index])
        raw[symbol]=frame
        # Preserve raw action/price accounting. The technical input is normalized
        # to latest raw-close units using the provider's adjustment ratio.
        factor=frame['Adj Close']/frame['Close'] if 'Adj Close' in frame else pd.Series(1.,index=frame.index)
        adj=frame.copy()
        for col in ('Open','High','Low','Close'):adj[col]=frame[col]*factor
        adjusted[symbol]=adj
    sessions=raw['SPY'].loc[spec['starts_on']:].index
    if len(sessions)==0:
        print(json.dumps({'status':'no_completed_market_session'}));return
    latest=sessions[-1]
    if latest.date()+timedelta(days=3)<cutoff:
        raise ValueError('Benchmark source stale; preserve prior ledger.')
    # Do not silently drop missing symbols or backfill old strategy decisions.
    for symbol,frame in raw.items():
        if not sessions.isin(frame.index).all():raise ValueError('Incomplete declared universe: '+symbol)
    observed_at=now.isoformat()
    captured={}
    for symbol in spec['universe']:
        frame=adjusted[symbol].loc[:latest]
        if len(frame)<200:raise ValueError('Insufficient technical warm-up: '+symbol)
        state=tactical.apply_extension_execution_overlay(tactical.compute(frame,adjusted['SPY'].loc[:latest]) or {})
        if not state or not state.get('action'):raise ValueError('No current rule decision: '+symbol)
        scale=float(raw[symbol].loc[latest,'Close']/frame.Close.iloc[-1])
        captured[symbol]={'action':state['action'],'observed_at':observed_at,
                         **{k:float(state[k])*scale if isinstance(state.get(k),(int,float)) else None for k in ('stop','ma50','ma200')}}
    with backend.db_connection() as conn:
        with conn.cursor() as cur:
            # Session-level append only ledger; same-protocol jobs are serialized.
            cur.execute('SELECT pg_advisory_xact_lock(%s)',(775110,))
            cur.execute('CREATE TABLE IF NOT EXISTS forward_paper_days (protocol_id TEXT NOT NULL,session DATE NOT NULL,payload JSONB NOT NULL,PRIMARY KEY(protocol_id,session))')
            cur.execute('CREATE TABLE IF NOT EXISTS forward_paper_signals (protocol_id TEXT NOT NULL,session DATE NOT NULL,payload JSONB NOT NULL,PRIMARY KEY(protocol_id,session))')
            cur.execute('INSERT INTO forward_paper_signals (protocol_id,session,payload) VALUES (%s,%s,%s::jsonb) ON CONFLICT DO NOTHING',(spec['protocol_id'],str(latest.date()),backend.json_dumps(captured)))
            cur.execute('SELECT session,payload FROM forward_paper_signals WHERE protocol_id=%s ORDER BY session',(spec['protocol_id'],))
            signals={str(day):payload for day,payload in cur.fetchall()}
            cur.execute('SELECT payload FROM forward_paper_days WHERE protocol_id=%s ORDER BY session',(spec['protocol_id'],))
            days=[r[0] for r in cur.fetchall()];prior=days[-1] if days else None
            missing=sum(d.get('missing_observations',0) for d in days)
            for session in sessions:
                stamp=str(session.date())
                if prior and stamp<=prior['session']:continue
                initial=prior is None
                sg=raw['SGOV'];i=sg.index.get_loc(session)
                previous=float(sg.Close.iloc[i-1]) if i>0 and not initial else float(sg.loc[session,'Open'])
                bar=sg.loc[session];split=float(bar.get('Stock Splits') or 0) or 1.
                cash_gap=(float(bar.Open)+float(bar.get('Dividends') or 0))*split/previous if not initial else 1.
                cash_day=float(bar.Close/bar.Open)
                wallets={};passive={};obs=signals.get(stamp,{})
                for symbol in spec['universe']:
                    b={k:float(raw[symbol].loc[session].get(k,0)) for k in ('Open','Close','Dividends','Stock Splits')}
                    wallets[symbol]=paper.advance(prior['wallets'][symbol] if prior else paper.wallet(),b,obs.get(symbol),session=stamp,cash_gap=cash_gap,cash_day=cash_day,cost_bps=spec['cost_bps_per_side'])
                    passive[symbol]=paper.advance(prior['passive'][symbol] if prior else paper.wallet(),b,None,session=stamp,cash_gap=cash_gap,cash_day=cash_day,cost_bps=spec['cost_bps_per_side'],passive=True,initial=initial)
                indexes={}
                for symbol in ('SPY','QQQ'):
                    b={k:float(raw[symbol].loc[session].get(k,0)) for k in ('Open','Close','Dividends','Stock Splits')}
                    indexes[symbol]=paper.advance(prior['indexes'][symbol] if prior else paper.wallet(),b,None,session=stamp,cash_gap=cash_gap,cash_day=cash_day,cost_bps=spec['cost_bps_per_side'],passive=True,initial=initial)
                n=len(wallets);miss=n-len(obs);missing+=miss
                d={'session':stamp,'recorded_at':now.isoformat(),'wallets':wallets,'passive':passive,'indexes':indexes,
                   'missing_observations':miss,
                   'raw_bars':{symbol:{k:float(raw[symbol].loc[session].get(k,0)) for k in ('Open','High','Low','Close','Volume','Dividends','Stock Splits')} for symbol in raw},
                   'cash_factors':{'overnight':cash_gap,'intraday':cash_day},
                   'equity':{'strategy':sum(w['equity'] for w in wallets.values())/n,'same_universe':sum(w['equity'] for w in passive.values())/n,
                             'SPY':indexes['SPY']['equity'],'QQQ':indexes['QQQ']['equity'],
                             'cash':(prior['equity']['cash'] if prior else 1.)*cash_gap*cash_day},
                   'exposure':sum(w['equity']*w['exposure'] for w in wallets.values())/sum(w['equity'] for w in wallets.values()),
                   'turnover':sum(w['turnover'] for w in wallets.values())/n}
                cur.execute('INSERT INTO forward_paper_days (protocol_id,session,payload) VALUES (%s,%s,%s::jsonb)',(spec['protocol_id'],stamp,backend.json_dumps(d)))
                days.append(d);prior=d
            report=paper.summary(spec,days,missing_observations=missing);publish(cur,report)
    print(json.dumps({k:report[k] for k in ('status','completed_sessions','universe_count','missing_observations','rows')}))

if __name__=='__main__':main()
