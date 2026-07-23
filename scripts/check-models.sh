#!/bin/bash

echo "=== Engram Model Availability Check ==="
echo "Timestamp: $(date)"
echo ""

echo "--- Claude Code (Primary) ---"
if which claude &>/dev/null; then
  echo "✓ claude found at $(which claude)"
else
  echo "✗ Claude Code not on PATH"
fi
echo ""

echo "--- Anthropic API (Opus/Haiku) — SAFE FOR ALL TASKS ---"
API_KEY=$(security find-generic-password -a "engram" -s "anthropic-api-key" -w 2>/dev/null)
if [ -n "$API_KEY" ]; then
  RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" \
    https://api.anthropic.com/v1/messages \
    -H "x-api-key: $API_KEY" \
    -H "anthropic-version: 2023-06-01" \
    -H "content-type: application/json" \
    -d '{"model":"claude-haiku-4-5-20251001","max_tokens":10,"messages":[{"role":"user","content":"ping"}]}')
  [ "$RESPONSE" = "200" ] && echo "✓ Anthropic API reachable" || echo "✗ HTTP $RESPONSE"
else
  echo "✗ No API key — run: security add-generic-password -a engram -s anthropic-api-key -w YOUR_KEY"
fi
echo ""

echo "--- DeepSeek API — ⚠ PUBLIC TASKS ONLY ---"
DS_KEY=$(security find-generic-password -a "engram" -s "deepseek-api-key" -w 2>/dev/null)
if [ -n "$DS_KEY" ]; then
  RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" \
    https://api.deepseek.com/chat/completions \
    -H "Authorization: Bearer $DS_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"ping"}],"max_tokens":5}')
  [ "$RESPONSE" = "200" ] && echo "✓ DeepSeek API reachable (\$0.14/MTok)" || echo "✗ HTTP $RESPONSE"
else
  echo "✗ No key — get at platform.deepseek.com (5M free tokens on signup)"
fi
echo ""

echo "--- Qwen API — ⚠ PUBLIC TASKS ONLY ---"
QW_KEY=$(security find-generic-password -a "engram" -s "qwen-api-key" -w 2>/dev/null)
if [ -n "$QW_KEY" ]; then
  echo "✓ Qwen key found (\$0.05/MTok — cheapest option)"
else
  echo "✗ No key — get at alibabacloud.com/dashscope (70M token trial)"
fi
echo ""

echo "--- Gemini API (Flash-Lite / 3.5 Flash) — SAFE, MULTIMODAL ---"
GM_KEY=$(security find-generic-password -a "engram" -s "gemini-api-key" -w 2>/dev/null)
if [ -n "$GM_KEY" ]; then
  RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" \
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key=$GM_KEY" \
    -H "Content-Type: application/json" \
    -d '{"contents":[{"parts":[{"text":"ping"}]}]}')
  [ "$RESPONSE" = "200" ] && echo "✓ Gemini API reachable (\$0.10/MTok, free tier)" || echo "✗ HTTP $RESPONSE"
else
  echo "✗ No key — get free at aistudio.google.com"
fi
echo ""

echo "--- Ollama Local Models (FULLY PRIVATE) ---"
if curl -s http://127.0.0.1:11434/api/tags &>/dev/null; then
  echo "✓ Ollama running on localhost"
  curl -s --connect-timeout 1 http://0.0.0.0:11434/api/tags &>/dev/null \
    && echo "  ⚠ WARNING: Ollama exposed on 0.0.0.0 — set OLLAMA_HOST=127.0.0.1" \
    || echo "  ✓ Localhost-only (secure)"
  MODELS=$(curl -s http://127.0.0.1:11434/api/tags | python3 -c "
import json, sys
data = json.load(sys.stdin)
models = [m['name'] for m in data.get('models', [])]
print('\n'.join(f'  ✓ {m}' for m in models) if models else '  (no models pulled yet)')
")
  echo "$MODELS"
else
  echo "✗ Ollama not running — brew services start ollama"
fi
echo ""

echo "--- Higgsfield MCP (Mira/Sōka assets) ---"
echo "  Manual check: confirm connected in Claude Code at https://mcp.higgsfield.ai/mcp"
echo ""

echo "=== Done ==="
