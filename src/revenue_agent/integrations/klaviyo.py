"""Klaviyo, read-only: accounts, profiles, segments, campaigns, flows, metrics.

Docs checked 2026-09-27 (URLs in docs/REAL_INTEGRATIONS.md): header
"Authorization: Klaviyo-API-Key <key>", a required "revision" header (current
2026-07-15), a Read-Only private key option, cursor paging via links.next, and
GET /api/campaigns requires a channel filter. There is no documented API that
reports a private key's scopes, so the doctor cannot verify them.
"""
from __future__ import annotations

from ._http import IntegrationError, request

VENDOR = "klaviyo"
BASE = "https://a.klaviyo.com"
REVISION = "2026-07-15"
MAX_PAGES = 5
RESOURCES = {
    "profiles": "/api/profiles",
    "segments": "/api/segments",
    "campaigns": "/api/campaigns?filter=equals(messages.channel,'email')",
    "flows": "/api/flows",
    "metrics": "/api/metrics",
}


def _get(key: str, url: str) -> dict:
    return request(VENDOR, "GET", url, {"Authorization": f"Klaviyo-API-Key {key}", "revision": REVISION})


def get_sample(key: str) -> int:
    """The smoke read: the account the key belongs to. Returns how many came back."""
    return len(_get(key, f"{BASE}/api/accounts").get("data") or [])


def list_resource(key: str, resource: str, max_pages: int = MAX_PAGES) -> tuple[list[dict], bool]:
    if resource not in RESOURCES:
        raise ValueError(f"not a supported read: {resource}")
    items, url = [], f"{BASE}{RESOURCES[resource]}"
    for _ in range(max_pages):
        page = _get(key, url)
        data = page.get("data")
        if not isinstance(data, list):
            raise IntegrationError(VENDOR, "bad_payload", detail="no data list")
        items += data
        url = (page.get("links") or {}).get("next")
        if not url:
            return items, True
        if not url.startswith(f"{BASE}/api/"):  # never follow a cursor off Klaviyo's API
            raise IntegrationError(VENDOR, "bad_payload", detail="next link leaves the API host")
    return items, False
