"""Account Radar: the read-only morning brief, drawn to the approved V4 design.

    python -m revenue_agent.console              # http://127.0.0.1:8765
    python -m revenue_agent.console --ledger runs/unattended.sqlite3 --port 8765

The markup, inline styles, tokens and interactions reproduce the approved
"Account Radar v4" prototype. The data does not come from that prototype: every
value is read from the unattended ledger, the verified receipt each row points
to, the synthetic dataset and the fixed presentation-only ownership table.

    /                               Today (the morning brief)       ?as=sofia|jordan|alex
    /portfolio                      My portfolio
    /team                           Team overview
    /run/<id>/account/<account>     Account detail
    /control, /run/<id>/control     Control room
    /sources                        Data sources
    /history                        History
    /reviews                        Human review prototype (linked from the control room's technical details)

A viewer, nothing more. It verifies each receipt's SHA-256 against the ledger,
renders only what was recorded, and recomputes no gate decision, rule or metric.
The small inline script only reveals, filters and sorts what is already on the
page; "Mark reviewed" is kept in the browser tab's sessionStorage and never
leaves it. Nothing here runs, retries, approves or writes anything, and nothing
calls a model or a vendor. The server binds to 127.0.0.1 and answers GET only.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import config, dataset, unattended
from .forensics import redact
from .review import NOT_APPLIED
from .tools import TOOL_DEFINITIONS

HOST = "127.0.0.1"  # never another interface: receipts hold account evidence
PORT = 8765
REGISTERED_TOOLS = {t["name"] for t in TOOL_DEFINITIONS}
FAILED_BEFORE_RESULT = "scan metrics unavailable; the run failed before a result was produced"

#: the credentials each read-only adapter would use (names only; the console
#: never imports the adapters or calls a vendor: see docs/REAL_INTEGRATIONS.md)
VENDORS = (
    ("HubSpot", ("HUBSPOT_ACCESS_TOKEN",), "companies, deals, notes, calls and owners"),
    ("Shopify", ("SHOPIFY_STORE_DOMAIN", "SHOPIFY_ADMIN_TOKEN"), "orders and abandoned checkouts"),
    ("Klaviyo", ("KLAVIYO_PRIVATE_API_KEY",), "profiles, segments, campaigns, flows and metrics"),
)

NAMES = {**dataset.SEEDED, **dataset.BACKGROUND}
#: URL-friendly first names, so internal owner ids never reach a page
VIEWERS = {name.split()[0].lower(): owner for owner, name in dataset.ACCOUNT_MANAGERS.items()}

# -- V4 design tokens (bg, chip, ink) ------------------------------------------------------------
T = {
    "blue": ("#e6f1fb", "#cbe2f6", "#1f6fb8"), "coral": ("#fdece6", "#fbd6c9", "#c2462b"),
    "yellow": ("#fff4dc", "#fbe2a6", "#946300"), "green": ("#e6f6ee", "#c6ecd8", "#2f7a55"),
    "lav": ("#eef0fb", "#d9ddf6", "#3c46a0"), "cyan": ("#e5f5f9", "#c3e8f1", "#1f6f86"),
    "gray": ("#f3f4f9", "#e3e5ef", "#4d5372"),
}
AM_TINT = {"am_sofia": "blue", "am_jordan": "lav", "am_alex": "green"}
SRC_TINT = {"Campaign data": "lav", "CRM activity": "yellow", "Commerce data": "cyan", "Billing": "coral"}
STATUS = {  # card status -> (label, ink, pill background)
    "ready": ("Ready for review", "#2f7a55", T["green"][0]),
    "info": ("Needs more information", "#946300", T["yellow"][0]),
    "held": ("Held back", "#c2462b", T["coral"][0]),
    "none": ("No action needed", "#4d5372", T["gray"][0]),
}
RUN = {  # ledger status -> (label, short, tint, icon)
    "ok": ("Completed normally", "OK", "green", "✓"),
    "partial": ("Needs review", "Needs review", "yellow", "!"),
    "failed": ("Stopped safely", "Stopped", "coral", "■"),
}


def e(value) -> str:
    return html.escape("-" if value is None or value == "" else str(value))


def name(account_id) -> str:
    return NAMES.get(account_id) or str(account_id or "Unknown account").replace("_", " ").title()


def owner_of(account_id) -> str | None:
    return next((o for o, accounts in dataset.PORTFOLIOS.items() if account_id in accounts), None)


def ini(text: str) -> str:
    """V4's initials: drop '&' and '.', first letters of the first two words."""
    words = text.replace("&", "").replace(".", "").split()
    return "".join(w[0] for w in words[:2]).upper()


# -- plain English: rewording recorded codes and facts, never deciding anything ---------------
NEXT_STEP = {
    "EXPAND_CART_RECOVERY": "Propose a cart-recovery campaign",
    "LAUNCH_REACTIVATION": "Propose a win-back campaign",
    "PREPARE_ACCOUNT_REVIEW": "Prepare for an account review",
    "INVESTIGATE_ATTRIBUTION": "Investigate the revenue attribution",
    "REFRESH_DATA": "Get the commerce data refreshed",
    "COORDINATE_BILLING": "Coordinate with billing before expanding",
    "REFRESH_CREATIVE": "Plan a creative refresh",
    "INVESTIGATE_BEFORE_EXPANSION": "Investigate before expanding",
    "NO_ACTION": "No action for now",
}
#: what each recorded action means for the account manager (V4's status sentence)
STATUS_TEXT = {
    "INVESTIGATE_ATTRIBUTION": "Radar won’t suggest growth here until someone explains the gap. Checking the "
                               "attribution comes first.",
    "INVESTIGATE_BEFORE_EXPANSION": "Two real signals point in opposite directions. Find out why before "
                                    "suggesting more spend.",
    "REFRESH_DATA": "Radar won’t suggest anything that depends on this data until it’s fresh again.",
}
READY_TEXT = "It passed every safety check, and everything it relies on traces back to data Radar read."
GATE_TEXT = "The next step is a review, not a change. Nothing is updated and no one is contacted."
MORE_INFO = {"INVESTIGATE_ATTRIBUTION", "INVESTIGATE_BEFORE_EXPANSION", "REFRESH_DATA"}
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
GROWTH = {"cart_recovery_gap", "reactivation_gap"}
HEADLINE = {
    "performance_drop": "Campaign performance dropped sharply",
    "attribution_exception": "Revenue numbers don’t agree",
    "stale_data": "Commerce data is out of date",
    "billing_hold": "An invoice is overdue",
    "cart_recovery_gap": "Shoppers are leaving carts behind",
    "reactivation_gap": "Past customers have stopped buying",
    "creative_fatigue": "The campaign creative is wearing out",
    "engagement_gap": "No recent account-manager contact",
}
WHY_IT_MATTERS = {
    "performance_drop": "Results fell well below this account’s recent level.",
    "attribution_exception": "The gap is too large to trust the campaign result as-is.",
    "stale_data": "Radar won’t recommend new spending on data this old.",
    "billing_hold": "Growing the account while an invoice is unpaid needs billing on board first.",
    "cart_recovery_gap": "No cart-recovery campaign is running to bring these shoppers back.",
    "reactivation_gap": "No win-back campaign is running for them.",
    "creative_fatigue": "Response has fallen each time the same creative ran.",
    "engagement_gap": "Nobody on the team has logged contact with this customer in a while.",
}
TINT = {"performance_drop": "coral", "engagement_gap": "coral", "attribution_exception": "lav",
        "stale_data": "cyan", "billing_hold": "coral", "cart_recovery_gap": "green",
        "reactivation_gap": "green", "creative_fatigue": "yellow"}
CHIP = {"performance_drop": "Campaign drop", "attribution_exception": "Revenue mismatch",
        "cart_recovery_gap": "Cart recovery", "engagement_gap": "No recent contact",
        "reactivation_gap": "Win-back", "stale_data": "Out-of-date data", "billing_hold": "Overdue invoice",
        "creative_fatigue": "Tired creative"}
ISSUE = {"performance_drop": "Campaign performance", "attribution_exception": "Revenue mismatch",
         "stale_data": "Out-of-date data", "billing_hold": "Overdue invoice", "cart_recovery_gap": "Cart recovery",
         "reactivation_gap": "Win-back", "creative_fatigue": "Tired creative", "engagement_gap": "No recent contact"}
SIGNAL_SOURCE = {"performance_drop": "Campaign data", "attribution_exception": "Campaign data",
                 "stale_data": "Commerce data", "billing_hold": "Billing", "cart_recovery_gap": "Commerce data",
                 "reactivation_gap": "Commerce data", "creative_fatigue": "Campaign data",
                 "engagement_gap": "CRM activity"}
HELD_NEXT = {
    "expansion_blocked_by_stale_data": "Held back until the commerce data refreshes",
    "expansion_blocked_by_reconciliation": "Held back until the revenue numbers reconcile",
    "expansion_without_activity_read": "Held back until recent CRM activity has been checked",
    "unknown_source_reference": "Held back until it cites only data Radar read",
    "cross_account_source_reference": "Held back until it cites only this account’s data",
    "schema_validation_failed": "Held back until the AI answers in the required format",
}


def pct(x) -> str:
    return f"{x:.0%}"


def signal_types(entry: dict) -> list[str]:
    return [s["type"] for s in entry.get("signals", [])]


def headline(entry: dict) -> str:
    types = signal_types(entry)
    growth = GROWTH & set(types)
    if entry.get("competing_signals"):
        return "Growth opportunity, but campaign efficiency is falling"
    if growth and "stale_data" in types:
        return "Growth opportunity, but the commerce data is out of date"
    if growth and "billing_hold" in types:
        return "Growth opportunity, but an invoice is overdue"
    if growth and "attribution_exception" in types:
        return "Growth opportunity, but revenue numbers don’t agree"
    if growth == GROWTH:
        return "Cart-recovery and win-back opportunities"
    return HEADLINE.get(types[0], "Worth a look") if types else "Worth a look"


def why_it_matters(entry: dict) -> str:
    if entry.get("competing_signals"):
        return "Radar sees potential, but the signals disagree."
    types = signal_types(entry)
    return WHY_IT_MATTERS.get(types[0], "") if types else ""


def tint(entry: dict) -> str:
    if entry.get("competing_signals"):
        return "yellow"
    types = signal_types(entry)
    return TINT.get(types[0], "gray") if types else "gray"


def facts(entry: dict) -> list[tuple[str, str, str]]:
    """(label, value, detail) for each recorded signal, formatted, strongest first."""
    out = []
    for s in entry.get("signals", []):
        f, kind = s.get("facts") or {}, s.get("type")
        if kind == "performance_drop":
            out.append(("Campaign ROAS", f"↓ {pct(f['relative_drop'])}",
                        f"{f['roas_7d']} this week · {f['roas_baseline_28d']} before"))
        elif kind == "cart_recovery_gap":
            growth = f.get("abandoned_cart_growth")
            out.append(("Abandoned carts, 30 days", f"{f['abandoned_carts_30d']:,}",
                        f"↑ {pct(growth)} vs the 30 days before" if growth and growth > 0 else "in the last 30 days"))
        elif kind == "reactivation_gap":
            out.append(("Lapsed customers", f"{f['lapsed_90d_customers']:,}", "no purchase in 90+ days"))
        elif kind == "attribution_exception":
            out.append(("Campaign-reported revenue", f"${f['reported']:,.0f}", "last 30 days"))
            out.append(("Commerce revenue", f"${f['commerce']:,.0f}", f"{pct(f['relative_delta'])} gap"))
        elif kind == "stale_data":
            out.append(("Commerce data age", f"{f['data_age_hours']:.0f} hours",
                        f"the limit is {config.STALE_AFTER_HOURS:.0f} hours"))
        elif kind == "billing_hold":
            out.append(("Overdue invoice", f"${f['amount_overdue']:,.0f}", f"{f['days_overdue']} days overdue"))
        elif kind == "creative_fatigue":
            rates = f.get("response_rates") or []
            if rates:
                out.append(("Creative response rate", f"{rates[0]:.1%} → {rates[-1]:.1%}",
                            f"across {f.get('campaigns', len(rates))} campaigns"))
        elif kind == "engagement_gap":
            out.append(("Last account-manager contact", f"{f['days_since_last_touch']} days ago",
                        "nothing logged since"))
    return out


def signal_sentence(signal: dict, competing: bool = False) -> str:
    f, kind = signal.get("facts") or {}, signal.get("type")
    if kind == "cart_recovery_gap":
        growth = f.get("abandoned_cart_growth")
        if competing and growth and growth > 0:
            return f"Abandoned carts are up {pct(growth)}, which points to room to grow."
        trend = f", {pct(growth)} more than the 30 days before" if growth and growth > 0 else ""
        return (f"{f['abandoned_carts_30d']:,} shoppers left carts behind in the last 30 days{trend}, "
                "and no cart-recovery campaign is running.")
    if kind == "reactivation_gap":
        return f"{f['lapsed_90d_customers']:,} customers haven’t bought in over 90 days, and no win-back campaign is running."
    if kind == "performance_drop":
        if competing:
            return f"At the same time, campaign return on ad spend fell {pct(f['relative_drop'])}."
        return (f"Campaign return on ad spend fell {pct(f['relative_drop'])}: {f['roas_7d']} this week "
                f"against {f['roas_baseline_28d']} before.")
    if kind == "attribution_exception":
        return (f"Campaign reporting shows ${f['reported']:,.0f} for the last 30 days. "
                f"Commerce shows ${f['commerce']:,.0f}.")
    if kind == "stale_data":
        return f"The commerce data is {f['data_age_hours']:.0f} hours old."
    if kind == "billing_hold":
        return f"An invoice for ${f['amount_overdue']:,.0f} is {f['days_overdue']} days overdue."
    if kind == "creative_fatigue":
        return f"The same creative ran in {f.get('campaigns', 'several')} campaigns and its response fell each time."
    if kind == "engagement_gap":
        return f"No account-manager contact has been logged for {f['days_since_last_touch']} days."
    return "Radar’s scan found a signal on this account."


def why_held(reason: str, account_facts: dict) -> str:
    """Why a safety check held a recommendation back, in plain words."""
    if reason == "expansion_blocked_by_stale_data":
        hours = (account_facts.get("data_freshness") or {}).get("commerce_hours")
        age = f"Commerce data is {hours:.0f} hours old" if hours is not None else "The commerce data is too old"
        return f"{age}. Expansion requires data no older than {config.STALE_AFTER_HOURS:.0f} hours."
    return {
        "expansion_blocked_by_reconciliation": "Campaign-reported and commerce revenue don’t reconcile, so "
            "Radar won’t recommend spending more until they do.",
        "expansion_without_activity_read": "The AI suggested expanding without first reading this customer’s "
            "recent CRM activity. Expansion requires that check.",
        "unknown_source_reference": "The AI pointed to a source Radar never gave it, so the suggestion "
            "can’t be traced back to real data.",
        "cross_account_source_reference": "The AI used evidence from a different account.",
        "unknown_proposed_task": "The AI attached a follow-up task Radar can’t match to this account.",
        "cross_account_proposed_task": "The AI attached a follow-up task drafted for a different account.",
        "account_not_shortlisted": "The AI raised an account that wasn’t on today’s list.",
        "recommendation_limit_exceeded": f"The AI raised more than {config.MAX_RECOMMENDATIONS} accounts in "
            "one check.",
        "schema_validation_failed": "The AI’s answer wasn’t in the required format, so none of it was used.",
    }.get(reason, "A safety check held this back.")


def held_clause(reason: str, account_facts: dict) -> str:
    """The same reason as a clause, for History's sentences."""
    if reason == "expansion_blocked_by_stale_data":
        hours = (account_facts.get("data_freshness") or {}).get("commerce_hours")
        if hours is not None:
            return (f"its commerce data is {hours:.0f} hours old, and growth suggestions need data under "
                    f"{config.STALE_AFTER_HOURS:.0f} hours")
    text = why_held(reason, account_facts).rstrip(".")
    return text[0].lower() + text[1:]


def capability(tool: str) -> str:
    """update_crm_record -> 'update a CRM record'."""
    words = [w.upper() if w in ("crm", "api", "id") else w for w in tool.split("_")]
    return f"{words[0]} a {' '.join(words[1:])}" if len(words) > 1 else words[0]


