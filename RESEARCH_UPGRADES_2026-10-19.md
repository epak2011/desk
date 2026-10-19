# Entry, execution and evidence upgrades — October 19, 2026

No production action thresholds or automatic trading are changed. The archived
October 17 control/report remain available; a new source manifest is a new
research release, never a silent rewrite of that archive.

## Implemented in this working release (deployment still to verify)

1. Durable event origins: a level must be observed and armed before a close
   crosses it. Level, stop, session and source fingerprint are frozen. Two
   confirmation closes must occur AFTER the event. Retrospective prose does not
   establish an observed origin. Worker observations live in separate journal and
   append-only observation keys, not disposable rule-output caches. Prior-date
   New York calendar bars are used conservatively; not an intraday exchange feed.
2. Opt-in `--include-event-study` adds `event_early` and `event_confirmed` to the
   technical replay. Both require a canonical qualifying action, the same frozen
   event stop, same next-open fills, same costs and same technical exits. No
   event may enter twice per arm. Default four-arm replay remains available.
3. Explicit PM instruction ledger supports entry/add from a specified fraction of
   available cash, specified share-fraction trims, non-decreasing saved stop
   changes and full exits. Missing choices remain unresolved, not filled. This
   is infrastructure, NOT a claim that historical discretionary instructions
   exist or that live PM sizing has been standardized.
4. Corrected-policy replay adds per-arm exposure-matched SPY diagnostics.
   Overnight gaps belong to previous exposure; new exposure starts at the open;
   turnover and terminal valuation costs apply. Ex-post exposure matching is a
   diagnostic, not an investable timing strategy. Forward comparisons additionally
   separate exact original code fingerprints and expose non-overlapping gates.
   Existing seven-day pooled gates remain labeled historical; they are not silently
   replaced. Missing fingerprints do not become verified controls.
5. Future-only experiment registration hashes source/universe/assumptions and
   rejects backdated starts. Runtime eligibility rejects changed sources, dates
   before registration start, and securities outside the frozen universe.
   Changes require a NEW registration, not revision of old evidence.

## Outstanding acceptance / policy choices

The active runtime registration is `experiments/entry-events-v2.json`. The first
registration is retained, not overwritten: a pre-start review tightened the live
eligibility guard to use the event's FIRED session, not a later refresh date.
An event originating before the registered start cannot join later merely by
being refreshed. Both registrations precede the October 20 trial start; changing
source again invalidates eligibility until a new future-only registration exists.

- Run hosted full tests, verify event-journal persistence and owner API fields,
  run/archive the opt-in historical study, then reconcile Lovable with the report.
- Historical event replay is simulated point-in-time observation, NOT historical
  proof that the live worker recorded those events. Prospective evidence starts
  only after registration and observation; earlier data remain exploratory.
- No automatic rule promotion. Do not claim better profitability from passing tests.
- Existing PM language allows a 1/3–1/2 trim and discretionary stop levels.
  User must choose an exact deterministic policy or supply recorded instructions
  before full PM execution parity can be claimed. No invented 25/50/100 sizing.
- Persisted event observations are daily-close observations, not precise intraday
  execution timestamps. Full exchange-calendar/intraday timing parity is not claimed.

## Commands

`python scripts/run_rules_replay.py ... --include-event-study` archives the expanded
study; it does not promote live rules.

`python scripts/register_entry_study.py --starts-on YYYY-MM-DD --tickers A,B --output experiments/entry-events-v1.json`
requires a future start and refuses to overwrite an existing specification.

`portfolio_ledger.replay_instructions` requires explicit decision-close instructions;
it retains residual cash and values open positions without inventing a final sale.
