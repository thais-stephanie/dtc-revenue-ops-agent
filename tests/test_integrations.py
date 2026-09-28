"""The read-only integration lab, fully offline: every vendor response is mocked.

Proves the boundary (reads only, fixed Shopify queries, bounded pagination,
typed errors, no secret in errors or doctor output), the doctor's states, the
HubSpot mapping against the synthetic dataset, and that nothing in the agent,
evals or demo depends on the lab.
"""
from __future__ import annotations

import collections
import contextlib
import dataclasses
import inspect
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_agent import signals  # noqa: E402
from revenue_agent.dataset import build_portfolio  # noqa: E402
from revenue_agent.domain import Account, CrmNote  # noqa: E402
from revenue_agent.integrations import _http, doctor, hubspot, klaviyo, shopify  # noqa: E402
from revenue_agent.integrations._http import IntegrationError  # noqa: E402
from revenue_agent.repository import InMemoryRepository  # noqa: E402

TOKEN = "pat-na1-SECRET-TOKEN-VALUE-0001"
VENDOR_ENV = ("HUBSPOT_ACCESS_TOKEN", "SHOPIFY_STORE_DOMAIN", "SHOPIFY_ADMIN_TOKEN", "KLAVIYO_PRIVATE_API_KEY")


class _Resp:
    def __init__(self, payload):
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(status, headers=None):
    return urllib.error.HTTPError("https://x", status, "err", headers or {}, io.BytesIO(b""))


class _Mocked(unittest.TestCase):
    """urlopen is patched; `self.route(request)` answers, `self.sent` records."""

    def setUp(self):
        self.sent = []
        self.urlopen = mock.patch("urllib.request.urlopen", side_effect=self._answer).start()
        self.addCleanup(mock.patch.stopall)

    def _answer(self, req, timeout):
        self.assertEqual(timeout, _http.TIMEOUT_S)
        body = json.loads(req.data) if req.data else None
        self.sent.append((req.get_method(), req.full_url, dict(req.header_items()), body))
        answer = self.route(req.get_method(), req.full_url, body)
        if isinstance(answer, BaseException):
            raise answer
        return _Resp(answer)

    def route(self, method, url, body):
        raise AssertionError(f"unexpected request {method} {url}")


# -- the transport ------------------------------------------------------------------------
class TestTransport(_Mocked):
    def test_get_returns_json(self):
        self.route = lambda *a: {"ok": 1}
        self.assertEqual(_http.request("v", "GET", "https://api.hubapi.com/x", {}), {"ok": 1})

    def test_only_two_fixed_post_targets_exist(self):
        for method, url in (("POST", "https://api.hubapi.com/crm/v3/objects/companies"),
                            ("PATCH", "https://api.hubapi.com/crm/v3/objects/companies/1"),
                            ("DELETE", "https://a.klaviyo.com/api/profiles/1"),
                            ("PUT", "https://shop.myshopify.com/admin/api/2026-07/graphql.json"),
                            ("POST", "https://evil.example/admin/api/2026-07/graphql.json"),
                            ("GET", "http://api.hubapi.com/insecure")):
            with self.assertRaises(IntegrationError) as refused:
                _http.request("v", method, url, {}, {"x": 1})
            self.assertEqual(refused.exception.kind, "refused", (method, url))
        self.urlopen.assert_not_called()

    def test_graphql_mutation_or_subscription_is_never_sent(self):
        url = "https://shop.myshopify.com/admin/api/2026-07/graphql.json"
        for document in ("mutation { productDelete(input: {id: 1}) { deletedProductId } }",
                         "  MUTATION  M { x }", "subscription { orders { id } }",
                         "query Q { shop { name } } mutation M { x }"):
            with self.assertRaises(IntegrationError) as refused:
                _http.request("shopify", "POST", url, {}, {"query": document})
            self.assertEqual(refused.exception.kind, "refused")
        self.urlopen.assert_not_called()

    def test_http_statuses_become_typed_errors(self):
        for status, kind in ((401, "auth"), (403, "forbidden"), (429, "rate_limited"),
                             (500, "server"), (503, "server"), (404, "http")):
            self.route = lambda *a, s=status: http_error(s, {"Retry-After": "7"})
            with self.assertRaises(IntegrationError) as err:
                _http.request("hubspot", "GET", "https://api.hubapi.com/x", {"Authorization": TOKEN})
            self.assertEqual((err.exception.kind, err.exception.status), (kind, status))
            self.assertNotIn(TOKEN, str(err.exception))
        self.assertEqual(err.exception.retry_after, 7)

    def test_timeouts_and_network_errors(self):
        for raised, kind in ((TimeoutError(), "timeout"),
                             (urllib.error.URLError(TimeoutError()), "timeout"),
                             (urllib.error.URLError(ConnectionRefusedError()), "network")):
            self.route = lambda *a, r=raised: r
            with self.assertRaises(IntegrationError) as err:
                _http.request("klaviyo", "GET", "https://a.klaviyo.com/api/x", {})
            self.assertEqual(err.exception.kind, kind)

    def test_malformed_payloads(self):
        for payload in (b"<html>maintenance</html>", b"[1, 2]"):
            self.route = lambda *a, p=payload: p
            with self.assertRaises(IntegrationError) as err:
                _http.request("klaviyo", "GET", "https://a.klaviyo.com/api/x", {})
            self.assertEqual(err.exception.kind, "bad_payload")