def problem_sentence(problem: str, retries: int) -> str | None:
    """One recorded problem string, reworded. None when another view already covers it."""
    if problem.startswith("model_error"):
        what = "timed out" if "Timeout" in problem else "didn’t respond"
        if retries:
            return f"The AI provider {what}. Radar tried {retries + 1} times, then stopped safely. Nothing was sent to anyone."
        return f"The AI provider {what}, and Radar stopped safely. Nothing was sent to anyone."
    if problem.startswith("operational_error"):
        return "Something outside the AI failed, so Radar stopped safely. Nothing was sent to anyone."
    if problem.startswith("forbidden_tool_attempt"):
        tool = problem.split(":", 1)[1].split("(")[0].strip()
        return f"The AI asked for something it isn’t allowed to do ({capability(tool)}). Nothing ran."
    if problem.startswith("refused_tool_calls"):
        n = problem.split(":")[1].strip()
        return f"{n} of the AI’s data requests were refused: over its budget or about an account not on today’s list."
    if problem == "loop:unattended_cost_budget":
        return "Radar stopped before the next AI call because it would have gone over the cost budget."
    if problem == "loop:refusal_streak_limit":
        return "Radar stopped after the AI’s data requests were refused several times in a row."
    if problem == "loop:no_brief_submitted":
        return "The AI never sent back an answer."
    if problem.startswith("schema:"):
        return why_held("schema_validation_failed", {})
    if problem.startswith("invariant:"):
        return "A final safety check failed, so nothing from this check became actionable."
    if problem.startswith("silent_success_streak"):
        return "Several checks in a row found nothing. Worth checking that the data feeds are healthy."
    return None  # held-back items and failed-request counts are explained elsewhere


def monitoring_sentence(monitoring: dict) -> str:
    hc = {"ok": "The uptime monitor heard from this check.",
          "not_configured": "No uptime monitor is set up on this machine."}
    slack = {"ok": "A Slack alert was sent.", "not_needed": "No Slack alert was needed.",
             "not_configured": "Slack isn’t set up here, so the alert was printed locally."}
    out = []
    value = monitoring.get("healthchecks_end", monitoring.get("healthchecks_start"))
    if value:
        out.append(hc.get(value, "The uptime monitor couldn’t be reached; the check itself wasn’t affected."))
    if "slack" in monitoring:
        out.append(slack.get(monitoring["slack"], "Slack couldn’t be reached, so the alert was printed locally."))
    return " ".join(out) or "No monitoring outcome was recorded."


# -- reading ------------------------------------------------------------------------------------
def load_receipt(row: dict, ledger: Path) -> tuple[dict | None, str]:
    """(receipt, state). state: verified | mismatch | missing | none | outside."""
    recorded = row.get("forensic_report_path")
    if not recorded:
        return None, "none"
    base = ledger.resolve().parent.parent  # receipts are written to runs/ beside the ledger
    path = (base / Path(*PureWindowsPath(recorded).parts)).resolve()
    if not path.is_relative_to(base):
        return None, "outside"  # a ledger row must not point the viewer elsewhere
    if not path.exists():
        return None, "missing"
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != row.get("forensic_report_sha256"):
        return None, "mismatch"
    return json.loads(data), "verified"


def find_run(ledger: Path, run_id: str | None) -> dict | None:
    if run_id is None:
        rows = unattended.recent(ledger, limit=1)
        return rows[0] if rows else None
    rows = unattended._sql(ledger, "SELECT * FROM runs WHERE run_id = ?", (run_id,))
    return rows[0] if rows else None


def context_by_account(receipt: dict) -> dict[str, dict]:
    out = {}
    for t in receipt.get("tool_log", []):
        if t["tool"] == "get_account_context" and t.get("status") == "ok" and t.get("result"):
            out.setdefault(t["account_id"], t["result"])
    return out


def latest_note(receipt: dict, account_id: str) -> dict | None:
    for t in receipt.get("tool_log", []):
        if (t["tool"] == "get_recent_account_activity" and t.get("account_id") == account_id
                and t.get("status") == "ok" and (t.get("result") or {}).get("activity")):
            return t["result"]["activity"][0]
    return None


def brief(receipt: dict) -> list[dict]:
    """One card per account the AI raised, with the recorded safety decision. Order:
    recorded priority, then held back first, then the AI's own order. No re-ranking."""
    recs = [r for r in (receipt.get("raw_brief") or {}).get("recommendations") or [] if isinstance(r, dict)]
    rejections = receipt.get("rejections", [])
    schema = next((r for r in rejections if r["stage"] == "schema"), None)
    by_index = {r["recommendation_index"]: r for r in rejections if r["recommendation_index"] is not None}
    published = iter(receipt.get("published_recommendations", []))
    entries = {s["account_id"]: s for s in receipt.get("shortlist_supplied", [])}
    ctx = context_by_account(receipt)
    cards = []
    for i, rec in enumerate(recs):
        if schema:
            decision, record = "held", schema
        elif i in by_index:
            decision, record = "held", by_index[i]
        else:
            decision, record = "ready", next(published, {})
        acc, action = rec.get("account_id"), rec.get("recommended_action")
        status = ("held" if decision == "held" else "none" if action == "NO_ACTION"
                  else "info" if action in MORE_INFO else "ready")
        entry = entries.get(acc, {})
        cards.append({"index": i, "account_id": acc, "owner": owner_of(acc), "rec": rec, "record": record,
                      "decision": decision, "status": status, "entry": entry, "tint": tint(entry),
                      "context": ctx.get(acc, {}), "note": latest_note(receipt, acc),
                      "draft": drafted_task(receipt, acc)})
    cards.sort(key=lambda c: (PRIORITY_ORDER.get(c["rec"].get("priority"), 3), c["decision"] == "ready", c["index"]))
    return cards


def next_step(card: dict) -> str:
    if card["decision"] == "held":
        return HELD_NEXT.get(card["record"].get("reason"), "Held back by a safety check")
    return NEXT_STEP.get(card["rec"].get("recommended_action"), "Take a look")


# -- from insight to a safe next step: the primary action, the evidence, the draft -----------------
#: card status -> (the action on the account page, the same action in a Today row). Every one only
#: opens what Radar recorded; none of them runs, creates or changes anything.
CTA = {"ready": ("Review recommendation", "Review →"), "info": ("Review investigation", "Investigate →"),
       "held": ("Investigate blocker", "See blocker →"), "none": ("View account", "View account")}
#: a source link is shown only when the source recorded its own URL; Radar never builds one from an id
#: vendor -> (name, hosts its own record URLs were verified on). Only HubSpot was verified live
#: (2026-09-28: every record's url is on app.hubspot.com); other vendors fall back to recorded evidence
LINKABLE = {"hubspot": ("HubSpot", frozenset({"app.hubspot.com"}))}
TOOL_SOURCE = {"get_account_context": ("Account context", "Account data"),
               "get_recent_account_activity": ("CRM activity", "Latest activity"),
               "get_commerce_context": ("Commerce data", "Commerce data")}
EVIDENCE_TINT = {**SRC_TINT, "Account context": "blue", "Unrecognised source": "coral"}


def plain(text) -> bool:
    """True when recorded text reads as plain English (no internal ids or enum names)."""
    return bool(text) and not re.search(r"\b[a-z0-9]+_[a-z0-9_]+\b|\b[A-Z]+_[A-Z_]+\b", str(text))


def source_action(item: dict) -> tuple[str, str | None]:
    """(label, url). A vendor link only when the recorded source carries its own https URL
    on that vendor's verified host (no userinfo, no port: lookalikes fall back)."""
    name, hosts = LINKABLE.get(str(item.get("source_system") or "").lower(), (None, ()))
    url = item.get("source_url")
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
        ok = (parts is not None and parts.scheme == "https" and parts.hostname in hosts
              and parts.port is None and not parts.username and not parts.password)
    except ValueError:  # a malformed netloc or port
        ok = False
    return (f"Open in {name} ↗", url) if ok else ("View recorded evidence", None)


def drafted_task(receipt: dict, account_id: str) -> dict | None:
    """The follow-up the AI drafted with propose_crm_task, exactly as recorded."""
    return next((t for t in receipt.get("tool_log", []) if t["tool"] == "propose_crm_task"
                 and t.get("account_id") == account_id and t.get("status") == "ok"), None)


def tool_values(tool: str, result: dict) -> list[str]:
    """What one recorded read returned, in plain words."""
    if tool == "get_account_context":
        out, perf = [], result.get("performance_summary") or {}
        if "roas_7d" in perf:
            out.append(f"Campaign ROAS {perf['roas_7d']} this week, {perf.get('roas_baseline_28d')} the 28 days before")
        fresh = result.get("data_freshness") or {}
        if "commerce_hours" in fresh:
            out.append(f"Commerce data {fresh['commerce_hours']:.0f} hours old")
        recon = result.get("reconciliation") or {}
        if "reported_attributed_revenue" in recon:
            out.append(f"Campaign-reported revenue ${recon['reported_attributed_revenue']:,.0f}, commerce revenue "
                       f"${recon['commerce_attributed_revenue']:,.0f} ({pct(recon.get('relative_delta') or 0)} apart)")
        bill = result.get("billing") or {}
        if bill.get("days_overdue"):
            out.append(f"Invoice of ${bill.get('amount_overdue', 0):,.0f} overdue by {bill['days_overdue']} days")
        elif bill:
            out.append("Billing current")
        return out
    if tool == "get_recent_account_activity":
        return [f"{a.get('days_ago')} days ago, {a.get('type')} by {a.get('author')}: “{a.get('body')}”"
                for a in result.get("activity") or []] or ["No activity logged"]
    return [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in result.items()
            if k != "source_ref" and isinstance(v, (str, int, float))]


def evidence(card: dict, receipt: dict, row: dict) -> list[dict]:
    """Each piece of evidence behind a card: the recorded signal facts, then every source the AI cited.
    Items may carry source_system / record_type / record_id / source_url, but only when a source
    recorded them; nothing here is guessed."""
    read = f"during this check, {when(row['started_at'])}"
    items = []
    for s in card["entry"].get("signals", []):
        rows = facts({"signals": [s]})
        domains = (["Campaign data", "Commerce data"] if s["type"] == "attribution_exception"
                   else [SIGNAL_SOURCE.get(s["type"], "Campaign data")] * len(rows))
        for (label, value, detail), domain in zip(rows, domains):
            items.append({"domain": domain, "label": label, "value": value, "detail": detail,
                          "values": [signal_sentence(s)], "ref": s.get("source_ref"),
                          "origin": "Found by Radar’s fixed-rule scan", "provenance": "deterministic signal",
                          "read": read})
    record, rec = card["record"], card["rec"]
    cited = list(dict.fromkeys(record.get("cited_source_refs") or rec.get("source_refs") or []))
    unknown = set(record.get("unknown_source_refs") or [])
    provenance = receipt.get("source_ref_provenance") or {}
    for ref in cited:
        entry = next((t for t in receipt.get("tool_log", []) if ref in (t.get("source_refs_returned") or [])), None)
        if entry is not None and entry["tool"] == "propose_crm_task":
            continue  # the AI's own draft is shown as a draft, not as evidence
        if ref in unknown or entry is None:
            items.append({"domain": "Unrecognised source", "label": "A source Radar never issued",
                          "value": "Nothing recorded", "detail": "the AI cited it, but Radar never read it",
                          "values": ["Radar has no record of this source, so it can’t be shown or trusted."],
                          "ref": ref, "origin": "The AI cited it; Radar never issued it",
                          "provenance": "not among the sources issued in this check", "read": read})
            continue
        result = entry.get("result") or {}
        values = tool_values(entry["tool"], result)
        prov = provenance.get(ref) or {}
        domain, label = TOOL_SOURCE.get(entry["tool"], ("Account data", "Account data"))
        items.append({"domain": domain, "label": label,
                      "value": values[0] if values else "Recorded", "detail": "", "values": values, "ref": ref,
                      "origin": "Read by the AI",
                      "provenance": f"{prov.get('origin') or entry['tool']} · {prov.get('account_id') or entry.get('account_id')}",
                      "read": read, "source_system": result.get("source_system"), "record_type": result.get("record_type"),
                      "record_id": result.get("record_id"), "source_url": result.get("source_url")})
    return items


#: exact known account ids, longest first; "_" counts as a word character, so an id inside a
#: longer identifier (task_harbor_home, harbor_home_2) is never touched
ACCOUNT_ID = re.compile(r"\b(" + "|".join(sorted(map(re.escape, NAMES), key=len, reverse=True)) + r")\b")


def humanize(text) -> str:
    """Display-only: known account ids in recorded AI text read as account names. The stored value,
    the tool trace and the technical details keep the raw text."""
    return ACCOUNT_ID.sub(lambda m: NAMES[m.group(1)], str(text or ""))


def text_button(label: str, attrs: str, ink: str = "#1f6fb8") -> str:
    return (f"<button type=button {attrs} style=\"border:none;background:none;padding:4px 0;font-size:12px;color:{ink};"
            f"cursor:pointer;white-space:nowrap\">{e(label)}</button>")


def mark_reviewed(account_id: str) -> str:
    """Inbox hygiene, kept in this browser tab only (sessionStorage)."""
    return text_button("Mark reviewed", f"data-seen-for=\"{e(account_id)}\" data-off=\"Mark reviewed\" "
                                        "data-on=\"Reviewed ✓ · undo\"", "#6b7190")


def portfolio_facts(owner: str, cards: list[dict], receipt: dict | None) -> dict:
    """Counts for one portfolio, from the recorded check only."""
    accounts = set(dataset.PORTFOLIOS[owner])
    listed = [s for s in (receipt or {}).get("shortlist_supplied", []) if s["account_id"] in accounts]
    mine = [c for c in cards if c["account_id"] in accounts]
    with_type = lambda wanted: sum(1 for s in listed if wanted & set(signal_types(s)))  # noqa: E731
    return {
        "accounts": len(accounts), "attention": len(mine),
        "held": sum(1 for c in mine if c["decision"] == "held"),
        "no_contact": with_type({"engagement_gap"}), "billing": with_type({"billing_hold"}),
        "data_quality": with_type({"stale_data", "attribution_exception"}), "growth": with_type(GROWTH),
        "issues": list(dict.fromkeys(ISSUE[t] for s in listed for t in signal_types(s) if t in ISSUE)),
        "listed": listed, "cards": mine,
    }


# -- the page frame: V4's shell, header and right rail ---------------------------------------------
FONTS = ("<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
         "<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>"
         "<link href=\"https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&amp;display=swap\" "
         "rel=\"stylesheet\">")
CSS = """body{margin:0;background:#dfe3ee;color:#232a4d;font-family:'Poppins',sans-serif;-webkit-font-smoothing:antialiased}
*{box-sizing:border-box}
a{color:#1f6fb8}a:hover{color:#232a4d}
button{font-family:inherit}
button:focus-visible,a:focus-visible,[data-href]:focus-visible{outline:2px solid #2385d6;outline-offset:2px}
[hidden]{display:none!important}
a.nav-i:hover{color:#232a4d!important}
.h-bd:hover{border-color:#c9cee0!important}
.h-lift{transition:transform .15s}.h-lift:hover{transform:translateY(-2px)}
.h-row:hover{background:#fafbfd}
.h-back:hover{background:#e8eaf3!important}
.h-navy:hover{background:#384175!important}
.h-dim:hover{filter:brightness(.98)}
.h-menu:hover{background:#f3f4f9!important}
.h-green:hover{background:#d6f0e3!important}
.h-gate:hover{border-color:#d5d9ea!important;box-shadow:0 6.5px 19px -11px rgba(35,42,77,.35)}
.h-white:hover{background:#eceef5!important}
@media (prefers-reduced-motion:reduce){.h-lift,.h-lift:hover{transition:none;transform:none}}
"""
NAV = (("today", "/", "Today", "◎"), ("portfolio", "/portfolio", "My portfolio", "▦"),
       ("team", "/team", "Team overview", "◫"), ("control", "/control", "Control room", "▤"),
       ("sources", "/sources", "Data sources", "◍"), ("history", "/history", "History", "↺"))


def href(ctx: dict, path: str, viewer: str | None = None) -> str:
    viewer = ctx["viewer"] if viewer is None else viewer
    return path + (f"?as={quote(viewer)}" if viewer != "all" else "")


def mine(ctx: dict, cards: list[dict]) -> list[dict]:
    return [c for c in cards if ctx["viewer"] == "all" or c["owner"] == VIEWERS[ctx["viewer"]]]


