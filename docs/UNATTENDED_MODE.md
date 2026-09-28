# Unattended mode

## 1. What the agent does

Every day the deterministic core scans 30 synthetic DTC accounts and turns
their facts into signals. It shortlists up to 8 accounts for the model. The
model investigates them with read-only tools and may propose (never create)
one CRM task per account. It then submits a brief. A deterministic publication
gate decides what is published, checking citations, account scope, freshness,
reconciliation and blocked actions. The gate derives confidence; the model
never supplies it. Nothing is written to any business system.

## 2. What unattended mode adds

One entrypoint that runs that same job once, with no one watching:

```
python -m revenue_agent.unattended            # offline scripted client (default, no spend)
python -m revenue_agent.unattended --live     # real model; unattended never implies live
```

Around the unchanged agent it adds:

- post-run invariants
- idempotent publication
- a ledger row per run
- a Healthchecks heartbeat
- a Slack alert when a human is needed
- bounded retry of transient model errors
- two loop guards

Prompts, tools, budgets, signals, the gate and the eval expectations are
untouched.

Each run goes through these steps in order: run the agent, classify, check
invariants, publish (one transaction), write the receipt, write the ledger row,
then ping and alert. See `run_once()` in `src/revenue_agent/unattended.py`.

## 3. Run statuses

| status | meaning |
|---|---|
| `ok` | Completed with no rejection and no refused, failed or unknown tool call. Publishing nothing with no refusal ("nothing needs a human today") is `ok`. |
| `partial` | At least one recommendation was accepted, but another was blocked, or a tool call was refused, failed, or named a tool that does not exist (`forbidden_tool_attempt`). |
| `failed` | Any of: the run raised (`model_error` for provider errors, including retries exhausted; `operational_error` for anything else); no recommendation was accepted while something was refused (no brief, an invalid brief, a guard stop, every recommendation blocked); or an invariant failed. |
| `missed` | The job never reported. Only Healthchecks can say this. |

The exact logic is in `classify()` and `run_once()`. Every reason is kept in
the row's `problems` column.

## 4. Why "missed" is detected outside the job

A job that never started, or that hung, or whose machine was off, cannot write
"I did not run". The ledger only ever holds runs that happened. Healthchecks
expects a ping on a schedule and alerts when none arrives within the grace
time. That is the only place `missed` exists.

**To demonstrate a missed run:** do not start the job. When the check's period
plus grace time passes with no ping, Healthchecks marks it late, then down, and
alerts. A shorter period on a test check makes this quick. The job itself never
writes `missed`.

## 5. Deterministic invariants

These run after the agent finishes, before anything is published. Each one
re-applies an existing rule to the recommendations the run accepted; none is a new
policy.

| | invariant | how it is checked |
|---|---|---|
| A | scan completeness | `accounts_scanned` equals the dataset's own account count, and the shortlist the model got equals a fresh full scan |
| B | publication integrity | each accepted recommendation is re-run through the existing `gate_recommendation` and must pass with the same confidence; at most 3 are accepted |
| C | write boundary | every tool call that ran is in the tool registry, and every proposal is still `PROPOSED` |
| D | evidence isolation | the evaluator's `isolation_checks`: no cross-account or unissued ref, no foreign task, no off-list read, and expansion only after that account's activity read |
| E | freshness | no accepted action is one that the existing `blocked_actions` rule (stale data or a reconciliation exception) forbids |
| F | budgets | 4 reads and 1 proposal per account, 14 tool calls and 20 turns per run, and, for live runs, cost within the unattended budget |

A violation makes the run `failed`, adds `invariant: ...` to `problems`, and
publishes nothing.

**Silent success.** When the latest `UNATTENDED_SILENT_SUCCESS_STREAK` runs
(default 3) for the same job and mode were all `ok` and accepted no recommendations, the
run records `silent_success_streak` in `warnings` and sends a Slack alert. The
status stays `ok`, so Healthchecks still gets a success ping. The streak is
read from the ledger, so it survives restarts. Any run that accepts a recommendation, or is
not `ok`, breaks it. The default of 3 is a demo value: how many quiet days are
normal is a RevOps/Operations decision.

## 6. Idempotent publication

A recommendation is published when it gets a row in the ledger's
`publications` table. Rerunning or retrying the same job must never publish
the same recommendation twice.

**Identity.** The key is the SHA-256 of:

