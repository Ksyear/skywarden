"""Two-point metric scale checked on a real RGB-D recording with metric ground truth (TUM fr1/desk).

The sparse map is built from the RGB frames only (monocular, RELATIVE). Two things come from the dataset and are
never used by the map:
  * reference scale: map camera centres Sim(3)-aligned to the motion-capture trajectory (groundtruth.txt);
  * "tape" lengths: the distance between two map points measured independently, by reading each point's depth in
    the registered Kinect depth image of every frame that sees it, back-projecting (dataset default intrinsics,
    as the dataset recommends for its pre-registered depth) and moving it to the world with the ground-truth pose.

One pair then fixes the scale (TWO_POINT_TAPE fit) and >= 2 other pairs are predicted with it; the status comes from
metric_scale.evaluate (pre-registered 5 % criterion). Repeated trials add the error a person makes in the viewer:
the intended point is snapped to some nearby sparse point, and the tape reading itself has a few mm of error.

  PYTHONPATH=packages .venv/bin/python -m mapping.scale_tum_check <session_dir> <dataset_dir> <out.json> [--write-scale]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from . import metric_scale
from .evaluate import evaluate_session, load_tum_groundtruth, umeyama

DEPTH_FACTOR = 5000.0  # 16-bit PNG: 5000 = 1 m (dataset file format; fr1 depth already corrected by ds = 1.035)
DEPTH_K = (525.0, 525.0, 319.5, 239.5)  # dataset default intrinsics for the pre-registered depth images
MAX_DEPTH_DT_S = 0.02
MIN_VIEWS = 3
MAX_SPREAD_M = 0.015  # a point is a reliable "physical point" if its depth-derived world positions agree this well
MIN_SEP_M = 0.10  # endpoints of different lengths must be at least this far apart (different spots on the desk)
# "Desk" pool: points a person would pick on the desk in front of the camera, i.e. seen often and from close by.
# Fixed after the first run (longest pairs over all reliable points, kept in the evidence as run 1) picked far
# background points; the 5 % criterion itself is unchanged.
DESK_POOL = {"min_track": 6, "min_views": 5, "max_median_depth_m": 1.5}


def _read_list(path: Path) -> tuple[np.ndarray, list[str]]:
    rows = [ln.split() for ln in path.read_text().splitlines() if ln and not ln.startswith("#")]
    return np.array([float(r[0]) for r in rows]), [r[1] for r in rows]


def _quat_to_R(q: np.ndarray) -> np.ndarray:
    x, y, z, w = q / np.linalg.norm(q)
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def gt_pose(gt: np.ndarray, t: float) -> np.ndarray | None:
    """4x4 world-from-camera at time t: linear position, normalised-lerp orientation between the two nearest rows."""
    j = int(np.searchsorted(gt[:, 0], t))
    if j <= 0 or j >= len(gt) or gt[j, 0] - gt[j - 1, 0] > 0.05:
        return None
    a = (t - gt[j - 1, 0]) / (gt[j, 0] - gt[j - 1, 0])
    q0, q1 = gt[j - 1, 4:8], gt[j, 4:8]
    if q0 @ q1 < 0:
        q1 = -q1
    T = np.eye(4)
    T[:3, :3] = _quat_to_R((1 - a) * q0 + a * q1)
    T[:3, 3] = (1 - a) * gt[j - 1, 1:4] + a * gt[j, 1:4]
    return T


def depth_points(session_dir: Path, dataset_dir: Path, min_track: int = 4) -> dict:
    """World position (metres, ground-truth frame) of each sparse point from depth + ground-truth pose."""
    import cv2
    import pycolmap
    sd, dd = Path(session_dir), Path(dataset_dir)
    frames = {f["file"]: f for f in json.loads((sd / "frames.json").read_text())}
    d_t, d_files = _read_list(dd / "depth.txt")
    gt = load_tum_groundtruth(dd / "groundtruth.txt")
    rec = pycolmap.Reconstruction(str(sd / "sparse" / "0"))
    fx, fy, cx, cy = DEPTH_K
    per_image = {}
    for iid, im in rec.images.items():
        t = frames[im.name]["timestamp_s"]
        k = int(np.argmin(np.abs(d_t - t)))
        if abs(d_t[k] - t) > MAX_DEPTH_DT_S:
            continue
        T = gt_pose(gt, d_t[k])
        if T is None:
            continue
        depth = cv2.imread(str(dd / d_files[k]), cv2.IMREAD_UNCHANGED)
        per_image[iid] = (depth, T, float(d_t[k] - t))
    pts = {}
    for pid, p in rec.points3D.items():
        if p.track.length() < min_track:
            continue
        world, zs = [], []
        for el in p.track.elements:
            if el.image_id not in per_image:
                continue
            depth, T, _ = per_image[el.image_id]
            u, v = np.asarray(rec.images[el.image_id].points2D[el.point2D_idx].xy, float)
            ui, vi = int(round(u)), int(round(v))
            if not (1 <= ui < depth.shape[1] - 1 and 1 <= vi < depth.shape[0] - 1):
                continue
            win = depth[vi - 1:vi + 2, ui - 1:ui + 2].astype(float) / DEPTH_FACTOR
            if (win <= 0).any() or win.max() - win.min() > 0.02 * win.mean():
                continue  # missing depth or a depth edge: skip this view
            z = float(np.median(win))
            pc = np.array([(u - cx) * z / fx, (v - cy) * z / fy, z, 1.0])
            world.append((T @ pc)[:3])
            zs.append(z)
        if len(world) < MIN_VIEWS:
            continue
        W = np.array(world)
        med = np.median(W, axis=0)
        spread = float(np.median(np.linalg.norm(W - med, axis=1)))
        pts[pid] = {"map": np.asarray(p.xyz, float), "world_m": med, "n_views": len(world), "spread_m": spread,
                    "track_length": int(p.track.length()), "median_depth_m": float(np.median(zs))}
    dts = [v[2] for v in per_image.values()]
    return {"points": pts, "all_map_xyz": np.array([p.xyz for p in rec.points3D.values()], float),
            "images_with_depth": len(per_image), "images_registered": rec.num_reg_images(),
            "depth_rgb_dt_abs_max_s": round(float(np.max(np.abs(dts))), 4) if dts else None}


def _pairs_greedy(ids: list[int], W: dict, n: int, min_len: float) -> list[tuple[int, int]]:
    """n disjoint pairs, longest first, every endpoint >= MIN_SEP_M from the endpoints already used."""
    used: list[np.ndarray] = []
    out = []
    cand = sorted(((float(np.linalg.norm(W[a] - W[b])), a, b) for i, a in enumerate(ids) for b in ids[i + 1:]), reverse=True)
    for d, a, b in cand:
        if d < min_len:
            break
        if any(np.linalg.norm(W[a] - u) < MIN_SEP_M or np.linalg.norm(W[b] - u) < MIN_SEP_M for u in used):
            continue
        if np.linalg.norm(W[a] - W[b]) < MIN_SEP_M:
            continue
        out.append((a, b))
        used += [W[a], W[b]]
        if len(out) == n:
            break
    return out


def _judge(fit: tuple, checks: list[tuple], map_hash: str) -> dict:
    """Run the module's own judgement on an in-memory scale (fit = (p1, p2, true_m); checks = [(name, p1, p2, true_m)])."""
    s = {"map_hash": map_hash, "observations": [], "verification": {"checks": []}, "cross_checks": []}
    a, b, t = fit
    d = float(np.linalg.norm(np.asarray(b) - np.asarray(a)))
    s["observations"].append({"method": "TWO_POINT_TAPE", "p1": list(a), "p2": list(b), "true_m": t, "map_units": d,
                              "pick_tolerance_units": 0.0, "metres_per_unit": t / d, "note": "fit", "accepted": True})
    for name, p, q, tm in checks:
        s["verification"]["checks"].append({"name": name, "p1": list(p), "p2": list(q), "true_m": tm,
                                            "map_units": float(np.linalg.norm(np.asarray(q) - np.asarray(p))),
                                            "pick_tolerance_units": 0.0})
    return metric_scale.evaluate(s, map_hash)


