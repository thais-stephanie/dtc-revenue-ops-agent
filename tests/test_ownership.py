"""Account-manager ownership: fixed, complete, and invisible to the agent.

Ownership is presentation context for the console. These tests prove it is
deterministic and that nothing the agent sees, decides or is scored on can
depend on it.
"""
from __future__ import annotations

import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from revenue_agent import dataset, signals  # noqa: E402
from revenue_agent.agent import AgentRunner  # noqa: E402
from revenue_agent.evaluation import run_case  # noqa: E402
from revenue_agent.repository import InMemoryRepository  # noqa: E402
from revenue_agent.testing import ScriptedClient  # noqa: E402

REPO = InMemoryRepository()
#: the deterministic shortlist this dataset has always produced
SHORTLIST = ["field_foundry", "morrow_goods", "evergreen_labs", "cinder_supply",
             "harbor_home", "luna_pantry", "atlas_ivy", "northstar_naturals"]
AGENT_PATH = ["agent", "tools", "signals", "guardrails", "repository", "evaluation", "unattended",
              "prompts", "confidence", "reconciliation", "daily", "forensics", "schemas", "domain"]


class TestPortfolios(unittest.TestCase):
    def test_every_account_has_exactly_one_owner(self):
        owned = [a for accounts in dataset.PORTFOLIOS.values() for a in accounts]
        self.assertEqual(len(owned), 30)
        self.assertEqual(len(set(owned)), 30)
        self.assertEqual(set(owned), set(REPO.list_account_ids()))
        for account_id in REPO.list_account_ids():
            owner = dataset.account_manager(account_id)
            self.assertEqual(set(owner), {"owner_id", "owner_name", "owner_role"})
            self.assertEqual(owner["owner_role"], "Account Manager")

    def test_three_managers_ten_accounts_each(self):
        self.assertEqual(dataset.ACCOUNT_MANAGERS, {"am_sofia": "Sofia Ramos", "am_jordan": "Jordan Lee",
                                                    "am_alex": "Alex Chen"})
        self.assertEqual({o: len(a) for o, a in dataset.PORTFOLIOS.items()},
                         {"am_sofia": 10, "am_jordan": 10, "am_alex": 10})

    def test_the_intended_portfolios(self):
        self.assertEqual({o: a[:4] for o, a in dataset.PORTFOLIOS.items()}, {  # the planted situations
            "am_sofia": ("field_foundry", "atlas_ivy", "luna_pantry", "golden_finch"),
            "am_jordan": ("evergreen_labs", "cinder_supply", "northstar_naturals", "brighttrail_gear"),
            "am_alex": ("morrow_goods", "harbor_home", "cedar_lane", "ember_oak"),
        })

    def test_every_book_is_a_mix_not_a_category(self):
        scanned = signals.scan(REPO)
        kinds = {"growth": {"cart_recovery_gap", "reactivation_gap"},
                 "relationship": {"engagement_gap", "performance_drop", "creative_fatigue"},
                 "data": {"stale_data", "attribution_exception"}, "billing": {"billing_hold"}}
        planted_types = {s.signal_type.value for a in dataset.SEEDED for s in scanned[a]}
        for owner, accounts in dataset.PORTFOLIOS.items():
            types = {s.signal_type.value for a in accounts for s in scanned[a]}
            present = {k for k, v in kinds.items() if v & types}
            self.assertGreaterEqual(len(present), 3, (owner, present))  # several kinds of situation
            self.assertIn("growth", present, owner)
            self.assertTrue(types, owner)  # never zero meaningful scenarios
            self.assertLess(types, planted_types, owner)  # never all of them
            self.assertGreaterEqual(sum(1 for a in accounts if not scanned[a]), 4, owner)  # quiet accounts too
        shortlisted = {e["account_id"] for e in signals.shortlist(REPO)}
        for owner, accounts in dataset.PORTFOLIOS.items():
            self.assertIn(len(shortlisted & set(accounts)), (2, 3), owner)  # attention spread, not concentrated

    def test_ownership_is_stable_between_runs(self):
        again = InMemoryRepository()
        self.assertEqual(REPO.list_account_ids(), again.list_account_ids())
        self.assertEqual([dataset.account_manager(a) for a in REPO.list_account_ids()],
                         [dataset.account_manager(a) for a in again.list_account_ids()])

    def test_new_names_do_not_collide_with_the_crm_owner_names(self):
        first_names = {name.split()[0] for name in dataset.ACCOUNT_MANAGERS.values()}
        self.assertFalse(first_names & set(dataset.OWNERS))


class TestOwnershipCannotChangeTheAgent(unittest.TestCase):
    def test_nothing_in_the_agent_path_reads_ownership(self):
        for module in AGENT_PATH:
            text = (ROOT / "src" / "revenue_agent" / f"{module}.py").read_text(encoding="utf-8")
            for symbol in ("PORTFOLIOS", "ACCOUNT_MANAGERS", "account_manager(", "owner_id", "owner_name"):
                self.assertNotIn(symbol, text, f"{module}.py references {symbol}")

    def test_the_shortlist_is_unchanged(self):
        self.assertEqual([e["account_id"] for e in signals.shortlist(REPO)], SHORTLIST)

    def test_what_the_agent_sees_carries_no_ownership(self):
        result = AgentRunner(REPO, ScriptedClient()).run()
        blob = repr([e["result"] for e in result.tool_log]) + repr(result.shortlist_supplied)
        for marker in ("owner_id", "owner_name", "Sofia", "Jordan Lee", "Alex Chen", "am_"):
            self.assertNotIn(marker, blob)

    def test_recommendations_and_gate_decisions_are_unchanged(self):
        result = AgentRunner(REPO, ScriptedClient()).run()
        self.assertEqual([(p.recommendation.account_id, p.recommendation.recommended_action.value, p.confidence)
                          for p in result.published],
                         [("field_foundry", "PREPARE_ACCOUNT_REVIEW", "high"),
                          ("morrow_goods", "INVESTIGATE_BEFORE_EXPANSION", "low"),
                          ("evergreen_labs", "INVESTIGATE_ATTRIBUTION", "low")])
        self.assertEqual(result.rejected, [])

    def test_every_eval_case_still_passes(self):
        cases = yaml.safe_load((ROOT / "evals" / "cases.yaml").read_text())
        repo = InMemoryRepository()
        results = [run_case(c, repo, lambda: ScriptedClient(), live=False) for c in cases]
        self.assertEqual([(r["id"], r["passed"]) for r in results], [(c["id"], True) for c in cases])

    def test_publication_identity_has_no_owner(self):
        source = (ROOT / "src" / "revenue_agent" / "unattended.py").read_text(encoding="utf-8")
        identity = re.search(r"identity = \[(.*?)\]", source, re.S).group(1)
        self.assertNotIn("owner", identity)


if __name__ == "__main__":
    unittest.main()
