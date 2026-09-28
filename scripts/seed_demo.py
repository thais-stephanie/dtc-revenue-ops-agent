"""Load the generated portfolio into Postgres.

    python scripts/seed_demo.py                  # uses DATABASE_URL
    python scripts/seed_demo.py --dump out.sql   # no database needed

The app itself runs on the in-memory portfolio by default. Postgres exists so
the same facts can be queried as SQL and pointed at Metabase.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_agent import config  # noqa: E402
from revenue_agent.dataset import build_portfolio  # noqa: E402

TABLES = [
    ("accounts", ("id", "brand_name", "vertical", "commerce_platform", "lifecycle_stage",
                  "account_owner", "created_at")),
    ("campaigns", ("id", "account_id", "campaign_type", "status", "creative_id",
                   "launched_at", "ended_at")),
    ("campaign_metrics_daily", ("campaign_id", "metric_date", "mailed", "spend",
                                "attributed_revenue", "conversions")),
    ("commerce_metrics_daily", ("account_id", "metric_date", "gross_revenue",
                                "attributed_direct_mail_revenue", "orders", "abandoned_carts",
                                "lapsed_90d_customers", "active_customers", "synced_at")),
    ("crm_activity", ("id", "account_id", "activity_type", "occurred_at", "author", "body")),
    ("invoices", ("id", "account_id", "amount", "due_date", "paid_at", "status")),
]

ATTR = {
    "accounts": "accounts",
    "campaigns": "campaigns",
    "campaign_metrics_daily": "campaign_metrics",
    "commerce_metrics_daily": "commerce_metrics",
    "crm_activity": "crm_notes",
    "invoices": "invoices",
}
FIELD_ALIASES = {"type": "activity_type"}


def rows_for(portfolio, table: str, columns: tuple[str, ...]) -> list[tuple]:
    records = getattr(portfolio, ATTR[table])
    out = []
    for record in records:
        values = []
        for column in columns:
            name = FIELD_ALIASES.get(column, column)
            values.append(getattr(record, name))
        out.append(tuple(values))
    return out


def literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump", help="write INSERT statements to a file instead of a database")
    args = parser.parse_args()

    portfolio = build_portfolio()
    schema = (ROOT / "sql" / "001_schema.sql").read_text()
    views = (ROOT / "sql" / "002_views.sql").read_text()
    signals_sql = (ROOT / "sql" / "003_signals.sql").read_text()
    clock = (
        "DELETE FROM demo_clock; "
        f"INSERT INTO demo_clock (as_of) VALUES ('{config.AS_OF.isoformat()}');"
    )

    if args.dump:
        parts = [schema, "\n-- data --\n"]
        for table, columns in TABLES:
            for row in rows_for(portfolio, table, columns):
                parts.append(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
                    f"({', '.join(literal(v) for v in row)});"
                )
        parts += [views, signals_sql, clock]
        pathlib.Path(args.dump).write_text("\n".join(parts))
        print(f"wrote {args.dump}")
        return 0

    import psycopg

    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute(schema)
        conn.execute(views)
        conn.execute(signals_sql)
        conn.execute(clock)
        for table, columns in TABLES:
            conn.execute(f"TRUNCATE {table} CASCADE")
            rows = rows_for(portfolio, table, columns)
            placeholders = ", ".join(["%s"] * len(columns))
            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                    rows,
                )
            print(f"  {table:26} {len(rows):>6} rows")
    print("seeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
