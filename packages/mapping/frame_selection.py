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
    # F1 (docs/PLAN_FRAMES_AND_DEPTH.md): "fixed" keeps the step sampler above; "adaptive" scans at adaptive_fps and
    # keeps frames by tracked static-feature overlap and rotation-compensated parallax (DSO-style keyframe idea)
    mode: str = "fixed"
    adaptive_fps: float = 10.0
    overlap_floor: float = 0.6     # share of the last kept frame's features still tracked
    parallax_px: float = 8.0       # median residual flow after a homography fit (rotation / far plane removed), 640 px
    flow_px: float = 40.0          # median total flow, 640 px (large rotation)
    slow_parallax_px: float = 3.0  # after slow_after_s without a keyframe, this much parallax is enough
    slow_after_s: float = 1.0
    max_gap_s: float = 2.0         # never leave a longer hole (static camera): keep a frame anyway, reason recorded
    force_indices: tuple[int, ...] = ()
    mask_weights: str | None = None  # sky/person segmentation (static_masks); features on masked pixels are not used
    adaptive_base: str = "fixed"     # "fixed" (F1b): the target_fps frames are kept and only connectors/flow frames are
                                     # added between them; "none" (F1): keyframes by parallax/flow only


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


TRACK_W = 640


def _keep_mask(net, image):
    from .static_masks import MEAN, STD
    rgb = cv2.cvtColor(cv2.resize(image, (384, 384)), cv2.COLOR_BGR2RGB)
    x = (rgb.astype(np.float32) / 255 - MEAN) / STD
    net.setInput(np.ascontiguousarray(x.transpose(2, 0, 1)[None]))
    lab = net.forward()[0].argmax(0)
    keep = ((lab != 1) & (lab != 3)).astype(np.uint8) * 255
    return keep


def _share(g0, g1, pts, n0):
    _, st, _ = cv2.calcOpticalFlowPyrLK(g0, g1, pts, None, winSize=(21, 21), maxLevel=4)
    return float((st.ravel() == 1).sum()) / max(1, n0)


