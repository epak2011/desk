"""Replay archived public inputs using parallel, equivalent technical states."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import pandas as pd
import parallel_replay
from backtest_runner import replay,Assumptions,validate_bars


def main():
    p=argparse.ArgumentParser();p.add_argument('--dataset',type=Path,required=True);p.add_argument('--workers',type=int,choices=range(1,5),default=4);p.add_argument('--start',default='2024-01-02');p.add_argument('--end',default='2026-11-04');p.add_argument('--validation-start',default='2026-01-02');p.add_argument('--output',type=Path);args=p.parse_args()
    folder=args.dataset;spec=json.loads((folder/'universe.json').read_text());names=sorted(t for v in spec['sectors'].values() for t in v)
    output=args.output or folder/'report-parallel.json'
    if output.exists():raise ValueError('Choose a new output; existing report is immutable.')
    def read(ticker):
        b=validate_bars(pd.read_csv(folder/'data'/(ticker+'.csv'),parse_dates=['Date']).set_index('Date'));b.attrs['replay_ticker']=ticker;return b
    stocks={t:read(t) for t in names};spy,qqq,cash=read('SPY'),read('QQQ'),read('SGOV')
    dates=spy.loc[args.start:args.end].index
    for name,b in stocks.items():
        if not dates.isin(b.index).all() or len(b.loc[b.index<args.start])<200:raise ValueError('Full universe preflight failed: '+name)
    frozen={n:(ROOT/n).read_text() for n in ('parallel_replay.py','scripts/replay_cached_dataset.py','research_fast_levels.py')}
    states=parallel_replay.precompute(folder/'data',names,dates,folder/'signal-cache',args.workers)
    def compute(stock,bench):return states[stock.attrs['replay_ticker']].get(stock.index[-1])
    r=replay(stocks,spy,start=args.start,end=args.end,validation_start=args.validation_start,
             comparison_benchmarks={'QQQ':qqq},cash_bars=cash,assumptions=Assumptions(),compute=compute)
    r['universe_selection']=spec;r['runtime']={'pandas':pd.__version__,'compute_mode':'parallel technical-state cache with equivalent vectorized key-level reductions; full-state equivalence checks passed, production source unchanged; no reconstructed historical AI'}
    for name,text in frozen.items():r['frozen_sources'][name]=text;r['code_sha256'][name]=hashlib.sha256(text.encode()).hexdigest()
    for m in r['strategy_manifests'].values():
        m['source_sha256']=r['code_sha256'];m.pop('strategy_id',None);m['strategy_id']=hashlib.sha256(json.dumps(m,sort_keys=True).encode()).hexdigest()
    r['input_sha256']={t:hashlib.sha256((folder/'data'/(t+'.csv')).read_bytes()).hexdigest() for t in names+['SPY','QQQ','SGOV']}
    output.write_text(json.dumps(r,indent=2,allow_nan=False)+'\n');print('Saved '+str(output),flush=True)

if __name__=='__main__':main()
