"""
test_run_task.py — unit tests for hermes.run_task background execution.

Covers the two behaviors that replaced the old tmux + HERMES_DONE sentinel +
30-min poll loop:
  1. Normal process exit → captured output is handed to on_complete; the ack
     is returned immediately (daemon never blocks).
  2. Safety timeout → the child is killed and on_complete still fires with a
     notice (no silent leak, no false "timed out" while work is unreported).
"""

import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path.home() / "hermes"))
import hermes  # noqa: E402


class _FakeProc:
    """Minimal Popen stand-in: optionally times out on the first communicate()."""

    def __init__(self, output: str = "out", timeout_first: bool = False):
        self._output = output
        self._timeout_first = timeout_first
        self._calls = 0
        self.kill = MagicMock()

    def communicate(self, timeout=None):
        self._calls += 1
        if self._timeout_first and self._calls == 1:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
        return (self._output, None)


def _run_and_wait(fake: _FakeProc, task: str = "do a thing"):
    """Run run_task with Popen faked out; block until on_complete fires."""
    done = threading.Event()
    box: dict = {}

    def cb(r: str) -> None:
        box["result"] = r
        done.set()

    with patch("subprocess.Popen", return_value=fake):
        ack = hermes.run_task(task, session_name="test-runtask", on_complete=cb)
        assert done.wait(timeout=10), "on_complete never fired"
    return ack, box["result"], fake


class TestRunTask(unittest.TestCase):
    def setUp(self):
        # Hermetic, fast config lookup — don't depend on config.yaml contents.
        self._cfg = patch.object(hermes, "load_config", return_value={})
        self._cfg.start()

    def tearDown(self):
        self._cfg.stop()
        for d in ("tasks", "logs"):
            p = (
                Path.home()
                / "hermes"
                / d
                / ("test-runtask.md" if d == "tasks" else "test-runtask.log")
            )
            p.unlink(missing_ok=True)

    def test_normal_exit_delivers_output(self):
        ack, result, _ = _run_and_wait(_FakeProc(output="agent output\n"))
        self.assertTrue(ack.startswith("▶ Started"))  # returns immediately
        self.assertEqual(result, "agent output")

    def test_safety_timeout_kills_and_reports(self):
        ack, result, fake = _run_and_wait(_FakeProc(output="partial", timeout_first=True))
        self.assertTrue(ack.startswith("▶ Started"))
        fake.kill.assert_called_once()  # child is actually killed, not leaked
        self.assertIn("Killed after", result)
        self.assertIn("partial", result)


if __name__ == "__main__":
    unittest.main()
