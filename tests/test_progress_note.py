"""
test_progress_note.py — the deterministic Progress.md writer + post-task-review
parsing/sanitizing. The write path is what makes 'done' GUARANTEE a note exists, so
it gets a real check; RAPHBRAIN_PROJECTS_DIR is patched to a tmp so the real vault is
untouched, and resolve_project is patched so folder resolution is deterministic.
"""

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engram  # noqa: E402


class _FakeProj:
    def __init__(self, raphbrain_dir):
        self.raphbrain_dir = raphbrain_dir
        self.repo = "~/Projects/x"


class _FakeEngine:
    def __init__(self, run):
        self._run = run

    def get_run(self, _run_id):
        return self._run


class TestProgressNote(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name) / "Projects"
        self._p = patch.object(engram, "RAPHBRAIN_PROJECTS_DIR", self._dir)
        self._p.start()
        # Default: no registry hit → falls back to project.capitalize() (deterministic,
        # no dependency on the real ~/Projects layout). H2 test overrides this.
        self._rp = patch.object(engram, "resolve_project", return_value=None)
        self._rp.start()

    def tearDown(self):
        self._rp.stop()
        self._p.stop()
        self._tmp.cleanup()

    def _progress(self, proj="Arbiter"):
        return self._dir / proj / "Progress.md"

    def test_creates_file_and_section_when_missing(self):
        # done ⇒ a note MUST exist even if Progress.md didn't.
        engram._append_pipeline_to_progress_note(
            "arbiter",
            "feature/x",
            "done",
            "12m",
            pr_url="http://pr/1",
            done_count=4,
            step_count=4,
        )
        body = self._progress().read_text()
        self.assertIn(engram.ENGRAM_RUN_LOG_HEADER, body)
        self.assertIn("✅ arbiter/feature/x — done", body)
        self.assertIn("4/4 steps · 12m", body)
        self.assertIn("PR: http://pr/1", body)

    def test_failed_entry_records_step_and_reason(self):
        engram._append_pipeline_to_progress_note(
            "arbiter",
            "",
            "failed",
            "3m",
            failed_step="deployer",
            reason="no PR marker",
            done_count=2,
            step_count=4,
        )
        body = self._progress().read_text()
        self.assertIn("❌ arbiter/(no branch) — failed", body)
        self.assertIn("Failed at `deployer` — no PR marker", body)

    def test_newest_first_and_preserves_human_sections(self):
        p = self._progress()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "# Arbiter — Progress\n\n## Status\nhand-curated\n\n"
            f"{engram.ENGRAM_RUN_LOG_HEADER}\n### old entry\n"
        )
        engram._append_pipeline_to_progress_note(
            "arbiter", "feature/new", "done", "1m", done_count=1, step_count=1
        )
        body = p.read_text()
        self.assertIn("hand-curated", body)  # human section survived
        self.assertLess(body.index("feature/new"), body.index("### old entry"))  # newest first

    def test_header_without_trailing_newline_stays_one_section(self):
        # a hand-edited file whose header is the last line (no trailing \n): the entry
        # must NOT glue onto the header, and a SECOND run must not append a 2nd section.
        p = self._progress()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# Arbiter — Progress\n\n{engram.ENGRAM_RUN_LOG_HEADER}")
        engram._append_pipeline_to_progress_note(
            "arbiter", "one", "done", "1m", step_count=1, done_count=1
        )
        engram._append_pipeline_to_progress_note(
            "arbiter", "two", "done", "1m", step_count=1, done_count=1
        )
        body = p.read_text()
        self.assertEqual(body.count(engram.ENGRAM_RUN_LOG_HEADER), 1)  # still one section
        self.assertIn(
            f"{engram.ENGRAM_RUN_LOG_HEADER}\n", body
        )  # header on its own line, not glued
        self.assertNotIn(f"{engram.ENGRAM_RUN_LOG_HEADER}###", body)
        self.assertLess(body.index("two"), body.index("one"))  # newest-first preserved

    def test_note_uses_registry_folder_not_capitalize(self):
        # hyphenated project must land in the registry folder, not a capitalize() orphan.
        with patch.object(engram, "resolve_project", return_value=_FakeProj("FinanceTracker")):
            engram._append_pipeline_to_progress_note(
                "finance-tracker", "b", "done", "1m", done_count=1, step_count=1
            )
        self.assertTrue((self._dir / "FinanceTracker" / "Progress.md").exists())
        self.assertFalse((self._dir / "Finance-tracker" / "Progress.md").exists())

    # --- post-task review parsing / sanitizing ---

    def test_split_review_literal_no_skill(self):
        learnings, skill = engram._split_review(
            "## Learnings\n- did a thing\n\n## Skill Candidate\nNO_SKILL"
        )
        self.assertIn("did a thing", learnings)
        self.assertEqual(skill, "")

    def test_split_review_paraphrased_no_skill_stages_nothing(self):
        # the 8B model rarely emits the literal token — prose must not become a skill.
        for prose in ("No skill needed here.", "We could maybe reuse the deploy step."):
            _, skill = engram._split_review(f"## Learnings\n- x\n\n## Skill Candidate\n{prose}")
            self.assertEqual(skill, "", prose)

    def test_split_review_extracts_real_frontmatter_skill(self):
        text = (
            "## Learnings\n- reusable flow\n\n## Skill Candidate\n"
            "---\nname: deploy-vercel-site\ndescription: ship a static site\n---\n1. build\n2. deploy"
        )
        learnings, skill = engram._split_review(text)
        self.assertIn("reusable flow", learnings)
        self.assertTrue(skill.startswith("---"))
        self.assertEqual(engram._skill_name(skill), "deploy-vercel-site")

    def test_skill_name_sanitizes_path_traversal(self):
        # the name is model output used as a path — it must never escape the staging dir.
        self.assertEqual(
            engram._skill_name("name: ../../.claude/skills/evil"), "claude-skills-evil"
        )
        self.assertEqual(engram._skill_name("name: /etc/passwd"), "etc-passwd")
        self.assertEqual(engram._skill_name('name: "My Cool Skill!"'), "my-cool-skill")
        self.assertEqual(engram._skill_name("no frontmatter"), "")

    # --- end-to-end staging path (security-relevant: model output → file on disk) ---

    def _run_review(self, review_out, staging):
        with (
            patch.object(engram, "SKILL_CANDIDATES_DIR", staging),
            patch.object(engram, "_ollama_generate", return_value=review_out),
            patch.object(engram, "_send_reply") as reply,
        ):
            engine = _FakeEngine({"task_raw": "do a thing", "pipeline": []})
            engram.spawn_post_task_review(engine, "rid", "arbiter", "feat/x", {})
            for t in list(threading.enumerate()):
                if t.name == "engram-review-rid":
                    t.join(timeout=5)
        return reply

    def test_review_stages_real_skill_and_pings(self):
        staging = self._dir.parent / "skill-candidates"
        reply = self._run_review(
            "## Learnings\n- did stuff\n\n## Skill Candidate\n"
            "---\nname: my-skill\ndescription: does a thing\n---\n1. step\n",
            staging,
        )
        self.assertEqual([f.name for f in staging.glob("*.md")], ["my-skill.md"])
        reply.assert_called_once()

    def test_review_no_skill_stages_nothing_and_no_ping(self):
        staging = self._dir.parent / "skill-candidates"
        reply = self._run_review(
            "## Learnings\n- did stuff\n\n## Skill Candidate\nNO_SKILL", staging
        )
        self.assertFalse(staging.exists() and list(staging.glob("*.md")))
        reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
