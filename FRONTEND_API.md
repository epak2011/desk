# Public frontend boundary

Trading Desk now has one canonical decision contract in `public_contract.py`.
A future Lovable or custom web frontend should render this contract rather than
reimplementing any rule, action, confidence, sizing, trigger, or data-trust logic.

Contract version 2 is fully described in `contracts/openapi.yaml`. Fictional,
privacy-safe fixtures live in `contracts/examples/`, and the complete Lovable
product/build prompt is in `LOVABLE_HANDOFF.md`.

## Recommended endpoints

- `GET /v1/decisions/{ticker}` → `decision_payload(...)`
- `GET /v1/attention` → `attention_payload(...)`
- `GET /v1/regime` → `regime_payload(...)`
- `GET/PATCH /v1/workspace` → `user_workspace_payload(...)` after verified auth
- `GET /v1/watchlist` → `watchlist_payload(...)` after verified auth
- `GET /v1/calibration` → the existing performance slices and confidence calibration

The future service layer should be thin: load canonical engine state, call these
serializers, apply authentication/rate limits, and return JSON. Private holdings,
notes, chats, database details, and manual levels must never enter public payloads.

## Branch point

The engine remains the source of truth on `main`; frontend work may evolve
independently against contract version 2. Contract changes require a version bump,
OpenAPI update, privacy review, and fixture tests.
