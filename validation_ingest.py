"""Owner-only complete-policy intake; authoritative arrival timestamp, never client backdating."""
from validation_engine import digest, validate_plan, timestamp


def record_plan(store,spec,plan,now,window):
    plan=validate_plan(plan,spec['universe'],spec['complete_policy_id'])
    if spec.get('complete_policy_manifest'):
        for section in ('selection','sizing','risk','execution'):
            if plan[section].get('policy_sha256')!=digest(spec['complete_policy_manifest'][section]):
                raise ValueError('Frozen complete-policy section changed: '+section)
    if plan['session']!=window['session']:
        raise ValueError('Receipt/calendar session mismatch')
    if not timestamp(window['market_close_at'])<=timestamp(now)<timestamp(window['next_open_at']):
        raise ValueError('Receipt outside close-to-next-open window')
    if not timestamp(window['market_close_at'])<=timestamp(plan['observed_at'])<=timestamp(now):
        raise ValueError('Observation must follow the completed close and precede arrival')
    existing=store.get(spec['protocol_id'],'plan',plan['session'])
    if existing:
        if digest(existing['plan'])==digest(plan):return {'status':'already_recorded','session':plan['session']}
        raise ValueError('Cannot replace immutable receipt')
    if store.get(spec['protocol_id'],'snapshot',plan['session']):
        raise ValueError('Snapshot already sealed; submit before collection')
    store.append(spec['protocol_id'],'plan',plan['session'],{'received_at':now,'plan':plan})
    return {'status':'receipt_recorded','session':plan['session']}
