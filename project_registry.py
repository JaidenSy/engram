#!/usr/bin/env python3
"""
project_registry.py — single source of truth for Jaiden's projects.

Engram routes a Telegram task to a project → a repo path (to `cd` into) + a
RaphBrain notes folder (for context). Previously three hardcoded lists
(`PROJECT_MAP`, `KNOWN_PROJECTS`, the Ollama prompt) drifted apart and a wrong
guess ran the agent in `$HOME` against nothing.

This scans the two real locations — `~/Projects/` and
`~/Documents/RaphBrain/Projects/` — and merges them, so adding a project means
creating the folder, not editing code. Matching is accent/case/punctuation
insensitive (`Vitré` == `vitre`, `FinanceTracker` == `finance-tracker`). A small
alias/skip table covers rebrands and repos that don't live under `~/Projects/`.
"""

from __future__ import annotations

import subprocess
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

PROJECTS_DIR = Path.home() / "Projects"
RAPHBRAIN_PROJECTS_DIR = Path.home() / "Documents" / "RaphBrain" / "Projects"

# Repos that don't live under ~/Projects/.
_EXTRA_REPOS = {
    # The daemon's own repo — resolves from this file's location (move-safe).
    "engram": Path(__file__).resolve().parent,
    "raphael": Path.home() / "raphael",
}

# RaphBrain/Projects/ subfolders that aren't dev projects (norm keys).
_RAPHBRAIN_SKIP = {
    "handoffs",
    "general",
    "career",
    "freelancewebdev",
    "jarvis",
    "odyssey",
    "raphbrain",
    "aipoweredapigateway",  # legacy Arbiter note; the real one is Projects/Arbiter
}

# norm(alias) -> norm(canonical). Applied to every scanned name AND every lookup,
# so rebrands / alt spellings collapse into one entry. Only for names that are
# genuinely the same project — never merge two distinct repos.
_ALIASES = {
    "raphui": "raphael",
    "raph": "raphael",
    "mira": "vitre",  # rebranded Mira -> Vitré
    "soka": "kirnu",  # rebranded Sōka -> Kirnu
    "nexvault": "arbiter",  # dead names for Arbiter
    "nexusgateai": "arbiter",
    "dropshipping": "dropshippingautomation",
    "aiemailleadgen": "emailleadgen",
}


def _norm(name: str) -> str:
    """Accent/case/punctuation-insensitive key: 'Vitré'→'vitre', 'FinanceTracker'→'financetracker'."""
    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return "".join(c for c in ascii_.lower() if c.isalnum())


def _canon(name: str) -> str:
    """Normalised key with aliases applied."""
    key = _norm(name)
    return _ALIASES.get(key, key)


@dataclass
class Project:
    name: str  # display / type-able name
    repo: Optional[str]  # repo path with ~ (e.g. "~/Projects/alphabot"), or None (notes-only)
    raphbrain_dir: Optional[str]  # real RaphBrain folder name (correct casing), or None
    pr_base: Optional[str]  # default branch for PRs (origin HEAD), or None (no remote)


def _detect_pr_base(repo: Path) -> Optional[str]:
    """Return the repo's default branch (origin HEAD), or None if no remote."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip().replace("origin/", "") or None
    except Exception:
        pass
    return None


def build_registry(
    projects_dir: Path = PROJECTS_DIR,
    raphbrain_dir: Path = RAPHBRAIN_PROJECTS_DIR,
    extra_repos: Optional[dict] = None,
    detect_pr_base: bool = False,
) -> dict:
    """Scan the filesystem and return {canonical_key: Project}."""
    extra_repos = _EXTRA_REPOS if extra_repos is None else extra_repos
    reg: dict = {}

    def _repo_iter():
        if projects_dir.is_dir():
            for d in sorted(projects_dir.iterdir()):
                if d.is_dir() and not d.name.startswith("."):
                    yield d.name, d
        for name, path in extra_repos.items():
            if path.is_dir():
                yield name, path

    # 1. Repos
    for name, path in _repo_iter():
        key = _canon(name)
        reg[key] = Project(
            name=name,
            repo=str(path).replace(str(Path.home()), "~"),
            raphbrain_dir=None,
            pr_base=_detect_pr_base(path) if detect_pr_base else None,
        )

    # 2. RaphBrain notes folders — merge onto repos, or add notes-only entries
    if raphbrain_dir.is_dir():
        for d in sorted(raphbrain_dir.iterdir()):
            if not d.is_dir():
                continue
            key = _canon(d.name)
            if key in _RAPHBRAIN_SKIP:
                continue
            if key in reg:
                reg[key].raphbrain_dir = d.name
            else:
                reg[key] = Project(
                    name=d.name.lower(), repo=None, raphbrain_dir=d.name, pr_base=None
                )

    return reg


_TEST_REGISTRY: Optional[dict] = None


@lru_cache(maxsize=1)
def _cached_registry() -> dict:
    return build_registry()


def get_registry() -> dict:
    """Cached registry built from the real filesystem (once per process).

    Tests install a fixed registry via set_test_registry() so they don't depend
    on the host's ~/Projects (which doesn't exist on CI).
    """
    if _TEST_REGISTRY is not None:
        return _TEST_REGISTRY
    return _cached_registry()


def set_test_registry(reg: Optional[dict]) -> None:
    """Override the registry for tests; pass None to restore filesystem scanning."""
    global _TEST_REGISTRY
    _TEST_REGISTRY = reg
    pr_base.cache_clear()


def resolve(name: str) -> Optional[Project]:
    """Look up a project by any name / alias / casing. Returns Project or None."""
    if not name:
        return None
    return get_registry().get(_canon(name))


@lru_cache(maxsize=64)
def pr_base(name: str) -> Optional[str]:
    """Default PR branch for a project's repo (origin HEAD), computed on demand.

    Kept off the registry hot path so name/path resolution needs no git subprocess.
    """
    p = resolve(name)
    if not p or not p.repo:
        return None
    return _detect_pr_base(Path(p.repo.replace("~", str(Path.home()))))


def project_names() -> list:
    """Sorted display names — for the `projects` command and the Ollama enum."""
    return sorted(p.name for p in get_registry().values())


def project_map_text() -> str:
    """Compact reference block injected into direct-task prompts."""
    lines = []
    for p in sorted(get_registry().values(), key=lambda p: p.name):
        loc = p.repo or f"notes-only: ~/Documents/RaphBrain/Projects/{p.raphbrain_dir}"
        lines.append(f"- {p.name}: {loc}")
    return "Known projects (use these paths directly — do not ask for URLs/paths):\n" + "\n".join(
        lines
    )


if __name__ == "__main__":
    reg = build_registry(detect_pr_base=True)
    print(f"{len(reg)} projects discovered:\n")
    for key in sorted(reg):
        p = reg[key]
        print(
            f"  {p.name:24} repo={p.repo or '—':28} notes={p.raphbrain_dir or '—':20} pr_base={p.pr_base or '—'}"
        )
    # Self-checks against known real projects.
    assert resolve("arbiter") and resolve("arbiter").repo, "arbiter repo missing"
    assert resolve("Vitré") is resolve("vitre"), "accent match broken"
    assert resolve("FinanceTracker") is resolve("finance-tracker"), "casing/hyphen match broken"
    assert resolve("mira") is resolve("vitre"), "alias broken"
    print("\nself-checks passed")
