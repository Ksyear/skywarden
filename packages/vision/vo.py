"""Minimal monocular visual odometry (VO) for recorded frames — honest, small, standard OpenCV.

Pipeline per processed frame:
  quality gates (scene-change histogram, blur = variance of Laplacian vs running median)
  -> KLT tracking of Shi-Tomasi corners with a forward-backward check
  -> keyframe step when the median parallax since the last keyframe exceeds a threshold:
       essential matrix (RANSAC) + recoverPose AND homography (RANSAC), compared with Torr's GRIC.
       Homography wins or rotation-compensated parallax is tiny -> pure rotation / low parallax:
       no triangulation, rotation-only update, LOW_CONFIDENCE.
       Otherwise triangulate; relative scale = median depth ratio of landmarks shared with earlier steps.
       Too few shared landmarks -> keep previous scale (LOW_CONFIDENCE); scale is never invented.
  -> between keyframes: PnP against the landmarks (or rotation-only homography before initialisation).
Failures (sustained blur, low texture, scene cut, failed geometry) -> LOST, and the next segment starts
with its own coordinate frame. Segments are independent and must never be chained.

Labels: kind=VO, scale=RELATIVE, loop_closure=false, NOT_NAVIGATION_SAFE. No loop closure, no global
optimisation, no relocalisation (RELOCALIZING is reserved and never emitted). Monocular: no metric scale,
no collision-avoidance guarantee. Pure image processing; nothing is sent anywhere.

CLI: python -m vision.vo <video|image_dir> [--fx F] [--calib camera.json] [--max-seconds N] --out out.json
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator

import cv2
import numpy as np

KIND = "VO"
SCALE = "RELATIVE"
NAV_SAFETY = "NOT_NAVIGATION_SAFE"


class State(str, Enum):
    INITIALIZING = "INITIALIZING"
    TRACKING = "TRACKING"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    LOST = "LOST"
    RELOCALIZING = "RELOCALIZING"  # reserved; relocalisation is NOT implemented and never emitted


@dataclass
class VOParams:
    max_corners: int = 500
    min_corners: int = 50            # fewer usable corners -> low texture -> LOST
    corner_quality: float = 0.01
    corner_min_dist: int = 10
    fb_max_px: float = 1.0           # forward-backward KLT consistency
    kf_parallax_frac: float = 0.03   # keyframe when median flow since keyframe > frac * width
    kf_min_track_frac: float = 0.5   # ... or when fewer than this fraction of keyframe tracks survive
    min_rc_parallax_frac: float = 0.006  # rotation-compensated median parallax below -> low parallax
    ransac_px: float = 1.0
    gric_sigma_px: float = 0.8
    min_inliers: int = 30
    min_inlier_ratio: float = 0.5
    min_shared_for_scale: int = 15
    min_tri_angle_deg: float = 0.5
    blur_frac: float = 0.35          # Laplacian variance below frac * running median -> blurred frame
    blur_window: int = 30
    max_blur_run: int = 8            # consecutive blurred frames -> LOST
    hist_corr_cut: float = 0.6       # grayscale-histogram correlation below -> scene change
    survival_cut: float = 0.3        # fraction of tracks surviving one frame below -> scene change
    init_timeout_s: float = 3.0      # no usable parallax for this long -> LOW_CONFIDENCE (static camera?)
    pnp_min_inliers: int = 15
    pnp_px: float = 2.0


def _T(R: np.ndarray, t: np.ndarray) -> np.ndarray:
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = np.ravel(t)
    return T


def _inv(T: np.ndarray) -> np.ndarray:
    R, t = T[:3, :3], T[:3, 3]
    return _T(R.T, -R.T @ t)


def _project_so3(M: np.ndarray) -> np.ndarray:
    U, _, Vt = np.linalg.svd(M)
    R = U @ Vt
    if np.linalg.det(R) < 0:
        R = U @ np.diag([1, 1, -1]) @ Vt
    return R


def rotation_from_homography(H: np.ndarray, K: np.ndarray) -> np.ndarray:
    """Infinite homography H = K R K^-1 (pure rotation); returns nearest rotation R (cam1 -> cam2)."""
    M = np.linalg.inv(K) @ H @ K
    d = np.linalg.det(M)
    if abs(d) > 1e-12:
        M = M / np.cbrt(d)
    return _project_so3(M)


def gric(e2: np.ndarray, sigma: float, d: int, k: int, r: int = 4) -> float:
    """Torr's Geometric Robust Information Criterion; lower is better. d: manifold dim, k: model params."""
    n = len(e2)
    rho = np.minimum(e2 / sigma ** 2, 2.0 * (r - d))
    return float(rho.sum() + np.log(r) * d * n + np.log(r * n) * k)


