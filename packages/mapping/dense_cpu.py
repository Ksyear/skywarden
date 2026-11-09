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
from dataclasses import asdict, dataclass
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
    def __init__(self, rec, im, frames_dir: Path, scale: float, mask_dir: Path | None = None):
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
        self.obs_all = self.obs  # before any keep-mask filtering (S2n uses it for neighbour selection)
        self.depth = None  # filled by its own pair: (u, v) -> depth in this undistorted view
        if mask_dir is not None:
            path = mask_dir / (im.name + ".png")
            keep = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
            if (keep is None or keep.shape != img.shape[:2] or keep.dtype != np.uint8
                    or np.any((keep != 0) & (keep != 255))):
                raise ValueError(f"Expected a full-resolution binary keep mask: {path}")
            kept_observations = set()
            h, w = keep.shape
            for p2 in im.points2D:
                if p2.has_point3D():
                    x, y = np.floor(p2.xy).astype(int)
                    if 0 <= x < w and 0 <= y < h and keep[y, x]:
                        kept_observations.add(p2.point3D_id)
            self.obs = kept_observations
            keep = cv2.resize(keep, self.img.shape[1::-1], interpolation=cv2.INTER_NEAREST)
            keep = keep.astype(np.float32) / 255
            if self.dist.any():
                keep = cv2.undistort(keep, self.K, self.dist)
            self.static_keep = (keep >= 0.999).astype(np.float32)


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


@dataclass(frozen=True)
class DenseConfig:
    """Defaults reproduce the M0 baseline. Each experiment of docs/PLAN_DENSE_COVERAGE.md changes one field."""
    work_width: int = WORK_WIDTH
    planes: int = PLANES
    near_rule: str = "p2"        # "p2": dmin = p2 x 0.8 | "p2_or_nearest": min(p2 x 0.8, 0.5 x nearest-1 % depth), 96 planes (E-R)
    sweep: str = "fronto"        # "fronto" | "fronto+ground" (E-S)
    verify: str = "next"         # "next": next accepted view only | "multi": every view within +-window (E-V)
    verify_window: int = 8
    min_agree: int = 1           # multi: views agreeing within REL_TOL needed (E-V2 = 2)
    voxel_near_scale: float = 1.0  # E-F: voxel x scale for points nearer than their view's median depth
    neighbour_rule: str = "median"  # "median_or_near" (E-N): p10-depth baseline also qualifies; "median_then_near" (E-N2): only for free slots
    border_exclusion: bool = False  # S1 (docs/PLAN_STATIC_MAP.md): no depth where the matching window leaves the image
    mask_cost: str = "erode"        # with keep-masks: "erode" drops a pixel if any window pixel is masked;
                                    # "valid_pixels" (S2) scores ZNCC over unmasked, in-image window pixels only
    min_valid_frac: float = 0.6     # S2: minimum share of usable window pixels (reference and each neighbour)
    neighbours_all_sparse: bool = False  # S2n: baseline/depth ratios from all sparse points, not only unmasked ones
    coarse_ground: bool = False  # D2: ground-plane sweep at half resolution fills pixels the full-resolution sweep missed


