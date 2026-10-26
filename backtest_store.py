"""Separate, owner-only historical replay summaries; never overwrite live scores."""
from datetime import datetime, timezone


def publish(report, backend):
    archive_only = (report.get("assumptions") or {}).get("exit_policy") == "legacy_14_session"
    summary = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "assumptions": report["assumptions"], "versions": report["versions"],
               "validation_start": report["validation_start"], "limitations": report["limitations"],
               "sample_start": report.get("sample_start"), "sample_end": report.get("sample_end"),
               "universe": report.get("universe", []),
               "experiment": report.get("experiment", "forced 14-session holding-period experiment"),
               "strategy_manifests": report.get("strategy_manifests", {}),
               "version_aliases": report.get("version_aliases", {}),
               "code_sha256": report.get("code_sha256", {}), "results": {}}
    for period, results in report["results"].items():
        summary["results"][period] = {
            version: {k: v for k, v in values.items() if k != "trades"}
            for version, values in results.items()}
    backend.ensure_backend_schema()
    with backend.db_connection() as conn:
        with conn.cursor() as cur:
            # Retain each exact experiment independently of the latest pointer.
            import hashlib
            identity = hashlib.sha256(backend.json_dumps(summary).encode()).hexdigest()
            cur.execute("""INSERT INTO engine_review_status (key, payload, updated_at)
                           VALUES (%s, %s::jsonb, NOW()) ON CONFLICT (key) DO NOTHING""",
                        ("historical_replay:" + identity, backend.json_dumps(summary)))
            if archive_only:
                # Preserve reproducibility without making an obsolete forced
                # sale experiment the active Streamlit/Lovable report.
                return
            cur.execute("""INSERT INTO engine_review_status (key, payload, updated_at)
                           VALUES ('historical_replay', %s::jsonb, NOW())
                           ON CONFLICT (key) DO UPDATE SET payload=EXCLUDED.payload, updated_at=NOW()""",
                        (backend.json_dumps(summary),))


def read(backend):
    if not backend.has_database():
        return None
    with backend.db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM engine_review_status WHERE key = 'historical_replay'")
            row = cur.fetchone()
    report = dict(row[0]) if row and isinstance(row[0], dict) else None
    if report and (report.get("assumptions") or {}).get("exit_policy") == "signal_invalidation":
        # Older corrected reports serialized unused legacy defaults such as
        # holding_sessions=14. Normalize display only; do not rewrite archives
        # or returns and do not mislabel actual legacy experiments.
        assumptions = dict(report["assumptions"])
        for field in ("holding_sessions", "target_r", "trail_r"):
            assumptions.pop(field, None)
        assumptions.update(calendar_exit_enabled=False, performance_checkpoints_sessions=[5, 14, 30])
        report["assumptions"] = assumptions
    return report
