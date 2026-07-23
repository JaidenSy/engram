# Engram — Repo Instructions

> **Renamed from "Hermes" → "Engram" (2026-07-22)** to disambiguate from Nous Research's `hermes-agent` (adopted separately as the conversational home assistant). Repo `JaidenSy/engram`, dir `~/engram`, LaunchAgent `dev.arbiterai.engram`, trigger `[ENGRAM]` (accepts `[HERMES]` too). Internal paths derive from `Path(__file__).parent` so the folder is move-safe. Kept on purpose: keychain service `hermes-telegram-bot` (live token), markers `## Hermes Run Log` / `hermes-meta`, `/api/hermes/*` routes.

**Status (2026-07-22):** `main` after the Engram rename, 0 open PRs, CI green (165 pass / 1 skip). **Daemon LIVE as Engram — PID 89909, clean boot, poller ready.** Merged today: **#22** Hermes→Engram rename; **#20** deterministic `## Hermes Run Log` in each project's Progress.md + Nous-style post-task learning (local-Ollama Learnings note + staged skill candidate in `skill-candidates/`, human-promoted, never auto-installed); **#21** phone handoff lifecycle (`handoffs`/`resume <n>`/`handoffs clear`); **#18** quiet idle poll noise + suppress redundant "❌ Check logs" + persist failure reason. Full state: `Projects/Engram/Progress.md` in RaphBrain.

**Reloading the daemon is PRE-AUTHORIZED for Claude (Jaiden, 2026-07-22)** — after merging an Engram PR, reload without asking: `launchctl kickstart -k gui/$(id -u)/dev.arbiterai.engram`, then verify the boot (`grep "poller ready" logs/hermes.log`). This is the one deploy action that doesn't need a fresh OK. (Still ask before anything else outward-facing.)

**Next up (approved, not yet built):** cron self-scheduler (port from Nous `cron/scheduler.py` — closes the "can't self-inject a task" gap), skill-candidate hygiene + `promote <n>`, per-run cost line, `recall <project>` run-search; then install Nous `hermes-agent` as the coexisting home assistant. See `Projects/Engram/Nous-Comparison-2026-07-22.md`.

Mac Mini orchestration daemon (replaced OpenClaw). Triggered by **Telegram** `[HERMES] task` (chat_id 8922766986); runs via `claude --print`. Tier-aware model routing (fable/opus/sonnet/haiku per role+tier). Direct tasks run **non-blocking** — `run_task` spawns the agent in a background thread and texts the result via an `on_complete` callback on exit (2h safety kill). Reload the LaunchAgent `dev.arbiterai.engram` after changing `engram.py`/`config.yaml`.

**Project routing** is via `project_registry.py` — the single source of truth (scans `~/Projects` + `~/Documents/RaphBrain/Projects`, knows all ~23 projects; add a project = make the folder). Say **`on <project>, <task>`** for deterministic routing; **`projects`/`help`/`status`/`abort`** are commands. Unknown project → fails loud, never runs in `$HOME`.

## Start here
- Full context + current state: `~/Documents/RaphBrain/Projects/Engram/CONTEXT.md`, then `Progress.md`.

## Rules
- **Branch names must be meaningful, not raw task slugs** — extract issue numbers first (e.g. `fix/issues-192-195`), strip control words, use meaningful nouns.
- Feature branches + PR + Jaiden's review before merge.
- **Never stack PRs.** Cut every branch from an up-to-date `main` and target `main`. Do not base a branch (or a PR) on another open PR's branch — that's what tangled #5/#6/#7. If work truly depends on an unmerged branch, wait for it to land first, then branch from `main`.
- When merging, **don't `--delete-branch` a branch another open PR is based on**, and merge independent PRs one at a time (re-check mergeable between merges). GitHub's default branch is `main` — keep it that way so new PRs base off `main` automatically.
