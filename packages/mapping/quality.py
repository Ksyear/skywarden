"""Map quality verdict, kept separate from job completion.

`status` (SUCCEEDED / FAILED / CANCELLED) only says the pipeline ran and wrote consistent files. `quality.verdict`
says whether the map is usable as one continuous reconstruction of the declared input:
  GOOD    every check passes
  PARTIAL reprojection passes and registered_ratio >= PARTIAL_MIN_RATIO, but another check fails
  POOR    otherwise
The thresholds were frozen in docs/PLAN_MAPPING_QUALITY.md before any re-run and are written into every report.
Changing them requires a new report schema version, never a silent edit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np


@dataclass(frozen=True)
class QualityThresholds:
    min_registered_ratio: float = 0.6
    min_time_coverage: float = 0.7
    max_internal_gap_s: float = 2.0
    max_mean_reprojection_px: float = 1.5
    models_ge_10pct_exactly: int = 1
    max_trajectory_jumps: int = 0
    jump_factor: float = 5.0  # normalised step (distance / dt) above this x median counts as a jump
    min_points3D: int = 2000
    partial_min_ratio: float = 0.3


THRESHOLDS = QualityThresholds()


def trajectory_jumps(times: list[float], centers: list[list[float]], factor: float) -> list[dict]:
    """Time-normalised jump test on one model's camera path (sorted by time). Units are the model's own."""
    if len(times) < 3:
        return []
    t = np.asarray(times, float)
    c = np.asarray(centers, float)
    dt = np.diff(t)
    dt[dt <= 0] = np.nan
    speed = np.linalg.norm(np.diff(c, axis=0), axis=1) / dt
    med = float(np.nanmedian(speed))
    if not np.isfinite(med) or med <= 0:
        return []
    return [{"t_from": float(t[i]), "t_to": float(t[i + 1]), "speed_x_median": round(float(speed[i] / med), 1)}
            for i in range(len(speed)) if np.isfinite(speed[i]) and speed[i] > factor * med]


def evaluate(selected_times: list[float], model_frames: list[list[int]], reprojection_px: float, points3D: int,
             largest_centers: list[list[float]], th: QualityThresholds = THRESHOLDS) -> dict:
    """selected_times: timestamp (s) of every selected frame, in order. model_frames: for each model (largest first)
    the indices into selected_times of its registered frames. largest_centers: camera centres of the largest
    model in the same order as model_frames[0]."""
    n = len(selected_times)
    t = np.asarray(selected_times, float)
    span = float(t[-1] - t[0]) if n > 1 else 0.0
    largest = sorted(model_frames[0]) if model_frames else []
    ratio = len(largest) / n if n else 0.0
    # time covered by the largest model: intervals between consecutive selected frames that are both registered
    reg = set(largest)
    covered = float(sum(t[i + 1] - t[i] for i in range(n - 1) if i in reg and i + 1 in reg))
    coverage = covered / span if span > 0 else 0.0
    gaps = [t[b] - t[a] for a, b in zip(largest, largest[1:])]
    max_gap = float(max(gaps)) if gaps else (span if n > 1 else 0.0)
    big_models = sum(1 for m in model_frames if n and len(m) >= 0.1 * n)
    jumps = trajectory_jumps([float(t[i]) for i in largest], largest_centers, th.jump_factor) if largest else []
    checks = {
        "registered_ratio": {"value": round(ratio, 3), "pass": bool(ratio >= th.min_registered_ratio)},
        "time_coverage": {"value": round(coverage, 3), "pass": bool(coverage >= th.min_time_coverage)},
        "max_internal_gap_s": {"value": round(max_gap, 2), "pass": bool(max_gap <= th.max_internal_gap_s)},
        "mean_reprojection_px": {"value": round(float(reprojection_px), 3), "pass": bool(reprojection_px <= th.max_mean_reprojection_px)},
        "models_ge_10pct": {"value": big_models, "pass": big_models == th.models_ge_10pct_exactly},
        "trajectory_jumps": {"value": len(jumps), "pass": len(jumps) <= th.max_trajectory_jumps},
        "points3D": {"value": int(points3D), "pass": points3D >= th.min_points3D},
    }
    if all(c["pass"] for c in checks.values()):
        verdict = "GOOD"
    elif checks["mean_reprojection_px"]["pass"] and ratio >= th.partial_min_ratio:
        verdict = "PARTIAL"
    else:
        verdict = "POOR"
    return {"verdict": verdict, "failed_checks": [k for k, c in checks.items() if not c["pass"]], "checks": checks,
            "jumps": jumps, "thresholds": asdict(th), "evaluated_on": "largest model vs all selected frames of the input"}
