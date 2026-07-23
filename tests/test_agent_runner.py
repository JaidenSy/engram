"""
test_agent_runner.py — Integration tests for engram/agent_runner.py

Tests cover:
  1. dispatch_step() writes valid task JSON with all required fields
  2. Task JSON contains engram_run_id and engram_step_role
  3. Prompt file is written and contains role name + task description
  4. _next_task_id() returns correct format
  5. _find_step_index() returns correct index
  6. extract_pr_url() extracts GitHub PR URL from text
  7. Cleanup: task JSON and prompt file are removed after each test
"""

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ENGRAM_DIR = Path.home() / "engram"
sys.path.insert(0, str(ENGRAM_DIR))


def _make_run(
    run_id: str = "abc12345",
    project: str = "engram",
    tier: int = 2,
    branch: str = "feature/test",
    pipeline: list = None,
) -> dict:
    """Build a minimal run dict mimicking what RunEngine.create_run() returns."""
    if pipeline is None:
        pipeline = [
            {
                "role": "coder",
                "status": "pending",
                "parallel_group": None,
                "task_id": None,
                "started_at": None,
                "completed_at": None,
                "output_path": None,
            },
            {
                "role": "tester",
                "status": "pending",
                "parallel_group": None,
                "task_id": None,
                "started_at": None,
                "completed_at": None,
                "output_path": None,
            },
        ]
    return {
        "id": run_id,
        "task_raw": "Add dark mode toggle to dashboard settings panel",
        "project": project,
        "tier": tier,
        "branch": branch,
        "pipeline": pipeline,
        "status": "running",
        "created_at": "2026-06-07T12:00:00Z",
        "completed_at": None,
        "pr_url": None,
    }


