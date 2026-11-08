"""Shot (continuous-take) detection for edited videos, plus a camera-motion measurement per shot.

Edited YouTube clips contain hard cuts; frames on both sides of a cut show different places and must never be
treated as consecutive views. A cut is flagged between two consecutive source frames (64x48 thumbnails) when
  (a) the HSV histogram correlation drops below CUT_HIST_CORR AND the mean absolute grey difference exceeds CUT_MAD, or
  (b) the grey difference jumps above CUT_MAD_MIN and above CUT_SPIKE x the median difference of the previous 0.5 s
      (inside one take consecutive frames at video rate are nearly identical; a hard cut between two similar-looking
      scenes keeps the colour histogram but breaks this continuity).
Camera motion inside a shot is the median sparse optical-flow magnitude between frames ~0.2 s apart (pixels per
source frame at 640 px width); below STATIC_FLOW_PX the camera is treated as static (good for tracking, unsuitable
for structure from motion).

python -m mapping.shots <video> [--out shots.json]
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

CUT_HIST_CORR = 0.5
CUT_MAD = 40.0
CUT_MAD_MIN = 15.0
CUT_SPIKE = 4.0
MIN_SHOT_S = 1.0
STATIC_FLOW_PX = 0.1  # px per source frame at 640 px width; slow aerial pans move 0.2-0.5, a tripod 0.006
FLOW_WIDTH = 640


@dataclass
class Shot:
    index: int
    start_frame: int
    end_frame: int  # exclusive
    start_s: float
    end_s: float
    duration_s: float
    cut_hist_corr: float | None  # values at the cut that STARTS this shot (None for the first shot)
    cut_mad: float | None
    median_flow_px_per_frame: float | None = None
    camera_motion: str | None = None  # "moving" | "static"


def _thumb(img: np.ndarray):
    small = cv2.resize(img, (64, 48), interpolation=cv2.INTER_AREA)
    grey = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
    hist = cv2.calcHist([cv2.cvtColor(small, cv2.COLOR_BGR2HSV)], [0, 1], None, [32, 8], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return grey, hist


def _flow_magnitude(a: np.ndarray, b: np.ndarray) -> float | None:
    """Median LK flow (px at FLOW_WIDTH) between two BGR frames, or None without trackable corners."""
    s = FLOW_WIDTH / a.shape[1]
    ga = cv2.cvtColor(cv2.resize(a, None, fx=s, fy=s), cv2.COLOR_BGR2GRAY)
    gb = cv2.cvtColor(cv2.resize(b, None, fx=s, fy=s), cv2.COLOR_BGR2GRAY)
    pts = cv2.goodFeaturesToTrack(ga, 300, 0.01, 8)
    if pts is None or len(pts) < 20:
        return None
    nxt, st, _ = cv2.calcOpticalFlowPyrLK(ga, gb, pts, None)
    ok = st.ravel() == 1
    if ok.sum() < 20:
        return None
    return float(np.median(np.linalg.norm((nxt - pts)[ok].reshape(-1, 2), axis=1)))


def detect_shots(video: str | Path, flow_samples: int = 25) -> tuple[list[Shot], dict]:
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise ValueError(f"cannot open {video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cuts: list[tuple[int, float, float]] = []
    prev = None
    recent: list[float] = []
    win = max(3, int(round(0.5 * fps)))
    k = 0
    while True:
        ok, img = cap.read()
        if not ok:
            break
        grey, hist = _thumb(img)
        if prev is not None:
            corr = float(cv2.compareHist(prev[1], hist, cv2.HISTCMP_CORREL))
            mad = float(np.mean(np.abs(prev[0] - grey)))
            base = float(np.median(recent)) if recent else None
            spike = base is not None and mad > CUT_MAD_MIN and mad > CUT_SPIKE * max(base, 1.0)
            if (corr < CUT_HIST_CORR and mad > CUT_MAD) or spike:
                cuts.append((k, corr, mad))
                recent = []  # the new shot starts its own baseline
            else:
                recent = (recent + [mad])[-win:]
        prev = (grey, hist)
        k += 1
    cap.release()
    n = k
    bounds = [0] + [c[0] for c in cuts] + [n]
    shots: list[Shot] = []
    for i, (a, b) in enumerate(zip(bounds, bounds[1:])):
        c = cuts[i - 1] if i > 0 else None
        shots.append(Shot(len(shots), a, b, round(a / fps, 3), round(b / fps, 3), round((b - a) / fps, 3),
                          None if c is None else round(c[1], 3), None if c is None else round(c[2], 1)))
    # merge shots shorter than MIN_SHOT_S into nothing: they stay as their own (tiny) shots but are flagged by length
    # camera motion per shot from a few frame pairs ~0.2 s apart
    gap = max(1, int(round(0.2 * fps)))
    cap = cv2.VideoCapture(str(video))
    for sh in shots:
        if sh.end_frame - sh.start_frame <= gap:
            continue
        starts = np.linspace(sh.start_frame, sh.end_frame - gap - 1, min(flow_samples, sh.end_frame - sh.start_frame - gap)).astype(int)
        mags = []
        for s0 in np.unique(starts):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(s0))
            ok1, a = cap.read()
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(s0 + gap))
            ok2, b = cap.read()
            if ok1 and ok2:
                m = _flow_magnitude(a, b)
                if m is not None:
                    mags.append(m / gap)
        if mags:
            sh.median_flow_px_per_frame = round(float(np.median(mags)), 3)
            sh.camera_motion = "static" if sh.median_flow_px_per_frame < STATIC_FLOW_PX else "moving"
    cap.release()
    meta = {"video": str(video), "fps": fps, "frames": n, "duration_s": round(n / fps, 3), "cuts": len(cuts),
            "rule": {"cut_hist_corr_lt": CUT_HIST_CORR, "cut_mad_gt": CUT_MAD, "or_mad_spike": {"min": CUT_MAD_MIN, "x_median_prev_0.5s": CUT_SPIKE}, "static_flow_px_per_frame_lt": STATIC_FLOW_PX,
                     "flow_width_px": FLOW_WIDTH}}
    return shots, meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.shots")
    ap.add_argument("video")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    shots, meta = detect_shots(a.video)
    doc = {**meta, "shots": [asdict(s) for s in shots]}
    if a.out:
        Path(a.out).write_text(json.dumps(doc, indent=1))
    longest = sorted((s for s in shots if s.camera_motion == "moving"), key=lambda s: -s.duration_s)[:5]
    print(json.dumps({**meta, "shots_total": len(shots), "longest_moving": [asdict(s) for s in longest]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
