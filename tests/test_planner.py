"""
test_planner.py — Unit tests for engram/planner.py

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

# Make ~/engram importable without modifying sys.path permanently
ENGRAM_DIR = Path.home() / "engram"
sys.path.insert(0, str(ENGRAM_DIR))

from planner import (
    classify_task,
    _fallback_result,
    _extract_json,
    _is_nondev_task,
    _parse_explicit_target,
    _detect_project,
)
import project_registry as _pr

_TMP_REG = None


def setUpModule():
    """Install a deterministic project registry so planner routing tests don't
    depend on the host's ~/Projects (absent on CI)."""
    global _TMP_REG
    import tempfile

    _TMP_REG = tempfile.TemporaryDirectory()
    root = Path(_TMP_REG.name)
    projects, rb = root / "Projects", root / "RaphBrain"
    projects.mkdir()
    rb.mkdir()
    for n in ("arbiter", "alphabot", "vitre", "omegabot", "engram", "raphael"):
        (projects / n).mkdir()
    (rb / "Vitré").mkdir()
    _pr.set_test_registry(_pr.build_registry(projects, rb, extra_repos={}, detect_pr_base=False))


def tearDownModule():
    _pr.set_test_registry(None)
    _TMP_REG.cleanup()


class TestExplicitTarget(unittest.TestCase):
    """`on <project>, <task>` pins the project deterministically (real registry)."""

    def test_on_project_comma(self):
        proj, rest = _parse_explicit_target("on arbiter, fix the login test")
        self.assertEqual(proj, "arbiter")
        self.assertEqual(rest, "fix the login test")

    def test_colon_form_and_alias_accent(self):
        self.assertEqual(_parse_explicit_target("alphabot: rebalance is off")[0], "alphabot")
        self.assertEqual(_parse_explicit_target("on vitré, update hero")[0], "vitre")

    def test_no_target(self):
        self.assertEqual(_parse_explicit_target("just do a thing")[0], None)

    def test_detect_project_mention(self):
        self.assertEqual(_detect_project("the omegabot resolver is blind"), "omegabot")

    def test_pinned_project_survives_classification(self):
        # "check ..." hits a direct keyword → classified without Ollama (CI has none);
        # the explicit "on alphabot" prefix pins the project.
        r = classify_task("on alphabot, check the rebalance status")
        self.assertTrue(r.is_direct)
        self.assertEqual(r.project, "alphabot")


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
        """'check if engram is running' starts with DIRECT_KEYWORD — no pipeline."""
        with patch("planner.subprocess.run") as mock_run:
            result = classify_task("check if engram is running")
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

    def test_nondev_task_routes_direct_no_ollama(self):
        """Sales/outreach work must NOT enter a code pipeline (the 2026-07-11 bug):
        it routes to a single general agent (is_direct), never plan→coder→deployer."""
        task = "Utilize your skills to get me potential customers to warm call for my services"
        with patch("planner.subprocess.run") as mock_run:
            result = classify_task(task)
            mock_run.assert_not_called()
        self.assertTrue(result.is_direct)
        self.assertEqual(result.tier, 0)
        self.assertEqual(result.pipeline, [])

    def test_is_nondev_precision(self):
        # Non-code work → caught here.
        self.assertTrue(_is_nondev_task("get me potential customers to warm call"))
        self.assertTrue(_is_nondev_task("draft a blog post about MCP"))
        # Real code tasks that merely share adjacent words → NOT caught (go to Ollama).
        self.assertFalse(_is_nondev_task("fix the bug that leads to a crash"))
        self.assertFalse(_is_nondev_task("build the marketing page in React"))
        self.assertFalse(_is_nondev_task("migrate customers to the new postgres schema"))


class TestPlannerTier1(unittest.TestCase):
    """Tier 1 hotfix — single file change / bug fix."""

    @patch("planner.subprocess.run")
    def test_tier1_hotfix(self, mock_run):
        """Ollama returns Tier 1 → pipeline has coder, tester, deployer."""
        mock_run.return_value = _mock_subprocess_run(
            _make_ollama_json(1, "engram", branch="fix/typo-readme")
        )
        result = classify_task("Fix typo in engram README configuration section file")
        self.assertEqual(result.tier, 1)
        self.assertFalse(result.is_direct)
        self.assertEqual(result.project, "engram")
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
            self.assertIsNone(step.parallel_group, f"Tier 1 step {step.role} should be sequential")


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
        # "raph-ui" canonicalizes to "raphael" via the project registry alias.
        self.assertEqual(result.project, "raphael")
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