class TestAgentRunnerDispatch(unittest.TestCase):
    """Test dispatch_step() writes correct task JSON and prompt file."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_tasks = Path(self._tmpdir.name)

        import agent_runner as ar_mod

        self._orig_tasks_dir = ar_mod.TASKS_DIR
        ar_mod.TASKS_DIR = self.tmp_tasks
        self.ar_mod = ar_mod

        from agent_runner import AgentRunner

        self.runner = AgentRunner()

    def tearDown(self):
        self.ar_mod.TASKS_DIR = self._orig_tasks_dir
        self._tmpdir.cleanup()

    def _dispatch_with_mock_tmux(self, step, run, task_description="Test task description"):
        """Helper: dispatch_step with tmux subprocess mocked out."""
        with patch.object(self.runner, "_spawn_run_agent") as mock_spawn:
            mock_spawn.return_value = None
            task_id = self.runner.dispatch_step(
                step=step,
                run=run,
                task_description=task_description,
            )
        return task_id

    def test_dispatch_writes_task_json(self):
        """dispatch_step() must write a task JSON file to TASKS_DIR."""
        run = _make_run()
        step = run["pipeline"][0]  # coder step
        task_id = self._dispatch_with_mock_tmux(step, run)

        task_file = self.tmp_tasks / f"{task_id}.json"
        self.assertTrue(task_file.exists(), f"Task JSON not found: {task_file}")

    def test_task_json_required_fields(self):
        """Task JSON must contain: id, prompt, status=pending, engram_run_id, engram_step_role."""
        run = _make_run()
        step = run["pipeline"][0]  # coder step
        task_description = "Add dark mode toggle to dashboard"
        task_id = self._dispatch_with_mock_tmux(step, run, task_description)

        task_file = self.tmp_tasks / f"{task_id}.json"
        task = json.loads(task_file.read_text())

        # Required fields
        self.assertIn("id", task)
        self.assertEqual(task["id"], task_id)
        self.assertEqual(task["status"], "pending")
        self.assertIn("engram_run_id", task)
        self.assertEqual(task["engram_run_id"], run["id"])
        self.assertIn("engram_step_role", task)
        self.assertEqual(task["engram_step_role"], "coder")
        self.assertIn("description", task)
        self.assertIn("role", task)
        self.assertIn("project", task)
        self.assertIn("created_at", task)

    def test_task_json_status_is_pending(self):
        """Freshly written task JSON must have status=pending."""
        run = _make_run()
        step = run["pipeline"][0]
        task_id = self._dispatch_with_mock_tmux(step, run)

        task = json.loads((self.tmp_tasks / f"{task_id}.json").read_text())
        self.assertEqual(task["status"], "pending")

    def test_prompt_file_written(self):
        """dispatch_step() must write a -prompt.md file alongside the task JSON."""
        run = _make_run()
        step = run["pipeline"][0]
        task_id = self._dispatch_with_mock_tmux(step, run)

        prompt_file = self.tmp_tasks / f"{task_id}-prompt.md"
        self.assertTrue(prompt_file.exists(), f"Prompt file not found: {prompt_file}")

    def test_prompt_contains_role_name(self):
        """Prompt file must mention the role name."""
        run = _make_run()
        step = run["pipeline"][0]  # coder
        task_id = self._dispatch_with_mock_tmux(step, run, "Build new API endpoint")

        prompt = (self.tmp_tasks / f"{task_id}-prompt.md").read_text()
        # The role name should appear in the prompt (as step label or role template)
        self.assertIn("coder", prompt.lower())

    def test_prompt_contains_task_description(self):
        """Prompt file must contain the task description."""
        run = _make_run()
        step = run["pipeline"][0]
        task_description = "Unique test task description for verification"
        task_id = self._dispatch_with_mock_tmux(step, run, task_description)

        prompt = (self.tmp_tasks / f"{task_id}-prompt.md").read_text()
        self.assertIn(task_description, prompt)

    def test_dispatch_tester_role(self):
        """dispatch_step() works for tester role and generates correct engram_step_role."""
        run = _make_run()
        step = run["pipeline"][1]  # tester step
        task_id = self._dispatch_with_mock_tmux(step, run)

        task = json.loads((self.tmp_tasks / f"{task_id}.json").read_text())
        self.assertEqual(task["engram_step_role"], "tester")

    def test_dispatch_spawns_run_agent(self):
        """dispatch_step() must call _spawn_run_agent with the correct task_id."""
        run = _make_run()
        step = run["pipeline"][0]
        with patch.object(self.runner, "_spawn_run_agent") as mock_spawn:
            mock_spawn.return_value = None
            task_id = self.runner.dispatch_step(
                step=step,
                run=run,
                task_description="Build dark mode toggle",
            )
            mock_spawn.assert_called_once()
            call_args = mock_spawn.call_args
            # First arg should be the task_id
            self.assertEqual(call_args[0][0], task_id)

    def test_next_task_id_concurrent_unique(self):
        """Parallel-group dispatches must not collide on the same YYYY-MM-DD-NNN."""
        import threading as _t

        ids, lock = [], _t.Lock()

        def grab():
            tid = self.runner._next_task_id()
            with lock:
                ids.append(tid)

        threads = [_t.Thread(target=grab) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(ids), len(set(ids)), f"collision: {ids}")


class TestAgentRunnerHelpers(unittest.TestCase):
    """Test helper methods."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_tasks = Path(self._tmpdir.name)

        import agent_runner as ar_mod

        self._orig_tasks_dir = ar_mod.TASKS_DIR
        ar_mod.TASKS_DIR = self.tmp_tasks
        self.ar_mod = ar_mod

        from agent_runner import AgentRunner

        self.runner = AgentRunner()

    def tearDown(self):
        self.ar_mod.TASKS_DIR = self._orig_tasks_dir
        self._tmpdir.cleanup()

    def test_next_task_id_format(self):
        """_next_task_id() must return YYYY-MM-DD-NNN format."""
        task_id = self.runner._next_task_id()
        self.assertRegex(
            task_id,
            r"^\d{4}-\d{2}-\d{2}-\d{3}$",
            f"task_id {task_id!r} does not match YYYY-MM-DD-NNN",
        )
        today = date.today().isoformat()
        self.assertTrue(task_id.startswith(today))

    def test_next_task_id_increments(self):
        """Creating task files should increment the sequence number."""
        today = date.today().isoformat()
        id1 = self.runner._next_task_id()
        # Simulate writing a task file so count increments
        (self.tmp_tasks / f"{id1}.json").write_text("{}")
        id2 = self.runner._next_task_id()
        seq1 = int(id1.split("-")[-1])
        seq2 = int(id2.split("-")[-1])
        self.assertEqual(seq2, seq1 + 1)

    def test_find_step_index_by_role_and_status(self):
        """_find_step_index fallback matches role+status=pending."""
        run = _make_run()
        pipeline = run["pipeline"]
        tester_step = pipeline[1]  # tester
        idx = self.runner._find_step_index(tester_step, pipeline)
        self.assertEqual(idx, 1)

    def test_find_step_index_returns_0_on_no_match(self):
        """When step not found, returns 0."""
        run = _make_run()
        pipeline = run["pipeline"]
        ghost_step = {"role": "nonexistent", "status": "pending"}
        idx = self.runner._find_step_index(ghost_step, pipeline)
        self.assertEqual(idx, 0)

    def test_extract_pr_url_from_text(self):
        """extract_pr_url() extracts a GitHub PR URL from output note content."""
        with tempfile.TemporaryDirectory() as tmpdir:
            raphbrain_tmp = Path(tmpdir)
            note_path = raphbrain_tmp / "output.md"
            note_path.write_text(
                "## Deployer Output\n"
                "**PR URL:** https://github.com/JaidenSy/Arbiter/pull/99\n"
                "**Status:** opened\n"
            )

            import agent_runner as ar_mod

            orig_raphbrain = ar_mod.RAPHBRAIN
            ar_mod.RAPHBRAIN = raphbrain_tmp

            from agent_runner import AgentRunner

            runner = AgentRunner()
            url = runner.extract_pr_url("output.md")

            ar_mod.RAPHBRAIN = orig_raphbrain

        self.assertEqual(url, "https://github.com/JaidenSy/Arbiter/pull/99")

    def test_extract_pr_url_returns_none_if_missing(self):
        """extract_pr_url() returns None when no URL in note."""
        with tempfile.TemporaryDirectory() as tmpdir:
            raphbrain_tmp = Path(tmpdir)
            note_path = raphbrain_tmp / "no-url.md"
            note_path.write_text("## Deployer Output\nNo URL here.\n")

            import agent_runner as ar_mod

            orig_raphbrain = ar_mod.RAPHBRAIN
            ar_mod.RAPHBRAIN = raphbrain_tmp

            from agent_runner import AgentRunner

            runner = AgentRunner()
            url = runner.extract_pr_url("no-url.md")

            ar_mod.RAPHBRAIN = orig_raphbrain

        self.assertIsNone(url)

    def test_extract_pr_url_returns_none_if_file_missing(self):
        """extract_pr_url() returns None gracefully when the file doesn't exist."""
        from agent_runner import AgentRunner

        runner = AgentRunner()
        url = runner.extract_pr_url("nonexistent/path/output.md")
        self.assertIsNone(url)


