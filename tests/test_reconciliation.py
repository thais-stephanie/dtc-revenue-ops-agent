"""Tolerance bands. These were fixed before any agent run and are not tuned.

Run with either:  python -m unittest discover -s tests   |   pytest
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from revenue_agent.domain import ReconciliationStatus  # noqa: E402
from revenue_agent.reconciliation import classify_revenue_difference  # noqa: E402


class TestReconciliation(unittest.TestCase):
    def test_identical_values_match(self):
        self.assertIs(
            classify_revenue_difference(10_000, 10_000).status, ReconciliationStatus.MATCH
        )

    def test_small_absolute_delta_matches_even_on_a_tiny_account(self):
        # $200 on $1,000 is 20% relative, but below the absolute floor.
        self.assertIs(
            classify_revenue_difference(1_200, 1_000).status, ReconciliationStatus.MATCH
        )

    def test_small_relative_delta_matches_on_a_large_account(self):
        # $2,400 on $100,000 is 2.4%: large in dollars, immaterial in context.
        self.assertIs(
            classify_revenue_difference(102_400, 100_000).status, ReconciliationStatus.MATCH
        )

    def test_probable_band(self):
        result = classify_revenue_difference(75_110, 70_303)
        self.assertIs(result.status, ReconciliationStatus.PROBABLE_MATCH)
        self.assertAlmostEqual(result.relative_delta, 0.0684, places=3)

    def test_exception_requires_both_bands_to_be_exceeded(self):
        result = classify_revenue_difference(24_360, 13_120)
        self.assertIs(result.status, ReconciliationStatus.EXCEPTION)
        self.assertGreater(result.relative_delta, 0.08)
        self.assertGreater(result.absolute_delta, 1_000)

    def test_zero_commerce_revenue_is_an_exception_not_a_crash(self):
        self.assertIs(
            classify_revenue_difference(5_000, 0).status, ReconciliationStatus.EXCEPTION
        )

    def test_both_zero_is_a_match(self):
        self.assertIs(classify_revenue_difference(0, 0).status, ReconciliationStatus.MATCH)

    def test_direction_does_not_matter(self):
        under = classify_revenue_difference(13_120, 24_360).status
        over = classify_revenue_difference(24_360, 13_120).status
        self.assertIs(under, ReconciliationStatus.EXCEPTION)
        self.assertIs(over, ReconciliationStatus.EXCEPTION)


if __name__ == "__main__":
    unittest.main()
