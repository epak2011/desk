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
    if report:
        report["comparison_summary"] = comparison_summary(report)
    return report


def comparison_summary(report):
    """Same-period strategy/benchmark returns, separate from signal averages."""
    rows = []
    validation = (report.get("results") or {}).get("validation") or {}
    for version, values in validation.items():
        if version == "benchmarks" or not isinstance(values, dict):
            continue
        manifest = (report.get("strategy_manifests") or {}).get(version) or {}
        rows.append({"version": version, "strategy_id": manifest.get("strategy_id"),
                     "return_pct": values.get("total_return_pct"),
                     "max_drawdown_pct": values.get("max_drawdown_pct"),
                     "closed_trades": values.get("closed_trade_count"),
                     "open_position_valuations": values.get("sample_boundary_valuation_count")})
    return {"basis": "archived_technical_portfolio_replay_not_forward_signal_evidence",
            "period_start": report.get("validation_start"), "period_end": report.get("sample_end"),
            "universe": report.get("universe") or [], "exit_policy": (report.get("assumptions") or {}).get("exit_policy"),
            "rows": rows, "benchmarks": validation.get("benchmarks") or {},
            "new_forward_results": False,
            "caveat": "Open positions contribute to portfolio value; closed-trade win rates exclude them. This is not a complete discretionary PM simulation."}
