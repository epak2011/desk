"""Append-only hash-chained evidence. Caller must hold a database transaction."""
import json
from validation_engine import digest


class Store:
    def __init__(self, cursor, *, sqlite=False):
        self.cur, self.sqlite = cursor, sqlite

    def execute(self, sql, params=()):
        self.cur.execute(sql.replace('%s','?') if self.sqlite else sql, params)
        return self.cur

    def initialize(self):
        self.execute('CREATE TABLE IF NOT EXISTS validation_records (protocol_id TEXT NOT NULL,kind TEXT NOT NULL,record_key TEXT NOT NULL,payload TEXT NOT NULL,record_hash TEXT NOT NULL,previous_hash TEXT NOT NULL,ordinal BIGINT NOT NULL,PRIMARY KEY(protocol_id,kind,record_key),UNIQUE(protocol_id,ordinal))')
        self.execute('CREATE TABLE IF NOT EXISTS validation_active (singleton INTEGER PRIMARY KEY,protocol_id TEXT NOT NULL)')

    def lock(self):
        if not self.sqlite:self.execute('SELECT pg_advisory_xact_lock(%s)',(775111,))

    def active(self):
        row=self.execute('SELECT protocol_id FROM validation_active WHERE singleton=1').fetchone()
        return row[0] if row else None

    def activate(self, spec):
        if self.active():raise ValueError('An experiment is already active; preserve it and explicitly create a parallel version')
        self.append(spec['protocol_id'],'protocol','registered',spec)
        self.execute('INSERT INTO validation_active(singleton,protocol_id) VALUES (1,%s)',(spec['protocol_id'],))

    def read(self, protocol, kind):
        return [json.loads(r[0]) for r in self.execute('SELECT payload FROM validation_records WHERE protocol_id=%s AND kind=%s ORDER BY ordinal',(protocol,kind)).fetchall()]

    def get(self, protocol, kind, key):
        row=self.execute('SELECT payload FROM validation_records WHERE protocol_id=%s AND kind=%s AND record_key=%s',(protocol,kind,key)).fetchone()
        return json.loads(row[0]) if row else None

    def append(self, protocol, kind, key, payload):
        old=self.get(protocol,kind,key)
        if old is not None:
            if digest(old)!=digest(payload):raise ValueError('Immutable evidence conflict: '+kind+'/'+key)
            return False
        last=self.execute('SELECT ordinal,record_hash FROM validation_records WHERE protocol_id=%s ORDER BY ordinal DESC LIMIT 1',(protocol,)).fetchone()
        ordinal=last[0]+1 if last else 1;previous=last[1] if last else ''
        envelope={'protocol_id':protocol,'kind':kind,'key':key,'ordinal':ordinal,'previous':previous,'payload':payload}
        self.execute('INSERT INTO validation_records(protocol_id,kind,record_key,payload,record_hash,previous_hash,ordinal) VALUES (%s,%s,%s,%s,%s,%s,%s)',
            (protocol,kind,key,json.dumps(payload,sort_keys=True,allow_nan=False),digest(envelope),previous,ordinal))
        return True

    def audit(self, protocol):
        rows=self.execute('SELECT kind,record_key,payload,record_hash,previous_hash,ordinal FROM validation_records WHERE protocol_id=%s ORDER BY ordinal',(protocol,)).fetchall()
        previous=''
        for expected,(kind,key,payload,stored_hash,link,ordinal) in enumerate(rows,1):
            envelope={'protocol_id':protocol,'kind':kind,'key':key,'ordinal':ordinal,'previous':link,'payload':json.loads(payload)}
            if ordinal!=expected or link!=previous or digest(envelope)!=stored_hash:
                raise ValueError('Evidence hash-chain verification failed')
            previous=stored_hash
        return {'records':len(rows),'head_hash':previous,'verified':True,
                'limitation':'Integrity chain is not an external timestamp or protection against a database administrator rewriting the entire chain.'}


def build_report(store, protocol):
    from validation_engine import scoreboard
    from validation_stats import candidate_outcomes,market_diagnostics
    from validation_execution import reconcile
    spec=store.get(protocol,'protocol','registered')
    days=store.read(protocol,'day')
    report=scoreboard(spec,days)
    outcomes=candidate_outcomes(store.read(protocol,'snapshot'),days)
    # Owner summary deliberately excludes private per-ticker decisions and events.
    report['candidate_comparison']={k:v for k,v in outcomes.items() if k!='events'}
    report['assessments']=store.read(protocol,'assessment')
    executions=reconcile(store.read(protocol,'executions'),days,spec['primary_cost_bps'])
    report['execution_quality']={k:v for k,v in executions.items() if k!='rows'}
    report['execution_quality']['matched_paper_orders']=sum(r['actual_quantity']>0 for r in executions['rows'])
    if report['assessments']:report['verdict']=report['assessments'][-1]['verdict']
    report['integrity']=store.audit(protocol)
    report['operational_incidents']=store.read(protocol,'incident')
    report['registration']= {k:spec[k] for k in ('registered_at','starts_on','complete_policy_id','success_gates')}
    report['initial_capital']=spec['initial_capital']
    primary=str(spec['primary_cost_bps'])
    benchmark=[d['books'][primary]['SPY']['equity'] for d in days]
    report['market_diagnostics']={r['arm']:market_diagnostics(
        [d['books'][primary][r['arm']]['equity'] for d in days],benchmark,spec['initial_capital'])
        for r in report['rows'] if r['cost_bps']==spec['primary_cost_bps'] and r['status']!='incomplete'} if days else {}
    row_map={r['arm']:r for r in report['rows'] if r['cost_bps']==spec['primary_cost_bps']}
    report['comparison_diagnostics']=[]
    for a,b,label in (('full_system','SPY','Primary market comparison'),
            ('selected_passive','broad_passive','Standardized selection comparison'),
            ('technical_timing','broad_passive','Technical timing comparison'),
            ('full_system','selected_passive','Timing, sizing and rebalance differences')):
        x=row_map.get(a,{}).get('return_pct');y=row_map.get(b,{}).get('return_pct')
        report['comparison_diagnostics'].append({'label':label,'portfolio':a,'control':b,
            'return_difference_pp':x-y if x is not None and y is not None else None,
            'basis':'Descriptive portfolio comparison; these differences are not an additive causal attribution.'})
    report['concentration']={}
    if days:
        for a,p in days[-1]['books'][primary].items():
            weights={s:q*days[-1]['raw_bars'][s]['Close']/p['equity'] for s,q in p['shares'].items()}
            sectors={}
            for s,w in weights.items():
                sector=spec.get('sectors',{}).get(s,'Unclassified')
                sectors[sector]=sectors.get(sector,0.)+w*100
            report['concentration'][a]={'largest_position_pct':max(weights.values(),default=0)*100,
                'sector_exposure_pct':sectors,'cash_pct':100*(1-p['exposure'])}
    report['equity_curves']=[{'session':d['session'],**{a:p['equity'] for a,p in d['books'][str(spec['primary_cost_bps'])].items()}} for d in days]
    return report
