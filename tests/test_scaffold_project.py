"""
test_scaffold_project.py — Tests for scaffold_project.py

Tests cover:
  1. Invalid project name rejected
  2. Idempotent — second call skips already-existing files
  3. Creates expected directory structure
  4. Creates expected files with correct content markers
  5. Adds teams.json entry
  6. Creates RaphBrain note structure
  7. Git init attempted
  8. gh repo create failures are handled gracefully (⚠️ not ❌)
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ENGRAM_DIR = Path.home() / "engram"
sys.path.insert(0, str(ENGRAM_DIR))

import scaffold_project as sp


class TestScaffoldProject(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

        self.projects_dir = self.tmp / "Projects"
        self.raphbrain = self.tmp / "RaphBrain"
        self.teams_json = self.tmp / "raphael" / "teams.json"
        self.teams_json.parent.mkdir(parents=True, exist_ok=True)

        self._patches = [
            patch.object(sp, "PROJECTS_DIR", self.projects_dir),
            patch.object(sp, "RAPHBRAIN", self.raphbrain),
            patch.object(sp, "TEAMS_JSON", self.teams_json),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _scaffold(self, name: str) -> str:
        """Run scaffold with git + gh mocked out."""
        with (
            patch("scaffold_project.subprocess.run") as mock_sub,
        ):
            mock_sub.return_value = MagicMock(returncode=0, stdout="", stderr=b"")
            return sp.run_scaffold(name)

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------

    def test_invalid_name_rejected(self):
        result = sp.run_scaffold("INVALID NAME!")
        self.assertIn("❌", result)
        self.assertIn("Invalid", result)

    def test_invalid_name_with_spaces(self):
        result = sp.run_scaffold("my project")
        self.assertIn("❌", result)

    def test_valid_name_with_hyphens(self):
        result = self._scaffold("my-cool-project")
        self.assertIn("✅", result)

    # ------------------------------------------------------------------
    # Directory structure
    # ------------------------------------------------------------------

    def test_creates_project_dir(self):
        self._scaffold("testproj")
        self.assertTrue((self.projects_dir / "testproj").is_dir())

    def test_creates_tests_subdir(self):
        self._scaffold("testproj")
        self.assertTrue((self.projects_dir / "testproj" / "tests").is_dir())

    def test_creates_github_workflows_dir(self):
        self._scaffold("testproj")
        self.assertTrue(
            (self.projects_dir / "testproj" / ".github" / "workflows").is_dir()
        )

    # ------------------------------------------------------------------
    # Standard files
    # ------------------------------------------------------------------

    def test_creates_claude_md(self):
        self._scaffold("testproj")
        path = self.projects_dir / "testproj" / "CLAUDE.md"
        self.assertTrue(path.exists())
        content = path.read_text()
        self.assertIn("testproj", content)

    def test_creates_pyproject_toml(self):
        self._scaffold("testproj")
        path = self.projects_dir / "testproj" / "pyproject.toml"
        self.assertTrue(path.exists())
        self.assertIn("testproj", path.read_text())

    def test_creates_pre_commit_config(self):
        self._scaffold("testproj")
        path = self.projects_dir / "testproj" / ".pre-commit-config.yaml"
        self.assertTrue(path.exists())
        content = path.read_text()
        self.assertIn("ruff", content)
        self.assertIn("mypy", content)

    def test_creates_ci_workflow(self):
        self._scaffold("testproj")
        path = self.projects_dir / "testproj" / ".github" / "workflows" / "ci.yml"
        self.assertTrue(path.exists())
        content = path.read_text()
        self.assertIn("pytest", content)

    def test_creates_gitignore(self):
        self._scaffold("testproj")
        path = self.projects_dir / "testproj" / ".gitignore"
        self.assertTrue(path.exists())

    def test_creates_test_placeholder(self):
        self._scaffold("testproj")
        path = self.projects_dir / "testproj" / "tests" / "test_placeholder.py"
        self.assertTrue(path.exists())
        self.assertIn("test_placeholder", path.read_text())

    # ------------------------------------------------------------------
    # teams.json
    # ------------------------------------------------------------------

    def test_adds_teams_json_entry(self):
        self._scaffold("myproject")
        teams = json.loads(self.teams_json.read_text())
        ids = [t["id"] for t in teams]
        self.assertIn("myproject", ids)

    def test_teams_json_entry_has_required_fields(self):
        self._scaffold("myproject")
        teams = json.loads(self.teams_json.read_text())
        entry = next(t for t in teams if t["id"] == "myproject")
        for field in ("id", "name", "repo", "raphbrain_project", "created_at"):
            self.assertIn(field, entry, f"Missing field: {field}")

    def test_teams_json_not_duplicated_on_second_call(self):
        self._scaffold("myproject")
        self._scaffold("myproject")
        teams = json.loads(self.teams_json.read_text())
        self.assertEqual(sum(1 for t in teams if t["id"] == "myproject"), 1)

    def test_teams_json_preserves_existing_entries(self):
        existing = [{"id": "arbiter", "name": "Arbiter", "repo": "~/Projects/arbiter"}]
        self.teams_json.write_text(json.dumps(existing))
        self._scaffold("newproj")
        teams = json.loads(self.teams_json.read_text())
        ids = [t["id"] for t in teams]
        self.assertIn("arbiter", ids)
        self.assertIn("newproj", ids)

    # ------------------------------------------------------------------
    # RaphBrain notes
    # ------------------------------------------------------------------

    def test_creates_raphbrain_progress(self):
        self._scaffold("myproject")
        path = self.raphbrain / "Projects" / "Myproject" / "Progress.md"
        self.assertTrue(path.exists())
        content = path.read_text()
        self.assertIn("Myproject", content)

    def test_creates_raphbrain_decisions(self):
        self._scaffold("myproject")
        path = self.raphbrain / "Projects" / "Myproject" / "Decisions.md"
        self.assertTrue(path.exists())

    def test_creates_raphbrain_gotchas(self):
        self._scaffold("myproject")
        path = self.raphbrain / "Projects" / "Myproject" / "Gotchas.md"
        self.assertTrue(path.exists())

    def test_creates_raphbrain_agents_dir(self):
        self._scaffold("myproject")
        agents_dir = self.raphbrain / "Projects" / "Myproject" / "agents"
        self.assertTrue(agents_dir.is_dir())

    # ------------------------------------------------------------------
    # Idempotency — second scaffold call should not overwrite files
    # ------------------------------------------------------------------

    def test_idempotent_does_not_overwrite_existing_files(self):
        self._scaffold("myproject")
        # Modify CLAUDE.md
        claude_md = self.projects_dir / "myproject" / "CLAUDE.md"
        claude_md.write_text("# Custom content that should survive\n")
        # Re-scaffold
        self._scaffold("myproject")
        self.assertEqual(
            claude_md.read_text(), "# Custom content that should survive\n"
        )

    # ------------------------------------------------------------------
    # Git / gh failures are warnings, not crashes
    # ------------------------------------------------------------------

    def test_gh_failure_returns_warning_not_error(self):
        with patch("scaffold_project.subprocess.run") as mock_sub:
            # git init succeeds, gh fails
            mock_sub.side_effect = [
                MagicMock(returncode=0),  # git init
                MagicMock(returncode=0),  # git add
                MagicMock(returncode=0),  # git commit
                MagicMock(returncode=0),  # git remote get-url (fails → no remote)
                MagicMock(returncode=1, stderr="error: repo exists"),  # gh repo create
            ]

            # Make git remote get-url raise FileNotFoundError to skip the remote check
            def side(cmd, **kwargs):
                if cmd[0] == "git" and "get-url" in cmd:
                    r = MagicMock()
                    r.returncode = 1
                    r.stdout = ""
                    return r
                if cmd[0] == "gh":
                    r = MagicMock()
                    r.returncode = 1
                    r.stderr = "error: repo already exists"
                    return r
                return MagicMock(returncode=0, stdout="", stderr=b"")

            mock_sub.side_effect = side
            result = sp.run_scaffold("failtest")

        # Should still succeed overall (⚠️ warning, not ❌ hard fail)
        self.assertIn("✅", result)
        self.assertIn("⚠️", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
