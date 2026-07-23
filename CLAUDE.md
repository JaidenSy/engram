# Hermes — Repo Instructions

**Status (2026-07-22):** `main` @ `23dad51`, 0 open PRs, CI green (144 pass / 1 skip). **Daemon reloaded onto this code — PID 39726, clean boot, poller ready.** PR #18 (2026-07-22): idle Telegram long-poll timeouts now log at DEBUG (were ERROR — a healthy daemon looked on fire); on a failed step the redundant "❌ …Check logs" completion text is suppressed (the step handler's "⚠️ Blocked on {role}: {reason}" + handoff is the terminal word); failure `reason` is persisted on the step + written into the daily note. PRs #15 + #16 (2026-07-18): #15 writes a resume handoff on *every* terminal step failure (incl. schema-validation, not just rate-limits); #16 lets a BLOCKED deployer pass validation — a deploy-only task with no PR is a valid terminal state, not a hard fail. Earlier PR #14: non-code tasks route direct (not a code pipeline), plan-step validator relaxed to match role templates, model routing auto-downgrades `fable→opus→sonnet→haiku` at startup, telegram-only. Full state: `Projects/Hermes/Progress.md` in RaphBrain.

**Reloading the daemon is PRE-AUTHORIZED for Claude (Jaiden, 2026-07-22)** — after merging a Hermes PR, reload without asking: `launchctl kickstart -k gui/$(id -u)/dev.arbiterai.hermes`, then verify the boot (`grep "poller ready" logs/hermes.log`). This is the one deploy action that doesn't need a fresh OK. (Still ask before anything else outward-facing.)

Mac Mini orchestration daemon (replaced OpenClaw). Triggered by **Telegram** `[HERMES] task` (chat_id 8922766986); runs via `claude --print`. Tier-aware model routing (fable/opus/sonnet/haiku per role+tier). Direct tasks run **non-blocking** — `run_task` spawns the agent in a background thread and texts the result via an `on_complete` callback on exit (2h safety kill). Reload the LaunchAgent `dev.arbiterai.hermes` after changing `hermes.py`/`config.yaml`.

**Project routing** is via `project_registry.py` — the single source of truth (scans `~/Projects` + `~/Documents/RaphBrain/Projects`, knows all ~23 projects; add a project = make the folder). Say **`on <project>, <task>`** for deterministic routing; **`projects`/`help`/`status`/`abort`** are commands. Unknown project → fails loud, never runs in `$HOME`.

## Start here
- Full context + current state: `~/Documents/RaphBrain/Projects/Hermes/CONTEXT.md`, then `Progress.md`.

## Rules
- **Branch names must be meaningful, not raw task slugs** — extract issue numbers first (e.g. `fix/issues-192-195`), strip control words, use meaningful nouns.
- Feature branches + PR + Jaiden's review before merge.
- **Never stack PRs.** Cut every branch from an up-to-date `main` and target `main`. Do not base a branch (or a PR) on another open PR's branch — that's what tangled #5/#6/#7. If work truly depends on an unmerged branch, wait for it to land first, then branch from `main`.
- When merging, **don't `--delete-branch` a branch another open PR is based on**, and merge independent PRs one at a time (re-check mergeable between merges). GitHub's default branch is `main` — keep it that way so new PRs base off `main` automatically.
