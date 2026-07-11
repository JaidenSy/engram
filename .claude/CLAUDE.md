# Hermes — Sub-agent Context

> If you are a Hermes sub-agent dispatched via `claude -p`: your role and task
> are fully described in the prompt. Focus only on your role instructions and the rules below.

## Stack
- Python 3.13, no external web framework — stdlib `http.server` for Mission Control UI
- Ollama at `http://localhost:11434` — used ONLY for task classification (llama3.1:8b)
- All agent dispatch via `claude -p` (Claude Code CLI, not API)
- LaunchAgent label: `dev.arbiterai.hermes` — restart via `launchctl stop/start`

## Key Paths
- `~/hermes/hermes.py` — main daemon, iMessage polling
- `~/hermes/planner.py` — Ollama task classification
- `~/hermes/run_engine.py` — pipeline state machine
- `~/hermes/agent_runner.py` — prompt builder, task JSON writer, tmux dispatch
- `~/raphael/agents/run-agent.sh` — universal agent runner (reads task JSON, calls `claude -p`)
- `~/raphael/tasks/` — task JSON + prompt files
- `~/hermes/runs/` — run state JSON files

## Critical Rules
- Never commit to `main` — Hermes uses `main` directly (no develop branch)
- LaunchAgent must be reloaded after any change to `hermes.py` or `config/config.yaml`
- Do NOT modify Ollama models or Tailscale/Jump watchdog plists without Jaiden's approval
- Tests live in `~/hermes/tests/` — run with `python3 -m pytest tests/ -x -q`

## RaphBrain Project Notes
- `~/Documents/RaphBrain/Projects/Hermes/Hermes-Agent-Setup.md`