def me(ctx: dict) -> tuple[str, str, str]:
    if ctx["viewer"] == "all":
        return "All", "All account managers", f"{len(dataset.ACCOUNT_MANAGERS)} managers · {len(NAMES)} accounts"
    owner = VIEWERS[ctx["viewer"]]
    n = len(dataset.PORTFOLIOS[owner])
    return ini(dataset.ACCOUNT_MANAGERS[owner]), dataset.ACCOUNT_MANAGERS[owner], f"Account Manager · {n} accounts"


def when(ts: str | None, fmt: str = "%b %d, %H:%M UTC") -> str:
    try:
        return datetime.fromisoformat(ts).strftime(fmt).replace(" 0", " ")
    except (TypeError, ValueError):
        return str(ts or "")


def sidebar(ctx: dict, active: str, unseen: int) -> str:
    items = ""
    for key, path, label, icon in NAV:
        on = active == key
        badge = (f"<span data-unseen=badge style=\"min-width:17.5px;height:17.5px;padding:0 5.5px;margin-right:5px;border-radius:9px;"
                 f"background:#2385d6;color:#fff;font-size:11px;font-weight:600;display:grid;place-items:center\""
                 f"{'' if unseen else ' hidden'}>{unseen}</span>") if key == "today" else ""
        items += (
            f"<a class=nav-i href=\"{href(ctx, path)}\"{' aria-current=page' if on else ''} style=\"display:flex;align-items:center;"
            f"gap:11px;text-align:left;padding:8px 13px;border:none;border-radius:14.5px 0 0 14.5px;text-decoration:none;"
            f"background:{'#ffffff' if on else 'transparent'};color:{'#232a4d' if on else '#6b7190'};font-size:13px;"
            f"font-weight:{600 if on else 400};cursor:pointer;box-shadow:{'0 5px 14.5px -8px rgba(35,42,77,.25)' if on else 'none'}\">"
            f"<span style=\"width:24px;height:24px;border-radius:8px;display:grid;place-items:center;"
            f"background:{'#e3f0fb' if on else 'transparent'};color:{'#1f6fb8' if on else '#8a90a8'};font-size:13px;"
            f"font-weight:600;flex-shrink:0\">{icon}</span>"
            f"<span style=\"flex:1;min-width:0;white-space:nowrap\">{label}</span>{badge}</a>")
    return (
        "<aside style=\"background:#f6f7fb;padding:25.5px 0 22.5px 0;display:flex;flex-direction:column;gap:24px\">"
        "<div style=\"display:flex;align-items:center;gap:9.5px;padding:0 21px\">"
        "<div style=\"width:32px;height:32px;border-radius:11px;background:#2385d6;display:grid;place-items:center;flex-shrink:0\">"
        "<div style=\"width:16px;height:16px;border-radius:50%;border:2.5px solid #ffffff;display:grid;place-items:center\">"
        "<div style=\"width:5px;height:5px;border-radius:50%;background:#ffffff\"></div></div></div>"
        "<div style=\"display:flex;flex-direction:column;line-height:1.2\"><span style=\"font-size:15.5px;font-weight:700\">"
        "Account Radar</span><span style=\"font-size:11px;color:#6b7190\">Revenue Operations Agent</span></div></div>"
        f"<nav aria-label=Views style=\"display:flex;flex-direction:column;gap:3px;padding-left:14.5px\">{items}</nav>"
        "<span style=\"margin-top:auto;padding:0 22.5px;font-size:11px;line-height:1.55;color:#8a90a8\">Synthetic demo data. "
        "Brands and people are fictional.</span></aside>")


def header(ctx: dict, kicker: str, greet_a: str, greet_b: str, row: dict | None, cards: list[dict], here: str) -> str:
    i, who, _ = me(ctx)
    options = [("all", "All account managers", "gray", f"{len(dataset.ACCOUNT_MANAGERS)} managers · {len(NAMES)} accounts",
                len(cards))]
    options += [(slug, dataset.ACCOUNT_MANAGERS[o], AM_TINT[o], f"{len(dataset.PORTFOLIOS[o])} accounts",
                 sum(1 for c in cards if c["owner"] == o)) for slug, o in VIEWERS.items()]
    menu = ""
    for slug, label, t, meta, attn in options:
        on = ctx["viewer"] == slug
        badge = (f"<span style=\"min-width:17.5px;height:17.5px;padding:0 5.5px;border-radius:9px;background:#fdece6;color:#c2462b;"
                 f"font-size:11px;font-weight:600;display:grid;place-items:center\">{attn}</span>") if attn else ""
        menu += (
            f"<a class=h-menu data-pick=\"{e(label.lower())}\" href=\"{href(ctx, here, slug)}\" style=\"display:flex;align-items:center;"
            f"gap:8px;padding:6.5px 8px;border:none;border-radius:9.5px;background:{'#eef4fb' if on else 'transparent'};"
            f"cursor:pointer;text-align:left;color:#232a4d;text-decoration:none\">"
            f"<span style=\"width:24px;height:24px;border-radius:50%;background:{T[t][1]};color:{T[t][2]};display:grid;"
            f"place-items:center;font-size:10.5px;font-weight:600;flex-shrink:0\">{'All' if slug == 'all' else ini(label)}</span>"
            f"<span style=\"flex:1;min-width:0;display:flex;flex-direction:column;line-height:1.3\"><span style=\"font-size:12.5px;"
            f"font-weight:{600 if on else 500}\">{e(label)}</span><span style=\"font-size:11px;color:#6b7190\">{e(meta)}</span>"
            f"</span>{badge}</a>")
    status = row["status"] if row else None
    label, short, t, _ = RUN.get(status, ("", "No checks yet", "gray", ""))
    dot = {"green": "#3fa874", "yellow": "#d9a21b", "coral": "#c2462b"}.get(t, "#8a90a8")
    last = (f"Last check {when(row['started_at'], '%H:%M UTC')} · {short}" if row else "No checks yet")
    return (
        "<header style=\"display:flex;align-items:flex-end;justify-content:space-between;gap:13px;flex-wrap:wrap\">"
        f"<div style=\"display:flex;flex-direction:column;gap:2px\"><span style=\"font-size:11.5px;color:#6b7190\">{e(kicker)}</span>"
        f"<h1 style=\"margin:0;font-size:27px;font-weight:300;letter-spacing:-.01em\">{html.escape(greet_a)}<strong style=\"font-weight:700\">"
        f"{e(greet_b)}</strong></h1></div>"
        "<div style=\"display:flex;align-items:center;gap:8px;flex-wrap:wrap\"><div style=\"position:relative\">"
        "<button class=h-bd type=button data-toggle=scope aria-haspopup=true style=\"display:flex;align-items:center;gap:8px;height:35px;"
        "padding:0 13px 0 5px;border-radius:799px;border:1px solid #e3e5ef;background:#ffffff;cursor:pointer;font-size:12.5px;color:#232a4d\">"
        f"<span style=\"width:25.5px;height:25.5px;border-radius:50%;background:#e3f0fb;color:#1f6fb8;display:grid;place-items:center;"
        f"font-size:10.5px;font-weight:600\">{e(i)}</span><span style=\"display:flex;flex-direction:column;align-items:flex-start;"
        f"line-height:1.2\"><span style=\"font-size:10.5px;color:#6b7190\">Viewing as</span><span style=\"font-weight:600;"
        f"white-space:nowrap\">{e(who)}</span></span><span style=\"color:#6b7190;font-size:11px;margin-left:3px\">▾</span></button>"
        "<div id=scope hidden style=\"position:absolute;top:41.5px;right:0;z-index:30;width:256px;background:#ffffff;border-radius:16px;"
        "box-shadow:0 19px 48px -16px rgba(35,42,77,.4);border:1px solid #eceef5;padding:8px;display:flex;flex-direction:column;gap:5px\">"
        "<input data-filter=pick placeholder=\"Search account managers\" aria-label=\"Search account managers\" style=\"height:34px;"
        "border-radius:9.5px;border:1px solid #e3e5ef;padding:0 11px;font-family:inherit;font-size:12.5px;color:#232a4d;outline:none;"
        "background:#f6f7fb\">"
        f"<div style=\"max-height:240px;overflow:auto;display:flex;flex-direction:column;gap:2px\">{menu}"
        "<span data-empty=pick hidden style=\"padding:11px 8px;font-size:12.5px;color:#6b7190\">No one matches that name.</span>"
        "</div></div></div>"
        f"<a class=h-green href=\"{href(ctx, '/history')}\" style=\"display:flex;align-items:center;gap:6.5px;height:35px;border:none;"
        f"background:{T[t][0]};color:{T[t][2]};border-radius:799px;padding:0 13px;font-size:11px;font-weight:500;cursor:pointer;"
        f"text-decoration:none\"><span style=\"width:6.5px;height:6.5px;border-radius:50%;background:{dot}\"></span>{e(last)}</a>"
        "</div></header>")


def rail(ctx: dict, row: dict | None, cards: list[dict], receipt: dict | None) -> str:
    i, who, role = me(ctx)
    total = len(NAMES)
    scanned = row["accounts_scanned"] if row else None
    fill = round(100 * (scanned or 0) / total) if total else 0
    label, _, t, _ = RUN.get(row["status"] if row else None, ("No checks yet", "", "gray", ""))
    dot = {"green": "#3fa874", "yellow": "#d9a21b", "coral": "#c2462b"}.get(t, "#8a90a8")
    held = sum(1 for c in cards if c["decision"] == "held")
    shortlisted = row["accounts_shortlisted"] if row and row["accounts_shortlisted"] is not None else None
    stats = [
        ("Shortlisted", "-" if shortlisted is None else shortlisted,
         f"strongest signals, capped at {config.MAX_SHORTLIST}", T["gray"][0], "#232a4d", "/control"),
        ("Held back", held if receipt else "-",
         "stopped by the safety checks" if held else "nothing stopped by the safety checks", T["lav"][0], T["lav"][2], "/control"),
        ("Data", "Demo", "synthetic, kept apart from live systems", T["yellow"][0], T["yellow"][2], "/sources"),
    ]
    blocks = "".join(
        f"<a class=h-dim href=\"{href(ctx, path)}\" style=\"border:none;border-radius:16px;background:{bg};padding:11px 13px;display:flex;"
        f"align-items:center;justify-content:space-between;gap:8px;cursor:pointer;text-align:left;color:#232a4d;text-decoration:none\">"
        f"<div style=\"display:flex;flex-direction:column;gap:2px\"><span style=\"font-size:11px;color:#4d5372;white-space:nowrap\">{e(k)}</span>"
        f"<span style=\"font-size:18.5px;font-weight:600;color:{ink}\">{e(v)}</span></div>"
        f"<span style=\"font-size:11px;color:#4d5372;text-align:right;max-width:112px;line-height:1.35\">{e(note)}</span></a>"
        for k, v, note, bg, ink, path in stats)
    return (
        "<aside style=\"flex:0 1 224px;min-width:200px;display:flex;flex-direction:column;gap:11px;padding-left:22.5px;"
        "border-left:1px solid #eef0f6\"><div style=\"display:flex;flex-direction:column;align-items:center;gap:5px;padding:5px 0 9.5px\">"
        f"<div style=\"width:61px;height:61px;border-radius:19px;background:#e3f0fb;color:#1f6fb8;display:grid;place-items:center;"
        f"font-size:20.5px;font-weight:600\">{e(i)}</div><span style=\"font-size:14.5px;font-weight:600;margin-top:5px\">{e(who)}</span>"
        f"<span style=\"font-size:11.5px;color:#1f6fb8;font-weight:500\">{e(role)}</span></div>"
        "<div style=\"border-radius:19px;background:#f6f7fb;padding:16px;display:flex;flex-direction:column;align-items:center;gap:3px\">"
        "<span style=\"align-self:flex-start;font-size:12.5px;font-weight:600\">Latest check</span>"
        "<div style=\"position:relative;width:160px;height:89px;margin-top:6.5px\">"
        "<svg viewBox=\"0 0 120 66\" width=\"160\" height=\"89\" style=\"display:block\" aria-hidden=true>"
        "<path d=\"M10 60 A50 50 0 0 1 110 60\" fill=\"none\" stroke=\"#e6e8f1\" stroke-width=\"10\" stroke-linecap=\"round\"></path>"
        f"<path d=\"M10 60 A50 50 0 0 1 110 60\" fill=\"none\" stroke=\"#2385d6\" stroke-width=\"10\" stroke-linecap=\"round\" "
        f"pathLength=\"100\" stroke-dasharray=\"{fill} 100\"></path></svg>"
        "<div style=\"position:absolute;left:0;right:0;bottom:0;display:flex;flex-direction:column;align-items:center\">"
        f"<span style=\"font-size:20.5px;font-weight:600;line-height:1\">{'-' if scanned is None else scanned}/{total}</span>"
        "<span style=\"font-size:11px;color:#4d5372\">accounts scanned</span></div></div>"
        f"<div style=\"display:flex;align-items:center;gap:6.5px;margin-top:8px;font-size:11.5px;color:{T[t][2]};font-weight:500\">"
        f"<span style=\"width:6.5px;height:6.5px;border-radius:50%;background:{dot}\"></span>{e(label)}</div>"
        f"<a class=h-white href=\"{href(ctx, '/control')}\" style=\"margin-top:6.5px;border:none;background:#ffffff;border-radius:9.5px;"
        "height:34px;padding:0 13px;font-size:11.5px;font-weight:500;color:#232a4d;cursor:pointer;width:100%;display:grid;"
        "place-items:center;text-decoration:none\">Open the control room</a></div>"
        f"{blocks}</aside>")


def page(title: str, ctx: dict, active: str, kicker: str, greet: tuple[str, str], section: str,
         row: dict | None, cards: list[dict], receipt: dict | None, here: str) -> str:
    my = mine(ctx, cards)
    data = {"run": row["run_id"] if row else "", "mine": [c["account_id"] for c in my]}
    return (
        f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content=\"width=device-width, initial-scale=1\">"
        f"<title>{e(title)} - Account Radar</title><link rel=icon href=\"data:,\">{FONTS}<style>{CSS}</style></head>"
        f"<body data-radar='{e(json.dumps(data))}'><div style=\"min-height:100vh;padding:clamp(0px,2.5vw,25.5px)\">"
        "<div style=\"max-width:1440px;margin:0 auto;background:#ffffff;border-radius:29px;box-shadow:0 24px 64px -24px "
        "rgba(35,42,77,.25);display:grid;grid-template-columns:200px minmax(0,1fr);overflow:hidden;min-height:calc(100vh - 51px)\">"
        + sidebar(ctx, active, len(my))
        + "<main style=\"padding:27px clamp(16px,2vw,30px) 35px;display:flex;flex-wrap:wrap;gap:29px;align-items:flex-start\">"
        "<div style=\"flex:1 1 480px;min-width:0;display:flex;flex-direction:column;gap:22.5px\">"
        + header(ctx, kicker, greet[0], greet[1], row, cards, here) + section + "</div>"
        + rail(ctx, row, cards, receipt) + "</main></div></div>" + f"<script>{SCRIPT}</script></body></html>")


def title_block(title: str, sub: str, max_width: bool = False) -> str:
    extra = "max-width:560px;" if max_width else ""
    return (f"<div style=\"display:flex;flex-direction:column;gap:3px\"><h2 style=\"margin:0;font-size:18.5px;font-weight:600\">"
            f"{e(title)}</h2><span style=\"font-size:13px;color:#4d5372;{extra}line-height:1.5\">{e(sub)}</span></div>")


def banner(state: str) -> str:
    text = {
        "mismatch": "The saved evidence for this check was changed after it ran (HASH MISMATCH), so Radar won’t show it.",
        "missing": "The saved evidence for this check is missing (RECEIPT MISSING), so its details can’t be shown.",
        "outside": "This check’s evidence points outside its folder, so Radar didn’t read it.",
    }.get(state)
    return (f"<div role=alert style=\"border-radius:16px;background:#fdece6;color:#c2462b;padding:13px 14.5px;font-size:13px;"
            f"font-weight:500;line-height:1.5\">{e(text)}</div>") if text else ""


def stopped(row: dict) -> str:
    problems = json.loads(row["problems"] or "[]")
    reason = next((s for p in problems if (s := problem_sentence(p, row["retry_count"] or 0))), None)
    return (f"<div style=\"border-radius:16px;background:#fdece6;padding:13px 14.5px;display:flex;flex-direction:column;gap:3px\">"
            f"<span style=\"font-size:13px;font-weight:600;color:#c2462b\">The latest check stopped before it finished</span>"
            f"<span style=\"font-size:12.5px;color:#3d4363;line-height:1.5\">{e(reason or 'It stopped safely before producing results.')} "
            "No account was changed, and the next check will try again.</span></div>")