def _h_err2(H: np.ndarray, p0: np.ndarray, p1: np.ndarray) -> np.ndarray:
    def tf(M, p):
        q = np.c_[p, np.ones(len(p))] @ M.T
        return q[:, :2] / q[:, 2:3]
    with np.errstate(all="ignore"):
        e = 0.5 * (((tf(H, p0) - p1) ** 2).sum(1) + ((tf(np.linalg.inv(H), p1) - p0) ** 2).sum(1))
    return np.nan_to_num(e, nan=1e6, posinf=1e6)


def _sampson2(F: np.ndarray, p0: np.ndarray, p1: np.ndarray) -> np.ndarray:
    x0 = np.c_[p0, np.ones(len(p0))]
    x1 = np.c_[p1, np.ones(len(p1))]
    Fx0 = x0 @ F.T
    Ftx1 = x1 @ F
    num = (x1 * Fx0).sum(1) ** 2
    den = Fx0[:, 0] ** 2 + Fx0[:, 1] ** 2 + Ftx1[:, 0] ** 2 + Ftx1[:, 1] ** 2
    return num / np.maximum(den, 1e-12)


class VisualOdometry:
    def __init__(self, K: np.ndarray, width: int, height: int, params: VOParams | None = None):
        self.K = np.asarray(K, float)
        self.Kinv = np.linalg.inv(self.K)
        self.w, self.h = width, height
        self.p = params or VOParams()
        self.segment = -1
        self.prev_gray: np.ndarray | None = None
        self.prev_hist: np.ndarray | None = None
        self.need_reset = True
        self.lk = dict(winSize=(21, 21), maxLevel=3,
                       criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))
        self.stats = {"keyframe_steps": 0, "rotation_only_steps": 0, "scale_held_steps": 0, "scaled_steps": 0}

    # ---------------------------------------------------------------- segment / tracks
    def _detect(self, gray: np.ndarray, existing: np.ndarray | None, n: int) -> np.ndarray:
        if n <= 0:
            return np.empty((0, 2), np.float32)
        mask = np.full(gray.shape, 255, np.uint8)
        if existing is not None:
            for x, y in existing:
                cv2.circle(mask, (int(x), int(y)), self.p.corner_min_dist, 0, -1)
        c = cv2.goodFeaturesToTrack(gray, n, self.p.corner_quality, self.p.corner_min_dist, mask=mask)
        return np.empty((0, 2), np.float32) if c is None else c.reshape(-1, 2).astype(np.float32)

    def _start_segment(self, gray: np.ndarray, t: float) -> bool:
        pts = self._detect(gray, None, self.p.max_corners)
        if len(pts) < self.p.min_corners:
            return False
        self.segment += 1
        self.pts = pts
        self.ids = np.arange(len(pts))
        self.next_id = len(pts)
        self.kfp = pts.copy()
        self.n_at_kf = len(pts)
        self.T_w_kf = np.eye(4)
        self.landmarks: dict[int, np.ndarray] = {}
        self.initialized = False   # True once a translation with triangulation has been accepted
        self.rot_only = False      # True after rotation-only steps before initialisation
        self.state = State.INITIALIZING
        self.scale = None
        self.seg_t0 = t
        self.lap_hist: list[float] = []
        self.blur_run = 0
        self.prev_gray = gray
        self.need_reset = False
        return True

    def _add_tracks(self, gray: np.ndarray, at_keyframe: bool) -> None:
        new = self._detect(gray, self.pts, self.p.max_corners - len(self.pts))
        if len(new):
            self.pts = np.vstack([self.pts, new])
            self.ids = np.r_[self.ids, np.arange(self.next_id, self.next_id + len(new))]
            self.next_id += len(new)
            kf = new.copy() if at_keyframe else np.full_like(new, np.nan)
            self.kfp = np.vstack([self.kfp, kf])

    # ---------------------------------------------------------------- main entry
    def process(self, t: float, bgr: np.ndarray) -> dict:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
        small = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA)
        hist = cv2.calcHist([small], [0], None, [32], [0, 256])
        cv2.normalize(hist, hist)
        lap = float(cv2.Laplacian(gray, cv2.CV_32F).var())
        prev_hist, self.prev_hist = self.prev_hist, hist

        if self.need_reset:
            if self._start_segment(gray, t):
                self.lap_hist.append(lap)
                return self._rec(t, State.INITIALIZING, np.eye(4), 0, 0.0, "segment origin (identity by definition)")
            return self._rec(t, State.LOST, None, 0, 0.0, "low texture: too few corners to start a segment")

        if prev_hist is not None:
            corr = float(cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL))
            if corr < self.p.hist_corr_cut:
                return self._lost(t, gray, lap, f"scene change: histogram correlation {corr:.2f}")

        if len(self.lap_hist) >= 5 and lap < self.p.blur_frac * float(np.median(self.lap_hist)):
            self.blur_run += 1
            if self.blur_run > self.p.max_blur_run:
                return self._lost(t, gray, lap, f"sustained blur ({self.blur_run} frames)")
            return self._rec(t, State.LOW_CONFIDENCE if self.state != State.INITIALIZING else self.state, None, 0,
                             0.0, f"blur: Laplacian var {lap:.0f} < {self.p.blur_frac} x median; frame skipped")
        self.blur_run = 0
        self.lap_hist = (self.lap_hist + [lap])[-self.p.blur_window:]

        # KLT with forward-backward check
        n_before = len(self.pts)
        p1, st, _ = cv2.calcOpticalFlowPyrLK(self.prev_gray, gray, self.pts.reshape(-1, 1, 2), None, **self.lk)
        p0r, st2, _ = cv2.calcOpticalFlowPyrLK(gray, self.prev_gray, p1, None, **self.lk)
        p1, p0r = p1.reshape(-1, 2), p0r.reshape(-1, 2)
        fb = np.linalg.norm(self.pts - p0r, axis=1)
        inside = (p1[:, 0] >= 0) & (p1[:, 1] >= 0) & (p1[:, 0] < self.w) & (p1[:, 1] < self.h)
        good = (st.ravel() == 1) & (st2.ravel() == 1) & (fb < self.p.fb_max_px) & inside
        survival = good.sum() / max(n_before, 1)
        if n_before >= 100 and survival < self.p.survival_cut:
            return self._lost(t, gray, lap, f"scene change: only {survival:.0%} of tracks survived one frame")
        self.pts, self.ids, self.kfp = p1[good], self.ids[good], self.kfp[good]
        self.prev_gray = gray
        if len(self.pts) < self.p.min_corners:
            self._add_tracks(gray, at_keyframe=False)
            if len(self.pts) < self.p.min_corners:
                return self._lost(t, gray, lap, f"low texture: {len(self.pts)} trackable corners")

        has_kf = ~np.isnan(self.kfp[:, 0])
        n_kf = int(has_kf.sum())
        par = float(np.median(np.linalg.norm(self.pts[has_kf] - self.kfp[has_kf], axis=1))) if n_kf else 0.0
        want_kf = par > self.p.kf_parallax_frac * self.w or n_kf < max(self.p.min_corners,
                                                                       self.p.kf_min_track_frac * self.n_at_kf)
        if want_kf:
            return self._keyframe_step(t, gray, lap, has_kf, par)
        return self._between_keyframes(t, has_kf, par)

    # ---------------------------------------------------------------- keyframe step
    def _keyframe_step(self, t, gray, lap, has_kf, par) -> dict:
        p0, p1, ids = self.kfp[has_kf].astype(np.float64), self.pts[has_kf].astype(np.float64), self.ids[has_kf]
        n = len(p0)
        if n < self.p.min_inliers:
            return self._lost(t, gray, lap, f"too few tracks since keyframe ({n}) for geometric verification")
        E, me = cv2.findEssentialMat(p0, p1, self.K, cv2.RANSAC, 0.999, self.p.ransac_px)
        H, mh = cv2.findHomography(p0, p1, cv2.RANSAC, self.p.ransac_px * 2)
        if E is None or E.shape[0] < 3 or H is None:
            return self._lost(t, gray, lap, "geometric verification failed (no E/H model)")
        E = E[:3]
        inl_e = me.ravel() > 0
        n_e = int(inl_e.sum())
        if n_e < self.p.min_inliers or n_e / n < self.p.min_inlier_ratio:
            return self._lost(t, gray, lap, f"geometric verification failed: E inliers {n_e}/{n}")
        F = self.Kinv.T @ E @ self.Kinv
        g_h = gric(_h_err2(H, p0, p1), self.p.gric_sigma_px, d=2, k=8)
        g_e = gric(_sampson2(F, p0, p1), self.p.gric_sigma_px, d=3, k=5)
        _, R, tu, mp = cv2.recoverPose(E, p0[inl_e], p1[inl_e], self.K)
        # rotation-compensated parallax: median residual of the best pure-rotation model K R K^-1
        # (R from the homography or from E). ~0 for pure rotation; grows with translation x inverse depth.
        def rot_residual(Rm):
            q = np.c_[p0[inl_e], np.ones(n_e)] @ (self.K @ Rm @ self.Kinv).T
            return float(np.median(np.linalg.norm(q[:, :2] / q[:, 2:3] - p1[inl_e], axis=1)))
        R_h = rotation_from_homography(H, self.K)
        rc_h, rc_e = rot_residual(R_h), rot_residual(R)
        rc_par, R_rot = (rc_h, R_h) if rc_h <= rc_e else (rc_e, R)
        h_pref = g_h <= g_e
        info = f"E inl {n_e}/{n}, H inl {int(mh.sum())}/{n}, GRIC H {g_h:.0f} vs E {g_e:.0f}, rc-parallax {rc_par:.1f}px"
        low_par = rc_par < self.p.min_rc_parallax_frac * self.w
        drained = n < max(self.p.min_corners, self.p.kf_min_track_frac * self.n_at_kf)
        big_rot = par > 3 * self.p.kf_parallax_frac * self.w
        if low_par and not (drained or big_rot):
            # Low parallax but tracks are still fine: defer the keyframe so the baseline can grow instead of
            # silently dropping the small translation (no new keyframe, no triangulation).
            rec = self._between_keyframes(t, has_kf, par)
            rec["reason"] = f"keyframe deferred (low parallax: {info}); " + rec["reason"]
            return rec
        self.stats["keyframe_steps"] += 1

        if low_par:
            T_rel = _T(R_rot, np.zeros(3))  # cam_kf -> cam_new, no translation (unobservable)
            self.stats["rotation_only_steps"] += 1
            why = ("homography explains motion" if h_pref else "E preferred but") + \
                  " and pure-rotation model residual is tiny"
            state, reason = State.LOW_CONFIDENCE, \
                f"pure rotation / low parallax ({why}); rotation only, no triangulation, translation NOT updated; {info}"
            if not self.initialized:
                self.rot_only = True
            new_landmarks = {}
        else:
            P0 = self.K @ np.eye(3, 4)
            P1 = self.K @ np.c_[R, tu]
            ok = inl_e.copy()
            ok[inl_e] = mp.ravel() > 0
            X = cv2.triangulatePoints(P0, P1, p0[ok].T, p1[ok].T)
            X = (X[:3] / X[3]).T  # in keyframe camera frame, unit baseline
            c2 = -R.T @ tu.ravel()
            r0 = X / np.linalg.norm(X, axis=1, keepdims=True)
            r1 = (X - c2) / np.linalg.norm(X - c2, axis=1, keepdims=True)
            ang = np.degrees(np.arccos(np.clip((r0 * r1).sum(1), -1, 1)))
            z2 = (X @ R.T + tu.ravel())[:, 2]
            valid = (X[:, 2] > 0) & (z2 > 0) & (ang > self.p.min_tri_angle_deg)
            X, vid = X[valid], ids[ok][valid]
            if not self.initialized:
                s, state, reason = 1.0, State.TRACKING, f"initialised: unit baseline defines RELATIVE scale; {info}"
                self.initialized = True
            else:
                T_kf_w = _inv(self.T_w_kf)
                ratios = []
                for i, x in zip(vid, X):
                    Lw = self.landmarks.get(int(i))
                    if Lw is not None:
                        zp = (T_kf_w[:3, :3] @ Lw + T_kf_w[:3, 3])[2]
                        if zp > 0:
                            ratios.append(zp / x[2])
                if len(ratios) >= self.p.min_shared_for_scale:
                    s = float(np.median(ratios))
                    state, reason = State.TRACKING, f"scale from {len(ratios)} shared landmarks; {info}"
                    self.stats["scaled_steps"] += 1
                else:
                    s = self.scale
                    state = State.LOW_CONFIDENCE
                    reason = f"only {len(ratios)} shared landmarks: previous scale kept (not re-estimated); {info}"
                    self.stats["scale_held_steps"] += 1
            if h_pref and state == State.TRACKING:
                state = State.LOW_CONFIDENCE
                reason = "homography competitive although motion is not a pure rotation (planar-dominant scene / " \
                         "weak parallax): pose from E kept but flagged; " + reason
            self.scale = s
            T_rel = _T(R, s * tu.ravel())
            new_landmarks = {int(i): self.T_w_kf[:3, :3] @ (s * x) + self.T_w_kf[:3, 3] for i, x in zip(vid, X)}

        self.T_w_kf = self.T_w_kf @ _inv(T_rel)
        alive = set(int(i) for i in self.ids)
        self.landmarks = {i: v for i, v in self.landmarks.items() if i in alive}
        self.landmarks.update(new_landmarks)
        self.state = state
        self.kfp = self.pts.copy()
        self._add_tracks(gray, at_keyframe=True)
        self.n_at_kf = len(self.pts)
        return self._rec(t, state, self.T_w_kf.copy(), n_e, par, "keyframe: " + reason, keyframe=True)

    # ---------------------------------------------------------------- non-keyframes
    def _between_keyframes(self, t, has_kf, par) -> dict:
        if self.initialized:
            sel = [k for k, i in enumerate(self.ids) if int(i) in self.landmarks]
            if len(sel) >= self.p.pnp_min_inliers:
                obj = np.array([self.landmarks[int(self.ids[k])] for k in sel], np.float64)
                img = self.pts[sel].astype(np.float64)
                T_kf_w = _inv(self.T_w_kf)
                rvec, _ = cv2.Rodrigues(T_kf_w[:3, :3])
                ok, rvec, tvec, inl = cv2.solvePnPRansac(obj, img, self.K, None, rvec, T_kf_w[:3, 3].copy(),
                                                         useExtrinsicGuess=True, iterationsCount=100,
                                                         reprojectionError=self.p.pnp_px, confidence=0.99)
                n_in = 0 if inl is None else len(inl)
                if ok and n_in >= self.p.pnp_min_inliers:
                    T_c_w = _T(cv2.Rodrigues(rvec)[0], tvec)
                    return self._rec(t, self.state, _inv(T_c_w), n_in, par, f"PnP on {n_in}/{len(sel)} landmarks")
            return self._rec(t, State.LOW_CONFIDENCE, None, 0, par, f"PnP not possible ({len(sel)} landmarks visible)")
        timed_out = t - self.seg_t0 > self.p.init_timeout_s
        if self.rot_only or timed_out:
            n_kf = int(has_kf.sum())
            if n_kf >= 8:
                H, mh = cv2.findHomography(self.kfp[has_kf], self.pts[has_kf], cv2.RANSAC, self.p.ransac_px * 2)
                if H is not None:
                    T = self.T_w_kf @ _inv(_T(rotation_from_homography(H, self.K), np.zeros(3)))
                    why = "after pure-rotation steps" if self.rot_only else \
                        f"no usable parallax for {t - self.seg_t0:.1f}s (static camera?)"
                    return self._rec(t, State.LOW_CONFIDENCE, T, int(mh.sum()), par,
                                     f"rotation-only pose from homography {why}; translation unobservable")
            return self._rec(t, State.LOW_CONFIDENCE, None, 0, par, "rotation-only and homography failed")
        return self._rec(t, State.INITIALIZING, None, 0, par, "waiting for parallax")

    # ---------------------------------------------------------------- helpers
    def _lost(self, t, gray, lap, reason) -> dict:
        rec = self._rec(t, State.LOST, None, 0, 0.0, reason)
        self.need_reset = True
        if self._start_segment(gray, t):
            self.lap_hist.append(lap)
            rec["reason"] += f"; this frame is the origin of new segment {self.segment}"
        return rec

    def _rec(self, t, state, T, inliers, parallax, reason, keyframe=False) -> dict:
        return {"t": round(float(t), 4), "state": state.value, "segment": int(self.segment),
                "T_world_camera": None if T is None else np.round(T, 6).tolist(),
                "inliers": int(inliers), "parallax_px": round(float(parallax), 2), "keyframe": keyframe,
                "tracks": int(len(getattr(self, "pts", []))), "reason": reason}


