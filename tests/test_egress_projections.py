import unittest
from contextlib import contextmanager
from unittest.mock import patch
import backend_layer as b

class Cursor:
    def __init__(self): self.query = ''
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, query, params): self.query = query; self.params = params
    def fetchall(self): return []

class ProjectionTests(unittest.TestCase):
    def query(self, call):
        cursor = Cursor()
        class Connection:
            def cursor(self): return cursor
        @contextmanager
        def connect(): yield Connection()
        with patch.object(b, 'has_database', return_value=True), patch.object(b, 'ensure_backend_schema'), patch.object(b, 'db_connection', connect):
            call()
        return cursor.query

    def test_maintenance_report_does_not_select_full_payload(self):
        query = self.query(lambda: b.read_json_table_many('research_reports', ['AAPL'], maintenance=True))
        self.assertIn('jsonb_build_object', query)
        self.assertIn('_maintenance_incomplete', query)
        self.assertIn("'\"\"'::jsonb", query)
        self.assertNotIn("''::jsonb", query)
        self.assertNotIn('ticker, payload,', query)
        self.assertIn('WHERE ticker = ANY', query)

    def test_normal_report_read_is_unchanged(self):
        query = self.query(lambda: b.read_json_table_many('research_reports', ['AAPL']))
        self.assertIn('ticker, payload, source', query)
        self.assertNotIn('_maintenance_incomplete', query)

    def test_summary_keeps_errors_without_full_result(self):
        query = self.query(lambda: b.latest_jobs(limit=100, summary=True))
        self.assertIn("jsonb_build_object('errors', result->'errors')", query)
        self.assertNotIn('status, result,', query)

    def test_detail_result_remains_available(self):
        query = self.query(lambda: b.latest_jobs(limit=8))
        self.assertIn('status, result,', query)