def tech_box(lines: list[str], attr: str = "data-tech") -> str:
    """V4's technical panel: dark, monospace, hidden until asked for."""
    body = "".join(f"<span>{e(line)}</span>" for line in lines)
    return (f"<div {attr} hidden style=\"border-radius:14.5px;background:#232a4d;padding:13px;font-family:ui-monospace,Menlo,monospace;"
            f"font-size:11px;line-height:1.7;color:#d6daf0;display:flex;flex-direction:column;overflow-x:auto;"
            f"word-break:break-all\">{body}</div>")


def tech_toggle(show: str, hide: str) -> str:
    return (f"<button type=button data-tech-toggle data-show=\"{e(show)}\" data-hide=\"{e(hide)}\" style=\"align-self:flex-start;"
            f"border:none;background:none;padding:5px 0;font-size:12.5px;color:#1f6fb8;cursor:pointer\">{e(show)}</button>")


def load(ledger: Path, run_id: str | None):
    row = find_run(ledger, run_id)
    if row is None:
        return None, None, "none", []
    receipt, state = load_receipt(row, ledger)
    return row, receipt, state, brief(receipt) if receipt else []


def empty_page(ctx: dict, title: str, active: str, here: str) -> tuple[int, str]:
    section = title_block("No checks recorded yet", "Run python scripts/demo.py healthy (offline, no cost), then reload.")
    return 200, page(title, ctx, active, "Account Radar", ("", title), section, None, [], None, here)


# -- 1. TODAY -----------------------------------------------------------------------------------
def render_today(ledger: Path, run_id: str | None, viewer: str = "all") -> tuple[int, str]:
    ctx = {"viewer": viewer}
    row, receipt, state, cards = load(ledger, run_id)
    here = f"/run/{quote(run_id)}" if run_id else "/"
    if row is None:
        if run_id:
            return 404, page("Not found", ctx, "today", "Account Radar", ("", "Check not found"), "", None, [], None, here)
        return empty_page(ctx, "Today", "today", here)
    team = viewer == "all"
    my = mine(ctx, cards)
    owned = len(NAMES) if team else len(dataset.PORTFOLIOS[VIEWERS[viewer]])
    listed = [s for s in (receipt or {}).get("shortlist_supplied", [])
              if team or owner_of(s["account_id"]) == VIEWERS[viewer]]
    held = sum(1 for c in my if c["decision"] == "held")
    greet = ("Team ", "morning brief") if team else ("Hello, ", dataset.ACCOUNT_MANAGERS[VIEWERS[viewer]].split()[0])
    kicker = when(row["started_at"], "%A, %b %d")
    if receipt is None:
        section = banner(state) or stopped(row)
        return 200, page("Today", ctx, "today", kicker, greet, section, row, cards, receipt, here)
    n = len(my)
    if team:
        title = f"{n} account{'s' if n != 1 else ''} across the team need{'s' if n == 1 else ''} attention today"
        sub = (f"Radar checked all {owned} team accounts and took a closer look at the {len(listed)} with the "
               "strongest signals.")
    else:
        title = f"{n} account{'s' if n != 1 else ''} in your portfolio need{'s' if n == 1 else ''} attention today"
        sub = (f"Radar checked your {owned} accounts. {len(listed)} made the shortlist, and {n} "
               f"{'is' if n == 1 else 'are'} worth your time now.")
    tiles = [(owned, "accounts checked", T["blue"][0], T["blue"][2], "/control", ""),
             (len(listed), "took a closer look", T["gray"][0], "#232a4d", "/control" if team else "/portfolio", ""),
             (n, "need attention", T["coral"][0], T["coral"][2], None, "data-unseen=tile"),
             (held, "held back for safety", T["lav"][0], T["lav"][2], "/control", "")]
    tile_html = "".join(
        (f"<a class=h-lift href=\"{href(ctx, path)}\"" if path else "<div") +
        f" style=\"border:none;border-radius:19px;background:{bg};padding:14.5px 16px;text-align:left;display:flex;"
        f"flex-direction:column;gap:3px;cursor:pointer;color:#232a4d;text-decoration:none\">"
        f"<span {attr} style=\"font-size:25.5px;font-weight:600;color:{ink};line-height:1.1\">{v}</span>"
        f"<span style=\"font-size:11.5px;color:#4d5372;line-height:1.35\">{e(label)}</span>" + ("</a>" if path else "</div>")
        for v, label, bg, ink, path, attr in tiles)
    statuses = [("all", "All"), ("ready", "Ready"), ("info", "Need info")]
    statuses += [(k, lbl) for k, lbl in (("held", "Held back"), ("none", "No action")) if any(c["status"] == k for c in my)]
    tabs = "".join(
        f"<button type=button data-tab=\"{k}\" style=\"height:30px;padding:0 9.5px;border-radius:8px;border:none;"
        f"background:{'#ffffff' if k == 'all' else 'transparent'};color:{'#232a4d' if k == 'all' else '#6b7190'};font-size:11.5px;"
        f"font-weight:500;cursor:pointer;white-space:nowrap;box-shadow:{'0 2px 6.5px -3px rgba(35,42,77,.3)' if k == 'all' else 'none'}\">"
        f"{e(lbl)} <span data-n style=\"color:#8a90a8\">{n if k == 'all' else sum(1 for c in my if c['status'] == k)}</span></button>"
        for k, lbl in statuses)
    chip_names = list(dict.fromkeys(CHIP[t] for c in my for t in signal_types(c["entry"]) if t in CHIP))
    chips = "".join(
        f"<button type=button data-chip=\"{e(k)}\" style=\"height:30px;padding:0 9.5px;border-radius:799px;border:1px solid "
        f"{'#232a4d' if k == 'all' else '#e3e5ef'};background:{'#232a4d' if k == 'all' else '#ffffff'};"
        f"color:{'#ffffff' if k == 'all' else '#4d5372'};font-size:11px;font-weight:500;cursor:pointer;white-space:nowrap\">"
        f"{e(label)} · <span data-n>{n if k == 'all' else sum(1 for c in my if label in [CHIP.get(t) for t in signal_types(c['entry'])])}</span></button>"
        for k, label in [("all", "All")] + [(x, x) for x in chip_names])
    grid = "minmax(176px,2.1fr) minmax(88px,.9fr) minmax(104px,1fr) minmax(144px,1.4fr) 150px 112px"
    rows = ""
    for rank, c in enumerate(my):
        nm = name(c["account_id"])
        t = T[c["tint"]]
        f0 = (facts(c["entry"]) or [("", "", "")])[0]
        label, ink, bg = STATUS[c["status"]]
        owner = dataset.ACCOUNT_MANAGERS.get(c["owner"], "")
        sigs = "|".join(CHIP[x] for x in signal_types(c["entry"]) if x in CHIP)
        link = f"/run/{quote(row['run_id'])}/account/{quote(str(c['account_id']))}"
        rows += (
            f"<div data-row data-href=\"{href(ctx, link)}\" role=link tabindex=0 data-acc=\"{e(c['account_id'])}\" data-status=\"{c['status']}\" "
            f"data-signals=\"{e(sigs)}\" data-rank=\"{rank}\" data-owner=\"{e(owner)}\" data-name=\"{e(nm)}\" "
            f"data-search=\"{e((nm + ' ' + owner + ' ' + headline(c['entry'])).lower())}\" class=h-row "
            f"style=\"display:grid;grid-template-columns:{grid};gap:11px;padding:9.5px 14.5px;border-top:1px solid #f0f1f7;align-items:center;cursor:pointer\">"
            f"<div style=\"display:flex;align-items:center;gap:9.5px;min-width:0\"><span style=\"width:29px;height:29px;border-radius:50%;"
            f"background:{t[1]};color:{t[2]};display:grid;place-items:center;font-size:11px;font-weight:600;flex-shrink:0\">{e(ini(nm))}</span>"
            f"<span style=\"display:flex;flex-direction:column;min-width:0;line-height:1.35\"><span style=\"font-size:13px;font-weight:600\">"
            f"{e(nm)}</span><span style=\"font-size:11.5px;color:#4d5372\">{e(headline(c['entry']))}</span></span></div>"
            f"<span style=\"font-size:12.5px\">{e(owner)}</span>"
            f"<span style=\"display:flex;flex-direction:column;line-height:1.3\"><span style=\"font-size:14px;font-weight:600;color:{t[2]}\">"
            f"{e(f0[1])}</span><span style=\"font-size:11px;color:#6b7190\">{e(f0[0])}</span></span>"
            f"<span style=\"display:flex;flex-direction:column;gap:2px;line-height:1.4\"><span style=\"font-size:12.5px\">"
            f"{e(next_step(c))}</span>" + ("<span style=\"font-size:11px;color:#6b7190\">Draft follow-up prepared</span>"
                                           if c["draft"] else "") + "</span>"
            f"<span style=\"justify-self:start;font-size:11px;font-weight:600;padding:4px 9px;border-radius:799px;background:{bg};"
            f"color:{ink};white-space:nowrap\">{e(label)}</span>"
            f"<span style=\"display:flex;flex-direction:column;align-items:flex-start;gap:1px\">"
            f"<a class=h-navy data-cta=\"{c['status']}\" href=\"{href(ctx, link)}{'' if c['status'] == 'none' else '#review'}\" "
            f"style=\"height:34px;padding:0 11px;border-radius:8px;background:#232a4d;color:#ffffff;font-size:12px;font-weight:500;"
            f"display:grid;place-items:center;white-space:nowrap;text-decoration:none\">{e(CTA[c['status']][1])}</a>"
            f"{mark_reviewed(c['account_id'])}</span></div>")
    section = (
        "<section data-screen-label=Today style=\"display:flex;flex-direction:column;gap:22.5px\">"
        + banner(state) + title_block(title, sub)
        + f"<div style=\"display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:11px\">{tile_html}</div>"
        "<div style=\"display:flex;flex-direction:column;gap:9.5px\"><div style=\"display:flex;gap:8px;flex-wrap:wrap;align-items:center\">"
        f"<input data-filter=rows placeholder=\"{'Search accounts or owners' if team else 'Search your accounts'}\" "
        "aria-label=\"Search accounts\" style=\"flex:1 1 192px;height:35px;border-radius:11px;border:1px solid #e3e5ef;padding:0 13px;"
        "font-family:inherit;font-size:12.5px;color:#232a4d;outline:none;background:#f6f7fb\">"
        f"<div style=\"display:flex;gap:3px;padding:3px;border-radius:11px;background:#f3f4f9\">{tabs}</div>"
        "<select data-sort=rows aria-label=Sort style=\"height:35px;border-radius:11px;border:1px solid #e3e5ef;padding:0 9.5px;"
        "font-family:inherit;font-size:12.5px;color:#232a4d;background:#ffffff\"><option value=rank>Sort: priority</option>"
        "<option value=owner>Sort: owner</option><option value=name>Sort: account A–Z</option></select></div>"
        "<div style=\"display:flex;gap:5px;flex-wrap:wrap;align-items:center\"><span style=\"font-size:11px;color:#6b7190;"
        f"margin-right:3px\">Signal</span>{chips}</div>"
        "<div style=\"border-radius:17.5px;border:1px solid #eceef5;overflow-x:auto\"><div style=\"min-width:800px\">"
        f"<div style=\"display:grid;grid-template-columns:{grid};gap:11px;padding:9px 14.5px;background:#f6f7fb;font-size:11px;"
        "font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:#6b7190\"><span>Account</span><span>Owner</span>"
        "<span>Key number</span><span>Recommendation</span><span>Status</span><span>Action</span></div>"
        f"<div data-rows>{rows}</div>"
        f"<div data-empty=rows {'' if not my else 'hidden'} style=\"padding:22.5px 14.5px;border-top:1px solid #f0f1f7;font-size:12.5px;"
        "color:#6b7190;text-align:center\">Nothing matches these filters. <button type=button data-clear style=\"border:none;"
        "background:none;color:#1f6fb8;font-size:12.5px;cursor:pointer;padding:0\">Clear filters</button></div></div></div>"
        f"<span data-foot data-total=\"{n}\" data-checked=\"{owned}\" style=\"font-size:11px;color:#6b7190\">Showing {n} of {n} surfaced · "
        f"{owned} accounts checked · the rest had nothing to raise</span></div></section>")
    return 200, page("Today", ctx, "today", kicker, greet, section, row, cards, receipt, here)


# -- 2. ACCOUNT DETAIL --------------------------------------------------------------------------
def evidence_item(item: dict, n: int) -> str:
    """One piece of evidence; "View recorded evidence" reveals exactly what was recorded."""
    tt = T[EVIDENCE_TINT.get(item["domain"], "gray")]
    label, url = source_action(item)
    link = (f"<a href=\"{e(url)}\" target=_blank rel=\"noopener noreferrer\" style=\"font-size:12px;white-space:nowrap\">"
            f"{e(label)}</a>" if url else "")
    detail = f" · {e(item['detail'])}" if item["detail"] else ""
    values = "".join(f"<span>{e(v)}</span>" for v in item["values"])
    reveal = text_button("View recorded evidence", f"data-reveal=ev{n} data-show=\"View recorded evidence\" "
                                                   "data-hide=\"Hide recorded evidence\"")
    return (
        "<div style=\"border-radius:16px;border:1px solid #eceef5;padding:10px 13px;display:flex;flex-direction:column;gap:6px\">"
        "<div style=\"display:flex;justify-content:space-between;align-items:center;gap:6px 10px;flex-wrap:wrap\">"
        "<span style=\"display:flex;flex-direction:column;gap:3px;min-width:0;flex:1 1 180px\"><span style=\"align-self:flex-start;"
        f"font-size:11px;font-weight:500;padding:3px 8px;border-radius:799px;background:{tt[1]};color:{tt[2]}\">{e(item['domain'])}"
        f"</span><span style=\"font-size:12.5px;line-height:1.45\"><span style=\"color:#6b7190\">{e(item['label'])}:</span> "
        f"{e(item['value'])}{detail}</span></span><span style=\"display:flex;flex-direction:column;align-items:flex-end;gap:1px\">"
        f"{link}{reveal}</span></div>"
        f"<div id=ev{n} data-panel hidden style=\"border-radius:12px;background:#f6f7fb;padding:10px 12px;display:flex;"
        f"flex-direction:column;gap:4px;font-size:12.5px;line-height:1.5;color:#3d4363\">{values}"
        f"<span style=\"font-size:11px;color:#6b7190\">{e(item['origin'])} · {e(item['read'])} · synthetic demo data</span>"
        "<span style=\"font-family:ui-monospace,Menlo,monospace;font-size:11px;color:#6b7190;word-break:break-all\">"
        f"source ref {e(item['ref'])} ← {e(item['provenance'])}</span></div></div>")


