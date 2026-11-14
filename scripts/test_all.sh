#!/usr/bin/env bash
# Run the automated test suite and keep the raw log. Usage: scripts/test_all.sh [pytest args]
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p outputs/evidence/tests
LOG="outputs/evidence/tests/pytest-$(date +%Y%m%d-%H%M%S).log"
.venv/bin/python -m pytest -v "$@" 2>&1 | tee "$LOG"
status=${PIPESTATUS[0]}
echo "log: $LOG (exit $status)"
exit "$status"
