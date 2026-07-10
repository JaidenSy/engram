#!/usr/bin/env python3
"""
planner.py — Task classification module for Hermes Mission Control.

Classifies incoming task text into a tier, project, branch name, and pipeline
using a local Ollama (llama3.1:8b) model. Falls back gracefully on failure.

Usage:
    from planner import classify_task, PlannerResult, PipelineStep
    result = classify_task("Add dark mode toggle to Arbiter dashboard")
"""

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from project_registry import project_names, resolve

log = logging.getLogger("hermes")

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PipelineStep:
    role: str  # plan|research|architect|coder|tester|cleanup|review|deployer
    parallel_group: Optional[int]  # None = sequential; int = run with all same-int steps


@dataclass
class PlannerResult:
    tier: int  # 0 (direct), 1, 2, or 3
    project: str  # e.g. "arbiter", "hermes", "raph-ui"
    branch_name: str  # e.g. "feature/dark-mode-toggle"
    pipeline: list  # list[PipelineStep]
    is_direct: bool  # True = skip pipeline, call run_task() directly
    raw_ollama_response: str  # stored for debug, not used downstream
    scaffold_project: Optional[str] = None  # set when this is a scaffold request


# ---------------------------------------------------------------------------
# Project detection — backed by project_registry (single source of truth)
# ---------------------------------------------------------------------------

# Explicit target: "on arbiter, fix the login test" / "in alphabot: rebalance"
_EXPLICIT_TARGET_RE = re.compile(
    r"^\s*(?:on|in|for)\s+([\w.\-]+)\s*[,:]\s*(.+)$", re.IGNORECASE | re.DOTALL
)
# Bare "arbiter: fix X" (only accepted when the prefix resolves to a real project)
_COLON_TARGET_RE = re.compile(r"^\s*([\w.\-]+)\s*:\s*(.+)$", re.DOTALL)


def _parse_explicit_target(task_text: str) -> tuple:
    """If the task names a project up front, return (canonical_name, remaining_task).

    Deterministic routing — when Jaiden says "on <project>", never leave it to the
    8B model's guess. Returns (None, task_text) when no known project is named.
    """
    for rx in (_EXPLICIT_TARGET_RE, _COLON_TARGET_RE):
        m = rx.match(task_text)
        if m:
            p = resolve(m.group(1))
            if p:
                return p.name, m.group(2).strip()
    return None, task_text


def _detect_project(task_text: str) -> Optional[str]:
    """Return the canonical name of the first known project mentioned, else None."""
    for word in re.findall(r"[\w.\-]+", task_text):
        p = resolve(word)
        if p:
            return p.name
    return None


# ---------------------------------------------------------------------------
# Privacy keywords — must NOT be sent to Ollama
# ---------------------------------------------------------------------------

PRIVACY_KEYWORDS = [
    "arbiter",
    "investor",
    "fundraising",
    "strategy",
    "supplier",
    "credentials",
    "api key",
    "secret",
    "private",
    "confidential",
]

# ---------------------------------------------------------------------------
# Scaffold-task detection
# ---------------------------------------------------------------------------

_SCAFFOLD_RE = re.compile(
    r"scaffold(?:\s+new)?\s+project[:\s]+([a-z0-9][a-z0-9_-]*)",
    re.IGNORECASE,
)


def _scaffold_project_name(task_text: str) -> Optional[str]:
    """Return the normalised project name if this is a scaffold request, else None."""
    m = _SCAFFOLD_RE.search(task_text)
    if m:
        return m.group(1).lower()
    return None


# ---------------------------------------------------------------------------
# Direct-task detection keywords
# ---------------------------------------------------------------------------

DIRECT_KEYWORDS = [
    "status",
    "check",
    "ping",
    "health",
    "is running",
    "report",
    "how many",
    "list",
    "show me",
    "what is",
]

ACTION_KEYWORDS = [
    "implement",
    "add",
    "build",
    "fix",
    "refactor",
    "write",
    "create",
    "update",
    "migrate",
    "deploy",
]

