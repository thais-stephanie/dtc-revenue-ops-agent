"""Fact access.

Two implementations behind one protocol:

* InMemoryRepository - computes the same facts in Python over the generated
  portfolio. Used by the eval suite so it runs with no database.
* PostgresRepository - MIRROR ONLY. The same facts as SQL, mirroring
  sql/002_views.sql. No app path reads it: the agent, API and daily run use
  InMemoryRepository. It exists so scripts/check_sql_parity.py can prove the
  SQL views match the Python facts. Metabase reads the SQL views directly.

Nothing here is ever computed by the model.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Protocol

from . import config
from .dataset import AS_OF_DATE, build_portfolio, in_window
from .domain import Portfolio
from .reconciliation import classify_revenue_difference


class Repository(Protocol):
    def list_account_ids(self) -> list[str]: ...
    def account_facts(self, account_id: str) -> dict: ...
    def campaign_performance(self, account_id: str, days: int) -> dict: ...
    def commerce_context(self, account_id: str) -> dict: ...
    def recent_activity(self, account_id: str, limit: int = 5) -> list[dict]: ...


class InMemoryRepository:
    def __init__(self, portfolio: Portfolio | None = None) -> None:
        self.p = portfolio or build_portfolio()
        self._accounts = {a.id: a for a in self.p.accounts}

    # -- helpers ----------------------------------------------------------
    def _window(self, days: int, offset: int = 0) -> tuple[date, date]:
        """`days` complete days ending yesterday, shifted back by `offset`."""
        return (
            AS_OF_DATE - timedelta(days=days + offset),
            AS_OF_DATE - timedelta(days=1 + offset),
        )

    def _campaign_rows(self, account_id: str, start: date, end: date) -> list:
        campaign_ids = {c.id for c in self.p.campaigns if c.account_id == account_id}
        return [
            m
            for m in self.p.campaign_metrics
            if m.campaign_id in campaign_ids and start <= m.metric_date <= end
        ]

    def _roas(self, rows) -> float | None:
        spend = sum(r.spend for r in rows)
        revenue = sum(r.attributed_revenue for r in rows)
        return round(revenue / spend, 2) if spend else None

    # -- protocol ---------------------------------------------------------
    def list_account_ids(self) -> list[str]:
        return [a.id for a in self.p.accounts]

    def campaign_performance(self, account_id: str, days: int) -> dict:
        start, end = self._window(days)
        rows = self._campaign_rows(account_id, start, end)
        spend = round(sum(r.spend for r in rows), 2)
        revenue = round(sum(r.attributed_revenue for r in rows), 2)
        mailed = sum(r.mailed for r in rows)
        conversions = sum(r.conversions for r in rows)
        by_type: dict[str, dict] = {}
        campaigns = {c.id: c for c in self.p.campaigns if c.account_id == account_id}
        for r in rows:
            ctype = campaigns[r.campaign_id].campaign_type
            entry = by_type.setdefault(ctype, {"spend": 0.0, "revenue": 0.0, "mailed": 0})
            entry["spend"] += r.spend
            entry["revenue"] += r.attributed_revenue
            entry["mailed"] += r.mailed
        for entry in by_type.values():
            entry["spend"] = round(entry["spend"], 2)
            entry["revenue"] = round(entry["revenue"], 2)
            entry["roas"] = round(entry["revenue"] / entry["spend"], 2) if entry["spend"] else None
        return {
            "account_id": account_id,
            "window_days": days,
            "spend": spend,
            "attributed_revenue": revenue,
            "roas": round(revenue / spend, 2) if spend else None,
            "mailed": mailed,
            "conversions": conversions,
            "response_rate": round(conversions / mailed, 5) if mailed else None,
            "by_campaign_type": by_type,
            "active_campaign_types": sorted(
                {c.campaign_type for c in campaigns.values() if c.status == "active"}
            ),
        }

    def commerce_context(self, account_id: str) -> dict:
        rows = [m for m in self.p.commerce_metrics if m.account_id == account_id]
        recent = [m for m in rows if in_window(m.metric_date, 30)]
        prior = [m for m in rows if in_window(m.metric_date, 30, offset=30)]
        latest = max(rows, key=lambda m: m.metric_date) if rows else None
        carts_30 = sum(m.abandoned_carts for m in recent)
        carts_prior = sum(m.abandoned_carts for m in prior)
        synced_at = latest.synced_at if latest else config.AS_OF
        return {
            "account_id": account_id,
            "attributed_direct_mail_revenue_30d": round(
                sum(m.attributed_direct_mail_revenue for m in recent), 2
            ),
            "gross_revenue_30d": round(sum(m.gross_revenue for m in recent), 2),
            "orders_30d": sum(m.orders for m in recent),
            "abandoned_carts_30d": carts_30,
            "abandoned_carts_prior_30d": carts_prior,
            "abandoned_cart_growth": round((carts_30 - carts_prior) / carts_prior, 3)
            if carts_prior
            else None,
            "lapsed_90d_customers": latest.lapsed_90d_customers if latest else 0,
            "active_customers": latest.active_customers if latest else 0,
            "synced_at": synced_at.isoformat(),
            "data_age_hours": round((config.AS_OF - synced_at).total_seconds() / 3600, 1),
        }

    def recent_activity(self, account_id: str, limit: int = 5) -> list[dict]:
        notes = sorted(
            (n for n in self.p.crm_notes if n.account_id == account_id),
            key=lambda n: n.occurred_at,
            reverse=True,
        )[: min(limit, 10)]
        return [
            {
                "id": n.id,
                "type": n.activity_type,
                "occurred_at": n.occurred_at.isoformat(),
                "days_ago": (config.AS_OF - n.occurred_at).days,
                "author": n.author,
                "body": n.body,
            }
            for n in notes
        ]

    def account_facts(self, account_id: str) -> dict:
        acct = self._accounts[account_id]
        perf_7 = self.campaign_performance(account_id, 7)
        perf_30 = self.campaign_performance(account_id, 30)
        base_start, base_end = self._window(28, offset=7)
        baseline_rows = self._campaign_rows(account_id, base_start, base_end)
        commerce = self.commerce_context(account_id)
        recon = classify_revenue_difference(
            perf_30["attributed_revenue"], commerce["attributed_direct_mail_revenue_30d"]
        )
        invoices = [i for i in self.p.invoices if i.account_id == account_id]
        overdue = [i for i in invoices if i.status == "overdue"]
        days_overdue = (
            max((AS_OF_DATE - i.due_date).days for i in overdue) if overdue else 0
        )
        notes = self.recent_activity(account_id, limit=10)
        days_since_touch = notes[0]["days_ago"] if notes else 999

        # creative fatigue: same creative across >= N campaigns with falling response
        series: dict[str, list[tuple[date, float]]] = {}
        campaigns = [c for c in self.p.campaigns if c.account_id == account_id]
        for c in campaigns:
            rows = [m for m in self.p.campaign_metrics if m.campaign_id == c.id]
            mailed = sum(r.mailed for r in rows)
            conv = sum(r.conversions for r in rows)
            if mailed:
                series.setdefault(c.creative_id, []).append(
                    (c.launched_at.date(), round(conv / mailed, 5))
                )
        fatigue = {}
        for creative_id, points in series.items():
            points.sort()
            if len(points) >= config.CREATIVE_FATIGUE_MIN_CAMPAIGNS:
                rates = [p[1] for p in points]
                if all(b < a for a, b in zip(rates, rates[1:])):
                    fatigue = {
                        "creative_id": creative_id,
                        "response_rates": rates,
                        "campaigns": len(points),
                    }
        return {
            "account": {
                "id": acct.id,
                "brand": acct.brand_name,
                "vertical": acct.vertical,
                "commerce_platform": acct.commerce_platform,
                "lifecycle_stage": acct.lifecycle_stage,
                "account_owner": acct.account_owner,
            },
            "billing": {
                "status": "overdue" if overdue else "current",
                "days_overdue": days_overdue,
                "amount_overdue": round(sum(i.amount for i in overdue), 2),
            },
            "data_freshness": {
                "commerce_hours": commerce["data_age_hours"],
                "stale": commerce["data_age_hours"] > config.STALE_AFTER_HOURS,
            },
            "reconciliation": recon.as_facts(),
            "performance": {
                "roas_7d": perf_7["roas"],
                "roas_30d": perf_30["roas"],
                "roas_baseline_28d": self._roas(baseline_rows),
                "active_campaign_types": perf_30["active_campaign_types"],
            },
            "commerce": commerce,
            "engagement": {"days_since_last_touch": days_since_touch},
            "creative_fatigue": fatigue,
        }


class PostgresRepository:
    """Same facts, read from Postgres. Mirrors sql/002_views.sql.

    Requires `psycopg`. Kept deliberately thin: every calculation lives in SQL.
    """

    def __init__(self, dsn: str | None = None) -> None:
        import psycopg  # local import so the eval suite runs without the driver

        self.conn = psycopg.connect(dsn or config.DATABASE_URL, autocommit=True)

    def _one(self, sql: str, params: tuple) -> dict | None:
        from psycopg.rows import dict_row

        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        from psycopg.rows import dict_row

        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def list_account_ids(self) -> list[str]:
        return [r["id"] for r in self._all("SELECT id FROM accounts ORDER BY id")]

    def campaign_performance(self, account_id: str, days: int) -> dict:
        row = self._one(
            "SELECT * FROM campaign_performance_window(%s, %s)", (account_id, days)
        )
        return dict(row or {})

    def commerce_context(self, account_id: str) -> dict:
        row = self._one("SELECT * FROM commerce_context_30d WHERE account_id = %s", (account_id,))
        return dict(row or {})

    def recent_activity(self, account_id: str, limit: int = 5) -> list[dict]:
        return self._all(
            "SELECT id, activity_type AS type, occurred_at, author, body "
            "FROM crm_activity WHERE account_id = %s ORDER BY occurred_at DESC LIMIT %s",
            (account_id, min(limit, 10)),
        )

    def account_facts(self, account_id: str) -> dict:
        row = self._one("SELECT * FROM account_facts WHERE account_id = %s", (account_id,))
        return dict(row or {})
