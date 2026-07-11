#!/bin/zsh
# brain-lint.sh — RaphBrain vault hygiene report for agent/MCP consumption.
# Checks: missing frontmatter, missing description, oversized notes,
# stale agents/ handoffs that should move to agents/archive/.
# Usage: brain-lint.sh [--fix-archive]   (--fix-archive moves stale handoffs)

set -uo pipefail

VAULT="$HOME/Documents/RaphBrain"
CUTOFF=$(date -v-14d +%Y-%m-%d)
FIX_ARCHIVE=false
[[ "${1:-}" == "--fix-archive" ]] && FIX_ARCHIVE=true

cd "$VAULT"

echo "=== RaphBrain lint — $(date +%Y-%m-%d) ==="
echo

echo "── Notes missing frontmatter (agents/MCP search can't index these) ──"
find Projects AI-Guides Personal -name "*.md" -not -path "*/archive/*" -print0 |
while IFS= read -r -d '' f; do
    [[ "$(head -1 "$f")" != "---" ]] && echo "  $f"
done

echo
echo "── Frontmatter but no description: ──"
find Projects AI-Guides Personal -name "*.md" -not -path "*/archive/*" -print0 |
while IFS= read -r -d '' f; do
    if [[ "$(head -1 "$f")" == "---" ]] && ! head -10 "$f" | grep -q "^description:"; then
        echo "  $f"
    fi
done

echo
echo "── Notes over 15KB (bloat agent context — split or summarize) ──"
find Projects AI-Guides -name "*.md" -size +15k -not -path "*/archive/*" -exec ls -la {} \; |
    awk '{printf "  %6.1fKB  %s\n", $5/1024, $9}' | sort -rn

echo
echo "── Stale agents/ handoffs (last git commit before $CUTOFF) ──"
for f in Projects/*/agents/*.md(N); do
    d=$(git log -1 --format=%as -- "$f" 2>/dev/null)
    if [[ -n "$d" && "$d" < "$CUTOFF" ]]; then
        if $FIX_ARCHIVE; then
            dir=$(dirname "$f")
            mkdir -p "$dir/archive"
            mv "$f" "$dir/archive/"
            echo "  archived: $f"
        else
            echo "  $f  (last commit $d)"
        fi
    fi
done

echo
echo "── Projects missing CONTEXT.md (agents fall back to reading 3+ files) ──"
for d in Projects/*/(N); do
    [[ -f "${d}CONTEXT.md" ]] || echo "  ${d%/}"
done

echo
echo "Done. Fix archives with: brain-lint.sh --fix-archive"
