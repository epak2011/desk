"""Run the current forward engine historically; never write to the forward ledger."""
import argparse,json,sys
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import validation_backtest as replay
import validation_engine as engine


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',type=Path,required=True);p.add_argument('--start',default='2021-01-04')
    p.add_argument('--end',default='2026-11-05');p.add_argument('--reset-start',default='2026-01-02')
    p.add_argument('--workers',type=int,choices=range(1,5),default=4)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--publish',action='store_true')
    args=p.parse_args()
    if args.output.exists():raise ValueError('Existing replay output is immutable; choose a new path')
    spec=json.loads((args.dataset/'universe.json').read_text())
    nominal,manifest,dates,seed=replay.validate_dataset(args.dataset,spec,args.start,args.end)
    if args.reset_start not in dates:raise ValueError('Reset start not in complete sample')
    signals=replay.precompute(args.dataset,spec['universe'],[seed]+dates,args.dataset/'signal-cache',ROOT,args.workers)
    periods={'full':replay.run_period(spec,nominal,signals,dates),
             'recent_reset':replay.run_period(spec,nominal,signals,[d for d in dates if d>=args.reset_start])}
    hashes=replay.source_hashes(ROOT)
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'evidence_type':'retrospective_validation_engine_simulation',
        'study_id':engine.digest({'inputs':manifest['files'],'source_sha256':hashes,'start':args.start,'end':args.end,'reset':args.reset_start,'assumptions':spec}),
        'universe':spec['universe'],'universe_basis':spec['universe_basis'],'source_sha256':hashes,
        'frozen_sources':{s:(ROOT/s).read_text() for s in replay.SOURCES},'input_manifest':manifest,
        'assumptions':{k:spec[k] for k in ('initial_capital','cost_scenarios_bps','primary_cost_bps','participation_limit')},
        'limitations':replay.LIMITATIONS,'periods':periods,'actual_account_performance':False,
        'new_forward_results':False,'complete_policy_reconstructed':False}
    args.output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    print('Saved '+str(args.output),flush=True)
    if args.publish:
        from performance_tracker import publish_backtest
        import backend_layer as backend
        publish_backtest(report,backend)


if __name__=='__main__':main()
