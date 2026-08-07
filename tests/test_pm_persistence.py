import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import backend_layer


class _FakeCursor:
    def __init__(self, *, mismatch=False):
        self.mismatch = mismatch
        self.payload = None
        self.source = None
        self.operation = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params):
        if "INSERT INTO pm_memos" in query:
            self.operation = "insert"
            self.payload = json.loads(params[1])
            self.source = params[2]
        elif "SELECT payload, source, updated_at" in query:
            self.operation = "select"

    def fetchone(self):
        payload = dict(self.payload or {})
        if self.operation == "select" and self.mismatch:
            payload["_revision_id"] = "different-revision"
        return payload, self.source, datetime.now(timezone.utc)


class _FakeConnection:
    def __init__(self, *, mismatch=False):
        self.cursor_instance = _FakeCursor(mismatch=mismatch)

    def cursor(self):
        return self.cursor_instance


class PmPersistenceTests(unittest.TestCase):
    def _connection(self, *, mismatch=False):
        @contextmanager
        def fake_connection():
            yield _FakeConnection(mismatch=mismatch)

        return fake_connection

    def test_blank_memo_cannot_overwrite_durable_row(self):
        with self.assertRaisesRegex(ValueError, "no durable thesis"):
            backend_layer.validate_pm_memo_payload({"thesis": ""}, "NVDA")
        with self.assertRaisesRegex(ValueError, "no durable thesis"):
            backend_layer.validate_pm_memo_payload(
                {"thesis": "No generated PM thesis yet for NVDA"},
                "NVDA",
            )

    def test_upsert_returns_only_database_verified_revision(self):
        with (
            mock.patch.object(backend_layer, "ensure_backend_schema"),
            mock.patch.object(
                backend_layer,
                "db_connection",
                self._connection(),
            ),
        ):
            saved = backend_layer.upsert_pm_memo(
                "nvda",
                {"thesis": "A durable thesis", "drivers": ["Demand"]},
                source="claude",
            )

        self.assertEqual(saved["thesis"], "A durable thesis")
        self.assertEqual(saved["_source"], "claude")
        self.assertTrue(saved["_revision_id"])
        self.assertTrue(saved["updated_at"])

    def test_revision_mismatch_is_a_failed_save(self):
        with (
            mock.patch.object(backend_layer, "ensure_backend_schema"),
            mock.patch.object(
                backend_layer,
                "db_connection",
                self._connection(mismatch=True),
            ),
            self.assertRaisesRegex(RuntimeError, "read-back revision mismatch"),
        ):
            backend_layer.upsert_pm_memo(
                "NVDA",
                {"thesis": "This must not be presented as saved"},
                source="claude",
            )

    def test_generic_writer_cannot_bypass_pm_persistence_contract(self):
        with self.assertRaisesRegex(ValueError, "Unsupported table"):
            backend_layer.upsert_json_table(
                "pm_memos",
                "ticker",
                "NVDA",
                {"thesis": "This path must remain unavailable"},
            )

    def test_full_report_worker_does_not_write_pm_memo_table(self):
        worker_source = (Path(__file__).resolve().parents[1] / "worker.py").read_text()
        full_report_body = worker_source.split("def refresh_full_report", 1)[1].split(
            "def refresh_watchlist_market_scan", 1
        )[0]
        self.assertNotIn("upsert_pm_memo", full_report_body)
        self.assertIn('upsert_json_table("research_reports"', full_report_body)

    def test_app_does_not_clear_or_replace_pm_memo_during_initialization(self):
        app_source = (Path(__file__).resolve().parents[1] / "app.py").read_text()
        clear_body = app_source.split("def clear_pm_cache", 1)[1].split(
            "def get_cached_dossier", 1
        )[0]
        dossier_hydration = app_source.split("def hydrate_dossier_cache_from_backend", 1)[1].split(
            "def get_cached_pm", 1
        )[0]
        dossier_generation = app_source.split("def get_cached_dossier", 1)[1].split(
            "def dossier_is_stale", 1
        )[0]

        self.assertNotIn('store["pm_cache"]', clear_body)
        self.assertNotIn("snapshot.pop", clear_body)
        self.assertNotIn("merge_ticker_snapshot", dossier_hydration)
        self.assertNotIn("merge_ticker_snapshot(ticker, pm_entry", dossier_generation)

    def test_manual_pm_refresh_triggers_inline_generation(self):
        app_source = (Path(__file__).resolve().parents[1] / "app.py").read_text()
        refresh_body = app_source.split("def refresh_current_ticker_state", 1)[1].split(
            "def queue_full_report_refresh", 1
        )[0]
        analyze_body = app_source.split('if view == "analyze":', 1)[1].split(
            "# ─────────────────────────────────────────────────────────────────────\n"
            "# Footer",
            1,
        )[0]

        self.assertIn('st.session_state["_force_pm_refresh_ticker"] = refresh_ticker', refresh_body)
        self.assertIn('st.session_state.setdefault("_pending_pm_refreshes", {})', refresh_body)
        self.assertIn("allow_generate=allow_pm_generate", analyze_body)
        self.assertIn("force_generate=force_pm_refresh", analyze_body)


if __name__ == "__main__":
    unittest.main()