def noise_trials(pts: dict, ids: list[int], all_map: np.ndarray, gt_scale: float, h: str, n: int, snap_m: float,
                 tape_sd_m: float, min_len: float, seed: int) -> dict:
    """Random fit + 2 check lengths per trial. Each intended point is replaced by the sparse point nearest to a click
    that lands up to `snap_m` away (uniform in a ball; radius converted to map units with the reference scale, used
    for the simulation only), and each tape reading gets N(0, tape_sd_m)."""
    rng = np.random.default_rng(seed)
    W = {i: pts[i]["world_m"] for i in ids}
    M = {i: pts[i]["map"] for i in ids}
    r_units = snap_m / gt_scale
    D = np.array([[np.linalg.norm(W[a] - W[b]) for b in ids] for a in ids])
    res = {"scale_err_pct": [], "max_check_err_pct": [], "check_err_pct": [], "verified": 0, "n": 0, "skipped": 0}

    def pick(i):
        if r_units <= 0:
            return M[i]
        v = rng.normal(size=3)
        click = M[i] + v / np.linalg.norm(v) * r_units * rng.random() ** (1 / 3)
        return all_map[int(np.argmin(np.linalg.norm(all_map - click, axis=1)))]

    for _ in range(n):
        chosen, used = [], []
        for _try in range(200):
            a, b = rng.choice(len(ids), 2, replace=False)
            if D[a, b] < min_len:
                continue
            ia, ib = ids[a], ids[b]
            if any(np.linalg.norm(W[ia] - u) < MIN_SEP_M or np.linalg.norm(W[ib] - u) < MIN_SEP_M for u in used):
                continue
            chosen.append((ia, ib))
            used += [W[ia], W[ib]]
            if len(chosen) == 3:
                break
        if len(chosen) < 3:
            res["skipped"] += 1
            continue
        segs = [(pick(a), pick(b), float(np.linalg.norm(W[a] - W[b]) + rng.normal(0, tape_sd_m))) for a, b in chosen]
        s = _judge(segs[0], [(f"check {k}", *segs[k]) for k in (1, 2)], h)
        errs = [c["error_pct"] for c in s["verification"]["checks"]]
        res["n"] += 1
        res["verified"] += s["status"] == "MEASURED_VERIFIED"
        res["scale_err_pct"].append(100 * abs(s["metres_per_unit"] / gt_scale - 1))
        res["max_check_err_pct"].append(max(errs))
        res["check_err_pct"] += errs

    def dist(v):
        v = np.asarray(v, float)
        return {"median": round(float(np.median(v)), 3), "p90": round(float(np.percentile(v, 90)), 3),
                "max": round(float(v.max()), 3)} if len(v) else None
    return {"snap_radius_m": snap_m, "tape_sd_m": tape_sd_m, "min_true_length_m": min_len, "seed": seed,
            "trials": res["n"], "trials_skipped_no_pairs": res["skipped"],
            "verified_rate": round(res["verified"] / res["n"], 4) if res["n"] else None,
            "scale_error_vs_reference_pct": dist(res["scale_err_pct"]),
            "max_check_error_pct": dist(res["max_check_err_pct"]),
            "each_check_error_pct": dist(res["check_err_pct"])}


