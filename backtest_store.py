"""Separate, owner-only historical replay summaries; never overwrite live scores."""
from datetime import datetime, timezone


def publish(report, backend):
    summary = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "assumptions": report["assumptions"], "versions": report["versions"],
               "validation_start": report["validation_start"], "limitations": report["limitations"],
               "code_sha256": report.get("code_sha256", {}), "results": {}}
    for period, results in report["results"].items():
        summary["results"][period] = {
            version: {k: v for k, v in values.items() if k != "trades"}
            for version, values in results.items()}
    backend.ensure_backend_schema()
    with backend.db_connection() as conn:
        with conn.cursor() as cur:
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
    return row[0] if row and isinstance(row[0], dict) else None
