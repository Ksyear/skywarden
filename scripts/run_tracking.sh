#!/usr/bin/env bash
# Offline person tracking on a clip with the real YOLOX detector. Usage: scripts/run_tracking.sh <video|image_dir> <session> [extra args]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export HARDWARE_ARMED=false DRY_RUN=true
PYTHONPATH="$ROOT/packages" exec "$ROOT/.venv/bin/python" -m vision.track_video "$1" --session "$2" --tracks-root outputs/tracks "${@:3}"