def _deterministic(pts: dict, pool: list[int], W: dict, h: str) -> tuple[list[dict], dict]:
    pairs = _pairs_greedy(pool, W, 3, 0.5)
    if len(pairs) < 3:
        raise ValueError(f"only {len(pairs)} disjoint pairs >= 0.5 m in a pool of {len(pool)} points")
    segs = []
    for name, (a, b) in zip(["fit: longest pair", "check A", "check B"], pairs):
        tm = float(np.linalg.norm(W[a] - W[b]))
        segs.append({"name": name, "point3D_ids": [int(a), int(b)], "p1": pts[a]["map"].round(7).tolist(),
                     "p2": pts[b]["map"].round(7).tolist(), "true_m": round(tm, 5),
                     "map_units": round(float(np.linalg.norm(pts[a]["map"] - pts[b]["map"])), 7),
                     "endpoint_depth_spread_m": [round(pts[a]["spread_m"], 4), round(pts[b]["spread_m"], 4)],
                     "endpoint_views": [pts[a]["n_views"], pts[b]["n_views"]],
                     "endpoint_median_depth_m": [round(pts[a]["median_depth_m"], 3), round(pts[b]["median_depth_m"], 3)]})
    judged = _judge((segs[0]["p1"], segs[0]["p2"], segs[0]["true_m"]),
                    [(g["name"], g["p1"], g["p2"], g["true_m"]) for g in segs[1:]], h)
    return segs, judged