# -------------------------------------------------------------------- inputs
def iter_video(path: Path, work_width: int = 640, stride: int | None = None, max_seconds: float | None = None,
               start_seconds: float = 0.0, target_fps: float = 15.0) -> tuple[dict, Iterator[tuple[float, np.ndarray]]]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise FileNotFoundError(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    stride = stride or max(1, int(round(fps / target_fps)))
    ww = min(work_width, W)
    wh = int(round(H * ww / W))
    meta = {"fps": fps, "native_size": [W, H], "work_size": [ww, wh], "stride": stride,
            "start_seconds": start_seconds, "max_seconds": max_seconds}
    if start_seconds:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(start_seconds * fps)))

    def gen():
        idx = int(round(start_seconds * fps))
        while True:
            ok = cap.grab()
            if not ok:
                break
            t = idx / fps  # seconds from the start of the file
            if max_seconds is not None and t - start_seconds > max_seconds:
                break
            if (idx - int(round(start_seconds * fps))) % stride == 0:
                ok, f = cap.retrieve()
                if not ok:
                    break
                yield t, (cv2.resize(f, (ww, wh), interpolation=cv2.INTER_AREA) if ww != W else f)
            idx += 1
        cap.release()
    return meta, gen()


def iter_image_dir(path: Path) -> tuple[dict, Iterator[tuple[float, np.ndarray]]]:
    d = Path(path)
    d = d / "rgb" if (d / "rgb").is_dir() else d
    files = sorted(d.glob("*.png")) + sorted(d.glob("*.jpg"))
    files.sort(key=lambda f: float(f.stem) if f.stem.replace(".", "", 1).isdigit() else f.stem)
    first = cv2.imread(str(files[0]))
    meta = {"native_size": [first.shape[1], first.shape[0]], "work_size": [first.shape[1], first.shape[0]],
            "stride": 1, "frames": len(files)}

    def gen():
        for i, f in enumerate(files):
            try:
                t = float(f.stem)
            except ValueError:
                t = float(i)
            yield t, cv2.imread(str(f))
    return meta, gen()


