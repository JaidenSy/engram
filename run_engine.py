"""
run_engine.py — RunEngine for Hermes Mission Control.

Manages run state files in ~/hermes/runs/, handles pipeline advancement,
parallel group dispatching, file-locked writes, and step polling.
"""

import fcntl
import json
import logging
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RUNS_DIR = Path.home() / "hermes" / "runs"
TASKS_DIR = Path.home() / "raphael" / "tasks"
STEP_POLL_INTERVAL_S = 5  # poll raphael task JSON every 5 seconds
STEP_TIMEOUT_S = 3600  # 60-min hard timeout per step
RUN_FILE_LOCK_TIMEOUT_S = 5  # max wait to acquire file lock

log = logging.getLogger("hermes")


# ---------------------------------------------------------------------------
# Minimal PipelineStep definition (mirrors planner.py dataclass).
# If planner.py is importable, import from there; otherwise use this.
# ---------------------------------------------------------------------------

try:
    from planner import PipelineStep, PlannerResult  # type: ignore
except ImportError:
    from dataclasses import dataclass
    from typing import Optional as _Opt

    @dataclass
    class PipelineStep:
        role: str
        parallel_group: _Opt[int] = None

    @dataclass
    class PlannerResult:
        tier: int
        project: str
        branch_name: str
        pipeline: list
        is_direct: bool
        raw_ollama_response: str


# ---------------------------------------------------------------------------
# RunEngine
# ---------------------------------------------------------------------------


