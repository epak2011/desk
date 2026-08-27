# Authentication rollout

Authentication is implemented but deliberately disabled until Supabase Auth and
the database migration are configured. The existing owner app remains unchanged
while `AUTH_REQUIRED` is false.

## 1. Prepare Supabase

1. In Supabase Authentication, enable Email authentication and require email confirmation.
2. Configure the production Site URL and allowed redirect URLs.
3. Run `migrations/001_user_app_state.sql` in the Supabase SQL editor.
4. Confirm row-level security is enabled on `user_app_state` and all four policies exist.

## 2. Add Streamlit secrets

Add these values in Streamlit Cloud settings. Never commit them:

```toml
SUPABASE_URL = "https://PROJECT.supabase.co"
SUPABASE_ANON_KEY = "public-anon-key"
AUTH_REQUIRED = true
TRADING_DESK_PUBLIC_DEMO = false
```

The anon key is the public Supabase client key; do not use the service-role key.
`DATABASE_URL` remains server-only.

## 3. Beta verification

- Create two test users and confirm email for each.
- Give the users different watchlists, holdings, notes, sizing, and chats.
- Sign out and switch accounts; verify no private state crosses accounts.
- Confirm anonymous Public Demo cannot save or access Holdings/System Health.
- Confirm a missing auth configuration closes the app instead of exposing owner data.
- Confirm the owner legacy record remains intact before migrating it to a user.

## Current beta limitation

Authentication survives Streamlit reruns but not a full browser-session reset. A
production React frontend should store Supabase sessions in secure browser storage
and refresh tokens through the Supabase SDK. Do not advertise persistent login
until that frontend/session layer is implemented and tested.

## Email delivery activation

Run `migrations/002_notification_outbox.sql`, verify a sender domain with Resend,
then add these GitHub Actions secrets:

```text
RESEND_API_KEY
NOTIFICATION_FROM_EMAIL
NOTIFICATIONS_ENABLED=true
UNSUBSCRIBE_SECRET
```

Keep `NOTIFICATIONS_ENABLED` unset until test recipients, unsubscribe handling,
and the sender domain have been verified. The worker will not claim or transmit
any outbox row unless all three values are present. Recipient addresses are never
printed in worker logs, and each digest key can be inserted only once.

After verifying a test delivery and its unsubscribe link, add this Streamlit secret:

```toml
NOTIFICATIONS_AVAILABLE = true
UNSUBSCRIBE_SECRET = "the same long random value used by the worker"
```

This makes the opt-in checkbox available to authenticated users. Keep it false
until the sender domain and unsubscribe link have both passed end-to-end testing.

## Data model boundary

Private user state is stored only in `user_app_state`, keyed by the verified
Supabase UUID. Market snapshots, deterministic rule outputs, regime data, and
non-personal research remain shared. Logged user decisions are not added to the
shared calibration dataset in authenticated mode; a future anonymized opt-in
pipeline should handle that explicitly.
