# Demo (3-5 minutes)

Everything below runs offline, against a scripted model, with no spend. Run
the commands from the repository root in PowerShell:

```
cd C:\AI-Workspace\Projects\dtc-revenue-ops-agent
```

## A. 30-second explanation

> This is an internal RevOps agent working over a synthetic portfolio of 30
> DTC brands. Plain code does the scanning: it computes signals and picks the
> handful of accounts worth a look today. Claude then investigates those
> accounts through a small set of read-only tools, and can at most propose a
> CRM task. Nothing the model says is trusted directly. Every recommendation
> goes through a deterministic publication gate that checks its citations,
> account scope, data freshness and reconciliation before anyone sees it.
> What I'll show is how it behaves when it runs on its own, with nobody
> watching.

## B. The walkthrough

### 0. Reset: before presenting, off screen

```
python scripts/demo.py reset
```

This archives the current ledger under a timestamped name (it is never
deleted) and creates an empty one, so `healthy` shows fresh publications. Do
not run it on screen.

### 1. Healthy run (~40 s)

```
python scripts/demo.py healthy
```

- **Point at:** scanned 30, shortlisted 8, `Recommendations accepted: 3`,
  `New publications: 3`, status `ok`, and the Healthchecks line.
- **Say:** "This is the daily job run unattended. Code scanned 30 accounts, the
  model looked at the shortlist, and three recommendations passed the gate and
  were published. Healthchecks got a start ping and a success ping."
- **Proves:** the whole pipeline runs end to end with no one watching, and it
  reports in.

### 2. Rerun (~30 s)

```
python scripts/demo.py rerun
```

- **Point at:** `Recommendations accepted: 3`, `New publications: 0`,
  `Duplicates skipped: 3`, and `3 rows, 3 distinct keys`.
- **Say:** "The same job ran again, which is what a retry or a double-fired
  schedule looks like. The agent accepted the same three recommendations, and
  none was published twice. Each has an idempotency key made of the job, date,
  account, action and the signals it cites, and the database enforces it."
- **Proves:** reruns are safe, and a duplicate is not treated as a failure (no
  alert).

### 3. Stale data (~40 s)

```
python scripts/demo.py stale
```

- **Point at:** `MODEL RECOMMENDATION: EXPAND_CART_RECOVERY` next to
  `PUBLICATION DECISION: blocked ...`, and the 76h data age.
- **Say:** "Here the model recommends expanding an account whose commerce data
  is three days old. The model makes a suggestion; the gate makes the
  publication decision. The freshness rule is plain code, so the
  recommendation is blocked and the run is marked partial."
- **Proves:** a wrong model output fails closed, through the existing rule and
  not a special case.

### 4. Forbidden write (~30 s)

```
python scripts/demo.py forbidden-write
```

- **Point at:** `Attempted tool: update_crm_record`, `Tool registered: no`,
  `did not execute`.
- **Say:** "The model asked for a capability it doesn't have. There is no CRM
  write tool, so the call fails locally, it's recorded, and a human is
  alerted. Nothing left this machine."
- **Proves:** the model's reach is limited to the tool registry, and an attempt
  outside it is visible, not silent.

### 5. Model timeout (~30 s, including about 3 s of backoff)

```
python scripts/demo.py timeout
```

- **Point at:** `Attempts: 3`, `Retries: 2`, `Final status: failed`,
  `New publications: 0`.
- **Say:** "Provider timeouts are the kind of failure worth retrying, so it
  retries a bounded number of times. Then it gives up cleanly. It doesn't
  retry things like a bad citation, because that would just produce the same
  bad answer again."
- **Proves:** retries are bounded and counted, and a failed run publishes
  nothing.

### 6. Ledger (~40 s)

```
python scripts/demo.py ledger
```

- **Point at:** one row per run, with its status, and the `new` versus `dup`
  columns.
- **Say:** "Every run leaves one row in a local SQLite ledger, with the status,
  counts, retries, cost and the reason. What you won't find is 'missed': a
  job that never started can't write a row. That's what the external heartbeat
  is for." Then go to section C.
- **Proves:** each run is explainable afterwards, and each row points to a
  forensic receipt (path and SHA-256) with the full tool log.