def review_panel(card: dict, status_text: str, held_why: str, items: list[dict]) -> str:
    """The recorded work behind a card, organised for review. Nothing here is generated or sent."""
    rec, record, entry, status = card["rec"], card["record"], card["entry"], card["status"]
    ai_action = NEXT_STEP.get(rec.get("recommended_action"), "something")

    def part(label: str, body: str, ink: str = "#6b7190") -> str:
        return (f"<div style=\"display:flex;flex-direction:column;gap:3px;padding-top:10px;border-top:1px solid #f0f1f7\">"
                f"<span style=\"font-size:11px;font-weight:600;color:{ink}\">{e(label)}</span>"
                f"<div style=\"display:flex;flex-direction:column;gap:3px;font-size:13px;line-height:1.5\">{body}</div></div>")

    lines = lambda texts: "".join(f"<span>{e(x)}</span>" for x in texts)  # noqa: E731
    parts = []
    if card["decision"] == "held":
        reason = record.get("reason")
        blocking = [i for i in items if (reason == "expansion_blocked_by_stale_data" and i["label"] == "Commerce data age")
                    or (reason in ("unknown_source_reference", "cross_account_source_reference")
                        and i["domain"] == "Unrecognised source")
                    or (reason == "expansion_blocked_by_reconciliation" and "revenue" in i["label"].lower())]
        parts += [part("What the AI suggested", lines([ai_action])),
                  part("What the safety check decided", lines(["Held back. This isn’t ready to act on."]), "#c2462b"),
                  part("Why", lines([held_why])),
                  part("What would release it", lines([next_step(card)]))]
        if blocking:
            parts.append(part("Evidence behind the block",
                              lines(f"{i['label']}: {i['value']}" + (f" · {i['detail']}" if i["detail"] else "") for i in blocking)))
    else:
        signals = entry.get("signals", [])
        if entry.get("competing_signals"):
            why = [f"{'Opportunity' if s['type'] in GROWTH else 'Risk'}: {signal_sentence(s)}"
                   for s in sorted(signals, key=lambda s: s["type"] not in GROWTH)]
        else:
            why = [signal_sentence(s) for s in signals]
        parts += [part("Why now", lines(why)), part("What Radar concluded", lines([status_text]))]
        if record.get("confidence"):
            reason = record.get("confidence_reason")
            parts.append(part("Confidence", lines([record["confidence"].capitalize() + (f": {reason}" if plain(reason) else "")])))
    rationale = rec.get("rationale")
    parts.append(part("The AI’s reasoning", lines([f"“{rationale}”" if plain(rationale) else
                      "Recorded with internal names; shown in full under “How Radar reached this result”."])))
    if rec.get("acknowledged_tension"):
        parts.append(part("Tension the AI noted", lines([f"“{rec['acknowledged_tension']}”"])))
    draft = card["draft"]
    if draft:
        args = draft.get("arguments") or {}
        note = ("Drafted before the safety check held the recommendation back, so it isn’t ready to use. "
                if card["decision"] == "held" else "")
        parts.append(
            f"<div id=draft style=\"border-radius:14px;background:#fff4dc;padding:12px 13px;display:flex;flex-direction:column;"
            f"gap:4px;scroll-margin-top:16px\"><span style=\"font-size:11px;font-weight:600;color:#946300\">Draft prepared by Radar"
            f"</span><span style=\"font-size:13.5px;font-weight:600\">{e(humanize(args.get('title')))}</span>"
            f"<span style=\"font-size:12.5px;color:#3d4363\">{e(humanize(args.get('description')))}</span>"
            f"<span style=\"font-size:12px;color:#3d4363\"><strong style=\"font-weight:600\">Status:</strong> Draft only. {e(note)}"
            "Nothing has been created in HubSpot or any CRM.</span></div>")
    else:
        parts.append(part("Draft prepared by Radar", lines(["No follow-up task was drafted for this account in this check."])))
    title = {"ready": "Recommendation review", "info": "Investigation review", "held": "Why this is held back"}.get(status, "Review")
    return (
        "<div id=review data-panel hidden style=\"border-radius:19px;border:1px solid #eceef5;background:#ffffff;padding:14.5px;"
        "display:flex;flex-direction:column;gap:10px;scroll-margin-top:16px\">"
        "<div style=\"display:flex;justify-content:space-between;align-items:center;gap:8px\"><span style=\"display:flex;"
        f"flex-direction:column;gap:2px\"><span style=\"font-size:11px;color:#6b7190\">{e(title)}</span><span style=\"font-size:15.5px;"
        f"font-weight:600\">{e(next_step(card) if card['decision'] == 'held' else ai_action)}</span></span>"
        f"<span style=\"font-size:11px;font-weight:600;padding:4px 9px;border-radius:799px;background:{STATUS[status][2]};"
        f"color:{STATUS[status][1]};white-space:nowrap\">{e(STATUS[status][0])}</span></div>"
        + "".join(parts)
        + "<div style=\"display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;padding-top:10px;"
        "border-top:1px solid #f0f1f7\"><span style=\"font-size:11px;color:#6b7190;flex:1 1 200px\">Reviewing changes nothing: "
        "Radar doesn’t update the CRM, create tasks or contact anyone.</span>"
        + text_button("Close review", "data-close=review") + "</div></div>")


def render_account(ledger: Path, run_id: str, account_id: str, viewer: str = "all") -> tuple[int, str]:
    ctx = {"viewer": viewer}
    row, receipt, state, cards = load(ledger, run_id)
    here = f"/run/{quote(run_id)}/account/{quote(account_id)}"
    back = (f"<a class=h-back href=\"{href(ctx, '/')}\" style=\"align-self:flex-start;border:none;background:#f3f4f9;border-radius:799px;"
            "padding:6.5px 13px;font-size:12.5px;color:#4d5372;cursor:pointer;text-decoration:none\">← Back to the morning brief</a>")
    if row is None:
        return 404, page("Not found", ctx, "today", "Account details", ("", "Check not found"), "", None, [], None, here)
    card = next((c for c in cards if c["account_id"] == account_id), None)
    if receipt is None or card is None:
        section = (f"<section style=\"display:flex;flex-direction:column;gap:19px\">{back}"
                   + (banner(state) or (stopped(row) if receipt is None else title_block(
                       f"{name(account_id)} wasn’t raised in this check", "Nothing was recorded for it.")))
                   + "</section>")
        return (404 if receipt is not None else 200), page(name(account_id), ctx, "today", "Account details",
                                                           ("", name(account_id)), section, row, cards, receipt, here)
    entry, rec, record, context = card["entry"], card["rec"], card["record"], card["context"]
    ready = card["decision"] == "ready"
    t = T[card["tint"]]
    nm = name(account_id)
    owner = dataset.ACCOUNT_MANAGERS.get(card["owner"], "unassigned")
    label, sink, _ = STATUS[card["status"]]
    competing = bool(entry.get("competing_signals"))
    signals = entry.get("signals", [])
    if competing:  # V4 tells the tension opportunity-first
        signals = sorted(signals, key=lambda s: s["type"] not in GROWTH)
    why = [(signal_sentence(s, competing), SIGNAL_SOURCE.get(s["type"], "Campaign data")) for s in signals]
    note = card["note"]
    why_html = "".join(
        f"<div style=\"display:grid;grid-template-columns:auto minmax(0,1fr);gap:11px;padding:13px;border-radius:16px;background:#f6f7fb\">"
        f"<span style=\"width:25.5px;height:25.5px;border-radius:50%;background:#ffffff;display:grid;place-items:center;font-size:12.5px;"
        f"font-weight:600;color:#1f6fb8\">{i}</span><div style=\"display:flex;flex-direction:column;gap:5px\">"
        f"<span style=\"font-size:13.5px;line-height:1.5\">{e(text)}</span><span style=\"align-self:flex-start;font-size:11px;"
        f"font-weight:500;padding:3px 8px;border-radius:799px;background:{T[SRC_TINT[src]][1]};color:{T[SRC_TINT[src]][2]}\">"
        f"{e(src)}</span></div></div>" for i, (text, src) in enumerate(why, 1))
    metrics = "".join(
        f"<div style=\"border-radius:16px;background:{t[0]};padding:13px;display:flex;flex-direction:column;gap:2px\">"
        f"<span style=\"font-size:11px;color:#4d5372\">{e(k)}</span><span style=\"font-size:18.5px;font-weight:600;color:{t[2]}\">{e(v)}</span>"
        f"<span style=\"font-size:11px;color:#4d5372\">{e(d)}</span></div>" for k, v, d in facts(entry))
    fresh = context.get("data_freshness") or {}
    recon = context.get("reconciliation") or {}
    rows = []
    if note:
        rows.append(("Latest CRM activity", f"{note.get('days_ago')} days ago: “{note.get('body')}”"))
    if "commerce_hours" in fresh:
        rows.append(("Commerce data", f"{fresh['commerce_hours']:.0f} hours old ({'stale' if fresh.get('stale') else 'fresh'})"))
    if recon.get("status"):
        rows.append(("Revenue numbers", {
            "match": "Campaign and commerce revenue agree",
            "probable_match": "Campaign and commerce revenue roughly agree",
            "exception": f"{pct(recon.get('relative_delta') or 0)} apart, outside the allowed range",
        }.get(recon["status"], recon["status"])))
    rows.append(("Owner", f"{owner}, {dataset.ACCOUNT_MANAGER_ROLE}"))
    context_html = "".join(
        f"<div style=\"display:flex;justify-content:space-between;gap:13px;padding:10.5px 14.5px;border-bottom:1px solid #f0f1f7;"
        f"font-size:12.5px;flex-wrap:wrap\"><span style=\"color:#6b7190\">{e(k)}</span><span style=\"text-align:right\">{e(v)}</span></div>"
        for k, v in rows)
    action = next_step(card)
    held_why = why_held(record.get("reason"), context) if not ready else ""
    status_text = (READY_TEXT if card["status"] == "ready" else STATUS_TEXT.get(rec.get("recommended_action"), READY_TEXT)
                   if ready else held_why)
    confidence = (record.get("confidence") or "").capitalize()
    unknown = record.get("unknown_source_refs") or []
    trust = [
        ("Confidence", confidence or "Not given",
         "set by Radar from the data, not by the AI" if confidence else "only given when a suggestion is ready", "lav"),
        ("Data", ("Out of date" if fresh.get("stale") else "Fresh") if "commerce_hours" in fresh else "Not checked",
         f"commerce data {fresh['commerce_hours']:.0f} hours old" if "commerce_hours" in fresh else "the account data wasn’t read",
         ("coral" if fresh.get("stale") else "green") if "commerce_hours" in fresh else "gray"),
        ("Revenue numbers", {"match": "Agree", "probable_match": "Roughly agree", "exception": "Don’t agree"}.get(recon.get("status"), "Not checked"),
         "campaign vs commerce", "yellow" if recon.get("status") == "exception" else "green" if recon.get("status") else "gray"),
        ("Evidence", "Not traceable" if unknown else "Traceable",
         "part of it points to data Radar never read" if unknown else "every source it cites is data Radar read",
         "coral" if unknown else "gray"),
    ]
    trust_html = "".join(
        f"<div style=\"border-radius:16px;background:{T[tt][0]};padding:13px;display:flex;flex-direction:column;gap:3px\">"
        f"<span style=\"font-size:11px;color:#4d5372\">{e(k)}</span><span style=\"font-size:15.5px;font-weight:600;color:{T[tt][2]};"
        f"line-height:1.25\">{e(v)}</span><span style=\"font-size:11px;color:#4d5372;line-height:1.4\">{e(sub)}</span></div>"
        for k, v, sub, tt in trust)
    check_bg, check_ink, check_icon, check_label, check_text = (
        ("#e6f6ee", "#2f7a55", "✓", "Safety checks: passed", GATE_TEXT) if ready
        else ("#fdece6", "#c2462b", "✕", "Safety checks: held back", held_why))
    log = [x for x in receipt.get("tool_log", []) if x.get("account_id") == account_id]
    prov = record.get("cited_source_ref_provenance") or {}
    tech = [f"account {account_id} · check {row['run_id']}"]
    tech += [f"{x['tool']}({x.get('account_id')}) · {x.get('status', 'ok')}"
             + (f" ({x.get('refusal_reason') or x.get('error')})" if x.get("status") not in (None, "ok") else "") for x in log]
    if recon.get("status"):
        tech.append(f"reconcile_revenue → {recon['status']} ({pct(recon.get('relative_delta') or 0)})")
    tech.append(f"recommended_action {rec.get('recommended_action')} · priority {rec.get('priority')}")
    tech.append("gate → PASS · published" if ready else
                f"gate → BLOCK {record.get('stage')}:{record.get('reason')} · {'; '.join(record.get('details') or [])}")
    if ready:
        tech.append(f"confidence {record.get('confidence')} · {record.get('confidence_reason')}")
    tech += [f"cited {r} ← {(p.get('account_id'), p.get('origin')) if isinstance(p := prov.get(r), dict) else p}"
             for r in (record.get("cited_source_refs") or rec.get("source_refs") or [])]
    tech.append("reference check: membership (issued to this account, this run); semantic entailment is not checked")
    tech.append(f"the AI's headline: {rec.get('headline')}")
    tech.append(f"the AI's reasoning: {rec.get('rationale')}")
    if rec.get("acknowledged_tension"):
        tech.append(f"the tension it noted: {rec['acknowledged_tension']}")
    task = next((x for x in log if x["tool"] == "propose_crm_task" and x.get("status") == "ok"), None)
    if task:
        tech.append(f"drafted task (a draft only, nothing created): {(task.get('arguments') or {}).get('title')}")
    tech.append(f"portfolio owner {card['owner']} · CRM account_owner field as read: {(context.get('account') or {}).get('account_owner')}")
    status = card["status"]
    items = evidence(card, receipt, row)
    evidence_html = "".join(evidence_item(i, n) for n, i in enumerate(items))
    found = "".join(f"<span style=\"font-size:12.5px;line-height:1.45;color:#3d4363\">• {e(k)}: {e(v)}</span>"
                    for k, v, _ in facts(entry))
    draft = card["draft"]
    buttons = (
        ("" if status == "none" else
         f"<button type=button class=h-navy data-open=review data-cta=\"{status}\" style=\"height:37px;padding:0 17.5px;"
         f"border-radius:11px;border:none;background:#232a4d;color:#fff;font-size:13px;font-weight:500;cursor:pointer\">"
         f"{e(CTA[status][0])}</button>")
        + "<a href=\"#evidence\" style=\"font-size:12px;text-decoration:none\">View evidence ↓</a>"
        + (text_button("Review draft", "data-open=review data-jump=draft") if draft else "")
        + mark_reviewed(account_id))
    action_card = (
        "<div style=\"border-radius:21px;background:#fff4dc;padding:19px;display:flex;flex-direction:column;gap:8px\">"
        f"<span style=\"font-size:11px;font-weight:600;color:#946300\">"
        f"{'Held back' if not ready else 'Status' if status == 'none' else 'Recommended next step'}</span>"
        f"<span style=\"font-size:18px;font-weight:600;line-height:1.3\">{e(action)}</span>"
        + (f"<div style=\"display:flex;flex-direction:column;gap:2px\"><span style=\"font-size:11px;color:#6b7190\">Radar found</span>"
           f"{found}</div>" if found else "")
        + f"<span style=\"font-size:13px;line-height:1.5;color:#3d4363\">{e(status_text)}</span>"
        + (f"<span style=\"font-size:12px;color:#946300\">Radar drafted a follow-up task (a draft only{', held back with it' if not ready else ''}).</span>"
           if draft else "")
        + f"<div style=\"display:flex;align-items:center;gap:6px 14px;flex-wrap:wrap;margin-top:5px\">{buttons}</div></div>")
    review_html = review_panel(card, status_text, held_why, items)
    section = (
        f"<section data-screen-label=\"Account detail\" style=\"display:flex;flex-direction:column;gap:19px\">{back}"
        + banner(state)
        + f"<div style=\"border-radius:24px;background:{t[0]};padding:22.5px;display:flex;flex-direction:column;gap:11px\">"
        f"<div style=\"display:flex;align-items:center;gap:11px;flex-wrap:wrap\"><div style=\"width:48px;height:48px;border-radius:50%;"
        f"background:{t[1]};color:{t[2]};display:grid;place-items:center;font-size:16px;font-weight:600\">{e(ini(nm))}</div>"
        f"<div style=\"display:flex;flex-direction:column;gap:2px\"><span style=\"font-size:22px;font-weight:600;color:{t[2]};"
        f"line-height:1.15\">{e(nm)}</span><span style=\"font-size:12.5px;color:#4d5372\">Owner: {e(owner)}, Account Manager</span></div>"
        f"<span style=\"margin-left:auto;font-size:11px;font-weight:600;padding:5px 11px;border-radius:799px;background:#ffffff;"
        f"color:{sink}\">{e(label)}</span></div><div style=\"display:flex;flex-direction:column;gap:3px\">"
        f"<span style=\"font-size:18px;font-weight:600;line-height:1.3\">{e(headline(entry))}</span>"
        f"<span style=\"font-size:14px;color:#3d4363;line-height:1.5\">{e(why_it_matters(entry))}</span></div></div>"
        "<div style=\"display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:17.5px;align-items:start\">"
        "<div style=\"display:flex;flex-direction:column;gap:9.5px\"><h2 style=\"margin:0;font-size:15.5px;font-weight:600\">"
        f"Why this account needs attention</h2>{why_html}"
        + (f"<h2 style=\"margin:8px 0 0;font-size:15.5px;font-weight:600\">What changed</h2><div style=\"display:grid;"
           f"grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px\">{metrics}</div>" if metrics else "")
        + "<h2 style=\"margin:8px 0 0;font-size:15.5px;font-weight:600\">Account context</h2><div style=\"border-radius:17.5px;"
        f"border:1px solid #eceef5;display:flex;flex-direction:column;overflow:hidden\">{context_html}</div>"
        "<h2 id=evidence style=\"margin:8px 0 0;font-size:15.5px;font-weight:600;scroll-margin-top:16px\">Evidence used</h2>"
        "<span style=\"font-size:12.5px;color:#6b7190;margin-top:-5px\">What Radar read to reach this. Nothing here is estimated.</span>"
        f"<div style=\"display:flex;flex-direction:column;gap:8px\">{evidence_html}</div></div>"
        "<div style=\"display:flex;flex-direction:column;gap:9.5px\">"
        + action_card + review_html
        + "<h2 style=\"margin:8px 0 0;font-size:15.5px;font-weight:600\">Can I trust this?</h2>"
        f"<div style=\"display:grid;grid-template-columns:1fr 1fr;gap:8px\">{trust_html}</div>"
        "<div style=\"display:flex;flex-direction:column;border-radius:19px;overflow:hidden;border:1px solid #eceef5\">"
        "<div style=\"padding:14.5px;display:flex;gap:11px;align-items:flex-start;background:#ffffff\"><span style=\"width:29px;height:29px;"
        "border-radius:9.5px;background:#eef0fb;color:#3c46a0;display:grid;place-items:center;font-size:11px;font-weight:700;"
        "flex-shrink:0\">AI</span><div style=\"display:flex;flex-direction:column;gap:3px\"><span style=\"font-size:11px;color:#6b7190\">"
        f"The AI suggested</span><span style=\"font-size:13.5px;line-height:1.45\">{e(NEXT_STEP.get(rec.get('recommended_action'), 'something'))}"
        "</span></div></div>"
        f"<div style=\"padding:14.5px;display:flex;gap:11px;align-items:flex-start;background:{check_bg}\"><span style=\"width:29px;height:29px;"
        f"border-radius:9.5px;background:#ffffff;color:{check_ink};display:grid;place-items:center;font-size:14px;font-weight:700;"
        f"flex-shrink:0\">{check_icon}</span><div style=\"display:flex;flex-direction:column;gap:3px\"><span style=\"font-size:11px;"
        f"font-weight:600;color:{check_ink}\">{check_label}</span><span style=\"font-size:13.5px;line-height:1.45\">{e(check_text)}</span>"
        "</div></div></div>"
        + tech_box(tech) + tech_toggle("How Radar reached this result", "Hide how Radar reached this")
        + "</div></div></section>")
    return 200, page(nm, ctx, "today", "Account details", ("", nm), section, row, cards, receipt, here)


