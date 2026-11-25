import unittest
from contextlib import contextmanager
from unittest import mock

import backend_layer


class _Cursor:
    def __init__(self, rows, calls):
        self.rows = rows
        self.calls = calls
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        self.calls.append((query, params))

    def fetchone(self):
        return (len(self.rows), "2026-11-25T00:00:00Z")

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, rows, calls):
        self.rows = rows
        self.calls = calls

    def cursor(self):
        return _Cursor(self.rows, self.calls)

    @contextmanager
    def transaction(self):
        yield


class DecisionLogsCacheTests(unittest.TestCase):
    """Guards the fix for the Supabase egress spike: the unbounded
    decisions_log read must not hit Postgres on every call, and a write
    must always invalidate it so a read-after-write sees its own change.
    """

    def setUp(self):
        backend_layer._decision_logs_cache["rows"] = None
        backend_layer._decision_logs_cache["at"] = 0.0
        reservation = mock.patch.object(backend_layer, "reserve_history_read", return_value={})
        reservation.start()
        self.addCleanup(reservation.stop)
        self.calls = []
        self.rows = [({"id": "a"},), ({"id": "b"},)]

        @contextmanager
        def connection():
            yield _Connection(self.rows, self.calls)

        patcher = mock.patch.object(backend_layer, "db_connection", connection)
        patcher.start()
        self.addCleanup(patcher.stop)
        schema_patcher = mock.patch.object(backend_layer, "ensure_backend_schema")
        schema_patcher.start()
        self.addCleanup(schema_patcher.stop)

    def test_repeated_unbounded_reads_hit_postgres_once(self):
        first = backend_layer.read_decision_logs()
        second = backend_layer.read_decision_logs()
        third = backend_layer.read_decision_logs()

        self.assertEqual(len(self.calls), 2)
        self.assertEqual(first, [{"id": "a"}, {"id": "b"}])
        self.assertEqual(second, first)
        self.assertEqual(third, first)

    def test_expired_cache_probes_version_without_downloading_unchanged_history(self):
        backend_layer.read_decision_logs()
        backend_layer._decision_logs_cache["at"] = -1000
        backend_layer.read_decision_logs()
        self.assertEqual(sum("SELECT entry" in sql for sql, _ in self.calls), 1)
        self.assertEqual(sum("COUNT(*)" in sql for sql, _ in self.calls), 2)

    def test_local_mutation_cannot_poison_history_cache(self):
        first = backend_layer.read_decision_logs()
        first[0]['id'] = 'mutated'
        self.assertEqual(backend_layer.read_decision_logs()[0]['id'], 'a')

    def test_limited_reads_are_never_cached(self):
        backend_layer.read_decision_logs(limit=5)
        backend_layer.read_decision_logs(limit=5)

        self.assertEqual(len(self.calls), 2)

    def test_upsert_invalidates_so_read_after_write_sees_the_change(self):
        backend_layer.read_decision_logs()
        self.assertEqual(len(self.calls), 2)

        backend_layer.upsert_decision_log({"id": "c", "ts": "2026-01-01T00:00:00Z"})
        backend_layer.read_decision_logs()

        # One read, one write, one more read: the cache must not mask the write.
        self.assertEqual(len(self.calls), 5)

    def test_import_invalidates_only_when_rows_were_actually_inserted(self):
        backend_layer.read_decision_logs()
        self.assertEqual(len(self.calls), 2)

        # No rows pass the allow-filter (missing auto_logged/source), so
        # nothing is written and the cache should stay warm.
        inserted = backend_layer.import_rule_decision_logs([{"id": "x", "ts": "t"}])
        self.assertEqual(inserted, 0)
        backend_layer.read_decision_logs()
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
