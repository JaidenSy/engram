#!/usr/bin/env python3
"""
daily_digest.py — Morning briefing sent via iMessage at 8am.

Aggregates:
  - Open PRs across watched repos
  - Failed/aborted Hermes runs from the last 24h
  - New GitHub issues (opened in last 24h) across watched repos

Reads GitHub token from keychain: service="hermes-github", account="token"
Sends iMessage to reply_to from config/config.yaml.

Invoked by LaunchAgent: ~/Library/LaunchAgents/com.hermes.daily-digest.plist
"""

import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import keyring
import requests
import yaml

HERMES_DIR = Path.home() / "hermes"
CONFIG_PATH = HERMES_DIR / "config" / "config.yaml"
RUNS_DIR = HERMES_DIR / "runs"
LOG_PATH = HERMES_DIR / "logs" / "daily-digest.log"

WATCHED_REPOS = [
    "JaidenSy/hermes",
    "JaidenSy/alphabot",
    "JaidenSy/Arbiter",
]

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
log = logging.getLogger("daily-digest")

LOOKBACK_HOURS = 24


def _gh_get(token: str, path: str, params: dict = None) -> list | dict:
    url = f"https://api.github.com{path}"
    resp = requests.get(
        url,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        params=params or {},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_open_prs(token: str) -> list[str]:
    lines = []
    for repo in WATCHED_REPOS:
        try:
            prs = _gh_get(
                token, f"/repos/{repo}/pulls", {"state": "open", "per_page": 10}
            )
            for pr in prs:
                lines.append(
                    f"  [{repo.split('/')[1]}] #{pr['number']} {pr['title'][:60]}"
                )
        except Exception as exc:
            log.warning(f"Failed to fetch PRs for {repo}: {exc}")
    return lines


def _fetch_recent_issues(token: str) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    lines = []
    for repo in WATCHED_REPOS:
        try:
            issues = _gh_get(
                token,
                f"/repos/{repo}/issues",
                {
                    "state": "open",
                    "sort": "created",
                    "direction": "desc",
                    "per_page": 10,
                },
            )
            for issue in issues:
                if "pull_request" in issue:
                    continue
                created = datetime.fromisoformat(
                    issue["created_at"].replace("Z", "+00:00")
                )
                if created >= cutoff:
                    lines.append(
                        f"  [{repo.split('/')[1]}] #{issue['number']} {issue['title'][:60]}"
                    )
        except Exception as exc:
            log.warning(f"Failed to fetch issues for {repo}: {exc}")
    return lines


def _fetch_failed_runs() -> list[str]:
    if not RUNS_DIR.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    lines = []
    for run_file in sorted(RUNS_DIR.glob("*.json"), reverse=True)[:50]:
        try:
            run = json.loads(run_file.read_text())
            status = run.get("status", "")
            if status not in ("failed", "aborted"):
                continue
            completed_at = run.get("completed_at") or run.get("created_at", "")
            if completed_at:
                ts = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
                if ts < cutoff:
                    continue
            project = run.get("project", "?")
            task = run.get("task_raw", "")[:50]
            lines.append(f"  [{status.upper()}] {project}: {task}")
        except Exception:
            continue
    return lines


def _send_imessage(reply_to: str, message: str) -> None:
    safe = message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    script = f"""tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set targetBuddy to buddy "{reply_to}" of targetService
    send "{safe}" to targetBuddy
end tell"""
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        log.error(f"iMessage send failed: {result.stderr.strip()}")
    else:
        log.info("Daily digest sent via iMessage")


def main():
    config = yaml.safe_load(CONFIG_PATH.read_text())
    reply_to = config.get("trigger", {}).get("imessage", {}).get("reply_to", "")
    if not reply_to:
        # Fall back to telegram chat_id for log-only mode
        log.info("No reply_to configured — printing digest to log only")

    token = keyring.get_password("hermes-github", "token")
    if not token:
        log.error(
            "GitHub token not found in keychain (service=hermes-github, account=token)"
        )
        sys.exit(1)

    open_prs = _fetch_open_prs(token)
    new_issues = _fetch_recent_issues(token)
    failed_runs = _fetch_failed_runs()

    now = datetime.now().strftime("%a %b %-d, %-I:%M %p")
    sections = [f"🌅 Hermes Morning Digest — {now}"]

    sections.append(f"\n📬 Open PRs ({len(open_prs)})")
    sections += open_prs if open_prs else ["  (none)"]

    sections.append(f"\n🐛 New Issues — last 24h ({len(new_issues)})")
    sections += new_issues if new_issues else ["  (none)"]

    sections.append(f"\n❌ Failed/Aborted Runs — last 24h ({len(failed_runs)})")
    sections += failed_runs if failed_runs else ["  (none)"]

    digest = "\n".join(sections)
    log.info(f"Digest:\n{digest}")

    if reply_to:
        _send_imessage(reply_to, digest[:1500])


if __name__ == "__main__":
    main()
