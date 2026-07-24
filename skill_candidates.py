"""Skill-candidate lifecycle — the Curator concept, ported from Nous hermes-agent.

Engram's post-task review (see engram.spawn_post_task_review) drafts reusable
skills as staged candidates in ~/engram/skill-candidates/. Without a lifecycle
those candidates just pile up. This module is the deterministic, LLM-free
lifecycle Nous's curator runs weekly, applied to Engram's staging dir and driven
from the phone:

    skills        — list staged candidates (ages stale ones first)
    promote <n>   — install candidate #n into ~/.claude/skills/<name>/SKILL.md
    reject <n>    — dismiss candidate #n

Design mirrors the handoff lifecycle (engram._list_handoffs et al): a flat store
with an archive/ subdir, newest-first, an index-only <n> selector, and
archive-only (never delete, so a mis-fire is recoverable). Aging is lazy —
swept when the list is viewed — so there's no extra daemon thread.

ponytail: the human promote gate IS the trust boundary (Nous says the same of
its own scanner). The candidate is drafted by a local 8B model over Engram's own
agent notes and Jaiden reads it before promoting, so promote does path-safety
(the name becomes a path) but not a full malware scan.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("engram")

# Same dir engram.py stages into (move-safe, follows the folder).
CANDIDATES_DIR = Path(__file__).resolve().parent / "skill-candidates"
ARCHIVE_DIR = CANDIDATES_DIR / "archive"
# Globally-active skills dir — promote's ONLY write target. A candidate is never
# written here directly (that would skip the review gate); promote moves it here
# by hand once Jaiden has read it.
SKILLS_INSTALL_DIR = Path.home() / ".claude" / "skills"
STALE_DAYS = 30


def sanitize_name(raw: str) -> str:
    """Kebab-case a skill name down to a safe bare filename ("" if it reduces to
    nothing). Load-bearing security: the name becomes a path, so an unsanitized
    `../../.claude/skills/x` must not escape the staging dir."""
    return re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")[:60]


def skill_name(skill_md: str) -> str:
    """Pull the sanitized kebab `name:` from a candidate's frontmatter ("" if none)."""
    for line in skill_md.splitlines():
        if line.strip().startswith("name:"):
            return sanitize_name(line.split("name:", 1)[1].strip().strip("\"'"))
    return ""


def _description(skill_md: str) -> str:
    for line in skill_md.splitlines():
        if line.strip().startswith("description:"):
            return line.split("description:", 1)[1].strip().strip("\"'")[:60]
    return ""


def already_known(name: str) -> bool:
    """True if a candidate with this name is already staged or already promoted —
    so the post-task reviewer can skip re-drafting (and re-pinging) the same skill
    every run. Dedup by name is the lazy analog of Nous's use_count tracking."""
    if not name:
        return False
    return (CANDIDATES_DIR / f"{name}.md").exists() or (SKILLS_INSTALL_DIR / name).is_dir()


def _pending() -> list[Path]:
    """Staged candidates, newest first. The top-level glob skips archive/."""
    if not CANDIDATES_DIR.exists():
        return []
    files = [p for p in CANDIDATES_DIR.glob("*.md") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _archive(p: Path) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    p.rename(ARCHIVE_DIR / p.name)


def age_candidates(stale_days: int = STALE_DAYS) -> int:
    """Archive candidates older than stale_days (deterministic, no LLM). Returns
    the count moved. Archive-only, so an aged-out candidate is still recoverable
    from archive/. Swept lazily when the list is viewed."""
    cutoff = datetime.now(timezone.utc).timestamp() - stale_days * 86400
    moved = 0
    for p in _pending():
        if p.stat().st_mtime < cutoff:
            try:
                _archive(p)
                moved += 1
            except OSError as exc:
                log.warning("[candidates] could not archive %s: %s", p.name, exc)
    if moved:
        log.info("[candidates] aged out %d stale candidate(s) → archive/", moved)
    return moved


def list_candidates() -> str:
    """Numbered, newest-first list for the phone. Ages stale ones first so the
    list never shows a rotting pile."""
    age_candidates()
    pend = _pending()
    if not pend:
        return "✅ No skill candidates staged."
    now = datetime.now(timezone.utc)
    lines = ["Skill candidates — `promote <n>` to install, `reject <n>` to dismiss:"]
    for i, p in enumerate(pend, 1):
        try:
            desc = _description(p.read_text(encoding="utf-8"))
        except OSError:
            desc = "(unreadable)"
        days = int((now.timestamp() - p.stat().st_mtime) / 86400)
        age = "today" if days == 0 else f"{days}d ago"
        lines.append(f"{i}. {p.stem} — {desc or '(no description)'}  ({age})")
    return "\n".join(lines)


def _select(arg: str) -> Path | None:
    """Resolve an index-only selector into a staged candidate. Numeric-only and
    bounds-checked — an out-of-range number returns None rather than falling
    through to a substring match (same rule as resume <n>)."""
    pend = _pending()
    sel = arg.strip().split()[0] if arg.strip() else ""
    if sel.isdigit():
        i = int(sel)
        return pend[i - 1] if 1 <= i <= len(pend) else None
    return None


def promote(arg: str) -> str:
    """Install candidate #n into ~/.claude/skills/<name>/SKILL.md, then archive the
    staged file. Re-sanitizes the name and confirms the resolved path stays inside
    the skills dir — the frontmatter is read fresh from a file Jaiden could have
    hand-edited, so the staging-time sanitize isn't trusted here."""
    if not arg.strip():  # bare `promote` → show the list to pick from
        return list_candidates()
    if not _pending():
        return "✅ No skill candidates staged. Send `skills` to check."
    chosen = _select(arg)
    if chosen is None:
        return f"❓ No candidate #{arg.strip()}. Send `skills` to list them."

    body = chosen.read_text(encoding="utf-8")
    name = skill_name(body)
    if not name:
        return f"⚠️ {chosen.stem} has no valid `name:` — can't promote. Fix or reject it."

    dest_dir = SKILLS_INSTALL_DIR / name
    dest = dest_dir / "SKILL.md"
    # Belt-and-suspenders: the resolved install path must stay inside the skills dir.
    if dest.resolve().parent.parent != SKILLS_INSTALL_DIR.resolve():
        return f"🚫 Refusing to promote {name!r} — resolves outside the skills dir."
    if dest_dir.exists():
        return f"⚠️ A skill named {name!r} is already installed. Reject the candidate or rename it."

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding="utf-8")
    _archive(chosen)
    log.info("[candidates] promoted %s → %s", chosen.stem, dest)
    return f"✅ Promoted {name} → {dest}\nActive in every Claude Code session now."


def reject(arg: str) -> str:
    """Archive candidate #n without installing it (recoverable from archive/)."""
    if not arg.strip():  # bare `reject` → show the list to pick from
        return list_candidates()
    if not _pending():
        return "✅ No skill candidates staged."
    chosen = _select(arg)
    if chosen is None:
        return f"❓ No candidate #{arg.strip()}. Send `skills` to list them."
    _archive(chosen)
    log.info("[candidates] rejected %s → archive/", chosen.stem)
    return f"🗑️ Rejected {chosen.stem} → archive/."
