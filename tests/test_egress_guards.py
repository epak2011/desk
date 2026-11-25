import io
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, MagicMock, patch
from urllib.error import HTTPError

import api_auth
import backend_layer as b
import user_state_store as u


class EgressGuardTests(unittest.TestCase):
    def setUp(self):
        u._state_cache.clear()
        b._payload_cache.clear()
        b._db_unavailable_until = 0
        api_auth._auth_unavailable_until = 0

    def tearDown(self):
        self.setUp()

    def test_workspace_version_hit_omits_blob_and_returns_isolated_copy(self):
        c = Mock(); c.connection.info.dsn = 'isolated-test'
        c.fetchone.side_effect = [('v1', {'notes': {'AAPL': 'saved'}}), ('v1', None), ('v2', {'notes': {'AAPL': 'changed'}})]
        first = u.load(c, 'user-a'); first['notes']['AAPL'] = 'mutated'
        self.assertEqual(u.load(c, 'user-a')['notes']['AAPL'], 'saved')
        self.assertEqual(c.execute.call_args.args[1], ('v1', 'user-a'))
        self.assertIn('THEN NULL ELSE value', c.execute.call_args.args[0])
        self.assertEqual(u.load(c, 'user-a')['notes']['AAPL'], 'changed')

    def test_workspace_cache_never_crosses_users_or_databases(self):
        c = Mock(); c.connection.info.dsn = 'db-one'
        c.fetchone.return_value = ('v1', {'notes': 'private'})
        u.load(c, 'a'); u.load(c, 'b')
        self.assertEqual(c.execute.call_args.args[1], (None, 'b'))
        c.connection.info.dsn = 'db-two'
        u.load(c, 'a')
        self.assertEqual(c.execute.call_args.args[1], (None, 'a'))

    def test_deleted_state_drops_cached_private_value(self):
        c = Mock(); c.connection.info.dsn = 'test'
        c.fetchone.side_effect = [('v1', {'watchlist': ['AAPL']}), None]
        u.load(c, 'a'); self.assertIsNone(u.load(c, 'a'))
        self.assertFalse(u._state_cache)

    def test_successful_schema_bootstrap_runs_once_but_failure_can_retry(self):
        with patch.object(b, '_schema_ready_url', None), patch.object(b, 'database_url', return_value='test'), patch.object(b, '_create_backend_schema', side_effect=[RuntimeError('offline'), None]) as create:
            with self.assertRaises(RuntimeError): b.ensure_backend_schema()
            b.ensure_backend_schema(); b.ensure_backend_schema()
            self.assertEqual(create.call_count, 2)

    def test_report_cache_checks_version_and_never_reuses_mutated_copy(self):
        c = Mock(); c.fetchone.side_effect = [('v1',), ('v1',), ('v2',), None]
        conn = MagicMock(); conn.cursor.return_value.__enter__.return_value = c
        @contextmanager
        def connect(): yield conn
        with patch.object(b, 'has_database', return_value=True), patch.object(b, 'database_url', return_value='test'), patch.object(b, 'ensure_backend_schema'), patch.object(b, 'db_connection', connect), patch.object(b, '_read_json_table_uncached', side_effect=[{'AAPL': {'text': 'old'}}, {'AAPL': {'text': 'new'}}]) as read:
            b.read_json_table('research_reports', 'AAPL')['AAPL']['text'] = 'local edit'
            self.assertEqual(b.read_json_table('research_reports', 'AAPL')['AAPL']['text'], 'old')
            self.assertEqual(b.read_json_table('research_reports', 'AAPL')['AAPL']['text'], 'new')
            self.assertEqual(b.read_json_table('research_reports', 'AAPL'), {})
            self.assertEqual(read.call_count, 2)

    def test_database_cooldown_stops_network_attempts(self):
        with patch.object(b.time, 'monotonic', return_value=10), patch.object(b, '_db_unavailable_until', 20), patch.object(b, 'get_pool') as pool:
            with self.assertRaises(b.DatabaseUnavailable):
                with b.db_connection(): pass
            pool.assert_not_called()

    def test_auth_quota_cooldown_does_not_accept_any_identity(self):
        with patch.dict(api_auth.os.environ, {'SUPABASE_URL':'https://demo.supabase.co','SUPABASE_ANON_KEY':'anon'}), patch.object(api_auth.urllib.request, 'urlopen', side_effect=HTTPError('url',402,'quota',{},io.BytesIO(b'{}'))) as network:
            for token in ['first', 'other']:
                with self.assertRaises(api_auth.AuthenticationUnavailable): api_auth.verify_access_token(token)
            self.assertEqual(network.call_count, 1)

    def test_history_reservation_stops_before_transfer_when_budget_is_exhausted(self):
        c = Mock(); c.fetchone.side_effect = [(10_000_000,), ({'days': {b.datetime.now(b.timezone.utc).date().isoformat(): 999_000_000}},)]
        with self.assertRaises(b.HistoryReadBudgetExceeded): b.reserve_history_read(c)
        self.assertFalse(any('INSERT INTO' in call.args[0] for call in c.execute.call_args_list))

    def test_history_reservation_discards_expired_days_and_adds_wire_headroom(self):
        c = Mock(); c.fetchone.side_effect = [(1_000_000,), ({'days': {'2020-01-01': 999_000_000}},)]
        result = b.reserve_history_read(c)
        self.assertEqual(result['reserved_bytes'], 1_101_024)
        self.assertEqual(result['rolling_bytes'], 1_101_024)
