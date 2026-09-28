"""Run the daily brief once, unattended: invariants, idempotent publication, ledger, alerts.

    python -m revenue_agent.unattended               # offline scripted client
    python -m revenue_agent.unattended --live        # real model; never implied
    python -m revenue_agent.unattended --list 10     # recent ledger rows
    python -m revenue_agent.unattended --inject bad_schema   # offline failure drill

An operational wrapper around the existing AgentRunner, nothing more. What the
agent may do is unchanged. This adds:

* deterministic post-run invariants, built from the existing gate, evaluator
  and runtime facts; a violation fails the run and publishes nothing
* idempotent publication: a `publications` table keyed by a stable identity,
  so a rerun or retry never publishes the same recommendation twice
* a SQLite row per run (runs/unattended.sqlite3, stdlib, gitignored)
* Healthchecks.io pings: /start, then success or /fail, same ?rid=<uuid>.
  "missed" is Healthchecks' verdict when no ping arrives; a job cannot
  report its own absence, so the ledger never records it
* a Slack Incoming Webhook alert for partial and failed runs and warnings
* the unattended loop guards (USD budget, refusal streak) and bounded retry
  of transient model errors

Monitoring is isolated: a Healthchecks or Slack outage is recorded in the
ledger's `monitoring` column and never changes the run's status or result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
import urllib.request
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config, guardrails, signals
from .agent import MAX_ITERATIONS, AgentRunner
from .forensics import MODE_LIVE, MODE_OFFLINE, redact, tool_metrics
from .repository import InMemoryRepository
from .tools import ACCOUNT_TOOLS, PROPOSAL_TOOLS, READ_ONLY_TOOLS

JOB = "daily_brief"
LEDGER = Path("runs") / "unattended.sqlite3"
PING_TIMEOUT_S = 10  # Healthchecks' own examples use curl -m 10
MODEL_TIMEOUT_S = 120  # per model request; the SDK default is 600
INJECTIONS = [
    "unknown_ref", "bad_schema", "extra_account", "force_expansion",
    "stale_data", "llm_timeout", "forbidden_write_attempt",
]

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    job TEXT NOT NULL,
    mode TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_s REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'partial', 'failed')),
    accounts_scanned INTEGER,
    accounts_shortlisted INTEGER,
    recommendations_published INTEGER,
    recommendations_blocked INTEGER,
    read_attempts INTEGER,
    proposal_attempts INTEGER,
    refused_calls INTEGER,
    failed_calls INTEGER,
    actual_cost_usd REAL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    problems TEXT NOT NULL,
    forensic_report_path TEXT,
    forensic_report_sha256 TEXT,
    monitoring TEXT
)""",
    # one row per recommendation ever published; the primary key is the
    # race-safe idempotency boundary
    """CREATE TABLE IF NOT EXISTS publications (
    idempotency_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    account_id TEXT NOT NULL,
    action TEXT NOT NULL,
    published_at TEXT NOT NULL,
    recommendation TEXT NOT NULL
)""",
]
#: added in Phase 2B; ALTERed onto a Phase 2A ledger so its rows survive
RUN_COLUMNS_2B = {
    "publications_new": "INTEGER",
    "publications_skipped_duplicate": "INTEGER",
    "warnings": "TEXT",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _db(ledger: Path):
    """A connection whose body is ONE transaction: committed, or rolled back on error."""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ledger)
    try:
        conn.row_factory = sqlite3.Row
        with conn:
            for statement in SCHEMA:
                conn.execute(statement)
            have = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
            for column, kind in RUN_COLUMNS_2B.items():
                if column not in have:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {kind}")
            yield conn
    finally:
        conn.close()


def _sql(ledger: Path, statement: str, params=()) -> list[dict]:
    with _db(ledger) as conn:
        return [dict(r) for r in conn.execute(statement, params).fetchall()]


