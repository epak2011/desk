"""Archive provider OHLCV/actions for an explicitly retrospective comparison."""
import argparse
import hashlib
import json
import sys
from datetime import datetime,timedelta,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def main():
    import yfinance as yf
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--start',required=True);p.add_argument('--end',required=True)
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--universe-file',type=Path,default=ROOT/'research/validation-template.json')
    args=p.parse_args()
    start=datetime.fromisoformat(args.start).date();end=datetime.fromisoformat(args.end).date()
    if end>=datetime.now(timezone.utc).date() or end<start:raise ValueError('Require a completed prior-day range')
    args.output_dir.mkdir(parents=True,exist_ok=False);data=args.output_dir/'provider';data.mkdir()
    spec=json.loads(args.universe_file.read_text());(args.output_dir/'universe.json').write_text(json.dumps(spec,indent=2)+'\n')
    result={'downloaded_at':datetime.now(timezone.utc).isoformat(),'start':args.start,'end':args.end,
        'source':'Yahoo via yfinance history(auto_adjust=False, actions=True, repair=False)',
        'yfinance_version':yf.__version__,'provider_prices_are_split_normalized':True,'files':{},'failed':[]}
    for symbol in sorted(set(spec['universe'])|{'SPY','QQQ','SGOV'}):
        try:
            f=yf.Ticker(symbol).history(start=str(start-timedelta(days=450)),end=str(end+timedelta(days=1)),
                auto_adjust=False,actions=True,repair=False,raise_errors=True)
            if f.empty:raise ValueError('Empty provider history')
            f.index=f.index.tz_localize(None).normalize();f.index.name='Date'
            name=symbol+'.csv';f.to_csv(data/name)
            result['files'][symbol]={'sha256':hashlib.sha256((data/name).read_bytes()).hexdigest(),'rows':len(f)}
            print('Archived provider inputs '+str(len(result['files']))+'/113 '+symbol,flush=True)
        except Exception as exc:
            result['failed'].append({'ticker':symbol,'reason':str(exc)[:200]})
    (args.output_dir/'manifest.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    if result['failed']:raise ValueError('Incomplete cohort; inspect manifest.json. No stock silently removed.')
    print('All provider inputs archived',flush=True)


if __name__=='__main__':main()
