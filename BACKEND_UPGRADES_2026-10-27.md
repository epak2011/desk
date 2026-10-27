# Backend reliability upgrades — October 27, 2026

## Five implemented improvements

1. **Bounded AI research recovery and diagnostics.** Fast dossier generation
   now handles missing numeric inputs without fake zero/default values, rejects
   past earnings dates as upcoming, combines text blocks, and repairs malformed
   or token-truncated responses once with a bounded output budget. Failed
   generations preserve the last successful report. Queue diagnostics expose
   safe categories (authentication, quota, timeout, model, truncation, JSON,
   identity, contract validation), not provider messages, keys or prompts.

2. **Persistent scheduled retry budgets.** One small SQL aggregate calculates
   failures since each ticker's latest successful report. Automatic retries
   wait 30, 60, 120 minutes and so on, capped at 24 hours. Provider authentication
   or quota failures wait 24 hours. Cooldowns survive worker restarts, do not
   consume the scheduling batch, and reset after success. Explicit user research
   requests remain available; no existing reports or jobs are deleted.

3. **Immutable experiment health.** System Health validates the real registration
   and every frozen source hash. Mismatches are visible as a blocked trial and
   name the changed sources. No start date or hash is rewritten. Eligibility
   alone is not presented as verified data collection or mature performance.

4. **Honest versioned performance context.** Calibration labels saved signal
   evidence as 5/14/30-session measurements, not strategy P&L. It exposes saved
   version breakdowns, evidence age and unknown/stale status. Owner replay data
   adds a same-period comparison of each strategy, SPY and same-stock passive
   benchmarks, retaining separate closed trades and open-position valuations.
   Archived replay is never called new forward performance.

5. **Selected-revision freshness checks.** System Health audits the timestamp
   and stale/content-blocked status of the actually selected PM memo/dossier,
   rather than a failed or absent full-report row. Missing or implausibly future
   timestamps cannot be called fresh. Worker-history read failures and blocked
   prospective trials make System Health report needs_attention.

## Boundaries and remaining verification

- No trading entry/exit thresholds changed; no calendar sales restored.
- Existing archives and prospective registrations remain unchanged.
- The older trial is actually source-mismatched. A separately authorized future
  registration is required before claiming new observations are valid for it.
- These upgrades improve recovery and diagnosis; they do not establish that
  provider credentials/billing are valid or that fresh AI generation succeeded.
- New API metadata is additive. Existing clients receive corrected freshness
  issues automatically, but new visual controls still require frontend work.
- Deployment and authenticated, real-provider acceptance must be reported
  separately from automated tests.

## API integration notes

- `GET /v1/system-health`: `prospective_trial` contains status, reason,
  changed_sources, starts_on and collection_verified. Preserve an explicit block.
- Recent full-report jobs can contain `result.research_diagnostics`. Render the
  safe error_code; do not infer missing data has been successfully regenerated.
- `GET /v1/calibration`: `calibration.performance_context` separates signal
  evidence from portfolio returns and includes freshness and version_breakdown.
- Owner case request `?include_cases=true`: historical_replay.comparison_summary
  contains period dates, strategy rows and same-period benchmarks. Do not
  combine its return denominator with signal-family average decision returns.
- Continue requesting case pages explicitly, at most 50 per page. Never restore
  unbounded decision-history reads or background case polling.
