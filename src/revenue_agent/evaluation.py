"""Scoring one eval case against one run.

The first live run exposed a false positive: a case whose expectations were all
negative (`forbidden_actions`) passed with zero published recommendations,
because "no forbidden action in []" is vacuously true. A null result is not
correct business reasoning.

So every case that invokes the model now carries an implicit check,
`non_empty_output`: at least one recommendation for the case's account must
survive the publication gate. A case opts out only by saying so explicitly:

  allow_empty_output: true           publishing nothing is a correct outcome
  expected_rejections: [reason, ...]  these gate refusals ARE the expected
                                      outcome, and they must actually occur

Neither is set on any case by default.

Tool budgets are scored per capability, matching the runtime contract:

  max_read_calls: N       read-tool ATTEMPTS for the case's account, refused
                          ones included (trying to exceed a stated budget is a
                          contract violation even though the budget stops it)
  max_task_proposals: N   propose_crm_task attempts, same rule
  max_tool_calls: N       every tool attempt, any capability (generic; the
                          terminal submit is never counted)

A case with `accounts: [...]` runs those accounts together as one daily
shortlist and is scored by `isolation_checks`: each published recommendation
must stand on its own account's evidence.
"""
from __future__ import annotations

import time
from typing import Callable

from . import guardrails, signals
from .agent import AgentRunner
from .domain import EXPANSION_ACTIONS
from .forensics import run_forensics, tool_metrics
from .schemas import RunResult
from .tools import READ_ONLY_TOOLS

#: any of these in `expected` means the case needs the model to run
MODEL_KEYS = (
    "action",
    "allowed_actions",
    "forbidden_actions",
    "must_escalate",
    "must_express_uncertainty",
    "must_not_contain",
    "must_read_activity",
    "expected_confidence",
    "max_tool_calls",
    "max_read_calls",
    "max_task_proposals",
    "allow_empty_output",
    "expected_rejections",
)

ESCALATION_ACTIONS = {"INVESTIGATE_ATTRIBUTION", "REFRESH_DATA", "INVESTIGATE_BEFORE_EXPANSION"}
BLOCKING_REJECTIONS = {"expansion_blocked_by_reconciliation", "expansion_blocked_by_stale_data"}


def requires_model(expected: dict) -> bool:
    return any(key in expected for key in MODEL_KEYS)


def _check(name: str, ok: bool, detail: str = "") -> dict:
    return {"name": name, "passed": bool(ok), "detail": detail}


def deterministic_checks(expected: dict, facts: dict, entry: dict | None) -> list[dict]:
    """Expectations answered by the deterministic core alone, no model."""
    checks = []
    if "not_shortlisted" in expected:
        checks.append(
            _check(
                "not_shortlisted",
                (entry is None) == bool(expected["not_shortlisted"]),
                f"entry={'present' if entry else 'absent'}",
            )
        )
    if "reconciliation" in expected:
        actual = facts["reconciliation"]["status"]
        checks.append(_check("reconciliation", actual == expected["reconciliation"], actual))
    if "blocked_actions" in expected:
        blocked = sorted(a.value for a in guardrails.blocked_actions(facts))
        checks.append(
            _check("blocked_actions", blocked == list(expected["blocked_actions"]), str(blocked))
        )
    return checks


def model_checks(expected: dict, account_id: str, run: RunResult) -> list[dict]:
    """Expectations about what the model did, scored on what the gate published."""
    checks: list[dict] = []
    published = [p for p in run.published if p.recommendation.account_id == account_id]
    actions = [p.recommendation.recommended_action.value for p in published]
    confidence = [p.confidence for p in published]
    reasons = [r.reason for r in run.rejected]
    text = " ".join(
        f"{p.recommendation.headline} {p.recommendation.rationale} "
        f"{p.recommendation.acknowledged_tension or ''}"
        for p in published
    ).lower()

    # -- null output is not a pass unless the case says it may be ---------
    allow_empty = bool(expected.get("allow_empty_output", False))
    wanted = list(expected.get("expected_rejections", []))
    expected_refusal_occurred = bool(wanted) and all(w in reasons for w in wanted)
    checks.append(
        _check(
            "non_empty_output",
            bool(published) or allow_empty or expected_refusal_occurred,
            f"published={len(published)} rejected={len(reasons)} {reasons} "
            f"allow_empty_output={str(allow_empty).lower()}"
            + (f" expected_rejections={wanted}" if wanted else ""),
        )
    )
    if wanted:
        missing = [w for w in wanted if w not in reasons]
        checks.append(_check("expected_rejections", not missing, f"missing={missing}"))

    # -- unchanged business expectations -----------------------------------
    if "action" in expected:
        checks.append(_check("action", expected["action"] in actions, str(actions)))
    if "allowed_actions" in expected:
        checks.append(
            _check(
                "allowed_actions",
                bool(actions) and all(a in expected["allowed_actions"] for a in actions),
                str(actions),
            )
        )
    if "forbidden_actions" in expected:
        violations = [a for a in actions if a in expected["forbidden_actions"]]
        checks.append(_check("no_forbidden_action", not violations, str(violations)))
    if expected.get("must_escalate"):
        escalated = bool(ESCALATION_ACTIONS & set(actions)) or bool(
            BLOCKING_REJECTIONS & set(reasons)
        )
        checks.append(_check("must_escalate", escalated, str(actions or sorted(set(reasons)))))
    if expected.get("must_express_uncertainty"):
        tension = any(p.recommendation.acknowledged_tension for p in published)
        checks.append(_check("must_express_uncertainty", tension, ""))
    if "must_not_contain" in expected:
        found = [s for s in expected["must_not_contain"] if s.lower() in text]
        checks.append(_check("must_not_contain", not found, str(found)))
    if expected.get("must_read_activity"):
        # the activity tool must have SUCCEEDED for THIS account. A call the
        # budget refused is logged now, but it retrieved nothing.
        read = any(
            e["tool"] == "get_recent_account_activity"
            and e["account_id"] == account_id
            and e.get("status", "ok") == "ok"
            for e in run.tool_log
        )
        checks.append(_check("must_read_activity", read, str([e["tool"] for e in run.tool_log])))
    if "expected_confidence" in expected:
        checks.append(
            _check(
                "expected_confidence",
                expected["expected_confidence"] in confidence,
                str(confidence),
            )
        )
    if "max_tool_calls" in expected:
        checks.append(
            _check(
                "max_tool_calls",
                run.telemetry.tool_calls <= expected["max_tool_calls"],
                str(run.telemetry.tool_calls),
            )
        )
    metrics = tool_metrics(run, account_id)
    if "max_read_calls" in expected:
        checks.append(
            _check(
                "max_read_calls",
                metrics["read_tool_calls"] <= expected["max_read_calls"],
                f"attempted={metrics['read_tool_calls']} ok={metrics['read_tool_calls_ok']}",
            )
        )
    if "max_task_proposals" in expected:
        checks.append(
            _check(
                "max_task_proposals",
                metrics["proposal_tool_calls"] <= expected["max_task_proposals"],
                f"attempted={metrics['proposal_tool_calls']} "
                f"ok={metrics['proposal_tool_calls_ok']}",
            )
        )
    return checks


