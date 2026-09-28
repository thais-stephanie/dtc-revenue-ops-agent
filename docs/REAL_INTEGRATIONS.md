# Real integrations: the read-only lab

`src/revenue_agent/integrations/` holds read-only adapters for HubSpot, Shopify
and Klaviyo, a doctor, and offline contract and parity tests. **They are not
connected to the agent.** The evals, the demo, the unattended job and every
agent-behaviour test read only the synthetic repository, and a test enforces
that nothing outside `integrations/` imports the lab.

## Why synthetic data stays the evaluation oracle

The synthetic portfolio has **known ground truth**: each planted situation
(stale data, a customer's pause, a billing hold) has a right answer, so the
evals can score the model. A live account has no answer key. Worse, it
changes under you. If a score drops next week you cannot tell whether the
model regressed or the data moved. So model quality is measured on synthetic
data, and real systems are connected read-only, separately. Connected is not
the same as trusted: live data earns trust through the same deterministic
checks (freshness, reconciliation, provenance) before it can reach a
recommendation.

## What the adapters prove, and what they don't

They prove the contracts:
- only reads, with Shopify limited to fixed query documents
- bounded pagination that reports when it stopped early
- typed errors for 401, 403, 429 (Retry-After kept), 5xx, timeouts and
  malformed payloads
- no token in any error or doctor output
- the doctor's read-only verdicts
- a HubSpot mapping that rebuilds the synthetic CRM facts exactly: all 30
  accounts and all 31 activities, from HubSpot-shaped JSON served through
  mocked HTTP
- the same deterministic shortlist (the same 8 accounts, in the same order)
  when CRM facts come from the adapter

They do not prove anything about a real portal's data. **A new test account is
mostly empty**, so a green doctor proves connectivity and scope, not data
quality.

## Mapping

| Synthetic field | Vendor object / field | Notes |
|---|---|---|
| `Account.id` | HubSpot company `dtc_account_id` | custom property (manual UI step, not needed for the doctor) |
| `Account.brand_name` | company `name` | |
| `Account.vertical` | company `dtc_vertical` | custom; HubSpot `industry` is a fixed enum that doesn't fit |
| `Account.lifecycle_stage` | company `dtc_account_stage` | custom; HubSpot `lifecyclestage` values (lead, customer…) mean something else |
| `Account.account_owner` | company `hubspot_owner_id` → owners `firstName` | |
| `Account.created_at` | company `createdate` | |
| `CrmNote` (note / call) | HubSpot notes (`hs_note_body`) / calls (`hs_call_body`), `hs_timestamp`, `hubspot_owner_id`, company association | exactly one associated company is required; otherwise the account is left empty, never guessed |
| abandoned carts | Shopify `abandonedCheckouts` | the synthetic data has daily aggregates; Shopify has checkout records |
| orders, gross revenue | Shopify `orders` (`totalPriceSet`, `displayFinancialStatus`) | |
| lapsed / active customers | Klaviyo profiles and segments | depends on how each brand defines "lapsed" |
| campaign and flow activity | Klaviyo campaigns, flows, metrics | |

**What does not map:**
- Direct-mail campaign spend, ROAS and attributed revenue come from this
  product's own campaign data, not from HubSpot, Shopify or Klaviyo.
- Invoices and overdue days belong to the billing system (for example
  Stripe). HubSpot invoices are a different object.
- The reconciliation "commerce attributed revenue" needs an attribution
  definition that none of the three vendors provides as-is.
- HubSpot deals and tasks can be read, but the synthetic model has no deals
  or tasks to compare them with.
- Synthetic note ids are replaced by HubSpot record ids.
- `commerce_platform` is commerce metadata, not CRM data.

## What goes wrong with real APIs

- **Stale data:** a sync lags behind, so the freshness rule must see real
  timestamps.
- **Missing associations:** a note linked to no company, or to two companies.
- **Partial pagination:** a page limit or an error mid-way. Every list returns
  a `complete` flag.
- **Permissions change:** a scope is removed, and yesterday's `ok` becomes
  `403`.
- **Rate limits:** HubSpot allows about 100–190 requests per 10 s per private
  app, Klaviyo campaigns 10/s burst and 150/min, and Shopify throttles
  GraphQL by query cost.
- **Schema evolution:** API versions and revisions are pinned (Shopify
  `2026-07`, Klaviyo `2026-07-15`) and must be moved deliberately.
- **Duplicate identities:** one brand appearing as two companies, one shopper
  as two profiles.
- **Different attribution definitions:** Shopify, Klaviyo and a mail vendor
  will each claim the same order.

## Order of work (manual, with read-only credentials)

1. **HubSpot first.** The CRM holds the account activity the agent reads
   most, and a read-only HubSpot smoke test is useful on its own.
2. **Shopify second, optional.**
3. **Klaviyo third, optional.**

None of them needs to be connected to run the demo.

### Prepare (once)

Nothing in this project reads a `.env` file (it is gitignored anyway). In
PowerShell, set only the variables for the vendor you are testing, for this
session only:

```
$env:HUBSPOT_ACCESS_TOKEN = "<paste>"      # never echo it back
$env:PYTHONPATH = "src"
python -m revenue_agent.integrations.doctor
```

The doctor prints `configured` or `missing`, never a value. It prints counts,
never record contents.

### 1. HubSpot: a read-only private app ("legacy app")

**VERIFIED CURRENT DOCUMENTATION** (2026-09-27):
- developers.hubspot.com/docs/guides/apps/private-apps/overview: private apps
  are now "Legacy apps", created under Development → Legacy apps → Create
  legacy app → Private. The token is under the Auth tab (Show token, Copy).
  A token's scopes can be read back with
  `POST /oauth/v2/private-apps/get/access-token-info`, which the doctor uses.
- developers.hubspot.com/docs/guides/apps/authentication/scopes: read scopes
  `crm.objects.companies.read`, `crm.objects.deals.read`,
  `crm.objects.owners.read`.
- developers.hubspot.com/docs/api-reference/crm-companies-v3/basic/get-crm-v3-objects-companies:
  listing companies needs `crm.objects.companies.read`.

**MANUAL STEP FOR THAIS:**
1. In the HubSpot test account: Development → Legacy apps → Create legacy
   app → Private. Name it `revops-agent-readonly`.
2. Scopes tab → Add new scope. Add **only `crm.objects.companies.read`**. That
   is the minimum for the doctor's smoke read (one page of one company).
   Optional for fuller reads: `crm.objects.owners.read` and
   `crm.objects.deals.read`. Notes and calls: the scopes page lists none, so
   **VERIFY IN VENDOR UI**. Do not add any `.write` scope, `tickets`,
   `e-commerce` or `content`.
3. Create the app. Auth tab → Show token → Copy, straight into
   `$env:HUBSPOT_ACCESS_TOKEN` (not into a file you might commit).
4. Run the doctor. Expected: `CONFIGURED / VERIFIED READ-ONLY`, connection
   `ok`, sample read `ok (0 records...)` or `(1 record...)`. If it says
   `UNSAFE`, delete the app and recreate it with read scopes only.
   `SCOPE UNKNOWN` means the scope response had an unrecognised scope or no
   `scopes` field (**VERIFY**: that field name is not confirmed, because the
   reference page returned 404).

### 2. Shopify: optional

**VERIFIED CURRENT DOCUMENTATION** (2026-09-27):
- shopify.dev/docs/apps/build/authentication-authorization/access-tokens/generate-app-access-tokens-admin:
  "You can no longer create new admin-created custom apps". New apps go
  through the Dev Dashboard or Shopify CLI.
- shopify.dev/docs/api/admin-graphql/latest/objects/AppInstallation:
  `currentAppInstallation.accessScopes` lists the granted scopes; the latest
  version is `2026-07`.
- shopify.dev/docs/api/admin-graphql/latest/queries/abandonedCheckouts:
  needs `read_orders`.

**MANUAL STEP FOR THAIS:** how a Dev Dashboard app on your own store yields an
Admin API token is **VERIFY BEFORE BUILDING**; the page I checked does not say.
It may need an install/OAuth step, which is out of scope tonight. If you do
get a token:
- The minimum scope is **`read_orders`** only (it covers the orders and
  abandoned-checkouts reads). Never grant any `write_*` scope.
- Set `$env:SHOPIFY_STORE_DOMAIN = "<store>.myshopify.com"` and
  `$env:SHOPIFY_ADMIN_TOKEN`.
- Expected doctor result: `CONFIGURED / VERIFIED READ-ONLY`. Any `write_`
  scope makes it `UNSAFE`.

### 3. Klaviyo: optional

**VERIFIED CURRENT DOCUMENTATION** (2026-09-27):
- developers.klaviyo.com/en/docs/authenticate_: the header is
  `Authorization: Klaviyo-API-Key <key>` plus `revision: 2026-07-15`. Settings
  → API keys → Create Private API Key → "Read-Only Key". A key cannot be
  edited after creation, and there is no API that lists a key's scopes.
- developers.klaviyo.com/en/reference/get_campaigns: campaigns need a channel
  filter and `campaigns:read`; rate limits are 10/s burst and 150/min.

**MANUAL STEP FOR THAIS:**
1. Settings → API keys → Create Private API Key, name it
   `revops-agent-readonly`, and choose **Read-Only Key**.
2. Put it in `$env:KLAVIYO_PRIVATE_API_KEY` and run the doctor.
3. Expected: `CONFIGURED / SCOPE UNKNOWN`. That is honest, not a failure:
   Klaviyo has no documented way for the doctor to prove a key is read-only.
   You chose Read-Only in the UI, and that choice is the guarantee. The
   doctor's smoke read is `GET /api/accounts` (`accounts:read`, included in
   Read-Only).

