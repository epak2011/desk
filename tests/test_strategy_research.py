import copy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import api_service
import strategy_research as research

class StrategyResearchTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api_service.app)

    def test_public_catalog_and_exact_default_result_without_database(self):
        with patch.object(api_service.api_repository, 'get_store', side_effect=AssertionError('no private store'), create=True):
            c=self.client.get('/v1/strategies');r=self.client.get('/v1/strategies/monthly12_rotate')
        self.assertEqual(c.status_code,200);self.assertEqual(r.status_code,200)
        self.assertNotIn('results',c.json());self.assertFalse(c.json()['eligible_to_implement'])
        self.assertEqual(c.json()['artifact_sha256'],r.json()['artifact_sha256'])
        self.assertAlmostEqual(r.json()['result']['final_value'],25671.229301,places=5)
        self.assertEqual(r.json()['result']['years_vs_spy']['denominator'],7)
        self.assertNotIn('account_id',str(c.json()))

    def test_all_saved_scopes_have_matched_years_and_zero_costs(self):
        data=research.load_artifact()
        self.assertEqual(len(data['results']),2664)
        for result in data['results'].values():
            scope=result['scope'];n=scope['end']-scope['start']+1
            self.assertEqual(scope['costs_bps'],0)
            self.assertEqual(len(result['annual_results']),n)
            for counts in [result['years_vs_spy'],result['years_vs_annual']]:
                self.assertEqual(counts['denominator'],n)
                self.assertEqual(counts['better']+counts['same']+counts['worse'],n)
            self.assertAlmostEqual(result['contributions'],10000 if scope['funding']=='lump_sum' else n*1000,places=6)
            if scope['funding']=='lump_sum':
                self.assertEqual(result['additional_contributions'],0)
                self.assertAlmostEqual(result['performance']['total_return_pct'],100*(result['final_value']/10000-1),places=6)

    def test_one_time_capital_is_an_exact_saved_scope(self):
        r=self.client.get('/v1/strategies/monthly12_rotate?funding=lump_sum')
        self.assertEqual(r.status_code,200)
        result=r.json()['result']
        self.assertEqual(result['contributions'],10000)
        self.assertEqual(result['additional_contributions'],0)
        self.assertIn('reinvest',result['funding_note'])
        hold=self.client.get('/v1/strategies/monthly12_hold?funding=lump_sum').json()['result']
        annual=self.client.get('/v1/strategies/annual12_hold?funding=lump_sum').json()['result']
        self.assertEqual(hold['actual_full_switches'],0)
        self.assertAlmostEqual(hold['final_value'],annual['final_value'])

    def test_unknown_scope_has_no_nearest_or_default_fallback(self):
        for query in ['?window=1800-2025','?funding=weekly','?study=complete_sp500','?fill=signal_close']:
            response=self.client.get('/v1/strategies/monthly12_rotate'+query)
            self.assertEqual(response.status_code,404)
            self.assertEqual(response.json()['error']['code'],'research_scope_not_found')
        self.assertEqual(self.client.post('/v1/strategies/monthly12_rotate').status_code,405)

    def test_corrupt_or_promoted_evidence_fails_closed(self):
        raw=(research.ARTIFACTS/'strategies-v1.json.gz').read_bytes()
        manifest=json.loads((research.ARTIFACTS/'strategies-v1.manifest.json').read_text())
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'strategies-v1.json.gz').write_bytes(raw+b'bad');(p/'strategies-v1.manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(research.ResearchUnavailable):research.load_artifact(p)
            data=json.loads(gzip.decompress(raw));data['strategies'][0]['eligible_to_implement']=True
            changed=gzip.compress(json.dumps(data).encode());manifest['sha256']=hashlib.sha256(changed).hexdigest()
            (p/'strategies-v1.json.gz').write_bytes(changed);(p/'strategies-v1.manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(research.ResearchUnavailable):research.load_artifact(p)

    def test_unavailable_returns_classified_503(self):
        with patch.object(research,'_saved',side_effect=research.ResearchUnavailable('Saved evidence unavailable.')):
            r=self.client.get('/v1/strategies')
        self.assertEqual(r.status_code,503);self.assertNotIn('strategies',r.json())

    def test_payload_isolation_and_manifest_parity(self):
        c=research.catalog();c['strategies'].clear();self.assertEqual(len(research.catalog()['strategies']),6)
        pages=api_service.api_repository.public_contract.app_manifest_payload()['pages']
        p=next(p for p in pages if p['key']=='strategies')
        self.assertEqual(p['auth'],'public');self.assertEqual(p['endpoint'],'/v1/strategies')
