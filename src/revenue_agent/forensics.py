"""What a run leaves behind: enough to reconstruct it from the artifact alone.

The first live report kept a PASS and threw away the evidence: no model id, no
token counts, no submitted brief, no rejection details, no tool arguments. This
module builds the forensic record that eval reports and run receipts persist.

Two rules:

* Persist only what the application actually received: tool payloads, the
  submitted brief, provider-reported usage. Nothing is inferred, and nothing
  the provider does not expose (hidden reasoning) is requested or stored.
* Log as if tools could one day touch real systems. Anything secret-shaped is
  redacted by key name, by value shape, and by exact match against the API key
  in the environment, without that key ever being printed.
"""
from __future__ import annotations

import os
import re
from typing import Any

from . import config
from .schemas import RunResult

# v3: available_source_refs now includes shortlist signal refs, with provenance
# v4: provenance is {"account_id", "origin"}: refs are citable only for their account
# v5: every tool_log entry carries refusal_reason (budget_exhausted, account_out_of_scope)
REPORT_SCHEMA_VERSION = 5

MODE_LIVE = "live"
MODE_OFFLINE = "offline_scripted"

REDACTED = "[REDACTED]"

# a key segment that names a secret; "author" and "max_tokens" do not match
_SECRET_KEY = re.compile(
    r"(^|[_\-.])(api[_\-]?key|apikey|secret|token|password|passwd|authorization|"
    r"auth|bearer|credentials?|cookie|session[_\-]?id|private[_\-]?key)($|[_\-.])"
)
_SECRET_VALUE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,}")
_SECRET_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "HEALTHCHECKS_PING_URL",
    "SLACK_WEBHOOK_URL",
)


def _secret_values() -> list[str]:
    return [v for name in _SECRET_ENV if len(v := os.getenv(name) or "") >= 8]


def redact(value: Any) -> Any:
    """A copy of `value` with anything secret-shaped replaced."""
    secrets = _secret_values()

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            return {
                k: REDACTED if _SECRET_KEY.search(str(k).lower()) else walk(inner)
                for k, inner in v.items()
            }
        if isinstance(v, (list, tuple)):
            return [walk(inner) for inner in v]
        if isinstance(v, str):
            for secret in secrets:
                v = v.replace(secret, REDACTED)
            return _SECRET_VALUE.sub(REDACTED, v)
        return v

    return walk(value)


def model_fields(run: RunResult, *, live: bool) -> dict:
    """The model that served the run, as the provider reported it.

    `model` is the id from the provider's responses. It falls back to the
    requested id only when no response reported one, and says so.
    """
    if not live:
        return {
            "model": None,
            "model_requested": None,
            "models_reported": [],
            "model_source": MODE_OFFLINE,
        }
    reported = sorted({t["model"] for t in run.turns if t.get("model")})
    if len(reported) == 1:
        model, source = reported[0], "provider_response"
    elif not reported:
        model, source = run.model_requested, "request_only_provider_reported_none"
    else:
        model, source = None, "multiple_models_reported"
    return {
        "model": model,
        "model_requested": run.model_requested,
        "models_reported": reported,
        "model_source": source,
    }


def usage_fields(run: RunResult, *, live: bool) -> dict:
    t = run.telemetry
    return {
        "input_tokens": t.input_tokens,
        "output_tokens": t.output_tokens,
        "total_tokens": t.input_tokens + t.output_tokens,
        "estimated_cost_usd": t.estimated_cost_usd,
        "pricing_usd_per_mtok": {
            "input": config.INPUT_USD_PER_MTOK,
            "output": config.OUTPUT_USD_PER_MTOK,
        },
        "usage_source": "provider_reported" if live else "offline_scripted_zero_by_design",
        "per_turn": run.turns,
    }


def _unknown(cited: list[str], available: set[str]) -> list[str]:
    return sorted(set(cited) - available)


NOT_ISSUED = "NOT_ISSUED"


