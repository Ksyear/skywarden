"""Hybrid static map: dense learned depth near the camera, multi-view (plane-sweep) geometry for far structures.

Outdoors the learned model is very good close to the camera (floor, paths, nearby objects) but places far trees,
bridges and buildings too close and with low confidence. So:
  near  - for each frame, every confident (>= 1.5) learned pixel, sky/person removed, up to that frame's trust depth
          (the far edge of the contiguous distance bands where >= 80 % of triangulated points agree within 10 %);
          each frame is taken from the chunk whose centre is closest; depth in our SfM units (per-chunk Sim3).
  far   - plane-sweep (MVS) points farther from every camera than the median trust depth, after far cleanup, with
          isolated points (fewer than 4 neighbours within 2 voxels) removed - the radial dust seen from above.
Our camera path is unchanged. Learned points are NOT multi-view verified here (as in the demo); the layer says so.

python -m learned.hybrid_map <session> --chunks <aligned dirs...> --masks <dir> --mvs <cloud.ply> --out <dir>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import pycolmap
from scipy.spatial import cKDTree

from mapping.dense_cpu import _pose
from mapping.export import PLY_DTYPE, read_ply, write_ply

CONF = 1.5
BAND_MIN = 0.8
STRIDE = 2


def frame_scale(depth, K, R, t, pts3d, W, H):
    """Per-frame scale check: triangulated / learned depth for the nearest 40 % of the frame's points (>= 30 points).
    A chunk's Sim3 scale comes from a few seconds of camera path and can be off on turning stretches."""
    X = np.asarray(pts3d)
    if not len(X):
        return 1.0, 0
    Xc = (R @ X.T).T + t; z = Xc[:, 2]
    u = np.round(K[0, 0] * Xc[:, 0] / z + K[0, 2]).astype(int); v = np.round(K[1, 1] * Xc[:, 1] / z + K[1, 2]).astype(int)
    ins = (z > 0) & (u >= 0) & (u < W) & (v >= 0) & (v < H)
    zi = z[ins]
    if len(zi) < 30:
        return 1.0, int(len(zi))
    near = zi <= np.percentile(zi, 40)
    d = depth[v[ins][near], u[ins][near]]
    ok = d > 0
    return (float(np.median(zi[near][ok] / d[ok])) if ok.sum() >= 30 else 1.0), int(ok.sum())