def plane_sweep(ref: View, nbrs: list[View], dmin: float, dmax: float, n_planes: int = PLANES, win: int = 7,
                ground: tuple | None = None, border_exclusion: bool = False, mask_cost: str = "erode",
                min_valid_frac: float = 0.6, crop_ground: bool = True, ground_only: bool = False):
    """Plane sweep (works for forward motion, unlike rectified two-view stereo). Cost = 1 - ZNCC over a win x win
    window, averaged over the best two neighbours per pixel (occlusion-robust).

    Family "fronto": planes z = d (fronto-parallel), n_planes samples uniform in inverse depth.
    Family "ground" (optional, `ground=(n, offsets)`): planes n . X = d with the ground normal n in reference camera
    coordinates; for pixels whose ray never meets the plane (above the horizon) the hypothesis is invalid.
    Per family a pixel is confident when its minimum is interior (not the first/last plane), cheap, distinct from the
    second best and textured. With two families, the cheaper confident family wins per pixel.
    Returns depth, conf, best cost and a diagnostics dict (per-family maps, texture variance)."""
    g0 = _undistorted_gray(ref)
    h, w = g0.shape
    box = lambda x: cv2.boxFilter(x, -1, (win, win), normalize=True, borderType=cv2.BORDER_REFLECT)  # noqa: E731
    m0 = box(g0)
    v0 = np.maximum(box(g0 * g0) - m0 * m0, 1e-3)
    Kinv = np.linalg.inv(ref.K)
    keep = getattr(ref, "static_keep", None)
    window = np.ones((win, win), np.uint8)
    masked = mask_cost == "valid_pixels"
    if masked:
        # S2: the reference centre pixel must be static and enough of its window must be usable; texture is measured on
        # the usable pixels only, so a branch next to the sky keeps its own texture instead of the sky's
        kr = np.ones((h, w), np.float32) if keep is None else (keep >= 0.999).astype(np.float32)
        ref_frac = box(kr)
        ref_support = (kr > 0) & (ref_frac >= min_valid_frac)
        n0 = np.maximum(ref_frac, 1e-6)
        m0r = box(kr * g0) / n0
        v0 = np.maximum(box(kr * g0 * g0) / n0 - m0r * m0r, 1e-3)
        g0sq = g0 * g0
    else:
        ref_support = cv2.erode(keep, window, borderType=cv2.BORDER_CONSTANT, borderValue=0) > 0 if keep is not None else True
    if border_exclusion:  # S1: a window that leaves the image is filled by reflection and gives column-constant depth
        half = win // 2
        inside = np.zeros((h, w), bool)
        inside[half:h - half, half:w - half] = True
        ref_support = inside & ref_support
    rel = []
    for nb in nbrs:
        R = nb.R @ ref.R.T
        t = nb.t - R @ ref.t
        rel.append((R, t, _undistorted_gray(nb), getattr(nb, "static_keep", None)))
    g0_full, m0_full, v0_full = g0, m0, v0
    if masked:
        kr_full, g0sq_full = kr, g0sq
    families = [("fronto", np.array([0, 0, 1.0]), [1.0 / idd for idd in np.linspace(1.0 / dmax, 1.0 / dmin, n_planes)])]
    if ground is not None:
        families.append(("ground", np.asarray(ground[0], float), list(ground[1])))
    if ground_only and ground is not None:
        families = families[1:]
    fam = {}
    for name, n, ds in families:
        if name == "fronto":
            denom = None
        else:
            uu, vv = np.meshgrid(np.arange(w), np.arange(h))
            rays = np.stack([uu, vv, np.ones_like(uu)], -1).astype(np.float64) @ Kinv.T
            denom = (rays @ n).astype(np.float32)
            front = denom > 1e-6
        best = np.full((h, w), np.inf, np.float32)
        second = np.full((h, w), np.inf, np.float32)
        depth = np.zeros((h, w), np.float32)
        best_i = np.zeros((h, w), np.int16)
        # Ground hypotheses only exist below the horizon: compute them on the rows from (first valid row - window)
        # down; rows above keep cost 2 (identical result, about half the work for this family).
        r0 = 0
        if denom is not None and crop_ground:
            rows = np.nonzero(front.any(1))[0]
            r0 = h if not len(rows) else max(0, int(rows[0]) - win)
        hh = h - r0
        T = np.array([[1, 0, 0], [0, 1, r0], [0, 0, 1.0]])
        g0, m0, v0_c = g0_full[r0:], m0_full[r0:], v0_full[r0:]
        if masked:
            kr, g0sq = kr_full[r0:], g0sq_full[r0:]
        for pi, d in enumerate(ds):
            costs = []
            for R, t, g1, nbr_keep in (rel if hh > 0 else []):
                H = ref.K @ (R + np.outer(t, n) / d) @ Kinv @ T  # ref pixel (row offset r0) -> neighbour pixel on the plane
                warped = cv2.warpPerspective(g1, H, (w, hh), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderValue=-1)
                valid = warped >= 0
                if masked:
                    wv = valid
                    if nbr_keep is not None:
                        wv = wv & (cv2.warpPerspective(nbr_keep, H, (w, hh), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP) >= 0.999)
                    wv = wv.astype(np.float32) * kr
                    N = box(wv)
                    Nn = np.maximum(N, 1e-6)
                    gw = np.where(wv > 0, warped, 0).astype(np.float32)
                    m0w, m1w = box(wv * g0) / Nn, box(gw) / Nn
                    v0w = np.maximum(box(wv * g0sq) / Nn - m0w * m0w, 1e-3)
                    v1w = np.maximum(box(gw * gw) / Nn - m1w * m1w, 1e-3)
                    zncc = (box(gw * g0) / Nn - m0w * m1w) / np.sqrt(v0w * v1w)
                    costs.append(np.where((N >= min_valid_frac) & (wv > 0), 1.0 - zncc, 2.0).astype(np.float32))
                    continue
                if nbr_keep is not None:
                    warped_keep = cv2.warpPerspective(nbr_keep, H, (w, hh),
                                                     flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
                    valid = cv2.erode((valid & (warped_keep >= 0.999)).astype(np.uint8), window,
                                      borderType=cv2.BORDER_CONSTANT, borderValue=0) > 0
                m1 = box(warped)
                v1 = np.maximum(box(warped * warped) - m1 * m1, 1e-3)
                zncc = (box(g0 * warped) - m0 * m1) / np.sqrt(v0_c * v1)
                costs.append(np.where(valid, 1.0 - zncc, 2.0).astype(np.float32))
            c = np.full((h, w), 2.0, np.float32)
            if costs:
                cs = np.sort(np.stack(costs), axis=0)
                c[r0:] = cs[: min(2, len(cs))].mean(0)
            if denom is not None:
                c = np.where(front, c, np.float32(2.0))
            better = c < best
            second = np.where(better, best, np.minimum(second, c))
            z = d if denom is None else np.where(front, d / np.maximum(denom, 1e-6), 0).astype(np.float32)
            depth = np.where(better, z, depth)
            best_i = np.where(better, pi, best_i)
            best = np.where(better, c, best)
        # A minimum on the first or last plane means the true surface lies outside the swept range (e.g. clouds, sky):
        # such pixels carry no depth estimate and are dropped instead of being pinned to the range limit.
        interior = (best_i > 0) & (best_i < len(ds) - 1)
        conf = (best < MAX_COST) & (second - best > MIN_MARGIN) & (v0_full > TEXTURE_VAR) & interior & ref_support
        fam[name] = {"best": best, "second": second, "idx": best_i, "depth": depth, "conf": conf, "n_planes": len(ds)}
    if "fronto" not in fam:
        g = fam["ground"]
        return g["depth"], g["conf"], g["best"], {"v0": v0_full, "families": fam, "chosen": np.ones(g["depth"].shape, np.int8)}
    f = fam["fronto"]
    depth, conf, best = f["depth"], f["conf"], f["best"]
    chosen = np.zeros(depth.shape, np.int8)
    if "ground" in fam:
        g = fam["ground"]
        use_g = g["conf"] & (~conf | (g["best"] < best))
        depth = np.where(use_g, g["depth"], depth)
        best = np.where(use_g, g["best"], best)
        conf = conf | g["conf"]
        chosen[use_g] = 1
    return depth, conf, best, {"v0": v0_full, "families": fam, "chosen": chosen}


def ground_plane(v: View, rec, max_tilt_deg: float = 30.0, min_inliers: int = 30):
    """Ground plane n . X = d (reference camera coordinates, n pointing down, d > 0 = camera height) fitted by RANSAC to
    the sparse points the view observes in the lower half of its image. The normal must lie within max_tilt_deg of the
    camera's image-down axis. Returns (n, d, inlier_share, n_candidates) or None."""
    X = np.array([rec.points3D[p].xyz for p in v.obs])
    Xc = (v.R @ X.T).T + v.t
    Xc = Xc[Xc[:, 2] > 0]
    uv = (v.K @ (Xc / Xc[:, 2:3]).T).T
    P = Xc[uv[:, 1] > v.K[1, 2]]
    if len(P) < min_inliers:
        return None
    thr = 0.02 * float(np.median(np.linalg.norm(P, axis=1)))
    cos_tilt = np.cos(np.radians(max_tilt_deg))
    rng = np.random.default_rng(0)
    best = None
    for _ in range(400):
        s = P[rng.choice(len(P), 3, replace=False)]
        n = np.cross(s[1] - s[0], s[2] - s[0])
        nn = np.linalg.norm(n)
        if nn < 1e-12:
            continue
        n = n / nn
        if n[1] < 0:
            n = -n
        d = float(n @ s[0])
        if n[1] < cos_tilt or d <= 0:
            continue
        inl = np.abs(P @ n - d) < thr
        if best is None or inl.sum() > best.sum():
            best = inl
    if best is None or best.sum() < min_inliers:
        return None
    Q = P[best]
    c = Q.mean(0)
    n = np.linalg.svd(Q - c)[2][-1]
    if n[1] < 0:
        n = -n
    d = float(n @ c)
    if n[1] < cos_tilt or d <= 0:
        return None
    return n, d, round(float(best.mean()), 3), int(len(P))


def _project(v: View, Xw: np.ndarray):
    """Depth of world points in view v, the view's stored confident depth at their pixel (nan where none), whether the
    point projects inside the image in front of the camera, and the pixel."""
    Xc = (v.R @ Xw.T).T + v.t
    z = Xc[:, 2]
    uv = (v.K @ (Xc / np.maximum(z[:, None], 1e-9)).T).T[:, :2]
    u, vv = np.round(uv[:, 0]).astype(np.int64), np.round(uv[:, 1]).astype(np.int64)
    ref = np.full(len(Xw), np.nan)
    h, w = (v.depth.shape if v.depth is not None else v.img.shape[:2])
    inside = (z > 0) & (u >= 0) & (u < w) & (vv >= 0) & (vv < h)
    if v.depth is not None:
        ref[inside] = v.depth[vv[inside], u[inside]]
    return z, ref, inside


# verification codes per confident pixel (stage diagnostics)
V_KEPT, V_OUTSIDE, V_NO_DEPTH, V_DISAGREE, V_VIEW_REJECTED = 0, 1, 2, 3, 4


def _verify_next(st, partner_view):
    z, ref, inside = _project(partner_view, st["Xw"])
    has = ~np.isnan(ref)
    good = has & (np.abs(ref - z) / np.maximum(z, 1e-9) <= REL_TOL)
    code = np.full(len(z), V_DISAGREE, np.int8)
    code[~inside] = V_OUTSIDE
    code[inside & ~has] = V_NO_DEPTH
    code[good] = V_KEPT
    return good, code, int(has.sum())


def _verify_multi(st, verifiers, min_agree):
    """Keep a point iff >= min_agree verifier views hold a confident depth within REL_TOL of it and none looks past it
    (its depth > (1 + REL_TOL) x the point's depth = free space where the point claims a surface). A depth in front of
    the point is occlusion and neutral. Points no verifier can check are dropped (unobserved stays empty)."""
    n = len(st["Xw"])
    agree = np.zeros(n, np.int16); contra = np.zeros(n, np.int16)
    seen = np.zeros(n, bool); has_any = np.zeros(n, bool)
    for vw in verifiers:
        z, ref, inside = _project(vw, st["Xw"])
        has = ~np.isnan(ref)
        seen |= inside
        has_any |= has
        rel = np.abs(ref - z) / np.maximum(z, 1e-9)
        agree += (has & (rel <= REL_TOL)).astype(np.int16)
        contra += (has & (ref > (1 + REL_TOL) * z)).astype(np.int16)
    good = (agree >= min_agree) & (contra == 0)
    code = np.full(n, V_DISAGREE, np.int8)
    code[~seen] = V_OUTSIDE
    code[seen & ~has_any] = V_NO_DEPTH
    code[good] = V_KEPT
    return good, code, int(has_any.sum())


def run(sd: Path, rank: int = 0, max_pairs: int = 60, cfg: DenseConfig = DenseConfig(), out: Path | None = None,
        t_range: tuple[float, float] | None = None, diag_frames: tuple[str, ...] = (),
        mask_dir: Path | None = None) -> dict:
    t0 = time.monotonic()
    report = json.loads((sd / "map_report.json").read_text())
    models = (report.get("reconstruction") or {}).get("models")
    rec = pycolmap.Reconstruction(str(sd / (models[rank]["colmap_dir"] if models else report["reconstruction"]["chosen_model_dir"])))
    frames = json.loads((sd / "frames.json").read_text())
    order = {f["file"]: i for i, f in enumerate(frames)}
    ims = sorted((im for im in rec.images.values() if im.has_pose), key=lambda im: order[im.name])
    cam = rec.cameras[ims[0].camera_id]
    scale = min(1.0, cfg.work_width / cam.width)
    out = out or sd / ("dense_static" if mask_dir is not None else "dense")
    out.mkdir(parents=True, exist_ok=True)
    flags_path = sd / "diagnosis_segments.json"
    low = json.loads(flags_path.read_text()).get("low_confidence_windows", []) if flags_path.exists() else []
    t_of = {f["file"]: f.get("timestamp_s") for f in frames}
    views: dict[int, View] = {}

    def view(i):
        if i not in views:
            views[i] = View(rec, ims[i], sd / "frames", scale, mask_dir)
        return views[i]

    views_half: dict[int, View] = {}

    def view_half(i):
        if i not in views_half:
            views_half[i] = View(rec, ims[i], sd / "frames", scale * 0.5, mask_dir)
        return views_half[i]

    ratio_cache: dict[tuple[int, int], tuple | None] = {}

    def ratios(i, j):
        """(baseline / median co-visible depth, baseline / p10 co-visible depth) or None."""
        if (i, j) in ratio_cache:
            return ratio_cache[(i, j)]
        Ri, ti = _pose(ims[i]); Rj, tj = _pose(ims[j])
        shared = (view(i).obs_all & view(j).obs_all) if cfg.neighbours_all_sparse else (view(i).obs & view(j).obs)
        r = None
        if len(shared) >= MIN_SHARED:
            z = np.array([(Ri @ rec.points3D[p].xyz + ti)[2] for p in shared])
            b = float(np.linalg.norm((-Rj.T @ tj) - (-Ri.T @ ti)))
            zp = z[z > 0]
            r = (b / float(np.median(z)), b / float(np.percentile(zp, 10)) if len(zp) else None,
                 b / float(np.percentile(zp, 90)) if len(zp) else None)
        ratio_cache[(i, j)] = r
        return r

    def ratio(i, j):
        r = ratios(i, j)
        return None if r is None else r[0]

    def in_range(x):
        return x is not None and BASE_DEPTH[0] <= x <= BASE_DEPTH[1]

    def neighbours(k):
        nb = []
        for s in (1, -1, 2, -2, 3, -3, 4, -4, 6, -6):
            j = k + s
            if 0 <= j < len(ims):
                rr = ratios(k, j)
                if rr is not None and (in_range(rr[0]) or (cfg.neighbour_rule == "median_or_near" and in_range(rr[1]))):
                    nb.append((j, rr[0]))
            if len(nb) >= 4:
                break
        if cfg.neighbour_rule in ("median_then_near", "median_then_near_far") and len(nb) < 4:
            # E-N2: free slots only are filled with frames whose baseline suits the near surfaces (p10 depth)
            for s in (1, -1, 2, -2, 3, -3, 4, -4, 6, -6):
                j = k + s
                if 0 <= j < len(ims) and j not in {x for x, _ in nb}:
                    rr = ratios(k, j)
                    if rr is not None and in_range(rr[1]):
                        nb.append((j, rr[0]))
                if len(nb) >= 4:
                    break
        if cfg.neighbour_rule == "median_then_near_far":
            # D1 (docs/PLAN_FRAMES_AND_DEPTH.md): far surfaces need a longer baseline than the median depth asks for;
            # up to 2 extra neighbours within +-12 frames whose baseline suits the p90 co-visible depth
            have = {x for x, _ in nb}
            for s in [v for k in range(1, 13) for v in (k, -k)]:
                if len(nb) >= 6:
                    break
                j = k + s
                if 0 <= j < len(ims) and j not in have:
                    rr = ratios(k, j)
                    if rr is not None and in_range(rr[2]):
                        nb.append((j, rr[0])); have.add(j)
        return nb

    # D1: reference views with 2-4 neighbours whose baseline / depth is in range (both directions in time)
    refs = []
    for k in range(len(ims)):
        tk = t_of[ims[k].name] or 0
        if any(a <= tk <= b for a, b in low) or (t_range and not t_range[0] <= tk <= t_range[1]):
            continue
        nb = neighbours(k)
        if len(nb) >= 2:
            refs.append((k, nb))
    refs = refs[:: max(1, len(refs) // max_pairs)][:max_pairs]
    ref_ids = {k for k, _ in refs}
    # Stage-diagnostic frames that are not production reference views get their own sweep (never fused).
    name_to_idx = {im.name: i for i, im in enumerate(ims)}
    forced = []
    for fn in diag_frames:
        k = name_to_idx.get(fn)
        if k is not None and k not in ref_ids:
            forced.append((k, neighbours(k)))

    ground_info = {}

    def depth_range(v):
        sp = np.array([rec.points3D[p].xyz for p in v.obs])
        zs = (v.R @ sp.T).T[:, 2] + v.t[2]
        zs = zs[zs > 0]
        dmin, dmax = float(np.percentile(zs, 2)) * 0.8, float(np.percentile(zs, 98)) * 1.25
        n_planes = cfg.planes
        if cfg.near_rule == "p2_or_nearest":
            dmin = min(dmin, 0.5 * float(np.percentile(zs, 1)))
            n_planes = 96
        return sp, dmin, dmax, n_planes

    def sweep(k, nb):
        v = view(k)
        sp, dmin, dmax, n_planes = depth_range(v)
        ground = None
        if cfg.sweep == "fronto+ground":
            gp = ground_plane(v, rec)
            ground_info[k] = None if gp is None else {"normal": [round(float(x), 4) for x in gp[0]], "height": round(gp[1], 5),
                                                      "inlier_share": gp[2], "candidates": gp[3]}
            if gp is not None:
                ground = (gp[0], [1.0 / x for x in np.linspace(1.0 / (2.0 * gp[1]), 1.0 / (0.5 * gp[1]), n_planes)])
        depth, conf, cost, dg = plane_sweep(v, [view(j) for j, _ in nb], dmin, dmax, n_planes=n_planes, ground=ground,
                                            border_exclusion=cfg.border_exclusion, mask_cost=cfg.mask_cost,
                                            min_valid_frac=cfg.min_valid_frac)
        if cfg.coarse_ground and ground is not None:
            # D2: the same 7x7 window covers twice the floor area at half resolution (low-texture, oblique floor);
            # its winning ground plane is re-evaluated exactly on the full-resolution rays, only where full resolution
            # had no confident depth; multi-view verification later decides as for every other point
            vh = view_half(k)
            _, _, _, gh = plane_sweep(vh, [view_half(j) for j, _ in nb], dmin, dmax, n_planes=n_planes, ground=ground,
                                      border_exclusion=cfg.border_exclusion, mask_cost=cfg.mask_cost,
                                      min_valid_frac=cfg.min_valid_frac, ground_only=True)
            G = gh["families"]["ground"]
            h, w = depth.shape
            conf_up = cv2.resize(G["conf"].astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
            idx_up = cv2.resize(G["idx"].astype(np.int16), (w, h), interpolation=cv2.INTER_NEAREST)
            uu, vv = np.meshgrid(np.arange(w), np.arange(h))
            denom = (np.stack([uu, vv, np.ones_like(uu)], -1).astype(np.float64) @ np.linalg.inv(v.K).T) @ ground[0]
            offs = np.asarray(ground[1])
            fill = conf_up & ~conf & (denom > 1e-6)
            keep = getattr(v, "static_keep", None)
            if keep is not None:
                fill &= keep > 0
            if cfg.border_exclusion:
                fill[:3] = fill[-3:] = False; fill[:, :3] = fill[:, -3:] = False
            depth = np.where(fill, offs[idx_up] / np.maximum(denom, 1e-6), depth).astype(np.float32)
            conf = conf | fill
            dg["coarse_fill"] = fill
        return v, sp, depth, conf, dg

    results = []
    stage = {}
    for k, nb in refs:
        v, sp, depth, conf, dg = sweep(k, nb)
        dm = np.where(conf, depth, np.nan).astype(np.float32)
        v.depth = dm
        z_s, z_d = _depth_lookup(v, sp)
        m = ~np.isnan(z_d)
        agree = float(np.mean(np.abs(z_d[m] - z_s[m]) / z_s[m] <= REL_TOL)) if m.sum() >= 20 else 0.0
        row = {"ref": k, "frame": ims[k].name, "neighbours": [j for j, _ in nb],
               "baseline_over_depth": [round(r, 3) for _, r in nb], "confident_px": int(conf.sum()),
               "confident_share": round(float(conf.mean()), 3), "sparse_checked": int(m.sum()), "sparse_agreement": round(agree, 3)}
        if k in ground_info:
            row["ground_plane"] = ground_info[k]
            row["ground_chosen_px"] = int((dg["chosen"] == 1).sum())
        if ims[k].name in diag_frames:
            stage[k] = dg
        if agree < SPARSE_AGREE_MIN:
            row.update(status="REJECTED", reason=f"sparse agreement {agree:.2f} < {SPARSE_AGREE_MIN}")
            results.append((row, None))
            continue
        vv, uu = np.nonzero(conf)
        z = depth[vv, uu]
        Xc = (np.linalg.inv(v.K) @ np.stack([uu, vv, np.ones_like(uu)]).astype(float)) * z
        Xw = (v.R.T @ (Xc - v.t[:, None])).T
        rgb = v._und[vv, uu][:, ::-1]
        row["status"] = "DEPTH_OK"
        results.append((row, {"Xw": Xw, "rgb": rgb, "u": uu.astype(np.int16), "v": vv.astype(np.int16), "z": z}))

    ok_idx = [n for n, (r, st) in enumerate(results) if st is not None]
    ok_refs = [results[n][0]["ref"] for n in ok_idx]

    def verifier_set(k):
        """Views allowed to verify depth of reference k (also excluded from its held-out check)."""
        if cfg.verify == "next":
            later = [j for j in ok_refs if j > k]
            earlier = [j for j in ok_refs if j < k]
            return [later[0]] if later else ([earlier[-1]] if earlier else [])
        return [j for j in ok_refs if j != k and abs(j - k) <= cfg.verify_window
                and (lambda r: r is not None and BASE_DEPTH[0] <= r <= BASE_DEPTH[1])(ratio(k, j))]

    # D3(ii): geometric consistency with other accepted reference views' own depth maps
    kept = []  # (Xw, rgb, ref, u, v, z, near)
    vcodes = {}
    for n in ok_idx:
        row, st = results[n]
        k = row["ref"]
        vs = verifier_set(k)
        if not vs:
            row.update(status="REJECTED", reason="no other accepted view for the consistency check")
            vcodes[k] = np.full(len(st["Xw"]), V_VIEW_REJECTED, np.int8)
            continue
        if cfg.verify == "next":
            good, code, checked = _verify_next(st, views[vs[0]])
            row["consistency_partner"] = vs[0]
        else:
            good, code, checked = _verify_multi(st, [views[j] for j in vs], cfg.min_agree)
            row["verifiers"] = vs
        kept_share = float(good.sum()) / max(1, len(good))
        row.update(consistency_checked=checked, consistency_kept=int(good.sum()), consistency_kept_share=round(kept_share, 3),
                   drop_reasons={"outside": int((code == V_OUTSIDE).sum()), "no_depth": int((code == V_NO_DEPTH).sum()),
                                 "disagree": int((code == V_DISAGREE).sum())})
        if kept_share < THIRD_KEEP_MIN:
            row.update(status="REJECTED", reason=f"multi-view consistency kept {kept_share:.2f} < {THIRD_KEEP_MIN}")
            code = np.where(code == V_KEPT, V_VIEW_REJECTED, code).astype(np.int8)
            vcodes[k] = code
            continue
        vcodes[k] = code
        row["status"] = "VERIFIED"
        near = st["z"][good] < float(np.median(st["z"]))
        kept.append((st["Xw"][good], st["rgb"][good], np.full(int(good.sum()), k, np.int16), st["u"][good], st["v"][good],
                     st["z"][good], near))

    sparse = np.array([p.xyz for p in rec.points3D.values()])
    nn, _ = cKDTree(sparse).query(sparse, k=2)
    voxel = float(np.median(nn[:, 1]))
    summary = {"session": sd.name, "model_rank": rank, "method": "plane-sweep MVS (ZNCC, best-2-of-4 neighbours), CPU",
               "deviation_from_plan": "rectified two-view SGBM degenerates for forward camera motion (baseline along the optical axis, epipole inside the image); replaced by plane sweep, verification criteria unchanged",
               "config": asdict(cfg), "t_range": list(t_range) if t_range else None,
               "work_width": int(round(cam.width * scale)), "reference_views": len(refs),
               "views": [r for r, _ in results], "voxel": voxel, "low_confidence_windows_excluded": low}
    summary["views_verified"] = sum(1 for r, _ in results if r.get("status") == "VERIFIED")
    covered = {r["ref"] for r, _ in results if r.get("status") == "VERIFIED"}
    sparse_covered = len({p for k in covered for p in views[k].obs})
    inv = None
    if kept:
        P = np.concatenate([x[0] for x in kept])
        C = np.concatenate([x[1] for x in kept]).astype(float)
        near = np.concatenate([x[6] for x in kept])
        # E-F: finer voxel for near points only (separate grids, so near and far voxels never merge)
        vox = np.where(near, voxel * cfg.voxel_near_scale, voxel) if cfg.voxel_near_scale != 1.0 else np.full(len(P), voxel)
        key = np.floor(P / vox[:, None]).astype(np.int64)
        if cfg.voxel_near_scale != 1.0:
            key = np.concatenate([key, near[:, None].astype(np.int64)], 1)
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
    summary["label"] = ("DENSE_STATIC_TRIAL (relative scale)" if mask_dir is not None
                        else "DENSE_VERIFIED (relative scale)")
    if mask_dir is not None:
        summary["static_masks"] = {"directory": str(mask_dir),
                                   "use": "reference and matching windows, and sparse depth support"}
        manifest = mask_dir / "mask_report.json"
        if manifest.is_file():
            meta = json.loads(manifest.read_text())
            summary["static_masks"].update({k: meta[k] for k in ("model", "revision", "sha256", "classes_excluded") if k in meta})
    summary["seconds"] = round(time.monotonic() - t0, 1)
    acc = {"reference_views_ge_20": len(refs) >= 20,
           "verified_share_ge_0.5": summary["views_verified"] >= 0.5 * max(1, len(refs)),
           "dense_ge_10x_sparse_of_covered_views": summary["dense_verified_points"] >= 10 * max(1, sparse_covered)}
    summary["acceptance"] = {**acc, "result": "PASS" if all(acc.values()) else "FAIL"}

    if diag_frames:  # harness outputs (docs/PLAN_DENSE_COVERAGE.md); not used by the viewer
        dd = out / "diag"
        dd.mkdir(exist_ok=True)
        t_diag = time.monotonic()
        np.savez_compressed(dd / "depth_maps.npz", refs=np.array(ok_refs, np.int32),
                            names=np.array([ims[k].name for k in ok_refs]),
                            **{f"d{k}": views[k].depth.astype(np.float16) for k in ok_refs})
        if kept:
            np.savez_compressed(dd / "raw_kept.npz", P=P.astype(np.float32), ref=np.concatenate([x[2] for x in kept]),
                                u=np.concatenate([x[3] for x in kept]), v=np.concatenate([x[4] for x in kept]),
                                z=np.concatenate([x[5] for x in kept]).astype(np.float32), inv=inv.astype(np.int32))
        verif_sets = {int(k): [int(j) for j in verifier_set(k)] for k in ok_refs}
        forced_rows = []
        for k, nb in forced:
            if not nb:
                forced_rows.append({"ref": k, "frame": ims[k].name, "status": "BLOCKED", "reason": "no neighbour in the baseline/depth range"})
                continue
            v, sp, depth, conf, dg = sweep(k, nb)
            stage[k] = dg
            vv, uu = np.nonzero(conf)
            z = depth[vv, uu]
            Xc = (np.linalg.inv(v.K) @ np.stack([uu, vv, np.ones_like(uu)]).astype(float)) * z
            st = {"Xw": (v.R.T @ (Xc - v.t[:, None])).T}
            vs = verifier_set(k)
            if not vs:
                code = np.full(len(z), V_VIEW_REJECTED, np.int8)
            elif cfg.verify == "next":
                code = _verify_next(st, views[vs[0]])[1]
            else:
                code = _verify_multi(st, [views[j] for j in vs], cfg.min_agree)[1]
            vcodes[k] = code
            forced_rows.append({"ref": k, "frame": ims[k].name, "status": "DIAGNOSTIC_ONLY (not fused)",
                                "neighbours": [j for j, _ in nb], "baseline_over_depth": [round(r, 3) for _, r in nb],
                                "note": "fewer than 2 neighbours: sweep uses the available one" if len(nb) < 2 else ""})
        for k, dg in stage.items():
            conf_map = np.zeros(dg["v0"].shape, bool)
            fr = dg["families"]["fronto"]
            conf_map = fr["conf"] | (dg["families"]["ground"]["conf"] if "ground" in dg["families"] else False)
            if "coarse_fill" in dg:
                conf_map = conf_map | dg["coarse_fill"]
            code_map = np.full(conf_map.shape, -1, np.int8)
            if k in vcodes:
                vv, uu = np.nonzero(conf_map)
                code_map[vv, uu] = vcodes[k]
            fam = {f"{nm}_{key}": a for nm, d in dg["families"].items() for key, a in d.items() if key != "n_planes"}
            np.savez_compressed(dd / f"stage_{ims[k].name}.npz", v0=dg["v0"], chosen=dg["chosen"], conf=conf_map,
                                verify_code=code_map, n_planes=np.array([d["n_planes"] for d in dg["families"].values()]),
                                families=np.array(list(dg["families"])), **fam)
        summary["diagnostics"] = {"frames": list(diag_frames), "forced": forced_rows, "verifier_sets": verif_sets,
                                  "image_names": [im.name for im in ims], "seconds": round(time.monotonic() - t_diag, 1)}
    (out / "dense_report.json").write_text(json.dumps(summary, indent=1))
    return summary


def _write_ply_normals(path: Path, X: np.ndarray, N: np.ndarray, C: np.ndarray) -> None:
    dt = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("nx", "<f4"), ("ny", "<f4"), ("nz", "<f4"),
                   ("red", "u1"), ("green", "u1"), ("blue", "u1")])
    a = np.zeros(len(X), dt)
    a["x"], a["y"], a["z"] = X.T
    a["nx"], a["ny"], a["nz"] = N.T
    a["red"], a["green"], a["blue"] = C.T
    head = ("ply\nformat binary_little_endian 1.0\n" f"element vertex {len(X)}\n"
            "property float x\nproperty float y\nproperty float z\nproperty float nx\nproperty float ny\nproperty float nz\n"
            "property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n")
    path.write_bytes(head.encode() + a.tobytes())


def read_mesh_ply(path: Path):
    """Minimal reader for binary little-endian PLY meshes (vertex float xyz [+ uchar rgb], face list uchar int)."""
    data = path.read_bytes()
    end = data.index(b"end_header\n") + len(b"end_header\n")
    lines = data[:end].decode("latin1").splitlines()
    nv = nf = 0
    vprops, cur = [], None
    cnt_t, idx_t = "u1", "<i4"
    for ln in lines:
        p = ln.split()
        if p[:2] == ["element", "vertex"]:
            nv, cur = int(p[2]), "v"
        elif p[:2] == ["element", "face"]:
            nf, cur = int(p[2]), "f"
        elif p and p[0] == "property" and cur == "v":
            vprops.append((p[2], {"float": "<f4", "double": "<f8", "uchar": "u1", "int": "<i4", "uint": "<u4", "char": "i1"}[p[1]]))
        elif p[:2] == ["property", "list"] and cur == "f":
            cnt_t, idx_t = {"float": "<f4", "double": "<f8", "uchar": "u1", "int": "<i4", "uint": "<u4", "char": "i1"}[p[2]], {"float": "<f4", "double": "<f8", "uchar": "u1", "int": "<i4", "uint": "<u4", "char": "i1"}[p[3]]
    vdt = np.dtype(vprops)
    V = np.frombuffer(data, vdt, nv, end)
    off = end + nv * vdt.itemsize
    F = np.frombuffer(data, np.dtype([("n", cnt_t), ("i", idx_t, (3,))]), nf, off)
    assert np.all(F["n"] == 3), "only triangle meshes are supported"
    return V, F["i"].copy()


def mesh(sd: Path, rank: int = 0, depth: int = 10, max_points: int = 400_000) -> dict:
    """Poisson surface from dense_verified.ply only, trimmed to vertices within 2 voxels of a verified point."""
    out = sd / "dense"
    rep = json.loads((out / "dense_report.json").read_text())
    D = read_ply(out / "dense_verified.ply")
    X = np.stack([D["x"], D["y"], D["z"]], 1).astype(np.float64)
    C = np.stack([D["red"], D["green"], D["blue"]], 1)
    voxel = rep["voxel"]
    if len(X) > max_points:  # coarser voxel grid for meshing input only
        key = np.floor(X / (voxel * 2)).astype(np.int64)
        _, idx = np.unique(key, axis=0, return_index=True)
        X, C = X[idx], C[idx]
    tree = cKDTree(X)
    _, nn = tree.query(X, k=16)
    Q = X[nn] - X[nn].mean(1, keepdims=True)
    cov = np.einsum("nki,nkj->nij", Q, Q)
    w, v = np.linalg.eigh(cov)
    N = v[:, :, 0]  # smallest eigenvalue -> surface normal
    report = json.loads((sd / "map_report.json").read_text())
    models = (report.get("reconstruction") or {}).get("models")
    traj = json.loads(((sd / "models" / str(rank)) if models else sd).joinpath("trajectory.json").read_text())
    cams = np.array([np.array(p["T_world_camera"])[:3, 3] for p in traj["poses"]])
    _, ci = cKDTree(cams).query(X)
    flip = np.einsum("ij,ij->i", N, cams[ci] - X) < 0  # orient towards the nearest camera (the side that was seen)
    N[flip] *= -1
    _write_ply_normals(out / "mesh_input_oriented.ply", X, N, C)
    raw = out / "mesh_poisson_raw.ply"
    # COLMAP's bundled Poisson aborts the whole process ("Failed to close loop") with multiple threads on this
    # machine; it runs single-threaded in a child process so a crash cannot take the caller down.
    import subprocess, sys
    code = ("import pycolmap,sys; o=pycolmap.PoissonMeshingOptions(); o.depth=int(sys.argv[3]); o.num_threads=1; "
            "pycolmap.poisson_meshing(sys.argv[1], sys.argv[2], o)")
    r = subprocess.run([sys.executable, "-c", code, str(out / "mesh_input_oriented.ply"), str(raw), str(depth)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not raw.exists() or raw.stat().st_size == 0:
        res = {"status": "FAILED", "reason": f"poisson exit {r.returncode}: {r.stderr.strip().splitlines()[0] if r.stderr.strip() else ''}"}
        rep["mesh"] = res
        (out / "dense_report.json").write_text(json.dumps(rep, indent=1))
        return res
    V, F = read_mesh_ply(raw)
    P = np.stack([V["x"], V["y"], V["z"]], 1).astype(np.float64)
    dist, _ = tree.query(P)
    keepv = dist <= 2 * voxel
    edge = np.max(np.stack([np.linalg.norm(P[F[:, a]] - P[F[:, b]], axis=1) for a, b in ((0, 1), (1, 2), (2, 0))]), axis=0)
    keepf = keepv[F].all(1) & (edge <= 4 * voxel)  # triangles longer than the observed spacing bridge unobserved gaps
    F2 = F[keepf]
    used = np.unique(F2)
    remap = -np.ones(len(P), np.int64)
    remap[used] = np.arange(len(used))
    V2, F2 = V[used], remap[F2]
    # write trimmed mesh (same vertex layout as the Poisson output)
    head = ["ply", "format binary_little_endian 1.0", f"element vertex {len(V2)}"]
    for name in V2.dtype.names:
        t = {"<f4": "float", "<f8": "double", "|u1": "uchar", "u1": "uchar", "<i4": "int", "<u4": "uint", "|i1": "char"}[V2.dtype[name].str]
        head.append(f"property {t} {name}")
    head += [f"element face {len(F2)}", "property list uchar int vertex_indices", "end_header"]
    fa = np.zeros(len(F2), np.dtype([("n", "u1"), ("i", "<i4", (3,))]))
    fa["n"], fa["i"] = 3, F2.astype(np.int32)
    (out / "mesh_verified.ply").write_bytes(("\n".join(head) + "\n").encode() + np.ascontiguousarray(V2).tobytes() + fa.tobytes())
    res = {"label": "MESH_FROM_VERIFIED_DENSE (relative scale; unobserved areas are holes)", "poisson_depth": depth,
           "input_points": int(len(X)), "raw_vertices": int(len(P)), "raw_faces": int(len(F)),
           "kept_vertices": int(len(V2)), "kept_faces": int(len(F2)), "trim_rule": "vertex within 2 voxels of a verified dense point and every edge <= 4 voxels",
           "kept_face_share": round(float(keepf.mean()), 3)}
    rep["mesh"] = res
    (out / "dense_report.json").write_text(json.dumps(rep, indent=1))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.dense_cpu")
    ap.add_argument("session_dir")
    ap.add_argument("--model", type=int, default=0)
    ap.add_argument("--max-pairs", type=int, default=60)
    ap.add_argument("--mesh-only", action="store_true", help="build the mesh from an existing dense_verified.ply")
    ap.add_argument("--out", help="output directory (defaults to dense_static with masks, otherwise dense)")
    ap.add_argument("--static-masks", type=Path, help="full-resolution binary keep masks, <frame filename>.png")
    ap.add_argument("--t-range", type=float, nargs=2, metavar=("T0", "T1"), help="reference views in this time window only")
    ap.add_argument("--diag-frames", nargs="*", default=[], help="frames to write stage diagnostics for")
    ap.add_argument("--verify", choices=["next", "multi"], default="next")
    ap.add_argument("--min-agree", type=int, default=1)
    ap.add_argument("--sweep", choices=["fronto", "fronto+ground"], default="fronto")
    ap.add_argument("--near-rule", choices=["p2", "p2_or_nearest"], default="p2")
    ap.add_argument("--work-width", type=int, default=WORK_WIDTH)
    ap.add_argument("--voxel-near-scale", type=float, default=1.0)
    ap.add_argument("--neighbour-rule", choices=["median", "median_or_near", "median_then_near", "median_then_near_far"], default="median")
    ap.add_argument("--border-exclusion", action="store_true")
    ap.add_argument("--mask-cost", choices=["erode", "valid_pixels"], default="erode")
    ap.add_argument("--min-valid-frac", type=float, default=0.6)
    ap.add_argument("--neighbours-all-sparse", action="store_true")
    ap.add_argument("--coarse-ground", action="store_true")
    a = ap.parse_args(argv)
    if a.mesh_only:
        if a.static_masks is not None:
            ap.error("--static-masks applies during depth reconstruction, not --mesh-only")
        print(json.dumps(mesh(Path(a.session_dir), a.model), indent=1))
        return 0
    cfg = DenseConfig(work_width=a.work_width, near_rule=a.near_rule, sweep=a.sweep, verify=a.verify,
                      min_agree=a.min_agree, voxel_near_scale=a.voxel_near_scale,
                      neighbour_rule=a.neighbour_rule, border_exclusion=a.border_exclusion, mask_cost=a.mask_cost,
                      min_valid_frac=a.min_valid_frac, neighbours_all_sparse=a.neighbours_all_sparse,
                      coarse_ground=a.coarse_ground)
    s = run(Path(a.session_dir), a.model, a.max_pairs, cfg, Path(a.out) if a.out else None,
            tuple(a.t_range) if a.t_range else None, tuple(a.diag_frames), a.static_masks)
    print(json.dumps({k: v for k, v in s.items() if k not in ("views", "diagnostics")} | {"view_status": [(p["ref"], p["status"], p.get("reason", ""), p.get("confident_share"), p.get("sparse_agreement"), p.get("consistency_kept_share")) for p in s["views"]]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
