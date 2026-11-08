"""Check that exported files reproduce the COLMAP model exactly (export/display errors vs reconstruction errors).

For one session model it re-projects every observed 3D point using ONLY the exported trajectory.json
(T_world_camera, camera-to-world) and the exported sparse.ply, and compares with the 2D keypoints COLMAP stored.
If the exported transform, axis order or point/camera frames were wrong, this error would be large even when
COLMAP's own reprojection error is small. It also checks that pose order follows the frame timestamps.

python -m mapping.verify_export <session_dir> [--model 0]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pycolmap
from scipy.spatial import cKDTree

from .export import read_ply


def verify(sd: Path, rank: int = 0) -> dict:
    report = json.loads((sd / "map_report.json").read_text())
    models = (report.get("reconstruction") or {}).get("models")
    colmap_dir = sd / (models[rank]["colmap_dir"] if models else report["reconstruction"]["chosen_model_dir"])
    exp_dir = sd / "models" / str(rank) if models else sd
    rec = pycolmap.Reconstruction(str(colmap_dir))
    traj = json.loads((exp_dir / "trajectory.json").read_text())
    ply = read_ply(exp_dir / "sparse.ply")
    pose = {p["image_name"]: np.array(p["T_world_camera"]) for p in traj["poses"]}
    xyz_ply = np.stack([ply["x"], ply["y"], ply["z"]], 1)
    # The PLY stores no point ids and COLMAP's point map has no stable iteration order once re-read from disk, so
    # each model point is matched to its nearest exported point; the match distance must be ~float32 rounding.
    ids = list(rec.points3D.keys())
    xyz_rec = np.array([rec.points3D[i].xyz for i in ids])
    dist, nn = cKDTree(xyz_ply).query(xyz_rec)
    point_index = dict(zip(ids, nn))
    scale = float(np.median(np.linalg.norm(xyz_rec - np.median(xyz_rec, 0), axis=1))) or 1.0
    errs_export, errs_colmap, behind = [], [], 0
    for im in rec.images.values():
        if not im.has_pose:
            continue
        cam = rec.cameras[im.camera_id]
        T = pose[im.name]
        R_wc, C = T[:3, :3], T[:3, 3]
        for p2 in im.points2D:
            if not p2.has_point3D():
                continue
            X = xyz_ply[point_index[p2.point3D_id]]
            Xc = R_wc.T @ (X - C)  # camera_from_world applied to the exported point
            if Xc[2] <= 0:
                behind += 1
                continue
            uv = np.asarray(cam.img_from_cam(np.asarray(Xc, float))).ravel()
            errs_export.append(float(np.linalg.norm(uv - np.asarray(p2.xy))))
            # COLMAP's own projection for the same observation (reference)
            Xc_ref = (im.cam_from_world() if callable(im.cam_from_world) else im.cam_from_world) * rec.points3D[p2.point3D_id].xyz
            errs_colmap.append(float(np.linalg.norm(np.asarray(cam.img_from_cam(np.asarray(Xc_ref, float))).ravel() - np.asarray(p2.xy))))
    ts = [p["timestamp_s"] for p in traj["poses"]]
    e, r = np.array(errs_export), np.array(errs_colmap)
    return {"session": sd.name, "model_rank": rank, "observations": int(len(e)), "behind_camera": behind,
            "points": {"model": len(ids), "ply": int(len(xyz_ply)), "unique_matches": int(len(set(nn.tolist()))),
                       "max_match_dist_rel": float(dist.max() / scale)},
            "reproj_export_px": {"mean": round(float(e.mean()), 4), "p95": round(float(np.percentile(e, 95)), 4)},
            "reproj_colmap_px": {"mean": round(float(r.mean()), 4), "p95": round(float(np.percentile(r, 95)), 4)},
            "max_abs_diff_px": round(float(np.max(np.abs(e - r))), 6),
            "pose_order_follows_time": bool(all(None not in (a, b) and a < b for a, b in zip(ts, ts[1:]))),
            "camera_in_trajectory": traj.get("camera")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.verify_export")
    ap.add_argument("session_dir")
    ap.add_argument("--model", type=int, default=0)
    a = ap.parse_args(argv)
    print(json.dumps(verify(Path(a.session_dir), a.model), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
