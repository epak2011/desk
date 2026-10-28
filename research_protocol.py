"""Explicit research-only event, execution and prospective-control contracts.

No function places orders or modifies production actions. Event timestamps are
observed completed-session dates, never inferred from a retrospective prose flag.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path

VERSION = "event-study-2026.10-v2"


def active_registration(root=None):
    """An explicit pointer selects an append-only registration, never rewrites it."""
    root = Path(root or Path(__file__).parent)
    pointer = json.loads((root / "experiments/active.json").read_text())
    name = pointer["registration"]
    if not isinstance(name, str) or Path(name).name != name or not name.endswith(".json"):
        raise ValueError("Invalid experiment registration path.")
    return json.loads((root / "experiments" / name).read_text())


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def advance_event(previous, *, ticker, session, close, state, fingerprint):
    """Advance one observed completed close; caller persists returned journal.

    Arm a level BEFORE observing a crossing. Freeze that level/stop/code identity
    for the event. A live level recomputation cannot move the confirmation goal.
    Two held closes must be strictly AFTER the crossing session. Same-session
    retries are idempotent. Caller must supply every consecutive exchange bar;
    missing observations are not evidence. Historical replay does this directly.
    """
    session = date.fromisoformat(str(session)).isoformat()
    close = number(close)
    if close is None or close <= 0 or not fingerprint:
        raise ValueError("Require a positive completed close and source fingerprint.")
    journal = deepcopy(previous or {})
    if journal.get("fingerprint") != fingerprint:
        journal = {}
    if journal.get("last_session") and session <= journal["last_session"]:
        return journal
    event = journal.get("event")
    armed = journal.get("armed")
    if event and event.get("status") == "active":
        stop = event.get("stop")
        if stop is not None and close <= stop:
            event.update(status="invalidated", invalidated_session=session)
        else:
            event["held_closes"] = event.get("held_closes", 0) + 1 if close >= event["level"] else 0
            if event["held_closes"] >= 2 and not event.get("confirmed_session"):
                event["confirmed_session"] = session
    elif armed and journal.get("last_close") is not None:
        if armed.get("stop") is not None and close <= armed["stop"]:
            journal["armed"] = None
        elif session > armed["session"] and journal["last_close"] < armed["level"] <= close:
            origin = {"ticker": ticker, "armed_session": armed["session"],
                      "fired_session": session, "level": armed["level"],
                      "stop": armed.get("stop"), "fingerprint": fingerprint,
                      "protocol": VERSION}
            event = {**origin, "event_id": identity(origin), "status": "active", "held_closes": 0,
                     "confirmed_session": None}
            journal["event"] = event
            journal["armed"] = None
    # Never arm using a level first published after its crossing. Already-fired
    # snapshots are unknown origins, not newly observed events.
    if not journal.get("armed") and not (journal.get("event") or {}).get("status") == "active":
        levels = (state.get("trigger") or {}).get("levels") or {}
        level = number(levels.get("buy_above"))
        stop = number(levels.get("abort_below") or state.get("stop"))
        if level is not None and level > close and stop is not None and 0 < stop < level:
            journal["armed"] = {"session": session, "level": level, "stop": stop}
    journal.update(protocol=VERSION, ticker=ticker, fingerprint=fingerprint,
                   last_session=session, last_close=close)
    return journal


def event_entry(state, confirmed=False):
    """Both arms require the same canonical qualifying action and frozen event.

    The early arm enters on the first eligible signal after observed crossing;
    the delayed arm additionally requires two closes after that crossing.
    Missing historical origins remain unscored, not reconstructed from today.
    """
    action = str(state.get("action") or "").lower()
    if action not in {"enter", "enter_now", "accumulate"}:
        return action
    event = state.get("entry_event") or {}
    if not event.get("event_id") or event.get("status") != "active":
        return "unknown"
    if confirmed and (not event.get("confirmed_session") or event.get("held_closes", 0) < 2):
        return "watch"
    return "enter_now"


def execution_instruction(kind, *, fraction=None, stop=None):
    """Validate explicit discretionary PM choices; NEVER invent trim/sizing.

    Fractions are portions of currently held shares for trims, or the explicitly
    allocated sleeve capital for entry/add. A numeric raised stop is mandatory.
    This contract records instructions; it is not a broker execution API.
    """
    if kind not in {"entry", "add", "trim", "raise_stop", "exit", "hold"}:
        raise ValueError("Unknown instruction.")
    result = {"kind": kind, "executable": kind in {"exit", "hold"}, "missing": []}
    if kind in {"entry", "add", "trim"}:
        fraction = number(fraction)
        if fraction is None or not 0 < fraction <= 1:
            result["missing"].append("explicit_fraction")
        else:
            result["fraction"] = fraction
            result["executable"] = True
    if kind == "raise_stop":
        stop = number(stop)
        if stop is None or stop <= 0:
            result["missing"].append("explicit_stop_price")
        else:
            result.update(stop=stop, executable=True)
    return result


def register_experiment(*, source_hashes, universe, assumptions, starts_on, registered_at=None):
    """Freeze a prospective specification before its first eligible session.

    Publication/storage is append-only by experiment_id. Changing any source,
    universe or assumption creates a new identity and restarts evidence.
    """
    registered = datetime.fromisoformat(registered_at) if registered_at else datetime.now(timezone.utc)
    if registered.tzinfo is None or date.fromisoformat(starts_on) <= registered.date():
        raise ValueError("Prospective start must be after timezone-aware registration date.")
    if not source_hashes or not universe:
        raise ValueError("Require frozen sources and universe.")
    spec = {"protocol": VERSION, "source_hashes": dict(source_hashes),
            "universe": sorted(set(universe)), "assumptions": deepcopy(assumptions),
            "starts_on": starts_on, "registered_at": registered.isoformat(),
            "promotion": "manual_only"}
    return {**spec, "experiment_id": identity(spec)}


def registration_status(spec, *, source_hashes, ticker, session):
    """A changed source or backdated session never joins a frozen trial."""
    spec = spec or {}
    declared = {k: v for k, v in spec.items() if k != "experiment_id"}
    reason = None
    if not spec.get("experiment_id") or identity(declared) != spec["experiment_id"]:
        reason = "missing_or_invalid_registration"
    elif spec.get("source_hashes") != source_hashes:
        reason = "frozen_sources_changed"
    elif ticker not in spec.get("universe", []):
        reason = "outside_registered_universe"
    elif str(session) < spec.get("starts_on", "9999-12-31"):
        reason = "before_prospective_start"
    return {"experiment_id": spec.get("experiment_id"), "eligible": reason is None,
            "reason": reason, "starts_on": spec.get("starts_on")}
