# Research cost controls

Background full reports use Haiku only. Missing/deprecated Haiku models never
escalate to Sonnet. The shared message helper also defaults to Haiku; a caller
must pass `allow_sonnet=True` to request the standard-model path. Existing UI
calls do not opt in. Fast reports omit the separate independent assessment;
explicit full dossiers retain a separate assessment on the default Haiku path.

The worker reserves a durable database slot before any full-report API call.
Defaults: six full-report attempts per UTC day across the worker, and at least
24 hours between attempts for a stock. Configure `RESEARCH_DAILY_REPORT_LIMIT`
(set to 0 to stop worker reports) and `RESEARCH_COOLDOWN_HOURS` (minimum 24).
Reservations include failures/timeouts; they are not refunded. An advisory
transaction lock serializes concurrent reservations. Queue planning excludes
cooling-down stocks so they cannot starve other watchlist names.

Price-derived market cap, forward P/E and PEG no longer invalidate the business
research fingerprint. Earnings/revenue growth and debt/equity changes still do.
A fingerprint from the old algorithm can trigger one bounded migration refresh.
Normal market-data and deterministic rule scans retain their existing cadence.

The limit counts worker full reports, not dollars or all account API requests.
Interactive direct calls, idea discovery, and other applications using the key
are outside this report budget. Keep the provider's account spend limit enabled.
No API funds are purchased and auto-reload is not changed by this patch.

PM and dossier SDK clients disable automatic retries and bound network timeouts.
The UI timeout cannot cancel a running request, so reservations remain consumed.
The budget table is created lazily using the existing database role, which must
have CREATE TABLE privileges (as it does for the existing backend schema).

The worker source identity changes. Existing frozen prospective experiments
correctly report `frozen_sources_changed` and will not mix new observations into
the old cohort. Their registrations and prior results are not rewritten; a new
explicit registration is needed to resume collection under the changed worker.
