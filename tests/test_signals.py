"""Portfolio composition and shortlist priority.

The dataset has 9 planted situations and 21 ordinary background accounts, and
the daily shortlist is capped at 8. That cap is the point: a planted situation
is not guaranteed a slot, it has to earn one against the others.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from revenue_agent import config, signals  # noqa: E402
from revenue_agent.dataset import BACKGROUND, SEEDED  # noqa: E402
from revenue_agent.repository import InMemoryRepository  # noqa: E402

REPO = InMemoryRepository()
RANKED = signals.shortlist(REPO, limit=100)
DAILY = signals.shortlist(REPO)


class TestPortfolioComposition(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(len(SEEDED), 9)
        self.assertEqual(len(BACKGROUND), 21)
        self.assertEqual(len(REPO.list_account_ids()), 30)

    def test_ordinary_background_accounts_are_quiet(self):
        noisy = {e["account_id"] for e in RANKED} & set(BACKGROUND)
        # only the control account carrying a planted CRM note may appear
        self.assertEqual(noisy, {"golden_finch"})

    def test_daily_shortlist_respects_the_cap(self):
        self.assertEqual(len(DAILY), config.MAX_SHORTLIST)
        self.assertLess(len(DAILY), len(RANKED))


class TestPriority(unittest.TestCase):
    def test_one_planted_situation_is_deliberately_left_out(self):
        """BrightTrail's creative fatigue is severity 3 and loses to severity 4+.

        This is the behaviour to keep, not to fix: it proves the shortlist is
        prioritising rather than passing everything through. The eval suite
        still exercises that account by handing the agent just that entry.
        """
        in_daily = {e["account_id"] for e in DAILY}
        left_out = set(SEEDED) - in_daily
        self.assertEqual(left_out, {"brighttrail_gear"})

        excluded = next(e for e in RANKED if e["account_id"] == "brighttrail_gear")
        weakest_included = min(e["top_severity"] for e in DAILY)
        self.assertLess(excluded["top_severity"], weakest_included)

    def test_severity_then_signal_count_decides_the_order(self):
        keys = [(-e["top_severity"], -e["signal_count"], e["account_id"]) for e in DAILY]
        self.assertEqual(keys, sorted(keys))

    def test_only_morrow_carries_genuine_ambiguity(self):
        competing = {e["account_id"] for e in RANKED if e["competing_signals"]}
        self.assertEqual(competing, {"morrow_goods"})


if __name__ == "__main__":
    unittest.main()
