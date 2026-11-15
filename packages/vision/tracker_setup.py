"""Shared tracker wiring for the recorded (track_video) and live (live_run) paths: presets, detector threshold for
the low-score association stage, and the optional appearance model. See docs/PLAN_OCCLUSION_TRACKING.md."""
from __future__ import annotations

import argparse
import time
from dataclasses import replace

from tracking.tracker import MultiTracker, TrackerConfig  # noqa: F401  (TrackerConfig re-exported for callers)

PRESETS = ("V0", "VA", "VAB", "VABC")
# Adopted for the app and CLIs by the pre-registered verdict (outputs/evidence/occlusion_tracking/verdict.json):
# margin association + camera motion compensation. Appearance (VABC / --reid) stays opt-in. The library default
# TrackerConfig() is still the original strict rule (V0), so tests and simulations that construct it are unchanged.
DEFAULT_PRESET = "VAB"


def preset(name: str) -> TrackerConfig:
    from sim.crossing_ext import variant_config
    return variant_config(name)


def detector_threshold(score_thr: float, cfg: TrackerConfig) -> float:
    """The detector must return low-score boxes when the second association stage is on."""
    if cfg.assoc == "margin" and cfg.low_score is not None:
        return min(score_thr, cfg.low_score)
    return score_thr


def stage_config(cfg: TrackerConfig, score_thr: float) -> TrackerConfig:
    """Stage 1 keeps exactly the boxes the user's detector threshold would have given."""
    if cfg.assoc == "margin" and cfg.low_score is not None:
        return replace(cfg, min_score=max(cfg.min_score, score_thr))
    return cfg


class TimedEmbedder:
    """Appearance model wrapper that only counts calls and time. It returns the vectors and keeps nothing."""

    def __init__(self, model):
        self.model, self.ms, self.people = model, [], 0

    def __call__(self, frame, boxes):
        t0 = time.perf_counter()
        e = self.model.embed(frame, boxes)
        self.ms.append((time.perf_counter() - t0) * 1000)
        self.people += len(boxes)
        return e

    def summary(self) -> dict:
        import numpy as np
        return {"model": self.model.info() if hasattr(self.model, "info") else str(type(self.model).__name__),
                "calls": len(self.ms), "people": self.people,
                "ms_per_call_p50": round(float(np.percentile(self.ms, 50)), 2) if self.ms else None,
                "ms_per_call_p95": round(float(np.percentile(self.ms, 95)), 2) if self.ms else None,
                "ms_per_person": round(sum(self.ms) / self.people, 3) if self.people else None,
                "embeddings_written": 0}


def make_embedder(cfg: TrackerConfig) -> TimedEmbedder | None:
    if not cfg.reid:
        return None
    from .reid import OsnetReid
    return TimedEmbedder(OsnetReid())


def make_tracker(cfg: TrackerConfig, embedder: TimedEmbedder | None = None) -> MultiTracker:
    return MultiTracker(cfg, embedder=embedder)


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--tracker", choices=PRESETS, default=DEFAULT_PRESET,
                    help=f"association preset (default {DEFAULT_PRESET}; V0 = original strict rule); VABC turns on appearance (opt-in)")
    ap.add_argument("--reid", action="store_true", help="opt-in: in-memory appearance embeddings for contested cases")
    ap.add_argument("--no-gmc", action="store_true")


def config_from_args(a: argparse.Namespace) -> TrackerConfig:
    cfg = preset(a.tracker) if a.tracker else preset(DEFAULT_PRESET)
    if a.reid:
        cfg = replace(cfg, reid=True) if cfg.assoc == "margin" else preset("VABC")  # appearance needs margin mode
    if a.no_gmc:
        cfg = replace(cfg, gmc=False)
    return cfg
