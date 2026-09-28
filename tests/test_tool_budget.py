"""Reading and proposing have separate, fail-closed budgets.

One shared per-account budget of 4 made the contract impossible: four reads
exhausted it and left no room for the task proposal. Cinder v2 hit exactly
that. The fix is not a bigger shared number; it is two capabilities.
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_agent import config, signals  # noqa: E402
from revenue_agent.agent import AgentRunner  # noqa: E402
from revenue_agent.evaluation import model_checks  # noqa: E402
from revenue_agent.forensics import tool_metrics  # noqa: E402
from revenue_agent.repository import InMemoryRepository  # noqa: E402
from revenue_agent.testing import ReplayClient  # noqa: E402
from revenue_agent.tools import BudgetExceeded, ToolRuntime  # noqa: E402

REPO = InMemoryRepository()
CINDER = next(e for e in signals.shortlist(REPO, limit=100) if e["account_id"] == "cinder_supply")
RANKED = signals.shortlist(REPO, limit=100)  # direct runtime tests: every account shortlisted
REPLAY = json.loads((ROOT / "tests" / "fixtures" / "cinder_v2_replay.json").read_text())
CASES = {c["id"]: c for c in __import__("yaml").safe_load((ROOT / "evals" / "cases.yaml").read_text())}

READS = (
    ("get_account_context", {}),
    ("get_commerce_context", {}),
    ("get_campaign_performance", {"days": 30}),
    ("get_recent_account_activity", {"limit": 5}),
)


def do(runtime, tool, account="cinder_supply", **extra):
    return runtime.execute(tool, {"account_id": account, **extra})


def four_reads(runtime, account="cinder_supply"):
    for tool, extra in READS:
        do(runtime, tool, account, **extra)


def propose(runtime, account="cinder_supply", title="Confirm Q4 timing"):
    return do(runtime, "propose_crm_task", account, title=title, description="Drafted in a test.")


class TestSplitBudget(unittest.TestCase):
    def test_the_contract_is_four_reads_and_one_proposal(self):
        self.assertEqual(config.MAX_READ_TOOL_CALLS_PER_ACCOUNT, 4)
        self.assertEqual(config.MAX_TASK_PROPOSALS_PER_ACCOUNT, 1)
        self.assertFalse(hasattr(config, "MAX_TOOL_CALLS_PER_ACCOUNT"))

    def test_four_reads_are_allowed(self):
        runtime = ToolRuntime(REPO, RANKED)
        four_reads(runtime)
        self.assertEqual([e["status"] for e in runtime.tool_log], ["ok"] * 4)

    def test_a_fifth_read_is_refused(self):
        runtime = ToolRuntime(REPO, RANKED)
        four_reads(runtime)
        with self.assertRaises(BudgetExceeded):
            do(runtime, "get_account_context")
        self.assertEqual(runtime.tool_log[-1]["status"], "refused")

    def test_four_reads_and_one_proposal_are_allowed(self):
        runtime = ToolRuntime(REPO, RANKED)
        four_reads(runtime)
        result = propose(runtime)
        self.assertEqual(result["status"], "PROPOSED")
        self.assertEqual([e["status"] for e in runtime.tool_log], ["ok"] * 5)

    def test_a_second_proposal_is_refused(self):
        runtime = ToolRuntime(REPO, RANKED)
        propose(runtime)
        with self.assertRaises(BudgetExceeded) as ctx:
            propose(runtime, title="A different task")
        self.assertIn("task proposal budget exhausted", str(ctx.exception))

    def test_the_proposal_allowance_cannot_buy_extra_reads(self):
        runtime = ToolRuntime(REPO, RANKED)
        four_reads(runtime)
        result = propose(runtime)
        # a proposal carries no account facts: it is not a read channel
        self.assertEqual(set(result), {"source_ref", "proposed_task_id", "status", "note"})
        with self.assertRaises(BudgetExceeded):
            do(runtime, "get_recent_account_activity", limit=5)
        # and proposing first does not enlarge the read allowance either
        runtime = ToolRuntime(REPO, RANKED)
        propose(runtime)
        four_reads(runtime)
        with self.assertRaises(BudgetExceeded):
            do(runtime, "get_account_context")

    def test_refused_calls_are_logged(self):
        runtime = ToolRuntime(REPO, RANKED)
        four_reads(runtime)
        propose(runtime)
        for tool in ("get_account_context", "propose_crm_task"):
            with self.assertRaises(BudgetExceeded):
                do(runtime, tool, title="x", description="y")
        refused = [e for e in runtime.tool_log if e["status"] == "refused"]
        self.assertEqual([e["tool"] for e in refused], ["get_account_context", "propose_crm_task"])
        self.assertTrue(all(e["error"] and e["result"] is None for e in refused))

    def test_budgets_do_not_leak_between_accounts(self):
        runtime = ToolRuntime(REPO, RANKED)
        four_reads(runtime, "cinder_supply")
        propose(runtime, "cinder_supply")
        four_reads(runtime, "atlas_ivy")
        propose(runtime, "atlas_ivy")
        self.assertTrue(all(e["status"] == "ok" for e in runtime.tool_log))
        with self.assertRaises(BudgetExceeded):
            do(runtime, "get_account_context", "atlas_ivy")

    def test_the_run_cap_still_counts_every_attempt(self):
        runtime = ToolRuntime(REPO, RANKED)
        for account in ("cinder_supply", "atlas_ivy", "luna_pantry"):
            four_reads(runtime, account)
        do(runtime, "get_account_context", "harbor_home")
        do(runtime, "get_commerce_context", "harbor_home")
        self.assertEqual(runtime.total_calls, config.MAX_TOOL_CALLS_PER_RUN)
        with self.assertRaises(BudgetExceeded) as ctx:
            do(runtime, "get_recent_account_activity", "harbor_home", limit=5)
        self.assertIn("run tool budget exhausted", str(ctx.exception))


class TestTheModelSeesBudgetErrors(unittest.TestCase):
    def test_a_refusal_reaches_the_model_as_a_tool_result(self):
        reads = [(tool, {"account_id": "cinder_supply", **extra}) for tool, extra in READS]
        client = ReplayClient([reads + [reads[0]], []])
        AgentRunner(REPO, client).run(shortlist=[CINDER])
        results = client.received[1][-1]["content"]  # what turn 2 was sent
        payloads = [json.loads(r["content"]) for r in results]
        self.assertTrue(all("error" not in p for p in payloads[:4]))
        self.assertIn("read tool budget exhausted for cinder_supply", payloads[4]["error"])

    def test_submit_consumes_no_budget(self):
        result = AgentRunner(REPO, ReplayClient(REPLAY["turns"])).run(shortlist=[CINDER])
        metrics = tool_metrics(result, "cinder_supply")
        self.assertEqual(metrics["terminal_submits"], 1)
        self.assertEqual(metrics["total_tool_attempts"], 5)  # submit is not among them
        self.assertEqual(metrics["refused_tool_calls"], 0)


class TestCinderV2ToolSequenceReplay(unittest.TestCase):
    """v2's 4 reads + 1 proposal: refused under the old budget, valid under the split."""

    def setUp(self):
        self.result = AgentRunner(REPO, ReplayClient(REPLAY["turns"])).run(shortlist=[CINDER])

    def test_every_call_succeeds_including_the_proposal(self):
        log = self.result.tool_log
        self.assertEqual([e["tool"] for e in log][-1], "propose_crm_task")
        self.assertEqual([e["status"] for e in log], ["ok"] * 5)
        self.assertTrue(log[-1]["source_refs_returned"][0].startswith("proposed_task:task_"))

    def test_the_cinder_budget_checks_pass_on_the_migrated_case(self):
        checks = {
            c["name"]: c
            for c in model_checks(
                CASES["customer_paused_expansion"]["expected"], "cinder_supply", self.result
            )
        }
        self.assertTrue(checks["max_read_calls"]["passed"], checks["max_read_calls"])
        self.assertTrue(checks["max_task_proposals"]["passed"], checks["max_task_proposals"])
        self.assertNotIn("max_tool_calls", checks)

    def test_over_budget_attempts_still_fail_the_case(self):
        """Attempts count, refused or not: trying to exceed the budget is a violation."""
        reads = [(tool, {"account_id": "cinder_supply", **extra}) for tool, extra in READS]
        over = AgentRunner(REPO, ReplayClient([reads + [reads[0]], []])).run(shortlist=[CINDER])
        checks = {
            c["name"]: c
            for c in model_checks(
                CASES["customer_paused_expansion"]["expected"], "cinder_supply", over
            )
        }
        self.assertFalse(checks["max_read_calls"]["passed"])
        self.assertIn("attempted=5 ok=4", checks["max_read_calls"]["detail"])

    def test_a_second_proposal_attempt_fails_the_case(self):
        task = {"account_id": "cinder_supply", "title": "t", "description": "d"}
        over = AgentRunner(
            REPO,
            ReplayClient([[("propose_crm_task", task), ("propose_crm_task", {**task, "title": "u"})], []]),
        ).run(shortlist=[CINDER])
        checks = {
            c["name"]: c
            for c in model_checks(
                CASES["customer_paused_expansion"]["expected"], "cinder_supply", over
            )
        }
        self.assertFalse(checks["max_task_proposals"]["passed"])
        self.assertIn("attempted=2 ok=1", checks["max_task_proposals"]["detail"])


if __name__ == "__main__":
    unittest.main()
