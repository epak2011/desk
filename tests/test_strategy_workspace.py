import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import api_service
import strategy_research as research

class StrategyWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api_service.app)

    def test_comparison_is_one_complete_scope_without_private_store(self):
        with patch.object(api_service.api_repository, 'get_store', side_effect=AssertionError('private access'), create=True):
            response = self.client.get('/v1/strategies/compare?funding=lump_sum&window=2007-2009&fill=close')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['eligible_to_implement'])
        self.assertEqual(len(data['comparisons']), 6)
        self.assertEqual(len({d['strategy']['id'] for d in data['comparisons']}), 6)
        self.assertEqual(len({d['result']['spy_final_value'] for d in data['comparisons']}), 1)
        for d in data['comparisons']:
            self.assertEqual(d['artifact_sha256'], data['artifact_sha256'])
            self.assertEqual(d['result']['scope']['funding'], 'lump_sum')
            self.assertEqual(d['result']['scope']['start'], 2007)
            self.assertEqual(d['result']['scope']['fill'], 'close')

    def test_every_saved_balance_series_reconciles_to_existing_totals(self):
        for r in research.load_artifact()['results'].values():
            rows = r['year_end_balances']; s = r['scope']
            self.assertEqual([x['year'] for x in rows], list(range(s['start'], s['end']+1)))
            for result_key, balance_key in [('final_value','strategy_value'), ('spy_final_value','spy_value'), ('annual_control_value','annual_control_value')]:
                self.assertAlmostEqual(r[result_key], rows[-1][balance_key], delta=max(1e-6,abs(r[result_key])*1e-12))
            self.assertAlmostEqual(rows[-1]['contributions'], r['contributions'], places=6)

    def test_period_counts_use_only_five_preexisting_nonoverlapping_blocks(self):
        response = self.client.get('/v1/strategies/monthly12_rotate/periods?funding=lump_sum')
        self.assertEqual(response.status_code, 200)
        d = response.json(); blocks = [r for r in d['periods'] if r['fixed_block']]
        self.assertEqual(len(d['periods']), 37)
        self.assertEqual({r['window'] for r in blocks}, {'2000-2004','2005-2009','2010-2014','2015-2019','2020-2024'})
        for c in [d['fixed_blocks_vs_spy'],d['fixed_blocks_vs_annual']]:
            self.assertEqual(c['denominator'], 5)
            self.assertEqual(c['better']+c['same']+c['worse'], 5)
        annual = self.client.get('/v1/strategies/annual12_rotate/periods').json()
        self.assertEqual(annual['fixed_blocks_vs_annual']['same'], 5)

    def test_unknown_scopes_or_missing_artifact_never_show_other_results(self):
        for path in ['/v1/strategies/compare?funding=weekly','/v1/strategies/compare?window=1800-2025','/v1/strategies/no_such_policy/periods','/v1/strategies/monthly12_rotate/periods?study=complete_sp500']:
            self.assertEqual(self.client.get(path).status_code, 404)
        with patch.object(research, '_saved', side_effect=research.ResearchUnavailable('Unavailable')):
            for path in ['/v1/strategies/compare','/v1/strategies/monthly12_rotate/periods']:
                self.assertEqual(self.client.get(path).status_code, 503)

    def test_result_payloads_are_isolated_and_catalog_remains_small(self):
        d = research.compare(); d['comparisons'].clear()
        self.assertEqual(len(research.compare()['comparisons']), 6)
        self.assertNotIn('input_manifest', research.catalog())

    def test_unmatched_spy_control_is_withheld(self):
        original = research.detail
        def damaged(policy, **scope):
            d=original(policy, **scope)
            if policy=='monthly12_rotate': d['result']['spy_final_value']+=1000
            return d
        with patch.object(research,'detail',side_effect=damaged):
            response=self.client.get('/v1/strategies/compare')
        self.assertEqual(response.status_code,503)
        self.assertNotIn('comparisons',response.json())

if __name__=='__main__': unittest.main()
