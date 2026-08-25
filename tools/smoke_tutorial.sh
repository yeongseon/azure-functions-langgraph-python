#!/usr/bin/env bash
# Boot the examples/simple_agent Function host locally and verify the exact
# commands documented in docs/tutorial-5-min.md (health + invoke).
#
# This is the *full-host* companion to tests/test_tutorial_smoke.py (which
# verifies the same contracts in-process, in CI). Run it locally to prove the
# tutorial end-to-end against a real `func start`:
#
#     ./tools/smoke_tutorial.sh
#
# Requires: Azure Functions Core Tools v4 (`func`), Python 3.10+, curl.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$REPO_ROOT/examples/simple_agent"
BASE="http://localhost:7071/api"
GRAPH="simple_agent"

command -v func >/dev/null 2>&1 || { echo "ERROR: Azure Functions Core Tools (func) not found" >&2; exit 1; }

echo ">> Installing example requirements"
python -m pip install -q -r "$APP_DIR/requirements.txt"

echo ">> Starting func host"
( cd "$APP_DIR" && func start ) >/tmp/func_smoke.log 2>&1 &
FUNC_PID=$!
cleanup() { kill "$FUNC_PID" 2>/dev/null || true; }
trap cleanup EXIT

echo ">> Waiting for host to become ready"
for _ in $(seq 1 60); do
  if curl -fsS "$BASE/health" >/dev/null 2>&1; then break; fi
  sleep 2
done

echo ">> GET /api/health"
health="$(curl -fsS "$BASE/health")"
echo "$health"
echo "$health" | grep -q '"status"' && echo "$health" | grep -q '"ok"' \
  || { echo "FAIL: unexpected health response" >&2; exit 1; }

echo ">> POST /api/graphs/$GRAPH/invoke"
invoke="$(curl -fsS -X POST "$BASE/graphs/$GRAPH/invoke" \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "World"}], "greeting": ""}}')"
echo "$invoke"
echo "$invoke" | grep -q 'Hello, World! Goodbye!' \
  || { echo "FAIL: unexpected invoke response" >&2; exit 1; }

echo ">> Tutorial smoke passed"