# -- 3. MY PORTFOLIO ----------------------------------------------------------------------------
def render_portfolio(ledger: Path, viewer: str = "all") -> tuple[int, str]:
    ctx = {"viewer": viewer}
    row, receipt, state, cards = load(ledger, None)
    if viewer == "all":
        section = (
            "<section data-screen-label=\"Portfolio picker\" style=\"display:flex;flex-direction:column;gap:14.5px\">"
            + title_block("Whose portfolio?", "Use “Viewing as” at the top to pick one, or compare everyone in Team overview.")
            + "<div style=\"display:flex;gap:8px;flex-wrap:wrap\"><button type=button class=h-navy data-toggle=scope style=\"height:37px;"
            "padding:0 16px;border-radius:11px;border:none;background:#232a4d;color:#ffffff;font-size:12.5px;font-weight:500;"
            "cursor:pointer\">Choose an account manager</button>"
            f"<a class=h-bd href=\"{href(ctx, '/team')}\" style=\"height:37px;padding:0 16px;border-radius:11px;border:1px solid #e3e5ef;"
            "background:#ffffff;color:#232a4d;font-size:12.5px;cursor:pointer;display:grid;place-items:center;text-decoration:none\">"
            "Open Team overview</a></div></section>")
        return 200, page("My portfolio", ctx, "portfolio", "Portfolio", ("My ", "portfolio"), section, row, cards, receipt, "/portfolio")
    owner = VIEWERS[viewer]
    greet = ("", dataset.ACCOUNT_MANAGERS[owner])
    if row is None:
        return empty_page(ctx, "My portfolio", "portfolio", "/portfolio")
    pf = portfolio_facts(owner, cards, receipt)
    if receipt is None:
        section = title_block(f"{pf['accounts']} accounts", "What the latest check found in this portfolio.") + (banner(state) or stopped(row))
        return 200, page("My portfolio", ctx, "portfolio", "Account Manager · portfolio", greet, section, row, cards, receipt, "/portfolio")
    stats = [(pf["attention"], "need attention", "coral"), (pf["held"], "held back for safety", "lav"),
             (pf["no_contact"], f"no account-manager contact in {config.ENGAGEMENT_GAP_DAYS}+ days", "yellow"),
             (pf["billing"], "billing risk", "coral"), (pf["data_quality"], "data-quality issues", "cyan"),
             (pf["growth"], "growth opportunities", "green")]
    stats_html = "".join(
        f"<div style=\"border-radius:16px;background:{T[tt][0]};padding:11px 13px;display:flex;flex-direction:column;gap:3px\">"
        f"<span style=\"font-size:22px;font-weight:600;color:{T[tt][2] if v else '#4d5372'};line-height:1.1\">{v}</span>"
        f"<span style=\"font-size:11px;color:#4d5372;line-height:1.35\">{e(label)}</span></div>" for v, label, tt in stats)
    surfaced = "".join(
        f"<a class=h-dim href=\"{href(ctx, '/run/' + quote(row['run_id']) + '/account/' + quote(str(c['account_id'])))}\" "
        f"style=\"border:none;border-radius:19px;background:{T[c['tint']][0]};padding:14.5px 16px;display:grid;grid-template-columns:auto "
        f"minmax(0,1fr) auto;gap:11px;align-items:center;cursor:pointer;text-align:left;color:#232a4d;text-decoration:none\">"
        f"<span style=\"width:38.5px;height:38.5px;border-radius:50%;background:{T[c['tint']][1]};color:{T[c['tint']][2]};display:grid;"
        f"place-items:center;font-size:14px;font-weight:600\">{e(ini(name(c['account_id'])))}</span>"
        f"<span style=\"display:flex;flex-direction:column;gap:2px;min-width:0\"><span style=\"font-size:14px;font-weight:600;"
        f"color:{T[c['tint']][2]}\">{e(name(c['account_id']))}</span><span style=\"font-size:13px\">{e(headline(c['entry']))}</span>"
        f"<span style=\"font-size:11px;color:#4d5372\">Next step: {e(next_step(c))}</span></span>"
        f"<span style=\"font-size:11px;font-weight:600;padding:4px 9.5px;border-radius:799px;background:#ffffff;"
        f"color:{STATUS[c['status']][1]};white-space:nowrap\">{e(STATUS[c['status']][0])}</span></a>" for c in pf["cards"])
    raised = {c["account_id"] for c in pf["cards"]}
    watch = [s for s in pf["listed"] if s["account_id"] not in raised]
    watch_html = "".join(
        f"<div style=\"border-radius:16px;background:#f6f7fb;padding:11px 14.5px;display:flex;justify-content:space-between;gap:9.5px;"
        f"flex-wrap:wrap;align-items:center\"><span style=\"font-size:13px\"><strong style=\"font-weight:600\">{e(name(s['account_id']))}"
        f"</strong> · {e(headline(s))}</span><span style=\"font-size:11.5px;color:#4d5372\">"
        f"{e(' · '.join(k + ': ' + v for k, v, _ in facts(s)[:2]))}</span></div>" for s in watch)
    listed_ids = {s["account_id"] for s in pf["listed"]}
    quiet = [name(a) for a in dataset.PORTFOLIOS[owner] if a not in listed_ids and a not in raised]
    quiet_rows = "".join(
        f"<div data-quiet=\"{e(q.lower())}\" style=\"display:flex;justify-content:space-between;gap:8px;padding:8px 13px;"
        f"border-bottom:1px solid #f3f4f9;font-size:12.5px\"><span>{e(q)}</span><span style=\"color:#8a90a8;font-size:11px\">"
        "Checked · nothing raised</span></div>" for q in quiet)
    section = (
        "<section data-screen-label=\"My portfolio\" style=\"display:flex;flex-direction:column;gap:21px\">" + banner(state)
        + title_block(f"{pf['accounts']} accounts", "What the latest check found in this portfolio.")
        + f"<div style=\"display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));gap:8px\">{stats_html}</div>"
        "<div style=\"display:flex;flex-direction:column;gap:9.5px\"><span style=\"font-size:14.5px;font-weight:600\">Needs attention now</span>"
        + (surfaced or "<span style=\"font-size:12.5px;color:#6b7190\">Nothing in this portfolio needs attention today.</span>") + "</div>"
        + ("<div style=\"display:flex;flex-direction:column;gap:8px\"><div style=\"display:flex;flex-direction:column;gap:2px\">"
           "<span style=\"font-size:14.5px;font-weight:600\">Worth watching</span><span style=\"font-size:12.5px;color:#6b7190\">These made "
           f"the shortlist, but Radar didn't raise them this time.</span></div>{watch_html}</div>" if watch else "")
        + "<div style=\"display:flex;flex-direction:column;gap:8px\"><div style=\"display:flex;flex-direction:column;gap:2px\">"
        "<span style=\"font-size:14.5px;font-weight:600\">Nothing raised</span><span style=\"font-size:12.5px;color:#6b7190\">Checked. "
        "No signal strong enough to make the list.</span></div>"
        "<div data-quiet-list hidden style=\"display:flex;flex-direction:column;gap:6.5px\"><input data-filter=quiet "
        "placeholder=\"Find a quiet account\" aria-label=\"Find a quiet account\" style=\"height:34px;border-radius:9.5px;border:1px solid "
        "#e3e5ef;padding:0 11px;font-family:inherit;font-size:12.5px;color:#232a4d;outline:none;background:#f6f7fb\">"
        f"<div style=\"border-radius:14.5px;border:1px solid #eceef5;max-height:208px;overflow:auto\">{quiet_rows}</div></div>"
        f"<button type=button class=h-bd data-quiet-toggle data-show=\"Show {len(quiet)} quiet accounts\" data-hide=\"Hide quiet accounts\" "
        "style=\"align-self:flex-start;height:34px;padding:0 13px;border-radius:9.5px;border:1px solid #e3e5ef;background:#ffffff;"
        f"color:#232a4d;font-size:11.5px;cursor:pointer\">Show {len(quiet)} quiet accounts</button></div></section>")
    return 200, page("My portfolio", ctx, "portfolio", "Account Manager · portfolio", greet, section, row, cards, receipt, "/portfolio")


# -- 4. TEAM OVERVIEW ---------------------------------------------------------------------------
def render_team(ledger: Path, viewer: str = "all") -> tuple[int, str]:
    ctx = {"viewer": viewer}
    row, receipt, state, cards = load(ledger, None)
    if row is None:
        return empty_page(ctx, "Team overview", "team", "/team")
    grid = "minmax(152px,1.6fr) 72px 104px 80px minmax(160px,2fr) 152px"
    rows = ""
    for slug, owner in sorted(VIEWERS.items(), key=lambda kv: dataset.ACCOUNT_MANAGERS[kv[1]]):
        pf = portfolio_facts(owner, cards, receipt)
        nm = dataset.ACCOUNT_MANAGERS[owner]
        t = T[AM_TINT[owner]]
        chips = "".join(f"<span style=\"font-size:11px;padding:3px 7px;border-radius:799px;background:#f3f4f9;color:#3d4363;"
                        f"white-space:nowrap\">{e(s)}</span>" for s in pf["issues"])
        rows += (
            f"<div data-am data-name=\"{e(nm)}\" data-attn=\"{pf['attention']}\" data-accounts=\"{pf['accounts']}\" style=\"display:grid;"
            f"grid-template-columns:{grid};gap:11px;padding:9.5px 14.5px;border-top:1px solid #f0f1f7;align-items:center\">"
            f"<span style=\"display:flex;align-items:center;gap:8px\"><span style=\"width:27px;height:27px;border-radius:50%;"
            f"background:{t[1]};color:{t[2]};display:grid;place-items:center;font-size:11px;font-weight:600;flex-shrink:0\">{e(ini(nm))}"
            f"</span><span style=\"font-size:13px;font-weight:600\">{e(nm)}</span></span>"
            f"<span style=\"font-size:13px\">{pf['accounts']}</span>"
            f"<span style=\"justify-self:start;min-width:24px;height:21px;padding:0 8px;border-radius:10.5px;"
            f"background:{T['coral'][0] if pf['attention'] else T['gray'][0]};color:{T['coral'][2] if pf['attention'] else '#6b7190'};"
            f"font-size:12.5px;font-weight:600;display:grid;place-items:center\">{pf['attention']}</span>"
            f"<span style=\"font-size:13px\">{len(pf['listed'])}</span>"
            f"<span style=\"display:flex;gap:3px;flex-wrap:wrap\">{chips}</span>"
            "<span style=\"display:flex;gap:5px;justify-content:flex-end\">"
            f"<a class=h-bd href=\"{href(ctx, '/portfolio', slug)}\" style=\"height:34px;padding:0 9.5px;border-radius:8px;border:1px solid "
            "#e3e5ef;background:#ffffff;color:#232a4d;font-size:11px;cursor:pointer;white-space:nowrap;display:grid;place-items:center;"
            "text-decoration:none\">Portfolio</a>"
            f"<a class=h-navy href=\"{href(ctx, '/', slug)}\" style=\"height:34px;padding:0 9.5px;border-radius:8px;border:none;"
            "background:#232a4d;color:#ffffff;font-size:11px;cursor:pointer;white-space:nowrap;display:grid;place-items:center;"
            "text-decoration:none\">Brief</a></span></div>")
    n = len(cards)
    total = len(NAMES)
    managers = len(dataset.ACCOUNT_MANAGERS)
    section = (
        "<section data-screen-label=\"Team overview\" style=\"display:flex;flex-direction:column;gap:17.5px\">" + banner(state)
        + title_block(f"{n} account{'s' if n != 1 else ''} across {managers} portfolios need{'s' if n == 1 else ''} attention today",
                      f"{total} accounts in all. These numbers describe the accounts, not how well each manager is doing.")
        + "<div style=\"display:flex;gap:8px;flex-wrap:wrap\"><input data-filter=am placeholder=\"Search account managers\" "
        "aria-label=\"Search account managers\" style=\"flex:1 1 192px;height:35px;border-radius:11px;border:1px solid #e3e5ef;"
        "padding:0 13px;font-family:inherit;font-size:12.5px;color:#232a4d;outline:none;background:#f6f7fb\">"
        "<select data-sort=am aria-label=Sort style=\"height:35px;border-radius:11px;border:1px solid #e3e5ef;padding:0 9.5px;"
        "font-family:inherit;font-size:12.5px;color:#232a4d;background:#ffffff\"><option value=name>Sort: name A–Z</option>"
        "<option value=attn>Sort: most needing attention</option><option value=accounts>Sort: most accounts</option></select></div>"
        "<div style=\"border-radius:17.5px;border:1px solid #eceef5;overflow-x:auto\"><div style=\"min-width:656px\">"
        f"<div style=\"display:grid;grid-template-columns:{grid};gap:11px;padding:9px 14.5px;background:#f6f7fb;font-size:11px;"
        "font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:#6b7190\"><span>Account manager</span><span>Accounts</span>"
        "<span>Need attention</span><span>Shortlisted</span><span>Today's signals</span><span></span></div>"
        f"<div data-ams>{rows}</div><div data-empty=am hidden style=\"padding:19px 14.5px;border-top:1px solid #f0f1f7;font-size:12.5px;"
        "color:#6b7190;text-align:center\">No one matches that name.</div></div></div>"
        f"<span data-am-foot data-total=\"{managers}\" style=\"font-size:11px;color:#6b7190\">{managers} of {managers} account managers. "
        "Numbers describe the accounts, not how well each manager is doing.</span></section>")
    return 200, page("Team overview", ctx, "team", "All account managers", ("Team ", "overview"), section, row, cards, receipt, "/team")