# -- the adapters ---------------------------------------------------------------------------
READ_NAMES = re.compile(r"^(get_|list_)")
MAPPING_HELPERS = {"account_fields", "activity_fields", "owner_names", "source_metadata"}


class TestReadOnlySurface(unittest.TestCase):
    def test_modules_expose_only_reads(self):
        for module in (hubspot, shopify, klaviyo):
            public = [n for n, f in inspect.getmembers(module, inspect.isfunction)
                      if f.__module__ == module.__name__ and not n.startswith("_")]
            self.assertTrue(public)
            for name in public:
                self.assertTrue(READ_NAMES.match(name) or name in MAPPING_HELPERS, f"{module.__name__}.{name}")
            self.assertEqual(doctor.write_methods(module), [])

    def test_the_write_detector_would_catch_a_write_method(self):
        fake = type(sys)("revenue_agent.integrations.fake")
        exec("def update_company(token, id, props): pass\ndef list_x(): pass", fake.__dict__)
        self.assertEqual(doctor.write_methods(fake), ["update_company"])

    def test_no_public_shopify_function_accepts_graphql_text(self):
        for name, fn in inspect.getmembers(shopify, inspect.isfunction):
            if fn.__module__ == shopify.__name__ and not name.startswith("_"):
                self.assertFalse({"query", "document", "graphql"} & set(inspect.signature(fn).parameters), name)

    def test_the_fixed_documents_are_reads(self):
        for name, document in shopify.QUERIES.items():
            self.assertTrue(document.startswith("query "), name)
            self.assertIsNone(_http._WRITE_OPERATION.search(document), name)
            self.assertEqual(document.count("{"), document.count("}"), name)


