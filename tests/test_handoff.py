"""
test_handoff.py — write_handoff_via_ollama records the real failure cause.

The resume note is read by the *next* agent to decide how to pick up. A
validation failure and a rate-limit need different resume paths, so the note
must not hard-code "usage limit reached" for every failure — that would tell a
fresh agent to sit and wait for quota when the real fix is to re-run the step.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path.home() / "hermes"))
import hermes  # noqa: E402


class _FakeResp:
    """Minimal context-manager stand-in for urlopen()'s response."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


class HandoffCauseTest(unittest.TestCase):
    def test_note_records_the_given_cause(self):
        fake = _FakeResp({"response": "## What Was Completed\n- ran the plan step"})
        with (
            patch("urllib.request.urlopen", return_value=fake),
            patch.object(hermes.Path, "home", return_value=Path(self._tmp())),
        ):
            path = hermes.write_handoff_via_ollama(
                task="get me some customers to warm call",
                session_name="2026-07-11-000",
                partial_output="some partial work",
                cause="Output schema validation failed: no heading",
            )
        self.assertTrue(path, "handoff path should be non-empty on success")
        note = Path(path).read_text()
        self.assertIn("Output schema validation failed", note)
        self.assertNotIn("usage limit reached", note)  # not the rate-limit default

    def test_default_cause_is_rate_limit(self):
        fake = _FakeResp({"response": "## What Was Completed\n- x"})
        with (
            patch("urllib.request.urlopen", return_value=fake),
            patch.object(hermes.Path, "home", return_value=Path(self._tmp())),
        ):
            path = hermes.write_handoff_via_ollama("t", "s", "p")
        self.assertIn("usage limit reached", Path(path).read_text())

    def _tmp(self) -> str:
        import tempfile

        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d


if __name__ == "__main__":
    unittest.main()
