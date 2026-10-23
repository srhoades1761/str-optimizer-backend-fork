-- STR Optimizer — billing & subscriber schema
-- Applied automatically at startup (services/db.py ensure_schema).
-- Safe to re-run; every statement is IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS subscribers (
    id                      BIGSERIAL PRIMARY KEY,
    email                   TEXT        NOT NULL,
    listing_url             TEXT        NOT NULL,

    stripe_customer_id      TEXT        UNIQUE,
    stripe_subscription_id  TEXT        UNIQUE,
    plan                    TEXT,                  -- 'monthly_499' | 'annual_4788'

    -- Mirrors Stripe's subscription.status:
    -- active | trialing | past_due | canceled | unpaid | incomplete | incomplete_expired
    status                  TEXT        NOT NULL DEFAULT 'incomplete',
    cancel_at_period_end    BOOLEAN     NOT NULL DEFAULT FALSE,
    current_period_end      TIMESTAMPTZ,
    cancellation_reason     TEXT,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS subscribers_email_idx  ON subscribers (LOWER(email));
CREATE INDEX IF NOT EXISTS subscribers_status_idx ON subscribers (status);

-- One listing may only be tracked once per subscriber.
CREATE UNIQUE INDEX IF NOT EXISTS subscribers_email_listing_idx
    ON subscribers (LOWER(email), listing_url);


-- Stripe retries webhooks. Recording every event id we've handled makes
-- delivery idempotent, so a retry can never generate a second report or
-- a second email.
CREATE TABLE IF NOT EXISTS processed_events (
    event_id     TEXT        PRIMARY KEY,
    event_type   TEXT        NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- Report generation is queued here rather than run inside the web request.
-- The pipeline takes far longer than Stripe's webhook timeout (~20s) and
-- Heroku's router limit (30s, not configurable).
CREATE TABLE IF NOT EXISTS report_jobs (
    id            BIGSERIAL PRIMARY KEY,
    subscriber_id BIGINT      NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    kind          TEXT        NOT NULL DEFAULT 'initial',  -- 'initial' | 'monthly'
    status        TEXT        NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
    attempts      INT         NOT NULL DEFAULT 0,
    last_error    TEXT,
    pdf_url       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS report_jobs_pending_idx
    ON report_jobs (status, created_at)
    WHERE status = 'pending';


-- Listing brief additions (October 2026) ------------------------------------

-- What each brief recommended, for month-two progress tracking.
ALTER TABLE report_jobs ADD COLUMN IF NOT EXISTS summary JSONB;

-- Emails that clicked unsubscribe. The worker never sends to these.
-- This stops emails only; billing is cancelled in the Stripe portal.
CREATE TABLE IF NOT EXISTS unsubscribes (
    email       TEXT        PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- AirROI market figures, shared by subscribers in the same market.
-- Aggregated monthly figures only; rows older than 30 days are ignored.
CREATE TABLE IF NOT EXISTS market_cache (
    key         TEXT        PRIMARY KEY,
    payload     JSONB       NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