class RunEngine:
    """
    Manages the lifecycle of pipeline runs stored as JSON files in RUNS_DIR.

    Each run file: ~/hermes/runs/{YYYY-MM-DD-HHMMSS}-{8-char-hex}.json

    Thread safety: all writes acquire an exclusive fcntl lock on the file.
    Reads are lockless (acceptable stale-read semantics inside poll loops).
    """

    def __init__(self):
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        self._recover_stale_runs()
        log.debug("RunEngine initialised — RUNS_DIR: %s", RUNS_DIR)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_run(self, task_raw: str, result: "PlannerResult") -> dict:
        """
        Creates a run JSON file with status=pending.

        Raises RuntimeError if another run is already active (status=running).
        Returns the full run dict.
        """
        active = self.get_active_run()
        if active:
            raise RuntimeError(
                f"Run {active['id']!r} is already active — abort it first"
            )

        RUNS_DIR.mkdir(parents=True, exist_ok=True)

        run_id = uuid.uuid4().hex[:8]
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
        filename = f"{ts}-{run_id}.json"

        pipeline = [
            {
                "role": step.role,
                "status": "pending",
                "task_id": None,
                "started_at": None,
                "completed_at": None,
                "output_path": None,
                "parallel_group": step.parallel_group,
            }
            for step in result.pipeline
        ]

        run = {
            "id": run_id,
            "task_raw": task_raw,
            "project": result.project,
            "tier": result.tier,
            "branch": result.branch_name,
            "pipeline": pipeline,
            "status": "pending",
            "created_at": now_iso,
            "completed_at": None,
            "pr_url": None,
        }

        (RUNS_DIR / filename).write_text(json.dumps(run, indent=2))
        log.info(
            "Run created: id=%s project=%s tier=%d branch=%s steps=%d",
            run_id,
            result.project,
            result.tier,
            result.branch_name,
            len(pipeline),
        )
        return run

    def start_run(self, run_id: str) -> None:
        """
        Sets status=running, then drives advance_pipeline() until the run
        reaches a terminal state.

        This method BLOCKS until the run completes, fails, or is aborted.
        Callers that need non-blocking behaviour must wrap this in a thread:
            t = threading.Thread(target=engine.start_run, args=(run_id,), daemon=True)
            t.start()
        """
        run = self.get_run(run_id)
        if run["status"] != "pending":
            log.warning(
                "start_run called on run %s with status=%s — ignoring",
                run_id,
                run["status"],
            )
            return

        self._update_run_field(run_id, "status", "running")
        log.info("Run started: %s", run_id)
        self.advance_pipeline(run_id)

    def advance_pipeline(self, run_id: str) -> None:
        """
        Finds the next pending step(s) and dispatches them.

        - Sequential step (parallel_group=None): dispatched inline; blocks until done.
        - Parallel group: all same-group steps dispatched in threads; waits for all.
        - After all steps finish: marks run done or failed.
        """
        run = self.get_run(run_id)

        if run["status"] in ("failed", "aborted", "done"):
            log.debug(
                "advance_pipeline: run %s is terminal (%s) — stopping",
                run_id,
                run["status"],
            )
            return

        pipeline = run["pipeline"]
        next_steps = self._find_next_pending_steps(pipeline)

        if not next_steps:
            # No more pending steps — finalise the run
            any_failed = any(s["status"] == "failed" for s in pipeline)
            final_status = "failed" if any_failed else "done"
            self._update_run_fields(
                run_id,
                {
                    "status": final_status,
                    "completed_at": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                },
            )
            log.info("Run %s complete — status=%s", run_id, final_status)
            return

        if len(next_steps) == 1:
            idx, step = next_steps[0]
            log.info(
                "Dispatching sequential step [%d] role=%s run=%s",
                idx,
                step["role"],
                run_id,
            )
            self._dispatch_and_wait(run_id, idx, step)
        else:
            log.info(
                "Dispatching parallel group (%d steps) run=%s: %s",
                len(next_steps),
                run_id,
                [s["role"] for _, s in next_steps],
            )
            threads = []
            for idx, step in next_steps:
                t = threading.Thread(
                    target=self._dispatch_and_wait,
                    args=(run_id, idx, step),
                    daemon=True,
                    name=f"hermes-step-{run_id}-{idx}",
                )
                threads.append(t)
                t.start()
            for t in threads:
                t.join()
            log.info("Parallel group finished for run %s", run_id)

        # Recurse to pick up the next step(s) in the pipeline
        self.advance_pipeline(run_id)

    def mark_step_done(
        self,
        run_id: str,
        step_index: int,
        output_path: Optional[str] = None,
        pr_url: Optional[str] = None,
    ) -> None:
        """
        Sets pipeline[step_index].status=done, completed_at=now.
        Optionally records output_path and pr_url on the run.
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run = self.get_run(run_id)
        run["pipeline"][step_index]["status"] = "done"
        run["pipeline"][step_index]["completed_at"] = now
        if output_path is not None:
            run["pipeline"][step_index]["output_path"] = output_path
        if pr_url is not None:
            run["pr_url"] = pr_url
        self._write_run(run_id, run)
        log.info(
            "Step %d done — run=%s role=%s",
            step_index,
            run_id,
            run["pipeline"][step_index]["role"],
        )

    def mark_step_failed(
        self, run_id: str, step_index: int, reason: Optional[str] = None
    ) -> None:
        """
        Sets pipeline[step_index].status=failed and run.status=failed.
        Stops pipeline advancement (caller must not call advance_pipeline after this).
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run = self.get_run(run_id)
        run["pipeline"][step_index]["status"] = "failed"
        run["pipeline"][step_index]["completed_at"] = now
        run["status"] = "failed"
        run["completed_at"] = now
        self._write_run(run_id, run)
        log.error(
            "Step %d FAILED — run=%s role=%s reason=%s",
            step_index,
            run_id,
            run["pipeline"][step_index]["role"],
            reason,
        )

    def get_active_run(self) -> Optional[dict]:
        """
        Scans RUNS_DIR for a run with status=running.
        Returns the first match, or None.
        Only one active run is enforced by create_run().
        """
        for path in sorted(RUNS_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                if data.get("status") == "running":
                    return data
            except Exception:
                pass
        return None

    def abort_run(self, run_id: str) -> None:
        """
        Sets run.status=aborted.
        Kills tmux sessions for any running steps (raphael-{task_id}).
        Sets running/pending steps to skipped.
        """
        run = self.get_run(run_id)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for step in run["pipeline"]:
            if step["status"] == "running" and step.get("task_id"):
                session = f"raphael-{step['task_id']}"
                log.info("Killing tmux session: %s", session)
                subprocess.run(
                    ["tmux", "kill-session", "-t", session],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                step["status"] = "skipped"
            elif step["status"] == "pending":
                step["status"] = "skipped"

        run["status"] = "aborted"
        run["completed_at"] = now
        self._write_run(run_id, run)
        log.info("Run aborted: %s", run_id)

    def get_run(self, run_id: str) -> dict:
        """
        Reads and returns a specific run by ID.
        Scans RUNS_DIR for the matching file.
        Raises FileNotFoundError if not found.
        """
        for path in RUNS_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                if data.get("id") == run_id:
                    return data
            except Exception:
                pass
        raise FileNotFoundError(f"Run {run_id!r} not found in {RUNS_DIR}")

    def list_runs(self, limit: int = 50) -> list:
        """
        Returns all run files sorted by created_at descending, up to limit.
        """
        runs = []
        for path in sorted(RUNS_DIR.glob("*.json"), reverse=True):
            try:
                data = json.loads(path.read_text())
                runs.append(data)
                if len(runs) >= limit:
                    break
            except Exception:
                pass
        return runs

    def poll_step_completion(
        self,
        run_id: str,
        step_index: int,
        task_id: str,
        timeout: int = STEP_TIMEOUT_S,
    ) -> str:
        """
        Polls ~/raphael/tasks/{task_id}.json every STEP_POLL_INTERVAL_S seconds.

        Terminal states: done | failed | needs-review | rate-limited
        Returns the terminal status string, or "timeout" if hard timeout exceeded.
        rate-limited is returned as-is so the caller can treat it as failure.
        """
        task_file = TASKS_DIR / f"{task_id}.json"
        elapsed = 0
        log.info(
            "Polling step completion: run=%s step=%d task_id=%s timeout=%ds",
            run_id,
            step_index,
            task_id,
            timeout,
        )

        while elapsed < timeout:
            time.sleep(STEP_POLL_INTERVAL_S)
            elapsed += STEP_POLL_INTERVAL_S
            try:
                data = json.loads(task_file.read_text())
                status = data.get("status", "pending")
                if status in ("done", "failed", "needs-review", "rate-limited"):
                    log.info(
                        "Step completed: run=%s step=%d task_id=%s final_status=%s elapsed=%ds",
                        run_id,
                        step_index,
                        task_id,
                        status,
                        elapsed,
                    )
                    return status
            except Exception:
                # File may be mid-write or not yet created — retry next tick
                pass

        log.warning(
            "Step timeout: run=%s step=%d task_id=%s elapsed=%ds",
            run_id,
            step_index,
            task_id,
            elapsed,
        )
        return "timeout"

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _recover_stale_runs(self) -> None:
        """
        Called once at startup to abort runs left in pending/running state
        by a previous crash. Writes directly to disk — no lock needed at startup.
        Does NOT call abort_run() because that does tmux kill-session, which is
        useless noise when the sessions are already dead.
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for path in sorted(RUNS_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except Exception:
                continue
            stale_status = data.get("status")
            if stale_status not in ("pending", "running"):
                continue
            for step in data.get("pipeline", []):
                if step.get("status") in ("pending", "running"):
                    step["status"] = "skipped"
            data["status"] = "aborted"
            data["completed_at"] = now
            path.write_text(json.dumps(data, indent=2))
            log.warning(
                "Startup recovery: aborted stale run %s (was %s)",
                data.get("id"),
                stale_status,
            )

    def _find_next_pending_steps(self, pipeline: list) -> list:
        """
        Scans pipeline for the next batch of pending steps to dispatch.

        Returns a list of (index, step_dict) tuples:
        - If the first pending step has parallel_group=None: returns single-item list.
        - If it has a parallel_group int: returns all pending steps sharing that group.
        """
        for i, step in enumerate(pipeline):
            if step["status"] != "pending":
                continue

            group = step.get("parallel_group")
            if group is None:
                return [(i, step)]

            # Collect all pending steps in the same parallel group
            group_steps = [(i, step)]
            for j, s2 in enumerate(pipeline[i + 1 :], i + 1):
                if s2.get("parallel_group") == group and s2["status"] == "pending":
                    group_steps.append((j, s2))
            return group_steps

        return []

    def _dispatch_and_wait(self, run_id: str, step_index: int, step: dict) -> None:
        """
        Marks a step as running, polls for its task completion, then marks
        it done or failed.

        This method is called directly for sequential steps, or as the thread
        target for parallel steps.

        Note: The actual task_id must already be set on the step before polling
        begins. This method checks if the step has a task_id; if not, it marks
        the step failed (caller — typically agent_runner — should set task_id via
        mark_step_started() before poll_step_completion is meaningful).

        For now, _dispatch_and_wait handles the state transitions. The actual
        dispatching of the agent subprocess is delegated to AgentRunner
        (agent_runner.py), which should call back into RunEngine to set task_id
        and started_at, then poll_step_completion() to wait.

        Standalone behaviour (used when agent_runner is not integrated):
        If step["task_id"] is already set (pre-populated), goes straight to polling.
        If task_id is None, logs a warning and marks step failed.
        """
        # Mark step as running
        self._mark_step_running(run_id, step_index)

        task_id = step.get("task_id")
        if not task_id:
            log.warning(
                "Step %d (role=%s) has no task_id — cannot poll; marking failed. "
                "AgentRunner should call mark_step_started() with task_id before calling advance_pipeline.",
                step_index,
                step.get("role"),
            )
            self.mark_step_failed(
                run_id,
                step_index,
                reason="task_id not set — AgentRunner did not populate it before pipeline advanced",
            )
            return

        final_status = self.poll_step_completion(run_id, step_index, task_id)

        if final_status == "done" or final_status == "needs-review":
            # needs-review: treat as done with a warning (review role will catch issues)
            if final_status == "needs-review":
                log.warning(
                    "Step %d (role=%s) task=%s ended with needs-review — "
                    "treating as done; review role should catch any issues.",
                    step_index,
                    step.get("role"),
                    task_id,
                )
            # Attempt to extract output_path from the task file
            output_path = self._read_task_output_note(task_id)
            # Attempt to extract pr_url if this is a deployer step
            pr_url = None
            if step.get("role") == "deployer" and output_path:
                pr_url = self._extract_pr_url(output_path)
            self.mark_step_done(
                run_id, step_index, output_path=output_path, pr_url=pr_url
            )

        elif final_status in ("failed", "rate-limited", "timeout"):
            reason_map = {
                "failed": "Agent task reported failed",
                "rate-limited": "Claude Code rate-limited — retry manually after reset",
                "timeout": f"Step exceeded {STEP_TIMEOUT_S}s hard timeout",
            }
            self.mark_step_failed(
                run_id,
                step_index,
                reason=reason_map.get(final_status, final_status),
            )
        else:
            # Unknown terminal status
            log.error(
                "Unknown terminal status %r for step %d run=%s — marking failed",
                final_status,
                step_index,
                run_id,
            )
            self.mark_step_failed(
                run_id, step_index, reason=f"Unknown status: {final_status}"
            )

    def _mark_step_running(self, run_id: str, step_index: int) -> None:
        """Sets pipeline[step_index].status=running, started_at=now."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run = self.get_run(run_id)
        run["pipeline"][step_index]["status"] = "running"
        run["pipeline"][step_index]["started_at"] = now
        self._write_run(run_id, run)
        log.info(
            "Step %d running — run=%s role=%s",
            step_index,
            run_id,
            run["pipeline"][step_index]["role"],
        )

    def mark_step_started(self, run_id: str, step_index: int, task_id: str) -> None:
        """
        Called by AgentRunner after dispatching the agent subprocess.
        Sets task_id and started_at on the step, status=running.
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run = self.get_run(run_id)
        run["pipeline"][step_index]["task_id"] = task_id
        run["pipeline"][step_index]["status"] = "running"
        run["pipeline"][step_index]["started_at"] = now
        self._write_run(run_id, run)
        log.info(
            "Step %d started — run=%s role=%s task_id=%s",
            step_index,
            run_id,
            run["pipeline"][step_index]["role"],
            task_id,
        )

    def _update_run_field(self, run_id: str, field: str, value) -> None:
        """Atomically update a single top-level field on a run."""
        run = self.get_run(run_id)
        run[field] = value
        self._write_run(run_id, run)

    def _update_run_fields(self, run_id: str, fields: dict) -> None:
        """Atomically update multiple top-level fields on a run."""
        run = self.get_run(run_id)
        for k, v in fields.items():
            run[k] = v
        self._write_run(run_id, run)
        log.info("Run %s updated fields: %s", run_id, list(fields.keys()))

    def _run_path(self, run_id: str) -> Path:
        """
        Finds the file path for a given run_id.
        Raises FileNotFoundError if not found.
        """
        for path in RUNS_DIR.glob("*.json"):
            try:
                # Fast check: ID is the last 8 chars of the stem before extension
                # Full validation: parse JSON to be safe
                data = json.loads(path.read_text())
                if data.get("id") == run_id:
                    return path
            except Exception:
                pass
        raise FileNotFoundError(f"Run file for {run_id!r} not found in {RUNS_DIR}")

    def _write_run(self, run_id: str, run_data: dict) -> None:
        """
        Writes run_data to the run file using POSIX exclusive file locking
        (fcntl.LOCK_EX) to prevent concurrent write corruption.
        """
        path = self._run_path(run_id)
        with open(path, "r+") as f:
            try:
                # Non-blocking first attempt
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Brief sleep then blocking retry
                time.sleep(0.1)
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                f.write(json.dumps(run_data, indent=2))
                f.truncate()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _read_task_output_note(self, task_id: str) -> Optional[str]:
        """
        Reads the output_note field from a raphael task JSON.
        Returns the relative RaphBrain path string, or None on failure.
        """
        task_file = TASKS_DIR / f"{task_id}.json"
        try:
            data = json.loads(task_file.read_text())
            return data.get("output_note") or None
        except Exception:
            return None

    def _extract_pr_url(self, output_note_relative: str) -> Optional[str]:
        """
        Reads a RaphBrain output note and extracts the first GitHub PR URL.
        Used to capture the PR URL from a deployer step's output.
        """

        raphbrain = Path.home() / "Documents" / "RaphBrain"
        path = raphbrain / output_note_relative
        try:
            text = path.read_text()
            match = re.search(r"https://github\.com/[^\s\)\"\'>]+", text)
            if match:
                return match.group(0)
        except Exception:
            pass
        return None
