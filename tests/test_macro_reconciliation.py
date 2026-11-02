import unittest
from unittest.mock import patch
import macro_reconciliation as audit
import worker
import api_repository


class MacroComparisonTests(unittest.TestCase):
    def setUp(self):
        self.m = dict(ism=49, ism_previous=51, ism_period='2026-09-01', ism_previous_period='2026-08-01',
                      unemployment=4.2, unemployment_previous=4.1, unemployment_two_months_ago=4.0,
                      unemployment_periods=['2026-09-01', '2026-08-01', '2026-07-01'], hy_oas_bps=475)

    def test_monthly_states_and_no_allocation(self):
        r = audit.evaluate(self.m)
        self.assertEqual((r['t1'], r['t2'], r['t3']), ('WARNING', 'WATCH', 'TAIL_RISK'))
        self.assertEqual(r['mode'], 'SHADOW')
        self.assertIsNone(r['allocation'])
        self.m['ism_previous'] = 48
        self.assertEqual(audit.evaluate(self.m)['t1'], 'FIRING')
        self.m['ism'] = 51
        self.assertEqual(audit.evaluate(self.m)['t1'], 'RECOVERY')

    def test_missing_and_nonconsecutive_months_never_clear(self):
        r = audit.evaluate({})
        self.assertEqual((r['t1'], r['t2'], r['t3']), ('UNKNOWN',)*3)
        self.m['unemployment_periods'][1] = '2026-09-01'
        self.m['ism_previous_period'] = '2026-07-01'
        r = audit.evaluate(self.m)
        self.assertEqual((r['t1'], r['t2']), ('UNKNOWN', 'UNKNOWN'))

    def test_published_tenths_cutoffs_match(self):
        for current in range(38, 52):
            for prior in range(38, 52):
                self.m.update(unemployment=current/10, unemployment_previous=prior/10)
                r = audit.evaluate(self.m)
                self.assertEqual(len(set(r['t2Candidates'].values())), 1)

    def test_acceleration_required_and_live_score_unchanged(self):
        live = worker._streamlit_regime_score(assets={}, macro=self.m)
        self.assertEqual(live['signals']['t2_unemployment'], 'FIRING')
        self.assertEqual(audit.evaluate(self.m)['t2'], 'WATCH')
        self.m.update(unemployment=4.3, unemployment_previous=4.1)
        self.assertEqual(audit.evaluate(self.m)['t2'], 'FIRING')
        self.assertEqual(worker._streamlit_regime_score(assets={}, macro=self.m)['score'], live['score'])

    def test_credit_boundaries(self):
        for value, state in [(449, 'CLEAR'), (450, 'EARLY_WARNING'), (475, 'TAIL_RISK'), (600, 'CRISIS')]:
            self.assertEqual(audit.evaluate({'hy_oas_bps': value})['t3'], state)

    @patch('api_repository.backend_layer.read_json_table')
    def test_old_saved_snapshot_not_mutated_and_input_dates_not_invented(self, read):
        saved = {'macro': {'unemployment': 4.2, 'unemployment_previous': 4.1}, 'score': 2, 'signals': {'t2_unemployment': 'FIRING'}}
        read.return_value = {'2026-11-01': saved}
        response = api_repository.regime()['regime']
        self.assertEqual(response['macro_rule_audit']['t2'], 'UNKNOWN')
        self.assertEqual(response['score'], 2)
        self.assertEqual(response['signals'], saved['signals'])
        self.assertNotIn('macro_rule_audit', saved)


    def test_supplemental_inputs_require_agreement_and_preserve_saved_data(self):
        from scripts.refresh_macro_comparison import enrich
        original = dict(self.m)
        rows = [{'date':'2026-07-01','value':4.0}, {'date':'2026-08-01','value':4.1}, {'date':'2026-09-01','value':4.3}]
        saved = {'ism':49, 'unemployment':4.2, 'unemployment_previous':4.1}
        enriched = enrich(saved, [{'date':'2026-08-01','value':51},{'date':'2026-09-01','value':49}], rows)
        self.assertNotIn('unemployment_periods', enriched)
        self.assertEqual(audit.evaluate(enriched)['t2'], 'UNKNOWN')
        self.assertEqual(saved, {'ism':49, 'unemployment':4.2, 'unemployment_previous':4.1})
        rows[-1]['value'] = 4.2
        self.assertEqual(audit.evaluate(enrich(saved, [], rows))['t2'], 'WATCH')

    def test_supplemental_key_tracks_macro_inputs_not_refresh_timestamp(self):
        a = {'generated_at':'2026-11-01T00:00:00Z', 'macro': self.m}
        b = {**a, 'generated_at':'2026-11-01T01:00:00Z'}
        self.assertEqual(audit.snapshot_key(a), audit.snapshot_key(b))
        b['macro'] = {**self.m, 'unemployment': 4.3}
        self.assertNotEqual(audit.snapshot_key(a), audit.snapshot_key(b))

if __name__ == '__main__':
    unittest.main()