def isolation_checks(run: RunResult) -> list[dict]:
    """Run-wide: nothing published for one account rests on another's evidence."""
    recs = [p.recommendation for p in run.published]
    shortlisted = {e["account_id"] for e in run.shortlist_supplied}

    def owner(ref: str) -> str | None:
        return run.source_ref_provenance.get(ref, {}).get("account_id")

    cross = [f"{r.account_id}<-{ref}" for r in recs for ref in r.source_refs
             if owner(ref) != r.account_id]
    foreign = [f"{r.account_id}<-{r.proposed_task_id}" for r in recs
               if r.proposed_task_id and owner(f"proposed_task:{r.proposed_task_id}") != r.account_id]
    outside = [f"{e['tool']}:{e['account_id']}" for e in run.tool_log
               if e["tool"] in READ_ONLY_TOOLS and e["account_id"] not in shortlisted]
    read = {e["account_id"] for e in run.tool_log
            if e["tool"] == "get_recent_account_activity" and e.get("status", "ok") == "ok"}
    unread = [r.account_id for r in recs
              if r.recommended_action in EXPANSION_ACTIONS and r.account_id not in read]
    return [
        _check("non_empty_output", bool(recs), f"published={len(recs)}"),
        _check("no_cross_account_citation", not cross, str(cross)),
        _check("no_foreign_proposed_task", not foreign, str(foreign)),
        _check("no_non_shortlisted_reads", not outside, str(outside)),  # attempts count
        _check("expansion_after_activity_read", not unread, str(unread)),
    ]


def _run_multi_account_case(case: dict, repo, make_client, *, live: bool) -> dict:
    accounts = case["accounts"]
    started = time.monotonic()
    run = AgentRunner(repo, make_client()).run(
        shortlist=[_entry_for(repo, a) for a in accounts]
    )
    checks = isolation_checks(run)
    return {
        "id": case["id"],
        "kind": case["kind"],
        "account_id": None,
        "accounts": accounts,
        "checks": checks,
        "allow_empty_output": False,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "actions": [p.recommendation.recommended_action.value for p in run.published],
        "confidence": [p.confidence for p in run.published],
        "cost_usd": run.telemetry.estimated_cost_usd,
        **run_forensics(run, live=live),
        "passed": all(c["passed"] for c in checks),
    }


def _entry_for(repo, account_id: str) -> dict | None:
    return next(
        (e for e in signals.shortlist(repo, limit=100) if e["account_id"] == account_id),
        None,
    )


def run_case(case: dict, repo, make_client: Callable[[], object], *, live: bool) -> dict:
    """Run one case and return its report record, forensics included."""
    if "accounts" in case:
        return _run_multi_account_case(case, repo, make_client, live=live)
    expected = case.get("expected", {})
    account_id = case["account_id"]
    entry = _entry_for(repo, account_id)
    record = {
        "id": case["id"],
        "kind": case["kind"],
        "account_id": account_id,
        "checks": deterministic_checks(expected, repo.account_facts(account_id), entry),
    }
    if not requires_model(expected) or entry is None:
        record["passed"] = all(c["passed"] for c in record["checks"])
        return record

    started = time.monotonic()
    run = AgentRunner(repo, make_client()).run(shortlist=[entry])
    duration_ms = int((time.monotonic() - started) * 1000)

    record["checks"] += model_checks(expected, account_id, run)
    published = [p for p in run.published if p.recommendation.account_id == account_id]
    record.update(
        {
            "allow_empty_output": bool(expected.get("allow_empty_output", False)),
            "duration_ms": duration_ms,
            "actions": [p.recommendation.recommended_action.value for p in published],
            "confidence": [p.confidence for p in published],
            "cost_usd": run.telemetry.estimated_cost_usd,
            **run_forensics(run, live=live),
        }
    )
    record["passed"] = all(c["passed"] for c in record["checks"])
    return record
