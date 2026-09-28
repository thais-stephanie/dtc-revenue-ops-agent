"""The human review queue: append-only decisions, and an approval that applies nothing."""
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_agent import console, review, unattended  # noqa: E402


class _Queue(unittest.TestCase):
    """A ledger holding the three publications of one offline unattended run."""

    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("ANTHROPIC_API_KEY", "SLACK_WEBHOOK_URL", "HEALTHCHECKS_PING_URL")}
        mock.patch.dict(os.environ, clean, clear=True).start()
        self.urlopen = mock.patch("urllib.request.urlopen").start()
        self.addCleanup(mock.patch.stopall)
        self.ledger = pathlib.Path("runs/unattended.sqlite3")
        unattended.run_once(lambda: unattended._runner(False, None), live=False, ledger=self.ledger)
        self.keys = [r["idempotency_key"] for r in unattended._sql(
            self.ledger, "SELECT idempotency_key FROM publications ORDER BY rowid")]
        self.urlopen.reset_mock()

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = review.main(["--ledger", str(self.ledger), *argv])
        return code, out.getvalue() + err.getvalue()

    def snapshot(self):
        return (unattended._sql(self.ledger, "SELECT * FROM publications ORDER BY rowid"),
                unattended._sql(self.ledger, "SELECT * FROM runs ORDER BY rowid"))


class TestDecisions(_Queue):
    def test_approval_is_recorded_and_applies_nothing(self):
        before = self.snapshot()
        code, out = self.cli("approve", self.keys[0][:12], "--by", "thais", "--note", "looks right")
        self.assertEqual(code, 0)
        self.assertIn("approved, not applied: no applier exists by design", out)
        self.assertEqual(self.snapshot(), before)  # publications and runs untouched
        self.urlopen.assert_not_called()  # nothing left the machine
        code, listing = self.cli("list")
        self.assertIn("approved, not applied: no applier exists by design  by thais: looks right", listing)
        self.assertEqual(listing.count("awaiting review"), 2)

    def test_reject_needs_a_reason(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            review.main(["--ledger", str(self.ledger), "reject", self.keys[1], "--by", "thais"])
        code, out = self.cli("reject", self.keys[1], "--by", "thais", "--note", "wrong account")
        self.assertEqual((code, "rejected" in out), (0, True))

    def test_unknown_or_ambiguous_keys_are_refused(self):
        for key in ("0" * 64, "abc", "%%%%%%%%", "________"):
            code, out = self.cli("approve", key, "--by", "thais")
            self.assertEqual(code, 1, key)
            self.assertIn("refused:", out)
        self.assertEqual(review._sql(self.ledger, "SELECT COUNT(*) AS n FROM reviews")[0]["n"], 0)

    def test_a_second_decision_needs_supersede_and_keeps_the_first(self):
        self.cli("approve", self.keys[0], "--by", "thais")
        code, out = self.cli("reject", self.keys[0], "--by", "maya", "--note", "customer paused")
        self.assertEqual(code, 1)
        self.assertIn("add --supersede", out)
        code, out = self.cli("reject", self.keys[0], "--by", "maya", "--note", "customer paused", "--supersede")
        self.assertEqual(code, 0)
        self.assertIn("(supersedes #1)", out)
        events = review._sql(self.ledger, "SELECT decision, reviewer, supersedes FROM reviews ORDER BY review_id")
        self.assertEqual(events, [{"decision": "approved", "reviewer": "thais", "supersedes": None},
                                  {"decision": "rejected", "reviewer": "maya", "supersedes": 1}])
        self.assertEqual(review.status(review.latest(self.ledger, self.keys[0])), "rejected")

    def test_supersede_without_a_decision_is_refused(self):
        code, out = self.cli("approve", self.keys[2], "--by", "thais", "--supersede")
        self.assertEqual(code, 1)
        self.assertIn("nothing to supersede", out)


class TestAppendOnly(_Queue):
    def test_events_cannot_be_edited_or_deleted(self):
        self.cli("approve", self.keys[0], "--by", "thais")
        for statement in ("UPDATE reviews SET decision = 'rejected'", "DELETE FROM reviews"):
            with self.assertRaises(sqlite3.IntegrityError) as refused:
                review._sql(self.ledger, statement)
            self.assertIn("append-only", str(refused.exception))
        self.assertEqual(review._sql(self.ledger, "SELECT decision FROM reviews"), [{"decision": "approved"}])

    def test_the_chain_cannot_branch_even_if_two_writers_race(self):
        insert = ("INSERT INTO reviews (idempotency_key, decision, reviewer, supersedes, decided_at) "
                  "VALUES (?, 'approved', 'x', ?, 't')")
        review._sql(self.ledger, insert, (self.keys[0], None))
        with self.assertRaises(sqlite3.IntegrityError):
            review._sql(self.ledger, insert, (self.keys[0], None))  # a second "first" decision
        review._sql(self.ledger, insert, (self.keys[0], 1))
        with self.assertRaises(sqlite3.IntegrityError):
            review._sql(self.ledger, insert, (self.keys[0], 1))  # a second successor of #1


class TestConsoleShowsReviewsReadOnly(_Queue):
    def test_reviews_page(self):
        status, html = console.render_reviews(self.ledger, False)  # before any review table exists
        self.assertEqual((status, html.count("Awaiting review")), (200, 3))
        self.assertEqual(unattended._sql(self.ledger, "SELECT name FROM sqlite_master WHERE name = 'reviews'"), [])
        self.cli("approve", self.keys[0], "--by", "thais", "--note", "a|b")
        html = console.render_reviews(self.ledger, False)[1]
        self.assertIn("approved, not applied: no applier exists by design", html)
        self.assertIn(": a|b", html)
        for control in ("<form", "method=", "fetch(", "xmlhttprequest"):  # the page can only show decisions
            self.assertNotIn(control, html.lower())


if __name__ == "__main__":
    unittest.main()
