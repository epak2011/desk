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
    return copy.deepcopy({k: v for k, v in data.items() if k != 'results'})


def detail(strategy_id: str, *, study: str = 'restricted', window: str = '2019-2025', funding: str = 'monthly', fill: str = 'open') -> dict:
    data = _saved()
    key = f'{study}:{strategy_id}:{window}:{funding}:{fill}'
    if key not in data['results']:
        raise ResearchScopeNotFound('No saved result exists for this exact strategy and scope.')
    strategy = next(s for s in data['strategies'] if s['id'] == strategy_id)
    return copy.deepcopy({'schema_version': 1, 'as_of': data['as_of'], 'artifact_sha256': data['artifact_sha256'], 'research_only': True, 'eligible_to_implement': False, 'strategy': strategy, 'result': data['results'][key]})