def _select_adaptive(source, out_dir, cfg, should_stop, on_progress) -> SelectionResult:
    fps = source.nominal_fps() or 30.0
    step = max(1, round(fps / cfg.adaptive_fps))
    forced = set(cfg.force_indices)
    base_step = max(1, round(fps / cfg.target_fps)) if cfg.adaptive_base == "fixed" else 0
    net = None
    if cfg.mask_weights:
        import hashlib
        from .static_masks import MODEL_SHA256
        if hashlib.sha256(Path(cfg.mask_weights).read_bytes()).hexdigest() != MODEL_SHA256:
            raise SelectionError("unexpected segmentation weights")
        net = cv2.dnn.readNetFromONNX(cfg.mask_weights)
    cands = []  # (index, name, t, sharpness, gray640, keep640, shape)
    total = max(1, source.frame_count())
    for f in source.frames(1):
        if should_stop():
            raise Cancelled("cancelled during frame scan")
        if f.index % step and f.index not in forced and not (base_step and f.index % base_step == 0):
            continue
        sharp, _ = _stats(f.image)
        h, w = f.image.shape[:2]
        g = cv2.resize(cv2.cvtColor(f.image, cv2.COLOR_BGR2GRAY), (TRACK_W, round(TRACK_W * h / w)), interpolation=cv2.INTER_AREA)
        keep = cv2.resize(_keep_mask(net, f.image), g.shape[::-1], interpolation=cv2.INTER_NEAREST) if net is not None else None
        cands.append((f.index, f.source_name, f.timestamp_s, sharp, g, keep, (h, w)))
        on_progress(f.index + 1, total)
    if not cands:
        raise SelectionError("Input produced no frames")
    sharp = np.array([c[3] for c in cands])
    good = []
    n_blur = n_flat = 0
    for i, c in enumerate(cands):
        lo, hi = max(0, i - 3 * cfg.blur_window), min(len(cands), i + 3 * cfg.blur_window + 1)
        if c[0] not in forced and c[3] < cfg.blur_ratio * float(np.median(sharp[lo:hi])):
            n_blur += 1; continue
        if c[0] not in forced and c[3] < cfg.min_sharpness:
            n_flat += 1; continue
        good.append(i)
    t_of = lambda i: cands[i][2] if cands[i][2] is not None else cands[i][0] / fps  # noqa: E731
    log, spans = [], []

    def feats(i):
        return cv2.goodFeaturesToTrack(cands[i][4], 800, 0.01, 8, mask=cands[i][5])

    kept = [good[0]]
    log.append({"index": cands[good[0]][0], "t": round(t_of(good[0]), 3), "reason": "first"})
    K = good[0]; p0 = feats(K); pts = p0.copy() if p0 is not None else None; prev = K; last_ok = None
    n0 = 0 if p0 is None else len(p0)
    gi = 1
    while gi < len(good):
        c = good[gi]
        if should_stop():
            raise Cancelled("cancelled during selection")
        reason = None; share = parallax = flow = 0.0
        if cands[c][0] in forced:
            reason = "forced"
        elif base_step and cands[c][0] % base_step == 0 and not (pts is not None and n0 >= 20 and
                                                                  _share(cands[prev][4], cands[c][4], pts, n0) < cfg.overlap_floor
                                                                  and last_ok is not None and last_ok != K):
            reason = "base"
        elif pts is None or n0 < 20:
            reason = "no_features"
        else:
            nxt, st, _ = cv2.calcOpticalFlowPyrLK(cands[prev][4], cands[c][4], pts, None, winSize=(21, 21), maxLevel=4)
            ok = st.ravel() == 1
            share = float(ok.sum()) / n0
            if share < cfg.overlap_floor:
                if last_ok is not None and last_ok != K:
                    kept.append(last_ok)
                    log.append({"index": cands[last_ok][0], "t": round(t_of(last_ok), 3), "reason": "connector"})
                    K = last_ok; p0 = feats(K); pts = None if p0 is None else p0.copy(); prev = K
                    n0 = 0 if p0 is None else len(p0); last_ok = None
                    continue  # re-test the same candidate from the connector
                reason = "overlap_no_intermediate"
                spans.append({"t_from": round(t_of(K), 3), "t_to": round(t_of(c), 3), "cause": f"tracked share {share:.2f} < {cfg.overlap_floor} with no usable frame between"})
            else:
                a, b = p0[ok].reshape(-1, 2), nxt[ok].reshape(-1, 2)
                flow = float(np.median(np.linalg.norm(b - a, axis=1)))
                H, _ = cv2.findHomography(a, b, cv2.RANSAC, 3.0) if len(a) >= 8 else (None, None)
                if H is not None:
                    pa = cv2.perspectiveTransform(a.reshape(-1, 1, 2), H).reshape(-1, 2)
                    parallax = float(np.median(np.linalg.norm(b - pa, axis=1)))
                dt = t_of(c) - t_of(K)
                if parallax >= cfg.parallax_px:
                    reason = "parallax"
                elif flow >= cfg.flow_px:
                    reason = "flow"
                elif dt >= cfg.slow_after_s and parallax >= cfg.slow_parallax_px:
                    reason = "slow_parallax"
                elif dt >= cfg.max_gap_s:
                    reason = "max_gap"
                    spans.append({"t_from": round(t_of(K), 3), "t_to": round(t_of(c), 3), "cause": f"parallax {parallax:.1f} px < {cfg.slow_parallax_px} px for {dt:.1f} s (camera nearly static or pure rotation)"})
                p0 = p0[ok].reshape(-1, 1, 2); pts = nxt[ok].reshape(-1, 1, 2)
                prev = c; last_ok = c
        if reason:
            kept.append(c)
            log.append({"index": cands[c][0], "t": round(t_of(c), 3), "reason": reason, "tracked_share": round(share, 3),
                        "parallax_px": round(parallax, 2), "flow_px": round(flow, 2)})
            K = c; p0 = feats(K); pts = None if p0 is None else p0.copy(); prev = K
            n0 = 0 if p0 is None else len(p0); last_ok = None
        gi += 1
    kept = sorted(set(kept))
    dropped_cap = 0
    if len(kept) > cfg.max_frames:  # drop the least informative frames, never forced frames or connectors
        info = {e["index"]: (e.get("parallax_px", 0.0) if e["reason"] in ("parallax", "flow", "slow_parallax", "max_gap") else 1e9) for e in log}
        order = sorted(kept, key=lambda i: info.get(cands[i][0], 1e9))
        drop = set(order[:len(kept) - cfg.max_frames])
        dropped_cap = len(drop)
        kept = [i for i in kept if i not in drop]
    if len(kept) < MIN_FRAMES_FOR_SFM:
        raise SelectionError(f"Only {len(kept)} usable frames")
    h, w = cands[kept[0]][6]
    scale = min(1.0, cfg.max_long_side / max(h, w))
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = {cands[i][0]: cands[i] for i in kept}
    result = SelectionResult(step, len(cands), n_blur, n_flat, 0, dropped_cap, scale)
    for f in source.frames(1):
        c = wanted.get(f.index)
        if c is None:
            continue
        img = f.image if scale == 1.0 else cv2.resize(f.image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        name = f"f{f.index:06d}.jpg"
        if not cv2.imwrite(str(out_dir / name), img, [cv2.IMWRITE_JPEG_QUALITY, 95]):
            raise SelectionError(f"Could not write frame {out_dir / name}")
        result.kept.append(KeptFrame(name, f.index, f.source_name, f.timestamp_s, round(c[3], 2)))
    import json
    kept_idx = {cands[i][0] for i in kept}
    (out_dir.parent / "selection.json").write_text(json.dumps({
        "mode": "adaptive", "config": asdict(cfg), "candidate_step": step, "candidates": len(cands), "kept": len(kept),
        "dropped_by_cap": dropped_cap, "reasons": {r: sum(1 for e in log if e["reason"] == r and e["index"] in kept_idx) for r in {e["reason"] for e in log}},
        "unusable_spans": spans, "log": [e for e in log if e["index"] in kept_idx]}, indent=1))
    return result


def select_frames(
    source: FrameSource,
    out_dir: Path,
    cfg: SelectionConfig = SelectionConfig(),
    should_stop: Callable[[], bool] = lambda: False,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
) -> SelectionResult:
    if cfg.mode == "adaptive":
        return _select_adaptive(source, out_dir, cfg, should_stop, on_progress)
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
