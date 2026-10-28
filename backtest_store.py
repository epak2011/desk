"""Separate, owner-only historical replay summaries; never overwrite live scores."""
from datetime import datetime, timezone


def publish(report, backend, *, archive_only=False, series="current"):
    if series not in {"current", "prior_years"}:
        raise ValueError("Unknown replay series.")
    archive_only = archive_only or (report.get("assumptions") or {}).get("exit_policy") == "legacy_14_session"
    summary = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "assumptions": report["assumptions"], "versions": report["versions"],
               "validation_start": report["validation_start"], "limitations": report["limitations"],
               "sample_start": report.get("sample_start"), "sample_end": report.get("sample_end"),
               "universe": report.get("universe", []),
               "experiment": report.get("experiment", "forced 14-session holding-period experiment"),
               "strategy_manifests": report.get("strategy_manifests", {}),
               "version_aliases": report.get("version_aliases", {}),
               "code_sha256": report.get("code_sha256", {}), "results": {}}
    summary["evidence_type"] = "retrospective_technical_simulation"
    summary["benchmark_symbols"] = report.get("benchmark_symbols", ["SPY"])
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
            if series == "prior_years":
                cur.execute("""INSERT INTO engine_review_status (key, payload, updated_at)
                    VALUES ('historical_replay_prior_years', %s::jsonb, NOW())
                    ON CONFLICT (key) DO UPDATE SET payload=EXCLUDED.payload, updated_at=NOW()""",
                    (backend.json_dumps(summary),))
                return
            cur.execute("""INSERT INTO engine_review_status (key, payload, updated_at)
                           VALUES ('historical_replay', %s::jsonb, NOW())
                           ON CONFLICT (key) DO UPDATE SET payload=EXCLUDED.payload, updated_at=NOW()""",
                        (backend.json_dumps(summary),))


def read(backend, *, series="current"):
    if series not in {"current", "prior_years"}:
        raise ValueError("Unknown replay series.")
    if not backend.has_database():
        return None
    with backend.db_connection() as conn:
        with conn.cursor() as cur:
            key = "historical_replay" if series == "current" else "historical_replay_prior_years"
            cur.execute("SELECT payload FROM engine_review_status WHERE key = %s", (key,))
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
        report["calendar_comparison"] = calendar_comparison(report)
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
                     "role": "current_technical_control" if version == "original" else "unpromoted_shadow_candidate",
                     "return_pct": values.get("total_return_pct"),
                     "max_drawdown_pct": values.get("max_drawdown_pct"),
                     "closed_trades": values.get("closed_trade_count"),
                     "open_position_valuations": values.get("sample_boundary_valuation_count"),
                     "exposure_pct": values.get("exposure_pct"),
                     "calendar_year_returns_pct": values.get("calendar_year_returns_pct") or {},
                     "excess_vs_spy_pp": _excess(values.get("total_return_pct"), validation, "spy"),
                     "excess_vs_qqq_pp": _excess(values.get("total_return_pct"), validation, "qqq")})
    return {"basis": "archived_technical_portfolio_replay_not_forward_signal_evidence",
            "period_start": report.get("validation_start"), "period_end": report.get("sample_end"),
            "universe": report.get("universe") or [], "exit_policy": (report.get("assumptions") or {}).get("exit_policy"),
            "rows": rows, "benchmarks": validation.get("benchmarks") or {},
            "qqq_comparison_available": (validation.get("benchmarks") or {}).get("qqq_buy_hold_return_pct") is not None,
            "new_forward_results": False,
            "caveat": "Open positions contribute to portfolio value; closed-trade win rates exclude them. This is not a complete discretionary PM simulation."}


def _excess(value, validation, symbol):
    benchmark = (validation.get("benchmarks") or {}).get(f"{symbol}_buy_hold_return_pct")
    return value - benchmark if value is not None and benchmark is not None else None


def calendar_comparison(report):
    rows = []
    for period, values in (report.get("results") or {}).items():
        benchmarks = values.get("benchmarks") or {}
        for version, result in values.items():
            if version == "benchmarks" or not isinstance(result, dict):
                continue
            for year, annual in (result.get("calendar_year_returns_pct") or {}).items():
                spy = (benchmarks.get("spy_calendar_year_returns_pct") or {}).get(year, {}).get("return_pct")
                qqq = (benchmarks.get("qqq_calendar_year_returns_pct") or {}).get(year, {}).get("return_pct")
                rows.append({"period": period, "year": year, "version": version, **annual,
                    "spy_return_pct": spy, "qqq_return_pct": qqq,
                    "same_universe_return_pct": (benchmarks.get("same_universe_calendar_year_returns_pct") or {}).get(year, {}).get("return_pct"),
                    "excess_vs_spy_pp": annual["return_pct"] - spy if spy is not None else None,
                    "excess_vs_qqq_pp": annual["return_pct"] - qqq if qqq is not None else None})
    return rows
