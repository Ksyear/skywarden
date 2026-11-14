#!/usr/bin/env bash
# Build a COLMAP sparse map from a video file or image folder.
# Usage: scripts/build_sparse_map.sh <video|image_dir> [--session ID] [--origin REAL_VIDEO|PUBLIC_DATASET|SYNTHETIC] [--camera-json FILE] [--fps 3] [--max-frames 150]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
[[ -x .venv/bin/python ]] || { echo "Missing .venv. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2; exit 2; }
PYTHONPATH="$ROOT/packages" exec .venv/bin/python -m mapping.cli build "$@"