class TestShopify(_Mocked):
    DOMAIN = "demo-store.myshopify.com"

    def route(self, method, url, body):
        self.assertEqual(url, f"https://{self.DOMAIN}/admin/api/{shopify.API_VERSION}/graphql.json")
        self.assertIn(body["query"], shopify.QUERIES.values())  # only fixed documents reach the wire
        if "AccessScopes" in body["query"]:
            return {"data": {"currentAppInstallation": {"accessScopes": [{"handle": "read_orders"}]}}}
        field = "orders" if "Orders" in body["query"] else "abandonedCheckouts"
        return {"data": {field: {"edges": [{"node": {"id": f"gid://{field}/{len(self.sent)}"}}],
                                 "pageInfo": {"hasNextPage": True, "endCursor": f"c{len(self.sent)}"}}}}

    def test_known_reads_work_and_paging_is_bounded(self):
        self.assertEqual(shopify.get_access_scopes(self.DOMAIN, TOKEN), ["read_orders"])
        orders, complete = shopify.list_orders(self.DOMAIN, TOKEN, max_pages=3)
        checkouts, _ = shopify.list_abandoned_checkouts(self.DOMAIN, TOKEN, max_pages=2)
        self.assertEqual((len(orders), complete, len(checkouts)), (3, False, 2))
        self.assertEqual(self.sent[-1][3]["variables"]["after"], "c5")
        self.assertTrue(all(s[2]["X-shopify-access-token"] == TOKEN for s in self.sent))

    def test_a_tampered_document_still_cannot_mutate(self):
        with mock.patch.dict(shopify.QUERIES, {"access_scopes": "mutation M { shopUpdate { id } }"}):
            with self.assertRaises(IntegrationError) as refused:
                shopify.get_access_scopes(self.DOMAIN, TOKEN)
        self.assertEqual(refused.exception.kind, "refused")
        self.urlopen.assert_not_called()

    def test_store_domain_is_validated_before_any_request(self):
        for domain in ("evil.example", "shop.myshopify.com.evil.example", "", "a/b.myshopify.com"):
            with self.assertRaises(IntegrationError):
                shopify.get_access_scopes(domain, TOKEN)
        self.urlopen.assert_not_called()

    def test_graphql_errors_are_typed(self):
        self.route = lambda *a: {"errors": [{"message": "x", "extensions": {"code": "THROTTLED"}}]}
        with self.assertRaises(IntegrationError) as err:
            shopify.get_access_scopes(self.DOMAIN, TOKEN)
        self.assertEqual(err.exception.kind, "rate_limited")


class TestKlaviyo(_Mocked):
    def test_headers_paging_bound_and_host_pinning(self):
        pages = iter([{"data": [1], "links": {"next": "https://a.klaviyo.com/api/profiles?page[cursor]=2"}},
                      {"data": [2], "links": {"next": "https://a.klaviyo.com/api/profiles?page[cursor]=3"}}])
        self.route = lambda *a: next(pages)
        items, complete = klaviyo.list_resource(TOKEN, "profiles", max_pages=2)
        self.assertEqual((items, complete), ([1, 2], False))
        headers = self.sent[0][2]
        self.assertEqual((headers["Authorization"], headers["Revision"]),
                         (f"Klaviyo-API-Key {TOKEN}", klaviyo.REVISION))
        self.route = lambda *a: {"data": [], "links": {"next": "https://evil.example/api/profiles"}}
        with self.assertRaises(IntegrationError):
            klaviyo.list_resource(TOKEN, "profiles")
        with self.assertRaises(ValueError):
            klaviyo.list_resource(TOKEN, "templates")

    def test_campaigns_carry_the_required_channel_filter(self):
        self.route = lambda *a: {"data": []}
        klaviyo.list_resource(TOKEN, "campaigns")
        self.assertIn("filter=equals(messages.channel,'email')", self.sent[0][1])


