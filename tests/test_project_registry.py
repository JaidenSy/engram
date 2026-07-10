"""
test_project_registry.py — unit tests for the project registry.

Hermetic: builds a registry over temp dirs so it doesn't depend on the machine's
real ~/Projects. Covers the merges that matter for routing — repo↔notes across
casing / hyphen / accent, notes-only projects, skips, and aliases.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.home() / "hermes"))
import project_registry as pr  # noqa: E402


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.projects = root / "Projects"
        self.rb = root / "RaphBrain"
        self.projects.mkdir()
        self.rb.mkdir()
        for n in ("alphabot", "finance-tracker", "vitre", "arbiter"):
            (self.projects / n).mkdir()
        for n in ("AlphaBot", "FinanceTracker", "Vitré", "Jacuzzi", "handoffs"):
            (self.rb / n).mkdir()
        self.reg = pr.build_registry(self.projects, self.rb, extra_repos={}, detect_pr_base=False)
        pr.set_test_registry(self.reg)  # so resolve()/aliases work without the real FS

    def tearDown(self):
        pr.set_test_registry(None)
        self._tmp.cleanup()

    def _get(self, name):
        return self.reg.get(pr._canon(name))

    def test_repo_and_notes_merge_same_case(self):
        a = self._get("alphabot")
        self.assertTrue(a.repo.endswith("/Projects/alphabot"))
        self.assertEqual(a.raphbrain_dir, "AlphaBot")  # real casing preserved

    def test_hyphen_vs_camelcase_merge(self):
        self.assertEqual(self._get("finance-tracker").raphbrain_dir, "FinanceTracker")

    def test_accent_insensitive_merge(self):
        self.assertEqual(self._get("vitre").raphbrain_dir, "Vitré")

    def test_notes_only_project_has_no_repo(self):
        j = self._get("jacuzzi")
        self.assertIsNone(j.repo)
        self.assertEqual(j.raphbrain_dir, "Jacuzzi")

    def test_nonproject_folder_skipped(self):
        self.assertNotIn(pr._canon("handoffs"), self.reg)

    def test_alias_resolves_to_canonical(self):
        # "mira" is an alias for vitre; _canon collapses it onto the vitre entry.
        self.assertIs(self.reg.get(pr._canon("mira")), self._get("vitre"))

    def test_norm_examples(self):
        self.assertEqual(pr._norm("Vitré"), "vitre")
        self.assertEqual(pr._norm("FinanceTracker"), "financetracker")
        self.assertEqual(pr._norm("finance-tracker"), "financetracker")

    def test_resolve_via_override(self):
        # resolve() reads the installed test registry, not the host FS.
        self.assertTrue(pr.resolve("arbiter").repo)
        self.assertIs(pr.resolve("Vitré"), pr.resolve("vitre"))  # accent
        self.assertIs(pr.resolve("mira"), pr.resolve("vitre"))  # alias
        self.assertIsNone(pr.resolve("nope-not-a-project"))


if __name__ == "__main__":
    unittest.main()
