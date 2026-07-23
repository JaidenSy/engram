#!/bin/bash
# Queue a task for Engram from terminal

TASK="$1"
SESSION_NAME="manual-$(date +%s)"

if [ -z "$TASK" ]; then
  echo "Usage: start-session.sh 'your task description'"
  exit 1
fi

echo "$TASK" > ~/engram/tasks/${SESSION_NAME}.task
echo "Task queued: $SESSION_NAME"
echo "Watch: tail -f ~/engram/logs/${SESSION_NAME}.log"
