"""
test_run_isolation.py — roadmap #6: the run-state lost-update fix, per-run git
worktrees, and W3C trace propagation into each step's Claude Code process.

  1. update_run holds the lock across read+write: N threads mutating one run
     concurrently never lose an update (works with fcntl on Linux/macOS and the
     in-process lock fallback on Windows).
  2. abort is final: state is committed before tmux is killed, and a step
     failure caused by the kill can't flip `aborted` back to `failed`.
  3. worktree.create / remove / recover_orphans against a real temp git repo.
  4. tracing: traceparent format, Claude Code telemetry env from config.
  5. agent_runner: the step runs in the worktree with TRACEPARENT in its env.
  6. engram._run_pipeline end to end (agents mocked): worktree created, every
     step dispatched into it, aborted run stands down without a retry, and the
     worktree is removed while the branch survives.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_runner  # noqa: E402
import run_engine  # noqa: E402
import tracing  # noqa: E402
import worktree  # noqa: E402
from planner import PipelineStep, PlannerResult  # noqa: E402

HAS_GIT = shutil.which("git") is not None

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Engram Test",
    "GIT_AUTHOR_EMAIL": "engram-test@example.invalid",
    "GIT_COMMITTER_NAME": "Engram Test",
    "GIT_COMMITTER_EMAIL": "engram-test@example.invalid",
}


def _result(steps=("coder", "tester", "deployer"), groups=None, branch="feature/iso"):
    groups = groups or [None] * len(steps)
    return PlannerResult(
        tier=2,
        project="demo",
        branch_name=branch,
        pipeline=[PipelineStep(r, g) for r, g in zip(steps, groups)],
        is_direct=False,
        raw_ollama_response="",
    )


class _TempRuns(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._orig = run_engine.RUNS_DIR
        run_engine.RUNS_DIR = self.tmp / "runs"
        self.engine = run_engine.RunEngine()

    def tearDown(self):
        run_engine.RUNS_DIR = self._orig
        self._tmp.cleanup()


# ── 1. lost-update race ──────────────────────────────────────────────────────


class TestUpdateRunAtomicity(_TempRuns):
    def test_concurrent_increments_are_never_lost(self):
        run = self.engine.create_run("t", _result())
        run_id = run["id"]
        threads_n, per_thread = 8, 40

        def _bump(r):
            r["counter"] = r.get("counter", 0) + 1

        def _worker():
            for _ in range(per_thread):
                self.engine.update_run(run_id, _bump)

        ts = [threading.Thread(target=_worker) for _ in range(threads_n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(self.engine.get_run(run_id)["counter"], threads_n * per_thread)

    def test_parallel_group_steps_do_not_clobber_each_other(self):
        """The real failure: tester ‖ cleanup each read the run, set their own
        step and wrote it back, and the second write erased the first."""
        steps = ("coder", "tester", "cleanup", "review", "a", "b", "c", "d")
        run = self.engine.create_run("t", _result(steps=steps, groups=[1] * len(steps)))
        run_id = run["id"]
        barrier = threading.Barrier(len(steps))

        def _step(i):
            barrier.wait()
            self.engine.mark_step_started(run_id, i, f"task-{i}")
            self.engine.update_step_fields(run_id, i, {"span_id": f"{i:016x}"})
            self.engine.mark_step_done(run_id, i, output_path=f"out-{i}.md")

        ts = [threading.Thread(target=_step, args=(i,)) for i in range(len(steps))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        final = self.engine.get_run(run_id)
        for i, s in enumerate(final["pipeline"]):
            self.assertEqual(s["status"], "done", f"step {i} lost its update")
            self.assertEqual(s["task_id"], f"task-{i}")
            self.assertEqual(s["output_path"], f"out-{i}.md")
            self.assertEqual(s["span_id"], f"{i:016x}")

    def test_update_run_leaves_no_temp_files_and_returns_written_state(self):
        run = self.engine.create_run("t", _result())
        out = self.engine.update_run(run["id"], lambda r: r.update(note="x"))
        self.assertEqual(out["note"], "x")
        names = [p.name for p in run_engine.RUNS_DIR.iterdir()]
        self.assertFalse([n for n in names if n.endswith(".tmp")], names)
        self.assertEqual(len(list(run_engine.RUNS_DIR.glob("*.json"))), 1)

    def test_update_run_can_replace_the_whole_dict(self):
        run = self.engine.create_run("t", _result())
        self.engine.update_run(run["id"], lambda r: {**r, "status": "done"})
        self.assertEqual(self.engine.get_run(run["id"])["status"], "done")

    def test_single_active_flag(self):
        self.engine._update_run_field(
            self.engine.create_run("a", _result())["id"], "status", "running"
        )
        with self.assertRaises(RuntimeError):
            self.engine.create_run("b", _result())
        second = self.engine.create_run("b", _result(), single_active=False)
        self.assertEqual(second["status"], "pending")

    def test_new_run_has_isolation_fields(self):
        run = self.engine.create_run("t", _result())
        for k in ("worktree", "trace_id", "span_id"):
            self.assertIn(k, run)

    def test_ledger_records_trace_id(self):
        run = self.engine.create_run("t", _result())
        self.engine._update_run_fields(run["id"], {"trace_id": "ab" * 16, "status": "done"})
        ledger = self.tmp / "ledger.jsonl"
        with patch.object(run_engine, "LEDGER_FILE", ledger):
            run_engine._append_ledger(self.engine.get_run(run["id"]))
        self.assertEqual(json.loads(ledger.read_text())["trace_id"], "ab" * 16)


# ── 2. abort is final ────────────────────────────────────────────────────────


class TestAbortIsFinal(_TempRuns):
    def test_abort_commits_state_before_killing_sessions(self):
        run = self.engine.create_run("t", _result())
        self.engine.mark_step_started(run["id"], 0, "task-0")
        seen = []

        def _fake_run(cmd, **kw):
            seen.append(self.engine.get_run(run["id"])["status"])
            return MagicMock(returncode=0)

        with patch("run_engine.subprocess.run", side_effect=_fake_run):
            self.engine.abort_run(run["id"])
        self.assertEqual(seen, ["aborted"])

    def test_step_failure_after_abort_keeps_run_aborted(self):
        run = self.engine.create_run("t", _result())
        self.engine.mark_step_started(run["id"], 0, "task-0")
        with patch("run_engine.subprocess.run"):
            self.engine.abort_run(run["id"])
        self.engine.mark_step_failed(run["id"], 0, reason="session died")
        self.assertEqual(self.engine.get_run(run["id"])["status"], "aborted")


# ── 3. worktrees ─────────────────────────────────────────────────────────────


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@unittest.skipUnless(HAS_GIT, "git not installed")
class _GitRepo(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, _GIT_ENV)
        self._env.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "demo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "README.md").write_text("hi\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "init")

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _branches(self):
        return _git(self.repo, "branch", "--format=%(refname:short)").split()


class TestWorktree(_GitRepo):
    def test_create_new_branch_from_base(self):
        path = worktree.create(self.repo, "r1", "feature/x", "main")
        self.assertEqual(path, self.repo.resolve() / ".engram-worktrees" / "r1")
        self.assertTrue((path / "README.md").exists())
        self.assertEqual(_git(path, "rev-parse", "--abbrev-ref", "HEAD"), "feature/x")
        # The worktree root is invisible to the main tree's `git status`.
        self.assertEqual(_git(self.repo, "status", "--porcelain"), "")

    def test_create_reuses_existing_branch(self):
        _git(self.repo, "branch", "feature/old")
        path = worktree.create(self.repo, "r2", "feature/old", "main")
        self.assertEqual(_git(path, "rev-parse", "--abbrev-ref", "HEAD"), "feature/old")

    def test_branch_held_by_stale_engram_worktree_is_reclaimed(self):
        old = worktree.create(self.repo, "dead", "feature/x", "main")
        new = worktree.create(self.repo, "live", "feature/x", "main")
        self.assertFalse(old.exists())
        self.assertEqual(_git(new, "rev-parse", "--abbrev-ref", "HEAD"), "feature/x")

    def test_branch_checked_out_in_main_tree_raises(self):
        with self.assertRaises(worktree.WorktreeError):
            worktree.create(self.repo, "r3", "main", "main")

    def test_missing_base_falls_back_to_head(self):
        path = worktree.create(self.repo, "r4", "feature/y", "no-such-branch")
        self.assertTrue(path.exists())

    def test_custom_root(self):
        root = Path(self._tmp.name) / "wts"
        path = worktree.create(
            self.repo, "r5", "feature/z", "main", {"worktrees": {"root": str(root)}}
        )
        self.assertEqual(path, root / "demo" / "r5")

    def test_remove_keeps_branch_and_stashes_leftovers(self):
        path = worktree.create(self.repo, "r6", "feature/w", "main")
        (path / "README.md").write_text("changed\n")
        (path / "new.txt").write_text("untracked\n")
        info = worktree.remove(self.repo, path, reason="failed")
        self.assertTrue(info["removed"])
        self.assertFalse(path.exists())
        self.assertIn("feature/w", self._branches())
        self.assertIn("r6", _git(self.repo, "stash", "list"))
        # Idempotent.
        self.assertFalse(worktree.remove(self.repo, path)["removed"])

    def test_remove_clean_worktree_makes_no_stash(self):
        path = worktree.create(self.repo, "r7", "feature/v", "main")
        info = worktree.remove(self.repo, path)
        self.assertIsNone(info["stash"])
        self.assertEqual(_git(self.repo, "stash", "list"), "")

    def test_recover_orphans_spares_live_runs(self):
        live = worktree.create(self.repo, "live1", "feature/a", "main")
        dead = worktree.create(self.repo, "dead1", "feature/b", "main")
        removed = worktree.recover_orphans([self.repo, self.repo], {"live1"})
        self.assertEqual([Path(p).name for p in removed], ["dead1"])
        self.assertTrue(live.exists())
        self.assertFalse(dead.exists())
        self.assertIn("feature/b", self._branches())

    def test_disabled_flag(self):
        self.assertTrue(worktree.enabled({}))
        self.assertFalse(worktree.enabled({"worktrees": {"enabled": False}}))


# ── 4. tracing ───────────────────────────────────────────────────────────────


class TestTracing(unittest.TestCase):
    def test_traceparent_roundtrip(self):
        t, s = tracing.new_trace_id(), tracing.new_span_id()
        tp = tracing.format_traceparent(t, s)
        self.assertRegex(tp, r"^00-[0-9a-f]{32}-[0-9a-f]{16}-01$")
        self.assertEqual(tracing.parse_traceparent(tp), (t, s, "01"))
        self.assertIsNone(tracing.parse_traceparent("00-" + "0" * 32 + "-" + "1" * 16 + "-01"))
        self.assertIsNone(tracing.parse_traceparent("garbage"))

    def test_start_workflow_and_step_return_valid_ids_without_sdk(self):
        trace_id, span_id = tracing.start_workflow("run1", "engram-tier-2")
        step_span = tracing.start_step("run1", 0, "coder", trace_id, span_id)
        self.assertRegex(trace_id, r"^[0-9a-f]{32}$")
        self.assertRegex(step_span, r"^[0-9a-f]{16}$")
        self.assertNotEqual(step_span, span_id)
        tracing.end_step("run1", 0, "done")
        tracing.end_workflow("run1", "done")  # no-ops without the SDK, must not raise

    def test_default_env_is_traceparent_only(self):
        env, secret = tracing.step_env({}, "a" * 32, "b" * 16, environ={})
        self.assertEqual(env, {"TRACEPARENT": f"00-{'a' * 32}-{'b' * 16}-01"})
        self.assertEqual(secret, {})

    def test_no_trace_no_traceparent(self):
        env, _ = tracing.step_env({}, None, None, environ={})
        self.assertEqual(env, {})

    def test_claude_code_telemetry_env_from_config(self):
        cfg = {
            "telemetry": {
                "claude_code": {
                    "enabled": True,
                    "enhanced_traces_beta": True,
                    "otlp_endpoint": "http://collector:4318",
                    "otlp_protocol": "http/protobuf",
                    "extra_env": {"OTEL_LOG_TOOL_DETAILS": "1"},
                },
                "passthrough_env": ["OTEL_EXPORTER_OTLP_HEADERS", "UNSET_VAR"],
            }
        }
        env, secret = tracing.step_env(
            cfg,
            "a" * 32,
            "b" * 16,
            run_id="r1",
            role="coder",
            environ={"OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer x"},
        )
        self.assertEqual(env["CLAUDE_CODE_ENABLE_TELEMETRY"], "1")
        self.assertEqual(env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"], "1")
        self.assertEqual(env["OTEL_TRACES_EXPORTER"], "otlp")
        self.assertEqual(env["OTEL_METRICS_EXPORTER"], "otlp")
        self.assertEqual(env["OTEL_LOGS_EXPORTER"], "otlp")
        self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"], "http://collector:4318")
        self.assertEqual(env["OTEL_RESOURCE_ATTRIBUTES"], "engram.run_id=r1,engram.role=coder")
        self.assertEqual(env["OTEL_LOG_TOOL_DETAILS"], "1")
        self.assertIn("TRACEPARENT", env)
        # Secrets stay out of the persisted env.
        self.assertNotIn("OTEL_EXPORTER_OTLP_HEADERS", env)
        self.assertEqual(secret, {"OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer x"})

    def test_traces_beta_off_means_no_traces_exporter(self):
        env, _ = tracing.step_env(
            {"telemetry": {"claude_code": {"enabled": True}}}, "a" * 32, "b" * 16, environ={}
        )
        self.assertNotIn("CLAUDE_CODE_ENHANCED_TELEMETRY_BETA", env)
        self.assertNotIn("OTEL_TRACES_EXPORTER", env)

    def test_init_without_flag_is_off(self):
        self.assertFalse(tracing.init({}))


# ── 5. agent_runner dispatch ─────────────────────────────────────────────────


class TestAgentRunnerIsolation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tasks = Path(self._tmp.name) / "tasks"
        self.tasks.mkdir()
        self.wt = Path(self._tmp.name) / "wt"
        self.wt.mkdir()
        self._p = patch.object(agent_runner, "TASKS_DIR", self.tasks)
        self._p.start()
        self.runner = agent_runner.AgentRunner()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def _run(self, worktree_path=None):
        return {
            "id": "abcd1234",
            "task_raw": "do it",
            "project": "demo",
            "tier": 2,
            "branch": "feature/iso",
            "worktree": worktree_path,
            "trace_id": "c" * 32,
            "span_id": "d" * 16,
            "pipeline": [{"role": "coder", "status": "pending", "span_id": "e" * 16}],
        }

    def test_task_json_points_at_worktree_with_traceparent(self):
        run = self._run(str(self.wt))
        with (
            patch.object(self.runner, "_spawn_run_agent") as spawn,
            patch.object(agent_runner, "_load_config", return_value={}),
        ):
            task_id = self.runner.dispatch_step(run["pipeline"][0], run, "do it")
        task = json.loads((self.tasks / f"{task_id}.json").read_text())
        self.assertEqual(task["repo"], str(self.wt))
        self.assertIn("main_repo", task)
        self.assertEqual(task["env"]["TRACEPARENT"], f"00-{'c' * 32}-{'e' * 16}-01")
        self.assertEqual(task["trace_id"], "c" * 32)
        self.assertEqual(spawn.call_args.kwargs["cwd"], str(self.wt))
        self.assertEqual(spawn.call_args.kwargs["env"]["TRACEPARENT"], task["env"]["TRACEPARENT"])
        prompt = (self.tasks / f"{task_id}-prompt.md").read_text(encoding="utf-8")
        self.assertIn("own git worktree", prompt)

    def test_no_worktree_keeps_repo(self):
        run = self._run(None)
        with (
            patch.object(self.runner, "_spawn_run_agent"),
            patch.object(agent_runner, "_load_config", return_value={}),
        ):
            task_id = self.runner.dispatch_step(run["pipeline"][0], run, "do it")
        task = json.loads((self.tasks / f"{task_id}.json").read_text())
        self.assertNotIn("main_repo", task)

    def test_tmux_gets_cwd_and_env(self):
        with patch("agent_runner.subprocess.run", return_value=MagicMock(returncode=0)) as sp:
            self.runner._spawn_run_agent(
                "t1", self.tasks / "t1.json", cwd=str(self.wt), env={"TRACEPARENT": "00-x"}
            )
        cmd = sp.call_args[0][0]
        self.assertEqual(cmd[cmd.index("-c") + 1], str(self.wt))
        self.assertEqual(cmd[cmd.index("-e") + 1], "TRACEPARENT=00-x")
        self.assertTrue(cmd[-1].endswith("; echo RAPHAEL_DONE"))

    def test_tmux_skips_missing_cwd(self):
        with patch("agent_runner.subprocess.run", return_value=MagicMock(returncode=0)) as sp:
            self.runner._spawn_run_agent("t1", self.tasks / "t1.json", cwd="/no/such/dir")
        self.assertNotIn("-c", sp.call_args[0][0])


# ── 6. engram pipeline end to end ────────────────────────────────────────────


class _FakeProj:
    def __init__(self, repo):
        self.repo = str(repo)
        self.raphbrain_dir = "Demo"


@unittest.skipUnless(HAS_GIT, "git not installed")
class TestPipelineIsolation(_GitRepo):
    def setUp(self):
        super().setUp()
        import engram

        self.engram = engram
        self._orig = run_engine.RUNS_DIR
        run_engine.RUNS_DIR = Path(self._tmp.name) / "runs"
        self.engine = run_engine.RunEngine()
        self.cfg = {"worktrees": {"enabled": True}}
        self.replies = []
        self._patches = [
            patch.object(engram, "resolve_project", return_value=_FakeProj(self.repo)),
            patch.object(engram, "pr_base", return_value="main"),
            patch.object(engram, "_send_reply", side_effect=lambda c, m: self.replies.append(m)),
            patch.object(engram, "_append_pipeline_to_daily_note"),
            patch.object(engram, "_append_pipeline_to_progress_note"),
            patch.object(engram, "spawn_post_task_review"),
            patch.object(engram, "validate_step_output", return_value=(True, "")),
            patch.object(engram, "STEP_RETRY_DELAY_S", 0),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        run_engine.RUNS_DIR = self._orig
        super().tearDown()

    def _start(self, steps=("coder", "tester")):
        run = self.engine.create_run("ship it", _result(steps=steps))
        self.engine._update_run_field(run["id"], "status", "running")
        return run["id"]

    def test_steps_run_in_worktree_and_cleanup_keeps_branch(self):
        run_id = self._start()
        seen = []
        runner = MagicMock()

        def _dispatch(step, run, task_description):
            seen.append(
                (run["worktree"], Path(run["worktree"]).is_dir(), run["trace_id"], step["span_id"])
            )
            return f"task-{len(seen)}"

        runner.dispatch_step.side_effect = _dispatch
        with (
            patch.object(self.engine, "poll_step_completion", return_value="done"),
            patch.object(self.engine, "_read_task_output_note", return_value=None),
        ):
            self.engram._run_pipeline(run_id, self.engine, runner, self.cfg)

        final = self.engine.get_run(run_id)
        self.assertEqual(final["status"], "done")
        self.assertEqual(len(seen), 2)
        wt = seen[0][0]
        self.assertTrue(all(s[0] == wt and s[1] for s in seen), seen)
        self.assertEqual(Path(wt).parent.name, ".engram-worktrees")
        self.assertRegex(final["trace_id"], r"^[0-9a-f]{32}$")
        self.assertTrue(all(s[2] == final["trace_id"] for s in seen))
        self.assertEqual([s[3] for s in seen], [p["span_id"] for p in final["pipeline"]])
        self.assertFalse(Path(wt).exists(), "worktree should be removed on a terminal state")
        self.assertTrue(final["worktree_removed"])
        self.assertIn("feature/iso", self._branches())

    def test_abort_stands_down_without_retry_and_cleans_up(self):
        run_id = self._start()
        runner = MagicMock()
        runner.dispatch_step.return_value = "task-1"

        def _poll(rid, idx, task_id):
            with patch("run_engine.subprocess.run"):
                self.engine.abort_run(rid)
            return "failed"  # what a killed tmux session looks like

        with patch.object(self.engine, "poll_step_completion", side_effect=_poll):
            self.engram._run_pipeline(run_id, self.engine, runner, self.cfg)

        final = self.engine.get_run(run_id)
        self.assertEqual(final["status"], "aborted")
        self.assertEqual(runner.dispatch_step.call_count, 1, "aborted step must not retry")
        self.assertFalse([m for m in self.replies if "Blocked" in m])
        self.assertFalse(Path(final["worktree"]).exists())

    def test_worktree_failure_falls_back_when_single_active(self):
        run_id = self._start(steps=("coder",))
        self.engine._update_run_field(run_id, "branch", "main")  # checked out in the main tree
        runner = MagicMock()
        runner.dispatch_step.return_value = "task-1"
        with (
            patch.object(self.engine, "poll_step_completion", return_value="done"),
            patch.object(self.engine, "_read_task_output_note", return_value=None),
        ):
            self.engram._run_pipeline(run_id, self.engine, runner, self.cfg)
        final = self.engine.get_run(run_id)
        self.assertEqual(final["status"], "done")
        self.assertIsNone(final["worktree"])
        self.assertIn("worktree_error", final)

    def test_worktree_failure_refuses_run_when_concurrent(self):
        run_id = self._start(steps=("coder",))
        self.engine._update_run_field(run_id, "branch", "main")
        runner = MagicMock()
        cfg = {"worktrees": {"enabled": True}, "runs": {"single_active_run": False}}
        self.engram._run_pipeline(run_id, self.engine, runner, cfg)
        self.assertEqual(self.engine.get_run(run_id)["status"], "failed")
        runner.dispatch_step.assert_not_called()

    def test_startup_sweeps_orphaned_worktrees(self):
        run_id = self._start(steps=("coder",))
        self.assertTrue(self.engram._prepare_run_isolation(run_id, self.engine, self.cfg))
        wt = Path(self.engine.get_run(run_id)["worktree"])
        self.assertTrue(wt.exists())
        # Daemon "crashes"; on restart the run is aborted, then the sweep runs.
        self.engine.startup_recover_and_cleanup()
        with patch("project_registry.get_registry", return_value={}):
            self.engram._recover_orphaned_worktrees(self.engine, self.cfg)
        self.assertFalse(wt.exists())
        self.assertTrue(self.engine.get_run(run_id)["worktree_removed"])

    def test_single_active_forced_when_worktrees_off(self):
        self.assertTrue(
            self.engram._single_active_run(
                {"worktrees": {"enabled": False}, "runs": {"single_active_run": False}}
            )
        )
        self.assertFalse(self.engram._single_active_run({"runs": {"single_active_run": False}}))


if __name__ == "__main__":
    unittest.main()
