"""
test_hermes_endpoints.py — Integration tests for server.py Hermes endpoints.

Tests cover:
  1. GET /api/hermes/runs → list with seeded run
  2. GET /api/hermes/runs/{id} → correct run detail
  3. GET /api/hermes/active → null when no running, correct run when one exists
  4. GET /api/hermes/triggers → list of strings (may be empty — should not 500)
  5. GET /api/hermes/runs/{id}/log/{step_index} → 404 or empty when no log (should not 500)
  6. GET /api/hermes/runs/nonexistent → 404
  7. POST /api/hermes/runs/{id}/abort → aborts run
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# server.py is in ~/raphael/ui/
SERVER_DIR = Path.home() / "raphael" / "ui"
sys.path.insert(0, str(SERVER_DIR))


def _make_run_json(
    run_id: str = "test1234",
    status: str = "done",
    project: str = "hermes",
    tier: int = 2,
) -> dict:
    """Build a minimal run dict for seeding test HERMES_RUNS_DIR."""
    return {
        "id": run_id,
        "task_raw": "Test task",
        "project": project,
        "tier": tier,
        "branch": "feature/test",
        "pipeline": [
            {
                "role": "coder",
                "status": "done" if status == "done" else "running",
                "task_id": "2026-06-07-001" if status == "running" else None,
                "started_at": "2026-06-07T10:00:00Z",
                "completed_at": "2026-06-07T10:05:00Z" if status == "done" else None,
                "output_path": None,
                "parallel_group": None,
            },
            {
                "role": "tester",
                "status": "pending" if status != "done" else "done",
                "task_id": None,
                "started_at": None,
                "completed_at": "2026-06-07T10:10:00Z" if status == "done" else None,
                "output_path": None,
                "parallel_group": None,
            },
        ],
        "status": status,
        "created_at": "2026-06-07T10:00:00Z",
        "completed_at": "2026-06-07T10:15:00Z" if status == "done" else None,
        "pr_url": None,
    }


class TestHermesEndpoints(unittest.TestCase):
    """Integration tests using FastAPI TestClient with a temp HERMES_RUNS_DIR."""

    def setUp(self):
        self._runs_tmpdir = tempfile.TemporaryDirectory()
        self._log_tmpdir = tempfile.TemporaryDirectory()
        self.runs_path = Path(self._runs_tmpdir.name)
        self.log_path = Path(self._log_tmpdir.name) / "hermes.log"

        # Patch HERMES_RUNS_DIR and HERMES_LOG in server module before importing app
        import server as srv

        self._orig_runs_dir = srv.HERMES_RUNS_DIR
        self._orig_log = srv.HERMES_LOG
        srv.HERMES_RUNS_DIR = self.runs_path
        srv.HERMES_LOG = self.log_path
        self.srv = srv

        from fastapi.testclient import TestClient

        self.client = TestClient(srv.app, raise_server_exceptions=True)

    def tearDown(self):
        self.srv.HERMES_RUNS_DIR = self._orig_runs_dir
        self.srv.HERMES_LOG = self._orig_log
        self._runs_tmpdir.cleanup()
        self._log_tmpdir.cleanup()

    def _seed_run(self, run_dict: dict):
        """Write a run JSON file to the temp runs directory."""
        ts = (
            run_dict["created_at"]
            .replace(":", "")
            .replace("-", "")
            .replace("T", "-")
            .replace("Z", "")
        )
        fname = f"{run_dict['created_at'][:10].replace('-', '-')}-{run_dict['id']}.json"
        (self.runs_path / fname).write_text(json.dumps(run_dict, indent=2))

    # ------------------------------------------------------------------
    # GET /api/hermes/runs
    # ------------------------------------------------------------------

    def test_get_runs_empty_list(self):
        """No run files → returns empty list."""
        response = self.client.get("/api/hermes/runs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_get_runs_returns_seeded_run(self):
        """Seeded run file should appear in list."""
        run = _make_run_json(run_id="abc00001", status="done")
        self._seed_run(run)

        response = self.client.get("/api/hermes/runs")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], "abc00001")
        self.assertEqual(data[0]["status"], "done")

    def test_get_runs_includes_step_count_fields(self):
        """Response should include step_count, done_count, current_role augmentations."""
        run = _make_run_json(run_id="abc00002", status="done")
        self._seed_run(run)

        response = self.client.get("/api/hermes/runs")
        data = response.json()
        self.assertIn("step_count", data[0])
        self.assertIn("done_count", data[0])
        self.assertIn("current_role", data[0])
        self.assertEqual(data[0]["step_count"], 2)
        self.assertEqual(data[0]["done_count"], 2)
        self.assertIsNone(data[0]["current_role"])  # no running steps

    def test_get_runs_status_filter(self):
        """status= query param filters runs."""
        done_run = _make_run_json(run_id="done0001", status="done")
        running_run = _make_run_json(run_id="run00001", status="running")
        self._seed_run(done_run)
        self._seed_run(running_run)

        response = self.client.get("/api/hermes/runs?status=done")
        data = response.json()
        ids = [r["id"] for r in data]
        self.assertIn("done0001", ids)
        self.assertNotIn("run00001", ids)

    def test_get_runs_current_role_for_running(self):
        """A running run should show the role of the running step."""
        run = _make_run_json(run_id="run00002", status="running")
        self._seed_run(run)

        response = self.client.get("/api/hermes/runs")
        data = response.json()
        run_data = next(r for r in data if r["id"] == "run00002")
        self.assertEqual(run_data["current_role"], "coder")

    # ------------------------------------------------------------------
    # GET /api/hermes/runs/{id}
    # ------------------------------------------------------------------

    def test_get_run_by_id(self):
        """GET /api/hermes/runs/{id} returns the correct run."""
        run = _make_run_json(run_id="specific01", status="done")
        self._seed_run(run)

        response = self.client.get("/api/hermes/runs/specific01")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["id"], "specific01")
        self.assertEqual(data["project"], "hermes")

    def test_get_run_by_id_not_found(self):
        """GET /api/hermes/runs/nonexistent → 404."""
        response = self.client.get("/api/hermes/runs/nonexistent-id")
        self.assertEqual(response.status_code, 404)

    # ------------------------------------------------------------------
    # GET /api/hermes/active
    # ------------------------------------------------------------------

    def test_get_active_returns_null_when_none(self):
        """No running runs → active=null."""
        response = self.client.get("/api/hermes/active")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("active", data)
        self.assertIsNone(data["active"])

    def test_get_active_returns_run_when_running(self):
        """A running run should appear as active."""
        run = _make_run_json(run_id="active001", status="running")
        self._seed_run(run)

        response = self.client.get("/api/hermes/active")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNotNone(data["active"])
        self.assertEqual(data["active"]["id"], "active001")
        self.assertEqual(data["active"]["status"], "running")

    def test_get_active_returns_null_for_done_run(self):
        """Done run does not appear as active."""
        run = _make_run_json(run_id="done9999", status="done")
        self._seed_run(run)

        response = self.client.get("/api/hermes/active")
        data = response.json()
        self.assertIsNone(data["active"])

    # ------------------------------------------------------------------
    # GET /api/hermes/triggers
    # ------------------------------------------------------------------

    def test_get_triggers_no_log_returns_empty_not_500(self):
        """Log file absent → returns {lines: [], log_path: ...}, not 500."""
        # log_path does NOT exist (we only created the directory entry)
        response = self.client.get("/api/hermes/triggers")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("lines", data)
        self.assertIsInstance(data["lines"], list)
        self.assertEqual(len(data["lines"]), 0)

    def test_get_triggers_with_log_returns_trigger_lines(self):
        """Log file with iMessage task lines → filters and returns them."""
        log_content = (
            "2026-06-07 10:00:00 [INFO] Some startup message\n"
            "2026-06-07 10:01:00 [INFO] iMessage task: add dark mode toggle...\n"
            "2026-06-07 10:02:00 [INFO] Some other log line\n"
            "2026-06-07 10:03:00 [INFO] Task file detected: test.task\n"
            "2026-06-07 10:04:00 [INFO] Another unrelated line\n"
        )
        self.log_path.write_text(log_content)

        response = self.client.get("/api/hermes/triggers")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        lines = data["lines"]
        self.assertIsInstance(lines, list)
        # Should contain the two trigger lines
        trigger_text = " ".join(lines)
        self.assertIn("iMessage task:", trigger_text)
        self.assertIn("Task file detected:", trigger_text)
        # Should NOT contain unrelated lines
        self.assertNotIn("startup message", trigger_text)

    def test_get_triggers_returns_log_path(self):
        """Response should include the log_path field."""
        response = self.client.get("/api/hermes/triggers")
        data = response.json()
        self.assertIn("log_path", data)

    # ------------------------------------------------------------------
    # GET /api/hermes/runs/{id}/log/{step_index}
    # ------------------------------------------------------------------

    def test_step_log_run_not_found_returns_404(self):
        """Non-existent run ID → 404, not 500."""
        response = self.client.get("/api/hermes/runs/nonexistent/log/0")
        self.assertEqual(response.status_code, 404)

    def test_step_log_step_not_dispatched_returns_text(self):
        """Step with no task_id → plain text response (not 500)."""
        run = _make_run_json(run_id="logtest01", status="done")
        # Ensure step has no task_id
        run["pipeline"][0]["task_id"] = None
        self._seed_run(run)

        response = self.client.get("/api/hermes/runs/logtest01/log/0")
        # Should be 200 with plain text, not 500
        self.assertEqual(response.status_code, 200)
        self.assertIn("not yet dispatched", response.text.lower())

    def test_step_log_step_index_out_of_range_returns_404(self):
        """Step index >= pipeline length → 404."""
        run = _make_run_json(run_id="logtest02", status="done")
        self._seed_run(run)

        response = self.client.get("/api/hermes/runs/logtest02/log/99")
        self.assertEqual(response.status_code, 404)

    def test_step_log_no_log_file_returns_text_not_500(self):
        """Step dispatched (has task_id) but no log file → returns text, not 500."""
        run = _make_run_json(run_id="logtest03", status="running")
        # coder step already has task_id set from _make_run_json(status="running")
        self._seed_run(run)

        response = self.client.get("/api/hermes/runs/logtest03/log/0")
        self.assertEqual(response.status_code, 200)
        # Should mention "no log" or task id
        self.assertIn("2026-06-07-001", response.text)

    # ------------------------------------------------------------------
    # POST /api/hermes/runs/{id}/abort
    # ------------------------------------------------------------------

    def test_abort_run_sets_status_aborted(self):
        """POST abort on a pending/running run → status=aborted."""
        run = _make_run_json(run_id="abort001", status="running")
        self._seed_run(run)

        with patch("server.subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=0)
            response = self.client.post("/api/hermes/runs/abort001/abort")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "aborted")

    def test_abort_run_not_found_returns_404(self):
        """Aborting nonexistent run → 404."""
        response = self.client.post("/api/hermes/runs/doesnotexist/abort")
        self.assertEqual(response.status_code, 404)

    def test_abort_already_done_run_returns_400(self):
        """Aborting a done run → 400."""
        run = _make_run_json(run_id="done0002", status="done")
        self._seed_run(run)

        response = self.client.post("/api/hermes/runs/done0002/abort")
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
