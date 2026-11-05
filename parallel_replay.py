"""Deterministic local process cache for the exact frozen technical computation.

No strategy changes. Cache keys cover dates, source hashes and input CSV bytes.
Files contain only locally generated Python states and are never remote inputs.
"""
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
import pickle
from pathlib import Path
import pandas as pd
import tactical


def build_one(task):
    ticker,data_dir,dates,cache_path=task
    p=Path(cache_path)
    if p.exists():
        with p.open('rb') as f:return ticker,pickle.load(f)
    from backtest_runner import validate_bars
    def read(symbol):return validate_bars(pd.read_csv(Path(data_dir)/(symbol+'.csv'),parse_dates=['Date']).set_index('Date'))
    stock,bench=read(ticker),read('SPY')
    result={}
    for date in dates:
        state=tactical.compute(stock.loc[:date],bench.loc[:date])
        result[pd.Timestamp(date)]=tactical.apply_extension_execution_overlay(state) if state else None
    p.parent.mkdir(parents=True,exist_ok=True)
    temporary=p.with_suffix('.tmp')
    with temporary.open('wb') as f:pickle.dump(result,f,protocol=pickle.HIGHEST_PROTOCOL)
    temporary.replace(p)
    return ticker,result


def precompute(data_dir,tickers,dates,cache_dir,workers=4):
    root=Path(__file__).parent
    sources={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in ('tactical.py','position_policy.py','parallel_replay.py','backtest_runner.py')}
    bench_hash=hashlib.sha256((Path(data_dir)/'SPY.csv').read_bytes()).hexdigest()
    tasks=[]
    for ticker in tickers:
        key=hashlib.sha256(json.dumps({'ticker':ticker,'stock_sha':hashlib.sha256((Path(data_dir)/(ticker+'.csv')).read_bytes()).hexdigest(),
              'benchmark_sha':bench_hash,'sources':sources,'dates':[str(d) for d in dates]},sort_keys=True).encode()).hexdigest()
        tasks.append((ticker,str(data_dir),list(dates),str(Path(cache_dir)/(key+'.pickle'))))
    states={}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures=[pool.submit(build_one,t) for t in tasks]
        for future in as_completed(futures):
            ticker,result=future.result();states[ticker]=result
            print(f'Frozen technical signals cached: {len(states)}/{len(tickers)} ({ticker})',flush=True)
    return states