def recent(ledger: Path = LEDGER, limit: int = 10) -> list[dict]:
    return _sql(ledger, "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,))


# -- classification ------------------------------------------------------------
def classify(result, error: BaseException | None, model_failure: bool = False) -> tuple[str, list[str]]:
    """ok / partial / failed, and every reason behind it.

    failed   the run raised, or no recommendation was accepted and something was refused
             (no brief, invalid brief, a guard stop, or every recommendation
             blocked): no trustworthy useful result
    partial  a recommendation was accepted, but another was blocked or a
             tool call was refused, failed, or named a tool that does not exist
    ok       completed with none of the above. Publishing nothing with no
             refusal ("nothing needs a human today") is ok

    A failed invariant (see `invariants`) also makes the run failed.
    """
    if error is not None:
        provider = model_failure or type(error).__module__.startswith("anthropic")
        kind = "model_error" if provider else "operational_error"
        return "failed", [f"{kind}: {type(error).__name__}: {redact(str(error))[:300]}"]
    problems = [
        f"{r.stage}:{r.reason}" + (f" ({r.account_id})" if r.account_id else "")
        for r in result.rejected
    ]
    metrics = tool_metrics(result)
    if metrics["refused_tool_calls"]:
        problems.append(f"refused_tool_calls: {metrics['refused_tool_calls']}")
    if metrics["failed_tool_calls"]:
        problems.append(f"failed_tool_calls: {metrics['failed_tool_calls']}")
    forbidden = sorted({e["tool"] for e in result.tool_log if e["tool"] not in ACCOUNT_TOOLS})
    if forbidden:
        problems.append(f"forbidden_tool_attempt: {', '.join(forbidden)} (not in the tool registry; nothing ran)")
    if result.rejected and not result.published:
        return "failed", problems
    return ("partial" if problems else "ok"), problems


# -- invariants ----------------------------------------------------------------------
def invariants(runner: AgentRunner, result) -> list[str]:
    """What must be true of any finished run, checked from the run's own record.

    Nothing here is a new policy: the gate, the blocked-action rule, the
    isolation checks and the budgets are the existing ones, re-applied to what
    was actually published. Returns the violations; empty means all held.
    """
    from .evaluation import isolation_checks  # evaluation imports agent; keep startup light

    repo, violations = runner.repo, []
    # A. scan completeness: every account in this dataset was scanned, and the
    # shortlist the model got is what a fresh full scan produces
    dataset_size = len(repo.list_account_ids())
    if result.telemetry.accounts_scanned != dataset_size:
        violations.append(f"scan_completeness: scanned {result.telemetry.accounts_scanned} "
                          f"of {dataset_size} accounts")
    fresh = [e["account_id"] for e in signals.shortlist(repo)]
    if [e["account_id"] for e in result.shortlist_supplied] != fresh:
        violations.append("scan_completeness: shortlist differs from a fresh full scan")

    # B + E. every published recommendation passes the existing gate again, and
    # none is an action the existing freshness/reconciliation rule blocks
    by_account = {e["account_id"]: e for e in result.shortlist_supplied}
    for item in result.published:
        rec, acc = item.recommendation, item.recommendation.account_id
        entry = by_account.get(acc)
        if entry is None:
            violations.append(f"publication_integrity: {acc} was never shortlisted")
            continue
        facts = repo.account_facts(acc)
        again, rejection = guardrails.gate_recommendation(
            rec,
            facts=facts,
            available_source_refs={
                r for r, p in result.source_ref_provenance.items() if p["account_id"] == acc
            },
            source_ref_provenance=result.source_ref_provenance,
            signal_severity=entry["top_severity"],
            competing_signals=entry["competing_signals"],
        )
        if again is None or again.confidence != item.confidence:
            violations.append(f"publication_integrity: {acc} {rec.recommended_action.value} "
                              f"not accepted by the gate ({rejection and rejection.reason})")
        if rec.recommended_action in guardrails.blocked_actions(facts):
            violations.append(f"freshness: {acc} {rec.recommended_action.value} is blocked "
                              "by stale data or reconciliation")
    if len(result.published) > config.MAX_RECOMMENDATIONS:
        violations.append(f"publication_integrity: {len(result.published)} recommendations accepted")

    # C. write boundary: only registry tools ran, and a proposal stayed a proposal
    ran = [e for e in result.tool_log if e.get("status", "ok") == "ok"]
    for e in ran:
        if e["tool"] not in ACCOUNT_TOOLS:
            violations.append(f"write_boundary: {e['tool']} executed")
        elif e["tool"] in PROPOSAL_TOOLS and (e.get("result") or {}).get("status") != "PROPOSED":
            violations.append(f"write_boundary: {e['tool']} returned a non-proposal")

    # D. evidence isolation (the evaluator's run-wide checks)
    violations += [
        f"isolation: {c['name']}: {c['detail']}"
        for c in isolation_checks(result)
        if c["name"] != "non_empty_output" and not c["passed"]
    ]

    # F. budgets
    reads = Counter(e["account_id"] for e in ran if e["tool"] in READ_ONLY_TOOLS)
    proposals = Counter(e["account_id"] for e in ran if e["tool"] in PROPOSAL_TOOLS)
    if len(ran) > config.MAX_TOOL_CALLS_PER_RUN:
        violations.append(f"budget: {len(ran)} tool calls ran")
    violations += [f"budget: {n} reads ran for {a}" for a, n in reads.items()
                   if n > config.MAX_READ_TOOL_CALLS_PER_ACCOUNT]
    violations += [f"budget: {n} proposals ran for {a}" for a, n in proposals.items()
                   if n > config.MAX_TASK_PROPOSALS_PER_ACCOUNT]
    if len(result.turns) > MAX_ITERATIONS:
        violations.append(f"budget: {len(result.turns)} model turns")
    cost = result.telemetry.estimated_cost_usd
    if runner.max_cost_usd is not None and cost > runner.max_cost_usd:
        violations.append(f"budget: cost ${cost:.4f} over the ${runner.max_cost_usd:.2f} run budget")
    return violations


# -- idempotent publication ----------------------------------------------------------
def publication_key(mode: str, rec) -> str:
    """The identity of one logical recommendation.

    job, mode, business date, account, action, and the deterministic signals it
    rests on (`signal:<account>:<type>` refs, issued by the scan). Two
    recommendations for one account and action on one day that rest on
    different signals are different recommendations. Prose, confidence,
    priority, tool refs and task ids are left out: they can vary between runs
    of the same logical recommendation.
    """
    signal_refs = sorted(r for r in rec.source_refs if r.startswith("signal:"))
    identity = [JOB, mode, config.AS_OF.date().isoformat(), rec.account_id,
                rec.recommended_action.value, signal_refs]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def publish(ledger: Path, run_id: str, mode: str, published) -> tuple[list[str], list[str]]:
    """Record this run's publications in ONE transaction: all of them or none.

    A key already present is a duplicate and is skipped, never an error. The
    PRIMARY KEY makes that race-safe: concurrent runs cannot both insert it.
    """
    new, duplicate = [], []
    with _db(ledger) as conn:
        for item in published:
            rec = item.recommendation
            key = publication_key(mode, rec)
            cursor = conn.execute(
                "INSERT OR IGNORE INTO publications VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, run_id, mode, rec.account_id, rec.recommended_action.value, _now(),
                 json.dumps(item.model_dump(mode="json"))),
            )
            (new if cursor.rowcount else duplicate).append(key)
    return new, duplicate


