"""Transient clothing-colour descriptor for one person box (non-biometric).

Only the torso and leg regions are used; the head region (where a face would be) is excluded on purpose. The
descriptor is an L1-normalised HSV colour histogram per region, compared with the Hellinger distance. It is meant to
live in memory for the current session only, to tell two nearby people apart; it is not an identity and is never
written to disk by the tracker.
"""
from __future__ import annotations

import cv2
import numpy as np

# Regions as fractions of the box (x0, x1, y0, y1). The top 20 % (head) is never sampled.
TORSO = (0.25, 0.75, 0.22, 0.50)
LEGS = (0.30, 0.70, 0.58, 0.90)
H_BINS, S_BINS, V_BINS = 12, 4, 6
MIN_REGION_PX = 6


def _region(img: np.ndarray, bbox, frac) -> np.ndarray | None:
    x1, y1, x2, y2 = bbox
    w, h = x2 - x1, y2 - y1
    xa, xb = int(round(x1 + frac[0] * w)), int(round(x1 + frac[1] * w))
    ya, yb = int(round(y1 + frac[2] * h)), int(round(y1 + frac[3] * h))
    H, W = img.shape[:2]
    xa, xb, ya, yb = max(0, xa), min(W, xb), max(0, ya), min(H, yb)
    if xb - xa < MIN_REGION_PX or yb - ya < MIN_REGION_PX:
        return None
    return img[ya:yb, xa:xb]


def _hist(region_bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(region_bgr, cv2.COLOR_BGR2HSV)
    # chromatic pixels: hue x saturation; dark / grey pixels: value only (hue is meaningless there)
    chroma = cv2.calcHist([hsv], [0, 1], cv2.inRange(hsv, (0, 40, 40), (180, 255, 255)), [H_BINS, S_BINS], [0, 180, 40, 256])
    grey = cv2.calcHist([hsv], [2], cv2.bitwise_not(cv2.inRange(hsv, (0, 40, 40), (180, 255, 255))), [V_BINS], [0, 256])
    v = np.concatenate([chroma.ravel(), grey.ravel()]).astype(np.float64)
    s = v.sum()
    return v / s if s > 0 else v


def clothing_descriptor(image_bgr: np.ndarray, bbox) -> np.ndarray | None:
    """Concatenated torso + legs histograms, or None when the box is too small to describe."""
    parts = [_region(image_bgr, bbox, f) for f in (TORSO, LEGS)]
    if any(p is None for p in parts):
        return None
    return np.concatenate([_hist(p) for p in parts])


def appearance_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Mean Hellinger distance over the two regions, in [0, 1] (0 = identical colour distributions)."""
    n = len(a) // 2
    d = []
    for p, q in ((a[:n], b[:n]), (a[n:], b[n:])):
        bc = float(np.sum(np.sqrt(p * q)))
        d.append(np.sqrt(max(0.0, 1.0 - bc)))
    return float(np.mean(d))