class TestHubSpotContract(_Mocked):
    def test_paging_is_bounded_and_reported(self):
        self.route = lambda *a: {"results": [{"id": "1"}], "paging": {"next": {"after": "x"}}}
        rows, complete = hubspot.list_objects(TOKEN, "companies", max_pages=3)
        self.assertEqual((len(rows), complete, len(self.sent)), (3, False, 3))
        self.assertTrue(all(s[0] == "GET" and s[2]["Authorization"] == f"Bearer {TOKEN}" for s in self.sent))

    #: the structure a real portal returned on 2026-09-27 (placeholders only, no live values)
    LIVE_RECORD = {"id": "<id>", "properties": {"hs_createdate": "<timestamp>", "hs_lastmodifieddate": "<timestamp>",
                                                "hs_object_id": "<id>"},
                   "createdAt": "<timestamp>", "updatedAt": "<timestamp>", "archived": False,
                   "url": "https://app.hubspot.com/<path>"}

    def test_the_verified_live_list_shape(self):
        pages = iter([{"results": [self.LIVE_RECORD], "paging": {"next": {"after": "<cursor>", "link": "<link>"}}},
                      {"results": [self.LIVE_RECORD]}])  # the last page carries no paging key
        self.route = lambda *a: next(pages)
        rows, complete = hubspot.list_objects(TOKEN, "calls")
        self.assertEqual((len(rows), complete, len(self.sent)), (2, True, 2))
        self.assertEqual(parse_qs(urlsplit(self.sent[1][1]).query)["after"], ["<cursor>"])
        self.assertEqual(rows[0], self.LIVE_RECORD)  # passed through untouched, url included
        self.route = lambda *a: {"results": []}  # an empty object type: no paging key at all
        self.assertEqual(hubspot.list_objects(TOKEN, "deals"), ([], True))

    #: a company as a real portal returned it on 2026-09-28, with associations requested
    LIVE_COMPANY = {**LIVE_RECORD, "associations": {"contacts": {"results": [{"id": "<id>", "type": "<type>"}]}}}

    def test_the_verified_live_company_shape(self):
        pages = iter([{"results": [self.LIVE_COMPANY], "paging": {"next": {"after": "<cursor>", "link": "<link>"}}},
                      {"results": [self.LIVE_RECORD]}])  # no associations key when a record has none
        self.route = lambda *a: next(pages)
        rows, complete = hubspot.list_objects(TOKEN, "companies", ("name",), ("contacts",))
        self.assertEqual((rows, complete), ([self.LIVE_COMPANY, self.LIVE_RECORD], True))
        query = parse_qs(urlsplit(self.sent[0][1]).query)
        self.assertEqual((query["limit"], query["associations"]), ([str(hubspot.PAGE_LIMIT)], ["contacts"]))
        self.assertTrue(all(s[0] == "GET" for s in self.sent))

    def test_source_metadata_passes_the_returned_url_through_and_never_guesses(self):
        read_at = datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc)
        record = {**self.LIVE_RECORD, "id": "<id>", "updatedAt": "2020-01-01T00:00:00Z"}
        meta = hubspot.source_metadata(record, "companies", read_at)
        self.assertEqual(meta, {"source_system": "hubspot", "record_type": "companies", "record_id": "<id>",
                                "source_url": "https://app.hubspot.com/<path>",
                                "retrieved_at": "2026-09-28T07:00:00+00:00"})
        self.assertNotEqual(meta["retrieved_at"], record["updatedAt"])  # when we read it, not when it changed
        for missing in ({}, {"url": ""}, {"url": None}, {"url": 7}):
            without = {k: v for k, v in record.items() if k != "url"} | missing
            self.assertIsNone(hubspot.source_metadata(without, "companies", read_at)["source_url"], missing)

    def test_a_returned_url_reaches_the_radar_link_and_only_on_the_vendor_host(self):
        from revenue_agent import console  # the presentation contract the metadata feeds
        meta = hubspot.source_metadata(self.LIVE_RECORD, "companies", datetime.now(timezone.utc))
        self.assertEqual(console.source_action(meta), ("Open in HubSpot ↗", self.LIVE_RECORD["url"]))
        spoofed = hubspot.source_metadata({**self.LIVE_RECORD, "url": "https://app.hubspot.com.evil.example/x"},
                                          "companies", datetime.now(timezone.utc))
        self.assertEqual(console.source_action(spoofed), ("View recorded evidence", None))
        no_url = hubspot.source_metadata({"id": "<id>"}, "companies", datetime.now(timezone.utc))
        self.assertEqual(console.source_action(no_url), ("View recorded evidence", None))

    def test_unsupported_objects_are_refused(self):
        with self.assertRaises(ValueError):
            hubspot.list_objects(TOKEN, "invoices")

    def test_token_scopes(self):
        self.route = lambda m, u, b: {"scopes": ["crm.objects.companies.read"]}
        self.assertEqual(hubspot.get_token_scopes(TOKEN), ["crm.objects.companies.read"])
        method, url, _, body = self.sent[0]
        self.assertEqual((method, url, body), ("POST", f"{hubspot.BASE}/oauth/v2/private-apps/get/"
                                               "access-token-info", {"tokenKey": TOKEN}))
        self.route = lambda *a: {"hubId": 1}  # a response that does not say
        self.assertIsNone(hubspot.get_token_scopes(TOKEN))


