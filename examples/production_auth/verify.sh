#!/usr/bin/env bash
set -euo pipefail

BASE="${BASE:-http://localhost:7071/api}"
KEY="${FUNCTION_KEY:-}"

echo "== health (requires key) =="
if [[ -n "$KEY" ]]; then
  curl -fsS "$BASE/health?code=$KEY" && echo
else
  status=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health")
  echo "  HTTP $status (set FUNCTION_KEY to get 200)"
fi

echo "== anonymous public_agent =="
curl -fsS -X POST "$BASE/graphs/public_agent/invoke" \
  -H "Content-Type: application/json" \
  -d '{"input":{"messages":[{"role":"human","content":"hi"}]}}' && echo

echo "== private_agent without key (expect 401) =="
status=$(curl -s -o /dev/null -w '%{http_code}' \
  -X POST "$BASE/graphs/private_agent/invoke" \
  -H "Content-Type: application/json" \
  -d '{"input":{"messages":[{"role":"human","content":"hi"}]}}')
echo "  HTTP $status"

if [[ -n "$KEY" ]]; then
  echo "== private_agent with key =="
  curl -fsS -X POST "$BASE/graphs/private_agent/invoke?code=$KEY" \
    -H "Content-Type: application/json" \
    -d '{"input":{"messages":[{"role":"human","content":"hi"}]}}' && echo
else
  echo "(set FUNCTION_KEY=... to test the authenticated call)"
fi
