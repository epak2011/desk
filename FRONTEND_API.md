# Public frontend boundary

Trading Desk now has one canonical decision contract in `public_contract.py`.
A future Lovable or custom web frontend should render this contract rather than
reimplementing any rule, action, confidence, sizing, trigger, or data-trust logic.

Contract version 2 is fully described in `contracts/openapi.yaml`. Fictional,
privacy-safe fixtures live in `contracts/examples/`, and the complete Lovable
product/build prompt is in `LOVABLE_HANDOFF.md`.

## Recommended endpoints

- `GET /v1/app-manifest` → backend-owned page map, routes, auth requirements, and parity status
- `GET /v1/decisions/{ticker}` → `decision_payload(...)`
- `POST /v1/decisions/{ticker}/research/requests` → saved research or a queued current AI report (authenticated)
- `GET /v1/attention` → `attention_payload(...)`
- `GET /v1/regime` → `regime_payload(...)`
- `GET/PATCH /v1/workspace` → `user_workspace_payload(...)` after verified auth
- `GET /v1/watchlist` → `watchlist_payload(...)` after verified auth
- `GET /v1/portfolio` → private holdings, position notes, and settings after verified auth
- `GET /v1/ideas` → saved thematic screens and candidate evidence after verified auth
- `POST /v1/ideas/requests` → queue a private thematic screen; poll `GET /v1/idea-requests/{request_id}`
- `GET /v1/system-health` → persisted storage, coverage, freshness, and worker-job audit after verified auth
- `GET /v1/methodology` → public action definitions, methodology, quality guide, and disclosures
- `GET /v1/calibration` → the existing performance slices and confidence calibration
- `GET /v1/operator/engine-updates` → owner-only rules governance feed after verified auth

## Owner update feed

Streamlit and Lovable must render `GET /v1/operator/engine-updates`; neither
frontend should maintain a separate changelog. Lovable sends the signed-in
Supabase access token as `Authorization: Bearer <token>`, shows the navigation
item only for the configured owner account, and still treats a `403` response as
authoritative. Hiding the navigation item is presentation, not authorization.

Each backend rules release updates `rules_updates.py` in the same commit. The
endpoint and Streamlit therefore receive the same versioned entries on deployment,
without copying content between frontends.

The decision response includes a presentation-ready `analyze_page` object with
the complete hero strip, evidence matrix, action rationale, call-change conditions,
technical picture, PM quality guide, all PM sections, earnings/analyst/Lynch cards,
and full-report state. Clients should render those named sections directly and
must not reproduce the Streamlit-only derivation logic.

`GET /v1/app-manifest` is also the parity checklist. A page marked `shared` has
a backend-owned render contract. A page marked `partial` must display its
declared `missing` capabilities instead of substituting sample data or quietly
reimplementing Streamlit logic in the browser.

It also includes a presentation-ready `research` object with the
saved company overview, thesis, drivers, risks, valuation, PM narrative, and
quality view. Research can explain or challenge a decision, but it cannot change
the canonical action returned in `decision`.

Research freshness is separate from market-data trust. `research.status: stale`
includes `age_days` and `stale_reasons`; clients should keep the research visible,
show a restrained red refresh warning, and offer the authenticated research
request action without changing or blocking the canonical decision.

The future service layer should be thin: load canonical engine state, call these
serializers, apply authentication/rate limits, and return JSON. Private holdings,
notes, chats, database details, and manual levels must never enter public payloads.

## Branch point

The engine remains the source of truth on `main`; frontend work may evolve
independently against contract version 2. Contract changes require a version bump,
OpenAPI update, privacy review, and fixture tests.
