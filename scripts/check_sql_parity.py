"""SQL / Python parity.

The Python repository and the SQL views are two implementations of the same
policy. Two implementations drift. This compares them account by account and
fails loudly on the first difference.

    docker compose up -d db
    python scripts/seed_demo.py
    python scripts/check_sql_parity.py

Not run in the environment where this project was written: it needs a live
Postgres. Run it before trusting anything the SQL views say.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_agent import config, signals  # noqa: E402
from revenue_agent.repository import InMemoryRepository, PostgresRepository  # noqa: E402

TOLERANCE = 0.01  # cents, not a fudge factor


def _close(a, b) -> bool:
    if a is None or b is None:
        return a == b
    return abs(float(a) - float(b)) <= TOLERANCE


def main() -> int:
    mem = InMemoryRepository()
    pg = PostgresRepository()
    differences: list[str] = []

    # Both sides must be standing on the same day, or the comparison is
    # meaningless and every difference below would be noise from a stale seed.
    with pg.conn.cursor() as cur:
        cur.execute("SELECT as_of FROM demo_clock")
        row = cur.fetchone()
    if row is None:
        print("  demo_clock is empty: run scripts/seed_demo.py first")
        return 1
    if row[0] != config.AS_OF:
        print(f"  clock mismatch: python={config.AS_OF.isoformat()} sql={row[0].isoformat()}")
        print("  reseed with scripts/seed_demo.py before trusting any comparison")
        return 1

    accounts_mem = sorted(mem.list_account_ids())
    accounts_pg = sorted(pg.list_account_ids())
    if accounts_mem != accounts_pg:
        print(f"  account sets differ: {set(accounts_mem) ^ set(accounts_pg)}")
        return 1

    for account_id in accounts_mem:
        m = mem.account_facts(account_id)
        p = pg.account_facts(account_id)
        checks = [
            ("roas_7d", m["performance"]["roas_7d"], p.get("roas_7d")),
            ("roas_30d", m["performance"]["roas_30d"], p.get("roas_30d")),
            ("roas_baseline_28d", m["performance"]["roas_baseline_28d"], p.get("roas_baseline_28d")),
            (
                "reported_revenue",
                m["reconciliation"]["reported_attributed_revenue"],
                p.get("reported_attributed_revenue"),
            ),
            (
                "commerce_revenue",
                m["reconciliation"]["commerce_attributed_revenue"],
                p.get("commerce_attributed_revenue"),
            ),
            ("data_age_hours", m["data_freshness"]["commerce_hours"], p.get("data_age_hours")),
            ("abandoned_carts_30d", m["commerce"]["abandoned_carts_30d"], p.get("abandoned_carts_30d")),
            ("days_overdue", m["billing"]["days_overdue"], p.get("days_overdue")),
        ]
        for name, left, right in checks:
            if not _close(left, right):
                differences.append(f"{account_id:22} {name:20} python={left!r} sql={right!r}")

        if m["reconciliation"]["status"] != p.get("reconciliation_status"):
            differences.append(
                f"{account_id:22} {'reconciliation':20} "
                f"python={m['reconciliation']['status']} sql={p.get('reconciliation_status')}"
            )

    py_shortlist = [e["account_id"] for e in signals.shortlist(mem)]
    with pg.conn.cursor() as cur:
        cur.execute("SELECT account_id FROM daily_shortlist")
        sql_shortlist = [row[0] for row in cur.fetchall()]
    if py_shortlist != sql_shortlist:
        differences.append(f"{'shortlist':22} python={py_shortlist} sql={sql_shortlist}")

    print(f"\n  SQL / Python parity\n\n  {len(accounts_mem)} accounts compared")
    print(f"  {len(py_shortlist)} shortlisted\n")
    if differences:
        for line in differences:
            print(f"  DIFF  {line}")
        print(f"\n  {len(differences)} differences\n")
        return 1
    print("  Signals        PASS\n  ROAS           PASS\n  Reconciliation PASS")
    print("  Freshness      PASS\n  Priority       PASS\n\n  0 differences\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
