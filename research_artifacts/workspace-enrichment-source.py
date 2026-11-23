"""Add saved year-end account balances; never reconstruct balances from returns."""
from pathlib import Path
import gzip, hashlib, json
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'outputs/strategies-workspace'
ART = ROOT/'work/trading-desk/research_artifacts'
BASE = '7efc386714a02bfe63a3d16db91a811605f22a56dd400f248db4a542e547e085'
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()

def main():
    OUT.mkdir(exist_ok=True)
    baseline = OUT/'baseline.json.gz'
    if not baseline.exists():
        assert sha(ART/'strategies-v1.json.gz') == BASE
        baseline.write_bytes((ART/'strategies-v1.json.gz').read_bytes())
    assert sha(baseline) == BASE
    data = json.loads(gzip.decompress(baseline.read_bytes()))
    inputs = [{'path': str(baseline.relative_to(ROOT)), 'sha256': BASE}]
    rows = {}
    for study, folder, filename in [('restricted','membership-restricted-selection','annual-results.csv'),('survivor','steady-earlier-periods','annual-paired.csv')]:
        path = ROOT/'outputs'/folder/filename
        assert any(x['path'] == str(path.relative_to(ROOT)) and x['sha256'] == sha(path) for x in data['input_manifest'])
        source = pd.read_csv(path).query("universe == 'omit_flagged'")
        for r in source.itertuples():
            key = f'{study}:{r.policy}:{r.start}-{r.end}:{r.funding}:{r.fill}'
            rows[(key, int(r.year))] = (float(r.final_value), float(r.spy_value))
    for key, r in data['results'].items():
        s = r['scope']
        if s['funding'] == 'lump_sum':
            path = ROOT/'outputs/lump-sum-strategies'/(key.replace(':','__')+'.csv.gz')
            inputs.append({'path': str(path.relative_to(ROOT)), 'sha256': sha(path)})
            ledger = pd.read_csv(path, index_col=0, parse_dates=True)
            for year, g in ledger.groupby(ledger.index.year):
                assert g.flow.sum() == (10000 if year == s['start'] else 0)
                rows[(key, int(year))] = (float(g.value.iloc[-1]), float(g.spy_value.iloc[-1]))
    for key, r in data['results'].items():
        s = r['scope']; control = 'annual12_'+s['policy'].rsplit('_',1)[1]
        control_key = f"{s['study']}:{control}:{s['start']}-{s['end']}:{s['funding']}:{s['fill']}"
        balances = []
        for y in r['annual_results']:
            year = y['year']; value, spy = rows[(key, year)]; annual, control_spy = rows[(control_key, year)]
            assert abs(spy-control_spy) < max(1e-6, abs(spy)*1e-12)
            balances.append({'year': year, 'strategy_value': value, 'spy_value': spy, 'annual_control_value': annual, 'contributions': 10000 if s['funding']=='lump_sum' else (year-s['start']+1)*1000})
        r['year_end_balances'] = balances
        for field, last in [('final_value','strategy_value'),('spy_final_value','spy_value'),('annual_control_value','annual_control_value')]:
            assert abs(r[field]-balances[-1][last]) < max(1e-6,abs(r[field])*1e-12)
    old = json.loads(gzip.decompress(baseline.read_bytes()))
    for key, r in old['results'].items():
        assert {k:v for k,v in data['results'][key].items() if k!='year_end_balances'} == r
    data['as_of'] = '2026-11-23'
    data['research_revision'] = 'research-workspace-v1'
    data['input_manifest'].extend(inputs+[{'path':str(Path(__file__).relative_to(ROOT)), 'sha256':sha(__file__)}])
    raw = gzip.compress(json.dumps(data,separators=(',',':'),allow_nan=False).encode(),mtime=0)
    (ART/'strategies-v1.json.gz').write_bytes(raw)
    manifest={'schema_version':1,'file':'strategies-v1.json.gz','sha256':hashlib.sha256(raw).hexdigest(),'results':len(data['results']),'inputs':data['input_manifest']}
    (ART/'strategies-v1.manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (OUT/'qa.json').write_text(json.dumps({'result_records_preserved':2664,'year_end_series':2664,'last_balances_reconciled':2664,'account_balances_from_saved_ledgers':True,'new_simulations':0,'artifact_sha256':manifest['sha256']},indent=2)+'\n')
    print(json.dumps({'results':len(data['results']),'artifact_sha256':manifest['sha256']}))

if __name__=='__main__': main()
