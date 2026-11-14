#!/usr/bin/env bash
# SIMULATED closed loop: synthetic people -> Kalman tracker -> yaw PID -> MockTello (loopback UDP). ~20 s.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export HARDWARE_ARMED=false DRY_RUN=true
PYTHONPATH="$ROOT/packages:$ROOT" exec .venv/bin/python -m sim.tracking_scenario "$@"
