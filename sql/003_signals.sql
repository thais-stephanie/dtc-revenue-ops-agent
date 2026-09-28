-- Signals. The deterministic layer that decides which accounts are worth a
-- human's attention at all. The agent receives the output of this view; it
-- never scans the portfolio itself.
--
-- Thresholds mirror config.py. Keep the two in step.

CREATE OR REPLACE VIEW account_signals AS
WITH f AS (SELECT * FROM account_facts)
SELECT account_id, 'attribution_exception' AS signal_type, 5 AS severity,
       jsonb_build_object('reported', reported_attributed_revenue,
                          'commerce', commerce_attributed_revenue,
                          'relative_delta', relative_delta) AS facts
FROM f WHERE reconciliation_status = 'exception'

UNION ALL
SELECT account_id, 'stale_data', 4,
       jsonb_build_object('data_age_hours', data_age_hours)
FROM f WHERE stale

UNION ALL
SELECT account_id, 'performance_drop',
       CASE WHEN (roas_baseline_28d - roas_7d) / roas_baseline_28d >= 0.5 THEN 5 ELSE 4 END,
       jsonb_build_object('roas_7d', roas_7d,
                          'roas_baseline_28d', roas_baseline_28d,
                          'relative_drop', ROUND((roas_baseline_28d - roas_7d)
                                                 / roas_baseline_28d, 3))
FROM f
WHERE roas_7d IS NOT NULL AND roas_baseline_28d IS NOT NULL
  AND (roas_baseline_28d - roas_7d) / roas_baseline_28d >= 0.30

UNION ALL
SELECT account_id, 'cart_recovery_gap',
       CASE WHEN abandoned_carts_30d >= 1000 THEN 4 ELSE 3 END,
       jsonb_build_object('abandoned_carts_30d', abandoned_carts_30d,
                          'abandoned_cart_growth', abandoned_cart_growth,
                          'active_campaign_types', active_campaign_types)
FROM f
WHERE abandoned_carts_30d >= 500 AND NOT ('cart_recovery' = ANY (active_campaign_types))

UNION ALL
SELECT account_id, 'reactivation_gap',
       CASE WHEN lapsed_90d_customers >= 4000 THEN 4 ELSE 3 END,
       jsonb_build_object('lapsed_90d_customers', lapsed_90d_customers,
                          'active_campaign_types', active_campaign_types)
FROM f
WHERE lapsed_90d_customers >= 2000 AND NOT ('win_back' = ANY (active_campaign_types))

UNION ALL
SELECT account_id, 'billing_hold', 4,
       jsonb_build_object('days_overdue', days_overdue, 'amount_overdue', amount_overdue)
FROM f WHERE days_overdue >= 30

UNION ALL
SELECT account_id, 'engagement_gap', 2,
       jsonb_build_object('days_since_last_touch', days_since_last_touch)
FROM f WHERE days_since_last_touch >= 45;

-- Creative fatigue needs the per-campaign response curve, so it is its own view.
CREATE OR REPLACE VIEW creative_fatigue AS
WITH per_campaign AS (
    SELECT c.account_id, c.creative_id, c.id AS campaign_id, c.launched_at,
           SUM(m.conversions)::NUMERIC / NULLIF(SUM(m.mailed), 0) AS response_rate
    FROM campaigns c
    JOIN campaign_metrics_daily m ON m.campaign_id = c.id
    GROUP BY c.account_id, c.creative_id, c.id, c.launched_at
), ordered AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY account_id, creative_id ORDER BY launched_at) AS seq,
              COUNT(*)    OVER (PARTITION BY account_id, creative_id) AS campaigns,
              LAG(response_rate) OVER (PARTITION BY account_id, creative_id ORDER BY launched_at)
                  AS prev_rate
    FROM per_campaign
)
SELECT account_id, creative_id, campaigns,
       ARRAY_AGG(ROUND(response_rate, 5) ORDER BY seq) AS response_rates
FROM ordered
WHERE campaigns >= 3
GROUP BY account_id, creative_id, campaigns
HAVING BOOL_AND(prev_rate IS NULL OR response_rate < prev_rate);

-- What the agent is handed each morning.
CREATE OR REPLACE VIEW daily_shortlist AS
SELECT account_id,
       MAX(severity)  AS top_severity,
       COUNT(*)       AS signal_count,
       jsonb_agg(jsonb_build_object('type', signal_type, 'severity', severity, 'facts', facts)
                 ORDER BY severity DESC) AS signals,
       BOOL_OR(signal_type IN ('cart_recovery_gap', 'reactivation_gap'))
         AND BOOL_OR(signal_type IN ('performance_drop')) AS competing_signals
FROM account_signals
GROUP BY account_id
ORDER BY top_severity DESC, signal_count DESC, account_id
LIMIT 8;
