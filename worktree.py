"""
worktree.py — per-run git worktree isolation for pipeline runs.

Every pipeline run used to share the project's single working tree: a failed
run left it dirty for the next one, and two runs could never overlap. Now each
run gets its own checkout:

    git -C <repo> worktree add <repo>/.engram-worktrees/<run_id> -b <branch> <base>

Every step of the run is dispatched with that directory as its cwd, and the
worktree is removed when the run reaches a terminal state. The *branch* is
kept (it carries the commits / the PR). Uncommitted leftovers are stashed
first (`git stash` lives in the shared repo, so it survives the removal), so a
failed run's partial work is never silently thrown away.

Config (config.yaml):

    worktrees:
      enabled: true          # false → legacy behaviour, steps run in the repo itself
      root: ""               # "" → <repo>/.engram-worktrees; else a dir, ~ allowed,
                             #      worktrees go to <root>/<repo-name>/<run_id>

Startup recovery (recover_orphans) removes worktrees whose run is no longer
running: the daemon died mid-run, or cleanup failed.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("engram")

WORKTREE_DIRNAME = ".engram-worktrees"
GIT_TIMEOUT_S = 60


class WorktreeError(RuntimeError):
    pass


def _git(repo: Path | str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
    )
    if check and proc.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def _expand(path: str) -> Path:
    return Path(path).expanduser()


def config_section(config: Optional[dict]) -> dict:
    return dict((config or {}).get("worktrees") or {})


def enabled(config: Optional[dict]) -> bool:
    return bool(config_section(config).get("enabled", True))


def worktree_root(repo: Path, config: Optional[dict] = None) -> Path:
    root = (config_section(config).get("root") or "").strip()
    if root:
        return _expand(root) / repo.name
    return repo / WORKTREE_DIRNAME


def _ensure_ignored(root: Path, repo: Path) -> None:
    """A `*` .gitignore inside the worktree root keeps the root (and itself)
    out of `git status` in the main tree, with no edit to the repo's own
    .gitignore. Only needed when the root lives inside the repo."""
    try:
        root.relative_to(repo)
    except ValueError:
        return
    root.mkdir(parents=True, exist_ok=True)
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text("# Engram per-run worktrees (worktree.py)\n*\n")


def _branch_exists(repo: Path, branch: str) -> bool:
    return (
        _git(
            repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False
        ).returncode
        == 0
    )


def _ref_exists(repo: Path, ref: str) -> bool:
    return (
        _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).returncode
        == 0
    )


def list_worktrees(repo: Path) -> list:
    """Parse `git worktree list --porcelain` → [{path, branch}] (branch may be None)."""
    out = _git(repo, "worktree", "list", "--porcelain", check=False).stdout
    items: list = []
    cur: dict = {}
    for line in out.splitlines() + [""]:
        if not line.strip():
            if cur:
                items.append(cur)
            cur = {}
        elif line.startswith("worktree "):
            cur = {"path": line[len("worktree ") :].strip(), "branch": None}
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch ") :].strip().removeprefix("refs/heads/")
    return items


def _same_path(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a) == str(b)


def create(
    repo: str | Path,
    run_id: str,
    branch: str,
    base: Optional[str],
    config: Optional[dict] = None,
) -> Path:
    """Create the run's worktree and return its absolute path.

    - New branch: `worktree add -b <branch> <path> <start>`, where start is
      origin/<base> (freshly fetched, best-effort) or the local <base>, else HEAD.
    - Existing branch: check it out as-is (a re-run / resume keeps its commits).
    - Branch checked out by a stale Engram worktree: that worktree is removed first.
    - Branch checked out in the main tree (or another non-Engram worktree): raise
      WorktreeError. git refuses a second checkout and we won't steal it.
    """
    repo = _expand(str(repo)).resolve()
    root = worktree_root(repo, config)
    path = root / run_id
    _ensure_ignored(root, repo)
    root.mkdir(parents=True, exist_ok=True)

    if path.exists():
        # Same run id twice (shouldn't happen, ids are random) or a leftover dir.
        remove(repo, path, reason="stale path before create")

    _git(repo, "worktree", "prune", check=False)
    for wt in list_worktrees(repo):
        if wt.get("branch") != branch:
            continue
        if _same_path(Path(wt["path"]).parent, root):
            log.warning(
                f"[worktree] Branch {branch!r} held by stale worktree {wt['path']}; removing"
            )
            remove(repo, Path(wt["path"]), reason="branch reclaimed by new run")
        else:
            raise WorktreeError(
                f"branch {branch!r} is already checked out at {wt['path']}; "
                "switch that checkout to another branch, or use a different branch name"
            )

    if _branch_exists(repo, branch):
        _git(repo, "worktree", "add", str(path), branch)
        log.info(f"[worktree] Run {run_id}: reusing existing branch {branch!r} at {path}")
        return path

    start = "HEAD"
    if base:
        _git(repo, "fetch", "--quiet", "origin", base, check=False)  # offline is fine
        if _ref_exists(repo, f"origin/{base}"):
            start = f"origin/{base}"
        elif _ref_exists(repo, base):
            start = base
    # --no-track: the PR head is pushed later; don't make it track the base.
    _git(repo, "worktree", "add", "--no-track", "-b", branch, str(path), start)
    log.info(f"[worktree] Run {run_id}: created {path} on new branch {branch!r} from {start}")
    return path


def remove(repo: str | Path, path: str | Path, reason: str = "") -> dict:
    """Remove a run worktree, keeping its branch. Idempotent.

    Uncommitted changes (tracked or untracked) are stashed into the shared repo
    first, so nothing a failed agent wrote is lost. Returns
    {"removed": bool, "stash": "<message>" | None}.
    """
    repo = _expand(str(repo))
    path = _expand(str(path))
    info: dict = {"removed": False, "stash": None}
    if path.exists():
        dirty = _git(path, "status", "--porcelain", check=False)
        if dirty.returncode == 0 and dirty.stdout.strip():
            msg = f"engram {path.name}: leftovers at worktree cleanup ({reason or 'terminal'})"
            st = _git(path, "stash", "push", "--include-untracked", "-m", msg, check=False)
            if st.returncode == 0:
                info["stash"] = msg
                log.warning(f"[worktree] Stashed uncommitted work from {path}: {msg!r}")
            else:
                log.warning(f"[worktree] Could not stash {path}: {st.stderr.strip()}")
        rm = _git(repo, "worktree", "remove", "--force", str(path), check=False)
        if rm.returncode != 0 and path.exists():
            log.warning(f"[worktree] git worktree remove failed for {path}: {rm.stderr.strip()}")
        info["removed"] = not path.exists()
    _git(repo, "worktree", "prune", check=False)
    return info


def recover_orphans(
    repos: Iterable[str | Path], live_run_ids: set, config: Optional[dict] = None
) -> list:
    """Startup sweep: remove every Engram worktree whose run isn't live.

    Called once from engram.main() AFTER RunEngine.startup_recover_and_cleanup(),
    so every run left `running` by a crash is already `aborted` and its worktree
    counts as an orphan. Returns the removed paths.
    """
    removed: list = []
    seen: set = set()
    for r in repos:
        try:
            repo = _expand(str(r)).resolve()
        except OSError:
            continue
        if repo in seen or not (repo / ".git").exists():
            continue
        seen.add(repo)
        root = worktree_root(repo, config)
        if not root.is_dir():
            continue
        try:
            for child in sorted(root.iterdir()):
                if not child.is_dir() or child.name in live_run_ids:
                    continue
                remove(repo, child, reason="orphaned at startup")
                removed.append(str(child))
                log.warning(f"[worktree] Startup recovery: removed orphaned worktree {child}")
        except Exception as exc:
            log.warning(f"[worktree] Orphan sweep failed for {repo}: {exc}")
    return removed