def trust_depth(depth, K, R, t, pts3d, W, H):
    X = np.asarray(pts3d)
    if not len(X):
        return 0.0
    Xc = (R @ X.T).T + t; z = Xc[:, 2]
    u = np.round(K[0, 0] * Xc[:, 0] / z + K[0, 2]).astype(int); v = np.round(K[1, 1] * Xc[:, 1] / z + K[1, 2]).astype(int)
    ins = (z > 0) & (u >= 0) & (u < W) & (v >= 0) & (v < H)
    if ins.sum() < 50:
        return 0.0
    zi = z[ins]; ok = np.abs(depth[v[ins], u[ins]] - zi) / zi <= 0.10
    edges = np.quantile(zi, np.linspace(0, 1, 6)); tz = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (zi >= lo) & (zi <= hi)
        if not m.sum() or ok[m].mean() < BAND_MIN:
            break
        tz = float(hi)
    return tz


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="learned.hybrid_map")
    ap.add_argument("session"); ap.add_argument("--chunks", nargs="+", required=True)
    ap.add_argument("--masks"); ap.add_argument("--mvs", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--model-first", type=float, default=0.0, help="use the model for every frame up to this camera distance (no per-frame trust check), as in the demo")
    a = ap.parse_args(argv)
    sd, out = Path(a.session), Path(a.out)
    rec = pycolmap.Reconstruction(str(sd / "sparse" / "0"))
    by = {im.name: im for im in rec.images.values()}
    t_of = {f["file"]: f["timestamp_s"] for f in json.loads((sd / "frames.json").read_text())}
    pts = {pid: p.xyz for pid, p in rec.points3D.items()}
    owner = {}
    for d in map(Path, a.chunks):
        names = [p.stem[len("depth_"):] + ".jpg" for p in d.glob("depth_*.npy")]
        if not names:
            continue
        ts = [t_of[n] for n in names]; mid = 0.5 * (min(ts) + max(ts))
        for n in names:
            if n not in owner or abs(t_of[n] - mid) < owner[n][1]:
                owner[n] = (d, abs(t_of[n] - mid))
    intr = json.loads((next(iter(owner.values()))[0] / "run_report.json").read_text())["intrinsics"]
    K = np.array([[intr["fx"], 0, intr["cx"]], [0, intr["fy"], intr["cy"]], [0, 0, 1.0]]); W, H = int(intr["width"]), int(intr["height"])
    vv, uu = np.mgrid[0:H:STRIDE, 0:W:STRIDE]
    near_p, near_c, trusts, rows = [], [], [], []
    for n in sorted(owner, key=lambda x: t_of[x]):
        d = owner[n][0]; st = n[:-4]
        depth = np.load(d / f"depth_{st}.npy"); conf = np.load(d / f"conf_{st}.npy")
        R, t = _pose(by[n])
        ids = [p2.point3D_id for p2 in by[n].points2D if p2.has_point3D()]
        k, nk = frame_scale(depth, K, R, t, [pts[i] for i in ids], W, H)
        depth = depth * k
        tz = trust_depth(depth, K, R, t, [pts[i] for i in ids], W, H)
        trusts.append(tz)
        if a.model_first > 0:
            tz = a.model_first
        if tz <= 0:
            rows.append({"frame": n, "trust_depth": 0.0, "near_points": 0, "scale_fix": round(k, 4)}); continue
        keep = np.ones((H, W), bool)
        if a.masks and (Path(a.masks) / (n + ".png")).exists():
            keep = cv2.resize(cv2.imread(str(Path(a.masks) / (n + ".png")), 0), (W, H), interpolation=cv2.INTER_NEAREST) > 0
        z = depth[vv, uu]
        m = keep[vv, uu] & (conf[vv, uu] >= CONF) & np.isfinite(z) & (z > 0) & (z <= tz)
        u_, v_, z_ = uu[m], vv[m], z[m]
        Xc = (np.linalg.inv(K) @ np.stack([u_, v_, np.ones_like(u_)]).astype(float)) * z_
        near_p.append((R.T @ (Xc - t[:, None])).T)
        img = cv2.resize(cv2.imread(str(sd / "frames" / n)), (W, H), interpolation=cv2.INTER_AREA)
        near_c.append(img[v_, u_][:, ::-1])
        rows.append({"frame": n, "chunk": d.name, "trust_depth": round(tz, 3), "near_points": int(m.sum()), "scale_fix": round(k, 4), "scale_points": nk})
    Pn = np.concatenate(near_p); Cn = np.concatenate(near_c)
    M = read_ply(a.mvs); Xm = np.stack([M["x"], M["y"], M["z"]], 1).astype(np.float64)
    cams = np.array([-_pose(im)[0].T @ _pose(im)[1] for im in by.values()])
    dist, _ = cKDTree(cams).query(Xm)
    t_near = a.model_first if a.model_first > 0 else float(np.median([x for x in trusts if x > 0]))
    sp_ = np.array(list(pts.values())); nn_, _ = cKDTree(sp_).query(sp_, k=2); vox_ = float(np.median(nn_[:, 1]))
    # near MVS points are replaced only where learned near points actually exist; elsewhere they stay
    covered = np.zeros(len(Xm), bool)
    nearm = dist <= t_near
    if nearm.any():
        dl, _ = cKDTree(Pn).query(Xm[nearm], k=1, distance_upper_bound=3 * vox_)
        covered[np.nonzero(nearm)[0][np.isfinite(dl)]] = True
    far = ~covered
    sp = np.array(list(pts.values())); nn, _ = cKDTree(sp).query(sp, k=2); voxel = float(np.median(nn[:, 1]))
    Xf = Xm[far]
    dd, _ = cKDTree(Xf).query(Xf, k=5, distance_upper_bound=2 * voxel)
    iso = np.isfinite(dd[:, 1:]).sum(1) < 4
    Mf = M[far][~iso]
    near = np.zeros(len(Pn), dtype=PLY_DTYPE)
    near["x"], near["y"], near["z"] = Pn.T; near["red"], near["green"], near["blue"] = Cn.T
    allp = np.concatenate([near, Mf.astype(PLY_DTYPE)])
    out.mkdir(parents=True, exist_ok=True)
    write_ply(out / "dense_verified.ply", allp)
    rep = {"session": sd.name, "model_rank": 0, "label": "HYBRID: learned depth near the camera (all confident pixels up to each frame's trust depth, not multi-view checked) + plane-sweep geometry for far structures (far-cleaned, isolated points removed)",
           "dense_verified_points": int(len(allp)), "near_learned_points": int(len(near)), "far_mvs_points": int(len(Mf)),
           "mvs_kept_before_isolation": int(far.sum()), "mvs_near_replaced_by_learned": int(covered.sum()), "mvs_isolated_removed": int(iso.sum()), "near_far_split_distance": t_near,
           "frames_with_learned_depth": int(sum(1 for r in rows if r["near_points"])), "frames": rows,
           "config": {"work_width": 640}, "views": [], "voxel": voxel}
    (out / "dense_report.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps({k: rep[k] for k in ("dense_verified_points", "near_learned_points", "far_mvs_points", "mvs_isolated_removed", "near_far_split_distance", "frames_with_learned_depth")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
