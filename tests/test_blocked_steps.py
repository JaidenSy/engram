"""
test_blocked_steps.py — the four defects that killed run dec77da3 (2026-08-02).

That run's research step correctly spotted two real blockers, stopped to ask
which one Jaiden wanted resolved, and was thrown away for it: no output note →
"Output schema validation failed: output note not found" → an Ollama handoff
that invented three files nobody had ever written. The retry re-ran the same
prompt at the same turn budget and got nowhere.

Covered here:
  1. read_blocked_section() — a declared blocker is detected, not silently advanced
  2. write_stub_handoff()   — deterministic facts, never an inferred file list
  3. RETRY_TURN_MULTIPLIER  — a retry buys more turns instead of repeating
  4. BLOCKED_PROTOCOL       — every step prompt tells the agent how to report blocked
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_runner  # noqa: E402
import engram  # noqa: E402


class TestReadBlockedSection(unittest.TestCase):
    """A step that says it's blocked must be recognised as blocked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        (self.vault / "notes").mkdir()
        patcher = patch.object(Path, "home", staticmethod(lambda: self.vault.parent))
        self.addCleanup(patcher.stop)
        patcher.start()
        # read_blocked_section resolves ~/Documents/RaphBrain/<note>
        self.notes_dir = self.vault.parent / "Documents" / "RaphBrain" / "notes"
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, body: str) -> str:
        (self.notes_dir / "out.md").write_text(body)
        return "notes/out.md"

    def test_blocked_section_is_returned(self):
        note = self._write(
            "# Plan\n\nSome work.\n\n"
            "## Blocked\nPR #16 is unmerged; merge it or authorise stacking.\n\n"
            "## Handoff\n**Status:** blocked\n"
        )
        self.assertIn("PR #16 is unmerged", engram.read_blocked_section(note))

    def test_status_blocked_alone_is_enough(self):
        """The agent may forget the heading but still declare the status."""
        note = self._write("# Plan\n\n## Handoff\n**Status:** blocked\n")
        self.assertTrue(engram.read_blocked_section(note))

    def test_stops_at_the_next_heading(self):
        note = self._write(
            "## Blocked\nneed a decision\n\n## Handoff\n**Status:** blocked\nrole: plan\n"
        )
        section = engram.read_blocked_section(note)
        self.assertIn("need a decision", section)
        self.assertNotIn("role: plan", section)

    def test_a_healthy_note_is_not_blocked(self):
        """The common case must not trip — 'blocked' as prose is not a status."""
        note = self._write(
            "# Research\n\nNothing blocked me.\n\n"
            "## Handoff\n**Status:** done\n\n### Blockers / open issues\nNone\n"
        )
        self.assertEqual(engram.read_blocked_section(note), "")

    def test_unfilled_template_is_not_blocked(self):
        """run-agent.sh's handoff template literally prints the status choices.
        An agent that pasted it without filling it in is sloppy, not blocked —
        a substring match here would halt almost every run."""
        note = self._write("# Research\n\n## Handoff\n\n**Status:** done | partial | blocked\n")
        self.assertEqual(engram.read_blocked_section(note), "")

    def test_missing_note_is_not_blocked(self):
        self.assertEqual(engram.read_blocked_section("notes/nope.md"), "")


class TestStubHandoff(unittest.TestCase):
    """With no partial output, state facts — never let a model fill the gap."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name)
        patcher = patch.object(engram, "HANDOFFS_DIR", home / "handoffs")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.addCleanup(self._tmp.cleanup)

    def test_records_task_and_cause_verbatim(self):
        path = engram.write_stub_handoff(
            task="compare the basket against SPY",
            session_name="2026-08-02-010",
            cause="🚫 BLOCKED by research: PR #16 unmerged",
        )
        text = Path(path).read_text()
        self.assertIn("compare the basket against SPY", text)
        self.assertIn("PR #16 unmerged", text)
        self.assertIn("2026-08-02-010", text)

    def test_points_at_the_log_not_at_invented_files(self):
        """dec77da3's Ollama handoff invented compare_projected_trajectory.sh,
        determine_winner.py and projection_data.csv. A stub cites sources only."""
        path = engram.write_stub_handoff(task="t", session_name="2026-08-02-010", cause="failed")
        text = Path(path).read_text()
        self.assertIn("raphael/logs/*-2026-08-02-010.log", text)
        self.assertIn("no output note", text.lower())
        self.assertNotIn("Ollama", text)

    def test_links_the_agents_own_note_when_there_is_one(self):
        path = engram.write_stub_handoff(
            task="t",
            session_name="s1",
            cause="blocked",
            source_note="Projects/AlphaBot/agents/out.md",
        )
        self.assertIn("Projects/AlphaBot/agents/out.md", Path(path).read_text())


class TestRetryEscalation(unittest.TestCase):
    """A retry at the identical turn budget is a repeat, not a retry."""

    def _turns(self, retry_count: int) -> int:
        runner = agent_runner.AgentRunner()
        task = runner._build_task_json(
            task_id="2026-08-02-001",
            step={"role": "research", "retry_count": retry_count},
            run={"id": "abc12345", "project": "engram", "tier": 2, "task_raw": "x"},
            output_note="Projects/Engram/agents/out.md",
            prompt_file="~/raphael/tasks/x-prompt.md",
            task_description="x",
        )
        return task["max_turns"]

    def test_first_attempt_uses_the_configured_budget(self):
        base = agent_runner.resolve_model_route("research", 2)["max_turns"]
        self.assertEqual(self._turns(0), base)

    def test_retry_doubles_the_budget(self):
        self.assertEqual(self._turns(1), self._turns(0) * agent_runner.RETRY_TURN_MULTIPLIER)

    def test_escalation_is_capped(self):
        """A runaway retry loop must not buy unbounded turns."""
        self.assertLessEqual(self._turns(9), agent_runner.RETRY_MAX_TURNS_CAP)


class TestBlockedProtocolInPrompt(unittest.TestCase):
    """Every step prompt must tell the agent what to do when blocked — the
    agent in dec77da3 was never told, so it asked a question into the void."""

    def test_prompt_carries_the_protocol(self):
        runner = agent_runner.AgentRunner()
        prompt = runner._build_prompt(
            step={"role": "research"},
            run={
                "id": "abc12345",
                "project": "engram",
                "tier": 2,
                "branch": "feature/x",
                "task_raw": "x",
                "pipeline": [{"role": "research"}],
            },
            step_label="Step 1 of 1: research",
            step_index=0,
            task_description="x",
            output_note="Projects/Engram/agents/out.md",
            target_branch="main",
        )
        self.assertIn("## If you get blocked", prompt)
        self.assertIn("**Status:** blocked", prompt)
        self.assertIn("## Blocked", prompt)


if __name__ == "__main__":
    unittest.main()
