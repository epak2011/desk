"""Refresh independent shadow diagnostics without modifying frozen worker sources."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend_layer as backend
import macro_reconciliation as audit
import ism_data


def dated_unemployment():
    import csv
    import io
    import ssl
    import urllib.request
    import certifi
    with urllib.request.urlopen('https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE', timeout=20,
                                context=ssl.create_default_context(cafile=certifi.where())) as response:
        rows = list(csv.DictReader(io.StringIO(response.read().decode())))
    return [{'date': r['observation_date'], 'value': float(r['UNRATE'])} for r in rows if r['UNRATE'] not in ('', '.')][-3:]


def enrich(macro, ism, unemp):
    """Only pair current source dates with saved levels when both levels agree."""
    m = dict(macro)
    if len(ism) >= 2 and m.get('ism') == ism[-1]['value']:
        m.update(ism_previous=ism[-2]['value'], ism_period=ism[-1]['date'], ism_previous_period=ism[-2]['date'])
    if len(unemp) >= 3 and m.get('unemployment') == unemp[-1]['value'] and m.get('unemployment_previous') == unemp[-2]['value']:
        m.update(unemployment_two_months_ago=unemp[-3]['value'], unemployment_periods=[r['date'] for r in reversed(unemp)])
    return m


def main():
    saved = backend.read_json_table('market_regime_daily', limit=1)
    if not saved:
        raise RuntimeError('No saved market regime; nothing changed.')
    snapshot = next(iter(saved.values()))
    try:
        unemp = dated_unemployment()
    except Exception:
        unemp = []
    ism = ism_data.manufacturing_pmi_rows()
    m = enrich(snapshot.get('macro') or {}, ism, unemp)
    result = audit.evaluate(m)
    result['compared_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    result['input_periods'] = {k: m.get(k) for k in ('ism_period', 'ism_previous_period', 'unemployment_periods')}
    result['comparison_basis'] = 'Current source observations matched to saved levels; diagnostic only, not a historical vintage replay.'
    with backend.db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('CREATE TABLE IF NOT EXISTS macro_regime_audits (snapshot_key TEXT PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())')
            cur.execute('INSERT INTO macro_regime_audits (snapshot_key, payload) VALUES (%s, %s::jsonb) ON CONFLICT (snapshot_key) DO UPDATE SET payload=EXCLUDED.payload, updated_at=NOW()', (audit.snapshot_key(snapshot), json.dumps(result)))
        conn.commit()
    print(json.dumps({'mode': 'SHADOW', 't1': result['t1'], 't2': result['t2'], 't3': result['t3'], 'compared_at': result['compared_at'], 'production_snapshot_changed': False}))


if __name__ == '__main__':
    main()
