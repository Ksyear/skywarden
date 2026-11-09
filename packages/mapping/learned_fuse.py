"""Verify and fuse learned per-frame depth (feed-forward multi-view model) on our FIXED SfM poses.

Learned depth fills textureless floors and far structures that plane-sweep matching cannot, but it is a model's guess.
A learned point enters the map layer only when the geometry of OTHER frames supports it:
  - it agrees (|dz| <= REL_TOL) with the learned depth of at least MIN_AGREE other frames that see it, and
  - no frame with a confident plane-sweep (MVS) depth looks past it (free space), and
  - the frame's learned depth matches our triangulated sparse points (per-frame sparse agreement >= SPARSE_MIN).
Everything else is written to a separate MODEL_ESTIMATED cloud and is never mixed into the verified map.
Sky and person pixels (keep masks) are excluded before anything else.

python -m mapping.learned_fuse <session_dir> <learned_dir> --masks <keep-mask dir> --mvs <dense run dir with diag/> --out <dir>
learned_dir: depth_<frame>.npy (z-depth, our SfM units), run_report.json with "intrinsics" {fx, fy, cx, cy, width, height}
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np
import pycolmap

from .dense_cpu import _pose
from .export import PLY_DTYPE, write_ply

REL_TOL = 0.05
MIN_AGREE = 2
SPARSE_MIN = 0.8   # within a distance band, share of triangulated points the learned depth must match (10 %)
STRIDE = 2  # unproject every 2nd pixel of the learned map
CONF_MIN = 1.5  # model confidence floor (the upstream demo's default display threshold)


def _K(intr):
    return np.array([[intr["fx"], 0, intr["cx"]], [0, intr["fy"], intr["cy"]], [0, 0, 1.0]])


def run(sd: Path, ld: Path, masks: Path | None, mvs: Path | None, out: Path, voxel_scale: float = 0.25) -> dict:
    t0 = time.monotonic()
    rep = json.loads((ld / "run_report.json").read_text())
    intr = rep["intrinsics"]
    K = _K(intr)
    W, H = int(intr["width"]), int(intr["height"])
    rec = pycolmap.Reconstruction(str(sd / "sparse" / "0"))
    by = {im.name: im for im in rec.images.values()}
    names = sorted(p.stem[len("depth_"):] for p in ld.glob("depth_*.npy"))
    names = [n if n.endswith(".jpg") else n + ".jpg" for n in names]
    D, P, keep = {}, {}, {}
    for n in names:
        stem = n[:-4]
        d = np.load(ld / f"depth_{stem}.npy").astype(np.float32)
        if d.shape != (H, W):
            d = cv2.resize(d, (W, H), interpolation=cv2.INTER_NEAREST)
        D[n] = d
        P[n] = _pose(by[n])
        k = np.ones((H, W), bool)
        if masks is not None and (masks / (n + ".png")).exists():
            k = cv2.resize(cv2.imread(str(masks / (n + ".png")), 0), (W, H), interpolation=cv2.INTER_NEAREST) > 0
        cf = ld / f"conf_{stem}.npy"
        if cf.exists():
            c = np.load(cf).astype(np.float32)
            if c.shape != (H, W):
                c = cv2.resize(c, (W, H), interpolation=cv2.INTER_NEAREST)
            k = k & (c >= CONF_MIN)
        keep[n] = k
    # per-frame agreement with triangulated sparse points (scale/shape sanity of the learned map)
    pts = {pid: p.xyz for pid, p in rec.points3D.items()}
    sparse_ok, z_trust, bands = {}, {}, {}
    for n in names:
        im = by[n]
        R, t = P[n]
        ids = [p2.point3D_id for p2 in im.points2D if p2.has_point3D()]
        X = np.array([pts[i] for i in ids]) if ids else np.zeros((0, 3))
        Xc = (R @ X.T).T + t
        z = Xc[:, 2]
        uv = (K @ (Xc / np.maximum(z[:, None], 1e-9)).T).T[:, :2]
        u, v = np.round(uv[:, 0]).astype(int), np.round(uv[:, 1]).astype(int)
        ins = (z > 0) & (u >= 0) & (u < W) & (v >= 0) & (v < H)
        dl = D[n][v[ins], u[ins]]
        zi = z[ins]
        ok_i = np.abs(dl - zi) / zi <= 0.10
        sparse_ok[n] = float(np.mean(ok_i)) if ins.sum() >= 20 else 0.0
        # trust range: walk distance bands (sparse-depth quintiles) from near to far while the learned depth matches the
        # triangulated points; learned depth beyond the last matching band is never used in this frame
        z_trust[n], bands[n] = 0.0, []
        if ins.sum() >= 50:
            edges = np.quantile(zi, np.linspace(0, 1, 6))
            for lo, hi in zip(edges[:-1], edges[1:]):
                m = (zi >= lo) & (zi <= hi)
                share = float(ok_i[m].mean()) if m.sum() else 0.0
                bands[n].append([round(float(lo), 3), round(float(hi), 3), round(share, 3)])
                if share < SPARSE_MIN:
                    break
                z_trust[n] = float(hi)
    mvs_maps = {}
    if mvs is not None and (mvs / "diag" / "depth_maps.npz").exists():
        Z = np.load(mvs / "diag" / "depth_maps.npz")
        mrep = json.loads((mvs / "dense_report.json").read_text())
        inames = mrep["diagnostics"]["image_names"]
        cam = rec.cameras[next(iter(rec.images.values())).camera_id]
        sc = mrep["config"]["work_width"] / cam.width
        Km = np.asarray(cam.calibration_matrix(), float).copy(); Km[:2] *= sc
        for r in Z["refs"]:
            mvs_maps[inames[int(r)]] = (Z[f"d{int(r)}"].astype(np.float32), Km)
    ver_pts, ver_col, est_pts, est_col = [], [], [], []
    rows = []
    vv, uu = np.mgrid[0:H:STRIDE, 0:W:STRIDE]
    for n in names:
        if z_trust[n] <= 0:
            rows.append({"frame": n, "sparse_agreement": round(sparse_ok[n], 3), "bands": bands[n], "status": "REJECTED_SPARSE"})
            continue
        d = D[n][vv, uu]
        m = keep[n][vv, uu] & np.isfinite(d) & (d > 0) & (d <= z_trust[n])
        u_, v_, z_ = uu[m], vv[m], d[m]
        R, t = P[n]
        Xc = (np.linalg.inv(K) @ np.stack([u_, v_, np.ones_like(u_)]).astype(float)) * z_
        Xw = (R.T @ (Xc - t[:, None])).T
        img = cv2.resize(cv2.imread(str(sd / "frames" / n)), (W, H), interpolation=cv2.INTER_AREA)
        col = img[v_, u_][:, ::-1]
        agree = np.zeros(len(Xw), np.int16); contra = np.zeros(len(Xw), bool)
        for o in names:
            if o == n or z_trust[o] <= 0:
                continue
            Ro, to = P[o]
            Xo = (Ro @ Xw.T).T + to
            zo = Xo[:, 2]
            uv = (K @ (Xo / np.maximum(zo[:, None], 1e-9)).T).T[:, :2]
            uo, vo = np.round(uv[:, 0]).astype(int), np.round(uv[:, 1]).astype(int)
            ins = (zo > 0) & (uo >= 0) & (uo < W) & (vo >= 0) & (vo < H)
            dl = np.full(len(Xw), np.nan); dl[ins] = D[o][vo[ins], uo[ins]]
            ko = np.zeros(len(Xw), bool); ko[ins] = keep[o][vo[ins], uo[ins]] & (dl[ins] <= z_trust[o])
            agree += (ko & (np.abs(dl - zo) / np.maximum(zo, 1e-9) <= REL_TOL)).astype(np.int16)
            if o in mvs_maps:
                dm, Km = mvs_maps[o]
                uvm = (Km @ (Xo / np.maximum(zo[:, None], 1e-9)).T).T[:, :2]
                um, vm = np.round(uvm[:, 0]).astype(int), np.round(uvm[:, 1]).astype(int)
                hm, wm = dm.shape
                im_ = (zo > 0) & (um >= 0) & (um < wm) & (vm >= 0) & (vm < hm)
                dd = np.full(len(Xw), np.nan); dd[im_] = dm[vm[im_], um[im_]]
                contra |= ~np.isnan(dd) & (dd > 1.10 * zo)  # a measured surface lies behind: free space
        ok = (agree >= MIN_AGREE) & ~contra
        ver_pts.append(Xw[ok]); ver_col.append(col[ok]); est_pts.append(Xw[~ok]); est_col.append(col[~ok])
        rows.append({"frame": n, "sparse_agreement": round(sparse_ok[n], 3), "trust_depth": round(z_trust[n], 3), "bands": bands[n], "status": "USED", "pixels": int(m.sum()),
                     "verified": int(ok.sum()), "contradicted_by_mvs": int(contra.sum()), "estimated_only": int((~ok).sum())})
    sparse = np.array(list(pts.values()))
    from scipy.spatial import cKDTree
    nn, _ = cKDTree(sparse).query(sparse, k=2)
    voxel = float(np.median(nn[:, 1])) * voxel_scale
    out.mkdir(parents=True, exist_ok=True)

    def fuse(Pl, Cl, path):
        if not Pl or not sum(len(p) for p in Pl):
            write_ply(path, np.zeros(0, dtype=PLY_DTYPE)); return 0
        Pa, Ca = np.concatenate(Pl), np.concatenate(Cl).astype(float)
        key = np.floor(Pa / voxel).astype(np.int64)
        _, inv, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True)
        inv = inv.ravel()
        F = np.zeros((len(cnt), 3)); C = np.zeros((len(cnt), 3))
        np.add.at(F, inv, Pa); np.add.at(C, inv, Ca); F /= cnt[:, None]; C /= cnt[:, None]
        a = np.zeros(len(F), dtype=PLY_DTYPE); a["x"], a["y"], a["z"] = F.T; a["red"], a["green"], a["blue"] = np.clip(C, 0, 255).T
        write_ply(path, a); return int(len(F))
    nv = fuse(ver_pts, ver_col, out / "dense_verified.ply")
    ne = fuse(est_pts, est_col, out / "model_estimated.ply")
    summary = {"session": sd.name, "model_rank": 0, "label": "LEARNED_DEPTH_VERIFIED (relative scale; learned depth kept only where other frames' geometry agrees and no measured surface lies behind it)",
               "learned": {k: rep.get(k) for k in ("model", "weights_url", "licence", "device", "alignment")}, "config": {"work_width": W},
               "rule": {"conf_min": CONF_MIN, "rel_tol": REL_TOL, "min_agree_views": MIN_AGREE, "trust_band_sparse_agreement_min": SPARSE_MIN, "mvs_free_space": bool(mvs_maps), "stride_px": STRIDE},
               "dense_verified_points": nv, "model_estimated_points": ne, "voxel": voxel, "views": rows, "seconds": round(time.monotonic() - t0, 1)}
    (out / "dense_report.json").write_text(json.dumps(summary, indent=1))
    (out / "model_estimated_report.json").write_text(json.dumps({"label": "MODEL_ESTIMATED - not verified by multi-view geometry; never part of the map", "points": ne}, indent=1))
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.learned_fuse")
    ap.add_argument("session_dir"); ap.add_argument("learned_dir")
    ap.add_argument("--masks"); ap.add_argument("--mvs"); ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    s = run(Path(a.session_dir), Path(a.learned_dir), Path(a.masks) if a.masks else None, Path(a.mvs) if a.mvs else None, Path(a.out))
    print(json.dumps({k: s[k] for k in ("dense_verified_points", "model_estimated_points", "seconds")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
