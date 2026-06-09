"""
test_planner.py — Unit tests for hermes/planner.py

Tests cover:
  1. Tier 1 hotfix detection
  2. Tier 2 feature classification
  3. Tier 3 architecture classification
  4. Direct/status query shortcut (no Ollama call)
  5. Privacy keyword task (bypass Ollama → fallback)
  6. Fallback on invalid JSON from Ollama
  7. Short task → is_direct=True without calling Ollama
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make ~/hermes importable without modifying sys.path permanently
HERMES_DIR = Path.home() / "hermes"
sys.path.insert(0, str(HERMES_DIR))

from planner import (
    classify_task,
    _has_privacy_keywords,
    _fallback_result,
    _extract_json,
    _sanitize_for_ollama,
)


def _make_ollama_json(
    tier: int,
    project: str,
    is_direct: bool = False,
    branch: str = "feature/test",
    pipeline=None,
) -> str:
    """Build a well-formed Ollama JSON response string."""
    if pipeline is None:
        if tier == 1:
            pipeline = [
                {"role": "coder", "parallel_group": None},
                {"role": "tester", "parallel_group": None},
                {"role": "deployer", "parallel_group": None},
            ]
        elif tier == 3:
            pipeline = [
                {"role": "plan", "parallel_group": None},
                {"role": "research", "parallel_group": 1},
                {"role": "architect", "parallel_group": 1},
                {"role": "coder", "parallel_group": None},
                {"role": "tester", "parallel_group": 2},
                {"role": "cleanup", "parallel_group": 2},
                {"role": "review", "parallel_group": None},
                {"role": "deployer", "parallel_group": None},
            ]
        else:
            pipeline = [
                {"role": "plan", "parallel_group": None},
                {"role": "coder", "parallel_group": None},
                {"role": "tester", "parallel_group": None},
                {"role": "cleanup", "parallel_group": None},
                {"role": "review", "parallel_group": None},
                {"role": "deployer", "parallel_group": None},
            ]

    return json.dumps(
        {
            "tier": tier,
            "project": project,
            "branch_name": branch,
            "is_direct": is_direct,
            "pipeline": pipeline,
        }
    )


# ---------------------------------------------------------------------------
# Subprocess mock helper
# ---------------------------------------------------------------------------


def _mock_subprocess_run(stdout: str, returncode: int = 0):
    """Return a mock that patches subprocess.run to return controlled output."""
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = ""
    return mock


class TestPlannerDirectTask(unittest.TestCase):
    """Tests that short / status tasks are handled without calling Ollama."""

    def test_short_task_is_direct_no_ollama(self):
        """Task under 40 chars should be classified as direct without calling Ollama."""
        with patch("planner.subprocess.run") as mock_run:
            result = classify_task("status")
            mock_run.assert_not_called()
        self.assertTrue(result.is_direct)
        self.assertEqual(result.tier, 0)
        self.assertEqual(result.pipeline, [])

    def test_check_keyword_is_direct(self):
        """'check if hermes is running' starts with DIRECT_KEYWORD — no pipeline."""
        with patch("planner.subprocess.run") as mock_run:
            result = classify_task("check if hermes is running")
            mock_run.assert_not_called()
        self.assertTrue(result.is_direct)

    def test_no_action_keyword_is_direct(self):
        """
        A task with no action keyword but > 40 chars should still be direct
        since _is_direct_task returns True when has_action is False.
        """
        task = "What is the current memory usage of the server right now"
        with patch("planner.subprocess.run") as mock_run:
            result = classify_task(task)
            mock_run.assert_not_called()
        self.assertTrue(result.is_direct)


class TestPlannerTier1(unittest.TestCase):
    """Tier 1 hotfix — single file change / bug fix."""

    @patch("planner.subprocess.run")
    def test_tier1_hotfix(self, mock_run):
        """Ollama returns Tier 1 → pipeline has coder, tester, deployer."""
        mock_run.return_value = _mock_subprocess_run(
            _make_ollama_json(1, "hermes", branch="fix/typo-readme")
        )
        result = classify_task("Fix typo in hermes README configuration section file")
        self.assertEqual(result.tier, 1)
        self.assertFalse(result.is_direct)
        self.assertEqual(result.project, "hermes")
        roles = [s.role for s in result.pipeline]
        self.assertIn("coder", roles)
        self.assertIn("tester", roles)
        self.assertIn("deployer", roles)
        # Tier 1 should NOT have plan or architect
        self.assertNotIn("plan", roles)

    @patch("planner.subprocess.run")
    def test_tier1_pipeline_all_sequential(self, mock_run):
        """All Tier 1 steps should have parallel_group=None."""
        mock_run.return_value = _mock_subprocess_run(_make_ollama_json(1, "raph-ui"))
        result = classify_task("Fix typo in raph-ui dashboard footer component label")
        for step in result.pipeline:
            self.assertIsNone(
                step.parallel_group, f"Tier 1 step {step.role} should be sequential"
            )


class TestPlannerTier2(unittest.TestCase):
    """Tier 2 feature — multi-file, needs planning."""

    @patch("planner.subprocess.run")
    def test_tier2_feature(self, mock_run):
        """Ollama returns Tier 2 → pipeline includes plan, coder, tester, cleanup, review, deployer."""
        mock_run.return_value = _mock_subprocess_run(
            _make_ollama_json(2, "raph-ui", branch="feature/dark-mode-toggle")
        )
        result = classify_task(
            "Add dark mode toggle to raph-ui dashboard settings panel with persistence"
        )
        self.assertEqual(result.tier, 2)
        self.assertFalse(result.is_direct)
        self.assertEqual(result.project, "raph-ui")
        self.assertTrue(result.branch_name.startswith("feature/"))
        roles = [s.role for s in result.pipeline]
        for expected in ("plan", "coder", "tester", "cleanup", "review", "deployer"):
            self.assertIn(expected, roles)


class TestPlannerTier3(unittest.TestCase):
    """Tier 3 architecture — new system, major refactor."""

    @patch("planner.subprocess.run")
    def test_tier3_architecture(self, mock_run):
        """Ollama returns Tier 3 → pipeline has parallel groups for research+architect and tester+cleanup."""
        mock_run.return_value = _mock_subprocess_run(
            _make_ollama_json(3, "nexvault", branch="feature/auth-system")
        )
        result = classify_task(
            "Build a new authentication system with JWT, refresh tokens, and OAuth2 for the nexvault platform with full test coverage and documentation"
        )
        self.assertEqual(result.tier, 3)
        self.assertFalse(result.is_direct)
        roles = [s.role for s in result.pipeline]
        # Must have parallel research + architect
        self.assertIn("research", roles)
        self.assertIn("architect", roles)
        # Verify parallel_group is set for research+architect
        research_step = next(s for s in result.pipeline if s.role == "research")
        architect_step = next(s for s in result.pipeline if s.role == "architect")
        self.assertIsNotNone(research_step.parallel_group)
        self.assertIsNotNone(architect_step.parallel_group)
        self.assertEqual(research_step.parallel_group, architect_step.parallel_group)


class TestPlannerPrivacyKeyword(unittest.TestCase):
    """Privacy-keyword tasks must bypass Ollama entirely."""

    @patch("planner.subprocess.run")
    def test_privacy_task_bypasses_ollama(self, mock_run):
        """Task containing 'arbiter' should not call Ollama, should return fallback result."""
        result = classify_task(
            "Implement new investor dashboard feature for Arbiter with JWT authentication and deployment"
        )
        mock_run.assert_not_called()
        # Fallback is Tier 2
        self.assertEqual(result.tier, 2)
        self.assertFalse(result.is_direct)
        self.assertEqual(result.raw_ollama_response, "FALLBACK")

    @patch("planner.subprocess.run")
    def test_credentials_keyword_bypasses_ollama(self, mock_run):
        """Task containing 'credentials' bypasses Ollama."""
        result = classify_task(
            "Update the API credentials rotation script and implement automated key refresh"
        )
        mock_run.assert_not_called()
        self.assertEqual(result.tier, 2)
        self.assertEqual(result.raw_ollama_response, "FALLBACK")


class TestPlannerFallback(unittest.TestCase):
    """Fallback behavior when Ollama returns invalid/missing JSON."""

    @patch("planner.subprocess.run")
    def test_invalid_json_falls_back_to_tier2(self, mock_run):
        """Ollama returning non-JSON → fallback to Tier 2."""
        mock_run.return_value = _mock_subprocess_run(
            "I cannot classify this task, sorry."
        )
        result = classify_task(
            "Implement dark mode with persistent user preferences across sessions and devices"
        )
        self.assertEqual(result.tier, 2)
        self.assertFalse(result.is_direct)
        self.assertEqual(result.raw_ollama_response, "FALLBACK")

    @patch("planner.subprocess.run")
    def test_missing_required_fields_falls_back(self, mock_run):
        """JSON missing 'tier' key → fallback."""
        mock_run.return_value = _mock_subprocess_run('{"project": "hermes"}')
        result = classify_task(
            "Refactor the hermes logging module to support structured JSON output"
        )
        self.assertEqual(result.tier, 2)
        self.assertEqual(result.raw_ollama_response, "FALLBACK")

    @patch("planner.subprocess.run")
    def test_ollama_nonzero_returncode_falls_back(self, mock_run):
        """Ollama returning non-zero exit code → fallback."""
        mock_run.return_value = _mock_subprocess_run("", returncode=1)
        result = classify_task(
            "Implement new feature for hermes system logging with rotation support"
        )
        self.assertEqual(result.tier, 2)
        self.assertEqual(result.raw_ollama_response, "FALLBACK")

    @patch("planner.subprocess.run")
    def test_project_heuristic_from_task_text(self, mock_run):
        """Fallback should pick up known project name from task text."""
        mock_run.return_value = _mock_subprocess_run("not valid json at all")
        result = classify_task(
            "Add new feature to the hermes project for email-based task dispatch routing"
        )
        self.assertEqual(result.tier, 2)
        self.assertEqual(result.project, "hermes")

    @patch("planner.subprocess.run")
    def test_unknown_role_in_pipeline_uses_tier_default(self, mock_run):
        """Ollama returning unknown role in pipeline → use tier default pipeline."""
        bad_pipeline = [{"role": "magic_agent", "parallel_group": None}]
        mock_run.return_value = _mock_subprocess_run(
            json.dumps(
                {
                    "tier": 2,
                    "project": "hermes",
                    "branch_name": "feature/test",
                    "is_direct": False,
                    "pipeline": bad_pipeline,
                }
            )
        )
        result = classify_task(
            "Build a new feature for the hermes project with comprehensive logging"
        )
        self.assertEqual(result.tier, 2)
        roles = [s.role for s in result.pipeline]
        # Should have fallen back to tier2 default pipeline
        self.assertIn("plan", roles)
        self.assertIn("coder", roles)


class TestPlannerHelpers(unittest.TestCase):
    """Test internal helper functions."""

    def test_extract_json_strips_markdown_fences(self):
        raw = '```json\n{"tier": 2, "project": "test", "pipeline": []}\n```'
        data = _extract_json(raw)
        self.assertEqual(data["tier"], 2)

    def test_extract_json_raises_on_no_json(self):
        with self.assertRaises(ValueError):
            _extract_json("No JSON here at all, just text.")

    def test_sanitize_removes_privacy_keywords(self):
        text = "Update arbiter with new investor strategy and credentials rotation"
        sanitized = _sanitize_for_ollama(text)
        for kw in ("arbiter", "investor", "strategy", "credentials"):
            self.assertNotIn(kw.lower(), sanitized.lower())

    def test_has_privacy_keywords_case_insensitive(self):
        self.assertTrue(_has_privacy_keywords("ARBITER is great"))
        self.assertTrue(_has_privacy_keywords("My investor meeting"))
        self.assertFalse(_has_privacy_keywords("hello world"))

    def test_fallback_result_always_tier2(self):
        r = _fallback_result("some arbitrary task text")
        self.assertEqual(r.tier, 2)
        self.assertFalse(r.is_direct)
        roles = [s.role for s in r.pipeline]
        self.assertIn("plan", roles)


class TestScaffoldDetection(unittest.TestCase):
    """Tests for scaffold task detection in planner."""

    def test_scaffold_project_name_basic(self):
        from planner import _scaffold_project_name

        self.assertEqual(
            _scaffold_project_name("scaffold new project: mynewapp"), "mynewapp"
        )

    def test_scaffold_project_name_without_new(self):
        from planner import _scaffold_project_name

        self.assertEqual(_scaffold_project_name("scaffold project: coolapp"), "coolapp")

    def test_scaffold_project_name_case_insensitive(self):
        from planner import _scaffold_project_name

        self.assertEqual(_scaffold_project_name("Scaffold New Project: MyApp"), "myapp")

    def test_scaffold_project_name_with_hyphens(self):
        from planner import _scaffold_project_name

        self.assertEqual(
            _scaffold_project_name("scaffold new project: my-cool-api"), "my-cool-api"
        )

    def test_scaffold_project_name_no_match(self):
        from planner import _scaffold_project_name

        self.assertIsNone(
            _scaffold_project_name("add dark mode to Arbiter dashboard feature")
        )

    def test_classify_scaffold_returns_scaffold_project(self):
        result = classify_task("scaffold new project: nexvault")
        self.assertIsNotNone(result.scaffold_project)
        self.assertEqual(result.scaffold_project, "nexvault")
        self.assertTrue(result.is_direct)
        self.assertEqual(result.tier, 0)

    def test_classify_scaffold_skips_pipeline(self):
        result = classify_task("scaffold new project: testapp")
        self.assertEqual(len(result.pipeline), 0)

    def test_classify_non_scaffold_has_no_scaffold_project(self):
        result = classify_task("status")
        self.assertIsNone(result.scaffold_project)


if __name__ == "__main__":
    unittest.main(verbosity=2)
