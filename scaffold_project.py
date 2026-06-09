"""
scaffold_project.py — Scaffold a new Hermes-managed project.

Triggered by: [HERMES] scaffold new project: <name>
Creates:
  - ~/Projects/<name>/  with src/, tests/, .github/workflows/ci.yml
  - Standard files: CLAUDE.md, pyproject.toml, .pre-commit-config.yaml
  - GitHub repo via `gh repo create`
  - RaphBrain notes: Projects/<Name>/Progress.md, Decisions.md, Gotchas.md, agents/
  - Entry in ~/raphael/teams.json
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("hermes")

PROJECTS_DIR = Path.home() / "Projects"
RAPHBRAIN = Path.home() / "Documents" / "RaphBrain"
TEAMS_JSON = Path.home() / "raphael" / "teams.json"
GITHUB_USER = "JaidenSy"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


# ---------------------------------------------------------------------------
# File templates
# ---------------------------------------------------------------------------

_CLAUDE_MD = """\
# {name_title} — Agent Context

## Stack
<!-- fill in tech stack -->

## Key Paths
- `~/Projects/{name}/` — project root

## Critical Rules
- Never commit to `main` — work on feature branches, open PRs
- Tests live in `tests/` — run with `python3 -m pytest tests/ -x -q`

## RaphBrain Project Notes
- `~/Documents/RaphBrain/Projects/{name_title}/Progress.md`
"""

_PYPROJECT_TOML = """\
[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.mypy]
ignore_missing_imports = true
"""

_PRE_COMMIT_CONFIG = """\
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.9.1
    hooks:
      - id: ruff
        args: [--fix]
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.10.0
    hooks:
      - id: mypy
        args: [--ignore-missing-imports]
  - repo: local
    hooks:
      - id: pytest
        name: pytest
        entry: python3 -m pytest tests/ -x -q --tb=short
        language: system
        pass_filenames: false
        types: [python]
"""

_CI_WORKFLOW = """\
name: CI

on:
  push:
    branches: ["**"]
  pull_request:
    branches: [main, develop]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install dependencies
        run: pip install -e ".[dev]" 2>/dev/null || pip install pytest
      - name: Run tests
        run: python -m pytest tests/ -x -q --tb=short
"""

_GITIGNORE = """\
__pycache__/
*.py[cod]
*.egg-info/
.venv/
dist/
build/
.mypy_cache/
.ruff_cache/
.pytest_cache/
*.log
.env
"""

_TEST_INIT = ""
_TEST_PLACEHOLDER = """\
\"\"\"Placeholder test — replace with real tests.\"\"\"


def test_placeholder():
    assert True
"""

_PROGRESS_MD = """\
# {name_title} — Progress

> Related: [[Daily/{today}]]

## Status
🟡 Scaffolded — not yet started

## Last Session — {today}
### Done
- Project scaffolded via Hermes

### Next
- Define stack and write first tests

## Open PRs
- (none)

## Blockers
- (none)

#project #{name}
"""

_DECISIONS_MD = """\
# {name_title} — Decisions

> Related: [[{name_title}/Progress]]

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| {today} | Project scaffolded | Initial setup via Hermes |

#decisions #{name}
"""

_GOTCHAS_MD = """\
# {name_title} — Gotchas

> Related: [[{name_title}/Progress]]

## Known Gotchas

- (none yet)

