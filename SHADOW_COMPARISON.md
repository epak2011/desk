# Automatic original-versus-shadow comparison

Live rules are unchanged. `tactical.compute` appends shadow-only confirmation
inputs. A held entry means the last two completed daily closes are at or above
the trigger's buy-above level. Today's daily bar is excluded conservatively.
No concrete level or incomplete bars means unknown, not false.

Each automatic decision's existing `decisions_log` row retains the original
action, immutable decision input snapshot, engine provenance and candidate
versions under `shadow_evaluations`. The maintenance worker adds missing
candidate versions only from frozen snapshots, never today's replacement data.
It scores the original and candidate on the same 14-session path.

`GET /v1/calibration` publishes `calibration.paired_shadow_comparison`:

- candidate, version
- eligible_count, unknown_count, changed_count
- original_avg_policy_return_pct, candidate_avg_policy_return_pct
- paired_lift_pct (percentage points), basis

Null returns indicate no eligible mature evidence. Both policies are long or
cash: cash earns 0 before interest. Avoid to Hold off is cash to cash, not an
imaginary short trade or invented profit. This is not stop-executed, cost-adjusted
or portfolio-sized P&L and cannot establish a rule promotion on its own.

Lovable consumes this response through its existing authenticated central API
connection. No spreadsheet transfer, duplicated rule code or additional database
is required. The UI should refresh on focus and every 60 seconds while visible,
and show absent backend data as unavailable rather than simulated values.

Candidates: `confirmed_structural_avoid` / `shadow-2026.10-a` and
`confirmed_entry_hold` / `shadow-2026.10-b`. Changing their definition requires a
new version. Existing production and historical decisions are never replaced.
