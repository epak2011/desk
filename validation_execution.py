"""Optional actual-fill import for execution-quality reconciliation, never inferred account returns."""
from validation_engine import number, timestamp


def validate_statement(statement):
    if not statement.get('source') or not statement.get('statement_id'):
        raise ValueError('Broker/export source and immutable statement ID required')
    fills=statement.get('fills')
    if not isinstance(fills,list):raise ValueError('Explicit actual fills required')
    ids=set()
    for f in fills:
        if not f.get('fill_id') or f['fill_id'] in ids:raise ValueError('Unique fill IDs required')
        ids.add(f['fill_id'])
        if f.get('side') not in ('buy','sell') or not f.get('ticker'):raise ValueError('Invalid actual fill')
        number(f.get('quantity'),minimum=1e-12);number(f.get('price'),minimum=1e-12)
        number(f.get('fee',0));timestamp(f['executed_at'])
    return statement


def validate_fill_identity(statement,prior_statements):
    known={(s['source'],f['fill_id']):f for s in prior_statements for f in s['fills']}
    for f in statement['fills']:
        prior=known.get((statement['source'],f['fill_id']))
        if prior and prior!=f:raise ValueError('Actual fill ID conflicts with prior evidence')


def reconcile(statements,days,primary_cost):
    """Match explicit decision IDs and sessions, aggregating partial broker fills by notional."""
    expected={}
    for day in days:
        for f in day['books'][str(primary_cost)]['full_system']['fills']:
            key=(f.get('decision_id'),f['ticker'],f['side'],day['session'])
            group=expected.setdefault(key,{'quantity':0.,'notional':0.,'cost':0.})
            group['quantity']+=f['shares'];group['notional']+=f['shares']*f['price'];group['cost']+=f['cost']
    actual={};seen=set();unmatched=0
    for statement in statements:
        for f in statement['fills']:
            fill_key=(statement['source'],f['fill_id'])
            if fill_key in seen:continue
            seen.add(fill_key)
            day=timestamp(f['executed_at']).astimezone(__import__('validation_engine').ET).date().isoformat()
            key=(f.get('decision_id'),f['ticker'],f['side'],day)
            if not f.get('decision_id') or key not in expected:
                unmatched+=1;continue
            g=actual.setdefault(key,{'quantity':0.,'notional':0.,'fee':0.})
            g['quantity']+=f['quantity'];g['notional']+=f['quantity']*f['price'];g['fee']+=f.get('fee',0)
    rows=[]
    for key,exp in expected.items():
        act=actual.get(key)
        exp_price=exp['notional']/exp['quantity']
        rows.append({'decision_id':key[0],'ticker':key[1],'side':key[2],'session':key[3],
            'expected_quantity':exp['quantity'],'actual_quantity':act['quantity'] if act else 0,
            'expected_price':exp_price,'actual_vwap':act['notional']/act['quantity'] if act else None,
            'adverse_price_slippage_bps':((act['notional']/act['quantity']/exp_price-1)*10000*(1 if key[2]=='buy' else -1)) if act else None,
            'actual_fees':act['fee'] if act else None})
    matched=[r for r in rows if r['actual_quantity']>0]
    notional=sum(r['actual_quantity']*r['expected_price'] for r in matched)
    return {'status':'available' if statements else 'no_actual_fills_imported','rows':rows,
        'matched_actual_notional_at_paper_price':notional,
        'notional_weighted_adverse_price_slippage_bps':sum(r['adverse_price_slippage_bps']*r['actual_quantity']*r['expected_price'] for r in matched)/notional if notional else None,
        'matched_actual_fees':sum(r['actual_fees'] for r in matched),
        'unmatched_actual_fills':unmatched,'unmatched_paper_orders':sum(r['actual_quantity']==0 for r in rows),
        'actual_account_performance':False,
        'basis':'Explicit broker/export fills matched by decision, ticker, side and US session. Price slippage excludes fees; quantity differences remain visible. Not an account-return calculation.'}