# -- the doctor -------------------------------------------------------------------------------
class TestDoctor(_Mocked):
    def run_doctor(self, env):
        clean = {k: v for k, v in os.environ.items() if k not in VENDOR_ENV}
        out = io.StringIO()
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True), contextlib.redirect_stdout(out):
            code = doctor.main()
        return code, out.getvalue()

    def test_nothing_configured_is_a_normal_success(self):
        code, out = self.run_doctor({})
        self.assertEqual(code, 0)
        self.assertEqual(len(re.findall(r"Status:\s+NOT CONFIGURED\n", out)), 3)
        self.assertEqual(out.count("Application write methods exposed:  NO"), 3)
        self.urlopen.assert_not_called()

    def hubspot_route(self, scopes):
        def route(method, url, body):
            if url.endswith("access-token-info"):
                return {"scopes": scopes} if scopes is not None else {"hubId": 1}
            return {"results": [{"id": "1", "properties": {"name": "PII-SHOULD-NOT-PRINT"}}]}
        return route

    def test_hubspot_states(self):
        for scopes, status, code in (
            (["crm.objects.companies.read", "crm.objects.deals.read", "oauth"], "VERIFIED READ-ONLY", 0),
            (["crm.objects.companies.read", "crm.objects.companies.write"], "UNSAFE", 1),
            (None, "SCOPE UNKNOWN", 0),
            (["crm.objects.companies.read", "tickets"], "SCOPE UNKNOWN", 0),
            # versioned read scope seen live on 2026-09-28; its write twin is still caught
            (["crm.objects.companies.read", "crm.objects.companies.sensitive.read.v2"], "VERIFIED READ-ONLY", 0),
            (["crm.objects.companies.read", "crm.objects.companies.sensitive.write.v2"], "UNSAFE", 1),
            (["crm.objects.companies.read", "crm.objects.companies.read.beta"], "SCOPE UNKNOWN", 0),
        ):
            self.route = self.hubspot_route(scopes)
            got, out = self.run_doctor({"HUBSPOT_ACCESS_TOKEN": TOKEN})
            self.assertIn(f"CONFIGURED / {status}", out, scopes)
            self.assertEqual(got, code, scopes)
            self.assertNotIn(TOKEN, out)
            self.assertNotIn("PII-SHOULD-NOT-PRINT", out)
            self.assertIn("ok (1 record;", out)

    def test_authenticated_but_refused_company_read_is_not_a_connection_failure(self):
        # the live case on 2026-09-27: every scope a read, but no company scope
        scopes = ["crm.objects.deals.read", "crm.objects.owners.read", "oauth"]
        self.route = lambda m, u, b: {"scopes": scopes} if u.endswith("access-token-info") else http_error(403)
        code, out = self.run_doctor({"HUBSPOT_ACCESS_TOKEN": TOKEN})
        self.assertEqual(code, 1)  # the adapter cannot do its job: attention needed
        self.assertNotIn("CONNECTION FAILED", out)
        self.assertIn("CONFIGURED / VERIFIED READ-ONLY", out)
        self.assertRegex(out, r"Connection:\s+ok\n")
        self.assertIn("refused by the vendor (HTTP 403): the token has no crm.objects.companies.read scope", out)
        self.assertNotIn(TOKEN, out)
        self.route = lambda m, u, b: {"hubId": 1} if u.endswith("access-token-info") else http_error(403)
        code, out = self.run_doctor({"HUBSPOT_ACCESS_TOKEN": TOKEN})  # scopes not reported: no guess
        self.assertIn("CONFIGURED / SCOPE UNKNOWN", out)
        self.assertIn("refused by the vendor (HTTP 403)\n", out)

    def test_connection_failure(self):
        self.route = lambda *a: http_error(401)
        code, out = self.run_doctor({"HUBSPOT_ACCESS_TOKEN": TOKEN})
        self.assertEqual(code, 1)
        self.assertIn("CONFIGURED / CONNECTION FAILED", out)
        self.assertIn("failed: auth (HTTP 401)", out)

    def test_shopify_write_scope_is_unsafe_and_klaviyo_is_unknown(self):
        def route(method, url, body):
            if "klaviyo" in url:
                return {"data": [{"id": "acct"}]}
            if "AccessScopes" in body["query"]:
                return {"data": {"currentAppInstallation": {"accessScopes": [
                    {"handle": "read_orders"}, {"handle": "write_orders"}]}}}
            return {"data": {"orders": {"edges": [], "pageInfo": {"hasNextPage": False}}}}
        self.route = route
        code, out = self.run_doctor({"SHOPIFY_STORE_DOMAIN": "demo.myshopify.com",
                                     "SHOPIFY_ADMIN_TOKEN": TOKEN, "KLAVIYO_PRIVATE_API_KEY": TOKEN})
        self.assertEqual(code, 1)
        self.assertIn("CONFIGURED / UNSAFE", out)
        self.assertIn("WRITE SCOPES PRESENT: write_orders", out)
        self.assertIn("CONFIGURED / SCOPE UNKNOWN", out)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("demo.myshopify.com", out)


