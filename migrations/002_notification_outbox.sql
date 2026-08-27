-- Server-only delivery queue. No anon/authenticated policies are intentional:
-- users manage preferences through their own user_app_state row; only the
-- trusted worker database connection may inspect recipients or message bodies.
CREATE TABLE IF NOT EXISTS public.notification_outbox (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    digest_key TEXT NOT NULL UNIQUE,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    html TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    provider_id TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.notification_outbox ENABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS notification_outbox_status_idx
    ON public.notification_outbox (status, created_at);