class TestPlannerNoPrivacyGate(unittest.TestCase):
    """Ollama runs locally, so 'arbiter'/'credentials' tasks classify normally now —
    the old gate that forced them into a Tier-2 fallback is gone."""

    @patch("planner.subprocess.run")
    def test_arbiter_task_reaches_ollama(self, mock_run):
        mock_run.return_value = _mock_subprocess_run(_make_ollama_json(2, "arbiter"))
        result = classify_task(
            "Implement a new investor dashboard feature for Arbiter with JWT auth and deployment"
        )
        mock_run.assert_called()  # Ollama WAS called — no bypass
        self.assertEqual(result.project, "arbiter")
        self.assertNotEqual(result.raw_ollama_response, "FALLBACK")


class TestPlannerFallback(unittest.TestCase):
    """Fallback behavior when Ollama returns invalid/missing JSON."""

    @patch("planner.subprocess.run")
    def test_invalid_json_falls_back_to_tier2(self, mock_run):
        """Ollama returning non-JSON → fallback to Tier 2."""
        mock_run.return_value = _mock_subprocess_run("I cannot classify this task, sorry.")
        result = classify_task(
            "Implement dark mode with persistent user preferences across sessions and devices"
        )
        self.assertEqual(result.tier, 2)
        self.assertFalse(result.is_direct)
        self.assertEqual(result.raw_ollama_response, "FALLBACK")

    @patch("planner.subprocess.run")
    def test_missing_required_fields_falls_back(self, mock_run):
        """JSON missing 'tier' key → fallback."""
        mock_run.return_value = _mock_subprocess_run('{"project": "engram"}')
        result = classify_task(
            "Refactor the engram logging module to support structured JSON output"
        )
        self.assertEqual(result.tier, 2)
        self.assertEqual(result.raw_ollama_response, "FALLBACK")

    @patch("planner.subprocess.run")
    def test_ollama_nonzero_returncode_falls_back(self, mock_run):
        """Ollama returning non-zero exit code → fallback."""
        mock_run.return_value = _mock_subprocess_run("", returncode=1)
        result = classify_task(
            "Implement new feature for engram system logging with rotation support"
        )
        self.assertEqual(result.tier, 2)
        self.assertEqual(result.raw_ollama_response, "FALLBACK")

    @patch("planner.subprocess.run")
    def test_project_heuristic_from_task_text(self, mock_run):
        """Fallback should pick up known project name from task text."""
        mock_run.return_value = _mock_subprocess_run("not valid json at all")
        result = classify_task(
            "Add new feature to the engram project for email-based task dispatch routing"
        )
        self.assertEqual(result.tier, 2)
        self.assertEqual(result.project, "engram")

    @patch("planner.subprocess.run")
    def test_unknown_role_in_pipeline_uses_tier_default(self, mock_run):
        """Ollama returning unknown role in pipeline → use tier default pipeline."""
        bad_pipeline = [{"role": "magic_agent", "parallel_group": None}]
        mock_run.return_value = _mock_subprocess_run(
            json.dumps(
                {
                    "tier": 2,
                    "project": "engram",
                    "branch_name": "feature/test",
                    "is_direct": False,
                    "pipeline": bad_pipeline,
                }
            )
        )
        result = classify_task(
            "Build a new feature for the engram project with comprehensive logging"
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

        self.assertEqual(_scaffold_project_name("scaffold new project: mynewapp"), "mynewapp")

    def test_scaffold_project_name_without_new(self):
        from planner import _scaffold_project_name

        self.assertEqual(_scaffold_project_name("scaffold project: coolapp"), "coolapp")

    def test_scaffold_project_name_case_insensitive(self):
        from planner import _scaffold_project_name

        self.assertEqual(_scaffold_project_name("Scaffold New Project: MyApp"), "myapp")

    def test_scaffold_project_name_with_hyphens(self):
        from planner import _scaffold_project_name

        self.assertEqual(_scaffold_project_name("scaffold new project: my-cool-api"), "my-cool-api")

    def test_scaffold_project_name_no_match(self):
        from planner import _scaffold_project_name

        self.assertIsNone(_scaffold_project_name("add dark mode to Arbiter dashboard feature"))

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
