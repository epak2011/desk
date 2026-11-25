# Trading Desk API deployment

The standalone API is implemented in `api_service.py` and reads only saved,
canonical engine output from Postgres. It does not import or run Streamlit.

## Local verification

```bash
python -m pip install -r requirements-api.txt
uvicorn api_service:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/v1/health
```

A local response may report `degraded` when `DATABASE_URL` is not configured.
That is expected and does not disclose connection details.

## Render deployment

`render.yaml` and `Dockerfile.api` define a deployable Render web service.
Connect the GitHub repository as a Render Blueprint and set these secret values
in Render's environment settings:

```text
DATABASE_URL
SUPABASE_URL
SUPABASE_ANON_KEY
CORS_ALLOWED_ORIGINS
TRADING_DESK_OWNER_EMAIL
```

`CORS_ALLOWED_ORIGINS` must be a comma-separated allowlist containing the exact
Lovable production origin and any temporary preview origin that is still needed.
Do not use `*` with authenticated routes.

Never add the Supabase service-role key, database URL, email-provider keys,
worker secrets, or private exports to the frontend or repository.

After Render assigns the API origin:

1. Confirm `GET /v1/health` returns `status: ok`.
2. Set Lovable's `VITE_TRADING_DESK_API_URL` to that origin.
3. Add the exact Lovable origin to `CORS_ALLOWED_ORIGINS`.
4. Test a public decision and regime request.
5. Test a private workspace request with a Supabase access token.
6. Test two users and verify their workspaces remain isolated.
7. Only then enable real watchlist mutations in Lovable.

## Endpoints implemented

- `GET /v1/health`
- `GET /v1/regime`
- `GET /v1/decisions/{ticker}`
- `GET /v1/workspace`
- `PATCH /v1/workspace`
- `GET /v1/watchlist`
- `PUT /v1/watchlist/{ticker}`
- `DELETE /v1/watchlist/{ticker}`
- `GET /v1/attention`
- `GET /v1/portfolio`
- `GET /v1/calibration`
- `GET /v1/operator/engine-updates` (verified owner only)

Private endpoints verify the bearer token with Supabase Auth and use only the
verified UUID to load state. The browser cannot select an account by passing an
email address or user ID.

### Database transfer safeguards (November 25, 2026)

- Worker maintenance uses five-minute triggers during the weekday 09:30–16:00 America/New_York window and hourly triggers otherwise. GitHub scheduling is best effort; manual queue drains remain available. No self-dispatch loop runs overnight. The weekday window does not additionally model exchange holidays.
- Schema bootstrap runs once per database/process, and retries after failure. Full saved reports validate their database timestamp before local reuse. User state uses a user/database-scoped conditional SQL read, so unchanged private JSON does not cross the network; other processes' writes remain visible. Caches are bounded and return independent copies.
- Full decision history is read once per scoring pass. Cached history checks count and last update after its existing 90-second window. Before a full transfer, a transactional ledger reserves estimated JSON bytes plus 10% and 1 KB of headroom, capped at 1 GB over a rolling 30 days and 32 MB per transfer. Exhaustion fails explicitly; no history is deleted or silently truncated, and saved summaries remain readable. The ledger is stored under engine_review_status/history_read_budget and the worker logs aggregate reserved/rolling bytes without private content.
- Failed/restarted scoring attempts reserve a six-hour durable cooldown; successful publications retain the existing twenty-hour gate. Database and authentication outages use a sixty-second process cooldown. Upstream auth outages return 503 rather than falsely expiring the user's session. The frontend pauses periodic refresh after auth/quota/rate-limit/server errors; explicit retry remains available.
- The ledger covers full-history reads only. Supabase's organization usage page remains authoritative for total billed egress, including protocol, auth, market and other traffic. Deployment does not remove an already-exhausted quota. Verify normal post-restoration daily usage against roughly 167 MB/day (5 GB over 30 days); use 100 MB/day as an operational headroom target. Restricted traffic is not a valid normal-usage baseline.
