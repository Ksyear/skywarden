"""Pick SfM-friendly frames: subsample to a target rate, drop blurred and near-duplicate frames, cap the count.

Two passes keep memory bounded for long videos: pass 1 keeps only small statistics per candidate,
pass 2 re-reads the source and writes the chosen frames.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .frame_source import FrameSource

MIN_FRAMES_FOR_SFM = 3


class SelectionError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


@dataclass
class SelectionConfig:
    target_fps: float = 3.0
    max_frames: int = 150
    max_long_side: int = 1600
    blur_ratio: float = 0.55  # reject if sharpness < ratio * local median sharpness
    blur_window: int = 7  # neighbours (each side) used for the local median
    min_sharpness: float = 5.0  # absolute floor: flat / featureless frames
    duplicate_mad: float = 1.5  # mean abs diff (0..255) on a 64x48 thumbnail


@dataclass
class KeptFrame:
    file: str
    source_index: int
    source_name: str
    timestamp_s: float | None
    sharpness: float


@dataclass
class SelectionResult:
    step: int
    candidates: int
    rejected_blur: int
    rejected_low_texture: int
    rejected_duplicate: int
    dropped_by_cap: int
    resize_scale: float
    kept: list[KeptFrame] = field(default_factory=list)

    def summary(self) -> dict:
        d = asdict(self)
        d["selected"] = len(self.kept)
        del d["kept"]
        return d


def _stats(image: np.ndarray) -> tuple[float, np.ndarray]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    small = cv2.resize(gray, (320, max(1, round(320 * h / w))), interpolation=cv2.INTER_AREA)
    sharpness = float(cv2.Laplacian(small, cv2.CV_64F).var())
    thumb = cv2.resize(gray, (64, 48), interpolation=cv2.INTER_AREA).astype(np.float32)
    return sharpness, thumb


def select_frames(
    source: FrameSource,
    out_dir: Path,
    cfg: SelectionConfig = SelectionConfig(),
    should_stop: Callable[[], bool] = lambda: False,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
) -> SelectionResult:
    fps = source.nominal_fps()
    step = max(1, round(fps / cfg.target_fps)) if fps and cfg.target_fps > 0 else 1
    expected = max(1, source.frame_count() // step)

    # Pass 1: statistics only.
    cands: list[tuple[int, str, float | None, float, np.ndarray, tuple[int, int]]] = []
    for f in source.frames(step):
        if should_stop():
            raise Cancelled("cancelled during frame scan")
        sharp, thumb = _stats(f.image)
        cands.append((f.index, f.source_name, f.timestamp_s, sharp, thumb, f.image.shape[:2]))
        on_progress(len(cands), expected)
    if not cands:
        raise SelectionError("Input produced no frames")

    sharp = np.array([c[3] for c in cands])
    keep_idx: list[int] = []
    n_blur = n_flat = n_dup = 0
    last_thumb = None
    for i, (_, _, _, s, thumb, _) in enumerate(cands):
        lo, hi = max(0, i - cfg.blur_window), min(len(cands), i + cfg.blur_window + 1)
        if s < cfg.blur_ratio * float(np.median(sharp[lo:hi])):  # much softer than its neighbours: blur
            n_blur += 1
            continue
        if s < cfg.min_sharpness:  # uniformly soft clip: nothing to match
            n_flat += 1
            continue
        if last_thumb is not None and float(np.mean(np.abs(thumb - last_thumb))) < cfg.duplicate_mad:
            n_dup += 1
            continue
        keep_idx.append(i)
        last_thumb = thumb

    dropped_cap = 0
    if len(keep_idx) > cfg.max_frames:
        pick = np.linspace(0, len(keep_idx) - 1, cfg.max_frames).round().astype(int)
        dropped_cap = len(keep_idx) - cfg.max_frames
        keep_idx = [keep_idx[j] for j in pick]

    if len(keep_idx) < MIN_FRAMES_FOR_SFM:
        raise SelectionError(
            f"Only {len(keep_idx)} usable frames out of {len(cands)} candidates "
            f"(blurred {n_blur}, low texture {n_flat}, duplicates {n_dup}). "
            "Capture a slower, steadier clip with more camera translation."
        )

    h, w = cands[keep_idx[0]][5]
    scale = min(1.0, cfg.max_long_side / max(h, w))

    # Pass 2: write the chosen frames.
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = {cands[i][0]: cands[i] for i in keep_idx}
    result = SelectionResult(step, len(cands), n_blur, n_flat, n_dup, dropped_cap, scale)
    for f in source.frames(step):
        if should_stop():
            raise Cancelled("cancelled while writing frames")
        c = wanted.get(f.index)
        if c is None:
            continue
        img = f.image if scale == 1.0 else cv2.resize(f.image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        name = f"f{f.index:06d}.jpg"
        if not cv2.imwrite(str(out_dir / name), img, [cv2.IMWRITE_JPEG_QUALITY, 95]):
            raise SelectionError(f"Could not write frame {out_dir / name}")
        result.kept.append(KeptFrame(name, f.index, f.source_name, f.timestamp_s, round(c[3], 2)))
    if len(result.kept) != len(wanted):
        raise SelectionError(f"Source changed between passes: wrote {len(result.kept)} of {len(wanted)} frames")
    return result
