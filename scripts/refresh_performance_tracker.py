"""Publish dated tracking metadata after collection, without altering frozen evidence."""
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import backend_layer as backend
import performance_tracker


def main():
    report=performance_tracker.read(backend)
    # The endpoint computes freshness at read time; this small saved receipt is
    # an operational audit marker, not a duplicate live ledger or performance.
    receipt={k:report[k] for k in ('generated_at','version','freshness','milestones')}
    with backend.db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO engine_review_status(key,payload,updated_at) VALUES ('performance_tracker_refresh',%s::jsonb,NOW()) ON CONFLICT(key) DO UPDATE SET payload=EXCLUDED.payload,updated_at=NOW()",(backend.json_dumps(receipt),))
    print(json.dumps(receipt,indent=2,allow_nan=False))


if __name__=='__main__':main()
