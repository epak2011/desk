"""Publish the completed study only after archive, source and reviewed-result checks."""
import argparse,gzip,hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import validation_engine as engine
import validation_backtest
import performance_tracker


def verified_summary(root=ROOT):
    folder=Path(root)/'research/results'
    manifest=json.loads((folder/'validation-2021-2026-publication.json').read_text())
    packed=(folder/'validation-2021-2026-summary.json.gz').read_bytes()
    if hashlib.sha256(packed).hexdigest()!=manifest['compressed_sha256']:raise ValueError('Study archive changed')
    with gzip.open(folder/'validation-2021-2026-summary.json.gz','rb') as f:
        payload=f.read(4_000_001)
    if len(payload)>4_000_000 or hashlib.sha256(payload).hexdigest()!=manifest['json_sha256']:raise ValueError('Study JSON hash or size mismatch')
    summary=json.loads(payload)
    performance_tracker.validate_summary(summary)
    if summary['study_id']!=manifest['study_id']:raise ValueError('Reviewed study identity mismatch')
    if summary['source_sha256']!=validation_backtest.source_hashes(root):raise ValueError('Calculation sources differ from completed replay')
    if len(summary['input_sha256'])!=113 or len(summary['universe'])!=110:raise ValueError('Complete input cohort required')
    reviewed=json.loads((folder/'validation-2021-2026-results.json').read_text())
    for name,period in reviewed['periods'].items():
        if engine.digest(period['rows'])!=engine.digest(summary['periods'][name]['rows']):raise ValueError('Published rows differ from reviewed results')
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--verify-only',action='store_true');args=p.parse_args()
    summary=verified_summary()
    if not args.verify_only:
        import backend_layer as backend
        performance_tracker.publish_summary(summary,backend)
    print(json.dumps({'status':'verified' if args.verify_only else 'published','study_id':summary['study_id'],
        'periods':list(summary['periods']),'universe_count':len(summary['universe']),'forward_ledger_changed':False},indent=2))


if __name__=='__main__':main()
