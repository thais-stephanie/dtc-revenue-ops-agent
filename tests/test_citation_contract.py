"""The citation contract: a ref is citable if the system issued it this run.

Cinder v2 was rejected for citing two refs our own signal layer had generated
and handed to the model, under the key `source_ref`, on the shortlist. The gate
only knew about tool-returned refs. Now both issuers register what they issue,
with provenance, and the gate stays fail-closed for everything else.
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_agent import signals  # noqa: E402
from revenue_agent.agent import AgentRunner  # noqa: E402
from revenue_agent.forensics import run_forensics  # noqa: E402
from revenue_agent.repository import InMemoryRepository  # noqa: E402
from revenue_agent.testing import ReplayClient, ScriptedClient  # noqa: E402

REPO = InMemoryRepository()
RANKED = signals.shortlist(REPO, limit=100)
CINDER = next(e for e in RANKED if e["account_id"] == "cinder_supply")
REPLAY = json.loads((ROOT / "tests" / "fixtures" / "cinder_v2_replay.json").read_text())

SIGNAL_CART = "signal:cinder_supply:cart_recovery_gap"
SIGNAL_REACTIVATION = "signal:cinder_supply:reactivation_gap"


def cinder(origin: str) -> dict:
    return {"account_id": "cinder_supply", "origin": origin}


def brief(refs, action="INVESTIGATE_BEFORE_EXPANSION", account_id="cinder_supply") -> dict:
    return {
        "investigated_accounts": 1,
        "recommendations": [
            {
                "account_id": account_id,
                "priority": "medium",
                "category": "risk",
                "headline": "A headline long enough to pass validation",
                "rationale": "A rationale long enough to pass validation of the schema.",
                "recommended_action": action,
                "source_refs": list(refs),
            }
        ],
    }


def run(turns, shortlist=(CINDER,)):
    return AgentRunner(REPO, ReplayClient(turns)).run(shortlist=list(shortlist))


def read(tool, **extra):
    return (tool, {"account_id": "cinder_supply", **extra})


def submit(refs, **kw):
    return [("submit_daily_brief", brief(refs, **kw))]


class TestValidRefs(unittest.TestCase):
    def test_a_shortlist_signal_ref_actually_supplied_is_accepted(self):
        result = run([submit([SIGNAL_CART])])
        self.assertEqual(result.rejected, [])
        self.assertEqual(len(result.published), 1)
        self.assertEqual(result.source_ref_provenance[SIGNAL_CART], cinder("shortlist_signal"))

    def test_a_tool_returned_ref_is_accepted(self):
        result = run([[read("get_account_context")], submit(["account_context:cinder_supply"])])
        self.assertEqual(result.rejected, [])
        self.assertEqual(
            result.source_ref_provenance["account_context:cinder_supply"],
            cinder("tool:get_account_context"),
        )

    def test_both_kinds_can_appear_in_one_recommendation(self):
        result = run(
            [
                [read("get_commerce_context")],
                submit([SIGNAL_CART, SIGNAL_REACTIVATION, "commerce_context:cinder_supply"]),
            ]
        )
        self.assertEqual(result.rejected, [])
        cited = run_forensics(result, live=False)["published_recommendations"][0]
        self.assertEqual(
            cited["cited_source_ref_provenance"],
            {
                "commerce_context:cinder_supply": cinder("tool:get_commerce_context"),
                SIGNAL_CART: cinder("shortlist_signal"),
                SIGNAL_REACTIVATION: cinder("shortlist_signal"),
            },
        )


class TestInvalidRefsStayRejected(unittest.TestCase):
    def assertRejectedFor(self, result, unknown):
        self.assertEqual(result.published, [])
        (rejection,) = result.rejected
        self.assertEqual(rejection.reason, "unknown_source_reference")
        self.assertEqual(rejection.details, sorted(unknown))

    def test_a_made_up_signal_ref_is_rejected(self):
        self.assertRejectedFor(
            run([submit([SIGNAL_CART, "signal:cinder_supply:inventory_magic"])]),
            ["signal:cinder_supply:inventory_magic"],
        )

    def test_a_real_signal_type_not_supplied_for_this_account_is_rejected(self):
        # performance_drop is a real signal type; Cinder was not handed one
        self.assertRejectedFor(
            run([submit(["signal:cinder_supply:performance_drop"])]),
            ["signal:cinder_supply:performance_drop"],
        )

    def test_a_tool_shaped_ref_that_was_not_returned_is_rejected(self):
        # the window was 30d; the 7d ref was never issued
        self.assertRejectedFor(
            run(
                [
                    [read("get_campaign_performance", days=30)],
                    submit(["campaign_performance:cinder_supply:7d"]),
                ]
            ),
            ["campaign_performance:cinder_supply:7d"],
        )
        # a tool that was never called issues nothing
        self.assertRejectedFor(
            run([submit(["account_context:cinder_supply"])]),
            ["account_context:cinder_supply"],
        )

    def test_the_scripted_unknown_ref_misbehaviour_is_still_rejected(self):
        result = AgentRunner(
            REPO, ScriptedClient(max_accounts=1, inject={"unknown_ref": True})
        ).run(shortlist=[CINDER])
        self.assertEqual(result.published, [])
        self.assertEqual(result.rejected[0].details, ["campaign_metric_999"])


class TestNoCrossAccountLeak(unittest.TestCase):
    def test_another_accounts_real_signal_is_not_citable_unless_supplied(self):
        # field_foundry really carries performance_drop, but was not on this shortlist
        self.assertTrue(
            any(s["source_ref"] == "signal:field_foundry:performance_drop"
                for e in RANKED if e["account_id"] == "field_foundry" for s in e["signals"])
        )
        result = run([submit(["signal:field_foundry:performance_drop"])])
        self.assertEqual(result.rejected[0].details, ["signal:field_foundry:performance_drop"])

    def test_another_accounts_tool_ref_is_not_citable_unless_returned(self):
        result = run([submit(["account_context:atlas_ivy"])])
        self.assertEqual(result.rejected[0].details, ["account_context:atlas_ivy"])

    def test_only_the_supplied_shortlist_is_issued(self):
        result = run([submit([SIGNAL_CART])])
        self.assertTrue(all(":cinder_supply:" in r for r in result.available_source_refs))
        # the daily shortlist excludes BrightTrail, so its signal is never issued
        daily = AgentRunner(REPO, ScriptedClient()).run()
        self.assertNotIn("signal:brighttrail_gear:creative_fatigue", daily.available_source_refs)
        supplied = {s["source_ref"] for e in daily.shortlist_supplied for s in e["signals"]}
        shortlist_refs = {
            r for r, p in daily.source_ref_provenance.items() if p["origin"] == "shortlist_signal"
        }
        self.assertEqual(shortlist_refs, supplied)


class TestCinderV2Replay(unittest.TestCase):
    """The exact recommendation Claude submitted in Cinder v2, through the new gate."""

    def setUp(self):
        self.result = AgentRunner(REPO, ReplayClient(REPLAY["turns"])).run(shortlist=[CINDER])
        self.record = run_forensics(self.result, live=False)

    def test_fixture_is_the_v2_run(self):
        self.assertEqual(REPLAY["source_sha256"],
                         "cd0dd731f30c6a99fd87ba96320b8f49caa1ed23720ac1de48fb35b2566899bb")
        submitted = REPLAY["turns"][-1][0][1]["recommendations"][0]
        self.assertIn(SIGNAL_CART, submitted["source_refs"])
        self.assertIn(SIGNAL_REACTIVATION, submitted["source_refs"])

    def test_the_v2_recommendation_now_passes_the_source_existence_gate(self):
        self.assertEqual(self.result.rejected, [])
        (published,) = self.record["published_recommendations"]
        self.assertEqual(published["recommended_action"], "INVESTIGATE_BEFORE_EXPANSION")
        self.assertEqual(published["unknown_source_refs"], [])
        self.assertEqual(published["cited_source_ref_provenance"][SIGNAL_CART],
                         cinder("shortlist_signal"))
        self.assertEqual(published["confidence"], "high")


if __name__ == "__main__":
    unittest.main()
