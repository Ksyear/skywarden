"""Remove weakly supported FAR points from a fused static map, keep far structures that several views confirm.

A point is "far" when its distance to the nearest camera exceeds FAR_X times the median point-to-camera distance.
For each far point we count, over the plane-sweep depth maps of the run (confident depth only):
  support  = views whose depth at the point's pixel agrees within REL_TOL,
  conflict = views that see PAST the point (their depth > (1 + REL_TOL) x the point's depth: free space).
A far point is removed when support < MIN_SUPPORT or conflict > support. Near points are never touched here.
Nothing is filled in; removed points are written to a separate file so the change is inspectable.

python -m mapping.far_cleanup <session_dir> <cloud.ply> --maps <run dir with diag/depth_maps.npz> --out <dir>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pycolmap
from scipy.spatial import cKDTree

from .dense_eval import Cam
from .export import read_ply, write_ply

FAR_X = 2.0
REL_TOL = 0.10
MIN_SUPPORT = 2


def far_points(X: np.ndarray, centers: np.ndarray) -> tuple[np.ndarray, float]:
    """(far mask, median point-to-nearest-camera distance)."""
    dist, _ = cKDTree(centers).query(X)
    med = float(np.median(dist))
    return dist > FAR_X * med, med


def votes(Xf: np.ndarray, views) -> tuple[np.ndarray, np.ndarray]:
    """(support, conflict) per point over `views`: (Cam, confident depth map with nan elsewhere) pairs."""
    support = np.zeros(len(Xf), np.int16)
    conflict = np.zeros(len(Xf), np.int16)
    for cam, depth in views:
        uv, z = cam.pixels_undistorted(cam.cam_coords(Xf))
        u, v = np.round(uv[:, 0]).astype(np.int64), np.round(uv[:, 1]).astype(np.int64)
        ins = (z > 0) & (u >= 0) & (u < cam.w) & (v >= 0) & (v < cam.h)
        d = np.full(len(Xf), np.nan)
        d[ins] = depth[v[ins], u[ins]]
        has = ~np.isnan(d)
        support += (has & (np.abs(d - z) / np.maximum(z, 1e-9) <= REL_TOL)).astype(np.int16)
        conflict += (has & (d > (1 + REL_TOL) * z)).astype(np.int16)
    return support, conflict


def removed(support: np.ndarray, conflict: np.ndarray) -> np.ndarray:
    return (support < MIN_SUPPORT) | (conflict > support)


def run(sd: Path, cloud: Path, maps_run: Path, out: Path) -> dict:
    rep = json.loads((maps_run / "dense_report.json").read_text())
    names = rep["diagnostics"]["image_names"]
    rec = pycolmap.Reconstruction(str(sd / "sparse" / "0"))
    by = {im.name: im for im in rec.images.values()}
    Z = np.load(maps_run / "diag" / "depth_maps.npz")
    ww = rep["config"]["work_width"]
    P = read_ply(cloud)
    X = np.stack([P["x"], P["y"], P["z"]], 1).astype(np.float64)
    C = np.array([-np.asarray(by[n].cam_from_world().rotation.matrix()).T @ np.asarray(by[n].cam_from_world().translation) for n in names])
    far, med = far_points(X, C)
    idx = np.nonzero(far)[0]
    support, conflict = votes(X[idx], ((Cam(rec, by[names[int(r)]], ww), Z[f"d{int(r)}"]) for r in Z["refs"]))
    drop_far = removed(support, conflict)
    drop = np.zeros(len(X), bool)
    drop[idx[drop_far]] = True
    out.mkdir(parents=True, exist_ok=True)
    write_ply(out / "dense_verified.ply", P[~drop])
    write_ply(out / "removed_far_points.ply", P[drop])
    res = {"label": "FAR CLEANUP of " + str(cloud), "rule": {"far_x_median_camera_distance": FAR_X, "rel_tol": REL_TOL,
           "min_support_views": MIN_SUPPORT, "remove_if": "support < min_support or conflict > support"},
           "maps": str(maps_run / "diag" / "depth_maps.npz"), "median_camera_distance": med,
           "points_in": int(len(X)), "far_points": int(far.sum()), "removed": int(drop.sum()),
           "kept_far": int(far.sum() - drop.sum()), "dense_verified_points": int((~drop).sum()),
           "model_rank": 0, "config": {"work_width": ww}, "voxel": rep.get("voxel"), "views": [], "seconds": None}
    (out / "dense_report.json").write_text(json.dumps(res, indent=1))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.far_cleanup")
    ap.add_argument("session_dir"); ap.add_argument("cloud"); ap.add_argument("--maps", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    r = run(Path(a.session_dir), Path(a.cloud), Path(a.maps), Path(a.out))
    print(json.dumps({k: r[k] for k in ("points_in", "far_points", "removed", "kept_far", "median_camera_distance")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
