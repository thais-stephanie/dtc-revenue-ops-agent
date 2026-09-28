"""A report must let you reconstruct the run from the artifact alone.

The first live report could not say which model ran, how many tokens it used,
what the model submitted, which source it mis-cited, or what arguments it passed
to the tools. Each test here pins one of those, offline, with no network.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_agent import signals  # noqa: E402
from revenue_agent.agent import AgentRunner  # noqa: E402
from revenue_agent.evaluation import run_case  # noqa: E402
from revenue_agent.forensics import MODE_OFFLINE, REDACTED, redact, run_forensics  # noqa: E402
from revenue_agent.repository import InMemoryRepository  # noqa: E402
from revenue_agent.testing import ScriptedClient  # noqa: E402
from revenue_agent.tools import ToolError, ToolRuntime  # noqa: E402

REPO = InMemoryRepository()
ENTRY = next(e for e in signals.shortlist(REPO, limit=100) if e["account_id"] == "cinder_supply")
SERVED = "claude-test-model-20990101"


class ProviderShapedClient(ScriptedClient):
    """The scripted client, plus what a real provider reports on every response."""

    def __init__(self, *, reported_model: str | None = SERVED, **kwargs) -> None:
        super().__init__(**kwargs)
        self.reported_model = reported_model
        self.calls = 0

    def create_message(self, **kwargs):
        response = super().create_message(**kwargs)
        if getattr(response, "_stamped", False):  # the parent recurses once
            return response
        self.calls += 1
        response.model = self.reported_model
        response.usage.input_tokens = 1000 * self.calls
        response.usage.output_tokens = 10 * self.calls
        response._stamped = True
        return response


def _run(client, *, model: str = "requested-alias"):
    return AgentRunner(REPO, client, model=model).run(shortlist=[ENTRY])


def _load_runner():
    spec = importlib.util.spec_from_file_location("eval_runner", ROOT / "evals" / "runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestRejectionForensics(unittest.TestCase):
    def setUp(self):
        self.run = _run(ScriptedClient(max_accounts=1, inject={"unknown_ref": True}))
        self.record = run_forensics(self.run, live=False)

    def test_rejection_keeps_reason_details_stage_and_what_was_refused(self):
        (rejection,) = self.record["rejections"]
        self.assertEqual(rejection["reason"], "unknown_source_reference")
        self.assertEqual(rejection["stage"], "gate")
        self.assertEqual(rejection["account_id"], "cinder_supply")
        self.assertEqual(rejection["details"], ["campaign_metric_999"])
        self.assertEqual(rejection["recommendation_index"], 0)
        self.assertIsNotNone(rejection["recommended_action"])
        self.assertIn("campaign_metric_999", rejection["cited_source_refs"])

    def test_unknown_refs_are_reconstructable_from_the_artifact_alone(self):
        (rejection,) = self.record["rejections"]
        available = set(self.record["available_source_refs"])
        derived = sorted(set(rejection["cited_source_refs"]) - available)
        self.assertEqual(derived, rejection["details"])
        self.assertEqual(derived, rejection["unknown_source_refs"])

    def test_model_said_and_gate_published_are_both_visible(self):
        self.assertTrue(self.record["raw_brief_submitted"])
        said = self.record["raw_brief"]["recommendations"][0]
        self.assertIn("campaign_metric_999", said["source_refs"])
        self.assertEqual(self.record["published_recommendations"], [])

    def test_a_schema_failure_keeps_the_raw_brief(self):
        record = run_forensics(
            _run(ScriptedClient(max_accounts=1, inject={"bad_schema": True})), live=False
        )
        self.assertEqual(record["rejections"][0]["stage"], "schema")
        self.assertTrue(record["rejections"][0]["details"])
        self.assertEqual(record["raw_brief"]["recommendations"], [{"account_id": "atlas_ivy"}])

    def test_published_recommendations_carry_their_citations(self):
        record = run_forensics(_run(ScriptedClient(max_accounts=1)), live=False)
        (item,) = record["published_recommendations"]
        self.assertTrue(item["cited_source_refs"])
        self.assertEqual(item["unknown_source_refs"], [])
        self.assertTrue(set(item["cited_source_refs"]) <= set(record["available_source_refs"]))


class TestModelAndUsage(unittest.TestCase):
    def test_live_records_the_model_the_provider_reported(self):
        record = run_forensics(_run(ProviderShapedClient(max_accounts=1)), live=True)
        self.assertEqual(record["model"], SERVED)
        self.assertEqual(record["model_requested"], "requested-alias")
        self.assertEqual(record["models_reported"], [SERVED])
        self.assertEqual(record["model_source"], "provider_response")

    def test_live_without_a_reported_model_says_it_fell_back(self):
        record = run_forensics(
            _run(ProviderShapedClient(max_accounts=1, reported_model=None)), live=True
        )
        self.assertEqual(record["model"], "requested-alias")
        self.assertEqual(record["model_source"], "request_only_provider_reported_none")

    def test_offline_records_no_model(self):
        record = run_forensics(_run(ScriptedClient(max_accounts=1)), live=False)
        self.assertIsNone(record["model"])
        self.assertIsNone(record["model_requested"])
        self.assertEqual(record["model_source"], MODE_OFFLINE)

    def test_provider_usage_is_persisted_in_total_and_per_turn(self):
        client = ProviderShapedClient(max_accounts=1)
        usage = run_forensics(_run(client), live=True)["usage"]
        turns = usage["per_turn"]
        self.assertEqual(len(turns), client.calls)
        self.assertEqual(usage["input_tokens"], sum(t["input_tokens"] for t in turns))
        self.assertEqual(usage["output_tokens"], sum(t["output_tokens"] for t in turns))
        self.assertEqual(usage["total_tokens"], usage["input_tokens"] + usage["output_tokens"])
        self.assertGreater(usage["estimated_cost_usd"], 0)
        self.assertEqual(usage["usage_source"], "provider_reported")
        self.assertEqual([t["turn"] for t in turns], list(range(1, client.calls + 1)))
        self.assertEqual(turns[-1]["tool_uses"], ["submit_daily_brief"])

    def test_offline_usage_is_zero_and_labelled(self):
        usage = run_forensics(_run(ScriptedClient(max_accounts=1)), live=False)["usage"]
        self.assertEqual(usage["total_tokens"], 0)
        self.assertEqual(usage["estimated_cost_usd"], 0)
        self.assertEqual(usage["usage_source"], "offline_scripted_zero_by_design")


class TestToolLog(unittest.TestCase):
    def test_arguments_and_returned_refs_are_logged(self):
        record = run_forensics(_run(ScriptedClient(max_accounts=1)), live=False)
        log = record["tool_log"]
        self.assertEqual([e["seq"] for e in log], list(range(1, len(log) + 1)))
        activity = next(e for e in log if e["tool"] == "get_recent_account_activity")
        self.assertEqual(activity["arguments"], {"account_id": "cinder_supply", "limit": 5})
        self.assertEqual(activity["status"], "ok")
        self.assertEqual(activity["source_refs_returned"], ["account_activity:cinder_supply"])
        # available = exactly what tools returned + the signal refs handed over
        # on the shortlist; each with the provenance that issued it
        returned = {r for e in log for r in e["source_refs_returned"]}
        supplied = {s["source_ref"] for e in record["shortlist_supplied"] for s in e["signals"]}
        self.assertEqual(returned | supplied, set(record["available_source_refs"]))
        provenance = record["source_ref_provenance"]
        for e in log:
            for ref in e["source_refs_returned"]:
                self.assertEqual(
                    provenance[ref], {"account_id": e["account_id"], "origin": f"tool:{e['tool']}"}
                )
        for entry in record["shortlist_supplied"]:
            for s in entry["signals"]:
                self.assertEqual(
                    provenance[s["source_ref"]],
                    {"account_id": entry["account_id"], "origin": "shortlist_signal"},
                )
        # the model's view is reconstructable: the sanitised payload is kept
        self.assertEqual(activity["result"]["source_ref"], "account_activity:cinder_supply")
        self.assertIn("pause", activity["result"]["activity"][0]["body"])

    def test_the_campaign_window_is_recoverable(self):
        """The ref embeds `days`; the first report lost it."""
        runtime = ToolRuntime(REPO, [ENTRY])
        runtime.execute("get_campaign_performance", {"account_id": "cinder_supply", "days": 7})
        (entry,) = runtime.tool_log
        self.assertEqual(entry["arguments"]["days"], 7)
        self.assertEqual(entry["source_refs_returned"], ["campaign_performance:cinder_supply:7d"])

    def test_a_refused_call_is_logged_as_refused(self):
        runtime = ToolRuntime(REPO, [ENTRY])
        for _ in range(4):
            runtime.execute("get_account_context", {"account_id": "cinder_supply"})
        with self.assertRaises(ToolError):
            runtime.execute("get_recent_account_activity", {"account_id": "cinder_supply"})
        last = runtime.tool_log[-1]
        self.assertEqual(last["tool"], "get_recent_account_activity")
        self.assertEqual(last["status"], "refused")  # a budget refusal, not a failure
        self.assertIn("read tool budget exhausted", last["error"])
        self.assertEqual(last["source_refs_returned"], [])
        self.assertIsNone(last["result"])
        self.assertNotIn("account_activity:cinder_supply", runtime.available_source_refs)

    def test_a_failing_tool_is_logged_as_an_error_not_a_refusal(self):
        runtime = ToolRuntime(REPO, [ENTRY])
        with self.assertRaises(ToolError):
            runtime.execute("no_such_tool", {"account_id": "cinder_supply"})
        self.assertEqual(runtime.tool_log[-1]["status"], "error")


class TestNoSecrets(unittest.TestCase):
    SENTINEL = "sk-ant-TESTSENTINEL-0123456789abcdef"

    def test_redact_by_key_by_shape_and_by_environment_value(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": self.SENTINEL}):
            out = redact(
                {
                    "account_id": "cinder_supply",
                    "api_key": "anything",
                    "Authorization": "anything",
                    "x-api-key": "anything",
                    "note": "header was Bearer abcdefghijklmnop",
                    "echo": f"key={self.SENTINEL}",
                    "author": "Dev",
                    "input_tokens": 12,
                    "max_tokens": 4096,
                }
            )
        self.assertEqual(out["api_key"], REDACTED)
        self.assertEqual(out["Authorization"], REDACTED)
        self.assertEqual(out["x-api-key"], REDACTED)
        self.assertNotIn("abcdefghijklmnop", out["note"])
        self.assertNotIn(self.SENTINEL, out["echo"])
        # ordinary fields survive
        self.assertEqual(out["account_id"], "cinder_supply")
        self.assertEqual(out["author"], "Dev")
        self.assertEqual(out["input_tokens"], 12)
        self.assertEqual(out["max_tokens"], 4096)

    def test_a_full_report_never_contains_the_api_key(self):
        runner = _load_runner()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": self.SENTINEL}):
            client = ProviderShapedClient(max_accounts=1)
            # a model that echoes a secret into a tool argument must not leak it
            original = client._brief

            def leaky_brief():
                brief = original()
                brief["notes"] = f"debug {self.SENTINEL}"
                return brief

            client._brief = leaky_brief
            case = {
                "id": "secret_probe",
                "kind": "control",
                "account_id": "cinder_supply",
                "expected": {"must_read_activity": True},
            }
            record = run_case(case, REPO, lambda: client, live=True)
            report = runner.build_report([record], live=True, label="probe", selected=None)
            text = json.dumps(report, default=str)
        self.assertNotIn(self.SENTINEL, text)
        self.assertNotIn("sk-ant-", text)
        self.assertEqual(report["report_schema_version"], 5)
        self.assertEqual(report["mode"], "live")
        self.assertEqual(report["model"], SERVED)
        self.assertNotIn("ANTHROPIC_API_KEY", text)


if __name__ == "__main__":
    unittest.main()
