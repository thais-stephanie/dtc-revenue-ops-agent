"""Human review queue: the approval step of SAFE_WRITE_GATEWAY, applier absent.

    python -m revenue_agent.review list
    python -m revenue_agent.review approve <publication_key> --by <name> [--note TEXT]
    python -m revenue_agent.review reject  <publication_key> --by <name> --note <reason>
    ... add --supersede to replace an earlier decision with a new one

A decision is one event in the ledger's append-only `reviews` table: SQLite
triggers refuse UPDATE and DELETE, so history cannot be rewritten. Changing a
decision needs --supersede and adds a new event linked to the one it replaces.
An approval changes nothing anywhere: there is no applier, by design, and the
status says so. Unattended runs never read or write this table.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

from . import unattended

NOT_APPLIED = "approved, not applied: no applier exists by design"
MIN_PREFIX = 8

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    reviewer TEXT NOT NULL CHECK (length(trim(reviewer)) > 0),
    note TEXT,
    supersedes INTEGER REFERENCES reviews(review_id),
    decided_at TEXT NOT NULL
)""",
    # append-only: history is never edited or removed
    """CREATE TRIGGER IF NOT EXISTS reviews_no_update BEFORE UPDATE ON reviews
    BEGIN SELECT RAISE(ABORT, 'reviews are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS reviews_no_delete BEFORE DELETE ON reviews
    BEGIN SELECT RAISE(ABORT, 'reviews are append-only'); END""",
    # one first decision per publication, and each event superseded at most
    # once: the decision history is a single chain even under concurrent use
    "CREATE UNIQUE INDEX IF NOT EXISTS reviews_one_first ON reviews(idempotency_key) WHERE supersedes IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS reviews_one_successor ON reviews(supersedes) WHERE supersedes IS NOT NULL",
]


class ReviewError(Exception):
    pass


def _sql(ledger: Path, statement: str, params=()) -> list[dict]:
    with unattended._db(ledger) as conn:  # the same ledger, one transaction
        for s in SCHEMA:
            conn.execute(s)
        return [dict(r) for r in conn.execute(statement, params).fetchall()]


def resolve(ledger: Path, key: str) -> str:
    """A full publication key from a key or a unique prefix of at least 8 chars."""
    if len(key) < MIN_PREFIX or not re.fullmatch(r"[0-9a-f]+", key):
        raise ReviewError(f"give at least {MIN_PREFIX} hex characters of the publication key")
    rows = _sql(ledger, "SELECT idempotency_key FROM publications WHERE idempotency_key LIKE ? || '%'",
                (key,))
    if not rows:
        raise ReviewError(f"no publication with key {key}")
    if len(rows) > 1:
        raise ReviewError(f"{key} matches {len(rows)} publications; give more characters")
    return rows[0]["idempotency_key"]


def latest(ledger: Path, key: str) -> dict | None:
    """The decision in force: the event no other event supersedes."""
    rows = _sql(ledger, "SELECT * FROM reviews r WHERE idempotency_key = ? AND NOT EXISTS "
                        "(SELECT 1 FROM reviews s WHERE s.supersedes = r.review_id)", (key,))
    return rows[0] if rows else None


def decide(ledger: Path, key: str, decision: str, reviewer: str, note: str | None,
           supersede: bool = False) -> dict:
    key = resolve(ledger, key)
    current = latest(ledger, key)
    if current and not supersede:
        raise ReviewError(f"already {current['decision']} by {current['reviewer']} at "
                          f"{current['decided_at']}; add --supersede to record a new decision")
    if supersede and not current:
        raise ReviewError("nothing to supersede: this publication has no decision yet")
    try:
        _sql(ledger, "INSERT INTO reviews (idempotency_key, decision, reviewer, note, supersedes, decided_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
             (key, decision, reviewer, note, current["review_id"] if current else None, unattended._now()))
    except sqlite3.IntegrityError as exc:  # e.g. a concurrent decision won the race
        raise ReviewError(f"not recorded: {exc}") from None
    return latest(ledger, key)


def status(review: dict | None) -> str:
    if review is None:
        return "awaiting review"
    return NOT_APPLIED if review["decision"] == "approved" else "rejected"


def queue(ledger: Path) -> list[dict]:
    rows = _sql(ledger, "SELECT idempotency_key, account_id, action, published_at FROM publications "
                        "ORDER BY published_at, rowid")
    return [{**r, "review": latest(ledger, r["idempotency_key"])} for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="append-only human review of publications")
    parser.add_argument("--ledger", type=Path, default=unattended.LEDGER)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("key")
        p.add_argument("--by", required=True)
        p.add_argument("--note", required=name == "reject")
        p.add_argument("--supersede", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "list":
        items = queue(args.ledger)
        if not items:
            print("No publications to review.")
        for item in items:
            review = item["review"]
            who = f"  by {review['reviewer']}" + (f": {review['note']}" if review["note"] else "") if review else ""
            print(f"{item['idempotency_key'][:12]}  {item['account_id']:20} {item['action']:30} "
                  f"{status(review)}{who}")
        return 0
    try:
        review = decide(args.ledger, args.key, "approved" if args.command == "approve" else "rejected",
                        args.by, args.note, args.supersede)
    except ReviewError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"recorded review #{review['review_id']} for {review['idempotency_key'][:12]}: {status(review)}"
          + (f" (supersedes #{review['supersedes']})" if review["supersedes"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