- the job
- the mode (`offline_scripted` or `live`)
- the business date (`AS_OF`)
- the account
- the action
- the sorted `signal:<account>:<type>` refs it cites

The signal refs are issued by the deterministic scan, so two recommendations
for one account and action on the same day that rest on different signals are
different recommendations. Prose, priority, confidence, tool refs and task ids
are left out, because they can vary between runs of the same recommendation.
Mode is included so a scripted offline run can never suppress a live one.

**Duplicates.** `INSERT OR IGNORE` against the primary key skips a
recommendation that is already present. The `PRIMARY KEY` is the race-safe
final boundary. A skipped duplicate is not a failure and triggers no alert. It
is counted in `publications_skipped_duplicate` and listed in the receipt.

**Crash ordering.** All of a run's publications are inserted in one SQLite
transaction.

- A crash after that transaction commits leaves the publications in place and
  no ledger row. A rerun skips them as duplicates.
- A crash inside the transaction rolls all of it back. A rerun publishes them
  normally, because nothing was suppressed.

The only in-between state is "published, but no ledger row". Its signature is a
Healthchecks `/start` with no success or fail, plus `publications` rows whose
`run_id` has no ledger row.

## 7. Retry policy

Only transient provider errors are retried: timeouts, connection errors, HTTP
408, 409 and 429, and any 5xx. These are the same conditions the Anthropic
SDK retries. `UNATTENDED_MAX_MODEL_ATTEMPTS` (default 3) sets the attempts per
model call, first try included. Backoff is 1 s, then 2 s, capped at 4 s.

Everything else is never retried. A bad citation, schema failure, gate block,
stale or reconciliation block, out-of-scope account, budget refusal, cost
stop, refusal-streak stop or ambiguous business condition is an outcome. None
of them raises, and running the model again would not make it trustworthy.
A 4xx other than 408, 409 or 429 is a request that will fail the same way
again.

**One policy, not two.** The SDK (anthropic 1.8.0) retries twice by default.
Unattended live runs build the client with `max_retries=0` and a 120 s request
timeout, so `AgentRunner`'s loop is the only retry and the `retry_count` in the
ledger is the real number. When retries run out, the run is `failed` with
`model_error ... (after N attempts)`, and nothing is published. Retries happen
inside a run, before publication, and a rerun goes through idempotent
publication, so a retry cannot duplicate anything.

## 8. Run ledger

`runs/unattended.sqlite3` uses stdlib `sqlite3` and is gitignored. It has two
tables:

- **`runs`**, one row per run: status, counts, tool attempts, cost,
  `retry_count`, `problems`, `warnings`, `publications_new`,
  `publications_skipped_duplicate`, the forensic receipt's path and SHA-256, and
  `monitoring` (the outcome of each Healthchecks and Slack call). Only `ok`,
  `partial` and `failed` are accepted, by a database `CHECK`. A Phase 2A ledger
  gains the new columns in place and keeps its rows.
- **`publications`**, one row per recommendation ever published (section 6).

The run row is written before any monitoring call.

```
python -m revenue_agent.unattended --list 10
```

Receipts carry the run id in their name and are opened exclusively, so two
runs in the same second never overwrite each other.

## 9. Healthchecks heartbeat

Set `HEALTHCHECKS_PING_URL`. The contract was verified against
healthchecks.io/docs/http_api:

- `POST <url>/start?rid=<uuid>` when the run starts
- `POST <url>?rid=<uuid>` for `ok`, with or without a warning
- `POST <url>/fail?rid=<uuid>` for `partial` or `failed`, with the alert text as
  the body

`rid` is the ledger `run_id`. Each request makes one attempt with a 10 s
timeout. If the variable is unset, pings are no-ops. If Healthchecks is down,
the failure is recorded in `monitoring` and the run is unchanged.

## 10. Slack escalation

Set `SLACK_WEBHOOK_URL` to an Incoming Webhook. It is used for `partial`,
`failed` and a silent-success warning; `ok` is otherwise silent. The alert
states:

- run id, job and mode
- what was expected and what actually happened, including retries and
  new/duplicate publications
- every reason and warning
- that external writes were 0
- the safest next action
- the receipt's path and hash

Terms used in alerts:

- **recommendations accepted**: passed the gate this run
- **new publications**: rows newly inserted into `publications`
- **duplicates skipped**: accepted, but already published under the same key

