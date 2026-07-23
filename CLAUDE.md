# Engram — Repo Instructions

> **Engram** — repo `JaidenSy/engram`, dir `~/engram`, LaunchAgent `dev.arbiterai.engram`. Trigger: text Telegram directly (an optional `[ENGRAM]` prefix is stripped if present). Internal paths derive from `Path(__file__).parent`, so the folder is move-safe. Bot token lives in keychain service `engram-telegram-bot`.

**Status (2026-07-22):** `main`@`0e2c296`, 0 open PRs, CI green (176 pass). **Daemon LIVE as Engram — PID 13710, scheduler + poller ready.** Recent: full de-Hermes (0 `hermes` strings, `[HERMES]` dropped, keychain `engram-telegram-bot`, `engram.log`); **cron self-scheduler** (`schedule <when> | <task>` / `schedules` / `unschedule <n>` — self-injects via orchestrate_task); deterministic `## Engram Run Log` note + staged skill-candidate learning (human-promoted, never auto-installed); phone handoff `handoffs`/`resume <n>`/`clear`; quiet idle poll noise + suppress redundant "❌ Check logs". Coexists with Nous `hermes-agent` (installed `~/.hermes`, config pending). Full state: `Projects/Engram/Progress.md` in RaphBrain.

**Reloading the daemon is PRE-AUTHORIZED for Claude (Jaiden, 2026-07-22)** — after merging an Engram PR, reload without asking: `launchctl kickstart -k gui/$(id -u)/dev.arbiterai.engram`, then verify the boot (`grep "poller ready" logs/engram.log`). This is the one deploy action that doesn't need a fresh OK. (Still ask before anything else outward-facing.)

**Next up (approved):** cron self-scheduler (closes the "can't self-inject a task" gap), skill-candidate hygiene + `promote <n>`, per-run cost line, `recall <project>` run-search; then install the Nous home-assistant agent to coexist. See `Projects/Engram/Nous-Comparison-2026-07-22.md`.

Mac Mini orchestration daemon (replaced OpenClaw). Triggered by **Telegram** `[ENGRAM] task` (chat_id 8922766986); runs via `claude --print`. Tier-aware model routing (fable/opus/sonnet/haiku per role+tier). Direct tasks run **non-blocking** — `run_task` spawns the agent in a background thread and texts the result via an `on_complete` callback on exit (2h safety kill). Reload the LaunchAgent `dev.arbiterai.engram` after changing `engram.py`/`config.yaml`.

**Project routing** is via `project_registry.py` — the single source of truth (scans `~/Projects` + `~/Documents/RaphBrain/Projects`, knows all ~23 projects; add a project = make the folder). Say **`on <project>, <task>`** for deterministic routing; **`projects`/`help`/`status`/`abort`** are commands. Unknown project → fails loud, never runs in `$HOME`.

## Start here
- Full context + current state: `~/Documents/RaphBrain/Projects/Engram/CONTEXT.md`, then `Progress.md`.

## Rules
- **Branch names must be meaningful, not raw task slugs** — extract issue numbers first (e.g. `fix/issues-192-195`), strip control words, use meaningful nouns.
- Feature branches + PR + Jaiden's review before merge.
- **Never stack PRs.** Cut every branch from an up-to-date `main` and target `main`. Do not base a branch (or a PR) on another open PR's branch — that's what tangled #5/#6/#7. If work truly depends on an unmerged branch, wait for it to land first, then branch from `main`.
- When merging, **don't `--delete-branch` a branch another open PR is based on**, and merge independent PRs one at a time (re-check mergeable between merges). GitHub's default branch is `main` — keep it that way so new PRs base off `main` automatically.
