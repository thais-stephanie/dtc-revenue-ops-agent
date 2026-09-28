"""Two rules that used to live only in the prompt, now enforced in code.

1. Account tools (reads and propose_crm_task) serve only accounts on the
   shortlist handed to the model. A call for any other account is refused
   before anything runs, logged as refused (account_out_of_scope), returned to
   the model as a tool error, and counted against the budgets like any other
   refusal.
2. No expansion action publishes for an account unless get_recent_account_activity
   succeeded for that account in this run. The gate enforces that the read
   happened, never how it was interpreted: Cinder still tests interpretation.
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from revenue_agent import config, signals  # noqa: E402
from revenue_agent.agent import AgentRunner  # noqa: E402
from revenue_agent.evaluation import run_case  # noqa: E402
from revenue_agent.repository import InMemoryRepository  # noqa: E402
from revenue_agent.testing import ReplayClient, ScriptedClient  # noqa: E402
from revenue_agent.tools import Refused, ToolRuntime  # noqa: E402

REPO = InMemoryRepository()
RANKED = {e["account_id"]: e for e in signals.shortlist(REPO, limit=100)}
A, B, C = "atlas_ivy", "northstar_naturals", "field_foundry"  # C is never shortlisted here
SHORTLIST = [RANKED[A], RANKED[B]]
CASES = {c["id"]: c for c in yaml.safe_load((ROOT / "evals" / "cases.yaml").read_text())}


def read(account_id, tool="get_account_context", **extra):
    return (tool, {"account_id": account_id, **extra})


def activity(account_id):
    return read(account_id, "get_recent_account_activity", limit=5)


def propose(account_id):
    return ("propose_crm_task", {"account_id": account_id, "title": "T", "description": "d"})


def expand(account_id, refs):
    return {
        "account_id": account_id,
        "priority": "high",
        "category": "growth",
        "headline": "A headline long enough to pass validation",
        "rationale": "A rationale long enough to pass validation of the schema.",
        "recommended_action": "EXPAND_CART_RECOVERY",
        "source_refs": list(refs),
    }


def submit(*recs):
    return [("submit_daily_brief", {"investigated_accounts": 2, "recommendations": list(recs)})]


def run(*turns, shortlist=SHORTLIST):
    client = ReplayClient(list(turns))
    return AgentRunner(REPO, client).run(shortlist=shortlist), client


class TestReadsOnlyForShortlistedAccounts(unittest.TestCase):
    def test_a_read_for_a_non_shortlisted_account_is_refused_and_logged(self):
        runtime = ToolRuntime(REPO, SHORTLIST)
        with self.assertRaises(Refused):
            runtime.execute("get_account_context", {"account_id": C})
        (entry,) = runtime.tool_log
        self.assertEqual((entry["status"], entry["refusal_reason"]),
                         ("refused", "account_out_of_scope"))
        self.assertIn("not on today's shortlist", entry["error"])
        self.assertEqual(entry["source_refs_returned"], [])
        self.assertIsNone(entry["result"])
        self.assertEqual(runtime.refs_for(C), set())

    def test_every_read_tool_is_refused_for_it(self):
        runtime = ToolRuntime(REPO, SHORTLIST)
        for tool, extra in (
            ("get_account_context", {}),
            ("get_campaign_performance", {"days": 30}),
            ("get_commerce_context", {}),
            ("get_recent_account_activity", {"limit": 5}),
        ):
            with self.assertRaises(Refused):
                runtime.execute(tool, {"account_id": C, **extra})
        self.assertEqual({e["status"] for e in runtime.tool_log}, {"refused"})

    def test_the_refusal_counts_against_the_budgets(self):
        runtime = ToolRuntime(REPO, SHORTLIST)
        with self.assertRaises(Refused):
            runtime.execute("get_account_context", {"account_id": C})
        self.assertEqual(runtime.total_calls, 1)
        self.assertEqual(runtime.read_calls_per_account[C], 1)

    def test_the_model_receives_it_as_a_tool_error(self):
        result, client = run([read(C), read(A)], submit())
        payloads = [json.loads(r["content"]) for r in client.received[1][-1]["content"]]
        self.assertIn("not on today's shortlist", payloads[0]["error"])
        self.assertNotIn("error", payloads[1])
        self.assertEqual([e["status"] for e in result.tool_log], ["refused", "ok"])

    def test_shortlisted_accounts_are_served(self):
        runtime = ToolRuntime(REPO, SHORTLIST)
        runtime.execute("get_account_context", {"account_id": A})
        runtime.execute(*propose(A))
        self.assertEqual([e["status"] for e in runtime.tool_log], ["ok", "ok"])
        self.assertEqual(len(runtime.proposed_tasks), 1)

    def test_a_proposal_for_a_non_shortlisted_account_is_refused_and_creates_nothing(self):
        runtime = ToolRuntime(REPO, SHORTLIST)
        with self.assertRaises(Refused):
            runtime.execute(*propose(C))
        (entry,) = runtime.tool_log
        self.assertEqual((entry["status"], entry["refusal_reason"]),
                         ("refused", "account_out_of_scope"))
        self.assertEqual(entry["source_refs_returned"], [])
        self.assertEqual(runtime.proposed_tasks, {})
        self.assertFalse(any(r.startswith("proposed_task:") for r in runtime.available_source_refs))
        # charged like any refused proposal: C's single proposal is now spent
        self.assertEqual(runtime.proposals_per_account[C], 1)
        self.assertEqual(runtime.total_calls, 1)

    def test_out_of_scope_and_budget_refusals_are_told_apart(self):
        runtime = ToolRuntime(REPO, SHORTLIST)
        with self.assertRaises(Refused):
            runtime.execute("get_account_context", {"account_id": C})
        for _ in range(config.MAX_READ_TOOL_CALLS_PER_ACCOUNT):
            runtime.execute("get_account_context", {"account_id": A})
        with self.assertRaises(Refused):
            runtime.execute("get_account_context", {"account_id": A})
        reasons = [e["refusal_reason"] for e in runtime.tool_log]
        self.assertEqual(reasons[0], "account_out_of_scope")
        self.assertEqual(reasons[-1], "budget_exhausted")
        self.assertEqual(set(reasons[1:-1]), {None})


class TestExpansionNeedsAnActivityRead(unittest.TestCase):
    A_REFS = [f"account_context:{A}", "signal:atlas_ivy:cart_recovery_gap"]

    def assertBlocked(self, result):
        self.assertEqual(result.published, [])
        (rejection,) = result.rejected
        self.assertEqual((rejection.account_id, rejection.reason),
                         (A, "expansion_without_activity_read"))

    def test_expansion_without_an_activity_read_is_blocked(self):
        result, _ = run([read(A)], submit(expand(A, self.A_REFS)))
        self.assertBlocked(result)

    def test_expansion_after_a_successful_activity_read_publishes(self):
        result, _ = run([read(A), activity(A)], submit(expand(A, self.A_REFS)))
        self.assertEqual(result.rejected, [])
        self.assertEqual(result.published[0].recommendation.recommended_action.value,
                         "EXPAND_CART_RECOVERY")

    def test_another_accounts_activity_read_does_not_count(self):
        result, _ = run([read(A), activity(B)], submit(expand(A, self.A_REFS)))
        self.assertBlocked(result)

    def test_a_failed_activity_read_does_not_count(self):
        bad = read(A, "get_recent_account_activity", limit="not-a-number")
        result, _ = run([read(A), bad], submit(expand(A, self.A_REFS)))
        self.assertEqual(result.tool_log[-1]["status"], "error")
        self.assertBlocked(result)

    def test_a_and_b_shortlisted_only_the_account_that_read_activity_may_expand(self):
        b_refs = [f"account_context:{B}", "signal:northstar_naturals:reactivation_gap"]
        b_rec = expand(B, b_refs) | {"recommended_action": "LAUNCH_REACTIVATION"}
        result, _ = run(
            [read(A), activity(A), read(B)],
            submit(expand(A, self.A_REFS), b_rec),
        )
        self.assertEqual([p.recommendation.account_id for p in result.published], [A])
        (rejection,) = result.rejected
        self.assertEqual((rejection.account_id, rejection.reason),
                         (B, "expansion_without_activity_read"))

    def test_a_refused_activity_read_does_not_count(self):
        reads = [read(A)] * config.MAX_READ_TOOL_CALLS_PER_ACCOUNT
        result, _ = run(reads + [activity(A)], submit(expand(A, self.A_REFS)))
        self.assertEqual(result.tool_log[-1]["status"], "refused")
        self.assertBlocked(result)

    def test_non_expansion_actions_do_not_need_it(self):
        rec = expand(A, self.A_REFS) | {"recommended_action": "PREPARE_ACCOUNT_REVIEW",
                                        "category": "risk"}
        result, _ = run([read(A)], submit(rec))
        self.assertEqual(result.rejected, [])

    def test_existing_block_reasons_keep_precedence(self):
        # evergreen's reconciliation exception is reported as before
        result, _ = run(
            [read("evergreen_labs")],
            submit(expand("evergreen_labs", ["account_context:evergreen_labs"])),
            shortlist=[RANKED["evergreen_labs"]],
        )
        self.assertEqual(result.rejected[0].reason, "expansion_blocked_by_reconciliation")


class TestCinderStillTestsInterpretation(unittest.TestCase):
    """The guard checks that activity was read. It cannot pass Cinder for the model."""

    def test_reading_the_pause_note_and_expanding_anyway_still_fails_the_case(self):
        case = CASES["customer_paused_expansion"]
        record = run_case(
            case, REPO,
            lambda: ScriptedClient(max_accounts=1, inject={"force_expansion": True}),
            live=False,
        )
        checks = {c["name"]: c for c in record["checks"]}
        # the model read the note, so the guard let the expansion through ...
        self.assertTrue(checks["must_read_activity"]["passed"])
        self.assertEqual(record["actions"], ["EXPAND_CART_RECOVERY"])
        # ... and only the case's interpretation check catches it
        self.assertFalse(checks["no_forbidden_action"]["passed"])
        self.assertFalse(record["passed"])

    def test_the_case_passes_when_the_model_respects_the_pause(self):
        case = CASES["customer_paused_expansion"]
        record = run_case(case, REPO, lambda: ScriptedClient(max_accounts=1), live=False)
        self.assertTrue(record["passed"])


if __name__ == "__main__":
    unittest.main()
