"""Immutable, public strategy evidence. No database, model, or trading side effects."""
from __future__ import annotations
import copy
import gzip
import hashlib
import json
from functools import lru_cache
from pathlib import Path

ARTIFACTS = Path(__file__).parent / 'research_artifacts'

class ResearchUnavailable(RuntimeError):
    pass

class ResearchScopeNotFound(LookupError):
    pass


def load_artifact(directory: Path = ARTIFACTS) -> dict:
    """Fail closed on damaged artifacts or accidental readiness promotion."""
    try:
        manifest = json.loads((directory / 'strategies-v1.manifest.json').read_text())
        raw = (directory / 'strategies-v1.json.gz').read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest['sha256']:
            raise ValueError('artifact digest mismatch')
        data = json.loads(gzip.decompress(raw))
        if data['schema_version'] != 1 or data['research_only'] is not True or data['eligible_to_implement'] is not False:
            raise ValueError('unapproved research contract')
        if len(data['results']) != manifest['results']:
            raise ValueError('incomplete result set')
        if any(s['eligible_to_implement'] is not False or s['status'] != 'research_only' for s in data['strategies']):
            raise ValueError('unapproved strategy status')
        data['artifact_sha256'] = manifest['sha256']
        return data
    except (OSError, ValueError, KeyError, TypeError, EOFError) as exc:
        raise ResearchUnavailable('Strategy research is unavailable; no substitute results are shown.') from exc


@lru_cache(maxsize=1)
def _saved() -> dict:
    return load_artifact()


def catalog() -> dict:
    data = _saved()
    return copy.deepcopy({k: v for k, v in data.items() if k not in ('results', 'input_manifest')})


def detail(strategy_id: str, *, study: str = 'restricted', window: str = '2019-2025', funding: str = 'monthly', fill: str = 'open') -> dict:
    data = _saved()
    key = f'{study}:{strategy_id}:{window}:{funding}:{fill}'
    if key not in data['results']:
        raise ResearchScopeNotFound('No saved result exists for this exact strategy and scope.')
    strategy = next(s for s in data['strategies'] if s['id'] == strategy_id)
    return copy.deepcopy({'schema_version': 1, 'as_of': data['as_of'], 'artifact_sha256': data['artifact_sha256'], 'research_only': True, 'eligible_to_implement': False, 'strategy': strategy, 'result': data['results'][key]})


def compare(*, study='restricted', window='2019-2025', funding='monthly', fill='open'):
    """One exact scope for every policy; no best-strategy recommendation."""
    data = _saved()
    scope = dict(study=study, window=window, funding=funding, fill=fill)
    results = [detail(s['id'], **scope) for s in data['strategies']]
    reference = results[0]['result']
    for item in results:
        r = item['result']
        for key in ('contributions', 'spy_final_value', 'spy_drawdown_pct'):
            if abs(r[key]-reference[key]) > max(1e-7,abs(reference[key])*1e-12):
                raise ResearchUnavailable('Saved comparison controls do not match; results are withheld.')
    return {'schema_version': 1, 'as_of': data['as_of'], 'artifact_sha256': data['artifact_sha256'], 'research_only': True, 'eligible_to_implement': False, 'scope': scope, 'comparisons': results}


def periods(strategy_id, *, study='restricted', funding='monthly', fill='open'):
    """Report all saved periods and the five pre-existing, nonoverlapping blocks."""
    data = _saved()
    fixed = {'2000-2004','2005-2009','2010-2014','2015-2019','2020-2024'}
    rows = []
    for window in data['windows']:
        d = detail(strategy_id, study=study, window=window, funding=funding, fill=fill)
        r = d['result']; p = r['performance']
        rows.append({'window': window, 'fixed_block': window in fixed, 'final_value': r['final_value'], 'spy_final_value': r['spy_final_value'], 'annual_control_value': r['annual_control_value'], 'annualized_return_pct': p['annualized_return_pct'], 'spy_annualized_return_pct': p['spy_annualized_return_pct'], 'annual_control_annualized_return_pct': p['annual_control_annualized_return_pct'], 'drawdown_pct': r['drawdown_pct'], 'contributions': r['contributions']})
    def counts(group, field):
        gaps = [r['final_value']-r[field] for r in group]
        return {'better': sum(g>1e-6 for g in gaps), 'same': sum(abs(g)<=1e-6 for g in gaps), 'worse': sum(g< -1e-6 for g in gaps), 'denominator': len(gaps)}
    blocks = [r for r in rows if r['fixed_block']]
    return {'schema_version':1, 'as_of':data['as_of'], 'artifact_sha256':data['artifact_sha256'], 'research_only':True, 'eligible_to_implement':False, 'strategy_id':strategy_id, 'scope':{'study':study,'funding':funding,'fill':fill}, 'periods':rows, 'fixed_blocks_vs_spy':counts(blocks,'spy_final_value'), 'fixed_blocks_vs_annual':counts(blocks,'annual_control_value'), 'note':'Five nonoverlapping five-year blocks are shown first. All other saved windows overlap; these are sensitivity comparisons, not independent replications. Counts compare ending values with identical funding and are not percentages of stocks.'}
