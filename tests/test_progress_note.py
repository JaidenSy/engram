"""
test_progress_note.py — the deterministic Progress.md writer + post-task-review
parsing. The write path is what makes 'done' GUARANTEE a note exists, so it gets a
real check; RAPHBRAIN_PROJECTS_DIR is patched to a tmp so the real vault is untouched.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path.home() / "hermes"))
import hermes  # noqa: E402


class TestProgressNote(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name) / "Projects"
        self._p = patch.object(hermes, "RAPHBRAIN_PROJECTS_DIR", self._dir)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def _progress(self, proj="Arbiter"):
        return self._dir / proj / "Progress.md"

    def test_creates_file_and_section_when_missing(self):
        # done ⇒ a note MUST exist even if Progress.md didn't.
        hermes._append_pipeline_to_progress_note(
            "arbiter",
            "feature/x",
            "done",
            "12m",
            pr_url="http://pr/1",
            done_count=4,
            step_count=4,
        )
        body = self._progress().read_text()
        self.assertIn(hermes.HERMES_RUN_LOG_HEADER, body)
        self.assertIn("✅ arbiter/feature/x — done", body)
        self.assertIn("4/4 steps · 12m", body)
        self.assertIn("PR: http://pr/1", body)

    def test_failed_entry_records_step_and_reason(self):
        hermes._append_pipeline_to_progress_note(
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
            f"{hermes.HERMES_RUN_LOG_HEADER}\n### old entry\n"
        )
        hermes._append_pipeline_to_progress_note(
            "arbiter", "feature/new", "done", "1m", done_count=1, step_count=1
        )
        body = p.read_text()
        self.assertIn("hand-curated", body)  # human section survived
        # newest entry sits above the old one under the header
        self.assertLess(body.index("feature/new"), body.index("### old entry"))

    def test_split_review_no_skill(self):
        learnings, skill = hermes._split_review(
            "## Learnings\n- did a thing\n\n## Skill Candidate\nNO_SKILL"
        )
        self.assertIn("did a thing", learnings)
        self.assertEqual(skill, "")

    def test_split_review_extracts_skill_and_name(self):
        text = (
            "## Learnings\n- reusable flow\n\n## Skill Candidate\n"
            "---\nname: deploy-vercel-site\ndescription: ship a static site\n---\n1. build\n2. deploy"
        )
        learnings, skill = hermes._split_review(text)
        self.assertIn("reusable flow", learnings)
        self.assertIn("name: deploy-vercel-site", skill)
        self.assertEqual(hermes._skill_name(skill), "deploy-vercel-site")


if __name__ == "__main__":
    unittest.main()
