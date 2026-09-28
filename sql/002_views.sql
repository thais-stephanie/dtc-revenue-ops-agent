-- Deterministic views. Every number the agent is allowed to treat as a fact is
-- computed here (or by the equivalent Python in repository.py), never by the model.
--
-- Window convention, used identically by the generator, the app and Metabase:
--   "N days" = N complete days ending yesterday, relative to the demo clock.

CREATE TABLE IF NOT EXISTS demo_clock (
    id     BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    as_of  TIMESTAMPTZ NOT NULL
);

CREATE OR REPLACE FUNCTION as_of_date() RETURNS DATE
LANGUAGE sql STABLE AS $$ SELECT (SELECT as_of FROM demo_clock LIMIT 1)::date $$;

CREATE OR REPLACE FUNCTION as_of_ts() RETURNS TIMESTAMPTZ
LANGUAGE sql STABLE AS $$ SELECT (SELECT as_of FROM demo_clock LIMIT 1) $$;

-- Campaign performance for an arbitrary window -----------------------------
CREATE OR REPLACE FUNCTION campaign_performance_window(p_account_id TEXT, p_days INT)
RETURNS TABLE (
    account_id         TEXT,
    window_days        INT,
    spend              NUMERIC,
    attributed_revenue NUMERIC,
    roas               NUMERIC,
    mailed             BIGINT,
    conversions        BIGINT,
    response_rate      NUMERIC
)
LANGUAGE sql STABLE AS $$
    SELECT
        c.account_id,
        p_days,
        COALESCE(SUM(m.spend), 0),
        COALESCE(SUM(m.attributed_revenue), 0),
        CASE WHEN COALESCE(SUM(m.spend), 0) = 0 THEN NULL
             ELSE ROUND(SUM(m.attributed_revenue) / SUM(m.spend), 2) END,
        COALESCE(SUM(m.mailed), 0),
        COALESCE(SUM(m.conversions), 0),
        CASE WHEN COALESCE(SUM(m.mailed), 0) = 0 THEN NULL
             ELSE ROUND(SUM(m.conversions)::NUMERIC / SUM(m.mailed), 5) END
    FROM campaigns c
    JOIN campaign_metrics_daily m ON m.campaign_id = c.id
    WHERE c.account_id = p_account_id
      AND m.metric_date BETWEEN as_of_date() - p_days AND as_of_date() - 1
    GROUP BY c.account_id;
$$;

CREATE OR REPLACE VIEW campaign_performance_30d AS
SELECT c.account_id,
       SUM(m.spend)              AS spend,
       SUM(m.attributed_revenue) AS attributed_revenue,
       CASE WHEN SUM(m.spend) = 0 THEN NULL
            ELSE ROUND(SUM(m.attributed_revenue) / SUM(m.spend), 2) END AS roas
FROM campaigns c
JOIN campaign_metrics_daily m ON m.campaign_id = c.id
WHERE m.metric_date BETWEEN as_of_date() - 30 AND as_of_date() - 1
GROUP BY c.account_id;

CREATE OR REPLACE VIEW campaign_performance_7d AS
SELECT c.account_id,
       CASE WHEN SUM(m.spend) = 0 THEN NULL
            ELSE ROUND(SUM(m.attributed_revenue) / SUM(m.spend), 2) END AS roas_7d
FROM campaigns c
JOIN campaign_metrics_daily m ON m.campaign_id = c.id
WHERE m.metric_date BETWEEN as_of_date() - 7 AND as_of_date() - 1
GROUP BY c.account_id;

-- The baseline is the 28 days BEFORE the last 7, never overlapping them.
CREATE OR REPLACE VIEW campaign_baseline_28d AS
SELECT c.account_id,
       CASE WHEN SUM(m.spend) = 0 THEN NULL
            ELSE ROUND(SUM(m.attributed_revenue) / SUM(m.spend), 2) END AS roas_baseline_28d
FROM campaigns c
JOIN campaign_metrics_daily m ON m.campaign_id = c.id
WHERE m.metric_date BETWEEN as_of_date() - 35 AND as_of_date() - 8
GROUP BY c.account_id;

CREATE OR REPLACE VIEW active_campaign_types AS
SELECT account_id, ARRAY_AGG(DISTINCT campaign_type ORDER BY campaign_type) AS types
FROM campaigns WHERE status = 'active' GROUP BY account_id;

