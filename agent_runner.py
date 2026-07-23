#!/usr/bin/env python3
"""
agent_runner.py — Engram Mission Control
Dispatches a pipeline step as a Raphael task JSON + prompt, then spawns
run-agent.sh in a detached tmux session.  The RunEngine polls the task JSON
for completion; this module is only responsible for *launching* the work.

Privacy note:
  Sensitive-keyword sanitisation (arbiter / investor / supplier / credentials)
  is applied ONLY before Ollama calls (handled in planner.py).
  All agent dispatch here uses Claude Code via run-agent.sh → `claude -p`,
  which runs entirely on the user's own machine, so no sanitisation is needed.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from project_registry import pr_base, resolve as resolve_project

# ── Paths ──────────────────────────────────────────────────────────────────────
TASKS_DIR = Path.home() / "raphael" / "tasks"
RAPHBRAIN = Path.home() / "Documents" / "RaphBrain"
RUN_AGENT = Path.home() / "raphael" / "agents" / "run-agent.sh"
ROLES_DIR = Path.home() / "raphael" / "templates" / "roles"
_BASE = Path(__file__).resolve().parent  # repo dir — rename/move-safe (follows the folder)
CONFIG_PATH = _BASE / "config" / "config.yaml"

# Used when config.yaml is missing or has no agent_models section
DEFAULT_MODEL_ROUTE = {"model": "sonnet", "max_turns": 60}

log = logging.getLogger("engram")

# ── Model availability / auto-downgrade ─────────────────────────────────────────
# Model tier aliases, strongest → weakest. When an alias stops resolving (e.g.
# Fable leaves the plan), routing auto-downgrades to the next tier that probed OK
# at daemon startup, so a run never dies on a model the CLI can no longer reach.
_FALLBACK_CHAIN = ["fable", "opus", "sonnet", "haiku"]

# alias -> substitute, populated by probe_models() at startup. Empty = no downgrades.
_ALIAS_DOWNGRADE: dict[str, str] = {}


def _claude_bin() -> str:
    """Absolute path to the claude CLI — a launchd daemon's PATH can be minimal."""
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")


