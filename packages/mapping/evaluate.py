"""Trajectory accuracy against ground truth (TUM format). Monocular SfM has unknown scale, so the estimate is
Sim(3)-aligned to ground truth first; the resulting metre value describes trajectory *shape* error only and does
not make the map METRIC_ANCHORED (scale was borrowed from ground truth for evaluation)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np


def umeyama(src: np.ndarray, dst: np.ndarray, with_scale: bool = True) -> tuple[float, np.ndarray, np.ndarray]:
    """Return s, R, t minimising ||dst - (s R src + t)||. src, dst: (N,3), N >= 3, not collinear."""
    if src.shape != dst.shape or src.shape[0] < 3:
        raise ValueError("need >= 3 corresponding points")
    mu_s, mu_d = src.mean(0), dst.mean(0)
    xs, xd = src - mu_s, dst - mu_d
    cov = xd.T @ xs / len(src)
    U, D, Vt = np.linalg.svd(cov)
    S = np.eye(3)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        S[2, 2] = -1
    R = U @ S @ Vt
    var_s = (xs ** 2).sum() / len(src)
    if var_s < 1e-12:
        raise ValueError("degenerate source points (no spread)")
    s = float(np.trace(np.diag(D) @ S) / var_s) if with_scale else 1.0
    t = mu_d - s * R @ mu_s
    return s, R, t


def load_tum_groundtruth(path: Path) -> np.ndarray:
    rows = [list(map(float, ln.split())) for ln in Path(path).read_text().splitlines() if ln and not ln.startswith("#")]
    return np.array(rows)  # t tx ty tz qx qy qz qw


def associate(est_times: np.ndarray, gt_times: np.ndarray, max_dt: float = 0.02) -> list[tuple[int, int]]:
    idx = np.clip(np.searchsorted(gt_times, est_times), 1, len(gt_times) - 1)
    pairs = []
    for i, j in enumerate(idx):
        k = j - 1 if abs(gt_times[j - 1] - est_times[i]) < abs(gt_times[j] - est_times[i]) else j
        if abs(gt_times[k] - est_times[i]) <= max_dt:
            pairs.append((i, k))
    return pairs


def evaluate_session(session_dir: Path, groundtruth: Path) -> dict:
    traj = json.loads((session_dir / "trajectory.json").read_text())
    poses = [p for p in traj["poses"] if p.get("timestamp_s") is not None]
    if len(poses) < 3:
        raise ValueError("fewer than 3 timestamped poses; cannot evaluate")
    est_t = np.array([p["timestamp_s"] for p in poses])
    est_c = np.array([np.array(p["T_world_camera"])[:3, 3] for p in poses])
    gt = load_tum_groundtruth(groundtruth)
    pairs = associate(est_t, gt[:, 0])
    if len(pairs) < 3:
        raise ValueError("fewer than 3 poses matched ground-truth timestamps")
    src = est_c[[i for i, _ in pairs]]
    dst = gt[[k for _, k in pairs], 1:4]
    s, R, t = umeyama(src, dst)
    err = np.linalg.norm((s * (R @ src.T)).T + t - dst, axis=1)
    path_len = float(np.linalg.norm(np.diff(dst, axis=0), axis=1).sum())
    return {
        "method": "ATE after Sim(3) Umeyama alignment of camera centres (scale taken from ground truth)",
        "matched_poses": len(pairs), "registered_poses": len(traj["poses"]),
        "ate_rmse_m": round(float(np.sqrt((err ** 2).mean())), 4),
        "ate_median_m": round(float(np.median(err)), 4), "ate_max_m": round(float(err.max()), 4),
        "gt_path_length_m": round(path_len, 3), "sim3_scale": round(s, 5),
        "note": "Scale borrowed from ground truth for evaluation only; the map itself stays RELATIVE.",
    }


if __name__ == "__main__":
    # python -m mapping.evaluate <session_dir> <groundtruth.txt>  -> prints and stores <session_dir>/evaluation.json
    session, gt = Path(sys.argv[1]), Path(sys.argv[2])
    result = {**evaluate_session(session, gt), "groundtruth": str(gt)}
    (session / "evaluation.json").write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))
