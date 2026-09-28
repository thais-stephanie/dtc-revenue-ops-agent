"""Unattended mode: ledger, Healthchecks, Slack, and the unattended loop guards.

No network: urllib.request.urlopen is patched. The Healthchecks contract used
(verified against healthchecks.io/docs/http_api): <url>/start, <url> for
success, <url>/fail, each with ?rid=<canonical uuid>, HTTP POST body stored.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import uuid
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_agent import agent, config, signals, unattended  # noqa: E402
from revenue_agent.agent import AgentRunner  # noqa: E402
from revenue_agent.repository import InMemoryRepository  # noqa: E402
from revenue_agent.testing import ReplayClient, ScriptedClient  # noqa: E402

# the operator's real values, read before any test patches the environment
REAL_URLS = [os.environ.get(k) for k in ("HEALTHCHECKS_PING_URL", "SLACK_WEBHOOK_URL")]
HC = "https://hc-ping.example/SECRET-HC-UUID-0000"
SLACK = "https://hooks.slack.example/services/SECRET/SLACK/TOKEN"
REPO = InMemoryRepository()
RANKED = {e["account_id"]: e for e in signals.shortlist(REPO, limit=100)}


def runner(inject=None, **guards):
    return lambda: AgentRunner(
        InMemoryRepository(), ScriptedClient(inject={inject: True} if inject else None), **guards
    )


class _Base(unittest.TestCase):
    """Each test runs in its own directory, so receipts and the ledger stay out of runs/."""

    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.ledger = pathlib.Path("runs") / "unattended.sqlite3"
        env = mock.patch.dict(os.environ, {"HEALTHCHECKS_PING_URL": HC, "SLACK_WEBHOOK_URL": SLACK})
        env.start()
        self.addCleanup(env.stop)
        self.urlopen = mock.patch("urllib.request.urlopen").start()
        self.urlopen.return_value.__enter__.return_value = SimpleNamespace(status=200)
        self.addCleanup(mock.patch.stopall)
        self.printed = mock.patch("builtins.print").start()

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def run_job(self, make_runner=None):
        return unattended.run_once(make_runner or runner(), live=False, ledger=self.ledger)

    def sent(self):
        """(url, body) of every outbound request, in order."""
        return [(c.args[0].full_url, c.args[0].data) for c in self.urlopen.call_args_list]


class TestLedger(_Base):
    def test_healthy_run_creates_ok_row(self):
        row = self.run_job()
        [stored] = unattended.recent(self.ledger)
        self.assertEqual(stored["status"], "ok")
        self.assertEqual(stored["run_id"], row["run_id"])
        self.assertEqual((stored["accounts_scanned"], stored["accounts_shortlisted"]), (30, 8))
        self.assertEqual(stored["recommendations_published"], 3)
        self.assertEqual(stored["retry_count"], 0)
        self.assertEqual(json.loads(stored["problems"]), [])

    def test_failed_run_creates_failed_row(self):
        self.run_job(runner("bad_schema"))
        [stored] = unattended.recent(self.ledger)
        self.assertEqual(stored["status"], "failed")
        self.assertTrue(any("schema:" in p for p in json.loads(stored["problems"])))

    def test_partial_run_creates_partial_row(self):
        self.run_job(runner("extra_account"))
        [stored] = unattended.recent(self.ledger)
        self.assertEqual(stored["status"], "partial")
        self.assertEqual(stored["recommendations_published"], 3)
        self.assertIn("limit:recommendation_limit_exceeded (not_a_real_account)",
                      json.loads(stored["problems"]))

    def test_exception_is_a_failed_row_not_a_crash(self):
        def boom():
            raise RuntimeError("database unreachable")

        row = self.run_job(boom)
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["forensic_report_path"])
        self.assertTrue(json.loads(row["problems"])[0].startswith("operational_error: RuntimeError"))

    def test_provider_exception_is_a_model_error(self):
        APIError = type("APIError", (Exception,), {"__module__": "anthropic._exceptions"})

        def overloaded():
            raise APIError("overloaded")

        row = self.run_job(overloaded)
        self.assertTrue(json.loads(row["problems"])[0].startswith("model_error: APIError"))

    def test_row_carries_forensic_path_and_hash(self):
        import hashlib

        self.run_job()
        [stored] = unattended.recent(self.ledger)
        path = pathlib.Path(stored["forensic_report_path"])
        self.assertTrue(path.exists())
        self.assertEqual(stored["forensic_report_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_ledger_survives_a_new_process(self):
        row = self.run_job()
        out = subprocess.run(
            [sys.executable, "-c",
             "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("
             "'SELECT status FROM runs WHERE run_id=?', (sys.argv[2],)).fetchone()[0])",
             str(self.ledger), row["run_id"]],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(out.stdout.strip(), "ok")

    def test_status_is_constrained_by_the_database(self):
        with self.assertRaises(sqlite3.IntegrityError):
            unattended._sql(self.ledger, "INSERT INTO runs (run_id, job, mode, started_at, "
                            "finished_at, duration_s, status, problems) VALUES "
                            "('x','j','m','t','t',0,'missed','[]')")


class TestHealthchecks(_Base):
    def test_start_then_success_with_the_same_run_id(self):
        row = self.run_job()
        urls = [u for u, _ in self.sent()]
        self.assertEqual(urls, [f"{HC}/start?rid={row['run_id']}", f"{HC}?rid={row['run_id']}"])
        uuid.UUID(row["run_id"])  # rid must be a canonical UUID
        self.assertEqual(json.loads(row["monitoring"])["healthchecks_end"], "ok")

    def test_partial_and_failed_use_fail_never_success(self):
        for inject in ("extra_account", "bad_schema"):
            self.urlopen.reset_mock()
            row = self.run_job(runner(inject))
            hc = [u for u, _ in self.sent() if u.startswith(HC)]
            self.assertEqual(hc, [f"{HC}/start?rid={row['run_id']}", f"{HC}/fail?rid={row['run_id']}"])

    def test_outage_does_not_crash_or_change_the_run(self):
        self.urlopen.side_effect = urllib.error.URLError("down")
        row = self.run_job()
        [stored] = unattended.recent(self.ledger)
        self.assertEqual(stored["status"], "ok")
        self.assertEqual(json.loads(stored["monitoring"]),
                         {"healthchecks_start": "error: URLError",
                          "healthchecks_end": "error: URLError", "slack": "not_needed"})
        self.assertEqual(row["recommendations_published"], 3)

    def test_missing_env_is_a_noop(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            row = self.run_job()
        self.urlopen.assert_not_called()
        self.assertEqual(row["status"], "ok")
        self.assertEqual(json.loads(row["monitoring"])["healthchecks_start"], "not_configured")


class TestSlack(_Base):
    def slack_texts(self):
        return [json.loads(b)["text"] for u, b in self.sent() if u == SLACK]

    def test_ok_sends_no_alert(self):
        self.run_job()
        self.assertEqual(self.slack_texts(), [])

    def test_partial_and_failed_send_a_complete_alert(self):
        for inject, status in (("extra_account", "PARTIAL"), ("bad_schema", "FAILED")):
            self.urlopen.reset_mock()
            row = self.run_job(runner(inject))
            [text] = self.slack_texts()
            for needed in (f"RevOps Agent - {status}", f"run_id: {row['run_id']}", "Expected:",
                           "Actual:", "Reason:", "External writes: 0", "Next action:",
                           "Forensic report:", row["forensic_report_sha256"]):
                self.assertIn(needed, text)
            self.assertEqual(json.loads(row["monitoring"])["slack"], "ok")

    def test_outage_falls_back_to_console_and_keeps_the_row(self):
        def only_slack_down(request, timeout):
            if request.full_url == SLACK:
                raise urllib.error.URLError("down")
            return mock.MagicMock()

        self.urlopen.side_effect = only_slack_down
        row = self.run_job(runner("bad_schema"))
        [stored] = unattended.recent(self.ledger)
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(json.loads(stored["monitoring"])["slack"], "error: URLError")
        self.assertTrue(any("RevOps Agent - FAILED" in str(c.args[0]) for c in self.printed.call_args_list))
        self.assertEqual(row["run_id"], stored["run_id"])

    def test_missing_env_falls_back_to_console(self):
        with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": ""}):
            row = self.run_job(runner("bad_schema"))
        self.assertEqual(json.loads(row["monitoring"])["slack"], "not_configured")
        self.assertTrue(any("RevOps Agent - FAILED" in str(c.args[0]) for c in self.printed.call_args_list))


class TestSecurity(_Base):
    def test_urls_never_persist_or_print(self):
        for inject in (None, "extra_account", "bad_schema"):
            self.run_job(runner(inject))
        persisted = self.ledger.read_bytes() + b"".join(
            p.read_bytes() for p in pathlib.Path("runs").glob("*.json")
        )
        printed = " ".join(str(c) for c in self.printed.call_args_list)
        for secret in (HC, SLACK, "SECRET"):
            self.assertNotIn(secret.encode(), persisted)
            self.assertNotIn(secret, printed)

    def test_the_only_outbound_requests_are_the_two_monitors(self):
        for inject in (None, "extra_account", "bad_schema"):
            self.run_job(runner(inject))
        self.assertTrue(self.sent())
        for url, _ in self.sent():
            self.assertTrue(url.startswith(HC) or url == SLACK, url)

    def test_redact_scrubs_the_monitor_urls(self):
        from revenue_agent.forensics import redact

        self.assertNotIn("SECRET", json.dumps(redact({"note": f"see {HC} and {SLACK}"})))

    def test_committed_docs_and_tests_hold_no_real_urls(self):
        texts = [(ROOT / "docs" / "UNATTENDED_MODE.md").read_text(encoding="utf-8"),
                 pathlib.Path(__file__).read_text(encoding="utf-8")]
        for value in REAL_URLS:
            if value:
                for text in texts:
                    self.assertNotIn(value, text)


def _usage_client(turns, input_tokens):
    """A replay client whose every response reports `input_tokens`."""
    replay = ReplayClient(turns)
    calls = []

    def create_message(**kwargs):
        calls.append(1)
        response = replay.create_message(**kwargs)
        response.usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=0)
        return response

    return SimpleNamespace(create_message=create_message), calls


A, D = "atlas_ivy", "field_foundry"  # D is real but not on the shortlist
READ_A = ("get_account_context", {"account_id": A})
READ_D = ("get_account_context", {"account_id": D})
SUBMIT = ("submit_daily_brief", {"recommendations": [], "investigated_accounts": 1})


def _run(client, **guards):
    return AgentRunner(REPO, client, **guards).run(shortlist=[RANKED[A]])


class TestCostGuard(unittest.TestCase):
    def test_stops_before_the_call_that_could_exceed_the_budget(self):
        # 150k input tokens = $0.45 spent; any further call would pass $0.50
        client, calls = _usage_client([[READ_A], [READ_A], [SUBMIT]], 150_000)
        result = _run(client, max_cost_usd=0.50)
        self.assertEqual(len(calls), 1)
        self.assertEqual([r.reason for r in result.rejected], ["unattended_cost_budget"])
        self.assertEqual(result.rejected[0].stage, "loop")

    def test_stops_before_the_first_call_when_even_that_cannot_fit(self):
        client, calls = _usage_client([[SUBMIT]], 0)
        result = _run(client, max_cost_usd=0.01)
        self.assertEqual(calls, [])
        self.assertEqual([r.reason for r in result.rejected], ["unattended_cost_budget"])

    def test_a_run_within_budget_is_untouched(self):
        client, calls = _usage_client([[READ_A], [SUBMIT]], 1_000)
        result = _run(client, max_cost_usd=0.50)
        self.assertEqual((len(calls), result.rejected), (2, []))


class TestRefusalStreak(unittest.TestCase):
    def test_three_consecutive_refusals_stop_the_loop(self):
        client = ReplayClient([[READ_D, READ_D], [READ_D], [READ_A], [SUBMIT]])
        result = _run(client, max_refusal_streak=3)
        self.assertEqual(len(client.received), 2)  # never asked the model a third time
        self.assertEqual([r.reason for r in result.rejected], ["refusal_streak_limit"])
        self.assertEqual([e["status"] for e in result.tool_log], ["refused"] * 3)

    def test_a_successful_call_resets_the_streak(self):
        client = ReplayClient([[READ_D, READ_D], [READ_A], [READ_D, READ_D], [SUBMIT]])
        result = _run(client, max_refusal_streak=3)
        self.assertEqual(result.rejected, [])
        self.assertIsNotNone(result.raw_brief)

    def test_off_by_default(self):
        client = ReplayClient([[READ_D, READ_D, READ_D, READ_D], [SUBMIT]])
        result = _run(client)
        self.assertEqual(result.rejected, [])


class TestExistingCapsUnchanged(unittest.TestCase):
    def test_caps(self):
        self.assertEqual(agent.MAX_ITERATIONS, 20)
        self.assertEqual(config.MAX_TOOL_CALLS_PER_RUN, 14)
        self.assertEqual(config.MAX_READ_TOOL_CALLS_PER_ACCOUNT, 4)
        self.assertEqual(config.MAX_TASK_PROPOSALS_PER_ACCOUNT, 1)

    def test_unattended_defaults(self):
        self.assertEqual(config.UNATTENDED_MAX_COST_USD, 0.50)
        self.assertEqual(config.UNATTENDED_MAX_REFUSAL_STREAK, 3)

    def test_guards_off_by_default_so_offline_runs_are_unchanged(self):
        r = AgentRunner(REPO, ScriptedClient())
        self.assertEqual((r.max_cost_usd, r.max_refusal_streak), (None, None))
        result = r.run()
        self.assertEqual((len(result.published), result.rejected), (3, []))

    def test_unattended_offline_runner_does_not_apply_the_usd_budget(self):
        r = unattended._runner(live=False, inject=None)
        self.assertIsNone(r.max_cost_usd)
        self.assertEqual(r.max_refusal_streak, 3)


if __name__ == "__main__":
    unittest.main()
