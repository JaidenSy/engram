"""
test_output_validator.py — Tests for output_validator.py

Tests cover:
  1. Valid output for each role
  2. Missing required markers → returns False + reason string
  3. Output note does not exist → returns False
  4. Output note too short → returns False
  5. Unknown role → always passes (no requirements)
  6. Deployer: needs all three markers; missing any one fails
  7. Review: BLOCKED is also a valid verdict
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERMES_DIR = Path.home() / "hermes"
sys.path.insert(0, str(HERMES_DIR))

from output_validator import validate_step_output, ROLE_REQUIREMENTS


class TestValidateStepOutput(unittest.TestCase):
    """All tests use a temp directory patched over RAPHBRAIN."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._patcher = patch("output_validator.RAPHBRAIN", self.tmp_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def _write_note(self, rel_path: str, content: str) -> str:
        """Write a fake output note into the temp RaphBrain and return rel_path."""
        full = self.tmp_path / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        return rel_path

    # ------------------------------------------------------------------
    # Unknown role — always passes
    # ------------------------------------------------------------------

    def test_unknown_role_passes(self):
        note = self._write_note("Projects/x/agents/out.md", "anything " * 20)
        valid, reason = validate_step_output("nonexistent_role", note)
        self.assertTrue(valid)
        self.assertEqual(reason, "")

    # ------------------------------------------------------------------
    # Missing file
    # ------------------------------------------------------------------

    def test_missing_note_fails(self):
        valid, reason = validate_step_output("coder", "Projects/x/agents/missing.md")
        self.assertFalse(valid)
        self.assertIn("not found", reason)

    # ------------------------------------------------------------------
    # Too short
    # ------------------------------------------------------------------

    def test_too_short_note_fails(self):
        note = self._write_note("Projects/x/agents/short.md", "hi")
        valid, reason = validate_step_output("coder", note)
        self.assertFalse(valid)
        self.assertIn("too short", reason)

    # ------------------------------------------------------------------
    # Coder
    # ------------------------------------------------------------------

    def test_coder_valid_with_summary(self):
        content = "## Summary\n\nI changed foo.py to fix the thing.\n" * 5
        note = self._write_note("Projects/x/agents/coder.md", content)
        valid, reason = validate_step_output("coder", note)
        self.assertTrue(valid, reason)

    def test_coder_valid_with_files_modified(self):
        content = "## Files Modified\n\n- foo.py\n- bar.py\n" * 5
        note = self._write_note("Projects/x/agents/coder2.md", content)
        valid, reason = validate_step_output("coder", note)
        self.assertTrue(valid, reason)

    def test_coder_missing_any_marker_fails(self):
        content = "I did some stuff and changed things but forgot the headers.\n" * 5
        note = self._write_note("Projects/x/agents/coder3.md", content)
        valid, reason = validate_step_output("coder", note)
        self.assertFalse(valid)
        self.assertIn("coder", reason)

    # ------------------------------------------------------------------
    # Review
    # ------------------------------------------------------------------

    def test_review_approved_passes(self):
        content = "The code looks good.\n\nAPPROVED — no issues found.\n" * 3
        note = self._write_note("Projects/x/agents/review.md", content)
        valid, reason = validate_step_output("review", note)
        self.assertTrue(valid, reason)

    def test_review_blocked_passes(self):
        content = "🚫 BLOCKED: security issue in auth module.\n" * 5
        note = self._write_note("Projects/x/agents/review2.md", content)
        valid, reason = validate_step_output("review", note)
        self.assertTrue(valid, reason)

    def test_review_missing_verdict_fails(self):
        content = "The code looks fine but I did not write a verdict.\n" * 5
        note = self._write_note("Projects/x/agents/review3.md", content)
        valid, reason = validate_step_output("review", note)
        self.assertFalse(valid)
        self.assertIn("review", reason)

    # ------------------------------------------------------------------
    # Deployer — all three markers required
    # ------------------------------------------------------------------

    def test_deployer_all_markers_passes(self):
        content = (
            "## Deployer Output\n\n"
            "**PR URL:** https://github.com/JaidenSy/hermes/pull/5\n\n"
            "**Status:** opened\n"
        )
        note = self._write_note("Projects/x/agents/deployer.md", content)
        valid, reason = validate_step_output("deployer", note)
        self.assertTrue(valid, reason)

    def test_deployer_missing_pr_url_fails(self):
        content = "## Deployer Output\n\n**Status:** opened\n" * 3
        note = self._write_note("Projects/x/agents/deployer2.md", content)
        valid, reason = validate_step_output("deployer", note)
        self.assertFalse(valid)
        self.assertIn("**PR URL:**", reason)

    def test_deployer_missing_status_fails(self):
        content = (
            "## Deployer Output\n\n"
            "**PR URL:** https://github.com/JaidenSy/hermes/pull/6\n" * 3
        )
        note = self._write_note("Projects/x/agents/deployer3.md", content)
        valid, reason = validate_step_output("deployer", note)
        self.assertFalse(valid)
        self.assertIn("**Status:**", reason)

    def test_deployer_missing_header_fails(self):
        content = (
            "**PR URL:** https://github.com/JaidenSy/hermes/pull/7\n\n**Status:** opened\n"
            * 3
        )
        note = self._write_note("Projects/x/agents/deployer4.md", content)
        valid, reason = validate_step_output("deployer", note)
        self.assertFalse(valid)
        self.assertIn("## Deployer Output", reason)

    # ------------------------------------------------------------------
    # Plan / research / architect / tester / cleanup
    # ------------------------------------------------------------------

    def test_plan_valid(self):
        content = "## Plan\n\n1. Do this\n2. Do that\n" * 5
        note = self._write_note("Projects/x/agents/plan.md", content)
        valid, _ = validate_step_output("plan", note)
        self.assertTrue(valid)

    def test_research_valid_with_results(self):
        content = "## Results\n\nFound several relevant patterns.\n" * 5
        note = self._write_note("Projects/x/agents/research.md", content)
        valid, _ = validate_step_output("research", note)
        self.assertTrue(valid)

    def test_architect_valid_with_design(self):
        content = "## Design\n\nWe will use a layered architecture.\n" * 5
        note = self._write_note("Projects/x/agents/architect.md", content)
        valid, _ = validate_step_output("architect", note)
        self.assertTrue(valid)

    def test_tester_valid(self):
        content = "## Test Results\n\n12/12 tests pass.\n" * 5
        note = self._write_note("Projects/x/agents/tester.md", content)
        valid, _ = validate_step_output("tester", note)
        self.assertTrue(valid)

    def test_cleanup_valid(self):
        content = "## Cleanup\n\nRemoved dead imports, fixed lint.\n" * 5
        note = self._write_note("Projects/x/agents/cleanup.md", content)
        valid, _ = validate_step_output("cleanup", note)
        self.assertTrue(valid)

    # ------------------------------------------------------------------
    # Role requirements completeness: all known roles have requirements
    # ------------------------------------------------------------------

    def test_all_pipeline_roles_have_requirements(self):
        """Every pipeline role that writes output should have a validator entry."""
        expected_roles = {
            "plan",
            "research",
            "architect",
            "coder",
            "tester",
            "cleanup",
            "review",
            "deployer",
        }
        for role in expected_roles:
            self.assertIn(
                role,
                ROLE_REQUIREMENTS,
                f"Role '{role}' is missing from ROLE_REQUIREMENTS",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