A run that failed before producing a result says "scan metrics unavailable"
instead of showing empty counts.

If the webhook is unset or down, the alert is printed to the console. Neither
URL is ever printed or persisted, and both are redacted from forensic artifacts.

## 11. Spend and loop controls

- **`UNATTENDED_MAX_COST_USD` (default 0.50), live only.** Before each model
  call the loop stops if the cost spent so far plus an estimate of that call's
  worst case would exceed the budget (`unattended_cost_budget`). The output
  side of the estimate is exact: `max_tokens` × output price. The input side
  counts characters, which normally overstates tokens but is not a proof.
  **This is a best-effort run budget, not a hard ceiling.** For a hard cap, set
  an Anthropic Console spend limit. A request that times out may still be
  billed without reporting usage.
- **`UNATTENDED_MAX_REFUSAL_STREAK` (default 3).** Three refused tool calls in
  a row stop the loop (`refusal_streak_limit`). A successful call resets the
  count.
- **Unchanged:** the 20-turn limit, the 14-attempt run cap and the per-account
  budgets.

## 12. Failure drills

`--inject <name>` makes the offline scripted client misbehave. It is
deterministic, lives only in `testing.py`, and is refused together with `--live`.

| drill | what it shows | expected |
|---|---|---|
| `unknown_ref` | **bad citation**: cites a source that was never issued | gate `unknown_source_reference`, `failed` |
| `bad_schema` | invalid brief | `schema_validation_failed`, `failed` |
| `extra_account` | recommends an account never shortlisted | refused, `partial` |
| `force_expansion` | expands whatever the facts say | the existing gate blocks wherever its rules apply |
| `stale_data` | expands `harbor_home`, whose data is 76 h old | existing gate `expansion_blocked_by_stale_data`, `partial` |
| `llm_timeout` | every model call times out | 3 attempts, `retry_count` 2, `failed`, no result, no publications |
| `forbidden_write_attempt` | calls `update_crm_record`, which is not a tool | fails as `unknown tool`, nothing runs, `forbidden_tool_attempt`, `partial` |

## 13. Run once by hand

From the repository root, where `runs/` lives:

```
set PYTHONPATH=src                                    # Windows cmd
python -m revenue_agent.unattended                    # offline
python -m revenue_agent.unattended --live             # real model
python -m revenue_agent.unattended --inject stale_data
python -m revenue_agent.unattended --list 5
```

The exit code is 0 for `ok` and 1 otherwise.

## 14. Scheduling

### Windows Task Scheduler

`HEALTHCHECKS_PING_URL`, `SLACK_WEBHOOK_URL` and `ANTHROPIC_API_KEY` must be
user environment variables of the account the task runs as. One command
creates a daily 07:00 run (drop `--live` to schedule the offline job):

```
schtasks /Create /TN "RevOpsAgentDaily" /SC DAILY /ST 07:00 /TR "cmd /c cd /d C:\AI-Workspace\Projects\dtc-revenue-ops-agent && set PYTHONPATH=src&& python -m revenue_agent.unattended --live >> runs\unattended.log 2>&1"
```

To set it up in the GUI instead:

1. Open Task Scheduler and choose Create Task.
2. General: name it `RevOpsAgentDaily` and choose "Run only when user is
   logged on". "Run whether user is logged on or not" also works, but it asks
   for the password.
3. Triggers: New, Daily, 07:00.
4. Actions: New.
   - Program: `cmd`.
   - Arguments: `/c set PYTHONPATH=src&& python -m revenue_agent.unattended --live >> runs\unattended.log 2>&1`.
   - Start in: `C:\AI-Workspace\Projects\dtc-revenue-ops-agent`.
5. Settings: turn on "Stop the task if it runs longer than 1 hour" and set "If
   the task is already running" to "Do not start a new instance".

Check it with `schtasks /Run /TN "RevOpsAgentDaily"`, then
`python -m revenue_agent.unattended --list 1`. `python` must resolve on the
task's PATH; if it does not, use the full path to `python.exe`.

### cron

```
0 7 * * * cd /path/to/dtc-revenue-ops-agent && PYTHONPATH=src python3 -m revenue_agent.unattended --live >> runs/unattended.log 2>&1
```

cron does not load your shell profile, so define the three variables in the
crontab or in a file it sources.