# -- 5. CONTROL ROOM ----------------------------------------------------------------------------
def render_control(ledger: Path, run_id: str | None, viewer: str = "all") -> tuple[int, str]:
    ctx = {"viewer": viewer}
    row, receipt, state, cards = load(ledger, run_id)
    here = f"/run/{quote(run_id)}/control" if run_id else "/control"
    if row is None:
        if run_id:
            return 404, page("Control room", ctx, "control", "Control room", ("Control ", "room"), "", None, [], None, here)
        return empty_page(ctx, "Control room", "control", here)
    problems = json.loads(row["problems"] or "[]")
    warnings = json.loads(row.get("warnings") or "[]")
    retries = row["retry_count"] or 0
    held = sum(1 for c in cards if c["decision"] == "held")
    surfaced = len(cards) - held
    at = when(row["started_at"])
    title = {"ok": "This check completed normally", "partial": "This check needs a review",
             "failed": "This check stopped safely"}.get(row["status"], "This check ran")
    sub = (f"{at}. {row['accounts_scanned']} accounts scanned, {row['accounts_shortlisted']} shortlisted, {surfaced} surfaced, "
           f"{held} held back." if row["accounts_scanned"] is not None else f"{at}. It stopped before producing results.")
    monitoring = json.loads(row["monitoring"] or "{}")
    steps = []
    if row["accounts_scanned"] is not None:
        steps.append(("Checked the whole portfolio", f"{row['accounts_scanned']} accounts, using fixed rules.",
                      f"scan: {row['accounts_scanned']} accounts · deterministic", "green"))
        steps.append(("Picked accounts for a closer look",
                      f"The {row['accounts_shortlisted']} with the strongest signals went to the AI.",
                      f"shortlist cap {config.MAX_SHORTLIST} · {row['accounts_shortlisted']} supplied", "green"))
    if receipt:
        m = receipt.get("tool_metrics", {})
        drafts = m.get("proposal_tool_calls_ok", 0)
        text = (f"The AI read {m.get('read_tool_calls_ok', 0)} pieces of account data. It drafted {drafts} follow-up "
                f"task{'s' if drafts != 1 else ''} (drafts only).")
        unknown = sorted({x["tool"] for x in receipt.get("tool_log", []) if x["tool"] not in REGISTERED_TOOLS})
        if m.get("refused_tool_calls"):
            text += f" {m['refused_tool_calls']} request(s) were refused."
        if unknown:
            text += f" It also asked for something it isn’t allowed to do ({', '.join(capability(u) for u in unknown)}). Nothing ran."
        steps.append(("Gathered more context", text,
                      f"{m.get('read_tool_calls', 0)} read-only tool calls · {m.get('proposal_tool_calls', 0)} drafts · "
                      f"{m.get('refused_tool_calls', 0)} refused · {m.get('failed_tool_calls', 0)} failed"
                      + (f" · unknown: {', '.join(unknown)}" if unknown else ""), "yellow" if unknown else "green"))
    if retries and receipt is not None:
        steps.append(("The AI provider was slow", f"Radar retried {retries} time{'s' if retries != 1 else ''}, then got an answer.",
                      f"model retries {retries}", "yellow"))
    for p in problems + warnings:
        sentence = problem_sentence(p, retries)
        if sentence and not sentence.startswith("The AI asked for something"):
            steps.append(("The AI provider didn’t answer" if p.startswith("model_error") else "Worth knowing", sentence, p,
                          "coral" if row["status"] == "failed" else "yellow"))
    if receipt is not None:
        steps.append(("Safety checks", f"{surfaced} ready for review, {held} held back.",
                      f"gate: {surfaced} PASS · {held} BLOCK", "coral" if held else "lav"))
        repeated = row["publications_skipped_duplicate"] or 0
        steps.append(("Added to the morning brief",
                      f"{row['publications_new'] or 0} new."
                      + (f" The {repeated} account{'s were' if repeated != 1 else ' was'} already raised earlier, so "
                         f"{'they weren’t' if repeated != 1 else 'it wasn’t'} repeated." if repeated else ""),
                      f"published {row['publications_new'] or 0} · dedup {repeated}", "blue"))
    else:
        steps.append(("Nothing became actionable", "The check stopped before producing results.", "no receipt", "coral"))
    steps.append(("Monitoring", monitoring_sentence(monitoring),
                  ", ".join(f"{k} {v}" for k, v in monitoring.items()) or "not recorded", "gray"))
    timeline = "".join(
        f"<div style=\"border-radius:14.5px;background:{T[tt][0]};padding:11px 13px;display:grid;grid-template-columns:auto minmax(0,1fr);"
        f"gap:9.5px;align-items:start\"><span style=\"width:27px;height:27px;border-radius:50%;background:{T[tt][1]};color:{T[tt][2]};"
        f"display:grid;place-items:center;font-size:12.5px;font-weight:700\">{i}</span><div style=\"display:flex;flex-direction:column;gap:3px\">"
        f"<span style=\"font-size:13px;font-weight:600;color:{T[tt][2]}\">{e(ttl)}</span><span style=\"font-size:12.5px;color:#3d4363;"
        f"line-height:1.5\">{e(detail)}</span><span data-tech hidden style=\"font-family:ui-monospace,Menlo,monospace;font-size:11px;"
        f"color:#6b7190\">{e(tech)}</span></div></div>" for i, (ttl, detail, tech, tt) in enumerate(steps, 1))
    gates = "".join(
        f"<a class=h-gate href=\"{href(ctx, '/run/' + quote(row['run_id']) + '/account/' + quote(str(c['account_id'])))}\" "
        "style=\"border:1px solid #eceef5;border-radius:16px;background:#ffffff;padding:11px 13px;text-align:left;display:flex;"
        "flex-direction:column;gap:8px;cursor:pointer;color:#232a4d;text-decoration:none\"><div style=\"display:flex;"
        f"justify-content:space-between;align-items:center;gap:8px\"><span style=\"font-size:12.5px;font-weight:600\">{e(name(c['account_id']))}"
        f"</span><span style=\"font-size:11px;font-weight:600;padding:3px 8px;border-radius:799px;background:{STATUS[c['status']][2]};"
        f"color:{STATUS[c['status']][1]};white-space:nowrap\">{e(STATUS[c['status']][0])}</span></div>"
        "<div style=\"display:grid;grid-template-columns:auto minmax(0,1fr);gap:5px 8px;font-size:11.5px;line-height:1.45\">"
        f"<span style=\"color:#6b7190\">AI</span><span>{e(NEXT_STEP.get(c['rec'].get('recommended_action'), 'something'))}</span>"
        "<span style=\"color:#6b7190\">Checks</span>"
        + ("<span style=\"color:#2f7a55;font-weight:500\">Passed</span>" if c["decision"] == "ready" else
           f"<span style=\"color:#c2462b;font-weight:500\">Held back: {e(why_held(c['record'].get('reason'), c['context']))}</span>")
        + "</div></a>" for c in cards)
    skipped = (receipt or {}).get("unattended", {}).get("publications_skipped_duplicate", [])
    dupes = ""
    if skipped:
        prior = unattended._sql(ledger, f"SELECT account_id, action, published_at FROM publications WHERE idempotency_key IN "
                                        f"({','.join('?' * len(skipped))}) ORDER BY published_at, rowid", tuple(skipped))
        dupes = ("<div style=\"border-radius:16px;background:#f6f7fb;padding:13px;display:flex;flex-direction:column;gap:6.5px\">"
                 "<span style=\"font-size:11px;font-weight:600;color:#4d5372\">Already raised earlier, so not repeated</span>"
                 + "".join(f"<div style=\"display:flex;justify-content:space-between;gap:8px;font-size:12.5px;flex-wrap:wrap\"><span>"
                           f"{e(name(p['account_id']))} · {e(NEXT_STEP.get(p['action'], ''))}</span><span style=\"color:#6b7190\">"
                           f"{e(when(p['published_at']))}</span></div>" for p in prior) + "</div>")
    right = (("<span style=\"font-size:14.5px;font-weight:600\">What the AI suggested, and what the safety checks said</span>"
              + gates + dupes) if receipt else
             ("<span style=\"font-size:14.5px;font-weight:600\">What the AI suggested</span><span style=\"font-size:12.5px;color:#4d5372;"
              "line-height:1.5\">Nothing: the AI never returned suggestions in this check, so there was nothing to check.</span>"))
    section = (
        "<section data-screen-label=\"Control room\" style=\"display:flex;flex-direction:column;gap:21px\">" + banner(state)
        + title_block(title, sub)
        + "<div style=\"display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:24px;align-items:start\">"
        "<div style=\"display:flex;flex-direction:column;gap:9.5px\"><span style=\"font-size:14.5px;font-weight:600\">What happened, in order"
        f"</span>{timeline}" + forensics(row, receipt, state, ctx) + tech_toggle("Show technical details", "Hide technical details")
        + f"</div><div style=\"display:flex;flex-direction:column;gap:9.5px\">{right}</div></div></section>")
    return 200, page("Control room", ctx, "control", f"How the check ran · {at}", ("Control ", "room"), section, row, cards, receipt, here)


def forensics(row: dict, receipt: dict | None, state: str, ctx: dict) -> str:
    """Everything the check recorded, complete, in V4's technical panel."""
    usage = (receipt or {}).get("usage", {})
    u = (receipt or {}).get("unattended", {})
    cost = row["actual_cost_usd"]
    model = (receipt or {}).get("model") or ("scripted (offline)" if row["mode"] != "live" else None)
    lines = [
        f"run_id {row['run_id']}", f"status {row['status']} · mode {row['mode']} · started {row['started_at']} · "
        f"duration {row['duration_s']} s", f"model {model} · tokens in/out {usage.get('input_tokens', '-')}/{usage.get('output_tokens', '-')}"
        f" · cost {'$%.4f' % cost if cost is not None else '-'} · retries {row['retry_count']}",
        f"recommendations accepted/blocked {row['recommendations_published']}/{row['recommendations_blocked']} · "
        f"publications new/duplicates skipped {row['publications_new']}/{row['publications_skipped_duplicate']}",
        f"receipt {row['forensic_report_path']}", f"receipt sha256 {row['forensic_report_sha256']}",
        "receipt verification: " + {"verified": "receipt verified: SHA-256 matches the ledger", "mismatch": "HASH MISMATCH",
                                     "missing": "RECEIPT MISSING", "outside": "path outside the ledger folder",
                                     "none": f"no receipt ({FAILED_BEFORE_RESULT})"}[state],
        f"system prompt sha256 {(receipt or {}).get('prompt', {}).get('system_prompt_sha256')}",
        f"tool definitions sha256 {(receipt or {}).get('prompt', {}).get('tool_definitions_sha256')}",
        f"problems (raw) {row['problems']}", f"warnings (raw) {row.get('warnings')}", f"monitoring (raw) {row['monitoring']}",
        f"invariant violations {u.get('invariant_violations') or 'none'}",
    ]
    lines += [f"new publication key {k}" for k in u.get("publications_new", [])]
    lines += [f"skipped duplicate key {k}" for k in u.get("publications_skipped_duplicate", [])]
    if receipt:
        lines += [f"gate rejection {r['stage']}:{r['reason']} ({r['account_id']}) · {'; '.join(r['details'])}"
                  + (f" · unknown refs {', '.join(r['unknown_source_refs'])}" if r.get("unknown_source_refs") else "")
                  for r in receipt.get("rejections", [])] or ["gate rejections none"]
        for x in receipt.get("tool_log", []):
            outcome = x.get("status", "ok") + (f" ({x.get('refusal_reason') or x.get('error')})" if x.get("status") not in (None, "ok") else "")
            flag = "" if x["tool"] in REGISTERED_TOOLS else " [not a registered tool]"
            lines.append(f"tool #{x.get('seq')} {x['tool']}{flag} · {x.get('account_id')} · {outcome} · args "
                         f"{json.dumps(redact(x.get('arguments') or {}))} · refs {', '.join(x.get('source_refs_returned') or []) or '-'}")
        lines += [f"source ref {ref} ← {p.get('account_id')} · {p.get('origin')}"
                  for ref, p in receipt.get("source_ref_provenance", {}).items()]
    return (tech_box(lines)[:-6]
            + f"<a href=\"{href(ctx, '/reviews')}\" style=\"color:#9fc3ee;margin-top:6.5px\">Experimental: human review prototype → "
              "(approval records a decision only; nothing is applied to a CRM)</a></div>")


# -- 6. DATA SOURCES ----------------------------------------------------------------------------
def render_sources(ledger: Path, viewer: str = "all") -> tuple[int, str]:
    ctx = {"viewer": viewer}
    row, receipt, state, cards = load(ledger, None)
    used = "".join(
        f"<div style=\"border-radius:17.5px;background:{T[tt][0]};padding:14.5px;display:flex;flex-direction:column;gap:8px\">"
        f"<span style=\"width:32px;height:32px;border-radius:10.5px;background:#ffffff;color:{T[tt][2]};display:grid;place-items:center;"
        f"font-size:14px;font-weight:700\">{letter}</span><span style=\"font-size:14px;font-weight:600;color:{T[tt][2]}\">{nm}</span>"
        "<span style=\"align-self:flex-start;font-size:11px;font-weight:600;padding:3px 8px;border-radius:799px;background:#ffffff;"
        "color:#4d5372\">Synthetic demo data</span></div>"
        for nm, letter, tt in (("CRM activity", "C", "yellow"), ("Commerce", "$", "cyan"), ("Campaign performance", "%", "lav"),
                               ("Billing", "B", "coral")))
    adapters = ""
    for (vendor, envs, reads), tt in zip(VENDORS, ("yellow", "cyan", "green")):
        configured = all(os.getenv(n) for n in envs)
        state_text = "Credentials set, not tested here" if configured else "Not connected"
        adapters += (
            "<div style=\"border-radius:17.5px;border:1px solid #eceef5;padding:13px 14.5px;display:grid;grid-template-columns:auto "
            "minmax(0,1fr) auto;gap:11px;align-items:center\">"
            f"<span style=\"width:33.5px;height:33.5px;border-radius:11px;background:{T[tt][0]};color:{T[tt][2]};display:grid;"
            f"place-items:center;font-size:14.5px;font-weight:700\">{vendor[0]}</span><span style=\"display:flex;flex-direction:column;gap:2px;"
            f"min-width:0\"><span style=\"font-size:14px;font-weight:600\">{e(vendor)}</span><span style=\"font-size:11.5px;"
            f"color:#4d5372\">Reads {e(reads)}</span></span><span style=\"display:flex;flex-direction:column;align-items:flex-end;gap:3px\">"
            "<span style=\"font-size:11px;font-weight:600;padding:3px 8px;border-radius:799px;background:#e6f6ee;color:#2f7a55;"
            "white-space:nowrap\">Adapter ready</span>"
            f"<span style=\"font-size:11px;color:{'#4d5372' if configured else '#c2462b'};white-space:nowrap\">{state_text}</span></span></div>")
    section = (
        "<section data-screen-label=\"Data sources\" style=\"display:flex;flex-direction:column;gap:19px\">"
        + title_block("Everything Radar checked today is synthetic demo data",
                      "It's kept apart from the live connections so test results repeat exactly. If a result changes, Radar "
                      "changed, not the data.", max_width=True)
        + "<div style=\"display:flex;flex-direction:column;gap:9.5px\"><span style=\"font-size:14.5px;font-weight:600\">Data used today</span>"
        f"<div style=\"display:grid;grid-template-columns:repeat(auto-fit,minmax(136px,1fr));gap:9.5px\">{used}</div></div>"
        "<div style=\"display:flex;flex-direction:column;gap:9.5px\"><div style=\"display:flex;flex-direction:column;gap:2px\">"
        "<span style=\"font-size:14.5px;font-weight:600\">Real-system connections</span><span style=\"font-size:12.5px;color:#6b7190\">"
        f"They can only read, and they aren't used for today's results yet.</span></div>{adapters}</div></section>")
    return 200, page("Data sources", ctx, "sources", "Where today’s evidence comes from", ("Data ", "sources"), section,
                     row, cards, receipt, "/sources")


# -- 7. HISTORY ---------------------------------------------------------------------------------
def history_story(row: dict, ledger: Path) -> str:
    """V4's one-line story of a check, from its ledger row and verified receipt."""
    retries = row["retry_count"] or 0
    problems = json.loads(row["problems"] or "[]")
    if row["accounts_scanned"] is None:
        return next((s for p in problems if (s := problem_sentence(p, retries))), "It stopped before producing results.")
    receipt, _ = load_receipt(row, ledger)
    cards = brief(receipt) if receipt else []
    held = [c for c in cards if c["decision"] == "held"]
    text = f"{row['accounts_scanned']} accounts checked. {len(cards) - len(held)} surfaced."
    if not held:
        text += " Nothing held back." if not any(p.startswith("forbidden_tool_attempt") for p in problems) else ""
    elif len(held) == 1:
        c = held[0]
        text += f" {name(c['account_id'])} was held back: {held_clause(c['record'].get('reason'), c['context'])}."
    else:
        text += f" {len(held)} were held back: {held_clause(held[0]['record'].get('reason'), held[0]['context'])}."
    extra = [s for p in problems if (s := problem_sentence(p, retries))]
    return " ".join([text] + extra)


