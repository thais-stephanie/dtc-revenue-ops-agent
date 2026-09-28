"""The publication gate fails closed, and confidence is never the model's opinion."""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from revenue_agent import guardrails  # noqa: E402
from revenue_agent.agent import AgentRunner  # noqa: E402
from revenue_agent.confidence import derive_confidence  # noqa: E402
from revenue_agent.domain import Action, Confidence, ReconciliationStatus  # noqa: E402
from revenue_agent.repository import InMemoryRepository  # noqa: E402
from revenue_agent.schemas import Recommendation  # noqa: E402
from revenue_agent.testing import ScriptedClient  # noqa: E402

REPO = InMemoryRepository()


def recommendation(account_id: str, action: Action, refs=("account_context:x",)) -> Recommendation:
    return Recommendation(
        account_id=account_id,
        priority="high",
        category="growth",
        headline="A headline long enough to pass validation",
        rationale="A rationale long enough to pass validation of the schema.",
        recommended_action=action,
        source_refs=list(refs),
    )


class TestPublicationGate(unittest.TestCase):
    def test_unknown_source_reference_is_rejected(self):
        facts = REPO.account_facts("atlas_ivy")
        ok, rejection = guardrails.gate_recommendation(
            recommendation("atlas_ivy", Action.EXPAND_CART_RECOVERY, ["metric_847"]),
            facts=facts,
            available_source_refs={"account_context:atlas_ivy"},
            signal_severity=4,
            competing_signals=False,
            source_ref_provenance={},
        )
        self.assertIsNone(ok)
        self.assertEqual(rejection.reason, "unknown_source_reference")

    def test_expansion_is_blocked_when_reconciliation_is_an_exception(self):
        facts = REPO.account_facts("evergreen_labs")
        ok, rejection = guardrails.gate_recommendation(
            recommendation("evergreen_labs", Action.EXPAND_CART_RECOVERY, ["r"]),
            facts=facts,
            available_source_refs={"r"},
            signal_severity=5,
            competing_signals=False,
            source_ref_provenance={},
        )
        self.assertIsNone(ok)
        self.assertEqual(rejection.reason, "expansion_blocked_by_reconciliation")

    def test_investigation_is_allowed_on_the_same_blocked_account(self):
        facts = REPO.account_facts("evergreen_labs")
        ok, rejection = guardrails.gate_recommendation(
            recommendation("evergreen_labs", Action.INVESTIGATE_ATTRIBUTION, ["r"]),
            facts=facts,
            available_source_refs={"r"},
            signal_severity=5,
            competing_signals=False,
            source_ref_provenance={},
        )
        self.assertIsNone(rejection)
        self.assertEqual(ok.confidence, Confidence.LOW.value)

    def test_expansion_is_blocked_on_stale_commerce_data(self):
        facts = REPO.account_facts("harbor_home")
        ok, rejection = guardrails.gate_recommendation(
            recommendation("harbor_home", Action.LAUNCH_REACTIVATION, ["r"]),
            facts=facts,
            available_source_refs={"r"},
            signal_severity=4,
            competing_signals=False,
            source_ref_provenance={},
        )
        self.assertIsNone(ok)
        self.assertEqual(rejection.reason, "expansion_blocked_by_stale_data")

    def test_blocked_actions_are_empty_for_a_clean_account(self):
        self.assertEqual(guardrails.blocked_actions(REPO.account_facts("atlas_ivy")), set())


class TestDerivedConfidence(unittest.TestCase):
    def test_clean_strong_signal_is_high(self):
        self.assertIs(
            derive_confidence(
                signal_severity=4,
                freshness_hours=4,
                reconciliation_status=ReconciliationStatus.MATCH,
            ),
            Confidence.HIGH,
        )

    def test_competing_signals_drag_confidence_to_low(self):
        self.assertIs(
            derive_confidence(
                signal_severity=5,
                freshness_hours=4,
                reconciliation_status=ReconciliationStatus.MATCH,
                conflicting_context=True,
            ),
            Confidence.LOW,
        )

    def test_probable_match_is_medium_not_a_block(self):
        self.assertIs(
            derive_confidence(
                signal_severity=4,
                freshness_hours=4,
                reconciliation_status=ReconciliationStatus.PROBABLE_MATCH,
            ),
            Confidence.MEDIUM,
        )


class TestRunLevelGuards(unittest.TestCase):
    def test_invalid_schema_publishes_nothing(self):
        result = AgentRunner(REPO, ScriptedClient(inject={"bad_schema": True})).run()
        self.assertEqual(result.published, [])
        self.assertEqual(result.rejected[0].reason, "schema_validation_failed")

    def test_account_that_was_never_shortlisted_is_rejected(self):
        client = ScriptedClient(max_accounts=1, inject={"extra_account": True})
        result = AgentRunner(REPO, client).run()
        reasons = {r.reason for r in result.rejected}
        self.assertIn("account_not_shortlisted", reasons)

    def test_an_instruction_inside_a_crm_note_is_not_followed(self):
        entry = next(
            e for e in __import__("revenue_agent.signals", fromlist=["x"]).shortlist(REPO, limit=100)
            if e["account_id"] == "golden_finch"
        )
        result = AgentRunner(REPO, ScriptedClient(max_accounts=1)).run(shortlist=[entry])
        text = " ".join(p.recommendation.headline for p in result.published).lower()
        self.assertNotIn("400%", text)
        self.assertNotIn("top priority", text)

    def test_the_agent_never_writes_only_proposes(self):
        result = AgentRunner(REPO, ScriptedClient()).run()
        for item in result.published:
            if item.recommendation.proposed_task_id:
                self.assertTrue(item.recommendation.proposed_task_id.startswith("task_"))
        # nothing in the pipeline can mark a task approved without the API
        self.assertFalse(hasattr(AgentRunner, "approve_task"))


if __name__ == "__main__":
    unittest.main()
