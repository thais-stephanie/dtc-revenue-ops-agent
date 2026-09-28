"""Run one daily brief from the command line.

    python -m revenue_agent.daily              # offline, scripted client
    python -m revenue_agent.daily --live       # real model, needs ANTHROPIC_API_KEY
"""
from __future__ import annotations

import argparse
import json
import pathlib
import uuid
from datetime import datetime, timezone

from . import config
from .agent import AgentRunner
from .forensics import (
    MODE_LIVE,
    MODE_OFFLINE,
    REPORT_SCHEMA_VERSION,
    redact,
    run_forensics,
)
from .repository import InMemoryRepository
from .schemas import RunResult
from .testing import ScriptedClient


def build_runner(live: bool) -> AgentRunner:
    repo = InMemoryRepository()
    if live:
        from .agent import AnthropicClient

        return AgentRunner(repo, AnthropicClient())
    return AgentRunner(repo, ScriptedClient())


def render(result: RunResult) -> str:
    lines = ["", "  DAILY BRIEF", "  " + "-" * 62]
    if not result.published:
        lines.append("  Nothing needs a human today.")
    for item in result.published:
        rec = item.recommendation
        lines += [
            f"  [{rec.priority.upper()} · {rec.category}] {rec.headline}",
            f"    action      {rec.recommended_action.value}",
            f"    confidence  {item.confidence}  ({item.confidence_reason})",
            f"    why         {rec.rationale}",
        ]
        if rec.acknowledged_tension:
            lines.append(f"    tension     {rec.acknowledged_tension}")
        lines.append(f"    sources     {', '.join(rec.source_refs)}")
        if rec.proposed_task_id:
            lines.append(
                f"    task        {rec.proposed_task_id} (PROPOSED - needs human approval)"
            )
        lines.append("")
    if result.rejected:
        lines += ["  REJECTED BY THE PUBLICATION GATE", "  " + "-" * 62]
        for rejection in result.rejected:
            lines.append(
                f"  {rejection.account_id or '-':22} {rejection.reason} "
                f"{'· ' + '; '.join(rejection.details) if rejection.details else ''}"
            )
        lines.append("")
    t = result.telemetry
    lines += [
        "  RUN",
        "  " + "-" * 62,
        f"  {t.accounts_scanned} accounts scanned · {t.accounts_shortlisted} shortlisted · "
        f"{t.accounts_recommended} published",
        f"  {t.tool_calls} tool calls · {t.duration_ms} ms · "
        f"{t.input_tokens + t.output_tokens} tokens · ${t.estimated_cost_usd:.4f}",
        "",
    ]
    return "\n".join(lines)


def write_receipt(
    result: RunResult,
    live: bool,
    label: str | None = None,
    run_id: str | None = None,
    extra: dict | None = None,
) -> pathlib.Path:
    """One JSON per run. This is the evidence the README and the Loom quote.

    The name carries a run id, so two runs in the same second get two
    receipts, and the file is opened exclusively, so no receipt is ever
    overwritten.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = pathlib.Path("runs")
    directory.mkdir(exist_ok=True)
    suffix = f"-{label}" if label else ""
    run_id = run_id or uuid.uuid4().hex[:8]
    path = directory / f"{stamp}-{'live' if live else 'offline'}{suffix}-{run_id}.json"
    receipt = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "run_id": path.stem,
        "mode": MODE_LIVE if live else MODE_OFFLINE,
        "demo_as_of": config.AS_OF.isoformat(),
        **result.telemetry.model_dump(),
        "outputs_rejected": len(result.rejected),
        "rejection_reasons": [r.reason for r in result.rejected],
        "actions": [
            p.recommendation.recommended_action.value for p in result.published
        ],
        "confidence": [p.confidence for p in result.published],
        **run_forensics(result, live=live),
        **(extra or {}),
    }
    with path.open("x") as f:
        f.write(json.dumps(redact(receipt), indent=2, default=str))
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--label", help="name this receipt, e.g. baseline")
    args = parser.parse_args()

    runner = build_runner(args.live)
    result = runner.run()
    receipt = write_receipt(result, args.live, args.label)
    print(json.dumps(result.model_dump(), indent=2, default=str) if args.json else render(result))
    print(f"  receipt: {receipt}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
