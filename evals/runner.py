"""Evaluation runner.

    python evals/runner.py                 # offline, scripted client, no spend
    python evals/runner.py --live          # against the real model
    python evals/runner.py --case attribution_exception

Offline runs measure the PIPELINE: budgets, the publication gate, confidence
derivation, telemetry. Model quality numbers only mean something with --live.
The report says which mode produced it, so the two never get confused.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from datetime import datetime, timezone

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_agent import config  # noqa: E402
from revenue_agent.evaluation import run_case  # noqa: E402
from revenue_agent.forensics import (  # noqa: E402
    MODE_LIVE,
    MODE_OFFLINE,
    REPORT_SCHEMA_VERSION,
    redact,
)
from revenue_agent.repository import InMemoryRepository  # noqa: E402
from revenue_agent.testing import ScriptedClient  # noqa: E402

CASES = yaml.safe_load((ROOT / "evals" / "cases.yaml").read_text())


def _make_client(live: bool):
    if not live:
        # investigates up to 3 shortlisted accounts; single-account cases have one
        return ScriptedClient()
    from revenue_agent.agent import AnthropicClient

    return AnthropicClient()


def build_report(results: list[dict], *, live: bool, label: str | None, selected) -> dict:
    """Schema v2. Everything needed to reconstruct each run, secrets removed."""
    model_cases = [r for r in results if r.get("actions") is not None]
    usage = [r["usage"] for r in model_cases]
    reported = sorted({m for r in model_cases for m in r.get("models_reported", [])})
    requested = sorted({r["model_requested"] for r in model_cases if r.get("model_requested")})
    return redact(
        {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "mode": MODE_LIVE if live else MODE_OFFLINE,
            "mode_description": "LIVE MODEL" if live else "OFFLINE (scripted client: pipeline only)",
            "label": label,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "demo_as_of": config.AS_OF.isoformat(),
            "dataset_seed": config.DATASET_SEED,
            "cases_selected": selected or "all",
            "model": (reported[0] if len(reported) == 1 else None) if live else None,
            "model_requested": (requested[0] if len(requested) == 1 else requested or None)
            if live
            else None,
            "models_reported": reported,
            "summary": {
                "cases": len(results),
                "passed": sum(1 for r in results if r["passed"]),
                "portfolio_tests": len(results) - len(model_cases),
                "agent_behaviour_cases": len(model_cases),
                "empty_output_failures": sum(
                    1
                    for r in model_cases
                    for c in r["checks"]
                    if c["name"] == "non_empty_output" and not c["passed"]
                ),
                "unsafe_recommendations": sum(
                    1
                    for r in model_cases
                    for c in r["checks"]
                    if c["name"] == "no_forbidden_action" and not c["passed"]
                ),
                "gate_rejections": sum(len(r.get("rejections", [])) for r in results),
                **{
                    key: sum(r["tool_metrics"][key] for r in model_cases)
                    for key in (
                        "total_tool_attempts",
                        "read_tool_calls",
                        "proposal_tool_calls",
                        "refused_tool_calls",
                        "terminal_submits",
                    )
                },
                "input_tokens": sum(u["input_tokens"] for u in usage),
                "output_tokens": sum(u["output_tokens"] for u in usage),
                "total_tokens": sum(u["total_tokens"] for u in usage),
                "estimated_cost_usd": round(sum(u["estimated_cost_usd"] for u in usage), 6),
                "duration_ms": sum(r.get("duration_ms", 0) for r in results),
            },
            "results": results,
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="use the real model")
    parser.add_argument(
        "--case", action="append",
        help="run one case by id; repeat the flag for a canary subset",
    )
    parser.add_argument("--label", help="name this report, e.g. baseline")
    args = parser.parse_args()

    known = {c["id"] for c in CASES}
    unknown = sorted(set(args.case or []) - known)
    if unknown:
        # a typo would otherwise run zero cases and report a perfect score
        parser.error(f"unknown case id(s): {unknown}")

    repo = InMemoryRepository()
    cases = [c for c in CASES if not args.case or c["id"] in args.case]
    results = [
        run_case(c, repo, lambda: _make_client(args.live), live=args.live) for c in cases
    ]
    report = build_report(results, live=args.live, label=args.label, selected=args.case)
    s = report["summary"]

    portfolio = [r for r in results if r.get("actions") is None]
    behaviour = [r for r in results if r.get("actions") is not None]
    tool_calls = [r["tool_calls"] for r in behaviour if r["tool_calls"]]
    print(f"\n  mode                  {report['mode_description']}")
    if args.live:
        print(f"  model                 {report['model']}  (requested {report['model_requested']})")
    print(f"  portfolio tests       {sum(1 for r in portfolio if r['passed'])}/{len(portfolio)} "
          f"passed   (deterministic detection and ranking across all 30 accounts)")
    print(f"  agent behaviour cases {sum(1 for r in behaviour if r['passed'])}/{len(behaviour)} "
          f"passed   (one known scenario at a time, independent of daily rank)")
    print(f"  empty-output failures  {s['empty_output_failures']}")
    print(f"  unsafe recommendations {s['unsafe_recommendations']}")
    print(f"  gate rejections        {s['gate_rejections']}")
    if tool_calls:
        print(f"  avg tool calls/account {sum(tool_calls)/len(tool_calls):.1f}")
    print(f"  tool attempts          {s['total_tool_attempts']} "
          f"(reads {s['read_tool_calls']}, proposals {s['proposal_tool_calls']}, "
          f"refused {s['refused_tool_calls']}; submits {s['terminal_submits']})")
    print(f"  tokens in/out/total    {s['input_tokens']}/{s['output_tokens']}/{s['total_tokens']}")
    print(f"  total estimated cost   ${s['estimated_cost_usd']:.4f}")
    print(f"  total duration         {s['duration_ms']} ms\n")

    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        detail = ", ".join(
            f"{c['name']}={c['detail']}" for c in r["checks"] if not c["passed"]
        )
        print(f"  {mark}  {r['id']:34} {detail}")

    # Reports are never overwritten: the first live run is the baseline you
    # compare against after you change something.
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    suffix = f"-{args.label}" if args.label else ""
    reports = ROOT / "evals" / "reports"
    reports.mkdir(exist_ok=True)
    out = reports / f"{stamp}-{'live' if args.live else 'offline'}{suffix}.json"
    if out.exists():
        raise SystemExit(f"refusing to overwrite existing report {out}")
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\n  report written to {out.relative_to(ROOT)}\n")
    return 0 if s["passed"] == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
