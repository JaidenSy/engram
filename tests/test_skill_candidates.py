"""
test_skill_candidates.py — the skill-candidate lifecycle (Curator concept).

Covers the money paths: promote installs a candidate into the skills dir and
archives the staged file; aging moves stale candidates (archive-only, never
delete); dedup keeps a recurring procedure from re-staging; and the name → path
sanitize can't escape the skills dir.
"""

import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import skill_candidates as sc  # noqa: E402

_SKILL = "---\nname: {name}\ndescription: {desc}\n---\n1. do the thing\n"


class SkillCandidateLifecycleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        # Redirect all three module-level dirs into the temp tree.
        self._orig = (sc.CANDIDATES_DIR, sc.ARCHIVE_DIR, sc.SKILLS_INSTALL_DIR)
        sc.CANDIDATES_DIR = root / "skill-candidates"
        sc.ARCHIVE_DIR = sc.CANDIDATES_DIR / "archive"
        sc.SKILLS_INSTALL_DIR = root / ".claude" / "skills"
        sc.CANDIDATES_DIR.mkdir(parents=True)

    def tearDown(self):
        sc.CANDIDATES_DIR, sc.ARCHIVE_DIR, sc.SKILLS_INSTALL_DIR = self._orig
        self._tmp.cleanup()

    def _stage(self, name, desc="does a thing", age_days=0):
        p = sc.CANDIDATES_DIR / f"{name}.md"
        p.write_text(_SKILL.format(name=name, desc=desc), encoding="utf-8")
        if age_days:
            old = time.time() - age_days * 86400
            os.utime(p, (old, old))
        return p

    # --- listing ---------------------------------------------------------
    def test_list_empty(self):
        self.assertIn("No skill candidates", sc.list_candidates())

    def test_list_numbers_newest_first(self):
        self._stage("older", age_days=2)
        time.sleep(0.01)
        self._stage("newer")
        out = sc.list_candidates()
        self.assertIn("1. newer", out)
        self.assertIn("2. older", out)
        self.assertIn("does a thing", out)

    # --- promote ---------------------------------------------------------
    def test_promote_installs_and_archives(self):
        self._stage("reuse-css", desc="reuse existing styles")
        msg = sc.promote("1")
        installed = sc.SKILLS_INSTALL_DIR / "reuse-css" / "SKILL.md"
        self.assertTrue(installed.exists(), "candidate should install to skills dir")
        self.assertIn("do the thing", installed.read_text())
        self.assertIn("✅ Promoted reuse-css", msg)
        # staged file archived, not deleted, not still pending
        self.assertFalse((sc.CANDIDATES_DIR / "reuse-css.md").exists())
        self.assertTrue((sc.ARCHIVE_DIR / "reuse-css.md").exists())

    def test_promote_out_of_range_installs_nothing(self):
        self._stage("only-one")
        msg = sc.promote("5")
        self.assertIn("No candidate #5", msg)
        self.assertFalse(any(sc.SKILLS_INSTALL_DIR.rglob("SKILL.md")))
        self.assertTrue((sc.CANDIDATES_DIR / "only-one.md").exists())

    def test_promote_refuses_when_already_installed(self):
        (sc.SKILLS_INSTALL_DIR / "dup").mkdir(parents=True)
        self._stage("dup")
        msg = sc.promote("1")
        self.assertIn("already installed", msg)
        # candidate stays staged so Jaiden can rename/reject it
        self.assertTrue((sc.CANDIDATES_DIR / "dup.md").exists())

    def test_promote_rejects_candidate_without_name(self):
        p = sc.CANDIDATES_DIR / "nameless.md"
        p.write_text("---\ndescription: no name here\n---\n1. step\n", encoding="utf-8")
        msg = sc.promote("1")
        self.assertIn("no valid", msg)
        self.assertFalse(any(sc.SKILLS_INSTALL_DIR.rglob("SKILL.md")))

    def test_bare_promote_lists(self):
        self._stage("x")
        self.assertIn("1. x", sc.promote(""))

    # --- reject ----------------------------------------------------------
    def test_reject_archives_without_installing(self):
        self._stage("nope")
        msg = sc.reject("1")
        self.assertIn("Rejected nope", msg)
        self.assertFalse((sc.CANDIDATES_DIR / "nope.md").exists())
        self.assertTrue((sc.ARCHIVE_DIR / "nope.md").exists())
        self.assertFalse(any(sc.SKILLS_INSTALL_DIR.rglob("SKILL.md")))

    # --- aging -----------------------------------------------------------
    def test_age_archives_stale_keeps_fresh(self):
        self._stage("fresh")
        self._stage("stale", age_days=sc.STALE_DAYS + 5)
        moved = sc.age_candidates()
        self.assertEqual(moved, 1)
        self.assertTrue((sc.CANDIDATES_DIR / "fresh.md").exists())
        self.assertFalse((sc.CANDIDATES_DIR / "stale.md").exists())
        self.assertTrue((sc.ARCHIVE_DIR / "stale.md").exists())  # archive-only

    def test_list_ages_before_showing(self):
        self._stage("old", age_days=sc.STALE_DAYS + 1)
        out = sc.list_candidates()
        self.assertIn("No skill candidates", out)  # aged out on view

    # --- dedup + safety --------------------------------------------------
    def test_already_known(self):
        self.assertFalse(sc.already_known("ghost"))
        self._stage("staged")
        self.assertTrue(sc.already_known("staged"))
        (sc.SKILLS_INSTALL_DIR / "promoted").mkdir(parents=True)
        self.assertTrue(sc.already_known("promoted"))
        self.assertFalse(sc.already_known(""))

    def test_name_sanitize_cannot_escape(self):
        # the name is model output used as a path — it must never escape the skills dir.
        self.assertEqual(sc.skill_name("name: ../../.claude/skills/evil"), "claude-skills-evil")
        self.assertEqual(sc.skill_name("name: /etc/passwd"), "etc-passwd")
        self.assertEqual(sc.skill_name('name: "My Cool Skill!"'), "my-cool-skill")
        self.assertEqual(sc.sanitize_name("../../.claude/skills/x"), "claude-skills-x")
        self.assertEqual(sc.skill_name("description: no name line"), "")
        self.assertEqual(sc.skill_name("no frontmatter"), "")


if __name__ == "__main__":
    unittest.main()
