"""
output_validator.py — Validate agent handoff notes before the pipeline advances.

Each role requires specific section markers in its output note. Validation
runs after a step reports "done"/"needs-review" and before mark_step_done()
is called. A missing marker fails the step immediately with a clear reason,
preventing silent downstream failures (e.g. deployer reading a malformed
review note and proceeding without an APPROVED verdict).
"""

from pathlib import Path

RAPHBRAIN = Path.home() / "Documents" / "RaphBrain"

# Strict per-role requirements: list of "groups". A group is a list of alternative
# markers — at least ONE must be present; ALL groups must pass. Only roles whose
# markers are LOAD-BEARING downstream live here — deployer reads the review verdict
# to gate the PR, and RunEngine parses the deployer note for the PR URL/status.
#
# Content roles (plan/research/architect/coder/tester/cleanup) are deliberately NOT
# pinned to exact headings: their role templates emit "## Scope", "## Changes",
# "## Files likely to change", etc., and hard-coding a different accepted set here
# is what silently failed every plan step (template said "## Scope", validator
# demanded "## Plan"). They pass on structure (≥1 heading) + length instead.
STRICT_ROLE_REQUIREMENTS: dict[str, list[list[str]]] = {
    # Review must declare verdict explicitly — deployer reads this to gate the PR.
    "review": [["APPROVED", "BLOCKED", "🚫 BLOCKED", "✅ APPROVED"]],
    # Deployer must have all three markers (three separate requirement groups).
    "deployer": [["## Deployer Output"], ["**PR URL:**"], ["**Status:**"]],
}

# Roles whose output is prose/structure the next agent reads, not a parsed contract.
# Valid = non-empty + at least one markdown heading (catches a raw error dump).
CONTENT_ROLES = frozenset({"plan", "research", "architect", "coder", "tester", "cleanup"})

MIN_OUTPUT_CHARS = 50


def validate_step_output(role: str, output_note: str) -> tuple[bool, str]:
    """
    Validate a step's output note before the pipeline advances.

    Args:
        role: Pipeline role name (e.g. "coder", "deployer").
        output_note: RaphBrain-relative path to the output note.

    Returns:
        (True, "")            — note is valid.
        (False, reason_str)   — note is missing required content.
    """
    strict = STRICT_ROLE_REQUIREMENTS.get(role)
    if strict is None and role not in CONTENT_ROLES:
        return True, ""  # unknown role — no validation rule, pass through

    path = RAPHBRAIN / output_note
    if not path.exists():
        return False, f"output note not found: {output_note}"

    try:
        content = path.read_text()
    except OSError as exc:
        return False, f"could not read output note: {exc}"

    if len(content.strip()) < MIN_OUTPUT_CHARS:
        return False, (
            f"output note too short ({len(content.strip())} chars) — likely empty or incomplete"
        )

    # Content roles: any structured note passes — no coupling to exact headings.
    if strict is None:
        if not any(line.lstrip().startswith("#") for line in content.splitlines()):
            return False, (
                f"role '{role}' output has no markdown heading — "
                "likely an error dump, not a real handoff note"
            )
        return True, ""

    # A deployer that legitimately can't open a PR (no GitHub repo, deploy-only
    # task) declares BLOCKED — a valid terminal state like the review role, not a
    # schema failure. Without this, a correct "no PR to open" note hard-fails the
    # whole run even though the deploy succeeded (anonuevo-survey-site, 2026-07-17).
    if role == "deployer" and ("🚫 BLOCKED" in content or "**Status:** BLOCKED" in content):
        return True, ""

    # Strict roles: every requirement group must match at least one alternative.
    missing: list[str] = []
    for group in strict:
        if not any(marker in content for marker in group):
            if len(group) == 1:
                missing.append(f"`{group[0]}`")
            else:
                # Show the shortest set of alternatives to keep message readable
                missing.append(f"one of {group}")

    if missing:
        return False, (f"role '{role}' output missing required markers: {', '.join(missing)}")

    return True, ""