def intrinsics(work_size, native_size, fx=None, fy=None, cx=None, cy=None, calib: Path | None = None):
    """Return (K at working resolution, provenance dict). Never silently pretends to be calibrated."""
    ww, wh = work_size
    W = native_size[0]
    if calib is not None:
        c = json.loads(Path(calib).read_text())
        cam = c.get("camera", c)
        params = cam.get("params_refined") or cam.get("params")
        model = cam.get("model", "PINHOLE")
        if model in ("SIMPLE_PINHOLE", "SIMPLE_RADIAL", "RADIAL"):
            f_, fy_, cx_, cy_ = params[0], params[0], params[1], params[2]
        else:
            f_, fy_, cx_, cy_ = params[:4]
        s = ww / float(cam.get("width", W))
        K = np.array([[f_ * s, 0, cx_ * s], [0, fy_ * s, cy_ * s], [0, 0, 1]])
        src = {"source": "CALIBRATION_FILE", "file": str(calib), "model": model,
               "note": "distortion ignored (pinhole only)" if model != "PINHOLE" else "pinhole"}
    elif fx is not None:
        K = np.array([[fx, 0, ww / 2 if cx is None else cx], [0, fy or fx, wh / 2 if cy is None else cy], [0, 0, 1]])
        src = {"source": "USER_GIVEN_FX", "note": "given at working resolution"}
    else:
        f_ = 0.9 * ww
        K = np.array([[f_, 0, ww / 2], [0, f_, wh / 2], [0, 0, 1]])
        src = {"source": "ESTIMATED", "note": "fx = fy = 0.9 x width, principal point at centre (UNCALIBRATED)"}
    return K, {**src, "fx": round(K[0, 0], 3), "fy": round(K[1, 1], 3), "cx": round(K[0, 2], 3),
               "cy": round(K[1, 2], 3), "at_size": [ww, wh]}


