"""Phase 2B: invariants, idempotent publication, crash ordering, bounded retry, drills.

No network (urlopen is patched by the shared base) and no real sleeps.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_unattended import SLACK, _Base  # noqa: E402

from revenue_agent import agent, config, signals, unattended  # noqa: E402
from revenue_agent.agent import AgentRunner, is_transient  # noqa: E402
from revenue_agent.repository import InMemoryRepository  # noqa: E402
from revenue_agent.schemas import PublishedRecommendation, Recommendation  # noqa: E402
from revenue_agent.testing import ReplayClient, ScriptedClient  # noqa: E402

REPO = InMemoryRepository()
SUBMIT_EMPTY = ("submit_daily_brief", {"recommendations": [], "investigated_accounts": 0})


class Crash(BaseException):
    """A process dying mid-run: not an Exception, so nothing in the job catches it."""


def drill(inject=None):
    return lambda: unattended._runner(live=False, inject=inject)


def quiet_runner():
    """A run that completes cleanly and publishes nothing."""
    return lambda: AgentRunner(InMemoryRepository(), ReplayClient([[SUBMIT_EMPTY]]))


class _Recovery(_Base):
    def setUp(self):
        super().setUp()
        self.sleep = mock.patch.object(agent.time, "sleep").start()

    def publications(self):
        return unattended._sql(self.ledger, "SELECT * FROM publications ORDER BY idempotency_key")

    def slack_texts(self):
        return [json.loads(b)["text"] for u, b in self.sent() if u == SLACK]


# -- invariants -------------------------------------------------------------------
class TestInvariants(unittest.TestCase):
    def setUp(self):
        self.runner = AgentRunner(REPO, ScriptedClient())
        self.result = self.runner.run()

    def check(self):
        return unattended.invariants(self.runner, self.result)

    def test_a_clean_run_holds_every_invariant(self):
        self.assertEqual(self.check(), [])

    def test_scan_completeness(self):
        self.result.telemetry.accounts_scanned = 29
        self.result.shortlist_supplied = self.result.shortlist_supplied[:-1]
        found = self.check()
        self.assertIn("scan_completeness: scanned 29 of 30 accounts", found)
        self.assertIn("scan_completeness: shortlist differs from a fresh full scan", found)

    def test_expected_size_comes_from_the_dataset_not_a_constant(self):
        self.assertEqual(len(REPO.list_account_ids()), self.result.telemetry.accounts_scanned)

    def test_a_publication_the_gate_would_refuse_is_caught(self):
        runner = AgentRunner(REPO, ScriptedClient(inject={"stale_data": True}))
        result = runner.run()
        [blocked] = result.rejected  # harbor_home expansion, refused for stale data
        result.published.append(PublishedRecommendation(
            recommendation=Recommendation(**blocked.recommendation),
            confidence="high", confidence_reason="smuggled past the gate",
        ))
        found = unattended.invariants(runner, result)
        self.assertIn("publication_integrity: harbor_home EXPAND_CART_RECOVERY not accepted "
                      "by the gate (expansion_blocked_by_stale_data)", found)
        self.assertIn("freshness: harbor_home EXPAND_CART_RECOVERY is blocked by stale data "
                      "or reconciliation", found)

    def test_a_confidence_the_gate_did_not_derive_is_caught(self):
        item = self.result.published[0]
        self.result.published[0] = item.model_copy(
            update={"confidence": "low" if item.confidence != "low" else "high"})
        self.assertTrue(any(v.startswith("publication_integrity") for v in self.check()))

    def test_write_boundary(self):
        self.result.tool_log.append({"tool": "update_crm_record", "account_id": "atlas_ivy",
                                     "status": "ok", "result": {}})
        self.result.tool_log.append({"tool": "propose_crm_task", "account_id": "atlas_ivy",
                                     "status": "ok", "result": {"status": "CREATED"}})
        found = self.check()
        self.assertIn("write_boundary: update_crm_record executed", found)
        self.assertIn("write_boundary: propose_crm_task returned a non-proposal", found)

    def test_a_refused_or_failed_unknown_tool_is_not_a_write(self):
        self.result.tool_log.append({"tool": "update_crm_record", "account_id": "atlas_ivy",
                                     "status": "error", "result": None})
        self.assertEqual(self.check(), [])

    def test_cross_account_citation_is_caught(self):
        item = self.result.published[0]
        mine = item.recommendation.account_id
        foreign = next(r for r, p in self.result.source_ref_provenance.items()
                       if p["account_id"] != mine)
        rec = item.recommendation.model_copy(
            update={"source_refs": [*item.recommendation.source_refs, foreign]})
        self.result.published[0] = item.model_copy(update={"recommendation": rec})
        found = self.check()
        self.assertTrue(any(v.startswith("isolation: no_cross_account_citation") for v in found))
        self.assertTrue(any(v.startswith("publication_integrity") for v in found))

    def test_budgets(self):
        read = {"tool": "get_account_context", "account_id": "atlas_ivy", "status": "ok"}
        self.result.tool_log += [dict(read) for _ in range(12)]
        self.result.turns += [{}] * agent.MAX_ITERATIONS
        self.runner.max_cost_usd, self.result.telemetry.estimated_cost_usd = 0.50, 0.51
        found = self.check()
        self.assertIn(f"budget: {len(self.result.tool_log)} tool calls ran", found)
        self.assertTrue(any(v.startswith("budget:") and "reads ran for atlas_ivy" in v for v in found))
        self.assertIn(f"budget: {len(self.result.turns)} model turns", found)
        self.assertIn("budget: cost $0.5100 over the $0.50 run budget", found)


class TestInvariantViolationFailsTheRun(_Recovery):
    def test_violation_fails_and_publishes_nothing(self):
        with mock.patch.object(unattended, "invariants", return_value=["write_boundary: x"]):
            row = self.run_job(drill())
        self.assertEqual(row["status"], "failed")
        self.assertIn("invariant: write_boundary: x", json.loads(row["problems"]))
        self.assertEqual((row["publications_new"], self.publications()), (0, []))
        self.assertEqual(len(self.slack_texts()), 1)


# -- silent success -----------------------------------------------------------------
class TestSilentSuccess(_Recovery):
    def test_the_nth_quiet_ok_run_warns_without_changing_status(self):
        rows = [self.run_job(quiet_runner()) for _ in range(config.UNATTENDED_SILENT_SUCCESS_STREAK)]
        self.assertEqual([r["status"] for r in rows], ["ok"] * 3)
        self.assertEqual([json.loads(r["warnings"]) for r in rows[:-1]], [[], []])
        self.assertEqual(json.loads(rows[-1]["warnings"]),
                         ["silent_success_streak: 3 consecutive ok runs accepted no recommendations"])
        [text] = self.slack_texts()
        self.assertIn("RevOps Agent - OK with WARNING", text)
        hc_end = [u for u, _ in self.sent()
                  if u.endswith(f"?rid={rows[-1]['run_id']}") and "/start" not in u]
        self.assertEqual(len(hc_end), 1)
        self.assertNotIn("/fail", hc_end[0])  # still an ok run to Healthchecks

    def test_streak_survives_a_restart_because_it_is_read_from_the_ledger(self):
        unattended._sql(self.ledger, "SELECT 1")  # create the schema, as an earlier process did
        conn = sqlite3.connect(self.ledger)
        with conn:
            for i in range(2):
                conn.execute(
                    "INSERT INTO runs (run_id, job, mode, started_at, finished_at, duration_s, "
                    "status, recommendations_published, problems) VALUES (?, ?, ?, ?, ?, 0, 'ok', 0, '[]')",
                    (f"earlier-{i}", unattended.JOB, "offline_scripted",
                     f"2026-09-2{i}T07:00:00+00:00", f"2026-09-2{i}T07:00:01+00:00"),
                )
        conn.close()
        row = self.run_job(quiet_runner())
        self.assertEqual(len(json.loads(row["warnings"])), 1)

    def test_a_run_that_publishes_or_fails_breaks_the_streak(self):
        self.run_job(quiet_runner())
        self.run_job(drill("bad_schema"))
        row = self.run_job(quiet_runner())
        self.assertEqual(json.loads(row["warnings"]), [])


# -- idempotent publication -----------------------------------------------------------
class TestIdempotency(_Recovery):
    def test_rerun_skips_every_duplicate_without_failing(self):
        first = self.run_job(drill())
        second = self.run_job(drill())
        self.assertEqual((first["publications_new"], first["publications_skipped_duplicate"]), (3, 0))
        self.assertEqual((second["publications_new"], second["publications_skipped_duplicate"]), (0, 3))
        self.assertEqual(second["status"], "ok")
        self.assertEqual(self.slack_texts(), [])
        self.assertEqual(len(self.publications()), 3)
        receipt = json.loads(pathlib.Path(second["forensic_report_path"]).read_text())
        self.assertEqual(len(receipt["unattended"]["publications_skipped_duplicate"]), 3)
        self.assertEqual(receipt["unattended"]["publications_new"], [])

    def test_same_account_action_day_on_different_signals_are_different(self):
        rec = AgentRunner(REPO, ScriptedClient()).run().published[0].recommendation
        a = rec.model_copy(update={"source_refs": [f"signal:{rec.account_id}:cart_recovery_gap"]})
        b = rec.model_copy(update={"source_refs": [f"signal:{rec.account_id}:reactivation_gap"]})
        self.assertNotEqual(unattended.publication_key("offline_scripted", a),
                            unattended.publication_key("offline_scripted", b))

    def test_prose_tool_refs_and_task_ids_do_not_change_identity(self):
        rec = AgentRunner(REPO, ScriptedClient()).run().published[0].recommendation
        reworded = rec.model_copy(update={
            "headline": "Entirely different wording here", "rationale": "Other prose " * 5,
            "priority": "low", "proposed_task_id": "task_other",
            "source_refs": [r for r in rec.source_refs if r.startswith("signal:")]
            + [f"campaign_performance:{rec.account_id}:7d"],
        })
        self.assertEqual(unattended.publication_key("offline_scripted", rec),
                         unattended.publication_key("offline_scripted", reworded))

    def test_offline_and_live_never_suppress_each_other(self):
        rec = AgentRunner(REPO, ScriptedClient()).run().published[0].recommendation
        self.assertNotEqual(unattended.publication_key("offline_scripted", rec),
                            unattended.publication_key("live", rec))

    def test_the_primary_key_is_the_final_boundary(self):
        self.run_job(drill())
        key = self.publications()[0]["idempotency_key"]
        with self.assertRaises(sqlite3.IntegrityError):
            unattended._sql(self.ledger, "INSERT INTO publications VALUES (?, 'r', 'm', 'a', 'x', 't', '{}')",
                            (key,))

    def test_duplicate_skip_survives_a_new_process(self):
        self.run_job(drill())
        env = {k: v for k, v in os.environ.items()
               if k not in ("HEALTHCHECKS_PING_URL", "SLACK_WEBHOOK_URL")}
        env["PYTHONPATH"] = str(ROOT / "src")
        out = subprocess.run(
            [sys.executable, "-c",
             "from revenue_agent import unattended as u;"
             "r = u.run_once(lambda: u._runner(False, None), live=False);"
             "print(r['publications_new'], r['publications_skipped_duplicate'])"],
            capture_output=True, text=True, check=True, env=env, cwd=os.getcwd(),
        )
        self.assertEqual(out.stdout.split()[-2:], ["0", "3"])


class TestCrashOrdering(_Recovery):
    def test_case_a_crash_after_publication_is_not_duplicated_on_retry(self):
        with mock.patch("revenue_agent.daily.write_receipt", side_effect=Crash):
            with self.assertRaises(Crash):
                self.run_job(drill())
        self.assertEqual(len(self.publications()), 3)   # committed before the crash
        self.assertEqual(unattended.recent(self.ledger), [])  # the run never finished
        retry = self.run_job(drill())
        self.assertEqual((retry["publications_new"], retry["publications_skipped_duplicate"]), (0, 3))
        self.assertEqual(len(self.publications()), 3)

    def test_case_b_crash_inside_publication_does_not_suppress_the_retry(self):
        real = unattended.publication_key
        calls = []

        def dies_on_second(mode, rec):
            calls.append(1)
            if len(calls) == 2:
                raise Crash
            return real(mode, rec)

        with mock.patch.object(unattended, "publication_key", side_effect=dies_on_second):
            with self.assertRaises(Crash):
                self.run_job(drill())
        self.assertEqual(self.publications(), [])  # the first insert was rolled back
        retry = self.run_job(drill())
        self.assertEqual((retry["publications_new"], retry["publications_skipped_duplicate"]), (3, 0))


# -- retry ----------------------------------------------------------------------------
class _Status(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class TestTransientClassification(unittest.TestCase):
    def test_transient(self):
        for exc in (TimeoutError(), ConnectionError(), _Status(408), _Status(409), _Status(429),
                    _Status(500), _Status(503), _Status(529),
                    type("APITimeoutError", (Exception,), {})(),
                    type("APIConnectionError", (Exception,), {})()):
            self.assertTrue(is_transient(exc), exc)

    def test_not_transient(self):
        for exc in (_Status(400), _Status(401), _Status(403), _Status(404), ValueError("schema"),
                    KeyError("x"), RuntimeError("policy")):
            self.assertFalse(is_transient(exc), exc)


class _Counting:
    """Wraps a client; optionally fails the first `fail` calls with `error`."""

    def __init__(self, inner, fail=0, error=TimeoutError):
        self.inner, self.fail, self.error, self.calls = inner, fail, error, 0

    def create_message(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail:
            raise self.error("injected")
        return self.inner.create_message(**kwargs)


class TestRetry(unittest.TestCase):
    def setUp(self):
        self.sleep = mock.patch.object(agent.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def test_timeout_retries_exactly_the_bounded_count(self):
        client = _Counting(ScriptedClient(), fail=99)
        runner = AgentRunner(REPO, client, max_model_attempts=3)
        with self.assertRaises(TimeoutError):
            runner.run()
        self.assertEqual((client.calls, runner.model_retries), (3, 2))
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 2])
        self.assertIsInstance(runner.model_error, TimeoutError)

    def test_a_transient_blip_recovers(self):
        client = _Counting(ScriptedClient(), fail=1, error=ConnectionError)
        runner = AgentRunner(REPO, client, max_model_attempts=3)
        result = runner.run()
        self.assertEqual((runner.model_retries, len(result.published)), (1, 3))

    def test_a_non_transient_error_is_never_retried(self):
        client = _Counting(ScriptedClient(), fail=99, error=ValueError)
        runner = AgentRunner(REPO, client, max_model_attempts=3)
        with self.assertRaises(ValueError):
            runner.run()
        self.assertEqual((client.calls, runner.model_retries), (1, 0))

    def test_business_failures_never_retry(self):
        for inject in ("bad_schema", "unknown_ref", "force_expansion", "extra_account", "stale_data"):
            baseline = _Counting(ScriptedClient(inject={inject: True}))
            AgentRunner(REPO, baseline).run()
            client = _Counting(ScriptedClient(inject={inject: True}))
            runner = AgentRunner(REPO, client, max_model_attempts=3)
            runner.run()
            self.assertEqual((client.calls, runner.model_retries), (baseline.calls, 0), inject)
        # guard stops are not errors either
        d = ("get_account_context", {"account_id": "ember_oak"})  # not shortlisted
        entry = signals.shortlist(REPO)[0]
        runner = AgentRunner(REPO, ReplayClient([[d, d, d]]), max_refusal_streak=3, max_model_attempts=3)
        result = runner.run(shortlist=[entry])
        self.assertEqual((runner.model_retries, result.rejected[0].reason), (0, "refusal_streak_limit"))

    def test_default_is_one_attempt(self):
        client = _Counting(ScriptedClient(), fail=1)
        with self.assertRaises(TimeoutError):
            AgentRunner(REPO, client).run()
        self.assertEqual(client.calls, 1)

    def test_live_unattended_turns_sdk_retries_off(self):
        with mock.patch("anthropic.Anthropic") as sdk:
            runner = unattended._runner(live=True, inject=None)
        sdk.assert_called_once_with(max_retries=0, timeout=unattended.MODEL_TIMEOUT_S)
        self.assertEqual(runner.max_model_attempts, config.UNATTENDED_MAX_MODEL_ATTEMPTS)


class TestRetryWithPublication(_Recovery):
    def test_retry_count_is_persisted_and_a_retried_run_is_not_duplicated(self):
        flaky = lambda: AgentRunner(InMemoryRepository(), _Counting(ScriptedClient(), fail=1),  # noqa: E731
                                    max_model_attempts=3)
        first = self.run_job(flaky)
        second = self.run_job(flaky)
        self.assertEqual((first["retry_count"], first["publications_new"]), (1, 3))
        self.assertEqual((second["retry_count"], second["publications_skipped_duplicate"]), (1, 3))
        self.assertEqual(unattended.recent(self.ledger)[0]["retry_count"], 1)
        self.assertEqual(len(self.publications()), 3)


# -- drills ---------------------------------------------------------------------------
class TestDrills(_Recovery):
    def test_stale_data(self):
        row = self.run_job(drill("stale_data"))
        self.assertEqual(row["status"], "partial")
        self.assertIn("gate:expansion_blocked_by_stale_data (harbor_home)", json.loads(row["problems"]))
        self.assertEqual(row["publications_new"], 2)
        self.assertNotIn("harbor_home", [p["account_id"] for p in self.publications()])
        [text] = self.slack_texts()
        self.assertIn("expansion_blocked_by_stale_data", text)
        receipt = json.loads(pathlib.Path(row["forensic_report_path"]).read_text())
        self.assertEqual(receipt["rejections"][0]["reason"], "expansion_blocked_by_stale_data")

    def test_llm_timeout(self):
        row = self.run_job(drill("llm_timeout"))
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["retry_count"], config.UNATTENDED_MAX_MODEL_ATTEMPTS - 1)
        [problem] = json.loads(row["problems"])
        self.assertTrue(problem.startswith("model_error: TimeoutError"), problem)
        self.assertIn(f"(after {config.UNATTENDED_MAX_MODEL_ATTEMPTS} attempts)", problem)
        self.assertEqual((row["recommendations_published"], self.publications()), (0, []))
        self.assertIsNone(row["forensic_report_path"])
        [text] = self.slack_texts()
        self.assertIn(f"{config.UNATTENDED_MAX_MODEL_ATTEMPTS - 1} model retries", text)

    def test_forbidden_write_attempt(self):
        row = self.run_job(drill("forbidden_write_attempt"))
        self.assertEqual(row["status"], "partial")
        problems = json.loads(row["problems"])
        self.assertIn("forbidden_tool_attempt: update_crm_record (not in the tool registry; "
                      "nothing ran)", problems)
        self.assertFalse(any(p.startswith("invariant:") for p in problems))  # nothing was written
        receipt = json.loads(pathlib.Path(row["forensic_report_path"]).read_text())
        [attempt] = [e for e in receipt["tool_log"] if e["tool"] == "update_crm_record"]
        self.assertEqual((attempt["status"], attempt["result"]), ("error", None))
        self.assertIn("unknown tool: update_crm_record", attempt["error"])
        [text] = self.slack_texts()
        self.assertIn("forbidden_tool_attempt", text)
        self.assertIn("External writes: 0", text)

    def test_existing_drills_still_behave(self):
        expected = {"unknown_ref": "failed", "bad_schema": "failed",
                    "extra_account": "partial", "force_expansion": None}
        for inject, status in expected.items():
            row = self.run_job(drill(inject))
            if status:
                self.assertEqual(row["status"], status, inject)
            self.assertFalse(any(p.startswith("invariant:") for p in json.loads(row["problems"])), inject)

    def test_every_drill_is_offline_only(self):
        with mock.patch.object(sys, "argv", ["unattended", "--live", "--inject", "llm_timeout"]), \
                mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as stop:
                unattended.main()
        self.assertEqual(stop.exception.code, 2)

    def test_the_ledger_never_holds_missed(self):
        for inject in unattended.INJECTIONS:
            self.run_job(drill(inject))
        statuses = {r["status"] for r in unattended.recent(self.ledger, limit=50)}
        self.assertLessEqual(statuses, {"ok", "partial", "failed"})


class TestAlertWording(_Recovery):
    """"Published" never means two things: accepted by the gate vs newly recorded."""

    def test_accepted_and_new_publications_are_named_apart(self):
        self.run_job(drill())  # publishes the two recommendations stale_data will repeat
        self.urlopen.reset_mock()
        self.run_job(drill("stale_data"))
        [text] = self.slack_texts()
        self.assertIn("Actual: 30 scanned, 8 shortlisted, 2 recommendations accepted, 1 blocked, "
                      "0 refused calls, 0 failed calls, 0 model retries", text)
        self.assertIn("Publications: 0 new, 2 duplicates skipped", text)
        self.assertNotIn(" published,", text)
        self.assertNotIn("skipped as duplicates", text)

    def test_a_run_without_a_result_says_so_instead_of_none(self):
        self.run_job(drill("llm_timeout"))
        [text] = self.slack_texts()
        self.assertIn("Actual: scan metrics unavailable; the run failed before a result was "
                      "produced (2 model retries)", text)
        self.assertNotIn("None", text)

    def test_the_silent_success_warning_uses_accepted(self):
        for _ in range(config.UNATTENDED_SILENT_SUCCESS_STREAK):
            self.run_job(quiet_runner())
        [text] = self.slack_texts()
        self.assertIn("Warning: silent_success_streak: 3 consecutive ok runs accepted no "
                      "recommendations", text)
        self.assertIn("0 recommendations accepted", text)


class TestLedgerMigration(_Recovery):
    def test_a_phase_2a_ledger_keeps_its_rows(self):
        self.ledger.parent.mkdir(parents=True)
        conn = sqlite3.connect(self.ledger)
        with conn:
            conn.execute(unattended.SCHEMA[0])  # exactly the 2A table: no 2B columns
            conn.execute("INSERT INTO runs (run_id, job, mode, started_at, finished_at, duration_s, "
                         "status, problems) VALUES ('old', 'daily_brief', 'offline_scripted', "
                         "'2026-09-27T05:20:36+00:00', 't', 0, 'ok', '[]')")
        conn.close()
        self.run_job(drill())
        ids = [r["run_id"] for r in unattended.recent(self.ledger)]
        self.assertIn("old", ids)
        self.assertEqual(len(ids), 2)


if __name__ == "__main__":
    unittest.main()
