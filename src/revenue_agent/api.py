"""Minimal demo surface: today's brief, and the approval step.

Deliberately small. The interesting part of this project is the pipeline, not
the UI. Two routes: read the brief, approve a proposed task. Approval is the
only path that can ever execute a write.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from . import config
from .agent import AgentRunner
from .repository import InMemoryRepository
from .schemas import RunResult
from .testing import ScriptedClient

app = FastAPI(title="DTC Revenue Operations Agent")

STATE: dict = {"run": None, "tasks": {}}


def _runner(live: bool) -> AgentRunner:
    repo = InMemoryRepository()
    if live:
        from .agent import AnthropicClient

        return AgentRunner(repo, AnthropicClient())
    return AgentRunner(repo, ScriptedClient())


@app.post("/runs/daily")
def run_daily(live: bool = False) -> dict:
    runner = _runner(live)
    result = runner.run()
    STATE["run"] = result
    for item in result.published:
        task_id = item.recommendation.proposed_task_id
        if task_id:
            STATE["tasks"][task_id] = {
                "account_id": item.recommendation.account_id,
                "title": item.recommendation.headline,
                "status": "PROPOSED",
                "approved_at": None,
            }
    return result.model_dump()


@app.post("/tasks/{task_id}/approve")
def approve(task_id: str) -> dict:
    task = STATE["tasks"].get(task_id)
    if not task:
        raise HTTPException(404, "unknown proposed task")
    if task["status"] != "PROPOSED":
        return task
    task["status"] = "EXECUTED"
    task["approved_at"] = datetime.now(timezone.utc).isoformat()
    # This is where a real CRM write would happen, behind the human's click.
    return task


@app.get("/", response_class=HTMLResponse)
def brief() -> str:
    result: RunResult | None = STATE["run"]
    if result is None:
        result = _runner(False).run()
        STATE["run"] = result

    cards = []
    for item in result.published:
        rec = item.recommendation
        tension = (
            f"<p class='tension'>{html.escape(rec.acknowledged_tension)}</p>"
            if rec.acknowledged_tension
            else ""
        )
        task = (
            f"<form method='post' action='/tasks/{rec.proposed_task_id}/approve'>"
            f"<button>Approve &amp; create task</button></form>"
            if rec.proposed_task_id
            else ""
        )
        cards.append(
            f"""
        <article class="card {item.confidence}">
          <div class="meta">{rec.priority.upper()} · {rec.category} ·
            confidence {item.confidence} <span>({html.escape(item.confidence_reason)})</span></div>
          <h2>{html.escape(rec.headline)}</h2>
          <p>{html.escape(rec.rationale)}</p>
          {tension}
          <p class="action">{rec.recommended_action.value}</p>
          <p class="src">sources: {html.escape(', '.join(rec.source_refs))}</p>
          {task}
        </article>"""
        )

    rejected = "".join(
        f"<li><b>{html.escape(r.account_id or '-')}</b> {html.escape(r.reason)} "
        f"{html.escape('; '.join(r.details))}</li>"
        for r in result.rejected
    )
    t = result.telemetry
    return f"""
<!doctype html><meta charset="utf-8"><title>Daily Brief</title>
<style>
 body{{font:15px/1.5 system-ui,sans-serif;margin:0;background:#0f1418;color:#e8edf1}}
 main{{max-width:820px;margin:0 auto;padding:32px 20px 64px}}
 h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#8ea0ad;margin:0 0 28px}}
 .card{{border:1px solid #223039;border-radius:10px;padding:18px;margin-bottom:14px;background:#141b21}}
 .card.low{{border-left:3px solid #c98a4b}} .card.high{{border-left:3px solid #6fbf8b}}
 .card.medium{{border-left:3px solid #6f9cbf}}
 .meta{{font:11px ui-monospace,monospace;letter-spacing:.08em;text-transform:uppercase;color:#8ea0ad}}
 .meta span{{text-transform:none;letter-spacing:0}}
 h2{{font-size:17px;margin:8px 0}} .action{{font:12px ui-monospace,monospace;color:#9fd2b4}}
 .src{{font:11px ui-monospace,monospace;color:#6d7e8a}}
 .tension{{border-left:2px solid #c98a4b;padding-left:10px;color:#e3c9a8}}
 button{{background:#1f2c35;color:#e8edf1;border:1px solid #2c3d49;border-radius:6px;padding:8px 12px;cursor:pointer}}
 ul{{color:#c98a4b;font-size:13px}} footer{{color:#6d7e8a;font-size:12px;margin-top:28px}}
</style>
<main>
  <h1>Daily brief</h1>
  <p class="sub">Synthetic portfolio · demo clock {config.AS_OF.date()}</p>
  {''.join(cards) or '<p>Nothing needs a human today.</p>'}
  {f'<h3>Rejected by the publication gate</h3><ul>{rejected}</ul>' if rejected else ''}
  <footer>{t.accounts_scanned} scanned · {t.accounts_shortlisted} shortlisted ·
   {t.accounts_recommended} published · {t.tool_calls} tool calls · {t.duration_ms} ms ·
   ${t.estimated_cost_usd:.4f}</footer>
</main>"""
