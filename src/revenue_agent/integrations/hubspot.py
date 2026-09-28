"""HubSpot CRM, read-only: companies, deals, notes, calls, tasks and owners.

Auth is a private-app ("legacy app") access token sent as a Bearer header.
Docs checked 2026-09-27 (see docs/REAL_INTEGRATIONS.md for URLs); the token's
own scopes are reported by POST /oauth/v2/private-apps/get/access-token-info.

Verified against a real portal on 2026-09-27/28 (structure only, see
docs/REAL_INTEGRATIONS.md): paging is paging.next.after (plus paging.next.link)
and the paging key is absent on the last page; each record carries id,
properties, createdAt, updatedAt, archived and url (an https://app.hubspot.com
link) -- companies, calls and tasks alike; companies need
crm.objects.companies.read (HTTP 403 without it). Associations come back as
associations.<type>.results[] of {id, type}, and the key is absent when a
record has none (observed company->contacts only; activity->company was not
observed in that portal).
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode

from ._http import IntegrationError, request

VENDOR = "hubspot"
BASE = "https://api.hubapi.com"
OBJECT_TYPES = ("companies", "deals", "notes", "calls", "tasks")
COMPANY_SCOPE = "crm.objects.companies.read"
PAGE_LIMIT = 100
MAX_PAGES = 5  # bounded: a partial read is reported, never silently treated as complete

#: company properties the mapping reads; the dtc_* ones are custom properties
#: a portal would need (a manual, UI-side step; not needed for the doctor)
COMPANY_PROPERTIES = ("name", "createdate", "hubspot_owner_id",
                      "dtc_account_id", "dtc_vertical", "dtc_account_stage")
ACTIVITY_PROPERTIES = {"notes": ("hs_note_body", "hs_timestamp", "hubspot_owner_id"),
                       "calls": ("hs_call_body", "hs_timestamp", "hubspot_owner_id")}


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _paged(token: str, path: str, params: dict, max_pages: int) -> tuple[list[dict], bool]:
    """(results, complete). complete is False when max_pages ran out first."""
    results, after = [], None
    for _ in range(max_pages):
        query = {**params, "limit": PAGE_LIMIT, **({"after": after} if after else {})}
        page = request(VENDOR, "GET", f"{BASE}{path}?{urlencode(query)}", _headers(token))
        rows = page.get("results")
        if not isinstance(rows, list):
            raise IntegrationError(VENDOR, "bad_payload", detail="no results list")
        results += rows
        after = (page.get("paging") or {}).get("next", {}).get("after")
        if not after:
            return results, True
    return results, False


def list_objects(token: str, object_type: str, properties=(), associations=(),
                 max_pages: int = MAX_PAGES) -> tuple[list[dict], bool]:
    if object_type not in OBJECT_TYPES:
        raise ValueError(f"not a supported read: {object_type}")
    params = {}
    if properties:
        params["properties"] = ",".join(properties)
    if associations:
        params["associations"] = ",".join(associations)
    return _paged(token, f"/crm/v3/objects/{object_type}", params, max_pages)


def list_owners(token: str, max_pages: int = MAX_PAGES) -> tuple[list[dict], bool]:
    return _paged(token, "/crm/v3/owners", {}, max_pages)


def get_token_scopes(token: str) -> list[str] | None:
    """The token's own scopes, or None if the response does not say."""
    payload = request(VENDOR, "POST", f"{BASE}/oauth/v2/private-apps/get/access-token-info",
                      {}, {"tokenKey": token})
    scopes = payload.get("scopes")
    return scopes if isinstance(scopes, list) and all(isinstance(s, str) for s in scopes) else None


def get_sample(token: str) -> int:
    """The smoke read: one page of one company. Returns how many came back."""
    return len(request(VENDOR, "GET", f"{BASE}/crm/v3/objects/companies?limit=1",
                       _headers(token)).get("results") or [])


# -- mapping back to the synthetic model -------------------------------------------------
def _when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def source_metadata(record: dict, object_type: str, retrieved_at: datetime) -> dict:
    """Where a record came from. source_url is only the url HubSpot returned, never built
    from ids; retrieved_at is when WE read it (HubSpot's updatedAt is when it last changed)."""
    url = record.get("url")
    return {"source_system": VENDOR, "record_type": object_type, "record_id": record.get("id"),
            "source_url": url if isinstance(url, str) and url else None,
            "retrieved_at": retrieved_at.isoformat()}


def owner_names(owners: list[dict]) -> dict[str, str]:
    return {str(o["id"]): o.get("firstName") for o in owners}


def account_fields(company: dict, owners: dict[str, str]) -> dict:
    """The CRM half of a synthetic Account. Commerce fields are not CRM data."""
    p = company.get("properties") or {}
    return {
        "id": p.get("dtc_account_id"),
        "brand_name": p.get("name"),
        "vertical": p.get("dtc_vertical"),
        "lifecycle_stage": p.get("dtc_account_stage"),
        "account_owner": owners.get(str(p.get("hubspot_owner_id"))),
        "created_at": _when(p["createdate"]) if p.get("createdate") else None,
    }


def activity_fields(record: dict, object_type: str, account_by_company: dict[str, str],
                    owners: dict[str, str]) -> dict:
    """A synthetic CrmNote from a HubSpot note or call. HubSpot's record id
    replaces the synthetic note id; the account comes from the association."""
    p = record.get("properties") or {}
    linked = ((record.get("associations") or {}).get("companies") or {}).get("results") or []
    return {
        "account_id": account_by_company.get(str(linked[0]["id"])) if len(linked) == 1 else None,
        "activity_type": {"notes": "note", "calls": "call"}[object_type],
        "occurred_at": _when(p["hs_timestamp"]) if p.get("hs_timestamp") else None,
        "author": owners.get(str(p.get("hubspot_owner_id"))),
        "body": p.get("hs_note_body" if object_type == "notes" else "hs_call_body"),
    }