def silent_success_warning(ledger: Path, mode: str, row: dict) -> list[str]:
    """Warn when this run is the Nth ok-but-empty run in a row. Never changes status."""
    n = config.UNATTENDED_SILENT_SUCCESS_STREAK
    if n < 1 or row["status"] != "ok" or row["recommendations_published"]:
        return []
    previous = _sql(
        ledger,
        "SELECT status, recommendations_published FROM runs WHERE job = ? AND mode = ? "
        "ORDER BY started_at DESC, rowid DESC LIMIT ?",
        (JOB, mode, n - 1),
    )
    if len(previous) == n - 1 and all(
        p["status"] == "ok" and not p["recommendations_published"] for p in previous
    ):
        return [f"silent_success_streak: {n} consecutive ok runs accepted no recommendations"]
    return []


# -- outbound notifications ------------------------------------------------------
def _post(url: str, body: bytes, content_type: str) -> str:
    """One POST, no retry. Returns 'ok' or the error type; never the URL."""
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": content_type}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=PING_TIMEOUT_S):
            return "ok"
    except Exception as exc:  # monitoring must never break the job
        return f"error: {type(exc).__name__}"


def ping(event: str, run_id: str, body: str = "") -> str:
    """Healthchecks: event is 'start', 'success' or 'fail'. No-op when unset."""
    base = os.getenv("HEALTHCHECKS_PING_URL")
    if not base:
        return "not_configured"
    suffix = {"start": "/start", "success": "", "fail": "/fail"}[event]
    return _post(
        f"{base.rstrip('/')}{suffix}?rid={run_id}",
        body.encode("utf-8")[:100_000],  # Healthchecks stores the first 100 kB
        "text/plain; charset=utf-8",
    )


