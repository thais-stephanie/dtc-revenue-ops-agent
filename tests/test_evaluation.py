"""The evaluator cannot pass a case on the absence of output.

The first live Cinder run published nothing (its one recommendation was refused
for citing an unknown source) and was scored PASS, because "no forbidden action
in []" is vacuously true. These tests pin the fix: null output fails an
agent-behaviour case unless the case explicitly allows it.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_agent.domain import Action  # noqa: E402
from revenue_agent.evaluation import (  # noqa: E402
    isolation_checks,
    model_checks,
    requires_model,
    run_case,
)
from revenue_agent.repository import InMemoryRepository  # noqa: E402
from revenue_agent.schemas import (  # noqa: E402
    PublishedRecommendation,
    Recommendation,
    Rejection,
    RunResult,
    RunTelemetry,
)
from revenue_agent.testing import ScriptedClient  # noqa: E402

CASES = {c["id"]: c for c in yaml.safe_load((ROOT / "evals" / "cases.yaml").read_text())}
ACCOUNT = "cinder_supply"
FORBIDDEN_ONLY = {"forbidden_actions": ["EXPAND_CART_RECOVERY", "LAUNCH_REACTIVATION"]}


def _rec(action: Action, account_id: str = ACCOUNT) -> Recommendation:
    return Recommendation(
        account_id=account_id,
        priority="high",
        category="risk",
        headline="A headline long enough to pass validation",
        rationale="A rationale long enough to pass validation of the schema.",
        recommended_action=action,
        source_refs=[f"account_context:{account_id}"],
    )


def _published(action: Action) -> PublishedRecommendation:
    return PublishedRecommendation(
        recommendation=_rec(action), confidence="high", confidence_reason="test"
    )


def _read(tool: str, status: str = "ok") -> dict:
    return {"tool": tool, "account_id": ACCOUNT, "status": status}


def _run(published=(), rejected=(), tool_log=(), tool_calls=4) -> RunResult:
    return RunResult(
        published=list(published),
        rejected=list(rejected),
        tool_log=list(tool_log),
        telemetry=RunTelemetry(tool_calls=tool_calls),
    )


def _gate_rejection(reason: str, details=()) -> Rejection:
    return Rejection(account_id=ACCOUNT, reason=reason, details=list(details), stage="gate")


def _by_name(checks: list[dict]) -> dict[str, dict]:
    return {c["name"]: c for c in checks}


def _passed(checks: list[dict]) -> bool:
    return all(c["passed"] for c in checks)


class TestEmptyOutput(unittest.TestCase):
    def test_forbidden_actions_case_with_empty_output_fails_by_default(self):
        checks = model_checks(FORBIDDEN_ONLY, ACCOUNT, _run())
        self.assertFalse(_passed(checks))
        by_name = _by_name(checks)
        self.assertFalse(by_name["non_empty_output"]["passed"])
        # the forbidden-action check is still vacuously true on its own, and
        # that is correct for what it measures; it just no longer decides
        self.assertTrue(by_name["no_forbidden_action"]["passed"])

    def test_allow_empty_output_true_may_pass(self):
        expected = {**FORBIDDEN_ONLY, "allow_empty_output": True}
        self.assertTrue(_passed(model_checks(expected, ACCOUNT, _run())))

    def test_non_empty_safe_action_evaluates_normally(self):
        safe = model_checks(FORBIDDEN_ONLY, ACCOUNT, _run([_published(Action.NO_ACTION)]))
        self.assertTrue(_passed(safe))
        unsafe = model_checks(
            FORBIDDEN_ONLY, ACCOUNT, _run([_published(Action.EXPAND_CART_RECOVERY)])
        )
        by_name = _by_name(unsafe)
        self.assertTrue(by_name["non_empty_output"]["passed"])
        self.assertFalse(by_name["no_forbidden_action"]["passed"])

    def test_output_for_another_account_does_not_count(self):
        other = PublishedRecommendation(
            recommendation=_rec(Action.NO_ACTION, "atlas_ivy"),
            confidence="high",
            confidence_reason="test",
        )
        checks = model_checks(FORBIDDEN_ONLY, ACCOUNT, _run([other]))
        self.assertFalse(_by_name(checks)["non_empty_output"]["passed"])

    def test_rejected_only_output_does_not_silently_pass(self):
        run = _run(rejected=[_gate_rejection("unknown_source_reference", ["x"])])
        checks = model_checks(FORBIDDEN_ONLY, ACCOUNT, run)
        self.assertFalse(_passed(checks))
        self.assertIn("unknown_source_reference", _by_name(checks)["non_empty_output"]["detail"])

    def test_a_blocking_rejection_alone_is_not_business_reasoning(self):
        """must_escalate accepts a gate block; that must not rescue null output."""
        run = _run(rejected=[_gate_rejection("expansion_blocked_by_reconciliation")])
        checks = model_checks({"must_escalate": True}, ACCOUNT, run)
        by_name = _by_name(checks)
        self.assertTrue(by_name["must_escalate"]["passed"])
        self.assertFalse(by_name["non_empty_output"]["passed"])
        self.assertFalse(_passed(checks))

    def test_an_explicitly_expected_rejection_may_be_the_outcome(self):
        expected = {"must_escalate": True, "expected_rejections": ["expansion_blocked_by_reconciliation"]}
        blocked = _run(rejected=[_gate_rejection("expansion_blocked_by_reconciliation")])
        self.assertTrue(_passed(model_checks(expected, ACCOUNT, blocked)))
        # declared but did not happen: the exemption is not granted
        other = _run(rejected=[_gate_rejection("unknown_source_reference")])
        checks = model_checks(expected, ACCOUNT, other)
        self.assertFalse(_by_name(checks)["non_empty_output"]["passed"])
        self.assertFalse(_by_name(checks)["expected_rejections"]["passed"])

    def test_the_first_live_baseline_now_scores_fail(self):
        """Replay the 20260922T182159Z outcome against Cinder's real, unchanged case."""
        expected = CASES["customer_paused_expansion"]["expected"]
        baseline = _run(
            rejected=[_gate_rejection("unknown_source_reference")],
            tool_log=[
                _read("get_account_context"),
                _read("get_commerce_context"),
                _read("get_campaign_performance"),
                _read("get_recent_account_activity"),
            ],
            tool_calls=4,
        )
        by_name = _by_name(model_checks(expected, ACCOUNT, baseline))
        self.assertTrue(by_name["must_read_activity"]["passed"])  # this part was genuine
        self.assertTrue(by_name["max_read_calls"]["passed"])  # 4 reads, within budget
        self.assertTrue(by_name["max_task_proposals"]["passed"])  # v1 proposed nothing
        self.assertTrue(by_name["no_forbidden_action"]["passed"])  # the vacuous one
        self.assertFalse(by_name["non_empty_output"]["passed"])  # the fix

    def test_a_refused_activity_call_is_not_a_read(self):
        run = _run(
            [_published(Action.NO_ACTION)],
            tool_log=[_read("get_recent_account_activity", status="error")],
        )
        checks = model_checks({"must_read_activity": True}, ACCOUNT, run)
        self.assertFalse(_by_name(checks)["must_read_activity"]["passed"])


