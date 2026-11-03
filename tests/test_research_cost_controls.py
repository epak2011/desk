import unittest
from unittest.mock import MagicMock, patch

import backend_layer as backend
import pm_view
import worker


class ResearchCostControlTests(unittest.TestCase):
    def test_fast_config_cannot_silently_escalate_to_sonnet(self):
        client = MagicMock()
        with patch.object(pm_view, 'CLAUDE_FAST_MODEL_FALLBACKS', ['claude-sonnet-test']):
            with self.assertRaises(RuntimeError):
                pm_view._messages_create_fast(client, max_tokens=20)
        client.messages.create.assert_not_called()

    def test_standard_call_defaults_to_haiku_but_explicit_opt_in_uses_sonnet(self):
        client = MagicMock()
        with patch.object(pm_view, 'CLAUDE_FAST_MODEL_FALLBACKS', ['claude-haiku-test']), \
             patch.object(pm_view, 'CLAUDE_MODEL_FALLBACKS', ['claude-sonnet-test']):
            pm_view._messages_create(client, max_tokens=20)
            self.assertEqual(client.messages.create.call_args.kwargs['model'], 'claude-haiku-test')
            pm_view._messages_create(client, max_tokens=20, allow_sonnet=True)
            self.assertEqual(client.messages.create.call_args.kwargs['model'], 'claude-sonnet-test')
            self.assertNotIn('allow_sonnet', client.messages.create.call_args.kwargs)

    def test_worker_stops_before_any_generation_when_budget_denied(self):
        with patch.object(worker, '_api_key', return_value='test'), \
             patch.object(backend, 'reserve_research_generation', return_value={'allowed': False, 'reason': 'limit'}), \
             patch.object(worker, '_fresh_tactical_state') as market, \
             patch.object(worker, 'get_pm_view') as pm, \
             patch.object(worker, 'get_decision_dossier') as dossier:
            self.assertTrue(worker.refresh_full_report('TEST')['skipped'])
        market.assert_not_called()
        pm.assert_not_called()
        dossier.assert_not_called()

    def test_fast_dossier_does_not_generate_a_second_opinion(self):
        with patch('anthropic.Anthropic'), \
             patch.object(pm_view, '_generate_independent_assessment') as second, \
             patch.object(pm_view, '_call_with_timeout', side_effect=RuntimeError('stop before API')):
            pm_view.get_decision_dossier('TEST', {}, [], {}, {}, api_key='test', fast=True)
        second.assert_not_called()

    def test_quote_changes_do_not_invalidate_business_research(self):
        profile = {"market_cap": 100, "forward_pe": 20, "peg": 2,
                   "debt_to_equity": 30, "earnings_growth": .1, "revenue_growth": .2}
        changed_quote = {**profile, "market_cap": 110, "forward_pe": 22, "peg": 2.2}
        self.assertEqual(worker._fundamentals_fingerprint(profile), worker._fundamentals_fingerprint(changed_quote))
        self.assertNotEqual(worker._fundamentals_fingerprint(profile),
                            worker._fundamentals_fingerprint({**profile, "revenue_growth": .3}))

    def reserve(self, rows, ticker='TEST', **env):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = rows
        with patch.object(backend, 'db_connection') as db, patch.dict('os.environ', env):
            db.return_value.__enter__.return_value = conn
            result = backend.reserve_research_generation(ticker)
        return result, cur, conn

    def test_daily_limit_blocks_even_a_different_stock_without_inserting(self):
        result, cur, conn = self.reserve([(6,)], RESEARCH_DAILY_REPORT_LIMIT='6')
        self.assertFalse(result['allowed'])
        self.assertFalse(any('INSERT INTO' in c.args[0] for c in cur.execute.call_args_list))
        conn.transaction.assert_called_once()
        self.assertIn('pg_advisory_xact_lock', cur.execute.call_args_list[0].args[0])

    def test_cooldown_blocks_a_recent_stock_without_inserting(self):
        result, cur, _ = self.reserve([(1,), (1,)], RESEARCH_COOLDOWN_HOURS='24')
        self.assertFalse(result['allowed'])
        self.assertFalse(any('INSERT INTO' in c.args[0] for c in cur.execute.call_args_list))

    def test_allowed_attempt_gets_a_durable_reservation(self):
        result, cur, _ = self.reserve([(1,), None])
        self.assertTrue(result['allowed'])
        self.assertIn('INSERT INTO', cur.execute.call_args.args[0])
        self.assertEqual(cur.execute.call_args.args[1], ('TEST',))

    def test_zero_daily_budget_disables_generation(self):
        result, _, _ = self.reserve([(0,)], RESEARCH_DAILY_REPORT_LIMIT='0')
        self.assertFalse(result['allowed'])

    def test_queue_budget_snapshot_does_not_reserve_a_slot(self):
        result, cur, _ = self.reserve([(2,)], ticker=None, RESEARCH_DAILY_REPORT_LIMIT='6')
        self.assertEqual(result['remaining'], 4)
        self.assertFalse(any('INSERT INTO' in c.args[0] for c in cur.execute.call_args_list))


if __name__ == '__main__':
    unittest.main()