def alert(text: str) -> str:
    """Slack Incoming Webhook. Unset or down: the alert goes to the console."""
    url = os.getenv("SLACK_WEBHOOK_URL")
    outcome = _post(url, json.dumps({"text": text}).encode(), "application/json") if url else "not_configured"
    if outcome != "ok":
        print(text)
    return outcome


NEXT_ACTION = {
    "ok": "Nothing failed, but the agent keeps finding nothing. Check the data feeds and "
    "whether the signal thresholds still fit the portfolio.",
    "partial": "Review the blocked items in the forensic report before acting on today's brief.",
    "failed": "Do not act on today's brief. Read the reasons and the forensic report, fix the "
    "cause, then rerun once by hand: python -m revenue_agent.unattended",
}


def alert_text(row: dict) -> str:
    report = (
        f"{row['forensic_report_path']} (sha256 {row['forensic_report_sha256']})"
        if row["forensic_report_path"]
        else "none written (the run did not produce a result)"
    )
    warnings = json.loads(row["warnings"])
    if row["accounts_scanned"] is None:  # the run raised before AgentRunner returned a result
        actual = (f"scan metrics unavailable; the run failed before a result was produced "
                  f"({row['retry_count']} model retries)")
    else:
        actual = (f"{row['accounts_scanned']} scanned, {row['accounts_shortlisted']} shortlisted, "
                  f"{row['recommendations_published']} recommendations accepted, "
                  f"{row['recommendations_blocked']} blocked, {row['refused_calls']} refused calls, "
                  f"{row['failed_calls']} failed calls, {row['retry_count']} model retries")
    return "\n".join(
        [
            f"RevOps Agent - {row['status'].upper()}" + (" with WARNING" if warnings else ""),
            f"run_id: {row['run_id']}",
            f"job: {row['job']} ({row['mode']})",
            "Expected: a daily brief from the full account scan, every recommendation "
            "accepted, no refused or failed tool calls",
            f"Actual: {actual}",
            f"Publications: {row['publications_new']} new, "
            f"{row['publications_skipped_duplicate']} duplicates skipped",
            f"Reason: {'; '.join(json.loads(row['problems'])) or 'none'}",
            *([f"Warning: {'; '.join(warnings)}"] if warnings else []),
            "External writes: 0. This job has no business-system write path; "
            "proposed tasks exist only in the run result.",
            f"Next action: {NEXT_ACTION[row['status']]}",
            f"Forensic report: {report}",
        ]
    )


