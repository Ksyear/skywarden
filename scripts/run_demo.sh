#!/usr/bin/env bash
# Start the local map console on 127.0.0.1. Usage: scripts/run_demo.sh [--check] [--port 8780]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export HARDWARE_ARMED=false DRY_RUN=true
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || { echo "Missing .venv. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2; exit 2; }
if [[ "${1:-}" == "--check" ]]; then
  echo "root=$ROOT"
  echo "python=$("$PY" --version 2>&1) pycolmap=$("$PY" -c 'import pycolmap;print(pycolmap.__version__)')"
  echo "maps=$ROOT/outputs/maps inputs=$ROOT/assets/input,$ROOT/data/public,$ROOT/outputs/synthetic"
  echo "HARDWARE_ARMED=$HARDWARE_ARMED DRY_RUN=$DRY_RUN"
  exit 0
fi
exec "$PY" "$ROOT/apps/desktop/server.py" "$@"
