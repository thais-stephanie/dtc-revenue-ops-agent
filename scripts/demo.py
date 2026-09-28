"""Demo: short, screen-sized scenes over the existing unattended job.

    python scripts/demo.py reset            # before presenting, off screen
    python scripts/demo.py healthy
    python scripts/demo.py rerun
    python scripts/demo.py stale
    python scripts/demo.py forbidden-write
    python scripts/demo.py timeout
    python scripts/demo.py ledger [N]
    python scripts/demo.py bad-citation     # optional backup scene

Every scene is OFFLINE: the scripted client, never the real model. It only
calls revenue_agent.unattended.run_once and reads back the ledger row and the
forensic receipt that run wrote; no rule lives here. Output is plain ASCII.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_agent import config, unattended  # noqa: E402
from revenue_agent.tools import TOOL_DEFINITIONS  # noqa: E402

WRITES = "External business-system writes: 0"


# -- running and reading back ----------------------------------------------------------
def run(inject: str | None = None) -> tuple[dict, dict | None, str]:
    """One offline unattended run. Returns (ledger row, receipt or None, console fallback)."""
    console = io.StringIO()  # alert() prints here only when Slack is unset or unreachable
    with contextlib.redirect_stdout(console):
        row = unattended.run_once(lambda: unattended._runner(live=False, inject=inject), live=False)
    receipt = None
    if row["forensic_report_path"]:
        receipt = json.loads(Path(row["forensic_report_path"]).read_text())
    return row, receipt, console.getvalue()


def writes_line(row: dict) -> str:
    """0 is not asserted blindly: the write-boundary invariant must have held."""
    broken = [p for p in json.loads(row["problems"]) if p.startswith("invariant: write_boundary")]
    return f"{WRITES}  (no write path exists; write-boundary invariant held)" if not broken \
        else f"WRITE BOUNDARY VIOLATION: {broken}"


def monitoring_lines(row: dict, console: str) -> list[str]:
    m = json.loads(row["monitoring"])
    hc_end = "success" if row["status"] == "ok" else "fail"
    hc = ("not configured" if m["healthchecks_start"] == "not_configured"
          else f"start ping {m['healthchecks_start']}, {hc_end} ping {m['healthchecks_end']}")
    slack = {"not_needed": "not needed (nothing for a human to do)",
             "not_configured": "not configured", "ok": "alert sent"}.get(m["slack"], m["slack"])
    lines = [f"Healthchecks:                {hc}", f"Slack:                       {slack}"]
    if m["slack"].startswith("error") and console.strip():
        lines += ["", "Slack unreachable; the alert it would have sent:", console.rstrip()]
    return lines


def counts(row: dict) -> list[str]:
    return [
        f"Recommendations accepted:    {row['recommendations_published']}",
        f"New publications:            {row['publications_new']}",
        f"Duplicates skipped:          {row['publications_skipped_duplicate']}",
    ]


def show(title: str, lines: list[str], say: str | None = None) -> None:
    body = [title, "-" * 60, *lines]
    if say:
        body += ["", say]
    text = "\n".join(body)
    print(text.encode("ascii", "replace").decode("ascii") + "\n")


# -- scenes ------------------------------------------------------------------------------
def reset() -> None:
    ledger = unattended.LEDGER
    archived = "none (no ledger yet)"
    if ledger.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = ledger.with_name(f"unattended-archive-{stamp}.sqlite3")
        if target.exists():
            raise SystemExit(f"refusing to overwrite {target}; wait a second and retry")
        ledger.rename(target)  # never deleted
        archived = str(target)
    unattended._sql(ledger, "SELECT 1")  # creates the empty tables
    show("DEMO RESET  (run before presenting, off screen)",
         [f"Archived ledger:  {archived}", f"New ledger ready: {ledger}",
          "Receipts, reports, baselines, fixtures and data were not touched."])


def healthy() -> None:
    row, _, console = run()
    show("HEALTHY RUN", [
        f"Accounts scanned:            {row['accounts_scanned']}",
        f"Accounts shortlisted:        {row['accounts_shortlisted']}",
        *counts(row),
        f"Run status:                  {row['status']}",
        f"Run ID:                      {row['run_id']}",
        *monitoring_lines(row, console),
        writes_line(row),
    ])


def rerun() -> None:
    row, _, console = run()
    rows = unattended._sql(unattended.LEDGER,
                           "SELECT COUNT(*) AS n, COUNT(DISTINCT idempotency_key) AS k FROM publications")
    show("RERUN: SAME LOGICAL JOB AGAIN", [
        *counts(row),
        f"Run status:                  {row['status']}",
        f"Publications table:          {rows[0]['n']} rows, {rows[0]['k']} distinct keys",
        *monitoring_lines(row, console),
        writes_line(row),
    ], "The agent accepted the same recommendations again; the idempotency key\n"
       "(job, mode, business date, account, action, cited signals) already exists,\n"
       "so nothing is published twice. A duplicate is not a failure.")


def bad_citation() -> None:
    row, receipt, console = run("unknown_ref")
    rejections = receipt["rejections"]
    unknown = sorted({r for x in rejections for r in x["unknown_source_refs"]})
    reasons = sorted({x["reason"] for x in rejections})
    show("BAD CITATION", [
        f"Unknown ref:                 {', '.join(unknown)}",
        f"Gate result:                 {len(rejections)} of {len(rejections)} refused "
        f"({', '.join(reasons)})",
        f"Recommendations accepted:    {row['recommendations_published']}",
        f"New publications:            {row['publications_new']}",
        f"Run status:                  {row['status']}",
        *monitoring_lines(row, console),
        writes_line(row),
    ], "The model can submit a recommendation, but a source reference the system\n"
       "never issued cannot pass publication.")


def forbidden_write() -> None:
    row, receipt, console = run("forbidden_write_attempt")
    registered = {t["name"] for t in TOOL_DEFINITIONS}
    [attempt] = [e for e in receipt["tool_log"] if e["tool"] not in registered]
    show("FORBIDDEN WRITE ATTEMPT", [
        f"Attempted tool:              {attempt['tool']}",
        "Tool registered:             no",
        f"Tool result:                 {attempt['status']}, did not execute ({attempt['error']})",
        f"Recommendations accepted:    {row['recommendations_published']}",
        f"Run status:                  {row['status']}",
        *monitoring_lines(row, console),
        writes_line(row),
    ], "The model asked for a capability it does not possess. There is no CRM\n"
       "write path to reach: the call was refused locally and nothing left this machine.")


def stale() -> None:
    row, receipt, console = run("stale_data")
    [blocked] = [x for x in receipt["rejections"] if x["reason"] == "expansion_blocked_by_stale_data"]
    show("STALE DATA", [
        f"Account:                     {blocked['account_id']}",
        f"Commerce data age:           {blocked['details'][0].removeprefix('commerce data is ')}"
        f" (stale after {config.STALE_AFTER_HOURS:.0f}h)",
        "",
        f"MODEL RECOMMENDATION:        {blocked['recommended_action']}",
        f"PUBLICATION DECISION:        blocked by the deterministic gate "
        f"({blocked['reason']})",
        "",
        f"Recommendations accepted:    {row['recommendations_published']}",
        f"Recommendations blocked:     {row['recommendations_blocked']}",
        f"Run status:                  {row['status']}",
        *monitoring_lines(row, console),
        writes_line(row),
    ], "The model may suggest expansion. The freshness rule decides whether it\n"
       "publishes, and it does not publish on data older than the threshold.")


def timeout() -> None:
    print("(simulated provider timeouts; the retry backoff takes about 3 seconds)\n")
    row, _, console = run("llm_timeout")
    [reason] = json.loads(row["problems"])
    show("MODEL TIMEOUT", [
        f"Attempts:                    {row['retry_count'] + 1}",
        f"Retries:                     {row['retry_count']}",
        f"Final status:                {row['status']}",
        f"Reason:                      {reason.split(' (after')[0]}",
        "Scan metrics:                unavailable; the run failed before a result was produced",
        f"Recommendations accepted:    {row['recommendations_published']}",
        f"New publications:            {row['publications_new']}",
        *monitoring_lines(row, console),
        writes_line(row),
    ], "Only transient provider errors retry, a bounded number of times. When the\n"
       "attempts run out the run fails safely: no result, nothing published.")


def ledger(n: int = 10) -> None:
    rows = unattended.recent(limit=n)
    header = f"{'time (UTC)':19} {'run':8} {'mode':7} {'status':7} {'acc':>3} {'blk':>3} " \
             f"{'new':>3} {'dup':>3} {'rty':>3} {'cost':>7}  reason / warning"
    lines = [header, "-" * len(header)]
    for r in reversed(rows):
        # the most specific reason is the last one; keep its code, drop the detail
        note = (json.loads(r["problems"] or "[]")[-1:] or json.loads(r.get("warnings") or "[]")[:1]
                or ["-"])[0]
        note = ": ".join(note.split(" (")[0].split(": ")[:2])
        cost = r["actual_cost_usd"]
        lines.append(
            f"{r['started_at'][:19].replace('T', ' ')} {r['run_id'][:8]} "
            f"{'live' if r['mode'] == 'live' else 'offline':7} {r['status']:7} "
            f"{r['recommendations_published'] or 0:>3} {r['recommendations_blocked'] or 0:>3} "
            f"{r['publications_new'] or 0:>3} {r['publications_skipped_duplicate'] or 0:>3} "
            f"{r['retry_count']:>3} {'$%.4f' % cost if cost is not None else '-':>7}  {note[:48]}"
        )
    show(f"RUN LEDGER (latest {len(rows)})", lines,
         "acc = recommendations accepted, blk = blocked, new = new publications,\n"
         "dup = duplicates skipped, rty = model retries. 'missed' never appears here:\n"
         "a run that never started cannot write a row. Healthchecks reports that.")


SCENES = {
    "reset": reset, "healthy": healthy, "rerun": rerun, "bad-citation": bad_citation,
    "forbidden-write": forbidden_write, "stale": stale, "timeout": timeout, "ledger": ledger,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in SCENES:
        print(__doc__)
        return 2
    os.chdir(ROOT)  # the ledger and receipts live under runs/ at the repo root
    if argv[0] == "ledger" and len(argv) > 1:
        ledger(int(argv[1]))
    else:
        SCENES[argv[0]]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