# -- one run -----------------------------------------------------------------------
def run_once(make_runner, *, live: bool, ledger: Path = LEDGER) -> dict:
    """Run the job once and persist its ledger row. Never raises for the job.

    Order: run, classify, invariants, publish (one transaction), receipt,
    ledger row, monitoring. A crash after `publish` commits leaves the
    publications in place and a rerun skips them; a crash inside it rolls the
    whole transaction back and a rerun publishes them.
    """
    from .daily import write_receipt  # daily imports the SDK path lazily too

    run_id = str(uuid.uuid4())  # Healthchecks rid must be a canonical UUID
    mode = MODE_LIVE if live else MODE_OFFLINE
    monitoring = {"healthchecks_start": ping("start", run_id)}
    started_at, started = _now(), time.monotonic()

    runner = result = error = None
    try:
        runner = make_runner()
        result = runner.run()
    except Exception as exc:
        error = exc
    retries = getattr(runner, "model_retries", 0)
    status, problems = classify(
        result, error, model_failure=error is not None and error is getattr(runner, "model_error", None)
    )
    if error is not None and retries:
        problems[0] += f" (after {retries + 1} attempts)"

    violations = invariants(runner, result) if result is not None else []
    if violations:
        status = "failed"
        problems += [f"invariant: {v}" for v in violations]

    new, duplicate = [], []
    if result is not None and not violations:
        new, duplicate = publish(ledger, run_id, mode, result.published)

    report_path = report_sha = None
    if result is not None:
        try:
            path = write_receipt(
                result, live, label="unattended", run_id=run_id,
                extra={"unattended": {
                    "status": status,
                    "problems": problems,
                    "invariant_violations": violations,
                    "model_retries": retries,
                    "publications_new": new,
                    "publications_skipped_duplicate": duplicate,
                }},
            )
            report_path = str(path)
            report_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception as exc:  # the result exists but cannot be explained later
            problems.append(f"operational_error: receipt not written: {type(exc).__name__}")
            status = "failed" if status == "failed" else "partial"

    t = result.telemetry if result is not None else None
    m = tool_metrics(result) if result is not None else {}
    row = {
        "run_id": run_id,
        "job": JOB,
        "mode": mode,
        "started_at": started_at,
        "finished_at": _now(),
        "duration_s": round(time.monotonic() - started, 3),
        "status": status,
        "accounts_scanned": t and t.accounts_scanned,
        "accounts_shortlisted": t and t.accounts_shortlisted,
        "recommendations_published": len(result.published) if result else 0,
        "recommendations_blocked": len(result.rejected) if result else 0,
        "read_attempts": m.get("read_tool_calls"),
        "proposal_attempts": m.get("proposal_tool_calls"),
        "refused_calls": m.get("refused_tool_calls"),
        "failed_calls": m.get("failed_tool_calls"),
        # provider-reported tokens x config prices; offline is genuinely zero
        "actual_cost_usd": t.estimated_cost_usd if t else None,
        "retry_count": retries,
        "problems": json.dumps(problems),
        "forensic_report_path": report_path,
        "forensic_report_sha256": report_sha,
        "monitoring": None,
        "publications_new": len(new),
        "publications_skipped_duplicate": len(duplicate),
    }
    row["warnings"] = json.dumps(silent_success_warning(ledger, mode, row))
    # persist first: a monitoring outage must not lose the record
    _sql(ledger, f"INSERT INTO runs ({', '.join(row)}) VALUES ({', '.join(':' + k for k in row)})", row)

    needs_human = status != "ok" or json.loads(row["warnings"])
    text = alert_text(row) if needs_human else ""
    monitoring["healthchecks_end"] = ping("success" if status == "ok" else "fail", run_id,
                                          text or f"{JOB} ok")
    monitoring["slack"] = alert(text) if needs_human else "not_needed"
    row["monitoring"] = json.dumps(monitoring)
    _sql(ledger, "UPDATE runs SET monitoring = ? WHERE run_id = ?", (row["monitoring"], run_id))
    return row


def _runner(live: bool, inject: str | None) -> AgentRunner:
    if live:
        from .agent import AnthropicClient

        # SDK retries off: AgentRunner's loop is the one retry policy
        client = AnthropicClient(max_retries=0, timeout=MODEL_TIMEOUT_S)
    else:
        from .testing import ScriptedClient

        client = ScriptedClient(inject={inject: True} if inject else None)
    return AgentRunner(
        InMemoryRepository(),
        client,
        max_cost_usd=config.UNATTENDED_MAX_COST_USD if live else None,
        max_refusal_streak=config.UNATTENDED_MAX_REFUSAL_STREAK,
        max_model_attempts=config.UNATTENDED_MAX_MODEL_ATTEMPTS,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="use the real model")
    parser.add_argument("--list", type=int, metavar="N", help="print the N most recent runs")
    parser.add_argument(
        "--inject", choices=INJECTIONS,
        help="offline drill only: make the scripted client misbehave",
    )
    args = parser.parse_args()

    if args.list:
        cols = ("started_at", "run_id", "mode", "status", "recommendations_published",
                "recommendations_blocked", "publications_new", "publications_skipped_duplicate",
                "retry_count", "actual_cost_usd", "problems", "warnings", "monitoring")
        for r in recent(limit=args.list):
            print("  ".join(str(r[c]) for c in cols))
        return 0
    if args.live and args.inject:
        parser.error("--inject is an offline drill; it cannot be combined with --live")

    row = run_once(lambda: _runner(args.live, args.inject), live=args.live)
    print(f"{row['status']}  run_id={row['run_id']}  report={row['forensic_report_path']}")
    print(f"recommendations accepted={row['recommendations_published']} "
          f"new publications={row['publications_new']} "
          f"duplicates skipped={row['publications_skipped_duplicate']} "
          f"model retries={row['retry_count']}")
    print(f"problems: {row['problems']}  warnings: {row['warnings']}")
    print(f"monitoring: {row['monitoring']}")
    return 0 if row["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