#gotchas #{name}
"""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_scaffold(project_name: str) -> str:
    """
    Scaffold a new project and return a status message (sent as iMessage reply).
    Safe to call multiple times — skips steps that are already done.
    """
    name = project_name.lower().strip()
    if not _NAME_RE.match(name):
        return (
            f"❌ Invalid project name {name!r}. "
            "Use lowercase letters, digits, hyphens, underscores only."
        )

    name_title = name.replace("-", " ").replace("_", " ").title().replace(" ", "")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    results: list[str] = []

    # 1. Local project directory
    project_dir = PROJECTS_DIR / name
    _mkdir(project_dir / "tests")
    _mkdir(project_dir / ".github" / "workflows")
    results.append(f"📁 Created {project_dir}")

    # 2. Standard files
    _write_if_missing(
        project_dir / "CLAUDE.md",
        _CLAUDE_MD.format(name=name, name_title=name_title),
    )
    _write_if_missing(
        project_dir / "pyproject.toml",
        _PYPROJECT_TOML.format(name=name),
    )
    _write_if_missing(project_dir / ".pre-commit-config.yaml", _PRE_COMMIT_CONFIG)
    _write_if_missing(project_dir / ".gitignore", _GITIGNORE)
    _write_if_missing(project_dir / "tests" / "__init__.py", _TEST_INIT)
    _write_if_missing(project_dir / "tests" / "test_placeholder.py", _TEST_PLACEHOLDER)
    _write_if_missing(project_dir / ".github" / "workflows" / "ci.yml", _CI_WORKFLOW)
    results.append("📄 Standard files written")

    # 3. Git init + initial commit
    git_status = _git_init(project_dir, name)
    results.append(git_status)

    # 4. GitHub repo
    gh_status = _create_github_repo(name, project_dir)
    results.append(gh_status)

    # 5. RaphBrain notes
    rb_status = _create_raphbrain_notes(name, name_title, today)
    results.append(rb_status)

    # 6. teams.json entry
    teams_status = _add_teams_entry(name, name_title)
    results.append(teams_status)

    summary = "\n".join(f"  {r}" for r in results)
    return f"✅ Scaffolded project '{name}':\n{summary}\n\nNext: add your stack to CLAUDE.md and write your first tests."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content)


def _git_init(project_dir: Path, name: str) -> str:
    git_dir = project_dir / ".git"
    if git_dir.exists():
        return "✓ git already initialised"
    try:
        subprocess.run(
            ["git", "init"], cwd=project_dir, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "add", "-A"], cwd=project_dir, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", f"chore: scaffold {name}"],
            cwd=project_dir,
            check=True,
            capture_output=True,
        )
        return "✓ git init + initial commit"
    except subprocess.CalledProcessError as exc:
        log.warning(f"[scaffold] git init failed: {exc}")
        return f"⚠️ git init failed: {exc.stderr.decode()[:80] if exc.stderr else exc}"


def _create_github_repo(name: str, project_dir: Path) -> str:
    """Create a private GitHub repo and add it as remote. Skip if remote already exists."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return f"✓ GitHub remote already set: {result.stdout.strip()}"
    except Exception:
        pass

    try:
        r = subprocess.run(
            [
                "gh",
                "repo",
                "create",
                f"{GITHUB_USER}/{name}",
                "--private",
                "--source",
                str(project_dir),
                "--remote",
                "origin",
                "--push",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode == 0:
            return f"✓ GitHub repo created: github.com/{GITHUB_USER}/{name}"
        return f"⚠️ gh repo create failed: {r.stderr.strip()[:120]}"
    except FileNotFoundError:
        return "⚠️ gh CLI not found — create GitHub repo manually"
    except subprocess.TimeoutExpired:
        return "⚠️ gh repo create timed out — create GitHub repo manually"


def _create_raphbrain_notes(name: str, name_title: str, today: str) -> str:
    proj_dir = RAPHBRAIN / "Projects" / name_title
    agents_dir = proj_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    _write_if_missing(
        proj_dir / "Progress.md",
        _PROGRESS_MD.format(name=name, name_title=name_title, today=today),
    )
    _write_if_missing(
        proj_dir / "Decisions.md",
        _DECISIONS_MD.format(name=name, name_title=name_title, today=today),
    )
    _write_if_missing(
        proj_dir / "Gotchas.md",
        _GOTCHAS_MD.format(name=name, name_title=name_title, today=today),
    )
    return f"✓ RaphBrain notes created at Projects/{name_title}/"


def _add_teams_entry(name: str, name_title: str) -> str:
    try:
        teams: list[dict] = (
            json.loads(TEAMS_JSON.read_text()) if TEAMS_JSON.exists() else []
        )
    except Exception:
        teams = []

    if any(t.get("id") == name for t in teams):
        return "✓ teams.json entry already exists"

    teams.append(
        {
            "id": name,
            "name": name_title,
            "description": f"{name_title} project",
            "color": "slate",
            "repo": f"~/Projects/{name}",
            "raphbrain_project": name_title,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    TEAMS_JSON.write_text(json.dumps(teams, indent=2))
    return f"✓ teams.json entry added for '{name}'"
