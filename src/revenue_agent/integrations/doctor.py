"""Integration doctor: is each vendor configured, reachable, and read-only?

    python -m revenue_agent.integrations.doctor

Per vendor, one of:
  NOT CONFIGURED              no credential: normal and safe, not an error
  CONFIGURED / VERIFIED READ-ONLY   the vendor reported the token's scopes; all are reads
  CONFIGURED / UNSAFE         the vendor reported a write scope: recreate the token read-only
  CONFIGURED / SCOPE UNKNOWN  no documented way to prove the scopes (or they were unrecognised)
  CONFIGURED / CONNECTION FAILED

"Application write methods exposed" is about THIS code (the adapter modules
have no mutating functions); "credential write scopes" is about the TOKEN.
They are different guarantees and are reported separately. A sample read the
vendor refuses (HTTP 403) after the token authenticated is reported as such,
with the missing scope when the vendor listed the token's scopes; it is not a
connection failure. Secret values are never printed, and no record content or
account identifier is printed either: only counts. Exit code 1 for UNSAFE,
CONNECTION FAILED or a refused sample read.
"""
from __future__ import annotations

import inspect
import os
import re
import sys

from . import hubspot, klaviyo, shopify
from ._http import IntegrationError

_WRITE_VERB = re.compile(r"^(create|update|delete|remove|put|patch|post|write|send|set|upsert|"
                         r"merge|archive|apply|execute|mutate)")


def write_methods(module) -> list[str]:
    """Public functions of an adapter module whose name is a write verb."""
    return [name for name, fn in inspect.getmembers(module, inspect.isfunction)
            if fn.__module__ == module.__name__ and not name.startswith("_") and _WRITE_VERB.match(name)]


def scope_status(scopes: list[str] | None, is_write, is_read) -> tuple[str, str]:
    if scopes is None:
        return "SCOPE UNKNOWN", "not verifiable"
    writes = [s for s in scopes if is_write(s)]
    if writes:
        return "UNSAFE", f"WRITE SCOPES PRESENT: {', '.join(sorted(writes))}"
    unknown = [s for s in scopes if not is_read(s)]
    if unknown:
        return "SCOPE UNKNOWN", f"unrecognised: {', '.join(sorted(unknown))}"
    return "VERIFIED READ-ONLY", f"read only: {', '.join(sorted(scopes))}"


def _check(name, module, env_names, connect) -> dict:
    report = {"vendor": name, "write_methods": write_methods(module)}
    missing = [n for n in env_names if not os.getenv(n)]
    if missing:
        return {**report, "status": "NOT CONFIGURED", "configured": f"missing ({', '.join(missing)})",
                "connection": "not attempted", "scopes": "NOT CONFIGURED", "sample": "skipped"}
    try:
        status, scopes, sample = connect(*(os.getenv(n) for n in env_names))
    except IntegrationError as exc:
        return {**report, "status": "CONNECTION FAILED", "configured": "configured",
                "connection": f"failed: {exc.kind}" + (f" (HTTP {exc.status})" if exc.status else ""),
                "scopes": "not checked", "sample": "skipped"}
    return {**report, "status": status, "configured": "configured", "connection": "ok",
            "scopes": scopes, "sample": sample}


SAMPLE_REFUSED = "refused by the vendor"


def _records(n: int) -> str:
    return f"ok ({n} record{'s' if n != 1 else ''}; an empty test account returns 0)"


def _hubspot(token):
    granted = hubspot.get_token_scopes(token)
    # a read scope may carry a version suffix (crm.objects.companies.sensitive.read.v2, seen live 2026-09-28)
    status, scopes = scope_status(granted, lambda s: "write" in s,
                                  lambda s: bool(re.search(r"\.read(\.v\d+)?$", s)) or s == "oauth")
    try:
        sample = _records(hubspot.get_sample(token))
    except IntegrationError as exc:
        if exc.kind != "forbidden":
            raise
        # authenticated (the scopes came back), but not allowed to read companies: say which, don't call it a failure
        missing = granted is not None and hubspot.COMPANY_SCOPE not in granted
        sample = (f"{SAMPLE_REFUSED} (HTTP 403)"
                  + (f": the token has no {hubspot.COMPANY_SCOPE} scope" if missing else ""))
    return status, scopes, sample


def _shopify(domain, token):
    status, scopes = scope_status(shopify.get_access_scopes(domain, token),
                                  lambda s: s.startswith("write_"), lambda s: s.startswith("read_"))
    return status, scopes, _records(len(shopify.list_orders(domain, token, max_pages=1)[0]))


def _klaviyo(key):
    # Klaviyo documents no API for a key's scopes
    return ("SCOPE UNKNOWN", "not verifiable (no documented API for a key's scopes; check the key in the UI)",
            _records(klaviyo.get_sample(key)))


CHECKS = (
    ("HubSpot", hubspot, ("HUBSPOT_ACCESS_TOKEN",), _hubspot),
    ("Shopify", shopify, ("SHOPIFY_STORE_DOMAIN", "SHOPIFY_ADMIN_TOKEN"), _shopify),
    ("Klaviyo", klaviyo, ("KLAVIYO_PRIVATE_API_KEY",), _klaviyo),
)


def render(report: dict) -> str:
    status = report["status"] if report["status"] == "NOT CONFIGURED" else f"CONFIGURED / {report['status']}"
    methods = "NO" if not report["write_methods"] else f"YES: {', '.join(report['write_methods'])}"
    rows = [("Status", status), ("Credentials", report["configured"]),
            ("Connection", report["connection"]), ("Credential write scopes", report["scopes"]),
            ("Sample read", report["sample"]), ("Application write methods exposed", methods)]
    return "\n".join([report["vendor"]] + [f"  {k + ':':35} {v}" for k, v in rows])


def main() -> int:
    reports = [_check(*check) for check in CHECKS]
    print("Integration doctor (read-only lab; not used by the agent, evals or demo)\n")
    print("\n\n".join(render(r) for r in reports))
    bad = [r for r in reports if r["status"] in ("UNSAFE", "CONNECTION FAILED") or r["write_methods"]
           or r["sample"].startswith(SAMPLE_REFUSED)]
    print("\nResult:", "ATTENTION NEEDED" if bad else "OK (NOT CONFIGURED is a normal, safe state)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
