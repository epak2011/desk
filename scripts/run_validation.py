#!/usr/bin/env python3
"""Register, collect, import complete-policy receipts, or export owner-only evidence.

No broker calls or production rule changes. DATABASE_URL uses the existing pool;
--sqlite is an isolated local evidence store for rehearsal and reproducibility.
"""
import argparse
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import validation_engine as engine
import validation_protocol as protocol
from validation_store import Store, build_report


@contextmanager
def transaction(sqlite_path=None):
    if sqlite_path:
        import sqlite3
        conn=sqlite3.connect(sqlite_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            store=Store(conn.cursor(),sqlite=True);store.initialize();yield store
            conn.commit()
        except Exception:conn.rollback();raise
        finally:conn.close()
    else:
        import backend_layer as backend
        with backend.db_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    store=Store(cur);store.initialize();store.lock();yield store


def publish(store, report):
    if not store.sqlite:
        store.execute("INSERT INTO engine_review_status(key,payload,updated_at) VALUES ('validation_performance',%s::jsonb,NOW()) ON CONFLICT(key) DO UPDATE SET payload=EXCLUDED.payload,updated_at=NOW()",(json.dumps(report,allow_nan=False),))


def register(args,now):
    from validation_capture import market_window
    market_window(args.starts_on)  # Reject weekends and exchange holidays.
    template=json.loads((ROOT/'research/validation-template.json').read_text())
    if args.universe_file:
        imported=json.loads(Path(args.universe_file).read_text())
        template.update(protocol.objective_universe(imported['records'],observed_at=now,source=imported['source']))
    policy=None
    if args.policy_file:
        policy=json.loads(Path(args.policy_file).read_text())
        if not all(policy.get(k) for k in ('selection','sizing','risk','execution')):
            raise ValueError('Complete selection, sizing, risk and execution policy must be frozen')
    spec=protocol.register(template,args.starts_on,now,ROOT,complete_policy_id=engine.digest(policy) if policy else None)
    if policy:spec['complete_policy_manifest']=policy;spec['protocol_id']=engine.digest({k:v for k,v in spec.items() if k!='protocol_id'})
    with transaction(args.sqlite) as store:
        store.activate(spec);publish(store,build_report(store,spec['protocol_id']))
    return {'status':'registered','protocol_id':spec['protocol_id'],'starts_on':spec['starts_on'],
            'universe_count':len(spec['universe']),'complete_system_ready':bool(policy)}


def import_plan(store,spec,path,now):
    from validation_capture import market_window
    from validation_ingest import record_plan
    plan=json.loads(Path(path).read_text())
    return record_plan(store,spec,plan,now,market_window(plan['session']))


def collect(store,spec,now):
    from validation_capture import capture
    from validation_stats import assess
    snapshot,bars,cash,archive=capture(spec,now)
    key=spec['protocol_id'];session=snapshot['session']
    existing=store.get(key,'snapshot',session)
    if existing:snapshot=existing
    else:
        store.append(key,'inputs',session,archive)
        receipt=store.get(key,'plan',session)
        if receipt:
            snapshot.update(full_plan=receipt['plan'],full_plan_received_at=receipt['received_at'])
        snapshot['snapshot_id']=engine.digest({k:v for k,v in snapshot.items() if k!='snapshot_id'})
        engine.validate_snapshot(snapshot,spec)
        store.append(key,'snapshot',session,snapshot)
    days=store.read(key,'day');prior=days[-1] if days else None
    if prior is None:
        seeds=[s for s in store.read(key,'snapshot') if s['next_session']==spec['starts_on']]
        prior=engine.initial_state(spec,seeds[-1] if seeds else None)
    for day in sorted(bars):
        if prior['session'] and day<=prior['session']:continue
        # Only a snapshot actually received before the next open can be used.
        obs=store.get(key,'snapshot',day)
        factors=dict(cash[day])
        if not days:factors['cash_gap']=1.
        prior=engine.process_day(prior,spec,day,bars[day],obs,**factors)
        prior['recorded_at']=now
        store.append(key,'day',day,prior);days.append(prior)
        if len(days) in (252,504):store.append(key,'assessment',str(len(days)),assess(spec,days))
    report=build_report(store,key)
    report['generated_at']=now
    publish(store,report)
    return {k:report[k] for k in ('status','completed_sessions','universe_count','verdict')}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=('register','collect','runtime-lock','import-plan','import-executions','export','status'))
    p.add_argument('--sqlite');p.add_argument('--starts-on');p.add_argument('--policy-file')
    p.add_argument('--universe-file');p.add_argument('--plan-file');p.add_argument('--executions-file');p.add_argument('--output')
    args=p.parse_args();now=datetime.now(timezone.utc).isoformat()
    if args.command=='runtime-lock':
        if not args.output:p.error('--output required for the calculation dependency lock')
        Path(args.output).write_text('')
    if args.command=='register':
        if not args.starts_on:p.error('--starts-on future exchange session required')
        result=register(args,now)
    else:
        try:
            with transaction(args.sqlite) as store:
                key=store.active()
                if not key:
                    result={'status':'not_registered','reason':'Register a future start after release readiness. No backdated observations.'}
                else:
                    spec=store.get(key,'protocol','registered');protocol.verify(spec,ROOT,check_runtime=args.command=='collect')
                    if args.command=='import-plan':
                        if not args.plan_file:p.error('--plan-file required')
                        result=import_plan(store,spec,args.plan_file,now)
                    elif args.command=='collect':result=collect(store,spec,now)
                    elif args.command=='runtime-lock':
                        from validation_protocol import runtime_requirements
                        lines=runtime_requirements(spec)
                        Path(args.output).write_text('\n'.join(lines)+'\n')
                        result={'status':'runtime_lock_written','packages':len(lines)}
                    elif args.command=='import-executions':
                        from validation_execution import validate_statement,validate_fill_identity
                        if not args.executions_file:p.error('--executions-file required')
                        statement=validate_statement(json.loads(Path(args.executions_file).read_text()))
                        validate_fill_identity(statement,store.read(key,'executions'))
                        statement_key=engine.digest([statement['source'],statement['statement_id']])
                        existing=store.get(key,'executions',statement_key)
                        if existing and engine.digest(existing)!=engine.digest(statement):raise ValueError('Immutable statement conflict')
                        store.append(key,'executions',statement_key,statement)
                        publish(store,build_report(store,key));result={'status':'actual_fills_recorded','fills':len(statement['fills'])}
                    elif args.command=='status':result=build_report(store,key)
                    else:
                        if not args.output:p.error('--output required; private evidence is written only to this explicit path')
                        result={'protocol':spec,'integrity':store.audit(key),
                            **{k:store.read(key,k) for k in ('snapshot','plan','day','assessment','incident','executions','inputs')}}
                        Path(args.output).write_text(json.dumps(result,indent=2,allow_nan=False))
                        result={'status':'exported','path':args.output}
        except Exception as exc:
            if args.command in ('collect','runtime-lock'):
                # Failed accounting transaction rolls back. Incident survives in
                # a fresh transaction; never replace the last good equity curve.
                with transaction(args.sqlite) as store:
                    key=store.active()
                    if key:
                        store.append(key,'incident',now,{'received_at':now,'kind':'collection_failed',
                            'reason':type(exc).__name__,'detail':str(exc)[:240]})
                        report=build_report(store,key)
                        report.update(status='operational_blocked',last_failure_at=now)
                        publish(store,report)
            raise
    print(json.dumps(result,indent=2,allow_nan=False))


if __name__=='__main__':main()
