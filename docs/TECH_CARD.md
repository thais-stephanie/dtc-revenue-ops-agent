# Tech card (one page)

## Numbers

| | |
|---|---|
| Portfolio | 30 synthetic DTC accounts → top **8** shortlisted → at most **3** recommendations |
| Evals | **14/14** (offline pipeline; live canaries in `evals/BASELINES.md`) |
| Tests | **305** unit and contract tests, all passing |
| Live unattended run | **$0.1345**, 3 accepted, 0 retries, invariants held (BASELINES run 5) |
| External business-system writes | **0**, by construction: no write path exists |
| Model timeout drill | **3 attempts / 2 retries**, then `failed`, nothing published |
| Stale-data drill | `harbor_home`, data **76 h** old (limit 48 h) → expansion blocked |
| Rerun (idempotency) | **3 accepted / 0 new publications / 3 duplicates skipped** |
| Model vs gate | the model recommends `EXPAND_CART_RECOVERY`; the deterministic gate decides `blocked: expansion_blocked_by_stale_data` |

## Five principles

1. **Deterministic where possible.** Arithmetic, ranking, freshness,
   reconciliation, citations and idempotency are code. The model interprets
   and explains.
2. **Least privilege.** The model has read tools and one proposal tool.
   Adapters are read-only, and tokens are checked for write scopes.
3. **Fail closed.** An unknown source, stale data, a missing activity read or
   an off-list account means the recommendation is refused, not guessed.
4. **Observe before tuning.** Every live run is kept as a baseline. A failure
   is classified (model, contract or evaluator) before anything changes.
5. **Retries require idempotency.** A rerun can never publish twice, and only
   transient provider errors are retried.

## Four common questions

**"Why synthetic data?"** Because I need to know the right answer to grade
the model. The synthetic portfolio has nine planted situations with known
correct actions, so a score means something. With a live account I couldn't
tell whether a score dropped because the model got worse or because the data
changed. Real systems connect separately, read-only, and never feed the
evaluation.

**"How would you connect HubSpot?"** Read first: a private app with read
scopes only. The doctor verifies those scopes through HubSpot's token-info
endpoint and fails if a write scope appears. Writes come later, through a
gateway. The model proposes a change-set; code checks policy and version,
applies the exact diff, and re-reads to verify. A 2xx isn't proof. The model
never holds the write token.

**"What happens when it fails at night?"** It writes a ledger row for every run
with ok, partial or failed and the exact reason, plus a hashed forensic
receipt. Partial or failed runs alert Slack and ping Healthchecks' fail
endpoint. If the job never starts at all, Healthchecks notices the missing
heartbeat, because a process can't report that it didn't run. Provider
timeouts retry a bounded number of times; nothing else retries, and a rerun
can't publish twice.

**"How would this roll out against real data?"** Week 1: learn how the team
works and which accounts get attention today, and why. Week 2: connect
HubSpot read-only against real data and measure freshness, associations and
duplicate identities, not model quality. Week 3: run the agent in shadow mode
next to the humans and compare. Week 4: pick one low-risk write, internal
follow-up tasks, and put it behind the approval gateway on a small slice
first. Every step keeps its own evidence trail.