Optional, never required: `python -m revenue_agent.console` (with `$env:PYTHONPATH="src"`)
serves the same ledger and receipts read-only at http://127.0.0.1:8765/
(`?as=sofia|jordan|alex` to view as one account manager; use browser zoom for screen share). It is
Account Radar, a morning brief drawn to the approved V4 design: Today, My portfolio, Team overview,
account detail, Control room, Data sources and History, with technical details one click away.
Screenshots: `docs/img/radar-*-v4-compact.png`. Each raised account has one next step (Review
recommendation, Review investigation or Investigate blocker) that opens the recorded reasoning, evidence
and any drafted task; nothing is applied anywhere. Screenshots: `docs/img/radar-*-actionable.png`,
`docs/img/radar-review-*.png`.

### Optional backup: bad citation

```
python scripts/demo.py bad-citation
```

- **Say:** "The model can submit a recommendation, but a source reference the
  system never issued can't pass publication."

With `SLACK_WEBHOOK_URL` and `HEALTHCHECKS_PING_URL` set, `stale`,
`forbidden-write`, `timeout` and `bad-citation` send a real Slack alert and a
Healthchecks `/fail`. `healthy` and `rerun` send a success ping and no Slack
alert. Have the Slack channel and the Healthchecks dashboard open in browser
tabs.

## C. The missed-run story

A process cannot report that it never started. If the machine is off, the
scheduler breaks or the job hangs, there is no code left to write "I didn't
run". So the job pings Healthchecks, an outside service that expects a ping
on a schedule. If none arrives in time, Healthchecks raises the alarm itself.

The drills send `/fail` pings, which already turn the check red. A live
"missed" demonstration after them would be ambiguous, so it is captured
beforehand as a screenshot.

**At least 1 hour before presenting, in the browser:**

1. In PowerShell run `python scripts/demo.py healthy`. The check receives a
   success ping and turns green ("up").
2. Open healthchecks.io, sign in, and click the check's name.
3. Find the **Schedule** section and click **Change Schedule...**. Leave it on
   "Simple". Set **Period** to **1 minute** and **Grace Time** to **1 minute**
   (1 minute is the minimum for each), then click **Save**.
4. Do nothing. Do not run any demo command. After the period passes, the
   check shows **late** (amber). When the grace time has also passed it shows
   **down** (red), and Healthchecks sends a notification through the
   integrations configured on the check, such as email. Healthchecks checks
   periodically, so allow a few minutes.
5. Take a screenshot of the check page showing "down" and its event log, and
   one of the notification you received.
6. Put the schedule back: **Change Schedule...**, **Period 1 day**, **Grace
   Time 1 hour**, **Save**.
7. Run `python scripts/demo.py healthy` once more, so the check is green again.
8. Then, still before presenting, run `python scripts/demo.py reset`.

**While presenting, say:** "Here the job simply didn't run. Nothing in my code
could report that, because nothing ran. Healthchecks expected a ping, didn't
get one within period plus grace, marked the check down, and notified me."

Healthchecks' notification is sent by Healthchecks itself, not by this
project's Slack webhook. It goes wherever the check's integrations point.

## D. If something fails during the demo

| failure | what still works |
|---|---|
| Internet | Every scene. They are offline, the monitors fail quietly, and the ledger and receipts are local. The scenes print whether Healthchecks and Slack could be reached. |
| Slack | Every scene. The Slack line shows the error, and the alert text is printed in the terminal instead. |
| Healthchecks | Every scene. The Healthchecks line shows the error; the run is unaffected. Use the missed-run screenshot. |
| Anthropic | Nothing in the demo calls it. The earlier live run is recorded in `evals/BASELINES.md` (run 5) with its receipt hash. |

The story can be told from the scenes, the ledger and a receipt alone. Open
the `runs/*.json` file named in the ledger to show the full tool log.

How writes to HubSpot would work: [SAFE_WRITE_GATEWAY.md](SAFE_WRITE_GATEWAY.md).

## E. What this demo does NOT prove

- The portfolio is synthetic.
- The offline scenes and drills use a scripted model, not Claude. Model
  quality evidence is the live runs recorded in `evals/BASELINES.md`.
- The demo never calls HubSpot. The read-only HubSpot adapter was verified
  against a real portal (structure only, GET reads), but it is not wired into
  the agent, the evals or the demo.
- There is no Shopify integration.
- There is no Klaviyo integration.
- There is no Metabase integration.
- There are no business-system writes of any kind; proposed tasks are
  in-memory proposals.
- The USD cost guard is a best-effort run budget, not a provider-side hard cap.
- A citation passing the gate means the ref was issued for that account this
  run. It does not mean the ref semantically supports the claim.
- The silent-success threshold (3) is a demo value, not a calibrated business
  policy.