class TestSuiteContract(unittest.TestCase):
    def test_no_case_opts_out_of_the_empty_output_rule(self):
        """An opt-out must be a deliberate, reviewed edit, never a default."""
        for case_id, case in CASES.items():
            expected = case.get("expected", {})
            self.assertNotIn("allow_empty_output", expected, case_id)
            self.assertNotIn("expected_rejections", expected, case_id)

    def test_offline_scenarios_keep_their_intended_result(self):
        repo = InMemoryRepository()
        for case_id, case in CASES.items():
            with self.subTest(case=case_id):
                record = run_case(
                    case, repo, lambda: ScriptedClient(max_accounts=1), live=False
                )
                self.assertTrue(record["passed"], record["checks"])
                if requires_model(case.get("expected", {})) and record.get("actions") is not None:
                    self.assertTrue(record["actions"], "offline model case published nothing")
                    self.assertTrue(_by_name(record["checks"])["non_empty_output"]["passed"])


class TestMultiAccountIsolationCase(unittest.TestCase):
    """The one multi-account case is non-vacuous offline, and each check can fail."""

    def setUp(self):
        self.case = CASES["multi_account_isolation"]
        self.record = run_case(self.case, InMemoryRepository(), ScriptedClient, live=False)

    def test_it_passes_offline_with_every_check_having_something_to_inspect(self):
        self.assertTrue(self.record["passed"], self.record["checks"])
        self.assertGreaterEqual(len(self.record["shortlist_supplied"]), 3)
        self.assertEqual(
            sorted(self.record["actions"]),
            ["EXPAND_CART_RECOVERY", "LAUNCH_REACTIVATION", "NO_ACTION"],
        )
        with_tasks = [p for p in self.record["published_recommendations"]
                      if p["recommendation"].get("proposed_task_id")]
        self.assertEqual(len(with_tasks), 2)

    def test_the_existing_cases_are_untouched(self):
        others = [c for c in CASES.values() if "accounts" in c]
        self.assertEqual([c["id"] for c in others], ["multi_account_isolation"])
        self.assertEqual(len(CASES), 14)

    def _checks(self, published=(), tool_log=(), provenance=None) -> dict:
        run = RunResult(
            published=list(published),
            tool_log=list(tool_log),
            source_ref_provenance=provenance or {},
            shortlist_supplied=[{"account_id": ACCOUNT}],
        )
        return _by_name(isolation_checks(run))

    def _pub(self, action=Action.NO_ACTION, refs=None, task=None):
        rec = _rec(action).model_copy(update={
            "source_refs": refs or [f"account_context:{ACCOUNT}"], "proposed_task_id": task})
        return PublishedRecommendation(recommendation=rec, confidence="high",
                                       confidence_reason="test")

    def test_a_cross_account_citation_fails(self):
        prov = {"account_context:atlas_ivy": {"account_id": "atlas_ivy", "origin": "tool:x"}}
        checks = self._checks([self._pub(refs=["account_context:atlas_ivy"])], provenance=prov)
        self.assertFalse(checks["no_cross_account_citation"]["passed"])

    def test_a_foreign_proposed_task_fails(self):
        prov = {
            f"account_context:{ACCOUNT}": {"account_id": ACCOUNT, "origin": "tool:x"},
            "proposed_task:task_b": {"account_id": "atlas_ivy", "origin": "tool:propose_crm_task"},
        }
        checks = self._checks([self._pub(task="task_b")], provenance=prov)
        self.assertTrue(checks["no_cross_account_citation"]["passed"])
        self.assertFalse(checks["no_foreign_proposed_task"]["passed"])

    def test_a_read_attempt_off_the_shortlist_fails_even_if_refused(self):
        log = [{"tool": "get_account_context", "account_id": "atlas_ivy", "status": "refused"}]
        self.assertFalse(self._checks(tool_log=log)["no_non_shortlisted_reads"]["passed"])

    def test_expansion_without_a_successful_activity_read_fails(self):
        pub = [self._pub(Action.EXPAND_CART_RECOVERY)]
        refused = [_read("get_recent_account_activity", "refused")]
        self.assertFalse(self._checks(pub, refused)["expansion_after_activity_read"]["passed"])
        ok = [_read("get_recent_account_activity")]
        self.assertTrue(self._checks(pub, ok)["expansion_after_activity_read"]["passed"])

    def test_nothing_published_fails(self):
        self.assertFalse(self._checks()["non_empty_output"]["passed"])


if __name__ == "__main__":
    unittest.main()
