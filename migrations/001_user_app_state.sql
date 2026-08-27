-- Run in the Supabase SQL editor before enabling AUTH_REQUIRED.
-- Existing owner data remains in legacy tables until an explicit migration.
CREATE TABLE IF NOT EXISTS public.user_app_state (
    user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    value JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.user_app_state ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "users read own app state" ON public.user_app_state;
CREATE POLICY "users read own app state" ON public.user_app_state
    FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "users insert own app state" ON public.user_app_state;
CREATE POLICY "users insert own app state" ON public.user_app_state
    FOR INSERT WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "users update own app state" ON public.user_app_state;
CREATE POLICY "users update own app state" ON public.user_app_state
    FOR UPDATE USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "users delete own app state" ON public.user_app_state;
CREATE POLICY "users delete own app state" ON public.user_app_state
    FOR DELETE USING (auth.uid() = user_id);