-- Commerce context ---------------------------------------------------------
CREATE OR REPLACE VIEW commerce_context_30d AS
WITH recent AS (
    SELECT account_id,
           SUM(attributed_direct_mail_revenue) AS attributed_direct_mail_revenue_30d,
           SUM(gross_revenue)                  AS gross_revenue_30d,
           SUM(orders)                         AS orders_30d,
           SUM(abandoned_carts)                AS abandoned_carts_30d
    FROM commerce_metrics_daily
    WHERE metric_date BETWEEN as_of_date() - 30 AND as_of_date() - 1
    GROUP BY account_id
), prior AS (
    SELECT account_id, SUM(abandoned_carts) AS abandoned_carts_prior_30d
    FROM commerce_metrics_daily
    WHERE metric_date BETWEEN as_of_date() - 60 AND as_of_date() - 31
    GROUP BY account_id
), latest AS (
    SELECT DISTINCT ON (account_id)
           account_id, lapsed_90d_customers, active_customers, synced_at
    FROM commerce_metrics_daily
    ORDER BY account_id, metric_date DESC
)
SELECT r.account_id,
       r.attributed_direct_mail_revenue_30d,
       r.gross_revenue_30d,
       r.orders_30d,
       r.abandoned_carts_30d,
       p.abandoned_carts_prior_30d,
       CASE WHEN COALESCE(p.abandoned_carts_prior_30d, 0) = 0 THEN NULL
            ELSE ROUND((r.abandoned_carts_30d - p.abandoned_carts_prior_30d)::NUMERIC
                       / p.abandoned_carts_prior_30d, 3) END AS abandoned_cart_growth,
       l.lapsed_90d_customers,
       l.active_customers,
       l.synced_at,
       ROUND(EXTRACT(EPOCH FROM (as_of_ts() - l.synced_at)) / 3600.0, 1) AS data_age_hours
FROM recent r
LEFT JOIN prior p USING (account_id)
LEFT JOIN latest l USING (account_id);

-- Reconciliation -----------------------------------------------------------
-- The bands live in one place. Change them here and in config.py together.
CREATE OR REPLACE VIEW account_reconciliation AS
SELECT cp.account_id,
       cp.attributed_revenue                        AS reported_attributed_revenue,
       cc.attributed_direct_mail_revenue_30d        AS commerce_attributed_revenue,
       ABS(cp.attributed_revenue - cc.attributed_direct_mail_revenue_30d) AS absolute_delta,
       CASE WHEN cc.attributed_direct_mail_revenue_30d = 0 THEN 1
            ELSE ROUND(ABS(cp.attributed_revenue - cc.attributed_direct_mail_revenue_30d)
                       / cc.attributed_direct_mail_revenue_30d, 4) END AS relative_delta,
       CASE
           WHEN ABS(cp.attributed_revenue - cc.attributed_direct_mail_revenue_30d) <= 250
             OR ABS(cp.attributed_revenue - cc.attributed_direct_mail_revenue_30d)
                / NULLIF(cc.attributed_direct_mail_revenue_30d, 0) <= 0.03 THEN 'match'
           WHEN ABS(cp.attributed_revenue - cc.attributed_direct_mail_revenue_30d) <= 1000
             OR ABS(cp.attributed_revenue - cc.attributed_direct_mail_revenue_30d)
                / NULLIF(cc.attributed_direct_mail_revenue_30d, 0) <= 0.08 THEN 'probable_match'
           ELSE 'exception'
       END AS status
FROM campaign_performance_30d cp
JOIN commerce_context_30d cc USING (account_id);

-- Billing ------------------------------------------------------------------
CREATE OR REPLACE VIEW account_billing AS
SELECT a.id AS account_id,
       CASE WHEN COUNT(i.id) FILTER (WHERE i.status = 'overdue') > 0
            THEN 'overdue' ELSE 'current' END AS status,
       COALESCE(MAX(as_of_date() - i.due_date) FILTER (WHERE i.status = 'overdue'), 0)
            AS days_overdue,
       COALESCE(SUM(i.amount) FILTER (WHERE i.status = 'overdue'), 0) AS amount_overdue
FROM accounts a
LEFT JOIN invoices i ON i.account_id = a.id
GROUP BY a.id;

-- One row per account, everything the agent may treat as fact ---------------
CREATE OR REPLACE VIEW account_facts AS
SELECT a.id AS account_id,
       a.brand_name, a.vertical, a.commerce_platform, a.lifecycle_stage, a.account_owner,
       b.status AS billing_status, b.days_overdue, b.amount_overdue,
       cc.data_age_hours,
       (cc.data_age_hours > 48) AS stale,
       r.status AS reconciliation_status,
       r.reported_attributed_revenue, r.commerce_attributed_revenue,
       r.absolute_delta, r.relative_delta,
       p7.roas_7d, p30.roas AS roas_30d, pb.roas_baseline_28d,
       COALESCE(act.types, ARRAY[]::TEXT[]) AS active_campaign_types,
       cc.abandoned_carts_30d, cc.abandoned_cart_growth,
       cc.lapsed_90d_customers, cc.active_customers,
       COALESCE((SELECT MIN(as_of_date() - ca.occurred_at::date)
                 FROM crm_activity ca WHERE ca.account_id = a.id), 999) AS days_since_last_touch
FROM accounts a
LEFT JOIN account_billing b ON b.account_id = a.id
LEFT JOIN commerce_context_30d cc ON cc.account_id = a.id
LEFT JOIN account_reconciliation r ON r.account_id = a.id
LEFT JOIN campaign_performance_7d p7 ON p7.account_id = a.id
LEFT JOIN campaign_performance_30d p30 ON p30.account_id = a.id
LEFT JOIN campaign_baseline_28d pb ON pb.account_id = a.id
LEFT JOIN active_campaign_types act ON act.account_id = a.id;
