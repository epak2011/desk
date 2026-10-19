"""Freeze a future-only entry study. Never backdate or overwrite a registry file."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import research_protocol

SOURCES = ("tactical.py", "engine_candidates.py", "research_protocol.py", "research_event_store.py",
           "decision_contract.py", "position_policy.py", "worker.py")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--starts-on", required=True)
    parser.add_argument("--tickers", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    spec = research_protocol.register_experiment(
        source_hashes={name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES},
        universe=args.tickers.upper().split(","), starts_on=args.starts_on,
        assumptions={"arms": ["event_early", "event_confirmed"], "cost_bps_per_side": 10,
                     "exit_policy": "signal_invalidation", "confirmation_closes_after_event": 2,
                     "sizing": "equal-capital sleeve research only; discretionary PM choices not inferred"},
        registered_at=datetime.now(timezone.utc).isoformat())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        json.dump(spec, output, indent=2, allow_nan=False)
        output.write("\n")
    if args.publish:
        import backend_layer as backend
        backend.ensure_backend_schema()
        with backend.db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO engine_review_status (key,payload,updated_at)
                    VALUES (%s,%s::jsonb,NOW()) ON CONFLICT (key) DO NOTHING""",
                    ("research_registration:"+spec["experiment_id"], backend.json_dumps(spec)))
    print("Frozen prospective specification:", spec["experiment_id"])


if __name__ == "__main__":
    main()
