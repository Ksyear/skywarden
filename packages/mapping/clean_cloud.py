"""Display cleanup for a dense layer: merge points into small cubes and drop the ones floating on their own.

  1. points are grouped into small cubes (size = --voxel-x times the layer's voxel)
  2. lonely points: a cube with fewer than --min-nb occupied cubes within 2 cube sizes is dropped with all its points
     (stray rays, dust); every other original point is kept as it was
  --merge additionally replaces each cube by one mean point (measured: thins near floor badly, so off by default)
No point is moved off its surface beyond half a cube; nothing is added. Writes dense_verified.ply + dense_report.json.

python -m mapping.clean_cloud <layer_dir> --out <dir> [--voxel-x 1.0 --min-nb 6]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

from mapping.export import PLY_DTYPE, read_ply, write_ply


def clean(X: np.ndarray, C: np.ndarray, vox: float, min_nb: int, merge: bool = False):
    key = np.floor(X / vox).astype(np.int64)
    _, inv, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    inv = inv.ravel()
    P = np.zeros((len(cnt), 3)); np.add.at(P, inv, X); P /= cnt[:, None]
    Q = np.zeros((len(cnt), 3)); np.add.at(Q, inv, C.astype(np.float64)); Q /= cnt[:, None]
    d, _ = cKDTree(P).query(P, k=min_nb + 1, distance_upper_bound=2.0 * vox)
    keep = np.isfinite(d[:, -1])
    if not merge:  # keep every original point whose cube has enough occupied neighbours (density stays as it was)
        return X[keep[inv]], C[keep[inv]], len(cnt), int((~keep).sum())
    return P[keep], Q[keep], len(cnt), int((~keep).sum())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.clean_cloud")
    ap.add_argument("layer"); ap.add_argument("--out", required=True)
    ap.add_argument("--voxel-x", type=float, default=1.0); ap.add_argument("--min-nb", type=int, default=6)
    ap.add_argument("--merge", action="store_true", help="also merge each cube to one point (thins near surfaces; off by default)")
    ap.add_argument("--voxel", type=float, help="cube size in map units (default: the layer report's voxel)")
    a = ap.parse_args(argv)
    src, out = Path(a.layer), Path(a.out)
    rep = json.loads((src / "dense_report.json").read_text())
    vox = (a.voxel or rep.get("voxel")) * a.voxel_x
    M = read_ply(src / "dense_verified.ply")
    X = np.stack([M["x"], M["y"], M["z"]], 1).astype(np.float64); C = np.stack([M["red"], M["green"], M["blue"]], 1)
    P, Q, n_vox, n_lonely = clean(X, C, vox, a.min_nb, a.merge)
    arr = np.zeros(len(P), dtype=PLY_DTYPE)
    arr["x"], arr["y"], arr["z"] = P.T; arr["red"], arr["green"], arr["blue"] = np.clip(np.round(Q), 0, 255).T
    out.mkdir(parents=True, exist_ok=True)
    write_ply(out / "dense_verified.ply", arr)
    rep.update({"dense_verified_points": int(len(P)), "cleanup": {"from": str(src), "points_in": int(len(X)), "cubes": n_vox,
                "lonely_cubes_removed": n_lonely, "cube_size": vox, "min_neighbours": a.min_nb}})
    rep["label"] = "CLEANED FOR DISPLAY (lonely points removed): " + rep.get("label", "")
    (out / "dense_report.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep["cleanup"] | {"points_out": int(len(P))}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
