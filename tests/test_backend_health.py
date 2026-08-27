import unittest
from contextlib import contextmanager
from unittest import mock

import backend_layer


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query):
        self.query = query

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cursor(self.rows)


class AuthSchemaHealthTests(unittest.TestCase):
    def test_health_reads_rls_without_running_schema_bootstrap(self):
        @contextmanager
        def connection():
            yield _Connection(
                [("user_app_state", True), ("notification_outbox", True)]
            )

        with (
            mock.patch.object(backend_layer, "db_connection", connection),
            mock.patch.object(backend_layer, "ensure_backend_schema") as ensure,
        ):
            result = backend_layer.auth_schema_health()

        self.assertEqual(
            result,
            {"user_app_state": True, "notification_outbox": True},
        )
        ensure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
