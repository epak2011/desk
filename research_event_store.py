"""Durable completed-session event observations, independent of cached outputs.

Uses the existing operator-status table. A per-ticker row lock serializes workers;
immutable observations are appended while the journal pointer advances. No schema
migration or trading-rule mutation is needed.
"""
from datetime import date, datetime, timezone
import research_protocol


def capture(backend, *, ticker, history, benchmark, compute, fingerprint, completed_before):
    if not backend.has_database():
        return {"status": "unavailable", "reason": "No durable database; event origin not recorded."}
    completed = history.loc[history.index.date < completed_before].sort_index()
    if completed.empty:
        return {"status": "unavailable", "reason": "No completed prior-date session."}
    if completed.index.has_duplicates:
        raise ValueError("Duplicate session observations.")
    key = "entry_event_journal:" + ticker
    backend.ensure_backend_schema()
    with backend.db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO engine_review_status (key,payload,updated_at)
                VALUES (%s,'{}'::jsonb,NOW()) ON CONFLICT (key) DO NOTHING""", (key,))
            cur.execute("SELECT payload FROM engine_review_status WHERE key=%s FOR UPDATE", (key,))
            journal = (cur.fetchone() or [{}])[0] or {}
            # Do not invent pre-registration events or carry another code release's
            # armed level into the new release's prospective population.
            if journal.get("fingerprint") != fingerprint or not journal.get("last_session"):
                journal, pending = {}, completed.tail(1)
            else:
                pending = completed.loc[completed.index.date > date.fromisoformat(journal["last_session"])]
            for session, bar in pending.iterrows():
                state = compute(completed.loc[:session], benchmark.loc[:session]) or {}
                journal = research_protocol.advance_event(journal, ticker=ticker,
                    session=str(session.date()), close=float(bar.Close), state=state, fingerprint=fingerprint)
                journal["observed_at"] = datetime.now(timezone.utc).isoformat()
                event = journal.get("event")
                if event:
                    observation = {"event": event, "observed_at": journal["observed_at"],
                                   "session": str(session.date()), "mode": "observed_completed_daily_bars"}
                    observation_key = "entry_event_observation:" + event["event_id"] + ":" + str(session.date())
                    cur.execute("""INSERT INTO engine_review_status (key,payload,updated_at)
                        VALUES (%s,%s::jsonb,NOW()) ON CONFLICT (key) DO NOTHING""",
                        (observation_key, backend.json_dumps(observation)))
            cur.execute("UPDATE engine_review_status SET payload=%s::jsonb,updated_at=NOW() WHERE key=%s",
                        (backend.json_dumps(journal), key))
    return journal