## Doctor states

| State | Meaning | Exit code |
|---|---|---|
| NOT CONFIGURED | no credential; normal and safe | 0 |
| CONFIGURED / VERIFIED READ-ONLY | the vendor reported the token's scopes, and every one is a read | 0 |
| CONFIGURED / SCOPE UNKNOWN | it connects, but no documented mechanism proves the scopes (Klaviyo), or a scope was unrecognised | 0 |
| CONFIGURED / UNSAFE | a write scope was reported: recreate the token | 1 |
| CONFIGURED / CONNECTION FAILED | auth, forbidden, rate limit, timeout, network or server error | 1 |
| any state + sample read "refused by the vendor (HTTP 403)" | the token authenticated but may not read companies; the missing scope is named when the vendor listed the scopes | 1 |

**"Application write methods exposed: NO"** is a separate guarantee about this
code. The adapter modules have no mutating functions, and the doctor checks
their function names on every run. It says nothing about the token. That is
what "Credential write scopes" reports.

## Real HubSpot smoke: 2026-09-27

One real portal, structure only: no token, portal id, record id, name, email,
note or other CRM content was printed, saved or committed. GET requests,
`limit=1`/`limit=5`, plus the token-info POST.

| Question | Verified result |
|---|---|
| Connection | **ok**: the token authenticates (token-info and owners reads succeed) |
| Credential scope | **VERIFIED READ-ONLY**: HubSpot listed 13 scopes, every one `*.read` or `oauth` (deals, goals, invoices, leads, line_items, listings, marketing_events, orders, owners, products, quotes, services) |
| Companies | **refused (HTTP 403)**: the token has no `crm.objects.companies.read`, the one scope the setup step above asks for. Company reads, the company sample and company associations are therefore **unverified** |
| Other reads | 200 for deals (0 records), notes (0), calls, tasks and owners. Notes, calls and tasks were readable **without** any notes/calls/tasks scope in the list, so "each object needs its own `.read` scope" does not hold for these activity objects in this portal |
| Paging | `paging.next.after` plus `paging.next.link`; the `paging` key is absent on the last page and on empty results. Matches the adapter |
| Record shape | `id`, `properties` (`hs_createdate`, `hs_lastmodifieddate`, `hs_object_id` by default), `createdAt`, `updatedAt`, `archived`, `url` |
| Owners | `id`, `email`, `firstName`, `lastName`, `userId`, `userIdIncludingInactive`, `type`, `createdAt`, `updatedAt`, `archived` |
| Associations | **unverified**: with `associations=companies,contacts,deals` no readable record had any association, so the response carried no `associations` key. The adapter's assumed shape (`associations.companies.results[].id`) is still untested against a real portal |
| Token info | returns `appId`, `hubId`, `isUserToken`, `scopes`, `userId`; `scopes` is a list of strings (this settles the earlier VERIFY on the field name) |
| Mutation audit | business reads are GET only; the one HubSpot POST is the token-info request (authentication metadata, not a business write). No public adapter function writes; nothing outside `integrations/` imports the lab |