def render_history(ledger: Path, viewer: str = "all") -> tuple[int, str]:
    ctx = {"viewer": viewer}
    rows = unattended.recent(ledger, limit=50)
    latest = rows[0] if rows else None
    receipt, _ = load_receipt(latest, ledger) if latest else (None, "none")
    cards = brief(receipt) if receipt else []
    if not rows:
        return empty_page(ctx, "History", "history", "/history")
    day = latest["started_at"][:10]
    today = [r for r in rows if r["started_at"][:10] == day]
    counts = {k: sum(1 for r in today if r["status"] == k) for k in RUN}
    title = f"{len(today)} check{'s' if len(today) != 1 else ''} today" if len(today) == len(rows) else f"{len(rows)} checks on record"
    sub = (f"{counts['ok']} completed normally, {counts['partial']} need{'s' if counts['partial'] == 1 else ''} review, "
           f"{counts['failed']} stopped safely. A check that never started won't show here; the uptime monitor reports those.")
    runs = ""
    for r in rows:
        label, _, tt, icon = RUN.get(r["status"], (r["status"], "", "gray", "•"))
        runs += (
            f"<a href=\"{href(ctx, '/run/' + quote(r['run_id']) + '/control')}\" style=\"border-radius:16px;background:{T[tt][0]};"
            "padding:11px 14.5px;display:grid;grid-template-columns:auto minmax(0,1fr);gap:11px;align-items:start;color:#232a4d;"
            f"text-decoration:none\"><span style=\"width:29px;height:29px;border-radius:50%;background:#ffffff;color:{T[tt][2]};display:grid;"
            f"place-items:center;font-size:13px;font-weight:700;margin-top:2px\">{icon}</span><div style=\"display:flex;flex-direction:column;"
            "gap:3px;min-width:0\"><div style=\"display:flex;align-items:center;gap:8px;flex-wrap:wrap\"><span style=\"font-size:13px;"
            f"font-weight:600\">{e(when(r['started_at']))}</span><span style=\"font-size:11px;font-weight:600;padding:3px 8px;"
            f"border-radius:799px;background:#ffffff;color:{T[tt][2]}\">{e(label)}</span></div><span style=\"font-size:12.5px;color:#3d4363;"
            f"line-height:1.5\">{e(history_story(r, ledger))}</span></div></a>")
    tech = [f"{r['started_at'][:19]} · {r['run_id']} · {r['mode']} · {r['status']} · accepted {r['recommendations_published']} · "
            f"blocked {r['recommendations_blocked']} · new {r['publications_new']} · duplicates skipped {r['publications_skipped_duplicate']}"
            f" · retries {r['retry_count']} · cost {'$%.4f' % r['actual_cost_usd'] if r['actual_cost_usd'] is not None else '-'} · "
            f"{(json.loads(r['problems'] or '[]') or ['-'])[-1]}" for r in rows]
    section = (
        "<section data-screen-label=History style=\"display:flex;flex-direction:column;gap:17.5px\">" + title_block(title, sub, max_width=True)
        + f"<div style=\"display:flex;flex-direction:column;gap:8px\">{runs}</div>"
        + tech_box(tech) + tech_toggle("Show technical details", "Hide technical details") + "</section>")
    return 200, page("History", ctx, "history", "Every check, on the record", ("", "History"), section, latest, cards, receipt, "/history")


# -- HUMAN REVIEW PROTOTYPE (not in the navigation) -------------------------------------------------
def render_reviews(ledger: Path, big: bool = False, viewer: str = "all") -> tuple[int, str]:
    """Review status, read-only. Decisions are made with `python -m revenue_agent.review`."""
    ctx = {"viewer": viewer}
    row, receipt, state, cards = load(ledger, None)
    in_force = {}
    if unattended._sql(ledger, "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reviews'"):
        in_force = {r["idempotency_key"]: r for r in unattended._sql(
            ledger, "SELECT * FROM reviews r WHERE NOT EXISTS (SELECT 1 FROM reviews s WHERE s.supersedes = r.review_id)")}
    items = ""
    for r in unattended._sql(ledger, "SELECT idempotency_key, account_id, action, published_at FROM publications ORDER BY published_at, rowid"):
        if viewer != "all" and owner_of(r["account_id"]) != VIEWERS[viewer]:
            continue
        review = in_force.get(r["idempotency_key"]) or {}
        label, tt = {"approved": ("Approved - not applied", "green"), "rejected": ("Rejected", "coral")}.get(
            review.get("decision"), ("Awaiting review", "gray"))
        detail = f"Raised {when(r['published_at'])}"
        if review:
            detail += f" · decided by {review.get('reviewer')}" + (f": {review.get('note')}" if review.get("note") else "")
        if review.get("decision") == "approved":
            detail += f" · {NOT_APPLIED}"
        items += (f"<div style=\"border-radius:16px;background:#f6f7fb;padding:11px 14.5px;display:flex;justify-content:space-between;gap:9.5px;"
                  f"flex-wrap:wrap;align-items:center\"><span style=\"display:flex;flex-direction:column;gap:2px\"><span style=\"font-size:13px\">"
                  f"<strong style=\"font-weight:600\">{e(name(r['account_id']))}</strong> · {e(NEXT_STEP.get(r['action'], ''))}</span>"
                  f"<span style=\"font-size:11px;color:#4d5372\">{e(detail)}</span></span><span style=\"font-size:11px;font-weight:600;"
                  f"padding:3px 8px;border-radius:799px;background:#ffffff;color:{T[tt][2]};white-space:nowrap\">{e(label)}</span></div>")
    section = (
        "<section data-screen-label=\"Human review prototype\" style=\"display:flex;flex-direction:column;gap:17.5px\">"
        f"<a class=h-back href=\"{href(ctx, '/control')}\" style=\"align-self:flex-start;border:none;background:#f3f4f9;border-radius:799px;"
        "padding:6.5px 13px;font-size:12.5px;color:#4d5372;cursor:pointer;text-decoration:none\">← Back to the control room</a>"
        + title_block("Approval records a decision only. Nothing is applied to a CRM.",
                      "No CRM applier exists, by design. Decisions are recorded from the command line; this page only shows them.")
        + f"<div style=\"display:flex;flex-direction:column;gap:8px\">{items or 'No raised accounts yet.'}</div></section>")
    return 200, page("Human review prototype", ctx, "control", "Experimental", ("Human review ", "prototype"), section,
                     row, cards, receipt, "/reviews")


# -- the interactions V4 has: reveal, filter, sort, mark as seen (browser tab only) -----------------------
SCRIPT = r"""(function(){
var D=JSON.parse(document.body.getAttribute('data-radar')||'{}');
function key(a){return 'radar-reviewed:'+D.run+':'+a}
function isSeen(a){try{return sessionStorage.getItem(key(a))==='1'}catch(x){return false}}
function setSeen(a,v){try{if(v)sessionStorage.setItem(key(a),'1');else sessionStorage.removeItem(key(a))}catch(x){}}
function $$(s,r){return Array.prototype.slice.call((r||document).querySelectorAll(s))}
function refreshSeen(){
  var unseen=(D.mine||[]).filter(function(a){return !isSeen(a)}).length;
  $$('[data-unseen]').forEach(function(el){el.textContent=unseen;if(el.getAttribute('data-unseen')==='badge')el.hidden=!unseen});
  $$('[data-seen-for]').forEach(function(b){var s=isSeen(b.getAttribute('data-seen-for'));b.textContent=b.getAttribute(s?'data-on':'data-off');
    var r=b.closest('[data-row]');if(r)r.style.opacity=s?0.5:1});
}
var F={q:'',status:'all',signal:'all',sort:'rank'};
function applyRows(){
  var box=document.querySelector('[data-rows]');if(!box)return;
  var rows=$$('[data-row]',box),base=rows.filter(function(r){return !F.q||r.getAttribute('data-search').indexOf(F.q)>=0});
  var bySig=base.filter(function(r){return F.signal==='all'||r.getAttribute('data-signals').split('|').indexOf(F.signal)>=0});
  var shown=bySig.filter(function(r){return F.status==='all'||r.getAttribute('data-status')===F.status});
  rows.forEach(function(r){r.hidden=shown.indexOf(r)<0});
  $$('[data-tab]').forEach(function(b){var k=b.getAttribute('data-tab'),on=F.status===k;
    b.querySelector('[data-n]').textContent=k==='all'?bySig.length:bySig.filter(function(r){return r.getAttribute('data-status')===k}).length;
    b.style.background=on?'#ffffff':'transparent';b.style.color=on?'#232a4d':'#6b7190';b.style.boxShadow=on?'0 2px 6.5px -3px rgba(35,42,77,.3)':'none'});
  $$('[data-chip]').forEach(function(b){var k=b.getAttribute('data-chip'),on=F.signal===k;
    b.querySelector('[data-n]').textContent=k==='all'?base.length:base.filter(function(r){return r.getAttribute('data-signals').split('|').indexOf(k)>=0}).length;
    b.style.background=on?'#232a4d':'#ffffff';b.style.color=on?'#ffffff':'#4d5372';b.style.borderColor=on?'#232a4d':'#e3e5ef'});
  rows.slice().sort(function(a,b){var x=F.sort==='rank'?(+a.getAttribute('data-rank'))-(+b.getAttribute('data-rank')):
    a.getAttribute('data-'+F.sort).localeCompare(b.getAttribute('data-'+F.sort));return x}).forEach(function(r){box.appendChild(r)});
  var foot=document.querySelector('[data-foot]');
  if(foot)foot.textContent='Showing '+shown.length+' of '+foot.getAttribute('data-total')+' surfaced · '+foot.getAttribute('data-checked')+' accounts checked · the rest had nothing to raise';
  var empty=document.querySelector('[data-empty=rows]');if(empty)empty.hidden=shown.length>0;
}
function applyTeam(q,sort){
  var box=document.querySelector('[data-ams]');if(!box)return;var rows=$$('[data-am]',box);
  rows.forEach(function(r){r.hidden=!!q&&r.getAttribute('data-name').toLowerCase().indexOf(q)<0});
  rows.slice().sort(function(a,b){if(sort==='attn')return (+b.getAttribute('data-attn'))-(+a.getAttribute('data-attn'))||a.getAttribute('data-name').localeCompare(b.getAttribute('data-name'));
    if(sort==='accounts')return (+b.getAttribute('data-accounts'))-(+a.getAttribute('data-accounts'));return a.getAttribute('data-name').localeCompare(b.getAttribute('data-name'))}).forEach(function(r){box.appendChild(r)});
  var n=rows.filter(function(r){return !r.hidden}).length,foot=document.querySelector('[data-am-foot]');
  if(foot)foot.textContent=n+' of '+foot.getAttribute('data-total')+' account managers. Numbers describe the accounts, not how well each manager is doing.';
  var empty=document.querySelector('[data-empty=am]');if(empty)empty.hidden=n>0;
}
var teamQ='',teamSort='name';
function openPanel(id,jump){var p=document.getElementById(id);if(!p)return;p.hidden=false;var j=jump&&document.getElementById(jump);(j||p).scrollIntoView({block:'start'})}
document.addEventListener('click',function(ev){
  if(ev.target.closest('a'))return;
  var t=ev.target.closest('[data-toggle],[data-seen-for],[data-tab],[data-chip],[data-clear],[data-tech-toggle],[data-quiet-toggle],[data-open],[data-close],[data-reveal],[data-href]');
  if(!t)return;
  if(t.hasAttribute('data-toggle')){var p=document.getElementById(t.getAttribute('data-toggle'));p.hidden=!p.hidden;if(!p.hidden){var i=p.querySelector('input');if(i)i.focus()}return}
  if(t.hasAttribute('data-seen-for')){ev.stopPropagation();var a=t.getAttribute('data-seen-for');setSeen(a,!isSeen(a));refreshSeen();return}
  if(t.hasAttribute('data-tab')){F.status=t.getAttribute('data-tab');applyRows();return}
  if(t.hasAttribute('data-chip')){F.signal=t.getAttribute('data-chip');applyRows();return}
  if(t.hasAttribute('data-clear')){F.q='';F.status='all';F.signal='all';var s=document.querySelector('[data-filter=rows]');if(s)s.value='';applyRows();return}
  if(t.hasAttribute('data-open')){openPanel(t.getAttribute('data-open'),t.getAttribute('data-jump'));return}
  if(t.hasAttribute('data-close')){document.getElementById(t.getAttribute('data-close')).hidden=true;return}
  if(t.hasAttribute('data-reveal')){var v=document.getElementById(t.getAttribute('data-reveal'));v.hidden=!v.hidden;t.textContent=t.getAttribute(v.hidden?'data-show':'data-hide');return}
  if(t.hasAttribute('data-tech-toggle')){var show=t.textContent===t.getAttribute('data-show');$$('[data-tech]').forEach(function(x){x.hidden=!show});t.textContent=t.getAttribute(show?'data-hide':'data-show');return}
  if(t.hasAttribute('data-quiet-toggle')){var l=document.querySelector('[data-quiet-list]');l.hidden=!l.hidden;t.textContent=t.getAttribute(l.hidden?'data-show':'data-hide');return}
  if(t.hasAttribute('data-href')){location.href=t.getAttribute('data-href')}
});
document.addEventListener('keydown',function(ev){var t=ev.target;if(ev.key==='Enter'&&t.hasAttribute&&t.hasAttribute('data-href'))location.href=t.getAttribute('data-href')});
document.addEventListener('input',function(ev){var t=ev.target,f=t.getAttribute&&t.getAttribute('data-filter'),q=t.value.toLowerCase();
  if(f==='rows'){F.q=q;applyRows()}
  if(f==='am'){teamQ=q;applyTeam(teamQ,teamSort)}
  if(f==='pick'){var n=0;$$('[data-pick]').forEach(function(a){var ok=a.getAttribute('data-pick')==='all account managers'||a.getAttribute('data-pick').indexOf(q)>=0;a.hidden=!ok;if(ok&&a.getAttribute('data-pick')!=='all account managers')n++});
    var em=document.querySelector('[data-empty=pick]');if(em)em.hidden=n>0||!q}
  if(f==='quiet'){$$('[data-quiet]').forEach(function(r){r.hidden=!!q&&r.getAttribute('data-quiet').indexOf(q)<0})}
});
document.addEventListener('change',function(ev){var t=ev.target,s=t.getAttribute&&t.getAttribute('data-sort');
  if(s==='rows'){F.sort=t.value;applyRows()}if(s==='am'){teamSort=t.value;applyTeam(teamQ,teamSort)}});
refreshSeen();applyTeam('', 'name');if(location.hash==='#review')openPanel('review');
})();"""


# -- serving ------------------------------------------------------------------------------------
def route(ledger: Path, path: str, big: bool = False, viewer: str = "all") -> tuple[int, str]:
    viewer = viewer if viewer in VIEWERS else "all"
    parts = [unquote(p) for p in path.strip("/").split("/") if p]
    if not parts:
        return render_today(ledger, None, viewer)
    one = {"portfolio": lambda: render_portfolio(ledger, viewer), "team": lambda: render_team(ledger, viewer),
           "control": lambda: render_control(ledger, None, viewer), "sources": lambda: render_sources(ledger, viewer),
           "history": lambda: render_history(ledger, viewer), "ledger": lambda: render_history(ledger, viewer),
           "reviews": lambda: render_reviews(ledger, False, viewer)}
    if len(parts) == 1 and parts[0] in one:
        return one[parts[0]]()
    if parts[0] == "run" and len(parts) == 2:
        return render_today(ledger, parts[1], viewer)
    if parts[0] == "run" and len(parts) == 3 and parts[2] == "control":
        return render_control(ledger, parts[1], viewer)
    if parts[0] == "run" and len(parts) == 4 and parts[2] == "account":
        return render_account(ledger, parts[1], parts[3], viewer)
    return 404, page("Not found", {"viewer": viewer}, "", "Account Radar", ("", "Not found"), "", None, [], None, "/")


class Handler(BaseHTTPRequestHandler):
    """GET only; any other method gets the stdlib's 501."""

    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        status, body = route(self.server.ledger, url.path, False, (query.get("as") or ["all"])[0])
        data = redact(body).encode("utf-8")  # last line of defence: no secret leaves
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(ledger: Path, port: int = PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.ledger = ledger
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Account Radar: read-only operator console")
    parser.add_argument("--host", default=HOST, choices=[HOST], help="only 127.0.0.1 is allowed")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--ledger", type=Path, default=unattended.LEDGER)
    args = parser.parse_args(argv)
    server = make_server(args.ledger, args.port)
    print(f"Account Radar (read-only): http://{HOST}:{server.server_address[1]}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
