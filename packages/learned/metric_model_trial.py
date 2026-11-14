"""Metric-depth model trial (PLAN_METRIC_SCALE appendix A / S6): prepare frames for a model, check its metre scale
against measured ground truth, and build the same steady layer as `model_on_path` from its depth.

Steps (each a subcommand; the model itself runs in its own environment via `learned/metric_model_run.py`):
  prepare-session <session> --work <dir>      our frames resized to the 518 x 294 grid, our K and camera-to-world poses
  prepare-tum <tum_dir> --work <dir>          undistorted TUM RGB at 518 x 392, ground-truth depth on the same grid,
                                              ground-truth camera-to-world poses (metres)
  job --work <dir> --model M --condition C --weights W --out <run>     writes the job.json the runner reads
  tum-check <run> --work <dir>                metre scale vs ground truth: per-frame median depth ratio, path length ratio
  to-chunk <run> --session <session> --out <dir>   model depth -> our units (one run scale), model mask -> conf 2/0,
                                              written in the chunk format `model_on_path` reads; also per-frame metres
                                              per map unit from our triangulated points (cross-check only, no ground truth)

Nothing here changes a map, a pose or `scale.json`. Model output stays MODEL_ESTIMATED; scale is a cross-check only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

GRID_SESSION = (518, 294)   # same grid as the steady layer's model depth (16:9 frames)
GRID_TUM = (518, 392)       # 4:3, both sides multiples of 14
MISMATCH_PCT = 15.0         # pre-registered flag: model scale differs from the measured one by more than this


# ---------------------------------------------------------------- pure helpers (unit-tested)

def scale_K(K, src_wh, dst_wh):
    """pinhole K for a plain resize from src (w, h) to dst (w, h)"""
    sx, sy = dst_wh[0] / src_wh[0], dst_wh[1] / src_wh[1]
    K = np.array(K, float).copy()
    K[0, :] *= sx; K[1, :] *= sy
    return K


def ratio_stats(r):
    """median and quartiles of positive finite ratios; None when fewer than 10"""
    r = np.asarray(r, float); r = r[np.isfinite(r) & (r > 0)]
    if len(r) < 10:
        return None
    q1, med, q3 = np.percentile(r, [25, 50, 75])
    return {"median": float(med), "p25": float(q1), "p75": float(q3), "n": int(len(r))}


def depth_ratio(pred, gt, mask=None):
    """per-pixel pred / gt where both are valid (gt > 0)"""
    pred, gt = np.asarray(pred, float), np.asarray(gt, float)
    ok = np.isfinite(pred) & (pred > 0) & np.isfinite(gt) & (gt > 0)
    if mask is not None:
        ok &= np.asarray(mask, bool)
    return pred[ok] / gt[ok]


def path_length(C):
    C = np.asarray(C, float)
    return float(np.linalg.norm(np.diff(C, axis=0), axis=1).sum()) if len(C) > 1 else 0.0


def sim3_scale(src, dst):
    """Umeyama similarity scale s with dst ~ s R src + t (s = dst units per src unit)"""
    from mapping.evaluate import umeyama
    return umeyama(np.asarray(src, float), np.asarray(dst, float))[0]


def pct_diff(model, measured):
    return 100.0 * (model / measured - 1.0)


def scale_flag(model, measured, limit=MISMATCH_PCT):
    """pre-registered cross-check flag (not a pass/fail): model scale vs a measured one"""
    d = pct_diff(model, measured)
    return {"diff_pct": round(d, 2), "flag": "MODEL_MISMATCH" if abs(d) > limit else "WITHIN_15PCT"}


def project_points(X, K, R, t, W, H):
    """our triangulated points into a frame: pixel ints and depth, only those inside the image in front"""
    X = np.asarray(X, float)
    Xc = X @ R.T + t; z = Xc[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        u = np.round(K[0, 0] * Xc[:, 0] / z + K[0, 2]); v = np.round(K[1, 1] * Xc[:, 1] / z + K[1, 2])
    ins = (z > 0) & (u >= 0) & (u < W) & (v >= 0) & (v < H)
    return u[ins].astype(int), v[ins].astype(int), z[ins]


def model_per_unit(depth, mask, K, R, t, X, W, H):
    """model depth / our depth at our triangulated points of this frame (= model units per map unit)"""
    u, v, z = project_points(X, K, R, t, W, H)
    d = depth[v, u]; ok = np.isfinite(d) & (d > 0)
    if mask is not None:
        ok &= mask[v, u] > 0
    return d[ok] / z[ok]


def gate(trial: dict, base: dict, classes=("floor", "structure"), fs_limit_pp=1.0):
    """pre-registered replacement check (appendix A): float_fs not worse by more than +1 pp AND floor / structure
    cov1px >= baseline on every eval frame. Inputs are static_eval results. Returns verdict and the failing items."""
    fails = []
    dfs = 100.0 * (trial["errors"]["float_common"]["float_fs"] - base["errors"]["float_common"]["float_fs"])
    if dfs > fs_limit_pp:
        fails.append(f"float_fs +{dfs:.2f} pp")
    rows = {}
    for fn, bf in base["frames"].items():
        tf = trial["frames"].get(fn)
        for c in classes:
            if c not in bf["classes"]:
                continue
            b = bf["classes"][c]["cov1px"]; t = tf["classes"][c]["cov1px"] if tf and c in tf["classes"] else 0.0
            rows[f"{fn}:{c}"] = (b, t)
            if t < b:
                fails.append(f"{fn} {c} cov1px {t:.4f} < {b:.4f}")
    return {"verdict": "REPLACEMENT_CANDIDATE" if not fails else "REFERENCE_ONLY", "fails": fails,
            "float_fs_delta_pp": round(dfs, 2), "cov1px": {k: {"baseline": b, "trial": t} for k, (b, t) in rows.items()}}


def quaternion_to_R(qx, qy, qz, qw):
    q = np.array([qw, qx, qy, qz], float); q /= np.linalg.norm(q); w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def nearest(ts_query, ts_ref, max_dt):
    """index into ts_ref of the nearest stamp per query, -1 when farther than max_dt"""
    ts_ref = np.asarray(ts_ref, float); out = []
    for q in ts_query:
        i = int(np.argmin(np.abs(ts_ref - q))); out.append(i if abs(ts_ref[i] - q) <= max_dt else -1)
    return out


# ---------------------------------------------------------------- preparation

def _our_camera(sd: Path):
    cam = json.loads((sd / "trajectory.json").read_text())["camera"]
    f, cx, cy = cam["params_refined"][:3]
    return np.array([[f, 0, cx], [0, f, cy], [0, 0, 1.0]]), (cam["width"], cam["height"])


def prepare_session(sd: Path, work: Path, grid=GRID_SESSION):
    import pycolmap
    from mapping.dense_cpu import _pose
    rec = pycolmap.Reconstruction(str(sd / "sparse" / "0"))
    K0, wh = _our_camera(sd); K = scale_K(K0, wh, grid)
    (work / "img").mkdir(parents=True, exist_ok=True)
    frames = []
    for im in sorted(rec.images.values(), key=lambda i: i.name):
        R, t = _pose(im); T = np.eye(4); T[:3, :3] = R.T; T[:3, 3] = -R.T @ t
        p = work / "img" / im.name
        if not p.exists():
            cv2.imwrite(str(p), cv2.resize(cv2.imread(str(sd / "frames" / im.name)), grid, interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 95])
        frames.append({"name": im.name, "path": str(p.resolve()), "K": K.tolist(), "c2w": T.tolist()})
    meta = {"kind": "session", "session": str(sd), "grid": list(grid), "K": K.tolist(), "frames": frames,
            "note": "K = our COLMAP SIMPLE_RADIAL f, cx, cy scaled to the grid (k1 ignored, as in the steady layer); c2w = our SfM poses"}
    (work / "frames.json").write_text(json.dumps(meta, indent=1))
    return meta


def prepare_tum(td: Path, work: Path, stride=10, grid=GRID_TUM):
    """TUM fr1 RGB + registered depth (depth/5000 = metres). Both undistorted with the official Freiburg-1 RGB model
    (depth with nearest-neighbour remap), then resized to the grid. Ground-truth poses: nearest mocap sample (<= 20 ms)."""
    seq = next(p for p in td.iterdir() if p.is_dir() and (p / "rgb.txt").exists())
    cam = json.loads((td / "camera.json").read_text()); fx, fy, cx, cy = cam["params"][:4]; dist = np.array(cam["params"][4:9], float)
    K0 = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1.0]]); wh = (cam["width"], cam["height"])
    rd = lambda f: [ln.split() for ln in (seq / f).read_text().splitlines() if ln and not ln.startswith("#")]  # noqa: E731
    rgb, dep, gt = rd("rgb.txt"), rd("depth.txt"), np.array([list(map(float, r)) for r in rd("groundtruth.txt")])
    dts = [float(r[0]) for r in dep]
    m1, m2 = cv2.initUndistortRectifyMap(K0, dist, None, K0, wh, cv2.CV_32FC1)
    K = scale_K(K0, wh, grid)
    for d in ("img", "gt"):
        (work / d).mkdir(parents=True, exist_ok=True)
    frames = []
    for r in rgb[::stride]:
        ts = float(r[0]); di = nearest([ts], dts, 0.02)[0]; gi = nearest([ts], gt[:, 0], 0.02)[0]
        if di < 0 or gi < 0:
            continue
        name = f"t{ts:.4f}.png"
        img = cv2.remap(cv2.imread(str(seq / r[1])), m1, m2, cv2.INTER_LINEAR)
        cv2.imwrite(str(work / "img" / name), cv2.resize(img, grid, interpolation=cv2.INTER_AREA))
        D = cv2.imread(str(seq / dep[di][1]), cv2.IMREAD_UNCHANGED).astype(np.float32) / 5000.0
        D = cv2.remap(D, m1, m2, cv2.INTER_NEAREST)
        np.save(work / "gt" / f"depth_{Path(name).stem}.npy", cv2.resize(D, grid, interpolation=cv2.INTER_NEAREST))
        g = gt[gi]; T = np.eye(4); T[:3, :3] = quaternion_to_R(*g[4:8]); T[:3, 3] = g[1:4]
        frames.append({"name": name, "path": str((work / "img" / name).resolve()), "K": K.tolist(), "c2w": None, "gt_c2w": T.tolist(),
                       "t": ts, "depth_dt": round(float(dts[di]) - ts, 4), "gt_dt": round(float(gt[gi, 0]) - ts, 4)})
    meta = {"kind": "tum", "sequence": str(seq), "grid": list(grid), "K": K.tolist(), "stride": stride, "frames": frames,
            "note": "undistorted with camera.json (FULL_OPENCV k1 k2 p1 p2 k3); ground truth depth = depth.png / 5000 (TUM), registered to RGB"}
    (work / "frames.json").write_text(json.dumps(meta, indent=1))
    return meta


def write_job(work: Path, model: str, condition: str, weights: str, out: str, chunk: int, dtype: str, **extra):
    meta = json.loads((work / "frames.json").read_text())
    fr = [{k: f[k] for k in ("name", "path", "K", "c2w")} for f in meta["frames"]]
    job = {"model": model, "condition": condition, "weights": str(Path(weights).resolve()), "out": str(Path(out).resolve()),
           "grid": meta["grid"], "chunk": chunk, "dtype": dtype, "device": "mps", "frames": fr, **extra}
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / "job.json").write_text(json.dumps(job))
    return Path(out) / "job.json"


# ---------------------------------------------------------------- checks

def tum_check(run: Path, work: Path):
    meta = json.loads((work / "frames.json").read_text()); rep = json.loads((run / "run_report.json").read_text())
    rows, all_r = [], []
    for f in meta["frames"]:
        st = Path(f["name"]).stem
        if not (run / f"depth_{st}.npy").exists():
            continue
        pred = np.load(run / f"depth_{st}.npy"); m = np.load(run / f"mask_{st}.npy") > 0; gt = np.load(work / "gt" / f"depth_{st}.npy")
        r_all = depth_ratio(pred, gt); r_m = depth_ratio(pred, gt, m)
        s = ratio_stats(r_m)
        if s is None:
            continue
        rows.append({"frame": f["name"], "pred_over_gt": s, "pred_over_gt_all_px": ratio_stats(r_all),
                     "abs_rel_unscaled": float(np.median(np.abs(r_m - 1))), "abs_rel_after_frame_median": float(np.median(np.abs(r_m / s["median"] - 1)))})
        all_r.append(s["median"])
    fr = np.array(all_r)
    out = {"run": str(run), "model": rep["model"], "condition": rep["condition"], "frames_checked": len(rows),
           "depth_pred_over_gt_median_of_frames": float(np.median(fr)) if len(fr) else None,
           "depth_pred_over_gt_frame_p25_p75": [float(np.percentile(fr, 25)), float(np.percentile(fr, 75))] if len(fr) else None,
           "depth_scale_error_pct": round(pct_diff(float(np.median(fr)), 1.0), 2) if len(fr) else None,
           "abs_rel_unscaled_median": float(np.median([r["abs_rel_unscaled"] for r in rows])) if rows else None,
           "abs_rel_after_frame_median_scale": float(np.median([r["abs_rel_after_frame_median"] for r in rows])) if rows else None,
           "frames": rows}
    # trajectory scale per chunk: model camera centres vs ground truth centres of the same frames
    poses = rep.get("poses_c2w") or {}
    gt_c = {f["name"]: np.array(f["gt_c2w"])[:3, 3] for f in meta["frames"]}
    ch = {}
    for n, p in poses.items():
        ch.setdefault(p["chunk"], []).append((n, np.array(p["T"])[:3, 3]))
    trows = []
    for c, items in sorted(ch.items()):
        items.sort(key=lambda x: x[0])
        P = np.array([x[1] for x in items]); G = np.array([gt_c[x[0]] for x in items])
        if len(P) < 3 or path_length(G) < 1e-3:
            continue
        try:
            s = sim3_scale(G, P)  # model units per metre
        except ValueError:
            s = None
        trows.append({"chunk": c, "frames": len(P), "gt_path_m": round(path_length(G), 4), "model_path": round(path_length(P), 4),
                      "path_ratio_model_over_gt": round(path_length(P) / path_length(G), 4), "sim3_model_per_gt": None if s is None else round(s, 4)})
    out["trajectory_chunks"] = trows
    if trows:
        pr = [t["path_ratio_model_over_gt"] for t in trows]; sr = [t["sim3_model_per_gt"] for t in trows if t["sim3_model_per_gt"]]
        out["path_ratio_median"] = float(np.median(pr)); out["path_scale_error_pct"] = round(pct_diff(float(np.median(pr)), 1.0), 2)
        out["sim3_scale_median"] = float(np.median(sr)) if sr else None
    out["flag_depth"] = scale_flag(out["depth_pred_over_gt_median_of_frames"], 1.0) if len(fr) else None
    out["flag_path"] = scale_flag(out["path_ratio_median"], 1.0) if trows else None
    return out


def to_chunk(run: Path, sd: Path, out: Path, frames_from: Path | None = None):
    """model depth -> chunk directory for `model_on_path`: depth scaled once to our units (median over frames of the
    per-frame ratio at our triangulated points), conf = 2.0 where the model's own mask keeps the pixel else 0.0."""
    import pycolmap
    from mapping.dense_cpu import _pose
    meta = json.loads((run / "job.json").read_text()); rep = json.loads((run / "run_report.json").read_text())
    W, H = meta["grid"]; K = np.array(meta["frames"][0]["K"])
    rec = pycolmap.Reconstruction(str(sd / "sparse" / "0")); by = {im.name: im for im in rec.images.values()}
    pts = {pid: p.xyz for pid, p in rec.points3D.items()}
    keep = None
    if frames_from:
        keep = {r["frame"] for r in json.loads(Path(frames_from).read_text())["per_frame"]}
    per, names = {}, []
    for f in meta["frames"]:
        n = f["name"]; st = Path(n).stem
        if keep is not None and n not in keep:
            continue
        if not (run / f"depth_{st}.npy").exists():
            continue
        d = np.load(run / f"depth_{st}.npy"); m = np.load(run / f"mask_{st}.npy")
        R, t = _pose(by[n]); X = [pts[p.point3D_id] for p in by[n].points2D if p.has_point3D()]
        s = ratio_stats(model_per_unit(d, m, K, R, t, X, W, H)) if X else None
        per[n] = s; names.append(n)
    meds = [s["median"] for s in per.values() if s]
    if not meds:
        raise SystemExit("no frame overlaps our triangulated points")
    mpu = float(np.median(meds))           # model units per map unit (metres per unit if the model is metric)
    out.mkdir(parents=True, exist_ok=True)
    for n in names:
        st = Path(n).stem
        d = np.load(run / f"depth_{st}.npy") / mpu; m = np.load(run / f"mask_{st}.npy") > 0
        np.save(out / f"depth_{st}.npy", d.astype(np.float32)); np.save(out / f"conf_{st}.npy", np.where(m, 2.0, 0.0).astype(np.float32))
    cross = {"model_units_per_map_unit_median": mpu, "frame_p25_p75": [float(np.percentile(meds, 25)), float(np.percentile(meds, 75))],
             "frames": len(meds), "per_frame": {n: s for n, s in per.items()}}
    (out / "run_report.json").write_text(json.dumps({
        "model": rep["model"], "condition": rep["condition"], "source_run": str(run), "frames": len(names),
        "intrinsics": {"fx": K[0, 0], "fy": K[1, 1], "cx": K[0, 2], "cy": K[1, 2], "width": W, "height": H},
        "scale_to_ours": 1.0 / mpu, "conf_rule": "2.0 where the model's own validity mask keeps the pixel, else 0.0 (model confidences are not comparable across models)",
        "scale_cross_check": cross}, indent=1))
    return cross


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="learned.metric_model_trial"); sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("prepare-session"); p.add_argument("session"); p.add_argument("--work", required=True)
    p = sp.add_parser("prepare-tum"); p.add_argument("tum"); p.add_argument("--work", required=True); p.add_argument("--stride", type=int, default=10)
    p = sp.add_parser("job"); p.add_argument("--work", required=True); p.add_argument("--model", required=True); p.add_argument("--condition", default="none")
    p.add_argument("--weights", required=True); p.add_argument("--out", required=True); p.add_argument("--chunk", type=int, default=16)
    p.add_argument("--dtype", default="fp32"); p.add_argument("--weights-rev"); p.add_argument("--code-commit")
    p = sp.add_parser("tum-check"); p.add_argument("run"); p.add_argument("--work", required=True); p.add_argument("--out")
    p = sp.add_parser("to-chunk"); p.add_argument("run"); p.add_argument("--session", required=True); p.add_argument("--out", required=True); p.add_argument("--frames-from")
    a = ap.parse_args(argv)
    if a.cmd == "prepare-session":
        m = prepare_session(Path(a.session), Path(a.work)); print(len(m["frames"]), "frames")
    elif a.cmd == "prepare-tum":
        m = prepare_tum(Path(a.tum), Path(a.work), a.stride); print(len(m["frames"]), "frames")
    elif a.cmd == "job":
        print(write_job(Path(a.work), a.model, a.condition, a.weights, a.out, a.chunk, a.dtype, weights_rev=a.weights_rev, code_commit=a.code_commit))
    elif a.cmd == "tum-check":
        r = tum_check(Path(a.run), Path(a.work))
        Path(a.out or Path(a.run) / "scale_check.json").write_text(json.dumps(r, indent=1))
        print(json.dumps({k: v for k, v in r.items() if k not in ("frames", "trajectory_chunks")}, indent=1))
    elif a.cmd == "to-chunk":
        c = to_chunk(Path(a.run), Path(a.session), Path(a.out), Path(a.frames_from) if a.frames_from else None)
        print(json.dumps({k: v for k, v in c.items() if k != "per_frame"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
