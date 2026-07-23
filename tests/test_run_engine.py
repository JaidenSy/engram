"""
test_run_engine.py — Unit tests for hermes/run_engine.py

Tests cover:
  1. State transitions: create_run → mark_step_started → mark_step_done → advance to next → final done
  2. Abort: create_run, abort_run → status=aborted, pending steps=skipped
  3. Parallel group: pipeline with two steps sharing parallel_group=1 → _find_next_pending_steps returns both
  4. One-active-run guard: second create_run while one active raises RuntimeError
  5. mark_step_failed → run.status=failed, step.status=failed
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make ~/hermes importable
HERMES_DIR = Path.home() / "hermes"
sys.path.insert(0, str(HERMES_DIR))


def _make_planner_result(tier=2, project="hermes", branch="feature/test", pipeline_steps=None):
    """Build a minimal PlannerResult-like object for testing."""
    from planner import PipelineStep, PlannerResult

    if pipeline_steps is None:
        pipeline_steps = [
            PipelineStep("coder", None),
            PipelineStep("tester", None),
            PipelineStep("deployer", None),
        ]
    return PlannerResult(
        tier=tier,
        project=project,
        branch_name=branch,
        pipeline=pipeline_steps,
        is_direct=False,
        raw_ollama_response="",
    )


class TestRunEngineWithTempDir(unittest.TestCase):
    """All tests use a temp directory for RUNS_DIR — never touch ~/hermes/runs/."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

        # Patch RUNS_DIR before importing RunEngine class
        import run_engine as re_mod

        self._orig_runs_dir = re_mod.RUNS_DIR
        re_mod.RUNS_DIR = self.tmp_path

        from run_engine import RunEngine

        self.RunEngine = RunEngine
        self.re_mod = re_mod

    def tearDown(self):
        self.re_mod.RUNS_DIR = self._orig_runs_dir
        self._tmpdir.cleanup()

    def _engine(self):
        # Represents the engine as it exists after daemon startup: recovery +
        # cleanup have run. Per-message construction is exercised via the bare
        # self.RunEngine() constructor (see test_construction_does_not_abort_running_run).
        eng = self.RunEngine()
        eng.startup_recover_and_cleanup()
        return eng

    # ------------------------------------------------------------------
    # create_run
    # ------------------------------------------------------------------

    def test_create_run_writes_json_with_pending_status(self):
        engine = self._engine()
        result = _make_planner_result()
        run = engine.create_run("test task text", result)

        self.assertEqual(run["status"], "pending")
        self.assertEqual(run["project"], "hermes")
        self.assertEqual(run["tier"], 2)
        self.assertIsNotNone(run["id"])
        self.assertEqual(len(run["pipeline"]), 3)

        # Verify file was actually written
        files = list(self.tmp_path.glob("*.json"))
        self.assertEqual(len(files), 1)
        on_disk = json.loads(files[0].read_text())
        self.assertEqual(on_disk["id"], run["id"])
        self.assertEqual(on_disk["status"], "pending")

    def test_create_run_pipeline_steps_all_pending(self):
        engine = self._engine()
        result = _make_planner_result()
        run = engine.create_run("another task", result)
        for step in run["pipeline"]:
            self.assertEqual(step["status"], "pending")
            self.assertIsNone(step["task_id"])

    # ------------------------------------------------------------------
    # One-active-run guard
    # ------------------------------------------------------------------

    def test_second_create_run_raises_if_active(self):
        """Creating a second run while one is running should raise RuntimeError."""
        engine = self._engine()
        result = _make_planner_result()
        run = engine.create_run("task 1", result)

        # Manually set status to running
        engine._update_run_field(run["id"], "status", "running")

        result2 = _make_planner_result(project="raph-ui")
        with self.assertRaises(RuntimeError):
            engine.create_run("task 2", result2)

    def test_create_run_allowed_when_prior_is_done(self):
        """A completed run should NOT block new run creation."""
        engine = self._engine()
        r1 = engine.create_run("task 1", _make_planner_result())
        engine._update_run_field(r1["id"], "status", "done")

        r2 = engine.create_run("task 2", _make_planner_result(project="raph-ui"))
        self.assertIsNotNone(r2)
        self.assertNotEqual(r1["id"], r2["id"])

    # ------------------------------------------------------------------
    # mark_step_started / mark_step_done
    # ------------------------------------------------------------------

    def test_mark_step_started_sets_task_id_and_running(self):
        engine = self._engine()
        result = _make_planner_result()
        run = engine.create_run("task text", result)
        run_id = run["id"]

        engine.mark_step_started(run_id, 0, "task-001")
        updated = engine.get_run(run_id)
        step = updated["pipeline"][0]
        self.assertEqual(step["status"], "running")
        self.assertEqual(step["task_id"], "task-001")
        self.assertIsNotNone(step["started_at"])

    def test_mark_step_done_sets_completed_at(self):
        engine = self._engine()
        result = _make_planner_result()
        run = engine.create_run("task text", result)
        run_id = run["id"]

        engine.mark_step_started(run_id, 0, "task-001")
        engine.mark_step_done(run_id, 0, output_path="Projects/hermes/agents/output.md")

        updated = engine.get_run(run_id)
        step = updated["pipeline"][0]
        self.assertEqual(step["status"], "done")
        self.assertIsNotNone(step["completed_at"])
        self.assertEqual(step["output_path"], "Projects/hermes/agents/output.md")

    def test_mark_step_done_with_pr_url(self):
        engine = self._engine()
        result = _make_planner_result()
        run = engine.create_run("task text", result)
        run_id = run["id"]

        engine.mark_step_started(run_id, 0, "task-deployer-001")
        engine.mark_step_done(run_id, 0, pr_url="https://github.com/JaidenSy/hermes/pull/42")

        updated = engine.get_run(run_id)
        self.assertEqual(updated["pr_url"], "https://github.com/JaidenSy/hermes/pull/42")

    # ------------------------------------------------------------------
    # mark_step_failed
    # ------------------------------------------------------------------

    def test_mark_step_failed_sets_run_status_failed(self):
        engine = self._engine()
        result = _make_planner_result()
        run = engine.create_run("task text", result)
        run_id = run["id"]

        engine.mark_step_started(run_id, 0, "task-fail-001")
        engine.mark_step_failed(run_id, 0, reason="agent crashed")

        updated = engine.get_run(run_id)
        self.assertEqual(updated["status"], "failed")
        self.assertEqual(updated["pipeline"][0]["status"], "failed")
        self.assertIsNotNone(updated["completed_at"])
        # Reason is persisted on the step so the completion path can surface it
        # (daily note, handoff) instead of only logging it and telling Jaiden "check logs".
        self.assertEqual(updated["pipeline"][0]["reason"], "agent crashed")

    # ------------------------------------------------------------------
    # abort_run
    # ------------------------------------------------------------------

    def test_abort_run_sets_status_and_skips_pending(self):
        engine = self._engine()
        result = _make_planner_result()
        run = engine.create_run("task to abort", result)
        run_id = run["id"]

        # Set first step to running (simulating in-progress)
        engine.mark_step_started(run_id, 0, "task-001")

        with patch("run_engine.subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=0)
            engine.abort_run(run_id)

        updated = engine.get_run(run_id)
        self.assertEqual(updated["status"], "aborted")
        self.assertIsNotNone(updated["completed_at"])

        # Running step becomes skipped; pending steps also become skipped
        statuses = [s["status"] for s in updated["pipeline"]]
        for s in statuses:
            self.assertIn(s, ("skipped", "done"), f"Unexpected status: {s}")
        # None should still be pending after abort
        self.assertNotIn("pending", statuses)

    def test_abort_attempts_tmux_kill_for_running_steps(self):
        engine = self._engine()
        result = _make_planner_result()
        run = engine.create_run("task to abort with tmux", result)
        run_id = run["id"]

        engine.mark_step_started(run_id, 0, "task-abc-123")

        with patch("run_engine.subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=0)
            engine.abort_run(run_id)

        # Should have called tmux kill-session for the running step
        call_args_list = mock_sub.call_args_list
        tmux_calls = [c for c in call_args_list if c[0][0][0] == "tmux"]
        self.assertTrue(len(tmux_calls) >= 1, "Expected at least one tmux kill-session call")

    # ------------------------------------------------------------------
    # get_active_run
    # ------------------------------------------------------------------

    def test_get_active_run_returns_none_when_none_running(self):
        engine = self._engine()
        result = engine.get_active_run()
        self.assertIsNone(result)

    def test_get_active_run_returns_running_run(self):
        engine = self._engine()
        result = _make_planner_result()
        run = engine.create_run("task text", result)
        self.assertIsNone(engine.get_active_run())

        engine._update_run_field(run["id"], "status", "running")
        active = engine.get_active_run()
        self.assertIsNotNone(active)
        self.assertEqual(active["id"], run["id"])

    # ------------------------------------------------------------------
    # _find_next_pending_steps (parallel group logic)
    # ------------------------------------------------------------------

    def test_find_next_pending_sequential(self):
        engine = self._engine()
        pipeline = [
            {"role": "plan", "status": "pending", "parallel_group": None},
            {"role": "coder", "status": "pending", "parallel_group": None},
        ]
        steps = engine._find_next_pending_steps(pipeline)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0][1]["role"], "plan")

    def test_find_next_pending_parallel_group_returns_both(self):
        """Two pending steps with same parallel_group → both returned together."""
        engine = self._engine()
        pipeline = [
            {"role": "research", "status": "pending", "parallel_group": 1},
            {"role": "architect", "status": "pending", "parallel_group": 1},
            {"role": "coder", "status": "pending", "parallel_group": None},
        ]
        steps = engine._find_next_pending_steps(pipeline)
        self.assertEqual(len(steps), 2)
        roles = [s["role"] for _, s in steps]
        self.assertIn("research", roles)
        self.assertIn("architect", roles)

    def test_find_next_pending_skips_done_steps(self):
        """Done step is skipped; next pending is returned."""
        engine = self._engine()
        pipeline = [
            {"role": "plan", "status": "done", "parallel_group": None},
            {"role": "coder", "status": "pending", "parallel_group": None},
            {"role": "tester", "status": "pending", "parallel_group": None},
        ]
        steps = engine._find_next_pending_steps(pipeline)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0][1]["role"], "coder")

    def test_find_next_pending_empty_when_all_done(self):
        engine = self._engine()
        pipeline = [
            {"role": "coder", "status": "done", "parallel_group": None},
            {"role": "tester", "status": "done", "parallel_group": None},
        ]
        steps = engine._find_next_pending_steps(pipeline)
        self.assertEqual(steps, [])

    # ------------------------------------------------------------------
    # poll_step_completion (mocked — no real waiting)
    # ------------------------------------------------------------------

    def test_poll_step_completion_returns_done_immediately(self):
        """Mock task JSON on disk with status=done → poll should return 'done'."""
        engine = self._engine()
        result = _make_planner_result()
        run = engine.create_run("task text", result)
        run_id = run["id"]

        # Write a fake task JSON to TASKS_DIR
        with tempfile.TemporaryDirectory() as tasks_tmp:
            tasks_path = Path(tasks_tmp)
            task_id = "2026-06-07-001"
            task_file = tasks_path / f"{task_id}.json"
            task_file.write_text(json.dumps({"id": task_id, "status": "done"}))

            # Patch TASKS_DIR
            import run_engine as re_mod

            orig_tasks = re_mod.TASKS_DIR
            re_mod.TASKS_DIR = tasks_path

            # Patch time.sleep to avoid real waiting; STEP_POLL_INTERVAL_S will run once
            with patch("run_engine.time.sleep"):
                status = engine.poll_step_completion(run_id, 0, task_id, timeout=60)

            re_mod.TASKS_DIR = orig_tasks

        self.assertEqual(status, "done")

    def test_poll_step_completion_returns_failed(self):
        """Task JSON with status=failed → poll returns 'failed'."""
        engine = self._engine()
        result = _make_planner_result()
        run = engine.create_run("task text", result)
        run_id = run["id"]

        with tempfile.TemporaryDirectory() as tasks_tmp:
            tasks_path = Path(tasks_tmp)
            task_id = "2026-06-07-002"
            task_file = tasks_path / f"{task_id}.json"
            task_file.write_text(json.dumps({"id": task_id, "status": "failed"}))

            import run_engine as re_mod

            orig_tasks = re_mod.TASKS_DIR
            re_mod.TASKS_DIR = tasks_path

            with patch("run_engine.time.sleep"):
                status = engine.poll_step_completion(run_id, 0, task_id, timeout=60)

            re_mod.TASKS_DIR = orig_tasks

        self.assertEqual(status, "failed")

    # ------------------------------------------------------------------
    # list_runs
    # ------------------------------------------------------------------

    def test_list_runs_returns_all(self):
        engine = self._engine()
        r1 = engine.create_run("task 1", _make_planner_result())
        r2 = engine.create_run("task 2", _make_planner_result(branch="feature/b"))

        # Mark r1 done so r2 can be created (guard check)
        engine._update_run_field(r1["id"], "status", "done")
        r2 = engine.create_run("task 2", _make_planner_result(branch="feature/b"))

        runs = engine.list_runs()
        self.assertGreaterEqual(len(runs), 2)

    def test_get_run_raises_if_not_found(self):
        engine = self._engine()
        with self.assertRaises(FileNotFoundError):
            engine.get_run("nonexistent-id")

    # ------------------------------------------------------------------
    # _recover_stale_runs (startup crash recovery)
    # ------------------------------------------------------------------

    def _write_raw_run(self, run_data: dict) -> Path:
        """Write a raw run dict directly to RUNS_DIR (bypasses engine)."""
        import run_engine as re_mod

        ts = "2026-06-07-000000"
        filename = f"{ts}-{run_data['id']}.json"
        path = re_mod.RUNS_DIR / filename
        path.write_text(json.dumps(run_data, indent=2))
        return path

    def test_recover_stale_running_run_is_aborted(self):
        """A run stuck at status=running is aborted with all steps skipped on startup."""
        run_data = {
            "id": "stale001",
            "status": "running",
            "completed_at": None,
            "pipeline": [
                {"role": "coder", "status": "running", "task_id": "t-001"},
                {"role": "tester", "status": "pending", "task_id": None},
            ],
        }
        self._write_raw_run(run_data)

        engine = self._engine()

        updated = json.loads((list(self.tmp_path.glob("*stale001*"))[0]).read_text())
        self.assertEqual(updated["status"], "aborted")
        self.assertIsNotNone(updated["completed_at"])
        for step in updated["pipeline"]:
            self.assertEqual(step["status"], "skipped")

    def test_recover_stale_pending_run_is_aborted(self):
        """A run stuck at status=pending is also aborted on startup."""
        run_data = {
            "id": "stale002",
            "status": "pending",
            "completed_at": None,
            "pipeline": [
                {"role": "plan", "status": "pending", "task_id": None},
            ],
        }
        self._write_raw_run(run_data)

        engine = self._engine()

        updated = json.loads((list(self.tmp_path.glob("*stale002*"))[0]).read_text())
        self.assertEqual(updated["status"], "aborted")
        for step in updated["pipeline"]:
            self.assertEqual(step["status"], "skipped")

    def test_construction_does_not_abort_running_run(self):
        """Regression (June-12 clobber race): orchestrate_task builds a fresh
        RunEngine() per incoming message. Bare construction must NOT abort the run
        already in flight — recovery only runs via startup_recover_and_cleanup()."""
        run_data = {
            "id": "live001",
            "status": "running",
            "completed_at": None,
            "pipeline": [{"role": "coder", "status": "running", "task_id": "t-1"}],
        }
        self._write_raw_run(run_data)

        self.RunEngine()  # exactly what a second [HERMES] message triggers mid-run

        updated = json.loads((list(self.tmp_path.glob("*live001*"))[0]).read_text())
        self.assertEqual(updated["status"], "running")
        self.assertEqual(updated["pipeline"][0]["status"], "running")

    def test_recover_terminal_run_is_left_untouched(self):
        """Runs in terminal states (done, failed, aborted) are not modified."""
        for terminal_status in ("done", "failed", "aborted"):
            run_data = {
                "id": f"terminal-{terminal_status}",
                "status": terminal_status,
                "completed_at": "2026-06-07T00:00:00Z",
                "pipeline": [
                    {"role": "coder", "status": "done", "task_id": "t-x"},
                ],
            }
            self._write_raw_run(run_data)

        engine = self._engine()

        for terminal_status in ("done", "failed", "aborted"):
            run_id = f"terminal-{terminal_status}"
            matches = [p for p in self.tmp_path.glob("*.json") if f"-{run_id}" in p.name]
            self.assertEqual(len(matches), 1)
            on_disk = json.loads(matches[0].read_text())
            self.assertEqual(
                on_disk["status"],
                terminal_status,
                f"status changed for {terminal_status}",
            )

    def test_recover_empty_runs_dir_does_not_raise(self):
        """Startup recovery on an empty RUNS_DIR must not raise."""
        engine = self._engine()
        files = list(self.tmp_path.glob("*.json"))
        self.assertEqual(len(files), 0)

    # ------------------------------------------------------------------
    # retry_count field in create_run
    # ------------------------------------------------------------------

    def test_create_run_steps_have_retry_count_zero(self):
        """Every step in a newly created run has retry_count=0."""
        engine = self._engine()
        run = engine.create_run("test task", _make_planner_result())
        for step in run["pipeline"]:
            self.assertIn("retry_count", step, "retry_count missing from step")
            self.assertEqual(step["retry_count"], 0)

    # ------------------------------------------------------------------
    # reset_step_for_retry
    # ------------------------------------------------------------------

    def test_reset_step_for_retry_increments_count_and_clears_state(self):
        """reset_step_for_retry resets status/task_id/started_at and bumps retry_count."""
        engine = self._engine()
        run = engine.create_run("test", _make_planner_result())
        run_id = run["id"]

        engine.mark_step_started(run_id, 0, "task-x-001")
        engine.reset_step_for_retry(run_id, 0)

        updated = engine.get_run(run_id)
        step = updated["pipeline"][0]
        self.assertEqual(step["status"], "pending")
        self.assertIsNone(step["task_id"])
        self.assertIsNone(step["started_at"])
        self.assertEqual(step["retry_count"], 1)

    def test_reset_step_for_retry_does_not_change_run_status(self):
        """reset_step_for_retry must not touch run-level status."""
        engine = self._engine()
        run = engine.create_run("test", _make_planner_result())
        run_id = run["id"]
        engine._update_run_field(run_id, "status", "running")
        engine.mark_step_started(run_id, 0, "task-y-001")

        engine.reset_step_for_retry(run_id, 0)

        updated = engine.get_run(run_id)
        self.assertEqual(updated["status"], "running")

    def test_reset_step_for_retry_second_call_increments_to_two(self):
        """A second reset bumps retry_count to 2 (caller must enforce cap)."""
        engine = self._engine()
        run = engine.create_run("test", _make_planner_result())
        run_id = run["id"]

        engine.reset_step_for_retry(run_id, 0)
        engine.reset_step_for_retry(run_id, 0)

        updated = engine.get_run(run_id)
        self.assertEqual(updated["pipeline"][0]["retry_count"], 2)

    # ------------------------------------------------------------------
    # _cleanup_old_runs
    # ------------------------------------------------------------------

    def _write_raw_run_with_mtime(self, run_data: dict, age_days: float) -> Path:
        """Write a raw run and backdate its mtime by age_days days."""
        import os
        import time as _time

        path = self._write_raw_run(run_data)
        old_mtime = _time.time() - age_days * 86400
        os.utime(path, (old_mtime, old_mtime))
        return path

    def test_cleanup_deletes_old_terminal_runs(self):
        """Terminal runs (done/failed/aborted) older than RUNS_RETENTION_DAYS are deleted."""
        from run_engine import RUNS_RETENTION_DAYS

        for status in ("done", "failed", "aborted"):
            run_data = {
                "id": f"old-{status}",
                "status": status,
                "completed_at": "2020-01-01T00:00:00Z",
                "pipeline": [],
            }
            self._write_raw_run_with_mtime(run_data, age_days=RUNS_RETENTION_DAYS + 1)

        engine = self._engine()

        remaining = list(self.tmp_path.glob("*.json"))
        self.assertEqual(len(remaining), 0, "Old terminal runs should have been deleted")

    def test_cleanup_keeps_recent_terminal_runs(self):
        """Terminal runs younger than RUNS_RETENTION_DAYS are kept."""
        from run_engine import RUNS_RETENTION_DAYS

        run_data = {
            "id": "recent-done",
            "status": "done",
            "completed_at": "2026-06-07T00:00:00Z",
            "pipeline": [],
        }
        self._write_raw_run_with_mtime(run_data, age_days=RUNS_RETENTION_DAYS - 1)

        engine = self._engine()

        remaining = list(self.tmp_path.glob("*.json"))
        self.assertEqual(len(remaining), 1, "Recent terminal run should be kept")

    def test_cleanup_keeps_active_runs_regardless_of_age(self):
        """Pending/running runs are never deleted even if very old."""
        from run_engine import RUNS_RETENTION_DAYS

        for status in ("pending", "running"):
            run_data = {
                "id": f"active-{status}",
                "status": status,
                "completed_at": None,
                "pipeline": [{"role": "coder", "status": status, "task_id": None}],
            }
            self._write_raw_run_with_mtime(run_data, age_days=RUNS_RETENTION_DAYS + 30)

        # active runs are recovered (set to aborted) then NOT deleted (they're fresh-aborted)
        engine = self._engine()

        # After recovery they'll be aborted but mtime updated to now, so should survive
        remaining = list(self.tmp_path.glob("*.json"))
        self.assertEqual(len(remaining), 2, "Recovered active runs should not be deleted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
