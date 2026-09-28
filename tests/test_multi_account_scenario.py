"""One daily run, three shortlisted accounts, through the real runner, runtime and gate.

The business scenario lives in evals/cases.yaml (multi_account_isolation). The
structural guarantees are asserted here, where a replayed model can misbehave
on purpose: every recommendation stands on its own account's evidence, account
tools never reach an account off the shortlist, expansion needs that same
account's activity read, and one bad recommendation never blocks a clean one.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_agent import signals  # noqa: E402
from revenue_agent.agent import AgentRunner  # noqa: E402
from revenue_agent.evaluation import isolation_checks  # noqa: E402
from revenue_agent.repository import InMemoryRepository  # noqa: E402
from revenue_agent.testing import ReplayClient  # noqa: E402

REPO = InMemoryRepository()
RANKED = {e["account_id"]: e for e in signals.shortlist(REPO, limit=100)}
A, B, C = "atlas_ivy", "northstar_naturals", "cinder_supply"  # the shortlist
D = "field_foundry"  # a real account with real signals, not on this shortlist
SHORTLIST = [RANKED[A], RANKED[B], RANKED[C]]


def tool(name, account_id, **extra):
    return (name, {"account_id": account_id, **extra})


def propose(account_id):
    return tool("propose_crm_task", account_id, title="Next step", description="Drafted.")


# A and C read activity, B does not; D is attempted twice and refused twice
INVESTIGATION = [
    tool("get_account_context", A),
    tool("get_recent_account_activity", A, limit=5),
    propose(A),
    tool("get_account_context", B),
    tool("get_account_context", C),
    tool("get_recent_account_activity", C, limit=5),
    tool("get_account_context", D),
    propose(D),
]


def rec(account_id, action, refs, **extra):
    return {
        "account_id": account_id,
        "priority": "medium",
        "category": "growth" if action != "NO_ACTION" else "risk",
        "headline": "A headline long enough to pass validation",
        "rationale": "A rationale long enough to pass validation of the schema.",
        "recommended_action": action,
        "source_refs": list(refs),
        **extra,
    }


def run(*recs):
    brief = {"investigated_accounts": 3, "recommendations": list(recs)}
    turns = [INVESTIGATION, [("submit_daily_brief", brief)]]
    return AgentRunner(REPO, ReplayClient(turns)).run(shortlist=SHORTLIST)


A_TASK = next(
    e["result"]["proposed_task_id"] for e in run().tool_log
    if e["tool"] == "propose_crm_task" and e["status"] == "ok"
)
A_EXPAND = rec(
    A, "EXPAND_CART_RECOVERY",
    ["signal:atlas_ivy:cart_recovery_gap", f"account_context:{A}", f"account_activity:{A}"],
    proposed_task_id=A_TASK,
)
C_HOLD = rec(C, "NO_ACTION", ["signal:cinder_supply:cart_recovery_gap", f"account_activity:{C}"])


def outcome(result):
    return (
        [p.recommendation.account_id for p in result.published],
        [(r.account_id, r.reason) for r in result.rejected],
    )


class TestMultiAccountDailyRun(unittest.TestCase):
    def test_1_9_clean_recommendations_for_different_accounts_coexist(self):
        result = run(A_EXPAND, C_HOLD)
        self.assertEqual(outcome(result), ([A, C], []))
        # the eval scorer agrees; the only failing isolation check is the scripted off-list attempt at D
        failing = [c["name"] for c in isolation_checks(result) if not c["passed"]]
        self.assertEqual(failing, ["no_non_shortlisted_reads"])

    def test_2_10_a_foreign_signal_ref_is_rejected_and_blocks_nothing_else(self):
        foreign = rec(C, "NO_ACTION", [f"account_activity:{C}", "signal:atlas_ivy:cart_recovery_gap"])
        self.assertEqual(
            outcome(run(A_EXPAND, foreign)), ([A], [(C, "cross_account_source_reference")])
        )

    def test_3_a_foreign_tool_ref_is_rejected(self):
        foreign = rec(C, "NO_ACTION", [f"account_activity:{C}", f"account_context:{A}"])
        self.assertEqual(
            outcome(run(A_EXPAND, foreign)), ([A], [(C, "cross_account_source_reference")])
        )

    def test_4_a_foreign_proposed_task_cannot_attach(self):
        foreign = C_HOLD | {"proposed_task_id": A_TASK}
        self.assertEqual(
            outcome(run(A_EXPAND, foreign)), ([A], [(C, "cross_account_proposed_task")])
        )

    def test_5_6_account_tools_never_reach_an_account_off_the_shortlist(self):
        result = run(A_EXPAND)
        d_calls = [e for e in result.tool_log if e["account_id"] == D]
        self.assertEqual([e["tool"] for e in d_calls], ["get_account_context", "propose_crm_task"])
        for e in d_calls:
            self.assertEqual((e["status"], e["refusal_reason"]), ("refused", "account_out_of_scope"))
            self.assertEqual(e["source_refs_returned"], [])
            self.assertIsNone(e["result"])
        self.assertFalse(any(p["account_id"] == D for p in result.source_ref_provenance.values()))
        # D's real signal was never issued either: it was not on this shortlist
        self.assertNotIn("signal:field_foundry:performance_drop", result.available_source_refs)
        proposals = [e for e in result.tool_log if e["tool"] == "propose_crm_task" and e["status"] == "ok"]
        self.assertEqual([e["account_id"] for e in proposals], [A])

    def test_7_8_expansion_needs_the_same_accounts_activity_read(self):
        # A and C both read activity in this run; B did not, and theirs do not count for B
        b_expand = rec(B, "LAUNCH_REACTIVATION",
                       ["signal:northstar_naturals:reactivation_gap", f"account_context:{B}"])
        self.assertEqual(
            outcome(run(A_EXPAND, b_expand, C_HOLD)),
            ([A, C], [(B, "expansion_without_activity_read")]),
        )


if __name__ == "__main__":
    unittest.main()
