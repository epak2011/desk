"""Owner-only aggregate coverage audit; never exports user decisions or ticker lists."""
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend_layer as backend


def main():
    with backend.db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*), COUNT(DISTINCT NULLIF(UPPER(entry->>'ticker'),'')),
                MIN(COALESCE(entry_ts,updated_at)), MAX(COALESCE(entry_ts,updated_at)),
                COUNT(*) FILTER (WHERE entry->'outcome' IS NOT NULL AND entry->'outcome'<>'null'::jsonb)
                FROM decisions_log""")
            total, tickers, first, last, scored = cur.fetchone()
            cur.execute("""SELECT COALESCE(entry->>'rule_engine_version',entry->>'engine_version','unknown'), COUNT(*)
                FROM decisions_log GROUP BY 1""")
            versions = [{'engine_version': row[0], 'records': row[1]} for row in cur.fetchall()]
            report = {'status': 'available', 'generated_at': datetime.now(timezone.utc).isoformat(),
                      'decision_records': total, 'distinct_tickers': tickers, 'scored_records': scored,
                      'first_record_at': str(first) if first else None, 'last_record_at': str(last) if last else None,
                      'versions': versions, 'original_replay_tickers': 8, 'broad_replay_tickers': 110,
                      'reason_for_eight': 'Manually configured smoke-test cohort and separately frozen eight-name event trial, not app-wide coverage.',
                      'basis': 'All durable decisions_log rows, no LIMIT. Counts are coverage, not independent samples or portfolio returns; versions and overlapping calls are not pooled as performance.'}
            cur.execute("INSERT INTO engine_review_status (key,payload,updated_at) VALUES ('performance_coverage',%s::jsonb,NOW()) ON CONFLICT (key) DO UPDATE SET payload=EXCLUDED.payload,updated_at=NOW()", (backend.json_dumps(report),))
    print(json.dumps({k:report[k] for k in ('status','decision_records','distinct_tickers','scored_records','first_record_at','last_record_at')}))

if __name__ == '__main__':
    main()
