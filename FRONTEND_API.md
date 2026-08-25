# Public frontend boundary

Trading Desk now has one canonical decision contract in `public_contract.py`.
A future Lovable or custom web frontend should render this contract rather than
reimplementing any rule, action, confidence, sizing, trigger, or data-trust logic.

## Recommended endpoints

- `GET /v1/decision/{ticker}` → `decision_payload(...)`
- `GET /v1/attention` → `attention_payload(...)`
- `GET /v1/regime` → the canonical saved market snapshot
- `GET /v1/calibration` → the existing performance slices and confidence calibration

The future service layer should be thin: load canonical engine state, call these
serializers, apply authentication/rate limits, and return JSON. Private holdings,
notes, chats, database details, and manual levels must never enter public payloads.

## Branch point

The frontend can branch once authentication and hosting are selected. The engine
should remain the source of truth on `main`; frontend work may evolve independently
against contract version 1. Contract changes require a version bump and fixture tests.
