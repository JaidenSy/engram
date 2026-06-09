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

# Per-role requirements: list of "groups".
# A group is a list of alternative markers — at least ONE must be present.
# ALL groups must pass. Order is arbitrary.
ROLE_REQUIREMENTS: dict[str, list[list[str]]] = {
    "plan": [["## Plan", "## Summary", "## Next Steps", "## Approach"]],
    "research": [["## Findings", "## Summary", "## Research", "## Results"]],
    "architect": [["## Architecture", "## Design", "## Summary", "## Approach"]],
    "coder": [
        [
            "## Summary",
            "## Changes",
            "## Files Modified",
            "## What Was Done",
            "## Implementation",
        ]
    ],
    "tester": [["## Test Results", "## Tests", "## Summary", "## Results"]],
    "cleanup": [["## Summary", "## Changes", "## Cleanup", "## What Was Done"]],
    # Review must declare verdict explicitly — deployer reads this to gate the PR.
    "review": [["APPROVED", "BLOCKED", "🚫 BLOCKED", "✅ APPROVED"]],
    # Deployer must have all three markers (three separate requirement groups).
    "deployer": [["## Deployer Output"], ["**PR URL:**"], ["**Status:**"]],
}

MIN_OUTPUT_CHARS = 50


def validate_step_output(role: str, output_note: str) -> tuple[bool, str]:
    """
    Validate a step's output note against role-specific required markers.

    Args:
        role: Pipeline role name (e.g. "coder", "deployer").
        output_note: RaphBrain-relative path to the output note.

    Returns:
        (True, "")            — note is valid.
        (False, reason_str)   — note is missing required content.
    """
    requirements = ROLE_REQUIREMENTS.get(role)
    if not requirements:
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
            f"output note too short ({len(content.strip())} chars) — "
            "likely empty or incomplete"
        )

    missing: list[str] = []
    for group in requirements:
        if not any(marker in content for marker in group):
            if len(group) == 1:
                missing.append(f"`{group[0]}`")
            else:
                # Show the shortest set of alternatives to keep message readable
                missing.append(f"one of {group}")

    if missing:
        return False, (
            f"role '{role}' output missing required markers: {', '.join(missing)}"
        )

    return True, ""
