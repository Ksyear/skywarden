"""Model output placed on OUR camera path: every confident model pixel, but each frame unprojected with our
bundle-adjusted camera pose instead of the model's own camera.

Why: the model's per-frame cameras wobble when the real camera shakes, and every point of that frame wobbles with
them (doubled walls, smeared ground). Our SfM poses are refined over all frames and stay steady.
Per frame:
  - depth from the aligned chunk whose centre is closest (already in our units, one Sim3 per chunk)
  - scale check against our triangulated points in that frame (nearest 40 %): depth *= median(z_sfm / z_model)
  - frame skipped when, after that scale, the model still disagrees with our points (median relative error > --max-err)
    - typically motion-blurred / shaking frames where the depth itself is off
  - every pixel with confidence >= 1.5, sky/person removed when masks are given, every --stride-th pixel
  - --curve: instead of one scale, a per-frame depth curve fitted to our points by distance (the model squeezes far
    depths: on the aerial video the farthest fifth of our points are 1.5-5x farther than the model says, which folds
    the far ground up like a dish); pixels farther than our points reach are dropped instead of extrapolated
  - --consistency N: a pixel is kept only when at least N of the neighbouring frames (+-1, +-2) see the same surface
    there (relative depth difference <= --tol): removes the smeared points of shaky frames pixel by pixel
Without --consistency nothing is checked against other views (as the demo); the layer says which.

python -m learned.model_on_path <session> --chunks <aligned dirs...> [--masks <dir>] --out <dir>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import pycolmap

from learned.hybrid_map import frame_scale
from mapping.dense_cpu import _pose
from mapping.export import PLY_DTYPE, write_ply

CONF = 1.5


def frame_error(depth, K, R, t, X, W, H):
    """median relative disagreement between model depth and our triangulated points (nearest 40 %) in this frame"""
    if not len(X):
        return None, 0
    Xc = (R @ np.asarray(X).T).T + t; z = Xc[:, 2]
    u = np.round(K[0, 0] * Xc[:, 0] / z + K[0, 2]).astype(int); v = np.round(K[1, 1] * Xc[:, 1] / z + K[1, 2]).astype(int)
    ins = (z > 0) & (u >= 0) & (u < W) & (v >= 0) & (v < H)
    if ins.sum() < 30:
        return None, int(ins.sum())
    zi = z[ins]; near = zi <= np.percentile(zi, 40)  # judged where the scale was fitted: the model compresses far depths anyway
    d = depth[v[ins][near], u[ins][near]]; ok = d > 0
    if ok.sum() < 15:
        return None, int(ok.sum())
    return float(np.median(np.abs(d[ok] - zi[near][ok]) / zi[near][ok])), int(ok.sum())


def _isotonic(y, w):
    """pool-adjacent-violators: the closest non-decreasing sequence (weighted least squares)"""
    blocks = [[float(v), float(c), 1] for v, c in zip(y, w)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] > blocks[i + 1][0]:
            a, b = blocks[i], blocks[i + 1]
            blocks[i] = [(a[0] * a[1] + b[0] * b[1]) / (a[1] + b[1]), a[1] + b[1], a[2] + b[2]]; del blocks[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    return np.concatenate([[v] * n for v, _, n in blocks])


def all_error(depth, K, R, t, X, W, H):
    """median relative disagreement over all of this frame's triangulated points"""
    if not len(X):
        return np.inf
    Xc = (R @ np.asarray(X).T).T + t; z = Xc[:, 2]
    u = np.round(K[0, 0] * Xc[:, 0] / z + K[0, 2]).astype(int); v = np.round(K[1, 1] * Xc[:, 1] / z + K[1, 2]).astype(int)
    ins = (z > 0) & (u >= 0) & (u < W) & (v >= 0) & (v < H)
    d = depth[v[ins], u[ins]]; ok = d > 0
    return float(np.median(np.abs(d[ok] - z[ins][ok]) / z[ins][ok])) if ok.sum() >= 30 else np.inf


