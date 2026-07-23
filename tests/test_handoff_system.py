"""
test_handoff_system.py — the `handoffs` / `resume` / `clear` lifecycle. The store
dirs are patched to a tmp so the real vault is untouched, and run_task is mocked so
no agent is actually spawned. Covers meta parsing, pending-vs-resolved separation,
and that resume dispatches into the resolved repo then archives the handoff.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engram  # noqa: E402


class _FakeProj:
    repo = "~/Projects/demo"
    raphbrain_dir = "Demo"


class TestHandoffSystem(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name) / "handoffs"
        self._dir.mkdir(parents=True)
        self._resolved = self._dir / "resolved"
        self._p1 = patch.object(engram, "HANDOFFS_DIR", self._dir)
        self._p2 = patch.object(engram, "HANDOFFS_RESOLVED_DIR", self._resolved)
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        self._tmp.cleanup()

    def _write(self, stem, project="demo", task="ship the thing"):
        marker = f'<!-- engram-meta project="{project}" -->\n' if project else ""
        (self._dir / f"{stem}-handoff.md").write_text(
            f"{marker}# Auto-Handoff — {stem}\n\n> **Original task:** {task}\n\n## How To Resume\ndo x\n"
        )

    def test_parse_meta(self):
        self._write("t1", project="finance-tracker", task="fix parser")
        meta = engram._parse_handoff_meta((self._dir / "t1-handoff.md").read_text())
        self.assertEqual(meta["project"], "finance-tracker")
        self.assertEqual(meta["task"], "fix parser")

    def test_parse_meta_legacy_no_marker(self):
        meta = engram._parse_handoff_meta("# Auto-Handoff\n\n> **Original task:** old one\n")
        self.assertEqual(meta["project"], "")
        self.assertEqual(meta["task"], "old one")

    def test_pending_excludes_resolved_and_is_newest_first(self):
        self._write("older")
        self._write("newer")
        os.utime(self._dir / "newer-handoff.md", (time.time() + 10, time.time() + 10))
        self.assertEqual(
            [p.stem for p in engram._pending_handoffs()], ["newer-handoff", "older-handoff"]
        )
        self._resolved.mkdir(parents=True, exist_ok=True)
        (self._resolved / "gone-handoff.md").write_text("x")
        self.assertNotIn("gone-handoff", [p.stem for p in engram._pending_handoffs()])

    def test_list_empty(self):
        self.assertIn("No pending handoffs", engram._list_handoffs())

    def test_resume_dispatches_into_repo_then_archives(self):
        self._write("t1", project="demo", task="do it")
        with (
            patch.object(engram, "run_task", return_value="▶ started") as rt,
            patch.object(engram, "resolve_project", return_value=_FakeProj()),
            patch.object(engram, "_send_reply"),
        ):
            out = engram._resume_handoff("1", {})
        rt.assert_called_once()
        self.assertEqual(rt.call_args.kwargs["cwd"], "~/Projects/demo")
        self.assertEqual(rt.call_args.kwargs["project"], "demo")
        self.assertIn("Resuming", out)
        self.assertEqual(engram._pending_handoffs(), [])  # left the pending list
        self.assertTrue((self._resolved / "t1-handoff.md").exists())  # archived

    def test_resume_no_match(self):
        self._write("t1")
        self.assertIn("matches", engram._resume_handoff("9", {}))

    def test_out_of_range_index_never_substring_matches_a_stem(self):
        # stems embed unix timestamps; `resume 9` (out of range) must error, NOT
        # substring-match "9" in a timestamp and resume/archive the wrong handoff.
        (self._dir / "engram-1753000009-handoff.md").write_text(
            '<!-- engram-meta project="demo" -->\n# h\n> **Original task:** t\n'
        )
        with (
            patch.object(engram, "run_task") as rt,
            patch.object(engram, "resolve_project", return_value=_FakeProj()),
        ):
            out = engram._resume_handoff("9", {})
        rt.assert_not_called()
        self.assertIn("matches", out)
        self.assertTrue((self._dir / "engram-1753000009-handoff.md").exists())  # not archived

    def test_resume_bare_lists_pending(self):
        self._write("t1")
        self.assertIn("Pending handoffs", engram._resume_handoff("", {}))

    def test_resume_unresolvable_project_does_not_dispatch(self):
        self._write("t1", project="")  # legacy, no marker
        with (
            patch.object(engram, "run_task") as rt,
            patch.object(engram, "classify_task", side_effect=Exception("no ollama")),
        ):
            out = engram._resume_handoff("1", {})
        rt.assert_not_called()
        self.assertIn("Couldn't map", out)
        self.assertTrue((self._dir / "t1-handoff.md").exists())  # NOT archived on failure

    def test_clear_moves_all(self):
        self._write("a")
        self._write("b")
        self.assertIn("Cleared 2", engram._clear_handoffs())
        self.assertEqual(engram._pending_handoffs(), [])
        self.assertTrue((self._resolved / "a-handoff.md").exists())


if __name__ == "__main__":
    unittest.main()
