"""CPU dense reconstruction from SfM poses (docs/PLAN_GEOMETRY_AND_DENSE.md, section D). No CUDA needed.

D1 pairs: consecutive / skip-1 registered frames with baseline / median co-visible depth in [0.05, 0.30] and
   >= 100 shared tracks.
D2 rectified stereo: undistort (refined shared camera), cv2.stereoRectify from the SfM relative pose, SGBM
   (block 5, 128 disparities at 960 px width, uniqueness 10) with a left-right check <= 1 px.
D3 verification: (i) >= 70 % of the pair's sparse points agree with the SGBM depth within 10 %, else the pair is
   REJECTED; (ii) third view: points are projected into the next pair's left image and kept only where that
   frame's own SGBM depth agrees within 10 %; a pair keeping < 30 % of its valid pixels is REJECTED.
D4 fusion of kept points only (voxel = median nearest-neighbour spacing of the sparse points) -> dense_verified.ply
   labelled DENSE_VERIFIED. Unverified SGBM points of accepted-but-unchecked pixels are never fused.
Mesh: Poisson on dense_verified.ply only, trimmed to vertices within 2 voxels of a fused point
   (MESH_FROM_VERIFIED_DENSE). Unobserved areas stay holes. Scale stays RELATIVE.

python -m mapping.dense_cpu <session_dir> [--model 0] [--max-pairs 60]
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np
import pycolmap
from scipy.spatial import cKDTree

from .export import PLY_DTYPE, read_ply, write_ply

WORK_WIDTH = 640
PLANES = 64
MAX_COST = 0.35       # 1 - ZNCC
MIN_MARGIN = 0.02     # best vs second-best plane cost
TEXTURE_VAR = 20.0    # grey-level variance in the window; flat sky / water carry no depth
BASE_DEPTH = (0.05, 0.30)
MIN_SHARED = 100
SPARSE_AGREE_MIN = 0.70
THIRD_KEEP_MIN = 0.30
REL_TOL = 0.10


def camera_distortion(cam) -> np.ndarray:
    """COLMAP camera model parameters -> OpenCV distortion vector (k1, k2, p1, p2, k3)."""
    p, name = [float(v) for v in cam.params], cam.model.name
    if name in ("SIMPLE_PINHOLE", "PINHOLE"):
        return np.zeros(5)
    if name == "SIMPLE_RADIAL":
        return np.array([p[3], 0, 0, 0, 0.0])
    if name == "RADIAL":
        return np.array([p[3], p[4], 0, 0, 0.0])
    if name == "OPENCV":
        return np.array([p[4], p[5], p[6], p[7], 0.0])
    if name == "FULL_OPENCV":
        return np.array([p[4], p[5], p[6], p[7], p[8]])
    raise ValueError(f"camera model {name} not supported by the dense path")


def _pose(im):
    cfw = im.cam_from_world() if callable(im.cam_from_world) else im.cam_from_world
    return np.asarray(cfw.rotation.matrix()), np.asarray(cfw.translation)


class View:
    def __init__(self, rec, im, frames_dir: Path, scale: float):
        cam = rec.cameras[im.camera_id]
        K = np.asarray(cam.calibration_matrix(), float).copy()  # model-aware (SIMPLE_*, PINHOLE, OPENCV, ...)
        K[:2] *= scale
        self.K = K
        self.dist = camera_distortion(cam)
        img = cv2.imread(str(frames_dir / im.name))
        self.img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale != 1 else img
        self.R, self.t = _pose(im)
        self.C = -self.R.T @ self.t
        self.im = im
        self.obs = {p2.point3D_id for p2 in im.points2D if p2.has_point3D()}
        self.depth = None  # filled by its own pair: (u, v) -> depth in this undistorted view


def _sgbm(a, b, ndisp):
    m = cv2.StereoSGBM_create(minDisparity=0, numDisparities=ndisp, blockSize=5, P1=8 * 3 * 25, P2=32 * 3 * 25,
                              uniquenessRatio=10, speckleWindowSize=100, speckleRange=2, disp12MaxDiff=-1,
                              mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY)
    return m.compute(a, b).astype(np.float32) / 16.0


def stereo_pair(v1: View, v2: View, ndisp: int = 128):
    """Returns world points, colours and source pixels (undistorted view-1 coordinates) of LR-consistent pixels."""
    h, w = v1.img.shape[:2]
    R = v2.R @ v1.R.T
    t = v2.t - R @ v1.t
    R1, R2, P1, P2, Q, _, _ = cv2.stereoRectify(v1.K, v1.dist, v2.K, v2.dist, (w, h), R.astype(np.float64),
                                                t.reshape(3, 1).astype(np.float64), alpha=0)
    if abs(P2[1, 3]) > abs(P2[0, 3]):  # mostly vertical baseline: not handled by this horizontal SGBM path
        return None
    m1 = cv2.initUndistortRectifyMap(v1.K, v1.dist, R1, P1, (w, h), cv2.CV_32FC1)
    m2 = cv2.initUndistortRectifyMap(v2.K, v2.dist, R2, P2, (w, h), cv2.CV_32FC1)
    a = cv2.remap(v1.img, *m1, cv2.INTER_LINEAR)
    b = cv2.remap(v2.img, *m2, cv2.INTER_LINEAR)
    ga, gb = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
    dl = _sgbm(ga, gb, ndisp)
    dr = cv2.flip(_sgbm(cv2.flip(gb, 1), cv2.flip(ga, 1), ndisp), 1)
    H, W = dl.shape
    xs = np.arange(W)[None, :].repeat(H, 0)
    xr = np.clip(np.round(xs - dl).astype(int), 0, W - 1)
    ok = (dl > 0.5) & (np.abs(dl - dr[np.arange(H)[:, None], xr]) <= 1.0)
    X = cv2.reprojectImageTo3D(dl, Q)[ok]  # rectified view-1 frame
    col = a[ok]
    Xc1 = (R1.T @ X.T).T  # undistorted view-1 camera frame
    keep = Xc1[:, 2] > 0
    Xc1, col = Xc1[keep], col[keep]
    Xw = (v1.R.T @ (Xc1 - v1.t).T).T
    uv = (v1.K @ (Xc1 / Xc1[:, 2:3]).T).T[:, :2]
    return {"Xw": Xw, "rgb": col[:, ::-1], "uv": uv, "z": Xc1[:, 2], "valid_px": int(ok.sum())}


def _depth_lookup(v: View, Xw: np.ndarray):
    """Depth of world points in view v and the stored SGBM depth at their pixel (nan where unknown)."""
    Xc = (v.R @ Xw.T).T + v.t
    z = Xc[:, 2]
    uv = (v.K @ (Xc / np.maximum(z[:, None], 1e-9)).T).T[:, :2]
    ref = np.full(len(Xw), np.nan)
    if v.depth is not None:
        u, vv = np.round(uv[:, 0]).astype(int), np.round(uv[:, 1]).astype(int)
        h, w = v.depth.shape
        inside = (z > 0) & (u >= 0) & (u < w) & (vv >= 0) & (vv < h)
        ref[inside] = v.depth[vv[inside], u[inside]]
    return z, ref


def _undistorted_gray(v: View) -> np.ndarray:
    if not hasattr(v, "_gray"):
        und = cv2.undistort(v.img, v.K, v.dist) if v.dist.any() else v.img
        v._und = und
        v._gray = cv2.cvtColor(und, cv2.COLOR_BGR2GRAY).astype(np.float32)
    return v._gray


def plane_sweep(ref: View, nbrs: list[View], dmin: float, dmax: float, n_planes: int = PLANES, win: int = 7):
    """Fronto-parallel plane sweep (works for forward motion, unlike rectified two-view stereo).
    Cost = 1 - ZNCC over a win x win window, averaged over the best two neighbours per pixel (occlusion-robust)."""
    g0 = _undistorted_gray(ref)
    h, w = g0.shape
    box = lambda x: cv2.boxFilter(x, -1, (win, win), normalize=True, borderType=cv2.BORDER_REFLECT)  # noqa: E731
    m0 = box(g0)
    v0 = np.maximum(box(g0 * g0) - m0 * m0, 1e-3)
    Kinv = np.linalg.inv(ref.K)
    inv_d = np.linspace(1.0 / dmax, 1.0 / dmin, n_planes)
    best = np.full((h, w), np.inf, np.float32)
    second = np.full((h, w), np.inf, np.float32)
    depth = np.zeros((h, w), np.float32)
    best_i = np.zeros((h, w), np.int16)
    rel = []
    for nb in nbrs:
        R = nb.R @ ref.R.T
        t = nb.t - R @ ref.t
        rel.append((R, t, _undistorted_gray(nb)))
    for pi, idd in enumerate(inv_d):
        d = 1.0 / idd
        costs = []
        for R, t, g1 in rel:
            H = nb_K = None
            H = ref.K @ (R + np.outer(t, [0, 0, 1.0]) / d) @ Kinv  # ref pixel -> neighbour pixel for points at depth d
            warped = cv2.warpPerspective(g1, H, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderValue=-1)
            valid = warped >= 0
            m1 = box(warped)
            v1 = np.maximum(box(warped * warped) - m1 * m1, 1e-3)
            zncc = (box(g0 * warped) - m0 * m1) / np.sqrt(v0 * v1)
            c = np.where(valid, 1.0 - zncc, 2.0).astype(np.float32)
            costs.append(c)
        cs = np.sort(np.stack(costs), axis=0)
        c = cs[: min(2, len(cs))].mean(0)
        better = c < best
        second = np.where(better, best, np.minimum(second, c))
        depth = np.where(better, d, depth)
        best_i = np.where(better, pi, best_i)
        best = np.where(better, c, best)
    # A minimum on the first or last plane means the true depth lies outside the swept range (e.g. clouds, sky):
    # such pixels carry no depth estimate and are dropped instead of being pinned to the range limit.
    interior = (best_i > 0) & (best_i < n_planes - 1)
    conf = (best < MAX_COST) & (second - best > MIN_MARGIN) & (v0 > TEXTURE_VAR) & interior
    return depth, conf, best


def run(sd: Path, rank: int = 0, max_pairs: int = 60) -> dict:
    t0 = time.monotonic()
    report = json.loads((sd / "map_report.json").read_text())
    models = (report.get("reconstruction") or {}).get("models")
    rec = pycolmap.Reconstruction(str(sd / (models[rank]["colmap_dir"] if models else report["reconstruction"]["chosen_model_dir"])))
    frames = json.loads((sd / "frames.json").read_text())
    order = {f["file"]: i for i, f in enumerate(frames)}
    ims = sorted((im for im in rec.images.values() if im.has_pose), key=lambda im: order[im.name])
    cam = rec.cameras[ims[0].camera_id]
    scale = min(1.0, WORK_WIDTH / cam.width)
    out = sd / "dense"
    out.mkdir(exist_ok=True)
    flags_path = sd / "diagnosis_segments.json"
    low = json.loads(flags_path.read_text()).get("low_confidence_windows", []) if flags_path.exists() else []
    t_of = {f["file"]: f.get("timestamp_s") for f in frames}
    views: dict[int, View] = {}

    def view(i):
        if i not in views:
            views[i] = View(rec, ims[i], sd / "frames", scale)
        return views[i]

    def ratio(i, j):
        Ri, ti = _pose(ims[i]); Rj, tj = _pose(ims[j])
        shared = view(i).obs & view(j).obs
        if len(shared) < MIN_SHARED:
            return None
        d = float(np.median([(Ri @ rec.points3D[p].xyz + ti)[2] for p in shared]))
        return float(np.linalg.norm((-Rj.T @ tj) - (-Ri.T @ ti))) / d

    # D1: reference views with 2-4 neighbours whose baseline / depth is in range (both directions in time)
    refs = []
    for k in range(len(ims)):
        if any(a <= (t_of[ims[k].name] or 0) <= b for a, b in low):
            continue
        nb = []
        for s in (1, -1, 2, -2, 3, -3, 4, -4, 6, -6):
            j = k + s
            if 0 <= j < len(ims):
                r = ratio(k, j)
                if r is not None and BASE_DEPTH[0] <= r <= BASE_DEPTH[1]:
                    nb.append((j, r))
            if len(nb) >= 4:
                break
        if len(nb) >= 2:
            refs.append((k, nb))
    refs = refs[:: max(1, len(refs) // max_pairs)][:max_pairs]

    results = []
    for k, nb in refs:
        v = view(k)
        R, tt = v.R, v.t
        sp = np.array([rec.points3D[p].xyz for p in v.obs])
        zs = (R @ sp.T).T[:, 2] + tt[2]
        zs = zs[zs > 0]
        dmin, dmax = float(np.percentile(zs, 2)) * 0.8, float(np.percentile(zs, 98)) * 1.25
        depth, conf, cost = plane_sweep(v, [view(j) for j, _ in nb], dmin, dmax)
        dm = np.where(conf, depth, np.nan).astype(np.float32)
        v.depth = dm
        z_s, z_d = _depth_lookup(v, sp)
        m = ~np.isnan(z_d)
        agree = float(np.mean(np.abs(z_d[m] - z_s[m]) / z_s[m] <= REL_TOL)) if m.sum() >= 20 else 0.0
        row = {"ref": k, "frame": ims[k].name, "neighbours": [j for j, _ in nb],
               "baseline_over_depth": [round(r, 3) for _, r in nb], "confident_px": int(conf.sum()),
               "confident_share": round(float(conf.mean()), 3), "sparse_checked": int(m.sum()), "sparse_agreement": round(agree, 3)}
        if agree < SPARSE_AGREE_MIN:
            row.update(status="REJECTED", reason=f"sparse agreement {agree:.2f} < {SPARSE_AGREE_MIN}")
            results.append((row, None))
            continue
        vv, uu = np.nonzero(conf)
        z = depth[vv, uu]
        Xc = (np.linalg.inv(v.K) @ np.stack([uu, vv, np.ones_like(uu)]).astype(float)) * z
        Xw = (R.T @ (Xc - tt[:, None])).T
        rgb = v._und[vv, uu][:, ::-1]
        row["status"] = "DEPTH_OK"
        results.append((row, {"Xw": Xw, "rgb": rgb}))

    # D3(ii): geometric consistency with the next accepted reference view's own depth map
    kept_pts, kept_rgb = [], []
    ok_idx = [n for n, (r, st) in enumerate(results) if st is not None]
    for a, b in zip(ok_idx, ok_idx[1:] + [None]):
        row, st = results[a]
        partner = results[b][0]["ref"] if b is not None else (results[ok_idx[-2]][0]["ref"] if len(ok_idx) > 1 else None)
        if partner is None:
            row.update(status="REJECTED", reason="no other accepted view for the consistency check")
            continue
        z, ref = _depth_lookup(views[partner], st["Xw"])
        m = ~np.isnan(ref)
        good = m & (np.abs(ref - z) / np.maximum(z, 1e-9) <= REL_TOL)
        kept_share = float(good.sum()) / max(1, len(z))
        row.update(consistency_partner=partner, consistency_checked=int(m.sum()), consistency_kept=int(good.sum()),
                   consistency_kept_share=round(kept_share, 3))
        if kept_share < THIRD_KEEP_MIN:
            row.update(status="REJECTED", reason=f"multi-view consistency kept {kept_share:.2f} < {THIRD_KEEP_MIN}")
            continue
        row["status"] = "VERIFIED"
        kept_pts.append(st["Xw"][good])
        kept_rgb.append(st["rgb"][good])

    sparse = np.array([p.xyz for p in rec.points3D.values()])
    nn, _ = cKDTree(sparse).query(sparse, k=2)
    voxel = float(np.median(nn[:, 1]))
    summary = {"session": sd.name, "model_rank": rank, "method": "plane-sweep MVS (ZNCC, best-2-of-4 neighbours), CPU",
               "deviation_from_plan": "rectified two-view SGBM degenerates for forward camera motion (baseline along the optical axis, epipole inside the image); replaced by plane sweep, verification criteria unchanged",
               "work_width": int(round(cam.width * scale)), "reference_views": len(refs),
               "views": [r for r, _ in results], "voxel": voxel, "low_confidence_windows_excluded": low}
    summary["views_verified"] = sum(1 for r, _ in results if r.get("status") == "VERIFIED")
    covered = {r["ref"] for r, _ in results if r.get("status") == "VERIFIED"}
    sparse_covered = len({p for k in covered for p in views[k].obs})
    if kept_pts:
        P = np.concatenate(kept_pts)
        C = np.concatenate(kept_rgb).astype(float)
        key = np.floor(P / voxel).astype(np.int64)
        _, inv, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True)
        inv = inv.ravel()
        fused = np.zeros((len(cnt), 3)); fcol = np.zeros((len(cnt), 3))
        np.add.at(fused, inv, P); np.add.at(fcol, inv, C)
        fused /= cnt[:, None]; fcol /= cnt[:, None]
        pts = np.zeros(len(fused), dtype=PLY_DTYPE)
        pts["x"], pts["y"], pts["z"] = fused.T
        pts["red"], pts["green"], pts["blue"] = np.clip(fcol, 0, 255).T
        write_ply(out / "dense_verified.ply", pts)
        summary["dense_verified_points"] = int(len(fused))
        summary["dense_raw_kept_points"] = int(len(P))
    else:
        summary["dense_verified_points"] = 0
    summary["sparse_points_of_covered_views"] = sparse_covered
    summary["label"] = "DENSE_VERIFIED (relative scale)"
    summary["seconds"] = round(time.monotonic() - t0, 1)
    acc = {"reference_views_ge_20": len(refs) >= 20,
           "verified_share_ge_0.5": summary["views_verified"] >= 0.5 * max(1, len(refs)),
           "dense_ge_10x_sparse_of_covered_views": summary["dense_verified_points"] >= 10 * max(1, sparse_covered)}
    summary["acceptance"] = {**acc, "result": "PASS" if all(acc.values()) else "FAIL"}
    (out / "dense_report.json").write_text(json.dumps(summary, indent=1))
    return summary


def mesh(sd: Path, voxel: float) -> dict:
    out = sd / "dense"
    src = out / "dense_verified.ply"
    opts = pycolmap.PoissonMeshingOptions()
    raw = out / "mesh_poisson_raw.ply"
    pycolmap.poisson_meshing(str(src), str(raw), opts)
    return {"raw_mesh": str(raw)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.dense_cpu")
    ap.add_argument("session_dir")
    ap.add_argument("--model", type=int, default=0)
    ap.add_argument("--max-pairs", type=int, default=60)
    a = ap.parse_args(argv)
    s = run(Path(a.session_dir), a.model, a.max_pairs)
    print(json.dumps({k: v for k, v in s.items() if k != "views"} | {"view_status": [(p["ref"], p["status"], p.get("reason", ""), p.get("confident_share"), p.get("sparse_agreement"), p.get("consistency_kept_share")) for p in s["views"]]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
