# Analyze parity and research integrity — October 21, 2026

No trading-rule thresholds, active experiment definitions or landing-page copy are changed.

## Confirmed defects and repairs

- API technical-picture labels diverged from Streamlit's `technical_picture_rows`:
  moving-average states, momentum +/-1.5, RS bands, volume 1.25/0.75 and range bands.
  Restore those presentation semantics; unavailable/nonfinite readings are explicit.
- Fix `pct_of_52u_range` typo to canonical `pct_of_52w_range`.
- Match Streamlit's setup checklist (both moving averages; R/R 1.3 or existing
  Enter action; existing Enter trigger; volume 0.8). This checklist is a display,
  not a new trading decision engine.
- Expose canonical execution-plan levels and raw technical-evidence numbers for
  frontend rendering; absent fields stay null rather than reconstructed.
- Label rules-only research separately with `provenance`, `generation_status`,
  `completeness`, `retry_required` and `display_label`; mirror metadata in PM panel.
- Do not assign a business-quality tier from the tactical fallback. Do not leak
  clipped prompt instructions or provider exception details in public fallback copy.
- Preserve actual compact Claude research when a later full dossier fails.
- Expose saved PM deep-dive fields without generating or inventing missing sections.
- Reject fallback/incomplete dossiers in `refresh_full_report` before overwriting
  the saved report. Failed background research must not be a successful job.
- Requeue known fresh fallbacks, not just week-old reports; maintenance remains
  bounded by its existing limit/cadence. Legacy provenance remains unverified.
- Polling a historical succeeded job with incomplete research returns failed with
  retry guidance. System Health distinguishes incomplete reports from full coverage.

## Frontend acceptance requested from Lovable

Keep action, sizing, trigger, constraint, invalidation, evidence, score hover,
company profile, technical picture and PM research all bound to canonical payloads.
Show incomplete sections explicitly and provide the existing authenticated research
POST/poll/refetch flow. A queue success is not evidence of completed Claude research.
Preserve current styling and the user's separately edited landing-page copy.

## Verification boundary

38 focused local tests passed before publication. Full hosted-suite and deployed
acceptance are separate checks; do not infer either from this local count.
The original Streamlit application woke successfully but requires sign-in for a
fresh authenticated visual comparison. Missing API sections must be inventoried,
not silently declared equivalent or filled with placeholders.
