"""Daily prospective paired checkpoint publication; no LLM calls or orders."""
import json
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend_layer as backend
import claude_assessment as study


def main():
    import yfinance as yf
    backend.ensure_backend_schema()
    with backend.db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT payload FROM engine_review_status
                WHERE key LIKE %s AND payload->>'version'=%s
                  AND payload->>'generated_at' >= %s
                ORDER BY updated_at DESC LIMIT 1000""", ("claude_pair:%", study.VERSION, study.PROTOCOL["eligible_issued_from"]))
            pairs = [r[0] for r in cur.fetchall()]
    completed_before = datetime.now(ZoneInfo("America/New_York")).date()
    histories, errors = {}, 0
    symbols = sorted({p["ticker"] for p in pairs} | ({"SPY", "QQQ"} if pairs else set()))
    start = min((p["generated_at"][:10] for p in pairs), default=completed_before.isoformat())
    for symbol in symbols:
        try:
            frame = yf.download(symbol, start=start, end=completed_before.isoformat(),
                auto_adjust=True, progress=False, threads=False, timeout=20)
            if hasattr(frame.columns, "levels"):
                frame.columns = frame.columns.get_level_values(0)
            if frame.empty:
                raise ValueError("Missing history")
            histories[symbol] = frame
        except Exception:
            errors += 1
    with backend.db_connection() as conn:
        with conn.cursor() as cur:
            for pair in pairs:
                if pair.get("measurement_protocol_sha256") != study.PROTOCOL_HASH:
                    errors += 1
                    continue
                try:
                    rows = study.score_checkpoints(pair, histories, completed_before=completed_before)
                except (KeyError, ValueError, TypeError):
                    errors += 1
                    continue
                for row in rows:
                    key = f"claude_checkpoint:{row['assessment_id']}:{row['horizon_sessions']}"
                    cur.execute("""INSERT INTO engine_review_status (key,payload,updated_at)
                        VALUES (%s,%s::jsonb,NOW()) ON CONFLICT (key) DO NOTHING""", (key, backend.json_dumps(row)))
            cur.execute("""SELECT payload FROM engine_review_status
                WHERE key LIKE %s AND payload->>'version'=%s
                ORDER BY updated_at DESC LIMIT 10000""", ("claude_checkpoint:%", study.VERSION))
            all_rows = [r[0] for r in cur.fetchall()]
            cur.execute("""SELECT COUNT(*), COUNT(*) FILTER (WHERE payload->>'generated_at'>=%s)
                FROM engine_review_status WHERE key LIKE %s AND payload->>'version'=%s
                  AND payload->>'measurement_protocol_sha256'=%s""",
                (study.PROTOCOL["eligible_issued_from"], "claude_pair:%", study.VERSION, study.PROTOCOL_HASH))
            captured, eligible = cur.fetchone()
            report = study.summarize(all_rows)
            report.update(tracked_pairs_in_batch=len(pairs), scoring_errors=errors,
                          captured_pair_count=captured, eligible_pair_count=eligible,
                          pair_batch_limit=1000, checkpoint_summary_limit=10000,
                          coverage_limited=len(pairs) >= 1000 or len(all_rows) >= 10000)
            cur.execute("""INSERT INTO engine_review_status (key,payload,updated_at)
                VALUES ('claude_comparison_summary',%s::jsonb,NOW())
                ON CONFLICT (key) DO UPDATE SET payload=EXCLUDED.payload,updated_at=NOW()""", (backend.json_dumps(report),))
    print(json.dumps({"version":study.VERSION,"tracked_pairs_in_batch":len(pairs),
                      "captured_pair_count":captured,"eligible_pair_count":eligible,
                      "checkpoint_count":len(all_rows),"scoring_errors":errors,
                      "published":True,"calendar_sale":False}, allow_nan=False))
    if errors:
        raise SystemExit("Comparison scoring incomplete; see scoring_errors (no fabricated outcomes).")


if __name__ == "__main__":
    main()