def _det_record(label: str, pool: list[int], segs: list[dict], judged: dict, s_ref: float) -> dict:
    return {"label": label, "pool_size": len(pool), "segments": segs, "metres_per_unit": judged["metres_per_unit"],
            "scale_error_vs_trajectory_reference_pct": round(100 * (judged["metres_per_unit"] / s_ref - 1), 3),
            "checks": [{k: c.get(k) for k in ("name", "true_m", "predicted_m", "error_pct", "pass")}
                       for c in judged["verification"]["checks"]],
            "criterion": metric_scale.CRITERION, "status": judged["status"], "status_reason": judged["status_reason"],
            "result": "PASS" if judged["status"] == "MEASURED_VERIFIED" else "FAIL"}


def run(session_dir: Path, dataset_dir: Path, write_scale: bool, command: str) -> dict:
    sd, dd = Path(session_dir), Path(dataset_dir)
    ref = evaluate_session(sd, dd / "groundtruth.txt")
    s_ref = float(ref["sim3_scale"])
    dp = depth_points(sd, dd)
    pts = dp["points"]
    ids = sorted(i for i, p in pts.items() if p["spread_m"] <= MAX_SPREAD_M)
    # second, trajectory-free reference: map points Sim(3)-aligned to their depth-derived world positions
    src = np.array([pts[i]["map"] for i in ids])
    dst = np.array([pts[i]["world_m"] for i in ids])
    s_pts, R, t = umeyama(src, dst)
    resid = np.linalg.norm((s_pts * (R @ src.T)).T + t - dst, axis=1)
    h = metric_scale.map_hash(sd)

    W = {i: pts[i]["world_m"] for i in ids}
    desk = [i for i in ids if pts[i]["track_length"] >= DESK_POOL["min_track"] and pts[i]["n_views"] >= DESK_POOL["min_views"]
            and pts[i]["median_depth_m"] <= DESK_POOL["max_median_depth_m"]]
    runs = [_det_record(label, pool, *_deterministic(pts, pool, W, h), s_ref)
            for label, pool in (("run 1: all reliable points", ids), ("run 2: desk pool", desk))]
    if write_scale:  # scale.json gets the desk-pool run (what a person would pick); run 1 stays in the evidence
        if (sd / "scale.json").exists():
            (sd / "scale.json").unlink()
        segs = runs[1]["segments"]
        metric_scale.add_two_point(sd, segs[0]["p1"], segs[0]["p2"], segs[0]["true_m"], "fit", note=segs[0]["name"], command=command)
        for g in segs[1:]:
            stored = metric_scale.add_two_point(sd, g["p1"], g["p2"], g["true_m"], "verify", note=g["name"], command=command)
        stored["notes"] = ["TWO_POINT_TAPE lengths here are not tape readings: each is the distance between two sparse points "
                           "measured from the dataset's registered depth and motion-capture poses (TUM fr1/desk)",
                           "test session, archived; not shown in the app"]
        metric_scale.write_scale(sd, stored)
        runs[1]["scale_json"] = str(sd / "scale.json")
        runs[1]["scale_json_status"] = stored["status"]

    cases = [(0.0, 0.5), (0.02, 0.5), (0.05, 0.5), (0.02, 1.0), (0.05, 1.0), (0.10, 1.0)]
    trials = {name: [noise_trials(pts, pool, dp["all_map_xyz"], s_ref, h, 2000, snap, 0.003, min_len, seed=k)
                     for k, (snap, min_len) in enumerate(cases)] for name, pool in (("all_reliable", ids), ("desk_pool", desk))}
    span = dst.max(0) - dst.min(0)
    return {
        "test": "two-point metric scale on a real recording with metric ground truth",
        "data": {"kind": "REAL_VIDEO (public dataset recording, monocular RGB used for the map)",
                 "data_origin_label": "PUBLIC_DATASET",
                 "dataset": "TUM RGB-D fr1/desk (CC BY 4.0), data/public/tum_fr1_desk",
                 "ground_truth": "motion-capture trajectory (groundtruth.txt) and registered Kinect depth (depth/*.png); "
                                 "neither is used to build the map",
                 "session": str(sd), "map_hash": h},
        "reference_scale": {"sim3_trajectory_metres_per_unit": s_ref, "sim3_trajectory": ref,
                            "sim3_points_vs_depth_metres_per_unit": round(float(s_pts), 6),
                            "sim3_points_residual_m": {"median": round(float(np.median(resid)), 4),
                                                       "p90": round(float(np.percentile(resid, 90)), 4)},
                            "points_vs_trajectory_scale_diff_pct": round(100 * (s_pts / s_ref - 1), 3)},
        "depth_points": {"images_registered": dp["images_registered"], "images_with_depth_and_gt": dp["images_with_depth"],
                         "depth_rgb_dt_abs_max_s": dp["depth_rgb_dt_abs_max_s"], "points_with_depth": len(pts),
                         "reliable_points": len(ids), "reliable_rule": f">= {MIN_VIEWS} views with valid depth, median "
                         f"spread of the per-view world positions <= {MAX_SPREAD_M} m, track length >= 4",
                         "desk_pool_points": len(desk), "desk_pool_rule": DESK_POOL,
                         "reliable_extent_m": span.round(3).tolist(),
                         "intrinsics_for_depth": DEPTH_K, "depth_factor": DEPTH_FACTOR},
        "deterministic": {"pair_rule": "greedy longest disjoint pairs in the pool, true length >= 0.5 m, "
                                       f"endpoints >= {MIN_SEP_M} m apart; first = fit, next two = checks",
                          "runs": runs},
        "noise_trials": {"what": "simulated viewer picking: snap to the sparse point nearest a click within the radius, "
                                 "tape reading N(0, 3 mm); random fit + 2 checks per trial in each pool",
                         "runs": trials},
        "limits": ["true lengths come from Kinect depth (a few mm noise, depth edges skipped) and mocap poses, not a tape",
                   "depth is read at the RGB pixel of the nearest depth frame (|dt| <= 20 ms); camera motion in that "
                   "time adds mm-level error", "desk scene is small: few pairs reach 1 m",
                   "the map stays NOT_NAVIGATION_SAFE"],
        "tools": metric_scale._tools(), "command": command,
        "created_at": metric_scale._now(),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.scale_tum_check")
    ap.add_argument("session_dir")
    ap.add_argument("dataset_dir")
    ap.add_argument("out")
    ap.add_argument("--write-scale", action="store_true", help="also write the deterministic fit/checks to <session>/scale.json")
    a = ap.parse_args(argv)
    res = run(Path(a.session_dir), Path(a.dataset_dir), a.write_scale, " ".join(["python -m mapping.scale_tum_check", *sys.argv[1:]]))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1, ensure_ascii=False))
    print(json.dumps({"reference": res["reference_scale"]["sim3_trajectory_metres_per_unit"],
                      "points_ref": res["reference_scale"]["sim3_points_vs_depth_metres_per_unit"],
                      "pools": [res["depth_points"]["reliable_points"], res["depth_points"]["desk_pool_points"]],
                      "deterministic": [{k: r[k] for k in ("label", "metres_per_unit", "scale_error_vs_trajectory_reference_pct",
                                                           "checks", "status")} for r in res["deterministic"]["runs"]],
                      "trials": {name: [{k: r[k] for k in ("snap_radius_m", "min_true_length_m", "trials", "verified_rate",
                                                           "scale_error_vs_reference_pct", "max_check_error_pct")} for r in runs]
                                 for name, runs in res["noise_trials"]["runs"].items()}},
                     indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
