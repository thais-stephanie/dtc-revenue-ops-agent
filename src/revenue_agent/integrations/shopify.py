"""Shopify Admin GraphQL, read-only, through fixed query documents only.

Callers name a query; they never pass GraphQL text. The documents below are
the whole surface, and the transport refuses any mutation or subscription
anyway. No customer PII is requested.

Docs checked 2026-09-27 (URLs in docs/REAL_INTEGRATIONS.md): endpoint
https://{shop}.myshopify.com/admin/api/{version}/graphql.json with the
X-Shopify-Access-Token header; latest stable version 2026-07;
currentAppInstallation.accessScopes lists the token's granted scopes;
abandonedCheckouts needs read_orders. Order field names are VERIFY BEFORE BUILDING.
"""
from __future__ import annotations

import re

from ._http import IntegrationError, request

VENDOR = "shopify"
API_VERSION = "2026-07"
PAGE_SIZE = 50
MAX_PAGES = 5
_DOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")

_MONEY = "totalPriceSet { shopMoney { amount currencyCode } }"
QUERIES = {
    "access_scopes": "query AccessScopes { currentAppInstallation { accessScopes { handle } } }",
    "orders": (
        "query Orders($first: Int!, $after: String) { orders(first: $first, after: $after, "
        "sortKey: CREATED_AT, reverse: true) { edges { node { id createdAt displayFinancialStatus "
        f"{_MONEY} }} }} pageInfo {{ hasNextPage endCursor }} }} }}"
    ),
    "abandoned_checkouts": (
        "query AbandonedCheckouts($first: Int!, $after: String) { abandonedCheckouts(first: $first, "
        f"after: $after) {{ edges {{ node {{ id createdAt completedAt {_MONEY} }} }} "
        "pageInfo { hasNextPage endCursor } } }"
    ),
}


def _query(domain: str, token: str, name: str, variables: dict | None = None) -> dict:
    """Private: only the fixed documents above can reach the transport."""
    if not _DOMAIN.match(domain or ""):
        raise IntegrationError(VENDOR, "refused", detail="SHOPIFY_STORE_DOMAIN must be <store>.myshopify.com")
    payload = request(
        VENDOR, "POST", f"https://{domain}/admin/api/{API_VERSION}/graphql.json",
        {"X-Shopify-Access-Token": token}, {"query": QUERIES[name], "variables": variables or {}},
    )
    if payload.get("errors"):
        codes = {(e.get("extensions") or {}).get("code") for e in payload["errors"] if isinstance(e, dict)}
        kind = "rate_limited" if "THROTTLED" in codes else "forbidden" if "ACCESS_DENIED" in codes else "http"
        raise IntegrationError(VENDOR, kind, detail="GraphQL errors: " + ", ".join(sorted(map(str, codes))))
    if not isinstance(payload.get("data"), dict):
        raise IntegrationError(VENDOR, "bad_payload", detail="no data object")
    return payload["data"]


def _paged(domain: str, token: str, name: str, field: str, max_pages: int) -> tuple[list[dict], bool]:
    nodes, after = [], None
    for _ in range(max_pages):
        conn = _query(domain, token, name, {"first": PAGE_SIZE, "after": after}).get(field)
        try:
            nodes += [edge["node"] for edge in conn["edges"]]
            page = conn["pageInfo"]
        except (TypeError, KeyError):
            raise IntegrationError(VENDOR, "bad_payload", detail=f"unexpected {field} shape") from None
        if not page.get("hasNextPage"):
            return nodes, True
        after = page.get("endCursor")
    return nodes, False


def get_access_scopes(domain: str, token: str) -> list[str]:
    installation = _query(domain, token, "access_scopes").get("currentAppInstallation") or {}
    return [s["handle"] for s in installation.get("accessScopes") or []]


def list_orders(domain: str, token: str, max_pages: int = MAX_PAGES) -> tuple[list[dict], bool]:
    return _paged(domain, token, "orders", "orders", max_pages)


def list_abandoned_checkouts(domain: str, token: str, max_pages: int = MAX_PAGES) -> tuple[list[dict], bool]:
    return _paged(domain, token, "abandoned_checkouts", "abandonedCheckouts", max_pages)
