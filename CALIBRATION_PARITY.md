# Shared rules evidence: Streamlit, worker and Lovable

The worker scores immutable automatic rule calls in `decisions_log`. The owner
Streamlit Backtest and authenticated `GET /v1/calibration` read this history
through `engine_evaluation.rules_performance_snapshot`. Account-state copies must
never replace scored outcomes. Missing owner automatic calls are appended with
`ON CONFLICT DO NOTHING`; private notes, manual choices and manual outcomes are
excluded. Other users retain their private Streamlit histories.

The API returns `{meta, calibration, shadow_rules}`. Render the contents of
`calibration`; do not expect evidence arrays at the response root. `shadow_rules`
is current watchlist divergence; `calibration.shadow_performance` is matured
historical evidence. They describe different observations.

## Required frontend sections

- `flags`: label, count, successes, success_rate_pct, avg_decision_return_pct,
  status and reason. Display `watch` as "Watch closely", `review_logic` as
  "Review logic", `stable` as "No review flag", and `collecting` as "Collecting
  evidence". A stable gate does not prove profitability; negative returns still
  need visible red formatting.
- `directional_families`: one row per long/avoid family. Show count,
  positive_direction_rate_pct, success_rate_pct, avg_return_pct,
  avg_decision_return_pct, median_decision_return_pct,
  trimmed_mean_decision_return_pct, avg_benchmark_return_pct,
  avg_decision_excess_pct, distinct_tickers and worst_3_loss_share_pct. Values
  ending `_pct` are already percentages: do not multiply by 100. Display null as
  unavailable, never zero. Cash_reference_return_pct is a 0% comparison before
  interest. Returns are equal-weight signal evidence, not realized account P&L.
- `patience_summary`: separate Watch/Hold Off lifecycle evidence, not failed
  directional predictions.
- `evidence_quality`: logged and cohort counts, directional and patience counts,
  pending path updates, excluded_directional_reasons and explanatory notes.
  Seven-calendar-day cohorts can overlap 14-session windows; do not call them
  fully independent. `non_overlapping_families` gives the sensitivity check.
- `confidence_calibration_by_family`: compare confidence within the same
  directional family. Do not silently mix long and avoid returns.
- `engine_version_families`: separate recorded labels from the unrecorded
  historical cohort, showing count, success and return for each action family.
  Friendly labels such as `rules-2026.08-d` were reused; they do not establish
  results for today's deployed code. `evidence_quality.recorded_input_coverage`
  reports the counts with engine version, code fingerprint, regime, confidence,
  extension-warning evidence and frozen inputs. Missing warnings are unknown,
  not proof that there was no extension warning.
- `shadow_performance` and `shadow_promotion`: show maturity and improvement
  evidence. Never change a live action from a shadow candidate.

## Owner-only detail

The backend checks the verified account email against `TRADING_DESK_OWNER_EMAIL`.
Only an owner response has `case_review_available: true` and the arrays below.
Do not manufacture owner-only content or treat its absence as missing backend
generation. Authentication is Supabase Bearer as for the existing workspace.

- `review_cases`: all eligible mature cases, ordered by decision return worst
  first. Show ticker, logged/scored date, action, engine_version, reference_price,
  setup_score, reward_risk, rs, vol_ratio, market_regime, confidence,
  extension_warning/severity, trigger_kind/summary/price, invalidation_price,
  forward_return_pct, benchmark_return_pct, excess_return_pct,
  decision_return_pct, decision_excess_pct, mfe_pct, mae_pct and invalidation
  status/date. Make columns sortable; link ticker to `/analyze/{ticker}`.
  Newly captured cases also carry `rule_source`, `rule_implementation`,
  `rule_code_fingerprint` and `deployment_revision`; old calls are never assigned
  today's provenance. Put fingerprints in advanced owner diagnostics only.
- `weakest_cases`: compact list of failed calls for priority review.
- `failure_patterns`: grouped decision-time attributes. Every row includes
  `family`; keep long and avoid panels separate.
- `performance_slices`: engine, ticker, action, trigger, entry warning, regime,
  tape, confidence and setup groups with at least three cases, each with family.

Use `evidence_id` privately to compare the case population across clients, never
as customer-facing jargon. The shared report includes only current evaluation
version 6, integrity-approved mature directional outcomes. Exclusions are
reported. A future re-score may legitimately change this ID.

## Interpretation

For Enter/Accumulate, success requires a positive 14-session return AND beating
SPY. Positive direction rate distinguishes rising underperformers from losses.
The mean and median stock returns establish whether the calls beat cash.

For Avoid, `decision_return_pct = -forward_return_pct`; a negative decision
return means missed upside, not a realized short position loss. The stock return
column shows what the avoided security actually did.

Historical diagnostics identify hypotheses. Trading thresholds are unchanged.
Do not generate a rule recommendation until the source/version, overlap,
benchmark, action, regime and ticker concentration checks have been reviewed.
