-- Trading Desk durable backend layer.
-- Safe to run more than once.

CREATE TABLE IF NOT EXISTS watchlist_assets (
    ticker TEXT PRIMARY KEY,
    asset_type TEXT DEFAULT 'stock',
    name TEXT,
    sector TEXT,
    industry TEXT,
    exchange TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    ticker TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    source TEXT NOT NULL DEFAULT 'yahoo',
    as_of TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rule_outputs (
    ticker TEXT PRIMARY KEY,
    action TEXT,
    trigger_text TEXT,
    invalidation_text TEXT,
    setup_type TEXT,
    confidence NUMERIC,
    payload JSONB NOT NULL,
    market_updated_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pm_memos (
    ticker TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    source TEXT,
    generated_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS research_reports (
    ticker TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    source TEXT,
    generated_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS holdings (
    ticker TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS market_regime_daily (
    day DATE PRIMARY KEY,
    payload JSONB NOT NULL,
    source TEXT,
    generated_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS refresh_jobs (
    id UUID PRIMARY KEY,
    job_type TEXT NOT NULL,
    ticker TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 100,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB,
    error TEXT,
    requested_by TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS refresh_jobs_status_priority_idx
    ON refresh_jobs (status, priority, created_at);

CREATE INDEX IF NOT EXISTS refresh_jobs_ticker_idx
    ON refresh_jobs (ticker, created_at DESC);
