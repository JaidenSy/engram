#!/bin/bash
# Queue a task for Hermes from terminal

TASK="$1"
SESSION_NAME="manual-$(date +%s)"

if [ -z "$TASK" ]; then
  echo "Usage: start-session.sh 'your task description'"
  exit 1
fi

echo "$TASK" > ~/hermes/tasks/${SESSION_NAME}.task
echo "Task queued: $SESSION_NAME"
echo "Watch: tail -f ~/hermes/logs/${SESSION_NAME}.log"
