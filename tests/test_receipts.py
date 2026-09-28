"""Receipt names: unique per run, never overwritten, history left alone."""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revenue_agent import daily  # noqa: E402
from revenue_agent.agent import AgentRunner  # noqa: E402
from revenue_agent.repository import InMemoryRepository  # noqa: E402
from revenue_agent.testing import ScriptedClient  # noqa: E402

FROZEN = datetime(2026, 9, 27, 5, 0, 0, tzinfo=timezone.utc)


class TestReceiptNames(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        clock = mock.patch.object(daily, "datetime", wraps=datetime)
        clock.start().now.return_value = FROZEN  # every receipt lands in the same second
        self.addCleanup(clock.stop)
        self.result = AgentRunner(InMemoryRepository(), ScriptedClient()).run()

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_two_runs_in_the_same_second_get_two_receipts(self):
        a = daily.write_receipt(self.result, live=False)
        b = daily.write_receipt(self.result, live=False)
        self.assertNotEqual(a, b)
        self.assertTrue(a.name.startswith("20260927T050000Z-offline-"))
        self.assertEqual(sorted(p.name for p in pathlib.Path("runs").iterdir()), sorted([a.name, b.name]))

    def test_a_given_run_id_names_the_receipt(self):
        path = daily.write_receipt(self.result, live=False, label="unattended", run_id="abc-123")
        self.assertEqual(path.name, "20260927T050000Z-offline-unattended-abc-123.json")

    def test_an_existing_receipt_is_never_overwritten(self):
        daily.write_receipt(self.result, live=False, run_id="same")
        with self.assertRaises(FileExistsError):
            daily.write_receipt(self.result, live=False, run_id="same")

    def test_historical_receipts_are_untouched(self):
        pathlib.Path("runs").mkdir()
        old = pathlib.Path("runs") / "20260927T050000Z-offline.json"  # the old, timestamp-only name
        old.write_text('{"historical": true}')
        daily.write_receipt(self.result, live=False)
        self.assertEqual(old.read_text(), '{"historical": true}')


if __name__ == "__main__":
    unittest.main()