# ---------------------------------------------------------------------------
# Ollama prompt templates (verbatim from architect spec)
# ---------------------------------------------------------------------------

OLLAMA_SYSTEM_PROMPT = (
    "You are a task classification assistant for a developer's personal orchestration system.\n"
    "Your job is to read a development task and output ONLY a JSON object — no prose, no markdown fences, no explanation.\n"
    "\n"
    "Privacy rule: Do NOT include any of these words in your output JSON if they appear in the task: "
    "arbiter, investor, fundraising, strategy, supplier, credentials, api key, secret, private, confidential. "
    'Replace them with "project" or omit entirely.'
)

OLLAMA_USER_PROMPT = """Classify this task and output ONLY valid JSON matching this exact schema:

{{
  "tier": <1|2|3>,
  "project": "<project name, lowercase, one word>",
  "branch_name": "<feature/kebab-case-slug, max 50 chars>",
  "is_direct": <true|false>,
  "pipeline": [
    {{"role": "<role>", "parallel_group": <null|integer>}}
  ]
}}

Tier rules:
- Tier 1 (hotfix): single file change, bug fix, typo, config tweak → pipeline: [coder, tester, deployer]
- Tier 2 (feature): new feature, multi-file change, needs planning → pipeline: [plan, coder, tester, cleanup, review, deployer]
- Tier 3 (architecture): new system, major refactor, needs research + architecture → pipeline: [plan, {{research+architect in parallel}}, coder, {{tester+cleanup in parallel}}, review, deployer]

Parallel group rules:
- Steps with the same integer parallel_group run simultaneously
- null means sequential (wait for previous step to finish)
- Only use parallel groups for: research+architect (group 1) and tester+cleanup (group 2)

is_direct: true only if this is a pure status check, question, or lookup (no code changes needed)

Available roles: plan, research, architect, coder, tester, cleanup, review, deployer

Task to classify:
{task_text}"""


# ---------------------------------------------------------------------------
# Pre-defined pipelines for each tier
# ---------------------------------------------------------------------------


def _tier1_pipeline() -> list:
    return [
        PipelineStep("coder", None),
        PipelineStep("tester", None),
        PipelineStep("deployer", None),
    ]


def _tier2_pipeline() -> list:
    return [
        PipelineStep("plan", None),
        PipelineStep("coder", None),
        PipelineStep("tester", None),
        PipelineStep("cleanup", None),
        PipelineStep("review", None),
        PipelineStep("deployer", None),
    ]


def _tier3_pipeline() -> list:
    return [
        PipelineStep("plan", None),
        PipelineStep("research", 1),
        PipelineStep("architect", 1),
        PipelineStep("coder", None),
        PipelineStep("tester", 2),
        PipelineStep("cleanup", 2),
        PipelineStep("review", None),
        PipelineStep("deployer", None),
    ]


def _tier_to_pipeline(tier: int) -> list:
    if tier == 1:
        return _tier1_pipeline()
    if tier == 3:
        return _tier3_pipeline()
    return _tier2_pipeline()  # default Tier 2


# ---------------------------------------------------------------------------
# Privacy sanitizer
# ---------------------------------------------------------------------------


def _sanitize_for_ollama(text: str) -> str:
    """Replace privacy-sensitive keywords with [REDACTED] before sending to Ollama."""
    sanitized = text
    for kw in PRIVACY_KEYWORDS:
        pattern = re.compile(re.escape(kw), re.IGNORECASE)
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized


def _has_privacy_keywords(text: str) -> bool:
    """Return True if any privacy keyword is found in task_text."""
    lower = text.lower()
    return any(kw in lower for kw in PRIVACY_KEYWORDS)


# ---------------------------------------------------------------------------
# Direct-task detection
# ---------------------------------------------------------------------------


