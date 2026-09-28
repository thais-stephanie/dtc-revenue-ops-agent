"""Account isolation: a recommendation for A may cite only evidence issued for A.

Refs used to be citable run-wide, so with A and B both shortlisted, a
recommendation for A could cite B's signal, B's tool result or B's proposed
task and publish. Every issued ref now carries the account it was issued for,
the gate for A receives only A's refs, and a real ref that belongs to another
account is rejected under its own reason, not as "unknown".
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_agent import config, prompts, signals  # noqa: E402
from revenue_agent.agent import AgentRunner  # noqa: E402
from revenue_agent.forensics import NOT_ISSUED, run_forensics  # noqa: E402
from revenue_agent.repository import InMemoryRepository  # noqa: E402
from revenue_agent.testing import ReplayClient  # noqa: E402

REPO = InMemoryRepository()
RANKED = {e["account_id"]: e for e in signals.shortlist(REPO, limit=100)}
A, B, C = "cinder_supply", "morrow_goods", "field_foundry"  # C is never shortlisted here
SHORTLIST = [RANKED[A], RANKED[B]]

A_SIGNAL = "signal:cinder_supply:cart_recovery_gap"
B_SIGNAL = "signal:morrow_goods:performance_drop"
C_SIGNAL = "signal:field_foundry:performance_drop"


def rec(account_id, refs, **extra) -> dict:
    return {
        "account_id": account_id,
        "priority": "medium",
        "category": "risk",
        "headline": "A headline long enough to pass validation",
        "rationale": "A rationale long enough to pass validation of the schema.",
        "recommended_action": "INVESTIGATE_BEFORE_EXPANSION",
        "source_refs": list(refs),
        **extra,
    }


def submit(*recs):
    return [("submit_daily_brief", {"investigated_accounts": 2, "recommendations": list(recs)})]


def read(account_id, tool="get_account_context", **extra):
    return (tool, {"account_id": account_id, **extra})


def propose(account_id):
    return ("propose_crm_task", {"account_id": account_id, "title": "Review", "description": "d"})


def run(*turns):
    return AgentRunner(REPO, ReplayClient(list(turns))).run(shortlist=SHORTLIST)


def task_id(result, account_id):
    return next(
        e["result"]["proposed_task_id"] for e in result.tool_log
        if e["tool"] == "propose_crm_task" and e["account_id"] == account_id and e["status"] == "ok"
    )


# both accounts read and propose, so every kind of B evidence really exists
BOTH_READ_AND_PROPOSE = [read(A), read(B), propose(A), propose(B)]


class TestAccepted(unittest.TestCase):
    def assertPublished(self, result, n=1):
        self.assertEqual(result.rejected, [])
        self.assertEqual(len(result.published), n)

    def test_1_own_shortlist_signal(self):
        self.assertPublished(run(submit(rec(A, [A_SIGNAL]))))

    def test_2_own_successful_tool_ref(self):
        self.assertPublished(run([read(A)], submit(rec(A, [f"account_context:{A}"]))))

    def test_3_own_signal_tool_ref_and_proposed_task(self):
        first = run(BOTH_READ_AND_PROPOSE, submit())
        result = run(
            BOTH_READ_AND_PROPOSE,
            submit(rec(A, [A_SIGNAL, f"account_context:{A}"], proposed_task_id=task_id(first, A))),
        )
        self.assertPublished(result)

    def test_11_both_accounts_citing_only_their_own_refs_both_publish(self):
        result = run(
            BOTH_READ_AND_PROPOSE,
            submit(
                rec(A, [A_SIGNAL, f"account_context:{A}"]),
                rec(B, [B_SIGNAL, f"account_context:{B}"]),
            ),
        )
        self.assertPublished(result, n=2)
        self.assertEqual([p.recommendation.account_id for p in result.published], [A, B])


class TestRejected(unittest.TestCase):
    def assertRejected(self, result, reason, details):
        self.assertEqual(result.published, [])
        (rejection,) = result.rejected
        self.assertEqual((rejection.account_id, rejection.reason), (A, reason))
        self.assertEqual(rejection.details, sorted(details))
        return rejection

    def test_4_b_shortlist_signal_while_both_shortlisted(self):
        self.assertRejected(
            run(submit(rec(A, [A_SIGNAL, B_SIGNAL]))), "cross_account_source_reference", [B_SIGNAL]
        )

    def test_5_b_successful_tool_ref(self):
        ref = f"account_context:{B}"
        self.assertRejected(
            run([read(A), read(B)], submit(rec(A, [f"account_context:{A}", ref]))),
            "cross_account_source_reference", [ref],
        )

    def test_5b_b_proposed_task_ref_cited_as_a_source(self):
        first = run(BOTH_READ_AND_PROPOSE, submit())
        ref = f"proposed_task:{task_id(first, B)}"
        self.assertRejected(
            run(BOTH_READ_AND_PROPOSE, submit(rec(A, [A_SIGNAL, ref]))),
            "cross_account_source_reference", [ref],
        )

    def test_6_b_proposed_task_id_attached(self):
        first = run(BOTH_READ_AND_PROPOSE, submit())
        b_task = task_id(first, B)
        self.assertRejected(
            run(BOTH_READ_AND_PROPOSE, submit(rec(A, [A_SIGNAL], proposed_task_id=b_task))),
            "cross_account_proposed_task", [b_task],
        )

    def test_7_non_shortlisted_c_signal_was_never_issued(self):
        # C really carries this signal; it was not on this run's shortlist
        self.assertIn(C_SIGNAL, [s["source_ref"] for s in RANKED[C]["signals"]])
        self.assertRejected(
            run(submit(rec(A, [A_SIGNAL, C_SIGNAL]))), "unknown_source_reference", [C_SIGNAL]
        )

    def test_7b_a_read_for_non_shortlisted_c_is_refused_so_issues_nothing(self):
        ref = f"account_context:{C}"
        result = run([read(C)], submit(rec(A, [A_SIGNAL, ref])))
        self.assertEqual(result.tool_log[0]["status"], "refused")
        self.assertRejected(result, "unknown_source_reference", [ref])

    def test_8_failed_tool_call_issues_nothing(self):
        # `days` is required: the call fails and no ref is issued
        result = run(
            [read(A, "get_campaign_performance")],
            submit(rec(A, [A_SIGNAL, f"campaign_performance:{A}:30d"])),
        )
        self.assertEqual(result.tool_log[0]["status"], "error")
        self.assertRejected(result, "unknown_source_reference", [f"campaign_performance:{A}:30d"])

    def test_9_refused_tool_call_issues_nothing(self):
        reads = [read(A)] * config.MAX_READ_TOOL_CALLS_PER_ACCOUNT
        result = run(
            reads + [read(A, "get_commerce_context")],
            submit(rec(A, [A_SIGNAL, f"commerce_context:{A}"])),
        )
        self.assertEqual(result.tool_log[-1]["status"], "refused")
        self.assertRejected(result, "unknown_source_reference", [f"commerce_context:{A}"])

    def test_10_invented_ref(self):
        self.assertRejected(
            run(submit(rec(A, [A_SIGNAL, "signal:cinder_supply:made_up"]))),
            "unknown_source_reference", ["signal:cinder_supply:made_up"],
        )

    def test_a_mix_of_unknown_and_cross_account_names_the_cross_account(self):
        self.assertRejected(
            run(submit(rec(A, [B_SIGNAL, "invented"]))),
            "cross_account_source_reference", [B_SIGNAL, "invented"],
        )

    def test_one_bad_recommendation_does_not_block_the_clean_one(self):
        result = run(submit(rec(A, [B_SIGNAL]), rec(B, [B_SIGNAL])))
        self.assertEqual([p.recommendation.account_id for p in result.published], [B])
        self.assertEqual([r.reason for r in result.rejected], ["cross_account_source_reference"])


class TestForensics(unittest.TestCase):
    def test_12_rejection_records_issuing_account_origin_and_reason(self):
        first = run(BOTH_READ_AND_PROPOSE, submit())
        b_task = task_id(first, B)
        result = run(
            BOTH_READ_AND_PROPOSE,
            submit(
                rec(A, [A_SIGNAL, B_SIGNAL, "invented"]),
                rec(A, [A_SIGNAL], proposed_task_id=b_task),
            ),
        )
        refs, task = run_forensics(result, live=False)["rejections"]
        self.assertEqual(refs["account_id"], A)
        self.assertEqual(refs["reason"], "cross_account_source_reference")
        self.assertEqual(
            refs["cited_source_ref_provenance"],
            {
                A_SIGNAL: {"account_id": A, "origin": "shortlist_signal"},
                B_SIGNAL: {"account_id": B, "origin": "shortlist_signal"},
                "invented": NOT_ISSUED,
            },
        )
        self.assertEqual(refs["unknown_source_refs"], ["invented"])
        self.assertEqual(task["reason"], "cross_account_proposed_task")
        self.assertEqual(
            task["proposed_task_provenance"], {"account_id": B, "origin": "tool:propose_crm_task"}
        )


class TestPromptContract(unittest.TestCase):
    def test_the_prompt_states_account_scoped_evidence_and_every_budget(self):
        p = " ".join(prompts.SYSTEM_PROMPT.split())
        self.assertIn("source_ref values are account-scoped", p)
        self.assertIn("Never cite one account's evidence", p)
        self.assertIn("that account's shortlist entry", p)
        self.assertNotIn("must come from a tool result", p)  # the old contradiction
        self.assertIn("Account tools (the get_* tools and propose_crm_task) only work for "
                      "accounts on today's shortlist", p)
        self.assertIn("do not investigate or propose tasks for any other account", p)
        self.assertIn("Read recent account activity before recommending", p)
        for phrase in (
            f"At most {config.MAX_RECOMMENDATIONS} recommendations per run",
            f"at most {config.MAX_READ_TOOL_CALLS_PER_ACCOUNT} read tool attempts",
            f"at most {config.MAX_TASK_PROPOSALS_PER_ACCOUNT} propose_crm_task attempt",
            f"At most {config.MAX_TOOL_CALLS_PER_RUN} tool attempts in total per run",
        ):
            self.assertIn(phrase, p)


if __name__ == "__main__":
    unittest.main()
