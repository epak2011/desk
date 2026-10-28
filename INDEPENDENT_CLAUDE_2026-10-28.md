# Independent Claude assessment and comparison

New research adds one bounded, separate stateless Claude request. The model sees factual whitelisted technical/business inputs and timestamps, NOT rule action, bias, scores, trigger levels, prior Claude thesis, or the constrained legacy tactical call. No rule thresholds are imposed. This is independence from the rule answer, not independent data collection: absent current news or verified facts must be disclosed, not invented.

## Analyze contract

`research.claude_assessment` contains:

- `status`: ready / legacy / stale / unavailable
- `version`: independent-claude-2026.10-v1, or legacy-rules-constrained
- `independent`, `generated_at`, `model`, `assessment_id`, `ownership_conditional`
- `rule_action`, `disagrees`, `comparison_scope`
- `assessment.entry`: action BUY_NOW / START_SMALL / WATCH / AVOID, reasoning, counterargument, what_changes_call, optional trigger/invalidation
- `assessment.ownership`: action ADD / HOLD / TRIM / SELL, reasoning, what_changes_call; conditional without actual user holdings
- `assessment.confidence`: subjective 1–10, not a calibrated probability
- `assessment.missing_information`, `assessment.evidence_notes`, `assessment.horizon`
- legacy uses `legacy_call` and an explicit rules-constrained label; it is never relabeled independent or mixed into the new study.

Put a small accessible **Claude disagrees** badge in the upper corner of the decision card ONLY when ready and disagrees=true. Hover/focus/tap shows both entry calls, reason and timestamp; click opens complete comparison. Keep ownership separate. A matching call can still be opened as Claude's independent view. Do not invent a sell disagreement without position context. Do not recalculate the flag or replace the canonical action in the client. Stale reports suppress current disagreement.

## Tracking

Every successfully saved worker research report appends `claude_pair:<assessment_id>` to the existing operator table using ON CONFLICT DO NOTHING, in the same transaction. Freeze issuance time, factual inputs, prompt hash, actual model, answer and paired rule action/code fingerprint. No retrospective rewrites or legacy backfill.

Prospective comparison is registered in experiments/claude-comparison-v1.json, eligible for calls issued from October 29, 2026. The scheduled GitHub workflow scores daily after the close, conservatively using only prior-date completed bars. Entry is the next exchange-session open after the actual issuance date. This is distinct from the existing immutable V4 entry-event trial, whose files and frozen sources are unchanged.

Measurements: 5/14/30 sessions, identical dates, 10 bps per side, cash zero interest. Research sleeve fractions: Buy/Enter 100%, Start small/Accumulate 25%, otherwise cash. These are explicit TEST assumptions, never live position sizing. Checkpoints are marked valuations, NEVER sell instructions. Score all paired calls and separately disagreements, use nonoverlapping ticker cohorts, and keep model/rule-code strata separate. Preserve missing data as unavailable. Snapshot and summary transfer is bounded, with coverage-limit flags.

`GET /v1/operator/performance` adds `claude_comparison`: groups by horizon/scope/model/rule_fingerprint with sample_count, average_checkpoint_returns_pct (claude/rules/SPY/QQQ), and claude_minus_rules_pp. These are paired checkpoint averages, NOT portfolio returns, drawdowns or a live-account strategy. No automated winner or promotion. Conditional ownership calls are not scored without an execution/position ledger.

## Operational boundaries

No live rule decision, entry-event registration, frozen experiment start, threshold or exit policy is changed. Existing research is not automatically regenerated in bulk. One optional manual DASH verification is bounded and uses real provider credits. Independent generation failure is explicitly unavailable, not a fabricated fallback success. Full independent portfolio performance remains unestablished.