# -- mapping and parity -------------------------------------------------------------------------
def hubspot_shaped(portfolio):
    """What a HubSpot portal holding the synthetic CRM facts would return."""
    names = sorted({a.account_owner for a in portfolio.accounts} | {n.author for n in portfolio.crm_notes})
    owner_id = {name: str(500 + i) for i, name in enumerate(names)}
    company_id = {a.id: str(9000 + i) for i, a in enumerate(portfolio.accounts)}
    iso = lambda dt: dt.isoformat().replace("+00:00", "Z")  # noqa: E731
    companies = [{"id": company_id[a.id], "properties": {
        "name": a.brand_name, "createdate": iso(a.created_at), "hubspot_owner_id": owner_id[a.account_owner],
        "dtc_account_id": a.id, "dtc_vertical": a.vertical, "dtc_account_stage": a.lifecycle_stage}}
        for a in portfolio.accounts]
    activities = collections.defaultdict(list)
    for i, n in enumerate(portfolio.crm_notes):
        kind = {"note": "notes", "call": "calls"}[n.activity_type]
        body_field = "hs_note_body" if kind == "notes" else "hs_call_body"
        activities[kind].append({"id": str(70000 + i), "properties": {
            body_field: n.body, "hs_timestamp": iso(n.occurred_at), "hubspot_owner_id": owner_id[n.author]},
            "associations": {"companies": {"results": [{"id": company_id[n.account_id], "type": "x_to_company"}]}}})
    owners = [{"id": oid, "firstName": name} for name, oid in owner_id.items()]
    return {"companies": companies, "notes": activities["notes"], "calls": activities["calls"], "owners": owners}