**Source metadata a HubSpot read can truthfully provide:** `source_system =
"hubspot"`, `record_type` (the object type requested), `record_id` (`id`),
`retrieved_at` (the time of the read; HubSpot's `updatedAt` is when the record
last changed, not when it was read), and `source_url` = the record's own `url`
field, passed through verbatim, never built from ids.

**Source URL classification: A (verified canonical link), for the objects
seen.** Every record returned (calls, tasks) carried a `url` on
`https://app.hubspot.com/...` whose path contains the record id, and HubSpot's
"Using Object APIs" reference shows the same `url` field in its example record
response. Limits: the docs show it without describing it (no stated stability
guarantee), and it was observed on calls and tasks only, not on companies or
notes (none readable). So "Open in HubSpot" may use the returned `url` as
`source_url`, verbatim, per record, and only where a read returned one; it must
never be constructed from a portal id and record id.

**Doctor fix made by this smoke:** a 403 on the sample read after the token
authenticated was reported as `CONNECTION FAILED`, discarding the verified scope
list. It now reports connection `ok`, the scope result, and "refused by the
vendor (HTTP 403): the token has no crm.objects.companies.read scope".

## Real HubSpot re-smoke: 2026-09-28 (after adding the company scope)

Same portal, same rules: structure only, nothing identifying printed or
committed. GET reads plus the token-info POST; 41 records read in total
(company pages of 1, 1 and 31, then at most 4 per object type for
associations).

