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

Private endpoints verify the bearer token with Supabase Auth and use only the
verified UUID to load state. The browser cannot select an account by passing an
email address or user ID.
