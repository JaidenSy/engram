"""
test_daily_note.py — unit tests for engram._append_pipeline_to_daily_note.

Salvaged from feature/pipeline-daily-note-logging. Verifies the create-vs-append
branch and per-status formatting, with RAPHBRAIN_DAILY_DIR patched to a tmp dir
so the real vault is never touched.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engram  # noqa: E402


class TestDailyNote(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name) / "Daily"
        self._p = patch.object(engram, "RAPHBRAIN_DAILY_DIR", self._dir)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def _today(self):
        from datetime import datetime

        return self._dir / f"{datetime.now().strftime('%Y-%m-%d')}.md"

    def test_creates_note_then_appends(self):
        engram._append_pipeline_to_daily_note("arbiter", "feature/x", "done", "42m")
        note = self._today()
        self.assertTrue(note.exists())
        body = note.read_text()
        self.assertIn("✅ Engram: `arbiter/feature/x` done in 42m", body)

        # Second call appends, doesn't clobber.
        engram._append_pipeline_to_daily_note("engram", "", "failed", "12m", failed_step="tester")
        body = note.read_text()
        self.assertIn("✅ Engram: `arbiter/feature/x` done", body)  # first line survived
        self.assertIn("❌ Engram: `engram` failed at `tester` (12m)", body)

    def test_unknown_status_writes_nothing(self):
        engram._append_pipeline_to_daily_note("p", "b", "running", "1m")
        self.assertFalse(self._today().exists())


if __name__ == "__main__":
    unittest.main()
