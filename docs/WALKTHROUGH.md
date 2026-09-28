# Account Radar walkthrough

A 5-minute path through the local console. Checked on 2026-09-28.

## Start the console

From the repository root, in PowerShell:

```
powershell -ExecutionPolicy Bypass -File scripts\start_demo.ps1
```

Or by hand: `$env:PYTHONPATH="src"` then `python -m revenue_agent.console`.
Both run offline: no Claude, no HubSpot, no writes. Ctrl+C stops it.

The ledger (`runs/unattended.sqlite3`, local, not in Git) ends on a **healthy**
run: 30 scanned → 8 shortlisted → 3 accepted, status ok. The earlier scenes
(rerun, stale, forbidden write, timeout, bad citation) are still in History.

## Recommended first URL

http://127.0.0.1:8765/?as=sofia — "Hello, Sofia · 1 account in your portfolio
needs attention today". Then switch to All account managers for the team brief
(3 accounts).

## Three strongest technical stories

1. **Cinder: the evaluator passed an empty result.** The first live canary
   "passed" while publishing nothing, because the gate had refused its only
   recommendation (unknown source). The fix was generic: a case that calls the model
   fails if nothing survives the gate, and citations must be refs the system issued
   for that account (`evals/BASELINES.md` 1–3).
2. **Stale-data gate.** Harbor Home's commerce data is too old, so an
   expansion is blocked deterministically before it can publish, whatever the
   model says.
3. **Unattended safety.** A rerun publishes nothing twice (idempotency key), a
   forbidden write tool is refused and recorded, and a model timeout retries a
   bounded number of times, then fails loudly (ledger row, receipt, Slack,
   Healthchecks).

## HubSpot status (verified 2026-09-28, real portal, structure only)

- Connection: **ok**
- Credential scopes: **VERIFIED READ-ONLY** (23 scopes, none with `write`)
- `crm.objects.companies.read`: **present**; company reads **verified**
- Paging (`paging.next.after`/`link`, absent on the last page): **verified**
- Record `url` pass-through: **verified** (every company, call and task record
  has an `https://app.hubspot.com` url); Radar shows "Open in HubSpot ↗" only
  for that host and never builds a URL
- Associations: company → contacts shape observed; activity → company **not
  observed** in this portal
- Business writes: **none** (GET only; the one POST is token-info introspection)

To check it yourself (prints no values):
```
$env:PYTHONPATH="src"
$env:HUBSPOT_ACCESS_TOKEN=[Environment]::GetEnvironmentVariable("HUBSPOT_ACCESS_TOKEN","User")
python -m revenue_agent.integrations.doctor
Remove-Item Env:HUBSPOT_ACCESS_TOKEN
```

## Known limitations

- The portfolio is synthetic; offline scenes use a scripted model, not Claude.
- HubSpot is read-only and not wired into the agent; the demo never shows real
  CRM data, so every evidence item says "View recorded evidence".
- Activity → company association not observed live; EU HubSpot host not
  allowlisted.
- Shopify and Klaviyo: adapters exist, never connected.
- No business-system writes exist; the write path is a design
  (`docs/SAFE_WRITE_GATEWAY.md`).
