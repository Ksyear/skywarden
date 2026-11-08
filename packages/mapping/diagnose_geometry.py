"""Per-point and per-frame geometry diagnostics for one reconstructed model (no ground truth needed).

Per point:  triangulation angle (largest angle between two viewing rays of its track), track length, reprojection.
            Points seen only under tiny angles have ill-defined depth: they are the "abnormally far" points.
Per frame:  median depth of the points it sees, camera step to the previous frame, image motion of the shared
            points (px), and the dimensionless motion-consistency ratio
                r = (step / median_depth) / (median_image_motion / focal)
            For a translating camera r stays roughly constant along a consistent reconstruction; a scale drift
            shows up as r changing by a large factor between segments. This is a relative check; it says nothing
            about metric accuracy.

python -m mapping.diagnose_geometry <session_dir> [--model 0] [--out file.json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pycolmap


def point_stats(rec) -> dict:
    centers = {i: np.asarray(im.projection_center()) for i, im in rec.images.items() if im.has_pose}
    ang, tl, err, pid_list = [], [], [], []
    for pid, p in rec.points3D.items():
        cs = [centers[e.image_id] for e in p.track.elements if e.image_id in centers]
        rays = np.array([p.xyz - c for c in cs])
        rays /= np.linalg.norm(rays, axis=1, keepdims=True)
        cosmin = float(np.min(rays @ rays.T)) if len(rays) > 1 else 1.0
        ang.append(np.degrees(np.arccos(np.clip(cosmin, -1, 1))))
        tl.append(p.track.length())
        err.append(p.error)
        pid_list.append(pid)
    return {"ids": pid_list, "tri_angle_deg": np.array(ang), "track_len": np.array(tl), "reproj_px": np.array(err)}


def frame_stats(rec, frames_json: list[dict]) -> list[dict]:
    order = {f["file"]: (i, f.get("timestamp_s")) for i, f in enumerate(frames_json)}
    ims = sorted((im for im in rec.images.values() if im.has_pose), key=lambda im: order[im.name][0])
    f = float(list(rec.cameras.values())[0].params[0])
    rows, prev = [], None
    for im in ims:
        C = np.asarray(im.projection_center())
        obs = {p2.point3D_id: np.asarray(p2.xy) for p2 in im.points2D if p2.has_point3D()}
        cfw = im.cam_from_world() if callable(im.cam_from_world) else im.cam_from_world
        depths = [float((cfw * rec.points3D[pid].xyz)[2]) for pid in obs]
        row = {"frame": order[im.name][0], "t": order[im.name][1], "n_obs": len(obs),
               "median_depth": float(np.median(depths)) if depths else None}
        if prev is not None:
            shared = set(obs) & set(prev["obs"])
            motion = [float(np.linalg.norm(obs[p] - prev["obs"][p])) for p in shared]
            step = float(np.linalg.norm(C - prev["C"]))
            row.update(shared=len(shared), step=step, image_motion_px=float(np.median(motion)) if motion else None)
            if motion and row["median_depth"] and np.median(motion) > 0.05:
                row["consistency_r"] = (step / row["median_depth"]) / (float(np.median(motion)) / f)
        rows.append(row)
        prev = {"obs": obs, "C": C}
    return rows


def diagnose(sd: Path, rank: int = 0) -> dict:
    report = json.loads((sd / "map_report.json").read_text())
    models = (report.get("reconstruction") or {}).get("models")
    rec = pycolmap.Reconstruction(str(sd / (models[rank]["colmap_dir"] if models else report["reconstruction"]["chosen_model_dir"])))
    frames = json.loads((sd / "frames.json").read_text())
    ps = point_stats(rec)
    fr = frame_stats(rec, frames)
    a = ps["tri_angle_deg"]
    r = np.array([x["consistency_r"] for x in fr if x.get("consistency_r")])
    t_r = np.array([x["t"] for x in fr if x.get("consistency_r")])
    third = np.array_split(np.arange(len(r)), 3)
    cam = list(rec.cameras.values())[0]
    return {
        "session": sd.name, "model_rank": rank, "camera": {"model": cam.model.name, "params": [round(float(v), 4) for v in cam.params]},
        "points": {"n": int(len(a)),
                   "tri_angle_deg": {q: round(float(np.percentile(a, q)), 3) for q in (5, 25, 50, 75, 95)},
                   "share_tri_angle_lt_1deg": round(float(np.mean(a < 1)), 3), "share_lt_2deg": round(float(np.mean(a < 2)), 3),
                   "track_len_p50": float(np.median(ps["track_len"])), "share_track_len_2": round(float(np.mean(ps["track_len"] <= 2)), 3)},
        "consistency_r": {"n": int(len(r)), "median": round(float(np.median(r)), 3) if len(r) else None,
                          "by_third": [{"t_range": [round(float(t_r[i[0]]), 1), round(float(t_r[i[-1]]), 1)],
                                        "median": round(float(np.median(r[i])), 3)} for i in third if len(i)]},
        "frames": fr if False else [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in x.items()} for x in fr],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.diagnose_geometry")
    ap.add_argument("session_dir")
    ap.add_argument("--model", type=int, default=0)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    d = diagnose(Path(a.session_dir), a.model)
    if a.out:
        Path(a.out).write_text(json.dumps(d, indent=1))
    print(json.dumps({k: v for k, v in d.items() if k != "frames"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
