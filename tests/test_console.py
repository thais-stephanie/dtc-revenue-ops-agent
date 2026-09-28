"""Account Radar, the read-only morning brief, drawn to the approved V4 design.

Pages are built from a temporary ledger produced by the real demo scenes, so
what is rendered is exactly what those runs recorded. The latest check is the
stale-data drill, so portfolio views hold both a ready and a held-back account.
Default views must read as an account manager's language; the forensics must
stay complete behind the technical disclosures.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_agent import agent, console, dataset, unattended  # noqa: E402
from revenue_agent.integrations import doctor  # noqa: E402
from revenue_agent.tools import TOOL_DEFINITIONS  # noqa: E402

spec = importlib.util.spec_from_file_location("demo", ROOT / "scripts" / "demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-SECRETSECRETSECRET",
    "SLACK_WEBHOOK_URL": "https://hooks.slack.example/services/T0/B0/SECRETHOOK",
    "HEALTHCHECKS_PING_URL": "https://hc-ping.example/SECRET-PING-UUID",
}
VIEWERS = ["all", "sofia", "jordan", "alex"]
#: system words an account manager should never need (word boundaries)
JARGON = [r"\baccepted\b", r"\bpublication", r"\bpublished\b", r"duplicates skipped", r"idempotency",
          r"source_ref", r"signal:", r"\bgates?\b", r"tool call", r"raw brief", r"forbidden_tool_attempt",
          r"offline_scripted", r"\brun id\b", r"\bagent loop\b"]
INTERNAL = (set(dataset.SEEDED) | set(dataset.BACKGROUND) | {t["name"] for t in TOOL_DEFINITIONS}
            | {"expansion_blocked_by_stale_data", "unknown_source_reference", "update_crm_record",
               "PREPARE_ACCOUNT_REVIEW", "EXPAND_CART_RECOVERY", "INVESTIGATE_BEFORE_EXPANSION"})
#: the technical panels, hidden until "Show technical details" / "How Radar reached this result"
TECH_PANELS = r"<(div|span) data-tech hidden.*?</\1>"


def strip_panels(html: str) -> str:
    """Remove every click-to-open panel (review, recorded evidence), however deeply its divs nest."""
    while (start := html.find(" data-panel hidden")) != -1:
        start = html.rindex("<div", 0, start)
        depth, i = 0, start
        for m in re.finditer(r"<div\b|</div>", html[start:]):
            depth += 1 if m.group(0) == "<div" else -1
            if depth == 0:
                i = start + m.end()
                break
        html = html[:start] + html[i:]
    return html


def default_view(html: str) -> str:
    """What an operator sees before opening a technical disclosure or a panel (script excluded)."""
    html = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
    return strip_panels(re.sub(TECH_PANELS, "", html, flags=re.S))


def disclosure(html: str) -> str:
    """Everything behind the technical disclosures, as text."""
    return " ".join(visible_text(m.group(0)) for m in re.finditer(TECH_PANELS, html, flags=re.S))


def visible_text(html: str) -> str:
    html = re.sub(r"<(style|script)>.*?</\1>", "", html, flags=re.S)
    return re.sub(r"\s+", " ", console.html.unescape(re.sub(r"<[^>]+>", " ", html)))


class _Ledger(unittest.TestCase):
    SCENES = ("healthy", "rerun", "forbidden-write", "timeout", "bad-citation", "stale")

    @classmethod
    def setUpClass(cls):
        cls._cwd = os.getcwd()
        cls._tmp = tempfile.TemporaryDirectory()
        os.chdir(cls._tmp.name)
        clean = {k: v for k, v in os.environ.items() if k not in SECRETS}
        with mock.patch.dict(os.environ, clean, clear=True), \
                mock.patch.object(agent.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            demo.SCENES["reset"]()
            for scene in cls.SCENES:
                demo.SCENES[scene]()
        cls.ledger = pathlib.Path("runs/unattended.sqlite3").resolve()
        rows = unattended.recent(cls.ledger, limit=10)[::-1]  # oldest first
        cls.ids = dict(zip(["healthy", "rerun", "forbidden", "timeout", "badcite", "stale"],
                           [r["run_id"] for r in rows]))

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._cwd)
        cls._tmp.cleanup()

    def get(self, path, viewer="all"):
        status, html = console.route(self.ledger, path, False, viewer)
        self.assertEqual(status, 200, (path, viewer))
        return html

    def text(self, path, viewer="all"):
        return visible_text(default_view(self.get(path, viewer)))

    def all_paths(self):
        paths = ["/", "/portfolio", "/team", "/control", "/sources", "/history", "/reviews"]
        for rid in self.ids.values():
            paths += [f"/run/{rid}", f"/run/{rid}/control"]
        paths += [f"/run/{self.ids['healthy']}/account/{a}" for a in ("field_foundry", "morrow_goods", "evergreen_labs")]
        paths.append(f"/run/{self.ids['stale']}/account/harbor_home")
        return paths


class TestToday(_Ledger):
    def test_a_sentence_then_the_numbers_then_the_accounts(self):
        view = default_view(self.get(f"/run/{self.ids['healthy']}", "jordan"))
        lead = view.index("1 account in your portfolio needs attention today")
        self.assertLess(lead, view.index("accounts checked"))
        self.assertLess(view.index("accounts checked"), view.index("data-row"))
        text = visible_text(view)
        self.assertIn("Radar checked your 10 accounts. 3 made the shortlist, and 1 is worth your time now.", text)
        self.assertIn("Hello, Jordan", text)
        for gone in ("Recent checks", "Repeats skipped", "model retries", "new publications"):
            self.assertNotIn(gone, text)

    def test_each_row_explains_itself_without_a_click(self):
        text = self.text(f"/run/{self.ids['healthy']}", "sofia")
        for expected in ("Field & Foundry", "Campaign performance dropped sharply", "↓ 62%", "Campaign ROAS",
                         "Prepare for an account review", "Ready for review", "Sofia Ramos"):
            self.assertIn(expected, text)
        self.assertNotIn("field_foundry", text)

    def test_morrow_shows_the_tension_and_evergreen_the_mismatch(self):
        text = self.text(f"/run/{self.ids['healthy']}")
        for expected in ("Growth opportunity, but campaign efficiency is falling", "Revenue numbers don’t agree",
                         "$24,360", "Needs more information", "Investigate the revenue attribution"):
            self.assertIn(expected, text)
        morrow = self.text(f"/run/{self.ids['healthy']}/account/morrow_goods")
        self.assertIn("Radar sees potential, but the signals disagree.", morrow)
        self.assertIn("Abandoned carts are up 179%, which points to room to grow.", morrow)
        evergreen = self.text(f"/run/{self.ids['healthy']}/account/evergreen_labs")
        self.assertIn("$13,120", evergreen)
        self.assertIn("The gap is too large to trust the campaign result as-is.", evergreen)

    def test_held_back_reads_as_a_status_not_an_instruction(self):
        text = self.text("/")  # latest check: the stale-data drill
        self.assertIn("Held back until the commerce data refreshes", text)
        self.assertIn("Held back", text)
        detail = self.text(f"/run/{self.ids['stale']}/account/harbor_home")
        self.assertIn("76 hours", detail)
        self.assertIn("the limit is 48 hours", detail)

    def test_the_team_brief_names_each_owner(self):
        html = default_view(self.get("/"))
        self.assertEqual(sorted(re.findall(r'data-owner="([^"]+)"', html)), ["Alex Chen", "Alex Chen", "Sofia Ramos"])
        self.assertIn("3 accounts across the team need attention today", visible_text(html))

    def test_a_check_that_stopped_early(self):
        text = self.text(f"/run/{self.ids['timeout']}")
        self.assertIn("The latest check stopped before it finished", text)
        self.assertIn("The AI provider timed out. Radar tried 3 times, then stopped safely. Nothing was sent to anyone.", text)
        self.assertNotIn("None", text)


class TestPortfolios(_Ledger):
    def names(self, owner):
        return {console.name(a) for a in dataset.PORTFOLIOS[owner]}

    def test_my_portfolio_never_leaks_another_managers_accounts(self):
        for slug, owner in console.VIEWERS.items():
            others = set().union(*(self.names(o) for o in dataset.PORTFOLIOS if o != owner))
            for path in ("/", "/portfolio", f"/run/{self.ids['healthy']}", "/reviews"):
                text = self.text(path, slug)
                leaked = [n for n in others if n in text]
                self.assertEqual(leaked, [], (slug, path))

    def test_portfolio_sections_account_for_all_ten(self):
        text = self.text("/portfolio", "sofia")
        self.assertIn("Sofia Ramos", text)
        self.assertIn("10 accounts", text)
        shown = [n for n in self.names("am_sofia") if n in text]
        self.assertEqual(len(shown), 10)
        for section in ("Needs attention now", "Worth watching", "Nothing raised"):
            self.assertIn(section, text)
        self.assertIn("Field & Foundry", text)

    def test_portfolio_counts_come_from_the_recorded_check(self):
        view = default_view(self.get("/portfolio", "sofia"))
        receipt, _ = console.load_receipt(console.find_run(self.ledger, None), self.ledger)
        recorded_gaps = sum(1 for s in receipt["shortlist_supplied"]
                            if s["account_id"] in dataset.PORTFOLIOS["am_sofia"]
                            and "engagement_gap" in [x["type"] for x in s["signals"]])
        self.assertEqual(recorded_gaps, 1)
        shown = re.search(r">(\d+)</span><span[^>]*>no account-manager contact in 45\+ days<", view).group(1)
        self.assertEqual(int(shown), recorded_gaps)

    def test_all_managers_see_everything_and_team_counts_reconcile(self):
        text = self.text("/team")
        self.assertIn("30 accounts in all", text)
        self.assertIn("3 accounts across 3 portfolios need attention today", text)
        view = default_view(self.get("/team"))
        self.assertEqual(sum(int(n) for n in re.findall(r'data-attn="(\d+)"', view)), 3)
        self.assertEqual(sum(int(n) for n in re.findall(r'data-accounts="(\d+)"', view)), 30)
        self.assertEqual(re.findall(r'data-am data-name="([^"]+)"', view),
                         ["Alex Chen", "Jordan Lee", "Sofia Ramos"])  # by name, not ranked
        self.assertIn("These numbers describe the accounts, not how well each manager is doing.", text)

    def test_no_employee_scoring_or_ranking(self):
        for path in ("/team", "/portfolio"):
            for viewer in VIEWERS:
                low = self.text(path, viewer).lower()
                for word in ("score", "rank #", "leaderboard", "top performer", "best am", "worst", "grade", "rating"):
                    self.assertNotIn(word, low, (path, viewer))

    def test_owner_ids_never_reach_a_page_by_default(self):
        for viewer in VIEWERS:
            for path in self.all_paths():
                html = default_view(self.get(path, viewer))
                self.assertNotRegex(html, r"\bam_(sofia|jordan|alex)\b", (viewer, path))

    def test_the_viewer_selector_is_links_that_change_nothing(self):
        before = unattended._sql(self.ledger, "SELECT COUNT(*) AS n FROM runs")
        for viewer in VIEWERS:
            html = self.get("/", viewer)
            self.assertIn("Viewing as", html)
            for slug in ("sofia", "jordan", "alex"):
                self.assertIn(f'href="/?as={slug}"', html)
        self.assertEqual(unattended._sql(self.ledger, "SELECT COUNT(*) AS n FROM runs"), before)


class TestAccountDetail(_Ledger):
    def test_field_foundry(self):
        text = self.text(f"/run/{self.ids['healthy']}/account/field_foundry")
        self.assertIn("Field & Foundry", text)
        self.assertIn("Campaign performance dropped sharply", text)
        self.assertIn("Owner: Sofia Ramos", text)
        for section in ("Why this account needs attention", "What changed", "Account context",
                        "Recommended next step", "Evidence used", "Can I trust this?"):
            self.assertIn(section, text)
        self.assertIn("Traceable", text)
        self.assertIn("every source it cites is data Radar read", text)
        self.assertIn("Safety checks: passed", text)
        self.assertNotIn("field_foundry", text)

    def test_the_ai_and_the_safety_checks_are_separated(self):
        html = self.get(f"/run/{self.ids['stale']}/account/harbor_home")
        text = visible_text(default_view(html))
        ai = text[text.index("The AI suggested"):text.index("Safety checks")]
        self.assertIn("Propose a cart-recovery campaign", ai)
        self.assertNotIn("Held back", ai)
        self.assertIn("Safety checks: held back Commerce data is 76 hours old. Expansion requires data no older "
                      "than 48 hours.", text)
        self.assertIn("Held back until the commerce data refreshes", text)
        how = disclosure(html)
        self.assertIn("expansion_blocked_by_stale_data", how)
        self.assertIn("semantic entailment is not checked", how)
        self.assertIn("How Radar reached this result", html)

    def test_bad_citation_in_plain_words(self):
        text = self.text(f"/run/{self.ids['badcite']}/control")
        self.assertIn("The AI pointed to a source Radar never gave it", text)


class TestControlRoomHistorySources(_Ledger):
    def test_control_room_leads_with_a_narrative(self):
        text = self.text(f"/run/{self.ids['stale']}/control")
        self.assertIn("This check needs a review", text)
        self.assertIn("30 accounts scanned, 8 shortlisted, 2 surfaced, 1 held back.", text)
        self.assertIn("What happened, in order", text)
        self.assertIn("This check completed normally", self.text(f"/run/{self.ids['healthy']}/control"))
        self.assertIn("This check stopped safely", self.text(f"/run/{self.ids['timeout']}/control"))

    def test_forbidden_write_and_repeats(self):
        text = self.text(f"/run/{self.ids['forbidden']}/control")
        self.assertIn("It also asked for something it isn’t allowed to do (update a CRM record). Nothing ran.", text)
        rerun = self.text(f"/run/{self.ids['rerun']}/control")
        self.assertIn("were already raised earlier, so they weren’t repeated", rerun)
        self.assertIn("Already raised earlier, so not repeated", rerun)

    def test_reviews_are_experimental_and_off_the_main_nav(self):
        html = self.get("/")
        nav = html[html.index("aria-label=Views"):html.index("</nav>")]
        self.assertNotIn("Review", nav)
        self.assertIn("Experimental: human review prototype →", disclosure(self.get("/control")))
        self.assertNotIn("human review prototype", self.text("/control"))
        self.assertIn("Approval records a decision only. Nothing is applied to a CRM.", self.text("/reviews"))

    def test_history_reads_as_sentences(self):
        text = self.text("/history")
        self.assertIn("30 accounts checked. 3 surfaced. Nothing held back.", text)
        self.assertIn("30 accounts checked. 2 surfaced. Harbor Home was held back: its commerce data is 76 hours "
                      "old, and growth suggestions need data under 48 hours.", text)
        self.assertIn("The AI provider timed out. Radar tried 3 times, then stopped safely. Nothing was sent to anyone.", text)
        for label in ("Completed normally", "Needs review", "Stopped safely"):
            self.assertIn(label, text)
        for moved in ("$0.0000", "retries", "duplicates"):
            self.assertNotIn(moved, text)
        self.assertIn("duplicates skipped", disclosure(self.get("/history")))

    def test_data_sources_claim_no_connection_it_has_not_verified(self):
        names = [n for _, ns, _ in console.VENDORS for n in ns]
        clean = {k: v for k, v in os.environ.items() if k not in names}
        with mock.patch.dict(os.environ, clean, clear=True):
            text = self.text("/sources")
        for row in ("CRM activity", "Commerce", "Campaign performance", "Billing"):
            self.assertIn(row, text)
        self.assertEqual(text.count("Synthetic demo data "), 4)  # one per source, besides the footer note
        self.assertEqual(text.count("Adapter ready"), 3)
        self.assertEqual(text.count("Not connected"), 3)
        self.assertIn("It's kept apart from the live connections so test results repeat exactly.", text)
        with mock.patch.dict(os.environ, {n: "placeholder" for n in names}):
            configured = self.text("/sources")
        self.assertEqual(configured.count("Credentials set, not tested here"), 3)
        for claim in ("Connected", "Verified", "read-only credential", "write methods"):
            self.assertNotIn(claim, text + configured)

    def test_console_vendor_names_match_the_doctor(self):
        self.assertEqual([(n, envs) for n, envs, _ in console.VENDORS],
                         [(n, envs) for n, _, envs, _ in doctor.CHECKS])

    def test_control_room_technical_details_are_complete(self):
        row = console.find_run(self.ledger, self.ids["forbidden"])
        tech = disclosure(self.get(f"/run/{self.ids['forbidden']}/control"))
        for detail in (f"run_id {self.ids['forbidden']}", f"receipt sha256 {row['forensic_report_sha256']}",
                       "receipt verification: receipt verified: SHA-256 matches the ledger", "retries 0",
                       "update_crm_record [not a registered tool]", "unknown tool: update_crm_record",
                       "source ref ", "gate rejection", "invariant violations", "tokens in/out", "cost ",
                       "monitoring (raw)", "forbidden_tool_attempt", "system prompt sha256",
                       "tool definitions sha256", "duplicate key", f"receipt {row['forensic_report_path']}", "model "):
            self.assertIn(detail, tech)


class TestOperatorLanguage(_Ledger):
    def test_no_system_jargon_or_internal_identifiers_by_default(self):
        for viewer in VIEWERS:
            for path in self.all_paths():
                text = visible_text(default_view(self.get(path, viewer)))
                for pattern in JARGON:
                    self.assertNotRegex(text, pattern, (viewer, path))
                self.assertEqual(re.findall(r"\b[a-z0-9]+_[a-z0-9_]+\b", text), [], (viewer, path))
                self.assertEqual([i for i in INTERNAL if i in text], [], (viewer, path))

    def test_no_fabricated_metrics_or_branding(self):
        for path in self.all_paths():
            html = self.get(path)
            low = visible_text(default_view(html)).lower()
            for invented in ("possible revenue", "at risk", "revenue at stake", "/ mo", "health score"):
                self.assertNotIn(invented, low)
            self.assertIn("Account Radar", html)
            self.assertIn("Revenue Operations Agent", html)

    def test_controls_only_navigate_filter_or_reveal(self):
        for path in self.all_paths():
            html = self.get(path)
            low = html.lower()
            for control in ("<form", "method=", "onclick", "fetch(", "xmlhttprequest", "sendbeacon", "websocket",
                            "localstorage", "eventsource", "import("):
                self.assertNotIn(control, low, path)
            self.assertEqual([b for b in re.findall(r"<button[^>]*>", html) if "type=button" not in b], [], path)
            for link in re.findall(r'href="([^"]+)"', html):
                self.assertTrue(link.startswith(("/", "#", "https://fonts.g")) or link == "data:,",
                                (path, link))
            self.assertEqual(re.findall(r"<script[^>]*src=", low), [], path)

    def test_status_is_never_colour_alone(self):
        for path in self.all_paths():
            html = self.get(path)
            for status, row in re.findall(r'data-status="(\w+)"(.*?)Mark reviewed', html, flags=re.S):
                self.assertIn(console.STATUS[status][0], row)

    def test_responsive_and_accessible_structure(self):
        html = self.get("/")
        for fragment in ("<meta name=viewport", ":focus-visible", "aria-current=page", "prefers-reduced-motion",
                         "flex-wrap:wrap", "overflow-x:auto", "family=Poppins", "aria-label=\"Search accounts\""):
            self.assertIn(fragment, html)
        self.assertNotIn("Segoe", html)

    def test_unknown_routes_are_404(self):
        for path in ("/nope", "/run/no-such-run", "/run/no-such/control",
                     f"/run/{self.ids['stale']}/account/not_an_account"):
            self.assertEqual(console.route(self.ledger, path, False)[0], 404, path)


class TestEvidenceIntegrity(_Ledger):
    def test_hash_mismatch_hides_the_evidence(self):
        row = unattended._sql(self.ledger, "SELECT * FROM runs WHERE run_id = ?", (self.ids["stale"],))[0]
        path = self.ledger.parent.parent / pathlib.Path(row["forensic_report_path"].replace("\\", "/"))
        original = path.read_bytes()
        try:
            path.write_bytes(original.replace(b"EXPAND_CART_RECOVERY", b"NO_ACTION"))
            for html in (self.get(f"/run/{self.ids['stale']}"), self.get(f"/run/{self.ids['stale']}/control")):
                self.assertIn("HASH MISMATCH", html)
                self.assertNotIn("Harbor Home", visible_text(html))
                self.assertNotIn("tool #", html)
            path.unlink()
            self.assertIn("RECEIPT MISSING", self.get(f"/run/{self.ids['stale']}"))
        finally:
            path.write_bytes(original)
        self.assertIn("receipt verified: SHA-256 matches the ledger", self.get(f"/run/{self.ids['stale']}/control"))

    def test_a_row_cannot_point_the_viewer_outside_its_folder(self):
        row = {"forensic_report_path": "..\\..\\..\\Windows\\win.ini", "forensic_report_sha256": "x"}
        self.assertEqual(console.load_receipt(row, self.ledger), (None, "outside"))

    def test_no_secret_value_can_appear_in_any_page(self):
        with mock.patch.dict(os.environ, SECRETS):
            pages = [self.get(p, v) for p in self.all_paths() for v in ("all", "alex")]
            served = console.redact(console.page("x", {"viewer": "all"}, "", SECRETS["SLACK_WEBHOOK_URL"],
                                                 ("", ""), "", None, [], None, "/"))
        for html in pages + [served]:
            for value in SECRETS.values():
                self.assertNotIn(value, html)


class TestActionability(_Ledger):
    """Insight to a safe next step: every raised account leads somewhere, and nothing acts."""
    SHORT = {short: status for status, (_, short) in console.CTA.items()}

    def account(self, run, account_id):
        return self.get(f"/run/{self.ids[run]}/account/{account_id}")

    def test_every_surfaced_account_has_a_meaningful_primary_action(self):
        for rid in self.ids.values():
            html = default_view(self.get(f"/run/{rid}"))
            rows = re.findall(r'data-row .*?data-status="(\w+)"', html)
            ctas = re.findall(r'data-cta="(\w+)" href="([^"]+)"[^>]*>([^<]+)</a>', html)
            self.assertEqual([c[0] for c in ctas], rows, rid)
            for status, link, label in ctas:
                self.assertEqual(self.SHORT[console.html.unescape(label)], status)
                self.assertIn("/account/", link)

    def test_ready_needs_info_and_held_each_get_their_own_action(self):
        expected = [("healthy", "field_foundry", "Review recommendation"),
                    ("healthy", "morrow_goods", "Review investigation"),
                    ("healthy", "evergreen_labs", "Review investigation"),
                    ("stale", "harbor_home", "Investigate blocker"),
                    ("badcite", "field_foundry", "Investigate blocker")]
        for run, account_id, action in expected:
            text = visible_text(default_view(self.account(run, account_id)))
            self.assertIn(action, text, (run, account_id))
            for other in {"Review recommendation", "Review investigation", "Investigate blocker"} - {action}:
                self.assertNotIn(other, text, (run, account_id))  # a held item is never offered as executable

    def test_held_back_separates_the_ai_from_the_safety_decision(self):
        text = visible_text(self.account("stale", "harbor_home"))
        review = text[text.index("Why this is held back"):text.index("Close review")]
        for part in ("What the AI suggested Propose a cart-recovery campaign",
                     "What the safety check decided Held back. This isn’t ready to act on.",
                     "Why Commerce data is 76 hours old. Expansion requires data no older than 48 hours.",
                     "What would release it Held back until the commerce data refreshes",
                     "Evidence behind the block Commerce data age: 76 hours · the limit is 48 hours"):
            self.assertIn(part, review)
        bad = visible_text(self.account("badcite", "field_foundry"))
        self.assertIn("What would release it Held back until it cites only data Radar read", bad)

    def test_no_action_accounts_are_not_urgent(self):
        original = console.brief

        def quiet(receipt):
            cards = original(receipt)
            for c in cards:
                if c["account_id"] == "field_foundry":
                    c["status"] = "none"
            return cards
        with mock.patch.object(console, "brief", quiet):
            today = default_view(self.get(f"/run/{self.ids['healthy']}"))
            detail = default_view(self.account("healthy", "field_foundry"))
        self.assertRegex(today, r'data-cta="none" href="[^"#]+"[^>]*>View account</a>')
        self.assertNotIn('data-cta="none" data-open', detail)
        self.assertNotIn("Recommended next step", visible_text(detail))

    def test_mark_reviewed_stays_in_this_browser_tab(self):
        html = self.get(f"/run/{self.ids['healthy']}")
        self.assertEqual(html.count("Mark reviewed</button>"), html.count("data-row "))
        for b in re.findall(r"<button[^>]*data-seen-for[^>]*>", html):
            self.assertIn("type=button", b)
        script = console.SCRIPT
        self.assertIn("sessionStorage", script)
        for forbidden in ("localStorage", "fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "cookie"):
            self.assertNotIn(forbidden, script)
        self.assertNotIn("Mark as seen", html)

    def test_reviewing_changes_nothing(self):
        tables = ("runs", "publications", "reviews")
        count = lambda: [unattended._sql(self.ledger, f"SELECT COUNT(*) AS n FROM {t}")  # noqa: E731
                         if unattended._sql(self.ledger, "SELECT 1 FROM sqlite_master WHERE name = ?", (t,)) else None
                         for t in tables]
        receipts = {p: p.read_bytes() for p in (self.ledger.parent).glob("*.json")}
        before = count()
        for run, account_id in (("healthy", "field_foundry"), ("healthy", "morrow_goods"), ("stale", "harbor_home")):
            html = self.account(run, account_id)
            self.assertIn("Reviewing changes nothing", html)
        self.assertEqual(count(), before)
        self.assertEqual({p: p.read_bytes() for p in receipts}, receipts)

    def test_the_draft_shown_is_exactly_the_recorded_proposal(self):
        row = console.find_run(self.ledger, self.ids["stale"])
        receipt, _ = console.load_receipt(row, self.ledger)
        [task] = [t for t in receipt["tool_log"] if t["tool"] == "propose_crm_task"]
        html = self.account("stale", task["account_id"])
        draft = html[html.index("id=draft"):]
        draft = visible_text(draft[:draft.index("</div>")])
        self.assertIn(console.humanize(task["arguments"]["title"]), draft)
        self.assertIn(console.humanize(task["arguments"]["description"]), draft)
        self.assertIn("Draft only.", draft)
        self.assertIn("Nothing has been created in HubSpot or any CRM.", draft)
        self.assertIn("Review draft", visible_text(default_view(html)))
        for run, account_id in (("healthy", "field_foundry"), ("healthy", "morrow_goods")):
            text = visible_text(self.account(run, account_id))
            self.assertIn("No follow-up task was drafted for this account in this check.", text)
            self.assertNotIn("Review draft", text)

    def test_the_draft_reads_account_names_while_the_record_keeps_the_raw_id(self):
        row = console.find_run(self.ledger, self.ids["stale"])
        path = self.ledger.parent.parent / pathlib.Path(row["forensic_report_path"].replace("\\", "/"))
        stored = path.read_bytes()
        html = self.account("stale", "harbor_home")
        draft = html[html.index("id=draft"):]
        draft = visible_text(draft[:draft.index("</div>")])
        self.assertIn("Discuss next step for Harbor Home", draft)
        self.assertNotIn("harbor_home", draft)
        self.assertNotIn("harbor_home", visible_text(strip_panels(re.sub(TECH_PANELS, "", html, flags=re.S))))
        self.assertIn("Discuss next step for harbor_home", disclosure(html))  # raw, behind technical details
        self.assertEqual(path.read_bytes(), stored)
        self.assertEqual(console.load_receipt(row, self.ledger)[1], "verified")

    def test_only_exact_known_account_ids_are_humanized(self):
        h = console.humanize
        self.assertEqual(h("Discuss next step for harbor_home"), "Discuss next step for Harbor Home")
        self.assertEqual(h("field_foundry and morrow_goods."), "Field & Foundry and Morrow Goods.")
        for untouched in ("task_harbor_home", "harbor_home_2", "harbor_homes", "some_other_id",
                          "account_context:field_foundry_x", "Harbor_Home"):
            self.assertEqual(h(untouched), untouched)
        self.assertEqual(h(None), "")

    def test_no_control_writes_to_a_business_system(self):
        verbs = re.compile(r"\b(create|approve|apply|send|sync|execute|launch|retry|run|update|push|submit)\b", re.I)
        for path in self.all_paths():
            html = self.get(path)
            for label in re.findall(r"<(?:button|a)\b[^>]*>([^<]*)</(?:button|a)>", html):
                self.assertNotRegex(label, verbs, (path, label))

    def test_no_invented_vendor_links(self):
        for path in self.all_paths():
            for link in re.findall(r'href="(https?://[^"]+)"', self.get(path)):
                self.assertTrue(link.startswith("https://fonts.g"), (path, link))
        resolve = console.source_action
        self.assertEqual(resolve({}), ("View recorded evidence", None))
        self.assertEqual(resolve({"source_system": "hubspot", "record_id": "123"}), ("View recorded evidence", None))
        self.assertEqual(resolve({"source_system": "hubspot", "source_url": "http://x"}), ("View recorded evidence", None))
        self.assertEqual(resolve({"source_system": "salesforce", "source_url": "https://x"}), ("View recorded evidence", None))
        good = "https://app.hubspot.com/<path>"
        self.assertEqual(resolve({"source_system": "HubSpot", "source_url": good}), ("Open in HubSpot ↗", good))
        for bad in ("http://app.hubspot.com/<path>", "https://app.example/1", "https://app.hubspot.com.evil.example/x",
                    "https://evilhubspot.com/x", "https://app.hubspot.com@evil.example/x",
                    "https://app.hubspot.com:8443/x", "https://[bad/x", "javascript:alert(1)", 42):
            self.assertEqual(resolve({"source_system": "hubspot", "source_url": bad}), ("View recorded evidence", None), bad)
        # a verified HubSpot host is not a pass for another vendor, and unverified vendors never link
        self.assertEqual(resolve({"source_system": "shopify", "source_url": good}), ("View recorded evidence", None))
        card = {"domain": "CRM activity", "label": "x", "value": "y", "detail": "", "values": [], "ref": "r",
                "origin": "o", "provenance": "p", "read": "now", "source_system": "hubspot", "source_url": good}
        self.assertIn('target=_blank rel="noopener noreferrer"', console.evidence_item(card, 0))
        self.assertNotIn("<a ", console.evidence_item({**card, "source_url": None}, 0))

    def test_evidence_reconciles_to_what_was_recorded(self):
        row = console.find_run(self.ledger, self.ids["healthy"])
        receipt, _ = console.load_receipt(row, self.ledger)
        entry = next(s for s in receipt["shortlist_supplied"] if s["account_id"] == "field_foundry")
        context = console.context_by_account(receipt)["field_foundry"]
        note = console.latest_note(receipt, "field_foundry")
        text = visible_text(self.account("healthy", "field_foundry"))
        evidence = text[text.index("Evidence used"):text.index("Recommended next step")]
        for label, value, detail in console.facts(entry):
            self.assertIn(f"{label}: {value} · {detail}", evidence)
        perf = context["performance_summary"]
        self.assertIn(f"Campaign ROAS {perf['roas_7d']} this week, {perf['roas_baseline_28d']} the 28 days before", evidence)
        self.assertIn(note["body"], evidence)
        evergreen = visible_text(self.account("healthy", "evergreen_labs"))
        evergreen = evergreen[evergreen.index("Evidence used"):]
        self.assertIn("Campaign data Campaign-reported revenue: $24,360", evergreen)
        self.assertIn("Commerce data Commerce revenue: $13,120", evergreen)

    def test_source_refs_are_hidden_by_default_and_provenance_stays_in_the_disclosure(self):
        html = self.account("healthy", "field_foundry")
        default = visible_text(default_view(html))
        for ref in ("signal:field_foundry", "account_context:field_foundry", "account_activity:field_foundry"):
            self.assertNotIn(ref, default)
            self.assertIn(f"source ref {ref}", visible_text(html))  # one click away
        self.assertIn("cited account_context:field_foundry ← ('field_foundry', 'tool:get_account_context')",
                      disclosure(html))


class TestEmptyAndServing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = pathlib.Path(self._tmp.name) / "runs" / "unattended.sqlite3"

    def test_empty_ledger(self):
        for path in ("/", "/control", "/team", "/history", "/sources"):
            status, html = console.route(self.ledger, path, False)
            self.assertEqual(status, 200)
            if path != "/sources":
                self.assertIn("No checks recorded yet", html)
        self.assertIn("No checks recorded yet", console.route(self.ledger, "/portfolio", False, "sofia")[1])

    def test_binds_to_localhost_only_and_serves_get_only(self):
        mock.patch("sys.stderr").start()  # the request log
        self.addCleanup(mock.patch.stopall)
        server = console.make_server(self.ledger, port=0)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], "127.0.0.1")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        for path in ("/", "/team?as=sofia", "/history"):
            with urllib.request.urlopen(base + path) as r:
                self.assertEqual(r.status, 200)
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            with self.assertRaises(urllib.error.HTTPError) as refused:
                urllib.request.urlopen(urllib.request.Request(base + "/", method=method))
            self.assertEqual(refused.exception.code, 501, method)
            refused.exception.close()

    def test_any_other_host_is_refused(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            console.main(["--host", "0.0.0.0"])


if __name__ == "__main__":
    unittest.main()
