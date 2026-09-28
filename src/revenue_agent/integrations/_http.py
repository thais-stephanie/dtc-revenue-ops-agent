"""The one HTTP path every adapter uses. Read-only by construction.

* GET only. POST is refused unless the (host, path) is one of two fixed,
  read-only endpoints: Shopify's Admin GraphQL endpoint (queries only; any
  mutation or subscription is refused before sending) and HubSpot's
  private-app token-info endpoint (it reports a token's own scopes).
* HTTPS only, a timeout on every request, no retries: a failure is reported,
  never hidden. Retry-After is surfaced on 429 so the caller can see it.
* Errors never carry headers, tokens or response bodies, only the vendor, the
  kind and the status.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

TIMEOUT_S = 10

#: the only POST targets, both read-only
_POST_ALLOWED = (
    re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$"),
    re.compile(r"^/admin/api/\d{4}-\d{2}/graphql\.json$"),
), (
    re.compile(r"^api\.hubapi\.com$"),
    re.compile(r"^/oauth/v2/private-apps/get/access-token-info$"),
)
_WRITE_OPERATION = re.compile(r"\b(mutation|subscription)\b", re.IGNORECASE)


class IntegrationError(Exception):
    """kind: auth | forbidden | rate_limited | server | timeout | network |
    bad_payload | http | refused."""

    def __init__(self, vendor: str, kind: str, status: int | None = None,
                 retry_after: float | None = None, detail: str = "") -> None:
        self.vendor, self.kind, self.status, self.retry_after = vendor, kind, status, retry_after
        message = f"{vendor}: {kind}" + (f" (HTTP {status})" if status else "")
        if retry_after is not None:
            message += f", retry after {retry_after:g}s"
        super().__init__(message + (f": {detail}" if detail else ""))


def _kind(status: int) -> str:
    if status == 401:
        return "auth"
    if status == 403:
        return "forbidden"
    if status == 429:
        return "rate_limited"
    return "server" if status >= 500 else "http"


def _retry_after(headers) -> float | None:
    value = headers.get("Retry-After") if headers else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None  # an HTTP-date; not worth parsing for a read-only lab


def _check_target(vendor: str, method: str, url: str, body: dict | None) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise IntegrationError(vendor, "refused", detail="https only")
    if method == "GET":
        return
    allowed = method == "POST" and any(
        host.match(parts.hostname or "") and path.match(parts.path) for host, path in _POST_ALLOWED
    )
    if not allowed:
        raise IntegrationError(vendor, "refused", detail=f"{method} is not a read operation")
    document = (body or {}).get("query", "")
    if _WRITE_OPERATION.search(document):
        raise IntegrationError(vendor, "refused", detail="GraphQL mutations and subscriptions are not sent")


def request(vendor: str, method: str, url: str, headers: dict, body: dict | None = None) -> dict:
    """One request; returns parsed JSON or raises IntegrationError."""
    _check_target(vendor, method, url, body)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Accept": "application/json", **({"Content-Type": "application/json"} if data else {}),
                 **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        retry_after = _retry_after(exc.headers)
        exc.close()
        raise IntegrationError(vendor, _kind(status), status, retry_after) from None
    except TimeoutError:
        raise IntegrationError(vendor, "timeout", detail=f"no answer in {TIMEOUT_S}s") from None
    except urllib.error.URLError as exc:
        kind = "timeout" if isinstance(exc.reason, TimeoutError) else "network"
        raise IntegrationError(vendor, kind, detail=type(exc.reason).__name__) from None
    try:
        payload = json.loads(raw)
    except ValueError:
        raise IntegrationError(vendor, "bad_payload", detail="response is not JSON") from None
    if not isinstance(payload, dict):
        raise IntegrationError(vendor, "bad_payload", detail="response is not a JSON object")
    return payload
