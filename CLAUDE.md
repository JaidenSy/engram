# Hermes — Repo Instructions

Mac Mini orchestration daemon (replaced OpenClaw). Triggered by **Telegram** `[HERMES] task` (chat_id 8922766986); runs via `claude --print`. Tier-aware model routing (fable/opus/sonnet/haiku per role+tier). Direct tasks run **non-blocking** — `run_task` spawns the agent in a background thread and texts the result via an `on_complete` callback on exit (2h safety kill). Reload the LaunchAgent `dev.arbiterai.hermes` after changing `hermes.py`/`config.yaml`.

**Project routing** is via `project_registry.py` — the single source of truth (scans `~/Projects` + `~/Documents/RaphBrain/Projects`, knows all ~23 projects; add a project = make the folder). Say **`on <project>, <task>`** for deterministic routing; **`projects`/`help`/`status`/`abort`** are commands. Unknown project → fails loud, never runs in `$HOME`.

## Start here
- Full context + current state: `~/Documents/RaphBrain/Projects/Hermes/CONTEXT.md`, then `Progress.md`.

## Rules
- **Branch names must be meaningful, not raw task slugs** — extract issue numbers first (e.g. `fix/issues-192-195`), strip control words, use meaningful nouns.
- Feature branches + PR + Jaiden's review before merge.
- **Never stack PRs.** Cut every branch from an up-to-date `main` and target `main`. Do not base a branch (or a PR) on another open PR's branch — that's what tangled #5/#6/#7. If work truly depends on an unmerged branch, wait for it to land first, then branch from `main`.
- When merging, **don't `--delete-branch` a branch another open PR is based on**, and merge independent PRs one at a time (re-check mergeable between merges). GitHub's default branch is `main` — keep it that way so new PRs base off `main` automatically.