def _model_available(alias: str) -> bool:
    """Cheap liveness probe: does `claude --model <alias>` actually run?

    False only on an explicit non-zero exit (unknown model / not on plan / auth).
    A timeout or a broken probe is treated as available — never downgrade a model
    on a slow or failed probe, only on a definitive 'the CLI rejected it'.
    """
    try:
        r = subprocess.run(
            [_claude_bin(), "--model", alias, "--print", "ok", "--max-turns", "1"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return r.returncode == 0
    except Exception:
        return True  # fail open — a probe error is not evidence the model is gone


def configured_aliases() -> set:
    """Every model alias referenced in agent_models routing (direct + roles + overrides)."""
    aliases: set = set()
    try:
        with open(CONFIG_PATH) as f:
            am = (yaml.safe_load(f) or {}).get("models", {}).get("agent_models", {})
    except Exception:
        return aliases
    if am.get("direct", {}).get("model"):
        aliases.add(am["direct"]["model"])
    for r in am.get("roles", {}).values():
        if r.get("model"):
            aliases.add(r["model"])
    for tier in am.get("tier_overrides", {}).values():
        for r in tier.values():
            if r.get("model"):
                aliases.add(r["model"])
    return aliases


def probe_models() -> dict:
    """At daemon startup: probe each tier; map any unavailable-but-used alias to the
    strongest still-available tier below it. If EVERY probe fails (claude down or
    logged out) change nothing — that's transient, not a set of dead models.

    ponytail: ~4 cheap `claude` calls per daemon start; upgrade to a cached probe
    if restarts ever get frequent enough to notice.
    """
    _ALIAS_DOWNGRADE.clear()
    avail = {tier: _model_available(tier) for tier in _FALLBACK_CHAIN}
    if not any(avail.values()):
        log.warning(
            "[agent_runner] All model probes failed — claude CLI down or logged out? "
            "Leaving model routing unchanged (assumed transient)."
        )
        return {}

    configured = configured_aliases()
    for i, alias in enumerate(_FALLBACK_CHAIN):
        if alias not in configured or avail.get(alias):
            continue
        sub = next((t for t in _FALLBACK_CHAIN[i + 1 :] if avail.get(t)), None)
        if sub:
            _ALIAS_DOWNGRADE[alias] = sub
            log.warning(f"[agent_runner] Model {alias!r} unavailable — routing it to {sub!r}.")
        else:
            log.error(
                f"[agent_runner] Model {alias!r} unavailable and no lower tier is up — "
                f"steps routed to {alias!r} will fail."
            )
    return dict(_ALIAS_DOWNGRADE)


def apply_downgrade(model: str) -> str:
    """Substitute an unavailable alias per the startup probe. No-op if all healthy."""
    return _ALIAS_DOWNGRADE.get(model, model)


def resolve_model_route(role: str, tier: int) -> dict:
    """
    Resolve {model, max_turns} for a pipeline step from config.yaml
    (models.agent_models). Tier overrides win over role defaults, then any
    startup-probed downgrade is applied to the chosen model.
    """
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        agent_models = cfg.get("models", {}).get("agent_models", {})
    except Exception as exc:
        log.warning(f"[agent_runner] Could not load model routing config: {exc}")
        return dict(DEFAULT_MODEL_ROUTE)

    route = dict(DEFAULT_MODEL_ROUTE)
    role_cfg = agent_models.get("roles", {}).get(role)
    if role_cfg:
        route.update(role_cfg)
    tier_cfg = agent_models.get("tier_overrides", {}).get(tier, {}).get(role)
    if tier_cfg:
        route.update(tier_cfg)
    route["model"] = apply_downgrade(str(route["model"]))
    return route


# Ensure required directories exist on import
TASKS_DIR.mkdir(parents=True, exist_ok=True)

# Map from Engram pipeline role name → template filename (without .md)
# run-agent.sh also injects these templates by TASK_ROLE, so the names must match.
ROLE_TEMPLATE_FILENAMES: dict[str, str] = {
    "plan": "planner",
    "architect": "architect",
    "coder": "coder",
    "review": "reviewer",
}

# Inline fallback templates for roles with no file in ~/raphael/templates/roles/
INLINE_ROLE_TEMPLATES: dict[str, str] = {
    "research": (
        "You are the Research Agent for this project. Your job is to gather information ONLY — do not write code.\n"
        "Read existing code, documentation, RaphBrain notes, and external references.\n"
        "Output a structured research summary the Architect and Coder can use directly.\n"
        "Write your output to the specified output note.\n"
        "Include: findings, constraints, gotchas discovered, recommended approach."
    ),
    "tester": (
        "You are the Tester Agent. Your job is to write and run tests for the code the Coder just implemented.\n"
        "Read agents/coder-output.md first. Run existing test suites. Write new tests for untested paths.\n"
        "Do NOT modify implementation code — only test files.\n"
        'Report: which tests pass, which fail, any bugs found (prefix with "🐛 BUG FOUND:").\n'
        "Write your output to the specified output note."
    ),
    "cleanup": (
        "You are the Cleanup Agent. Your job is to polish the code after the Coder finishes.\n"
        "Read agents/coder-output.md first. Remove dead imports, fix lint, ensure consistent logging patterns.\n"
        "Do NOT change functionality — only style, naming, dead code removal.\n"
        "Do NOT modify test files.\n"
        "Write your output to the specified output note."
    ),
    "deployer": (
        # branch placeholder filled in at prompt-build time
        "You are the Deployer Agent. Your job is to open the pull request.\n"
        "1. Read agents/review-output.md — verify verdict is APPROVED before proceeding.\n"
        '2. If BLOCKED: write "🚫 BLOCKED: {{reason}}" to your output note and stop.\n'
        "3. If APPROVED: run:\n"
        "     gh pr create --base {target_branch} --head {branch} "
        '--title "{title}" --body "{body}"\n'
        "4. Capture the PR URL from gh output.\n"
        "5. Write your output to the specified output note in this exact format:\n\n"
        "## Deployer Output\n"
        "**PR URL:** {{url}}\n"
        "**Status:** opened"
    ),
}

# Serializes task-id generation so concurrent parallel-group dispatches don't collide.
_ID_LOCK = threading.Lock()


class AgentRunner:
    """
    Builds task JSON + prompt and spawns run-agent.sh for a single pipeline step.
    Non-blocking: returns task_id immediately; RunEngine polls for completion.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def dispatch_step(
        self,
        step: dict,
        run: dict,
        task_description: str,
    ) -> str:
        """
        1. Generates a unique task_id (YYYY-MM-DD-NNN, incrementing per day).
        2. Determines the current step index + step count for context.
        3. Builds the full prompt markdown and writes it to
           ~/raphael/tasks/{task_id}-prompt.md
        4. Writes the task JSON to ~/raphael/tasks/{task_id}.json
        5. Spawns run-agent.sh in a detached tmux session (non-blocking).
        6. Returns task_id.
        """
        TASKS_DIR.mkdir(parents=True, exist_ok=True)

        role = step["role"]
        project = run["project"]
        pipeline = run.get("pipeline", [])

        # Step position for human-readable context ("Step 2 of 4: tester")
        step_index = self._find_step_index(step, pipeline)
        step_label = f"Step {step_index + 1} of {len(pipeline)}: {role}"

        task_id = self._next_task_id()
        output_note = f"Projects/{project}/agents/engram-{task_id}-{role}-output.md"
        prompt_file = TASKS_DIR / f"{task_id}-prompt.md"

        # PR target branch = the repo's actual default branch (origin HEAD), via the
        # registry. Fixes the old hardcoded "develop" that was wrong for most repos
        # (alphabot/kirnu/vitre use main; arbiter really is develop).
        target_branch = pr_base(project) or "main"

        # Build and write prompt
        prompt_md = self._build_prompt(
            step=step,
            run=run,
            step_label=step_label,
            step_index=step_index,
            task_description=task_description,
            output_note=output_note,
            target_branch=target_branch,
        )
        prompt_file.write_text(prompt_md)
        log.info(f"[agent_runner] Prompt written: {prompt_file}")

        # Build and write task JSON
        task = self._build_task_json(
            task_id=task_id,
            step=step,
            run=run,
            output_note=output_note,
            prompt_file=str(prompt_file).replace(str(Path.home()), "~"),
            task_description=task_description,
        )
        task_file = TASKS_DIR / f"{task_id}.json"
        task_file.write_text(json.dumps(task, indent=2))
        log.info(f"[agent_runner] Task JSON written: {task_file}")

        # Spawn run-agent.sh (non-blocking tmux session)
        self._spawn_run_agent(task_id, task_file)
        log.info(f"[agent_runner] Spawned tmux session raphael-{task_id} for role={role}")

        return task_id

    # ------------------------------------------------------------------
    # Task JSON construction
    # ------------------------------------------------------------------

    def _build_task_json(
        self,
        task_id: str,
        step: dict,
        run: dict,
        output_note: str,
        prompt_file: str,
        task_description: str,
    ) -> dict:
        role = step["role"]
        project = run["project"]
        tier = run.get("tier", 2)
        route = resolve_model_route(role, tier)

        # Resolve repo path + RaphBrain folder via the registry (single source of
        # truth). Unknown project → best-guess path that fail-loud catches downstream.
        proj = resolve_project(project)
        repo_path = proj.repo if (proj and proj.repo) else f"~/Projects/{project}"
        raphbrain_dir = (proj.raphbrain_dir if proj else None) or project.capitalize()

        return {
            # ── Standard Raphael task fields ──────────────────────────
            "id": task_id,
            "title": f"[Engram/T{tier}] {role.upper()}: {run['task_raw'][:80]}",
            "description": task_description,
            "status": "pending",
            "priority": "high",
            "team": "engram",
            "role": role,
            "project": project,
            "tier": tier,
            "model": route["model"],
            "max_turns": route["max_turns"],
            "repo": repo_path,
            "depends_on": [],
            "handoff_required": role not in ("deployer",),
            "prompt_file": prompt_file,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "claimed_at": None,
            "completed_at": None,
            "output_note": output_note,
            "raphbrain_project": raphbrain_dir,
            # ── Engram metadata (ignored by run-agent.sh, used by RunEngine) ──
            "engram_run_id": run["id"],
            "engram_step_role": role,
        }

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        step: dict,
        run: dict,
        step_label: str,
        step_index: int,
        task_description: str,
        output_note: str,
        target_branch: str,
    ) -> str:
        role = step["role"]
        project = run["project"]
        branch = run.get("branch", "feature/unknown")

        # 1. Role system prompt (file-backed or inline fallback)
        role_prompt = self._load_role_template(role, branch=branch, target_branch=target_branch)

        # 2. Previous step outputs
        prev_outputs = self._collect_previous_outputs(run, step_index)

        # 3. RaphBrain context paths (the agent reads these at session start)
        raphbrain_paths = self._raphbrain_context_section(project)

        # 4. Branch instruction (injected for all roles; critical for coder)
        branch_section = f"## Branch\nWork on: `{branch}`\nNever commit to `main` or `develop`.\n"

        parts = [
            role_prompt,
            "---",
            f"## Step\n{step_label}",
            "---",
            "## Context: Previous Steps",
            prev_outputs,
            "---",
            raphbrain_paths,
            "---",
            branch_section,
            "---",
            f"## Task\n{task_description}",
            "---",
            f"## Output\nWrite your full output to:\n`~/Documents/RaphBrain/{output_note}`",
        ]
        return "\n\n".join(parts)

    def _load_role_template(
        self,
        role: str,
        branch: str,
        target_branch: str,
    ) -> str:
        """
        Load from ~/raphael/templates/roles/{filename}.md if it exists,
        otherwise use inline fallback.  For 'deployer', fill in branch/target_branch.
        """
        filename = ROLE_TEMPLATE_FILENAMES.get(role)
        if filename:
            path = ROLES_DIR / f"{filename}.md"
            if path.exists():
                return path.read_text().strip()

        # Inline fallback
        template = INLINE_ROLE_TEMPLATES.get(role, f"You are the {role.capitalize()} Agent.")
        if role == "deployer":
            template = template.format(
                branch=branch,
                target_branch=target_branch,
                title="feat: {branch}",
                body="Automated PR via Engram Mission Control.",
            )
        return template.strip()

    def _raphbrain_context_section(self, project: str) -> str:
        """Build a markdown section listing RaphBrain context paths to read."""
        proj_cap = project.capitalize()
        lines = [
            "## RaphBrain Context Files",
            "Read these before starting (use Read tool, skip if missing):",
            f"- `~/Documents/RaphBrain/Projects/{proj_cap}/Progress.md`",
            f"- `~/Documents/RaphBrain/Projects/{proj_cap}/Decisions.md`",
            f"- `~/Documents/RaphBrain/Projects/{proj_cap}/Gotchas.md`",
            f"- `~/Documents/RaphBrain/Projects/{proj_cap}/agents/`  ← scan for recent handoff files",
        ]
        return "\n".join(lines)

    def _collect_previous_outputs(self, run: dict, current_step_index: int) -> str:
        """
        Read RaphBrain output files for all DONE steps before current_step_index.
        Truncates each to 2000 chars to avoid context bloat.
        """
        parts: list[str] = []
        for i, s in enumerate(run.get("pipeline", [])):
            if i >= current_step_index:
                break
            if s.get("status") == "done" and s.get("output_path"):
                path = RAPHBRAIN / s["output_path"]
                if path.exists():
                    content = path.read_text()[:2000]
                    parts.append(f"### {s['role'].upper()} output\n{content}\n")
        return "\n".join(parts) if parts else "(no prior steps)"

    # ------------------------------------------------------------------
    # Task ID generation
    # ------------------------------------------------------------------

    def _next_task_id(self) -> str:
        """
        Returns YYYY-MM-DD-NNN, where NNN increments from the count of
        existing task JSON files for today.  UUID4 is NOT used here because
        run-agent.sh expects the YYYY-MM-DD-NNN pattern for log file naming
        (LOG_FILE=$LOG_DIR/$(date +%Y-%m-%d)-${TASK_ID}.log).
        """
        today = date.today().isoformat()
        # Lock + immediately reserve the id (placeholder file) so two parallel-group
        # steps (Tier 3 research+architect) can't compute the same NNN and clobber
        # each other's task JSON. dispatch_step overwrites the placeholder right after.
        with _ID_LOCK:
            seq = len(list(TASKS_DIR.glob(f"{today}-*.json")))
            task_id = f"{today}-{seq:03d}"
            (TASKS_DIR / f"{task_id}.json").write_text("{}")
        return task_id

    # ------------------------------------------------------------------
    # tmux spawning
    # ------------------------------------------------------------------

    def _spawn_run_agent(self, task_id: str, task_file: Path) -> None:
        """
        Spawns run-agent.sh in a new detached tmux session named
        'raphael-{task_id}'.  This matches the abort pattern used by
        RunEngine.abort_run():
            tmux kill-session -t raphael-{task_id}

        run-agent.sh does NOT create its own tmux session — it invokes
        `claude -p` directly.  This outer tmux session IS the container;
        killing it kills the claude process.
        """
        session_name = f"raphael-{task_id}"
        safe_path = str(task_file)
        cmd = [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session_name,
            "-x",
            "220",
            "-y",
            "50",
            f"zsh {RUN_AGENT} {safe_path} ; echo RAPHAEL_DONE",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to spawn tmux session {session_name}: {result.stderr.strip()}"
            )

    # ------------------------------------------------------------------
    # Deployer: extract PR URL from output note
    # ------------------------------------------------------------------

    def extract_pr_url(self, output_note: str) -> Optional[str]:
        """
        Reads the RaphBrain output note for a deployer step and extracts
        the PR URL using regex.  Called by RunEngine after the deployer
        step completes.

        Pattern matches: https://github.com/<owner>/<repo>/pull/<number>
        Stops at whitespace, closing paren/bracket/quote, or angle bracket.
        """
        path = RAPHBRAIN / output_note
        if not path.exists():
            log.warning(f"[agent_runner] Deployer output note not found: {path}")
            return None
        text = path.read_text()
        match = re.search(r"https://github\.com/[^\s\)\"\'>]+", text)
        if match:
            url = match.group(0).rstrip(".,;")
            log.info(f"[agent_runner] Extracted PR URL: {url}")
            return url
        log.warning("[agent_runner] No PR URL found in deployer output note")
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_step_index(self, step: dict, pipeline: list[dict]) -> int:
        """
        Return the 0-based index of `step` in `pipeline`.
        Matches on role + status combination; falls back to 0 if not found.
        The RunEngine calls dispatch_step with the exact step dict from the
        run JSON, so object identity is reliable if the caller passes it correctly.
        We match by identity first, then by role+status as fallback.
        """
        for i, s in enumerate(pipeline):
            if s is step:
                return i
        # Fallback: match first pending step with same role
        for i, s in enumerate(pipeline):
            if s.get("role") == step.get("role") and s.get("status") == "pending":
                return i
        return 0