| Question | Verified result |
|---|---|
| Connection | **ok** |
| Credential scope | **VERIFIED READ-ONLY**: 23 scopes, none containing `write`. `crm.objects.companies.read` is **present**. HubSpot also listed `crm.objects.companies.sensitive.read.v2`; the doctor used to call that "unrecognised" (SCOPE UNKNOWN) and now accepts a `.read.vN` suffix as a read (its `.write.vN` twin is still UNSAFE; tested) |
| Companies | **readable**: 200, `results` list, each record has `id`, `properties` (a mapping), `createdAt`, `updatedAt`, `archived`, `url`. The doctor's sample read returns 1 record |
| Company `url` | **present on every company read**, `https`, host `app.hubspot.com` |
| Paging | `paging.next.after` + `paging.next.link` on a non-final page (limit=1); `paging` absent on the final page (limit=100 returned the whole small portal). Matches the adapter, unchanged |
| Associations | **observed: company → contacts** as `associations.contacts.results[]` of `{id, type}`; the `associations` key is absent when a record has none. Activity → company (what the note/call mapping reads) was **not observed in this portal** (no call or task had a company association; there are no notes or deals). The structure matches the adapter's assumption, but that exact direction is still unverified |
| Calls / tasks / notes / deals | calls and tasks readable (same record shape, `url` on `app.hubspot.com`); notes and deals readable but empty (0 records, no `paging`) |
| Mutation audit | unchanged: business reads are GET only; the one POST is token-info (token metadata/introspection, not a business write); `_http` refuses PUT/PATCH/DELETE and any other POST before sending |

**Source metadata (implemented):** `hubspot.source_metadata(record,
object_type, retrieved_at)` returns `source_system="hubspot"`, `record_type`,
`record_id`, `source_url` (the record's own `url` verbatim, else `null`) and
`retrieved_at` (our read time, never `updatedAt`). These are the fields Account
Radar's evidence items already carry.

**Source navigation: supported when the API supplies a record URL.** The
console shows "Open in HubSpot ↗" only for `source_system` hubspot with an
`https` URL whose host is exactly `app.hubspot.com` (no port, no userinfo);
`http`, other domains, lookalikes (`app.hubspot.com.evil…`,
`…@evil…`) and a missing URL fall back to "View recorded evidence". The link
opens with `target=_blank rel="noopener noreferrer"`. Shopify and Klaviyo
record URLs were never verified, so they never link. EU-hosted portals
(`app-eu1.hubspot.com`) were not observed and are not allowlisted. The
synthetic demo has no HubSpot records, so it shows "View recorded evidence"
everywhere, as it should.

