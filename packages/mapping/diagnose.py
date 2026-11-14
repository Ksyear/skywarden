"""Sliding-window geometry diagnosis (docs/PLAN_GEOMETRY_AND_DENSE.md, section B). Read-only on a finished session.

Per consecutive registered pair i -> i+1 of the largest model:
  phi = median |x' - warp(x; K R_rel K^-1)| / f      rotation-compensated image flow (rad), KLT on the session frames
  psi = |C_{i+1} - C_i| / d_i                         reconstructed angular motion, d_i = median depth of points seen by i
  r   = psi / phi                                      dimensionless; roughly constant inside one consistent model
Per 5 s window (stepped 2.5 s): median r vs the model median, new_over_old depth ratio, positive-depth share,
triangulation-angle p50. A window is LOW_CONFIDENCE when median r or new_over_old is outside [1/3, 3] (r relative to
the model median), positive-depth share < 0.99, or angle p50 < 1.0 deg. Thresholds frozen in the plan.

python -m mapping.diagnose <session_dir> [--window 5 --step 2.5] [--model 0]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import pycolmap

R_BAND = 3.0
POS_DEPTH_MIN = 0.99
ANGLE_P50_MIN = 1.0


def _klt(a: np.ndarray, b: np.ndarray, n: int = 600):
    ga, gb = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
    p0 = cv2.goodFeaturesToTrack(ga, n, 0.01, 7)
    if p0 is None:
        return None, None
    p1, st, _ = cv2.calcOpticalFlowPyrLK(ga, gb, p0, None, winSize=(21, 21), maxLevel=4)
    back, st2, _ = cv2.calcOpticalFlowPyrLK(gb, ga, p1, None, winSize=(21, 21), maxLevel=4)
    ok = (st.ravel() == 1) & (st2.ravel() == 1) & (np.linalg.norm((back - p0).reshape(-1, 2), axis=1) < 1.0)
    return p0.reshape(-1, 2)[ok], p1.reshape(-1, 2)[ok]


def diagnose(sd: Path, rank: int = 0, window: float = 5.0, step: float = 2.5) -> dict:
    report = json.loads((sd / "map_report.json").read_text())
    models = (report.get("reconstruction") or {}).get("models")
    rec = pycolmap.Reconstruction(str(sd / (models[rank]["colmap_dir"] if models else report["reconstruction"]["chosen_model_dir"])))
    frames = json.loads((sd / "frames.json").read_text())
    order = {f["file"]: (i, f.get("timestamp_s") if f.get("timestamp_s") is not None else float(i)) for i, f in enumerate(frames)}
    cam = list(rec.cameras.values())[0]
    f = float(cam.params[0])
    cx, cy = float(cam.params[1]), float(cam.params[2])
    K = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1.0]])
    Kinv = np.linalg.inv(K)
    ims = sorted((im for im in rec.images.values() if im.has_pose), key=lambda im: order[im.name][0])
    cfw = {im.image_id: (im.cam_from_world() if callable(im.cam_from_world) else im.cam_from_world) for im in ims}
    centers = {im.image_id: np.asarray(im.projection_center()) for im in ims}

    # per point: triangulation angle, first observing time, depth positivity in every observing camera
    t_of = {im.image_id: order[im.name][1] for im in ims}
    pt_angle, pt_first, pos_ok = {}, {}, {}
    for pid, p in rec.points3D.items():
        obs = [e.image_id for e in p.track.elements if e.image_id in centers]
        rays = np.array([p.xyz - centers[i] for i in obs])
        rays /= np.linalg.norm(rays, axis=1, keepdims=True)
        pt_angle[pid] = float(np.degrees(np.arccos(np.clip(np.min(rays @ rays.T), -1, 1)))) if len(rays) > 1 else 0.0
        pt_first[pid] = min(t_of[i] for i in obs)
        pos_ok[pid] = all((cfw[i] * p.xyz)[2] > 0 for i in obs)

    pairs = []
    prev_img = None
    for a, b in zip(ims, ims[1:]):
        ia = prev_img if prev_img is not None else cv2.imread(str(sd / "frames" / a.name))
        ib = cv2.imread(str(sd / "frames" / b.name))
        prev_img = ib
        x0, x1 = _klt(ia, ib)
        Ra, Rb = np.asarray(cfw[a.image_id].rotation.matrix()), np.asarray(cfw[b.image_id].rotation.matrix())
        H = K @ (Rb @ Ra.T) @ Kinv
        row = {"t": t_of[a.image_id], "t_next": t_of[b.image_id]}
        if x0 is not None and len(x0) >= 20:
            w = cv2.perspectiveTransform(x0.reshape(-1, 1, 2).astype(np.float64), H).reshape(-1, 2)
            phi = float(np.median(np.linalg.norm(x1 - w, axis=1))) / f
            depths = [float((cfw[a.image_id] * rec.points3D[p2.point3D_id].xyz)[2]) for p2 in a.points2D if p2.has_point3D()]
            d = float(np.median(depths)) if depths else None
            psi = float(np.linalg.norm(centers[b.image_id] - centers[a.image_id])) / d if d else None
            row.update(phi=phi, psi=psi, r=(psi / phi) if psi is not None and phi > 1e-5 else None, n_flow=int(len(x0)))
        pairs.append(row)
    rs = np.array([p["r"] for p in pairs if p.get("r")])
    r_model = float(np.median(rs)) if len(rs) else None

    t0, t1 = t_of[ims[0].image_id], t_of[ims[-1].image_id]
    wins = []
    s = t0
    while s < t1 or not wins:
        e = s + window
        inw = [p for p in pairs if s <= p["t"] < e and p.get("r")]
        pids = {p2.point3D_id for im in ims if s <= t_of[im.image_id] < e for p2 in im.points2D if p2.has_point3D()}
        new = [pid for pid in pids if pid in pt_first and pt_first[pid] >= s]
        old = [pid for pid in pids if pid in pt_first and pt_first[pid] < s]

        def mdepth(ids):
            ds = []
            for im in ims:
                if s <= t_of[im.image_id] < e:
                    for p2 in im.points2D:
                        if p2.has_point3D() and p2.point3D_id in ids:
                            ds.append(float((cfw[im.image_id] * rec.points3D[p2.point3D_id].xyz)[2]))
            return float(np.median(ds)) if ds else None
        newset, oldset = set(new), set(old)
        dn, do = (mdepth(newset), mdepth(oldset)) if new and old else (None, None)
        w = {"t_range": [round(s, 2), round(e, 2)], "pairs": len(inw),
             "r_median_rel": round(float(np.median([p["r"] for p in inw])) / r_model, 3) if inw and r_model else None,
             "new_over_old": round(dn / do, 3) if dn and do else None,
             "positive_depth_share": round(float(np.mean([pos_ok[p] for p in pids])), 4) if pids else None,
             "angle_p50": round(float(np.median([pt_angle[p] for p in pids])), 3) if pids else None}
        flags = []
        if w["r_median_rel"] is not None and not (1 / R_BAND <= w["r_median_rel"] <= R_BAND):
            flags.append("r_out_of_band")
        if w["new_over_old"] is not None and not (1 / R_BAND <= w["new_over_old"] <= R_BAND):
            flags.append("new_over_old_out_of_band")
        if w["positive_depth_share"] is not None and w["positive_depth_share"] < POS_DEPTH_MIN:
            flags.append("negative_depth")
        if w["angle_p50"] is not None and w["angle_p50"] < ANGLE_P50_MIN:
            flags.append("small_triangulation_angle")
        w["flags"] = flags
        w["status"] = "LOW_CONFIDENCE" if flags else "OK"
        wins.append(w)
        s += step
    return {"session": sd.name, "model_rank": rank, "window_s": window, "step_s": step, "r_model_median": r_model,
            "thresholds": {"r_band_x_model_median": [1 / R_BAND, R_BAND], "new_over_old_band": [1 / R_BAND, R_BAND],
                           "positive_depth_min": POS_DEPTH_MIN, "angle_p50_min_deg": ANGLE_P50_MIN},
            "windows": wins, "low_confidence_windows": [w["t_range"] for w in wins if w["flags"]], "pairs": pairs}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.diagnose")
    ap.add_argument("session_dir")
    ap.add_argument("--model", type=int, default=0)
    ap.add_argument("--window", type=float, default=5.0)
    ap.add_argument("--step", type=float, default=2.5)
    a = ap.parse_args(argv)
    sd = Path(a.session_dir)
    d = diagnose(sd, a.model, a.window, a.step)
    (sd / "diagnosis_segments.json").write_text(json.dumps(d, indent=1))
    cov = sd / "coverage.json"
    if cov.exists():  # expose the flags to the UI timeline
        c = json.loads(cov.read_text())
        c["low_confidence_windows"] = d["low_confidence_windows"]
        cov.write_text(json.dumps(c, indent=1))
    print(json.dumps({"session": d["session"], "r_model_median": d["r_model_median"],
                      "low_confidence_windows": d["low_confidence_windows"],
                      "windows": [{k: w[k] for k in ("t_range", "r_median_rel", "new_over_old", "positive_depth_share", "angle_p50", "flags")} for w in d["windows"]]}, indent=0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
