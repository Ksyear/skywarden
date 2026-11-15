"""Global (camera) motion compensation between consecutive frames: sparse optical flow + RANSAC partial affine.

Follows the camera-motion step of BoT-SORT (Aharon et al., arXiv 2206.14651): corners outside the person boxes are
tracked from the previous frame with pyramidal Lucas-Kanade, and a 4-dof similarity (rotation, uniform scale,
translation) is fitted with RANSAC. The tracker applies it to every track after the Kalman prediction.

The estimate is refused (identity, reason recorded) when the background has too little texture or the fit is not
trustworthy: sky, water, motion blur and shot cuts give few corners or a low inlier ratio, and a wrong warp would
move every track at once. A refused frame simply falls back to the uncompensated prediction.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class GmcConfig:
    max_side: int = 480            # estimate on a downscaled grey image (long side, px)
    max_corners: int = 400
    min_features: int = 40         # fewer tracked corners than this -> LOW_TEXTURE
    min_inlier_ratio: float = 0.5  # RANSAC inliers / tracked corners
    ransac_px: float = 1.5         # reprojection threshold in downscaled px
    max_rot_deg: float = 10.0      # per frame; more is treated as a cut or a failed fit
    max_scale_change: float = 0.10


class GlobalMotion:
    """Stateful: call estimate() once per processed frame, in order. Returns a 2x3 affine in full-resolution pixels
    that maps previous-frame coordinates to current-frame coordinates, or None with a reason."""

    def __init__(self, cfg: GmcConfig | None = None):
        self.cfg = cfg or GmcConfig()
        self.prev: np.ndarray | None = None
        self.prev_mask: np.ndarray | None = None
        self.scale = 1.0
        self.last_reason = "FIRST_FRAME"

    def reset(self) -> None:
        self.prev = self.prev_mask = None

    def _prep(self, frame: np.ndarray, boxes: list[tuple[float, float, float, float]]) -> tuple[np.ndarray, np.ndarray, float]:
        h, w = frame.shape[:2]
        s = min(1.0, self.cfg.max_side / max(h, w))
        g = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if s < 1.0:
            g = cv2.resize(g, (int(round(w * s)), int(round(h * s))), interpolation=cv2.INTER_AREA)
        mask = np.full(g.shape, 255, np.uint8)
        for x1, y1, x2, y2 in boxes:  # people move on their own: never use their pixels for the camera motion
            mask[max(0, int(y1 * s)):int(np.ceil(y2 * s)), max(0, int(x1 * s)):int(np.ceil(x2 * s))] = 0
        return g, mask, s

    def estimate(self, frame: np.ndarray, boxes: list[tuple[float, float, float, float]]) -> tuple[np.ndarray | None, str]:
        g, mask, s = self._prep(frame, boxes)
        prev, prev_mask, prev_s = self.prev, self.prev_mask, self.scale
        self.prev, self.prev_mask, self.scale = g, mask, s
        if prev is None or prev.shape != g.shape or prev_s != s:
            self.last_reason = "FIRST_FRAME"
            return None, self.last_reason
        p0 = cv2.goodFeaturesToTrack(prev, self.cfg.max_corners, 0.01, 8, mask=prev_mask, blockSize=3)
        if p0 is None or len(p0) < self.cfg.min_features:
            self.last_reason = "LOW_TEXTURE"
            return None, self.last_reason
        p1, st, _ = cv2.calcOpticalFlowPyrLK(prev, g, p0, None, winSize=(21, 21), maxLevel=3)
        ok = st.reshape(-1) == 1
        a, b = p0.reshape(-1, 2)[ok], p1.reshape(-1, 2)[ok]
        if len(a) < self.cfg.min_features:
            self.last_reason = "LOW_TEXTURE"
            return None, self.last_reason
        M, inl = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC, ransacReprojThreshold=self.cfg.ransac_px,
                                             maxIters=2000, confidence=0.99)
        if M is None or inl is None or inl.sum() / len(a) < self.cfg.min_inlier_ratio:
            self.last_reason = "LOW_INLIERS"
            return None, self.last_reason
        scale = float(np.sqrt(abs(np.linalg.det(M[:, :2]))))
        rot = float(np.degrees(np.arctan2(M[1, 0], M[0, 0])))
        if abs(rot) > self.cfg.max_rot_deg or abs(scale - 1.0) > self.cfg.max_scale_change:
            self.last_reason = "IMPLAUSIBLE"
            return None, self.last_reason
        A = M.astype(np.float64).copy()
        A[:, 2] /= s  # same linear part, translation back to full-resolution pixels
        self.last_reason = "OK"
        return A, self.last_reason
