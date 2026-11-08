"""C5 point filtering view (docs/PLAN_GEOMETRY_AND_DENSE.md): relative criteria only, no metric cut-offs.

A point is kept in `sparse_filtered.ply` when its triangulation angle >= 1.5 deg, its track has >= 3 observations,
and its reprojection error <= 2 x the model's median. The raw `sparse.ply` is never modified; the viewer offers raw
and filtered as separate views and shows both counts.

python -m mapping.points_filter <session_dir>     (all exported models)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pycolmap

from .export import PLY_DTYPE, write_ply

MIN_ANGLE_DEG = 1.5
MIN_TRACK = 3
MAX_REPROJ_X_MEDIAN = 2.0


def filter_model(rec, out: Path) -> dict:
    centers = {i: np.asarray(im.projection_center()) for i, im in rec.images.items() if im.has_pose}
    names = sorted(((rec.images[i].name, i) for i in centers))
    rank = {i: k for k, (_, i) in enumerate(names)}
    errs = np.array([p.error for p in rec.points3D.values()])
    max_err = MAX_REPROJ_X_MEDIAN * float(np.median(errs)) if len(errs) else 0.0
    kept, why = [], {"angle": 0, "track": 0, "reproj": 0}
    for p in rec.points3D.values():
        obs = [e.image_id for e in p.track.elements if e.image_id in centers]
        rays = np.array([p.xyz - centers[i] for i in obs])
        rays /= np.linalg.norm(rays, axis=1, keepdims=True)
        ang = float(np.degrees(np.arccos(np.clip(np.min(rays @ rays.T), -1, 1)))) if len(rays) > 1 else 0.0
        bad = False
        if ang < MIN_ANGLE_DEG:
            why["angle"] += 1; bad = True
        if len(obs) < MIN_TRACK:
            why["track"] += 1; bad = True
        if p.error > max_err:
            why["reproj"] += 1; bad = True
        if not bad:
            kept.append((p, min(rank[i] for i in obs)))
    pts = np.zeros(len(kept), dtype=PLY_DTYPE)
    for j, (p, fs) in enumerate(kept):
        pts[j]["x"], pts[j]["y"], pts[j]["z"] = p.xyz
        pts[j]["red"], pts[j]["green"], pts[j]["blue"] = p.color
        pts[j]["first_seen"] = fs
    write_ply(out, pts)
    return {"raw": len(errs), "filtered": len(kept), "removed_by": why,
            "rule": {"min_triangulation_angle_deg": MIN_ANGLE_DEG, "min_track_length": MIN_TRACK,
                     "max_reprojection_x_model_median": MAX_REPROJ_X_MEDIAN, "max_reprojection_px": round(max_err, 3)}}


def filter_session(sd: Path) -> dict:
    report = json.loads((sd / "map_report.json").read_text())
    models = (report.get("reconstruction") or {}).get("models")
    out = {}
    if models:
        for m in models:
            rec = pycolmap.Reconstruction(str(sd / m["colmap_dir"]))
            out[m["rank"]] = filter_model(rec, sd / "models" / str(m["rank"]) / "sparse_filtered.ply")
        (sd / "sparse_filtered.ply").write_bytes((sd / "models" / "0" / "sparse_filtered.ply").read_bytes())
    else:
        rec = pycolmap.Reconstruction(str(sd / report["reconstruction"]["chosen_model_dir"]))
        out[0] = filter_model(rec, sd / "sparse_filtered.ply")
    report["point_filter"] = out
    (sd / "map_report.json").write_text(json.dumps(report, indent=1))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.points_filter")
    ap.add_argument("session_dir")
    print(json.dumps(filter_session(Path(ap.parse_args(argv).session_dir)), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