# -------------------------------------------------------------------- run + summary
def run(frames: Iterable[tuple[float, np.ndarray]], K: np.ndarray, size, params: VOParams | None = None) -> dict:
    vo = VisualOdometry(K, size[0], size[1], params)
    recs, ms = [], []
    for t, img in frames:
        t0 = time.perf_counter()
        r = vo.process(t, img)
        ms.append((time.perf_counter() - t0) * 1000)
        r["ms"] = round(ms[-1], 2)
        recs.append(r)
    return {"frames": recs, "summary": summarize(recs, ms, vo.stats), "params": asdict(vo.p)}


def summarize(recs: list[dict], ms: list[float], stats: dict) -> dict:
    counts = {s.value: 0 for s in State}
    for r in recs:
        counts[r["state"]] += 1
    segs = {}
    for r in recs:
        s = segs.setdefault(r["segment"], {"segment": r["segment"], "t_start": r["t"], "t_end": r["t"], "frames": 0,
                                           "poses": 0, "tracking_frames": 0, "keyframes": 0, "_c": []})
        s["t_end"] = r["t"]
        s["frames"] += 1
        s["keyframes"] += int(r["keyframe"])
        s["tracking_frames"] += int(r["state"] == "TRACKING")
        if r["T_world_camera"] is not None:
            s["poses"] += 1
            s["_c"].append(np.array(r["T_world_camera"])[:3, 3])
    for s in segs.values():
        c = np.array(s.pop("_c")) if s["_c"] else np.zeros((1, 3))
        s["path_length_relative"] = round(float(np.linalg.norm(np.diff(c, axis=0), axis=1).sum()), 4)
        s["max_dist_from_origin_relative"] = round(float(np.linalg.norm(c, axis=1).max()), 4)
    a = np.array(ms) if ms else np.zeros(1)
    return {"processed_frames": len(recs), "state_counts": counts, "num_segments": len(segs),
            "segments": list(segs.values()), "steps": stats,
            "timing_ms_per_frame": {"p50": round(float(np.percentile(a, 50)), 2),
                                    "p95": round(float(np.percentile(a, 95)), 2),
                                    "mean": round(float(a.mean()), 2),
                                    "note": "VO processing only (decode/resize excluded)"}}


