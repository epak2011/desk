"""Future-only registration, explicit universe provenance and source locking."""
import copy
import hashlib
from datetime import datetime
from pathlib import Path
from validation_engine import digest, timestamp, ET

SOURCES=('validation_engine.py','validation_stats.py','validation_store.py','validation_protocol.py',
         'validation_capture.py','validation_ingest.py','validation_execution.py','scripts/run_validation.py','tactical.py','position_policy.py')


def runtime_requirements(spec):
    import re
    allowed={'numpy','pandas','yfinance','exchange-calendars'}
    versions=spec['runtime_versions']
    if set(versions)!=allowed:raise ValueError('Unexpected calculation dependency set')
    if any(not isinstance(v,str) or not re.fullmatch(r'[0-9][A-Za-z0-9.!+_-]*',v) for v in versions.values()):
        raise ValueError('Invalid frozen package version')
    return [p+'=='+versions[p] for p in sorted(allowed)]


def register(template, starts_on, now, root, *, complete_policy_id=None):
    spec=copy.deepcopy(template)
    if timestamp(now).astimezone(ET).date().isoformat()>=starts_on:
        raise ValueError('Start must be a future US market session; never backdate')
    universe=spec['universe']
    if not universe or len(universe)!=len(set(universe)) or {'SPY','QQQ','SGOV'} & set(universe):
        raise ValueError('Invalid stock universe')
    if not spec.get('universe_basis') or not spec.get('universe_source'):
        raise ValueError('Universe provenance required')
    if spec['primary_cost_bps'] not in spec['cost_scenarios_bps'] or len(set(spec['cost_scenarios_bps']))!=len(spec['cost_scenarios_bps']):
        raise ValueError('Invalid cost scenarios')
    if spec['initial_capital']<=0 or not 0<spec['participation_limit']<=1 or not 0<spec['selection_count']<=len(universe):
        raise ValueError('Invalid portfolio assumptions')
    spec.update(registered_at=now,starts_on=starts_on,complete_policy_id=complete_policy_id)
    from importlib.metadata import version
    spec['runtime_versions']={p:version(p) for p in ('numpy','pandas','yfinance','exchange-calendars')}
    spec['source_sha256']={s:hashlib.sha256((Path(root)/s).read_bytes()).hexdigest() for s in SOURCES}
    spec['protocol_id']=digest(spec)
    return spec


def verify(spec,root,*,check_runtime=False):
    unsigned={k:v for k,v in spec.items() if k!='protocol_id'}
    if digest(unsigned)!=spec['protocol_id']:raise ValueError('Protocol edited')
    if timestamp(spec['registered_at']).astimezone(ET).date().isoformat()>=spec['starts_on']:
        raise ValueError('Backdated protocol')
    for s,expected in spec['source_sha256'].items():
        if hashlib.sha256((Path(root)/s).read_bytes()).hexdigest()!=expected:
            raise ValueError('Frozen source changed: '+s)
    if check_runtime:
        from importlib.metadata import version
        for p,expected in spec['runtime_versions'].items():
            if version(p)!=expected:raise ValueError('Frozen calculation dependency changed: '+p)


def objective_universe(records, *, observed_at, source, limit=500):
    """Import a provider's dated eligible-security master, not today's historical reconstruction."""
    if not source or not records:raise ValueError('Provider and full eligible-security master required')
    observed=timestamp(observed_at);qualified=[];tickers=set()
    for row in records:
        if row['ticker'] in tickers:raise ValueError('Duplicate security')
        tickers.add(row['ticker'])
        if timestamp(row['as_of'])>observed:raise ValueError('Future membership information')
        for k in ('float_market_cap','price','median_dollar_volume_60'):
            if not isinstance(row[k],(float,int)) or not __import__('math').isfinite(row[k]) or row[k]<0:raise ValueError('Invalid eligibility data')
        if (row['security_type']=='common_stock' and row['country']=='US' and row['primary_listing'] is True
                and row['price']>=5 and row['median_dollar_volume_60']>=20_000_000 and row['float_market_cap']>0):
            qualified.append(row)
    qualified.sort(key=lambda r:(-r['float_market_cap'],r['ticker']))
    chosen=[];issuers=set()
    for row in qualified:
        if row['issuer_id'] not in issuers:
            issuers.add(row['issuer_id']);chosen.append(row)
        if len(chosen)==limit:break
    if len(chosen)<limit:raise ValueError('Insufficient eligible names; do not silently shrink the universe')
    return {'universe':[r['ticker'] for r in chosen],'universe_basis':'Prospective objective liquidity/float-cap cohort; fixed at registration',
        'universe_source':{'provider':source,'observed_at':observed_at,'records_sha256':digest(records),
            'source_records':len(records),'selected':len(chosen),'rule':'US primary common stocks; $5 price; $20m trailing 60-session median dollar volume; largest eligible class per issuer; descending float cap'},
        'sectors':{r['ticker']:r.get('sector') for r in chosen},
        'membership_refresh':'Separate version required; original fixed cohort and delistings preserved'}