def _is_direct_task(task_text: str) -> bool:
    """
    Return True if this looks like a status query or short lookup that
    doesn't require a full pipeline run.

    Action keywords always force a pipeline — "fix the login bug" is short
    but is real work, not a lookup. Tasks with no action keywords (status
    queries, questions, lookups) are direct.
    """
    lower = task_text.strip().lower()
    return not any(kw in lower for kw in ACTION_KEYWORDS)


# ---------------------------------------------------------------------------
# Ollama subprocess call
# ---------------------------------------------------------------------------


def _call_ollama(prompt: str) -> str:
    """Call ollama run llama3.1:8b with the given prompt. Returns stdout. Raises on failure."""
    result = subprocess.run(
        ["ollama", "run", "llama3.1:8b", prompt],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Ollama failed: {result.stderr.strip()}")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# JSON extraction from Ollama output
# ---------------------------------------------------------------------------


def _extract_json(raw: str) -> dict:
    """
    Strip markdown fences if present and extract the first JSON object.
    Raises ValueError if no valid JSON object is found.
    """
    # Strip markdown fences
    text = re.sub(r"```(?:json)?\s*", "", raw).strip()
    text = re.sub(r"```\s*$", "", text).strip()

    # Find first complete { ... } block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in Ollama output")
    return json.loads(match.group(0))


# ---------------------------------------------------------------------------
# Fallback result when Ollama fails or returns invalid JSON
# ---------------------------------------------------------------------------


def _fallback_result(task_text: str, forced_project: Optional[str] = None) -> PlannerResult:
    """Called when Ollama fails or returns unparseable JSON. Defaults to Tier 2."""
    project = forced_project or _detect_project(task_text) or "general"
    slug = re.sub(r"[^a-z0-9]+", "-", task_text.lower()[:50]).strip("-")

    log.warning(f"[planner] Falling back to Tier 2 default for task: {task_text[:60]!r}")

    return PlannerResult(
        tier=2,
        project=project,
        branch_name=f"feature/{slug}",
        pipeline=_tier2_pipeline(),
        is_direct=False,
        raw_ollama_response="FALLBACK",
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def classify_task(task_text: str) -> PlannerResult:
    """
    Classify a task string into a PlannerResult containing tier, project,
    branch name, and pipeline steps.

    1. Check for direct tasks (status queries, short commands) — skip Ollama.
    2. Check for privacy keywords — if found, skip Ollama and use fallback.
    3. Call Ollama to classify; parse and validate JSON response.
    4. On any failure: fall back to Tier 2 with heuristic project detection.
    """

    # Step 0: Scaffold request — intercept before everything else
    scaffold_name = _scaffold_project_name(task_text)
    if scaffold_name:
        log.info(f"[planner] Scaffold request detected: {scaffold_name!r}")
        return PlannerResult(
            tier=0,
            project=scaffold_name,
            branch_name="",
            pipeline=[],
            is_direct=True,
            raw_ollama_response="",
            scaffold_project=scaffold_name,
        )

    # Step 0.5: Explicit "on <project>, <task>" — deterministic routing, never a guess.
    forced_project, task_text = _parse_explicit_target(task_text)
    if forced_project:
        log.info(f"[planner] Explicit target pinned: project={forced_project!r}")

    # Step 1: Direct task shortcut — runs before Ollama
    if _is_direct_task(task_text):
        log.info(f"[planner] Direct task detected (no pipeline): {task_text[:60]!r}")
        return PlannerResult(
            tier=0,
            project=forced_project or _detect_project(task_text) or "general",
            branch_name="",
            pipeline=[],
            is_direct=True,
            raw_ollama_response="",
        )

    # Step 2: Privacy keyword gate — do not send sensitive content to Ollama
    if _has_privacy_keywords(task_text):
        log.warning(
            f"[planner] Privacy keywords found in task — bypassing Ollama, using fallback: "
            f"{task_text[:60]!r}"
        )
        return _fallback_result(task_text, forced_project)

    # Step 3: Build prompt and call Ollama
    sanitized_task = _sanitize_for_ollama(task_text)
    projects_hint = 'Known project names (pick one for "project", else "general"): ' + ", ".join(
        project_names()
    )
    user_prompt = OLLAMA_USER_PROMPT.format(task_text=sanitized_task)
    full_prompt = f"{OLLAMA_SYSTEM_PROMPT}\n\n{projects_hint}\n\n{user_prompt}"

    try:
        log.info(f"[planner] Calling Ollama to classify: {task_text[:60]!r}")
        raw = _call_ollama(full_prompt)
        log.debug(f"[planner] Ollama raw response: {raw[:200]}")

        data = _extract_json(raw)

        # Validate required fields
        if "tier" not in data or "pipeline" not in data or "project" not in data:
            raise ValueError(f"Missing required fields in Ollama response: {list(data.keys())}")

        tier = int(data["tier"])
        # Forced target wins; otherwise canonicalize Ollama's guess via the registry
        # (an unknown name is kept as-is and caught by the fail-loud guard downstream).
        ollama_project = str(data["project"]).lower().strip()
        resolved = resolve(ollama_project)
        project = forced_project or (resolved.name if resolved else ollama_project)
        branch_name = str(data.get("branch_name", f"feature/task-{int(time.time())}"))
        is_direct = bool(data.get("is_direct", False))

        # Build pipeline from Ollama response; validate roles
        valid_roles = {
            "plan",
            "research",
            "architect",
            "coder",
            "tester",
            "cleanup",
            "review",
            "deployer",
        }
        raw_pipeline = data.get("pipeline", [])

        if raw_pipeline and all("role" in s for s in raw_pipeline):
            pipeline = []
            for s in raw_pipeline:
                role = s["role"]
                if role not in valid_roles:
                    log.warning(f"[planner] Unknown role {role!r} from Ollama — using tier default")
                    pipeline = _tier_to_pipeline(tier)
                    break
                pg = s.get("parallel_group")
                pipeline.append(PipelineStep(role, int(pg) if pg is not None else None))
        else:
            log.warning("[planner] Empty or malformed pipeline from Ollama — using tier default")
            pipeline = _tier_to_pipeline(tier)

        log.info(
            f"[planner] Classified: tier={tier}, project={project!r}, "
            f"branch={branch_name!r}, steps={len(pipeline)}, is_direct={is_direct}"
        )

        return PlannerResult(
            tier=tier,
            project=project,
            branch_name=branch_name,
            pipeline=pipeline,
            is_direct=is_direct,
            raw_ollama_response=raw,
        )

    except subprocess.TimeoutExpired:
        log.warning("[planner] Ollama timed out after 30s — falling back to Tier 2")
        return _fallback_result(task_text, forced_project)
    except FileNotFoundError:
        log.warning("[planner] Ollama not found — falling back to Tier 2")
        return _fallback_result(task_text, forced_project)
    except Exception as e:
        log.warning(f"[planner] Ollama classification failed ({e!r}) — falling back to Tier 2")
        return _fallback_result(task_text, forced_project)


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler()],
    )

    test_tasks = [
        "status",
        "check if hermes is running",
        "Fix typo in README",
        "Add dark mode toggle to Arbiter dashboard",
        "Build a new authentication system with JWT, refresh tokens, and OAuth2 for the Arbiter platform",
    ]

    tasks_to_test = sys.argv[1:] if len(sys.argv) > 1 else test_tasks

    for task in tasks_to_test:
        print(f"\n{'=' * 60}")
        print(f"Task: {task!r}")
        result = classify_task(task)
        print(f"  tier       : {result.tier}")
        print(f"  project    : {result.project}")
        print(f"  branch     : {result.branch_name}")
        print(f"  is_direct  : {result.is_direct}")
        print(f"  pipeline   : {[(s.role, s.parallel_group) for s in result.pipeline]}")
        print(f"  ollama_raw : {result.raw_ollama_response[:60]!r}...")