def header() -> dict:
    return {"schema": "skywarden/vo@1", "kind": KIND, "scale": SCALE, "loop_closure": False,
            "relocalization": "NOT_IMPLEMENTED", "nav_safety": NAV_SAFETY,
            "coordinate_frame": "per segment: origin = first frame of the segment; camera axes x right, y down, "
                                "z forward; T_world_camera is camera-to-world. Segments are independent frames "
                                "and must never be chained.",
            "warning": "Monocular VO: relative scale only, drifts, no loop closure; not for navigation or collision "
                       "avoidance."}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Monocular VO on a recorded video or image folder (offline).")
    ap.add_argument("input")
    ap.add_argument("--fx", type=float)
    ap.add_argument("--fy", type=float)
    ap.add_argument("--cx", type=float)
    ap.add_argument("--cy", type=float)
    ap.add_argument("--calib", type=Path, help="camera.json / trajectory.json with intrinsics at native resolution")
    ap.add_argument("--work-width", type=int, default=640)
    ap.add_argument("--stride", type=int)
    ap.add_argument("--start-seconds", type=float, default=0.0)
    ap.add_argument("--max-seconds", type=float)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    cv2.setNumThreads(a.threads)
    src = Path(a.input)
    meta, frames = iter_image_dir(src) if src.is_dir() else iter_video(src, a.work_width, a.stride, a.max_seconds,
                                                                       a.start_seconds)
    K, ksrc = intrinsics(meta["work_size"], meta["native_size"], a.fx, a.fy, a.cx, a.cy, a.calib)
    t0 = time.perf_counter()
    res = run(frames, K, meta["work_size"])
    wall = time.perf_counter() - t0
    out = {**header(), "input": str(src), "video": meta, "intrinsics": ksrc, **res}
    out["summary"]["wall_s_including_decode"] = round(wall, 2)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out))
    print(json.dumps({"out": str(a.out), "intrinsics": ksrc, **out["summary"]}, indent=1)[:4000])


if __name__ == "__main__":
    main()
