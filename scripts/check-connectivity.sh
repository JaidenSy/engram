#!/bin/bash

echo "=== Engram Connectivity Check ==="
echo "Timestamp: $(date)"
echo ""

echo "--- Tailscale ---"
if /usr/local/bin/tailscale status &>/dev/null; then
  /usr/local/bin/tailscale status
else
  echo "OFFLINE - attempting restart"
  open -a Tailscale
fi

echo ""

echo "--- Jump Desktop Connect ---"
if pgrep -x "JumpConnect" &>/dev/null; then
  echo "RUNNING (PID: $(pgrep -x 'JumpConnect'))"
else
  echo "OFFLINE - attempting restart"
  open -a "Jump Desktop Connect"
fi

echo ""
echo "=== Done ==="
