# Architecture

```mermaid
flowchart TB
  subgraph EVAL["1 - EVALUATION CORE (built, tested; synthetic data only)"]
    direction LR
    P[(Synthetic portfolio<br/>30 accounts, known ground truth)] --> S[Deterministic scan<br/>signals, rank]
    S --> SL[Shortlist<br/>top 8]
    SL --> C[Claude<br/>investigates, recommends]
    C <--> T[Bounded read tools<br/>4 reads + 1 proposal per account<br/>14 calls per run]
    C --> R[Structured recommendation<br/>+ cited refs]
    R --> G{Publication gate<br/>deterministic, fail closed}
    G -->|accepted| I[Idempotent publication<br/>SQLite key]
    G -->|blocked| X[Rejection, recorded]
    I --> L[(Ledger + receipts)]
    X --> L
    L --> M[Slack alerts<br/>Healthchecks heartbeat<br/>read-only console]
  end

  subgraph LAB["2 - READ-ONLY ADAPTERS (built, mocked tests; NOT connected to zone 1)"]
    direction LR
    H[HubSpot] --- D[doctor<br/>configured? reachable?<br/>read-only?]
    SH[Shopify] --- D
    K[Klaviyo] --- D
    WALL["No arrow from here into zone 1:<br/>synthetic evaluation data is separate<br/>from real integration data"]
  end

  subgraph FUTURE["3 - FUTURE WRITE PATH (designed only: SAFE_WRITE_GATEWAY.md)"]
    direction LR
    PC[Change-set proposal] --> SW[Safe-write gateway] --> PA{Policy / approval}
    PA --> AP[Deterministic applier] --> RB[Read-back verification]
  end

  C -. proposes, never writes .-> PC
```

**The wall between zones 1 and 2 is deliberate.** The evals score the model
against a portfolio whose right answers are known. Real systems have no answer
key and change underneath you, so mixing them in would make a score drop
unexplainable. A test fails if anything outside `integrations/` imports the
lab (`tests/test_integrations.py`). Zone 3 shares zone 1's machinery (refs,
gate style, idempotency keys, ledger, monitoring) but has no code yet.

## Who decides what

| DETERMINISTIC (code) | LLM (Claude) | HUMAN |
|---|---|---|
| arithmetic: ROAS, deltas, cart volume | which extra context matters | policy: thresholds, allow-lists |
| scan and rank; shortlist | interpreting CRM notes (a customer's pause) | approving higher-risk actions |
| data freshness | weighing conflicting signals | ambiguous decisions |
| revenue reconciliation | explaining trade-offs | exception review (alerts, partial runs) |
| citation membership (ref issued for this account, this run) | drafting the recommendation and task text | |
| idempotency | | |
| authorization and account scope | | |
| optimistic concurrency (future) | | |
| write verification (future) | | |

## Where each piece lives

| Piece | File |
|---|---|
| Synthetic portfolio, planted situations | `src/revenue_agent/dataset.py` |
| Scan and shortlist | `signals.py`, `repository.py`, `reconciliation.py` |
| Agent loop, budgets, retries, guards | `agent.py`, `tools.py` |
| Publication gate, confidence | `guardrails.py`, `confidence.py` |
| Unattended run, invariants, idempotency, ledger, monitoring | `unattended.py` (docs: `UNATTENDED_MODE.md`) |
| Operator console (read-only) | `console.py` |
| Read-only adapters and doctor | `integrations/` (docs: `REAL_INTEGRATIONS.md`) |
| Future write path | `docs/SAFE_WRITE_GATEWAY.md` |
| Evals and live baselines | `evals/cases.yaml`, `evals/BASELINES.md` |