def depth_curve(depth, K, R, t, X, W, H, bins=8):
    """monotone map model depth -> our depth, fitted on this frame's triangulated points (median per distance bin).
    Returns (f, d_max) with f(depth) and the farthest model depth our points support, or (None, 0) if too few."""
    if not len(X):
        return None, 0.0
    Xc = (R @ np.asarray(X).T).T + t; z = Xc[:, 2]
    u = np.round(K[0, 0] * Xc[:, 0] / z + K[0, 2]).astype(int); v = np.round(K[1, 1] * Xc[:, 1] / z + K[1, 2]).astype(int)
    ins = (z > 0) & (u >= 0) & (u < W) & (v >= 0) & (v < H)
    z = z[ins]; d = depth[v[ins], u[ins]]; ok = (d > 0) & np.isfinite(d); z, d = z[ok], d[ok]
    if len(z) < 30 * bins // 2:
        return None, 0.0
    q = np.quantile(d, np.linspace(0, 1, bins + 1))
    # walk outward from the near scale, one distance band at a time; in each band only points whose ratio stays
    # within x1.6 of the previous band count (river reflections are triangulated far below the water: a second
    # cluster 5-10x farther than the surface the model sees, which a plain median would follow)
    near = z <= np.percentile(z, 40)
    prev = float(np.median(np.log(z[near] / d[near])))
    xs, ys, ws, d_top = [], [], [], 0.0
    for lo_, hi_ in zip(q[:-1], q[1:]):
        m = (d >= lo_) & (d <= hi_)
        lr = np.log(z[m] / d[m]); sel = np.abs(lr - prev) < np.log(1.6)
        if sel.sum() < max(10, 0.2 * m.sum()):
            break
        prev = float(np.median(lr[sel]))
        xs.append(np.log(np.median(d[m][sel]))); ys.append(np.log(np.median(d[m][sel])) + prev); ws.append(float(sel.sum())); d_top = float(hi_)
    if len(xs) < 3:
        return None, 0.0
    xs, ys = np.array(xs), _isotonic(np.array(ys), np.array(ws))  # farther model depth never maps nearer
    slope_lo = 1.0  # below the nearest bin: plain scale of that bin
    def f(D):
        L = np.log(np.maximum(D, 1e-6)); y = np.interp(L, xs, ys)
        y = np.where(L < xs[0], ys[0] + slope_lo * (L - xs[0]), y)
        return np.exp(y)
    return f, d_top


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="learned.model_on_path")
    ap.add_argument("session"); ap.add_argument("--chunks", nargs="+", required=True)
    ap.add_argument("--masks"); ap.add_argument("--out", required=True)
    ap.add_argument("--stride", type=int, default=2); ap.add_argument("--max-err", type=float, default=0.15)
    ap.add_argument("--curve", action="store_true", help="per-frame depth curve by distance instead of one scale")
    ap.add_argument("--consistency", type=int, default=0, help="keep a pixel only if >= N neighbouring frames agree")
    ap.add_argument("--tol", type=float, default=0.05, help="relative depth tolerance for the neighbour check")
    ap.add_argument("--neighbours", default="1,2", help="frame offsets (each side) used by the neighbour check")
    ap.add_argument("--edge", type=float, default=0.0, help="drop pixels whose depth jumps by more than this share to an adjacent pixel (smeared edge points)")
    ap.add_argument("--fill-mvs", help="multi-view cloud: its points whose nearest camera is a skipped frame fill those gaps")
    a = ap.parse_args(argv)
    sd, out = Path(a.session), Path(a.out)
    rec = pycolmap.Reconstruction(str(sd / "sparse" / "0"))
    by = {im.name: im for im in rec.images.values()}
    t_of = {f["file"]: f["timestamp_s"] for f in json.loads((sd / "frames.json").read_text())}
    pts = {pid: p.xyz for pid, p in rec.points3D.items()}
    owner = {}
    for d in map(Path, a.chunks):
        names = [p.stem[len("depth_"):] + ".jpg" for p in d.glob("depth_*.npy")]
        names = [n for n in names if n in by and n in t_of]
        if not names:
            continue
        ts = [t_of[n] for n in names]; mid = 0.5 * (min(ts) + max(ts))
        for n in names:
            if n not in owner or abs(t_of[n] - mid) < owner[n][1]:
                owner[n] = (d, abs(t_of[n] - mid))
    intr = json.loads((next(iter(owner.values()))[0] / "run_report.json").read_text())["intrinsics"]
    K = np.array([[intr["fx"], 0, intr["cx"]], [0, intr["fy"], intr["cy"]], [0, 0, 1.0]]); W, H = int(intr["width"]), int(intr["height"])
    vv, uu = np.mgrid[0:H:a.stride, 0:W:a.stride]
    Kinv = np.linalg.inv(K)
    P, Cc, rows = [], [], []
    frames = []  # (name, R, t, depth, keep-mask) of used frames, for the neighbour check
    for n in sorted(owner, key=lambda x: t_of[x]):
        d = owner[n][0]; st = n[:-4]
        depth = np.load(d / f"depth_{st}.npy"); conf = np.load(d / f"conf_{st}.npy")
        R, t = _pose(by[n])
        X = [pts[p.point3D_id] for p in by[n].points2D if p.has_point3D()]
        k, nk = frame_scale(depth, K, R, t, X, W, H)
        far_cut = np.inf; curve_used = False
        if a.curve:
            f, dmax = depth_curve(depth, K, R, t, X, W, H)
            if f is not None and all_error(f(depth), K, R, t, X, W, H) < all_error(depth * k, K, R, t, X, W, H):
                far_cut = f(np.array([dmax]))[0] * 1.05
                depth = f(depth); curve_used = True
            else:  # one scale fits this frame's points better; still no extrapolation beyond our points
                far_cut = (dmax * k * 1.05) if f is not None else np.inf
                depth = depth * k; curve_used = False
        else:
            depth = depth * k
        err, ne = frame_error(depth, K, R, t, X, W, H)
        row = {"frame": n, "t": t_of[n], "chunk": d.name, "scale_fix": round(k, 4), "err_vs_sfm": None if err is None else round(err, 4), "sfm_points": ne, "curve": curve_used}
        if err is not None and err > a.max_err:
            rows.append(row | {"used": False, "why": f"model depth disagrees with our points by {err:.0%} after scaling"}); continue
        keep = np.ones((H, W), bool)
        if a.masks and (Path(a.masks) / (n + ".png")).exists():
            keep = cv2.resize(cv2.imread(str(Path(a.masks) / (n + ".png")), 0), (W, H), interpolation=cv2.INTER_NEAREST) > 0
        keep &= (conf >= CONF) & np.isfinite(depth) & (depth > 0) & (depth <= far_cut)
        if a.edge:  # depth edges: points there land between foreground and background as streaks along the ray
            lo = cv2.erode(depth.astype(np.float32), np.ones((3, 3), np.uint8)); hi = cv2.dilate(depth.astype(np.float32), np.ones((3, 3), np.uint8))
            keep &= (hi - lo) <= a.edge * depth
        frames.append((n, R, t, depth.astype(np.float32), keep))
        rows.append(row | {"used": True})
    rowd = {r["frame"]: r for r in rows}
    for i, (n, R, t, depth, keep) in enumerate(frames):
        z = depth[vv, uu]; m = keep[vv, uu]
        u_, v_, z_ = uu[m], vv[m], z[m]
        Xc = (Kinv @ np.stack([u_, v_, np.ones_like(u_)]).astype(float)) * z_
        Xw = (R.T @ (Xc - t[:, None])).T
        if a.consistency:
            votes = np.zeros(len(Xw), int)
            offs = [int(o) for o in a.neighbours.split(",")]
            for j in [i - o for o in offs] + [i + o for o in offs]:
                if not 0 <= j < len(frames):
                    continue
                _, Rj, tj, dj, kj = frames[j]
                Y = Xw @ Rj.T + tj; zj = Y[:, 2]
                uj = np.round(K[0, 0] * Y[:, 0] / np.maximum(zj, 1e-9) + K[0, 2]).astype(int)
                vj = np.round(K[1, 1] * Y[:, 1] / np.maximum(zj, 1e-9) + K[1, 2]).astype(int)
                ins = (zj > 0) & (uj >= 0) & (uj < W) & (vj >= 0) & (vj < H)
                ok = np.zeros(len(Xw), bool)
                dd = dj[vj[ins], uj[ins]]
                ok[ins] = kj[vj[ins], uj[ins]] & (np.abs(dd - zj[ins]) <= a.tol * zj[ins])
                votes += ok
            sel = votes >= a.consistency
            Xw, u_, v_ = Xw[sel], u_[sel], v_[sel]
        P.append(Xw)
        img = cv2.resize(cv2.imread(str(sd / "frames" / n)), (W, H), interpolation=cv2.INTER_AREA)
        Cc.append(img[v_, u_][:, ::-1])
        rowd[n]["points"] = int(len(Xw))
    n_fill = 0
    skipped = {r["frame"] for r in rows if not r["used"]}
    if a.fill_mvs and skipped:
        from mapping.export import read_ply
        from scipy.spatial import cKDTree as _T
        M = read_ply(a.fill_mvs); Xm = np.stack([M["x"], M["y"], M["z"]], 1).astype(np.float64)
        names = sorted(by); cams = np.array([-_pose(by[k])[0].T @ _pose(by[k])[1] for k in names])
        _, j = _T(cams).query(Xm)
        f = np.isin(np.array(names)[j], list(skipped))
        P.append(Xm[f]); Cc.append(np.stack([M["red"], M["green"], M["blue"]], 1)[f]); n_fill = int(f.sum())
    Pa = np.concatenate(P); Ca = np.concatenate(Cc)
    arr = np.zeros(len(Pa), dtype=PLY_DTYPE)
    arr["x"], arr["y"], arr["z"] = Pa.T; arr["red"], arr["green"], arr["blue"] = Ca.T
    out.mkdir(parents=True, exist_ok=True)
    write_ply(out / "dense_verified.ply", arr)
    sp = np.array(list(pts.values()))
    from scipy.spatial import cKDTree
    nn, _ = cKDTree(sp).query(sp, k=2)
    used = [r for r in rows if r["used"]]
    rep = {"session": sd.name, "model_rank": 0,
           "label": "MODEL OUTPUT ON OUR CAMERA PATH: confident model pixels, each frame placed with our steady SfM camera (not the model's), "
                    + ("per-frame depth curve fitted to our points by distance, pixels beyond our points dropped, " if a.curve else "per-frame scale checked against our points, ")
                    + "shaky frames whose depth disagrees skipped (multi-view points fill their area when given); "
                    + (f"each pixel kept only where >= {a.consistency} neighbouring frames see the same surface (within {a.tol:.0%})" if a.consistency else "not checked against other views"),
           "dense_verified_points": int(len(arr)), "frames": len(used), "frames_skipped": len(rows) - len(used), "mvs_fill_points": n_fill,
           "max_err": a.max_err, "depth_curve": a.curve, "consistency_min_views": a.consistency, "consistency_tol": a.tol, "neighbours": a.neighbours, "edge": a.edge, "per_frame": rows, "config": {"work_width": 640}, "views": [], "voxel": float(np.median(nn[:, 1]))}
    (out / "dense_report.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps({k: rep[k] for k in ("dense_verified_points", "frames", "frames_skipped")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
