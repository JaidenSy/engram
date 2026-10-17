"""
run_engine.py — RunEngine for Engram Mission Control.

Manages run state files in ~/engram/runs/: creation, atomic read-modify-write
updates (update_run), step state transitions, abort, startup recovery, and step
polling. The pipeline driver itself lives in engram._run_pipeline.
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

try:  # POSIX (the Mac Mini, CI). Absent on Windows → in-process lock only.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on Windows dev boxes
    fcntl = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE = Path(__file__).resolve().parent  # repo dir — rename/move-safe (follows the folder)
RUNS_DIR = _BASE / "runs"
TASKS_DIR = Path.home() / "raphael" / "tasks"
STEP_POLL_INTERVAL_S = 5  # poll raphael task JSON every 5 seconds
STEP_TIMEOUT_S = 3600  # 60-min hard timeout per step
RUN_FILE_LOCK_TIMEOUT_S = 5  # max wait to acquire file lock
RUNS_RETENTION_DAYS = 7  # delete terminal runs older than this many days
LEDGER_FILE = RUNS_DIR / "ledger.jsonl"  # append-only run outcomes, survives cleanup

log = logging.getLogger("engram")


def _append_ledger(data: dict) -> None:
    """Append one compact outcome line per run to LEDGER_FILE — the raw material
    for any future routing/learning loop, kept even after the bulky run JSON is
    pruned. Best-effort: never let ledger IO block cleanup."""
    try:
        with LEDGER_FILE.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "id": data.get("id"),
                        "project": data.get("project"),
                        "tier": data.get("tier"),
                        "status": data.get("status"),
                        "created_at": data.get("created_at"),
                        "completed_at": data.get("completed_at"),
                        "trace_id": data.get("trace_id"),
                        "pipeline": [
                            {"role": s.get("role"), "status": s.get("status")}
                            for s in data.get("pipeline", [])
                        ],
                    }
                )
                + "\n"
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Minimal PipelineStep definition (mirrors planner.py dataclass).
# If planner.py is importable, import from there; otherwise use this.
# ---------------------------------------------------------------------------

try:
    from planner import PipelineStep, PlannerResult  # type: ignore
except ImportError:
    from dataclasses import dataclass
    from typing import Optional as _Opt

    @dataclass  # type: ignore[no-redef]
    class PipelineStep:
        role: str
        parallel_group: _Opt[int] = None

    @dataclass  # type: ignore[no-redef]
    class PlannerResult:
        tier: int
        project: str
        branch_name: str
        pipeline: list
        is_direct: bool
        raw_ollama_response: str


# ---------------------------------------------------------------------------
# Locking + atomic writes
# ---------------------------------------------------------------------------

# One in-process lock per run id. flock alone would also serialise threads
# (each open() is its own open file description on Linux/macOS), but the
# threading lock keeps this correct on platforms without fcntl, and is cheap.
_RUN_LOCKS: dict = {}
_RUN_LOCKS_GUARD = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def _run_lock(run_id: str) -> Iterator[None]:
    """Exclusive lock for one run, held across a whole read-modify-write.

    The flock is taken on a sidecar `.{run_id}.lock` file, NOT on the run JSON
    itself: the JSON is replaced by rename on every write, so a lock on its
    inode would protect a file nobody reads any more.
    """
    with _RUN_LOCKS_GUARD:
        tlock = _RUN_LOCKS.setdefault(run_id, threading.Lock())
    with tlock:
        if fcntl is None:
            yield
            return
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        with open(RUNS_DIR / f".{run_id}.lock", "a+") as lf:
            deadline = time.monotonic() + RUN_FILE_LOCK_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        log.warning(
                            "Run %s lock still held after %ss — blocking",
                            run_id,
                            RUN_FILE_LOCK_TIMEOUT_S,
                        )
                        # A stall beats writing unlocked and losing an update.
                        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                        break
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON to a temp file in the same dir, fsync, then os.replace() it
    over the target: readers see the old file or the new one, never a torn
    one. The temp name doesn't end in .json, so RUNS_DIR globs skip it."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w") as f:
            f.write(json.dumps(data, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------------------------------------------------------------------------
# RunEngine
# ---------------------------------------------------------------------------


class RunEngine:
    """
    Manages the lifecycle of pipeline runs stored as JSON files in RUNS_DIR.

    Each run file: ~/engram/runs/{YYYY-MM-DD-HHMMSS}-{8-char-hex}.json

    Thread safety: every mutation goes through update_run(), which holds the
    run's lock (in-process threading.Lock + cross-process fcntl.flock on a
    sidecar lock file) across the whole read -> mutate -> write, and publishes
    the new state with an atomic temp-file rename. Reads are lockless: thanks to
    the rename they always see a complete file, just possibly a stale one.
    """

    def __init__(self):
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        log.debug("RunEngine initialised — RUNS_DIR: %s", RUNS_DIR)

    def startup_recover_and_cleanup(self) -> None:
        """Run ONCE at daemon startup: abort runs orphaned by a crash and prune
        aged-out run files. Deliberately NOT in __init__ — orchestrate_task builds
        a fresh RunEngine() per incoming message, and running recovery there aborts
        the run already in flight (the June-12 clobber race)."""
        self._recover_stale_runs()
        self._cleanup_old_runs()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_run(
        self, task_raw: str, result: "PlannerResult", single_active: bool = True
    ) -> dict:
        """
        Creates a run JSON file with status=pending.

        Raises RuntimeError if another run is already active (status=running)
        and single_active is True (the default — see runs.single_active_run in
        config.yaml; only safe to turn off once every run has its own worktree).
        Returns the full run dict.
        """
        if single_active:
            active = self.get_active_run()
            if active:
                raise RuntimeError(f"Run {active['id']!r} is already active — abort it first")

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
                "retry_count": 0,
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
            # Filled in by the pipeline thread (worktree.py / tracing.py).
            "worktree": None,
            "trace_id": None,
            "span_id": None,
        }

        _atomic_write_json(RUNS_DIR / filename, run)
        log.info(
            "Run created: id=%s project=%s tier=%d branch=%s steps=%d",
            run_id,
            result.project,
            result.tier,
            result.branch_name,
            len(pipeline),
        )
        return run

    def update_run(self, run_id: str, fn: Callable[[dict], Optional[dict]]) -> dict:
        """
        Atomic read-modify-write of one run: holds the run lock across the read,
        fn(run) and the write, so two threads mutating different steps of the same
        run (a parallel group) can never drop each other's update.

        fn mutates the dict in place (or returns a replacement dict). It runs under
        the lock, so keep it fast and side-effect free (no subprocesses, no network).
        Returns the state as written.
        """
        with _run_lock(run_id):
            path = self._run_path(run_id)
            run = json.loads(path.read_text())
            replaced = fn(run)
            if replaced is not None:
                run = replaced
            _atomic_write_json(path, run)
            return run

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
        now = _now_iso()

        def _mut(run: dict) -> None:
            step = run["pipeline"][step_index]
            step["status"] = "done"
            step["completed_at"] = now
            if output_path is not None:
                step["output_path"] = output_path
            if pr_url is not None:
                run["pr_url"] = pr_url

        run = self.update_run(run_id, _mut)
        log.info(
            "Step %d done — run=%s role=%s",
            step_index,
            run_id,
            run["pipeline"][step_index]["role"],
        )

    def mark_step_failed(self, run_id: str, step_index: int, reason: Optional[str] = None) -> None:
        """
        Sets pipeline[step_index].status=failed and run.status=failed.
        Stops pipeline advancement. An aborted run stays aborted: the abort is
        the user's decision and outranks the failure it caused (a killed session).
        """
        now = _now_iso()

        def _mut(run: dict) -> None:
            step = run["pipeline"][step_index]
            step["status"] = "failed"
            step["completed_at"] = now
            if reason:
                step["reason"] = reason
            if run.get("status") != "aborted":
                run["status"] = "failed"
                run["completed_at"] = now

        run = self.update_run(run_id, _mut)
        log.error(
            "Step %d FAILED — run=%s role=%s reason=%s",
            step_index,
            run_id,
            run["pipeline"][step_index]["role"],
            reason,
        )

    def reset_step_for_retry(self, run_id: str, step_index: int) -> None:
        """
        Resets a failed/timed-out step back to pending so the caller can
        re-dispatch it.  Increments retry_count so the caller can enforce
        a retry cap.  Does NOT change run-level status.
        """

        def _mut(run: dict) -> None:
            step = run["pipeline"][step_index]
            step["retry_count"] = step.get("retry_count", 0) + 1
            step["status"] = "pending"
            step["task_id"] = None
            step["started_at"] = None

        run = self.update_run(run_id, _mut)
        step = run["pipeline"][step_index]
        log.info(
            "Step %d reset for retry (attempt %d) — run=%s role=%s",
            step_index,
            step["retry_count"],
            run_id,
            step.get("role"),
        )

    def get_active_run(self) -> Optional[dict]:
        """
        Scans RUNS_DIR for a run with status=running.
        Returns the first match, or None.
        Only one active run is enforced by create_run() (unless single_active=False).
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

        State is committed first (under the lock) and sessions are killed after,
        so the pipeline thread, which notices the dead session, also sees
        `aborted` and stands down instead of retrying. Worktree cleanup is the
        pipeline thread's job (its finally block), or startup recovery's.
        """
        now = _now_iso()
        to_kill: list = []

        def _mut(run: dict) -> None:
            for step in run["pipeline"]:
                if step["status"] == "running" and step.get("task_id"):
                    to_kill.append(step["task_id"])
                    step["status"] = "skipped"
                elif step["status"] == "pending":
                    step["status"] = "skipped"
            run["status"] = "aborted"
            run["completed_at"] = now

        self.update_run(run_id, _mut)
        for task_id in to_kill:
            session = f"raphael-{task_id}"
            log.info("Killing tmux session: %s", session)
            subprocess.run(
                ["tmux", "kill-session", "-t", session],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
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

        session = f"raphael-{task_id}"
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

            # Fast-fail: if the agent's tmux session has died without writing a
            # terminal status, the agent crashed — don't wait out the 60-min
            # timeout. Grace of one extra tick avoids a spawn-latency false read.
            if elapsed > STEP_POLL_INTERVAL_S and (
                subprocess.run(
                    ["tmux", "has-session", "-t", session], capture_output=True
                ).returncode
                != 0
            ):
                try:  # re-read once in case the terminal write raced session exit
                    status = json.loads(task_file.read_text()).get("status", "pending")
                    if status in ("done", "failed", "needs-review", "rate-limited"):
                        return status
                except Exception:
                    pass
                log.warning(
                    "Step agent session died: run=%s step=%d task_id=%s elapsed=%ds — failing fast",
                    run_id,
                    step_index,
                    task_id,
                    elapsed,
                )
                return "failed"

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

    def _cleanup_old_runs(self) -> None:
        """Delete terminal run files (done/failed/aborted) older than RUNS_RETENTION_DAYS."""
        cutoff = datetime.now(timezone.utc).timestamp() - RUNS_RETENTION_DAYS * 86400
        deleted = 0
        for path in RUNS_DIR.glob("*.json"):
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                data = json.loads(path.read_text())
                if data.get("status") not in ("done", "failed", "aborted"):
                    continue
                _append_ledger(data)  # preserve the outcome signal before deleting
                path.unlink()
                (RUNS_DIR / f".{data.get('id')}.lock").unlink(missing_ok=True)
                deleted += 1
                log.debug("Cleanup: deleted old run %s (%s)", data.get("id"), path.name)
            except Exception:
                pass
        if deleted:
            log.info(
                "Cleanup: deleted %d run file(s) older than %d days",
                deleted,
                RUNS_RETENTION_DAYS,
            )

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
            _atomic_write_json(path, data)
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

    def mark_step_started(self, run_id: str, step_index: int, task_id: str) -> None:
        """
        Called by AgentRunner after dispatching the agent subprocess.
        Sets task_id and started_at on the step, status=running.
        """
        now = _now_iso()

        def _mut(run: dict) -> None:
            step = run["pipeline"][step_index]
            step["task_id"] = task_id
            step["status"] = "running"
            step["started_at"] = now

        run = self.update_run(run_id, _mut)
        log.info(
            "Step %d started — run=%s role=%s task_id=%s",
            step_index,
            run_id,
            run["pipeline"][step_index]["role"],
            task_id,
        )

    def update_step_fields(self, run_id: str, step_index: int, fields: dict) -> None:
        """Atomically merge fields into pipeline[step_index] (e.g. trace ids)."""

        def _mut(run: dict) -> None:
            run["pipeline"][step_index].update(fields)

        self.update_run(run_id, _mut)

    def _update_run_field(self, run_id: str, field: str, value) -> None:
        """Atomically update a single top-level field on a run."""
        self._update_run_fields(run_id, {field: value})

    def _update_run_fields(self, run_id: str, fields: dict) -> None:
        """Atomically update multiple top-level fields on a run."""
        self.update_run(run_id, lambda run: run.update(fields))
        log.info("Run %s updated fields: %s", run_id, list(fields.keys()))

    def _run_path(self, run_id: str) -> Path:
        """
        Finds the file path for a given run_id.
        Raises FileNotFoundError if not found.
        """
        # Fast path first: create_run names files {ts}-{run_id}.json.
        candidates = list(RUNS_DIR.glob(f"*-{run_id}.json")) + list(RUNS_DIR.glob("*.json"))
        for path in candidates:
            try:
                data = json.loads(path.read_text())
                if data.get("id") == run_id:
                    return path
            except Exception:
                pass
        raise FileNotFoundError(f"Run file for {run_id!r} not found in {RUNS_DIR}")

    def _write_run(self, run_id: str, run_data: dict) -> None:
        """
        Blind overwrite of a run under its lock. Prefer update_run(): a caller
        that did get_run() -> mutate -> _write_run() can still lose a concurrent
        update, because its read happened outside the lock. Kept for callers
        that genuinely replace the whole state.
        """
        with _run_lock(run_id):
            _atomic_write_json(self._run_path(run_id), run_data)

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