def _provenance(cited: list[str], provenance: dict[str, dict]) -> dict[str, dict | str]:
    """Account and origin of each cited ref, or NOT_ISSUED if never issued."""
    return {ref: provenance.get(ref, NOT_ISSUED) for ref in cited}


def tool_metrics(run: RunResult, account_id: str | None = None) -> dict:
    """Tool attempts split by capability and outcome, from the tool log alone.

    `account_id` narrows it to one account. The terminal submit is counted from
    the model turns: it is handled by the agent loop and never enters the log.
    """
    # imported here: tools imports this module for redact()
    from .tools import PROPOSAL_TOOLS, READ_ONLY_TOOLS

    log = [e for e in run.tool_log if account_id is None or e.get("account_id") == account_id]

    def count(tools, status=None):
        return sum(
            1 for e in log
            if e["tool"] in tools and (status is None or e.get("status", "ok") == status)
        )

    return {
        "total_tool_attempts": len(log),
        "read_tool_calls": count(READ_ONLY_TOOLS),
        "read_tool_calls_ok": count(READ_ONLY_TOOLS, "ok"),
        "proposal_tool_calls": count(PROPOSAL_TOOLS),
        "proposal_tool_calls_ok": count(PROPOSAL_TOOLS, "ok"),
        "refused_tool_calls": sum(1 for e in log if e.get("status") == "refused"),
        "failed_tool_calls": sum(1 for e in log if e.get("status") == "error"),
        "terminal_submits": sum(
            t.get("tool_uses", []).count("submit_daily_brief") for t in run.turns
        ),
    }


def recommendation_fields(run: RunResult) -> dict:
    """MODEL SAID (raw_brief) next to GATE PUBLISHED and GATE REJECTED."""
    available = set(run.available_source_refs)
    provenance = run.source_ref_provenance
    published = []
    for item in run.published:
        rec = item.recommendation.model_dump(mode="json")
        cited = rec.get("source_refs", [])
        published.append(
            {
                "account_id": rec["account_id"],
                "recommended_action": rec["recommended_action"],
                "cited_source_refs": cited,
                "cited_source_ref_provenance": _provenance(cited, provenance),
                "unknown_source_refs": _unknown(cited, available),
                "confidence": item.confidence,
                "confidence_reason": item.confidence_reason,
                "recommendation": rec,
            }
        )
    rejections = []
    for r in run.rejected:
        rec = r.recommendation or {}
        cited = list(rec.get("source_refs", []))
        task_id = rec.get("proposed_task_id")
        rejections.append(
            {
                "stage": r.stage,
                "reason": r.reason,
                "account_id": r.account_id,
                "details": list(r.details),
                "recommendation_index": r.recommendation_index,
                "recommended_action": rec.get("recommended_action"),
                "cited_source_refs": cited,
                "cited_source_ref_provenance": _provenance(cited, provenance),
                "unknown_source_refs": _unknown(cited, available),
                "proposed_task_provenance": (
                    provenance.get(f"proposed_task:{task_id}", NOT_ISSUED) if task_id else None
                ),
                "recommendation": r.recommendation,
            }
        )
    return {
        "raw_brief_submitted": run.raw_brief is not None,
        "raw_brief": run.raw_brief,
        "published_recommendations": published,
        "rejections": rejections,
    }


def run_forensics(run: RunResult, *, live: bool) -> dict:
    """Everything needed to reconstruct a run, secrets removed."""
    return redact(
        {
            **model_fields(run, live=live),
            "usage": usage_fields(run, live=live),
            "prompt": {
                "system_prompt_sha256": run.system_prompt_sha256,
                "tool_definitions_sha256": run.tool_definitions_sha256,
            },
            "shortlist_supplied": run.shortlist_supplied,
            "tool_calls": run.telemetry.tool_calls,
            "tool_metrics": tool_metrics(run),
            "tool_log": run.tool_log,
            "available_source_refs": sorted(run.available_source_refs),
            "source_ref_provenance": run.source_ref_provenance,
            **recommendation_fields(run),
        }
    )
