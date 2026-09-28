"""scripts/demo.py: every scene runs offline, prints plain ASCII, and tells the right story."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_agent import agent  # noqa: E402

spec = importlib.util.spec_from_file_location("demo", ROOT / "scripts" / "demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

ORDER = ["reset", "healthy", "rerun", "stale", "forbidden-write", "timeout", "ledger", "bad-citation"]


class TestDemoScenes(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)  # scenes use runs/ under the cwd; main() is not called
        env = {k: v for k, v in os.environ.items()
               if k not in ("HEALTHCHECKS_PING_URL", "SLACK_WEBHOOK_URL")}
        mock.patch.dict(os.environ, env, clear=True).start()
        mock.patch.object(agent.time, "sleep").start()
        self.urlopen = mock.patch("urllib.request.urlopen").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def scene(self, name):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            demo.SCENES[name]()
        return out.getvalue()

    def test_the_full_sequence(self):
        out = {name: self.scene(name) for name in ORDER}
        for name, text in out.items():
            text.encode("ascii")  # raises if any scene prints non-ASCII
            self.assertNotIn("None", text, name)
            self.assertNotIn("RevOps Agent -", text, name)  # no alert dump when Slack is unset
        self.urlopen.assert_not_called()  # monitors unset: nothing leaves the machine

        self.assertIn("New ledger ready:", out["reset"])
        self.assertIn("Recommendations accepted:    3\nNew publications:            3\n"
                      "Duplicates skipped:          0", out["healthy"])
        self.assertIn("Recommendations accepted:    3\nNew publications:            0\n"
                      "Duplicates skipped:          3", out["rerun"])
        self.assertIn("Publications table:          3 rows, 3 distinct keys", out["rerun"])
        self.assertIn("Slack:                       not needed", out["rerun"])
        self.assertIn("Account:                     harbor_home", out["stale"])
        self.assertIn("MODEL RECOMMENDATION:        EXPAND_CART_RECOVERY", out["stale"])
        self.assertIn("expansion_blocked_by_stale_data", out["stale"])
        self.assertIn("Attempted tool:              update_crm_record", out["forbidden-write"])
        self.assertIn("Tool registered:             no", out["forbidden-write"])
        self.assertIn("Attempts:                    3\nRetries:                     2\n"
                      "Final status:                failed", out["timeout"])
        self.assertIn("Slack:                       not configured", out["timeout"])
        self.assertIn("Healthchecks:                not configured", out["timeout"])
        self.assertIn("Unknown ref:                 campaign_metric_999", out["bad-citation"])
        for name in ORDER[1:]:
            if name != "ledger":
                self.assertIn("External business-system writes: 0", out[name], name)
        self.assertEqual(out["ledger"].count(" offline "), 5)  # bad-citation runs after it

    def test_reset_archives_and_never_deletes(self):
        self.scene("reset")
        self.scene("healthy")
        with mock.patch.object(demo, "datetime") as clock:
            clock.now.return_value.strftime.return_value = "20260928T070000Z"
            self.scene("reset")
        archive = pathlib.Path("runs") / "unattended-archive-20260928T070000Z.sqlite3"
        self.assertTrue(archive.exists())
        from revenue_agent import unattended

        self.assertEqual(len(unattended.recent(archive)), 1)  # the healthy run survived
        self.assertEqual(unattended.recent(), [])

    def test_an_unknown_scene_prints_usage(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(demo.main(["nope"]), 2)
        self.assertIn("python scripts/demo.py healthy", out.getvalue())


if __name__ == "__main__":
    unittest.main()