class TestHubSpotParity(_Mocked):
    PAGE = 10  # smaller than the 30 companies, so paging is exercised

    def setUp(self):
        super().setUp()
        self.portfolio = build_portfolio()
        self.data = hubspot_shaped(self.portfolio)

    def route(self, method, url, body):
        parts = urlsplit(url)
        kind = "owners" if parts.path == "/crm/v3/owners" else parts.path.rsplit("/", 1)[-1]
        start = int(parse_qs(parts.query).get("after", ["0"])[0])
        rows = self.data[kind][start:start + self.PAGE]
        more = start + self.PAGE < len(self.data[kind])
        return {"results": rows, **({"paging": {"next": {"after": str(start + self.PAGE)}}} if more else {})}

    def rebuilt(self):
        owners_rows, done_o = hubspot.list_owners(TOKEN)
        companies, done_c = hubspot.list_objects(TOKEN, "companies", hubspot.COMPANY_PROPERTIES)
        owners = hubspot.owner_names(owners_rows)
        by_company = {c["id"]: c["properties"]["dtc_account_id"] for c in companies}
        activities = []
        for kind in ("notes", "calls"):
            rows, done = hubspot.list_objects(TOKEN, kind, hubspot.ACTIVITY_PROPERTIES[kind], ("companies",))
            self.assertTrue(done)
            activities += [(r["id"], hubspot.activity_fields(r, kind, by_company, owners)) for r in rows]
        self.assertTrue(done_o and done_c)
        return [hubspot.account_fields(c, owners) for c in companies], activities

    def test_a_every_mapped_field_matches_the_synthetic_original(self):
        accounts, activities = self.rebuilt()
        self.assertEqual(len(accounts), 30)
        for original, got in zip(self.portfolio.accounts, accounts):
            expected = {f: getattr(original, f) for f in got}
            self.assertEqual(got, expected, original.id)
        key = lambda d: (d["account_id"], d["occurred_at"], d["activity_type"])  # noqa: E731
        want = sorted(({"account_id": n.account_id, "activity_type": n.activity_type,
                        "occurred_at": n.occurred_at, "author": n.author, "body": n.body}
                       for n in self.portfolio.crm_notes), key=key)
        self.assertEqual(sorted((a for _, a in activities), key=key), want)

    def test_b_the_shortlist_is_unchanged_when_crm_facts_come_from_the_adapter(self):
        accounts, activities = self.rebuilt()
        commerce = {a.id: a.commerce_platform for a in self.portfolio.accounts}  # not CRM data
        via_adapter = dataclasses.replace(
            self.portfolio,
            accounts=[Account(**fields, commerce_platform=commerce[fields["id"]]) for fields in accounts],
            crm_notes=[CrmNote(id=f"hubspot_{rid}", **fields) for rid, fields in activities],
        )
        original = [e["account_id"] for e in signals.shortlist(InMemoryRepository(self.portfolio))]
        rebuilt = [e["account_id"] for e in signals.shortlist(InMemoryRepository(via_adapter))]
        self.assertEqual(len(original), 8)
        self.assertEqual(rebuilt, original)

    def test_an_activity_without_exactly_one_company_is_not_guessed(self):
        record = {"properties": {"hs_note_body": "x"}, "associations": {"companies": {"results": [
            {"id": "1"}, {"id": "2"}]}}}
        self.assertIsNone(hubspot.activity_fields(record, "notes", {"1": "a", "2": "b"}, {})["account_id"])


# -- isolation from the agent, evals and demo -----------------------------------------------------
class TestIsolation(unittest.TestCase):
    def test_nothing_outside_the_lab_imports_it(self):
        offenders = [str(p.relative_to(ROOT)) for base in ("src", "scripts", "evals")
                     for p in (ROOT / base).rglob("*.py")
                     if "integrations" not in p.parts and "integrations" in p.read_text(encoding="utf-8")
                     and re.search(r"^\s*(from|import)\s+\S*integrations", p.read_text(encoding="utf-8"), re.M)]
        self.assertEqual(offenders, [])

    def test_the_agent_the_unattended_job_and_the_console_load_without_it(self):
        code = ("import sys; import revenue_agent.unattended, revenue_agent.console, revenue_agent.daily; "
                "print(any(m.startswith('revenue_agent.integrations') for m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                             env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(out.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
