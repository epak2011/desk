import csv
import io
import unittest
import performance_tracker as t


class TrackerTests(unittest.TestCase):
    def test_future_start_has_no_invented_return_or_stale_warning(self):
        live={'status':'registered','starts_on':'2026-11-09','completed_sessions':0,'rows':[]}
        report=t.compose(live,now='2026-11-06T04:00:00+00:00')
        self.assertEqual(report['freshness']['status'],'future_start')
        self.assertEqual(report['forward']['rows'],[])
        self.assertEqual(report['milestones']['sessions_to_confirmatory'],504)

    def test_monthly_returns_use_prior_month_equity_not_initial_capital(self):
        curves=[{'session':'2026-01-30','SPY':110.},{'session':'2026-02-02','SPY':121.}]
        rows=t.calendar_rows(curves,100.)
        self.assertAlmostEqual(rows[0]['return_pct'],10.)
        self.assertAlmostEqual(rows[1]['return_pct'],10.)

    def test_incomplete_full_system_does_not_get_a_zero_monthly_score(self):
        live={'status':'partial_coverage','starts_on':'2026-11-06','as_of':'2026-11-06','completed_sessions':1,
            'rows':[{'arm':'full_system','status':'incomplete','return_pct':None},{'arm':'SPY','status':'collecting','return_pct':1.}],
            'initial_capital':100.,'equity_curves':[{'session':'2026-11-06','full_system':100.,'SPY':101.}]}
        report=t.compose(live,now='2026-11-07T04:00:00+00:00')
        self.assertEqual({r['arm'] for r in report['forward']['monthly_scorecard']},{'SPY'})
        self.assertNotIn('monthly_scorecard',live)

    def test_holiday_is_not_a_missing_market_observation(self):
        live={'status':'collecting','starts_on':'2026-11-09','as_of':'2026-11-25'}
        self.assertEqual(t.freshness(live,'2026-11-27T09:00:00+00:00')['status'],'up_to_date')

    def test_overdue_session_is_visible(self):
        live={'status':'collecting','starts_on':'2026-11-09','as_of':'2026-11-09'}
        result=t.freshness(live,'2026-11-11T03:00:00+00:00')
        self.assertEqual(result['status'],'awaiting_complete_collection')
        self.assertEqual(result['expected_through'],'2026-11-10')

    def test_backtest_cannot_be_labeled_forward(self):
        with self.assertRaises(ValueError):t.compose(historical={'evidence_type':'prospective_paper'})

    def test_csv_preserves_unavailable_values_and_evidence_periods(self):
        tracker={'forward':{'protocol_id':'live','starts_on':'2026-11-09','as_of':'2026-11-10',
            'rows':[{'arm':'full_system','status':'incomplete','cost_bps':10,'return_pct':None}]},
            'historical':{'study_id':'history','periods':{'full':{'start':'2021-01-04','end':'2026-11-05',
                'rows':[{'arm':'SPY','cost_bps':10,'return_pct':50.}]}}}}
        rows=list(csv.DictReader(io.StringIO(t.csv_export(tracker))))
        self.assertEqual(rows[0]['return_pct'],'')
        self.assertEqual(rows[1]['evidence'],'retrospective_simulation')
        self.assertEqual(rows[0]['study_or_protocol'],'live')
        self.assertEqual(rows[1]['study_or_protocol'],'history')


if __name__=='__main__':unittest.main()