class TestAgentRunnerTargetBranch(unittest.TestCase):
    """Test the target_branch logic in dispatch_step."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_tasks = Path(self._tmpdir.name)

        import agent_runner as ar_mod

        self._orig_tasks_dir = ar_mod.TASKS_DIR
        ar_mod.TASKS_DIR = self.tmp_tasks
        self.ar_mod = ar_mod

        from agent_runner import AgentRunner

        self.runner = AgentRunner()

    def tearDown(self):
        self.ar_mod.TASKS_DIR = self._orig_tasks_dir
        self._tmpdir.cleanup()

    @patch("agent_runner.pr_base", return_value="main")
    def test_target_branch_is_repo_default_main(self, _pb):
        """Target branch = the repo's default branch (origin HEAD) via the registry."""
        run = _make_run(project="engram")
        step = run["pipeline"][0]
        with patch.object(self.runner, "_spawn_run_agent"):
            task_id = self.runner.dispatch_step(step=step, run=run, task_description="test")
        prompt = (self.tmp_tasks / f"{task_id}-prompt.md").read_text()
        self.assertIn("main", prompt)

    @patch("agent_runner.pr_base", return_value="develop")
    def test_target_branch_is_repo_default_develop(self, _pb):
        """A repo whose default is develop (e.g. arbiter) targets develop."""
        run = _make_run(project="arbiter")
        step = run["pipeline"][0]
        with patch.object(self.runner, "_spawn_run_agent"):
            task_id = self.runner.dispatch_step(step=step, run=run, task_description="test")
        prompt = (self.tmp_tasks / f"{task_id}-prompt.md").read_text()
        self.assertIn("develop", prompt)


class TestModelProbeDowngrade(unittest.TestCase):
    """probe_models() must keep a run alive when a model tier (e.g. Fable) is gone."""

    def tearDown(self):
        import agent_runner as ar

        ar._ALIAS_DOWNGRADE.clear()  # don't leak downgrade state into other tests

    def _probe(self, available: dict, configured: set):
        import agent_runner as ar

        with (
            patch.object(ar, "_model_available", lambda a: available.get(a, False)),
            patch.object(ar, "configured_aliases", lambda: configured),
        ):
            return ar.probe_models(), ar

    def test_fable_gone_downgrades_to_opus(self):
        avail = {"fable": False, "opus": True, "sonnet": True, "haiku": True}
        m, ar = self._probe(avail, {"fable", "opus", "sonnet", "haiku"})
        self.assertEqual(m.get("fable"), "opus")
        self.assertEqual(ar.apply_downgrade("fable"), "opus")
        self.assertEqual(ar.apply_downgrade("opus"), "opus")  # healthy → untouched

    def test_cascades_to_next_available_tier(self):
        # fable AND opus gone → both route to the strongest that's up (sonnet).
        avail = {"fable": False, "opus": False, "sonnet": True, "haiku": True}
        m, _ = self._probe(avail, {"fable", "opus", "sonnet", "haiku"})
        self.assertEqual(m.get("fable"), "sonnet")
        self.assertEqual(m.get("opus"), "sonnet")

    def test_all_down_changes_nothing(self):
        # claude CLI down / logged out → probes all fail → don't rewrite routing.
        m, ar = self._probe({}, {"fable", "opus"})
        self.assertEqual(m, {})
        self.assertEqual(ar.apply_downgrade("fable"), "fable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
