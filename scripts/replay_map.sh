#!/usr/bin/env bash
# Load a stored map session and print its verified summary. Usage: scripts/replay_map.sh outputs/maps/<session>
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHONPATH="$ROOT/packages" exec .venv/bin/python -m mapping.cli replay "$@"
