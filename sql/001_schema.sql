-- Schema for the synthetic portfolio.
-- Postgres is the analytics mirror: the same facts the agent reads, in a shape
-- Metabase can sit on top of. All content is generated; no real data.

CREATE TABLE IF NOT EXISTS accounts (
    id                TEXT PRIMARY KEY,
    brand_name        TEXT        NOT NULL,
    vertical          TEXT        NOT NULL,
    commerce_platform TEXT        NOT NULL,
    lifecycle_stage   TEXT        NOT NULL,
    account_owner     TEXT        NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id            TEXT PRIMARY KEY,
    account_id    TEXT        NOT NULL REFERENCES accounts (id),
    campaign_type TEXT        NOT NULL,
    status        TEXT        NOT NULL,
    creative_id   TEXT        NOT NULL,
    launched_at   TIMESTAMPTZ NOT NULL,
    ended_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS campaign_metrics_daily (
    campaign_id        TEXT          NOT NULL REFERENCES campaigns (id),
    metric_date        DATE          NOT NULL,
    mailed             INTEGER       NOT NULL,
    spend              NUMERIC(12,2) NOT NULL,
    attributed_revenue NUMERIC(12,2) NOT NULL,
    conversions        INTEGER       NOT NULL,
    PRIMARY KEY (campaign_id, metric_date)
);

CREATE TABLE IF NOT EXISTS commerce_metrics_daily (
    account_id                     TEXT          NOT NULL REFERENCES accounts (id),
    metric_date                    DATE          NOT NULL,
    gross_revenue                  NUMERIC(12,2) NOT NULL,
    attributed_direct_mail_revenue NUMERIC(12,2) NOT NULL,
    orders                         INTEGER       NOT NULL,
    abandoned_carts                INTEGER       NOT NULL,
    lapsed_90d_customers           INTEGER       NOT NULL,
    active_customers               INTEGER       NOT NULL,
    synced_at                      TIMESTAMPTZ   NOT NULL,
    PRIMARY KEY (account_id, metric_date)
);

CREATE TABLE IF NOT EXISTS crm_activity (
    id            TEXT PRIMARY KEY,
    account_id    TEXT        NOT NULL REFERENCES accounts (id),
    activity_type TEXT        NOT NULL,
    occurred_at   TIMESTAMPTZ NOT NULL,
    author        TEXT        NOT NULL,
    body          TEXT        NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id         TEXT PRIMARY KEY,
    account_id TEXT          NOT NULL REFERENCES accounts (id),
    amount     NUMERIC(12,2) NOT NULL,
    due_date   DATE          NOT NULL,
    paid_at    DATE,
    status     TEXT          NOT NULL
);

-- Proposals, not tasks. Nothing here has touched a CRM.
CREATE TABLE IF NOT EXISTS proposed_tasks (
    id              TEXT PRIMARY KEY,
    account_id      TEXT        NOT NULL REFERENCES accounts (id),
    title           TEXT        NOT NULL,
    description     TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT        NOT NULL DEFAULT 'PROPOSED',
    approved_at     TIMESTAMPTZ,
    executed_at     TIMESTAMPTZ,
    idempotency_key TEXT        NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id                  TEXT PRIMARY KEY,
    started_at          TIMESTAMPTZ   NOT NULL,
    finished_at         TIMESTAMPTZ,
    status              TEXT          NOT NULL,
    accounts_scanned    INTEGER       NOT NULL DEFAULT 0,
    accounts_shortlisted INTEGER      NOT NULL DEFAULT 0,
    accounts_recommended INTEGER      NOT NULL DEFAULT 0,
    tool_calls          INTEGER       NOT NULL DEFAULT 0,
    input_tokens        INTEGER       NOT NULL DEFAULT 0,
    output_tokens       INTEGER       NOT NULL DEFAULT 0,
    estimated_cost_usd  NUMERIC(10,6) NOT NULL DEFAULT 0,
    duration_ms         INTEGER       NOT NULL DEFAULT 0,
    output_json         JSONB
);

-- Every model output the gate refused, with the reason. This table is the
-- difference between "we measured failures" and "we did something about them".
CREATE TABLE IF NOT EXISTS rejected_outputs (
    id         BIGSERIAL PRIMARY KEY,
    run_id     TEXT        NOT NULL REFERENCES agent_runs (id),
    account_id TEXT,
    reason     TEXT        NOT NULL,
    details    JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_campaign_metrics_date ON campaign_metrics_daily (metric_date);
CREATE INDEX IF NOT EXISTS idx_commerce_metrics_account_date ON commerce_metrics_daily (account_id, metric_date);
CREATE INDEX IF NOT EXISTS idx_crm_activity_account ON crm_activity (account_id, occurred_at DESC);
