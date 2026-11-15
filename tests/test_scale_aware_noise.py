"""Scale-aware Kalman noise (docs/PLAN_SCALE_AWARE_NOISE.md, F-34): measurement / process noise proportional to the
box height. A person filling a 1080p frame must keep a trusted lock through normal box jitter; small boxes and the
library default keep the original behaviour bit for bit; the safety rules (F-33 re-connection guard, contested -> STOP)
still hold."""
from dataclasses import replace

import numpy as np
import pytest

from sim.crossing_ext import variant_config
from tracking.tracker import Detection, MultiTracker, TargetSelector, TargetState, TrackerConfig, TrackState

SCALED = {k: getattr(variant_config("VABS"), k) for k in ("meas_noise_frac", "accel_noise_frac")}


def det(cx, cy, h, score=0.9):
    return Detection((cx - 0.2 * h, cy - h / 2, cx + 0.2 * h, cy + h / 2), score, "SYNTHETIC")


def walk(cfg, h, jitter_frac, n=90, fps=30.0, seed=0):
    """A walker whose box centre jitters by jitter_frac x h (normal, per axis) around a slow straight path."""
    rng = np.random.default_rng(seed)
    tr = MultiTracker(cfg)
    states = []
    for k in range(n):
        t = k / fps
        cx, cy = 900 + 40 * t + rng.normal(0, jitter_frac * h), 540 + rng.normal(0, jitter_frac * h)
        tr.update(t, [det(cx, cy, h)])
        states.append(tr.get(1).state if tr.get(1) else None)
    return tr, states


@pytest.mark.parametrize("base", ["V0", "VAB"])
def test_large_box_normal_jitter_stays_locked_only_with_scaled_noise(base):
    """850 px tall person, centre jitter 0.015 x h (~13 px, below the 23 px seen when the Utrecht target dropped to
    PREDICTING): the fixed 6 px noise loses the trusted lock, the scaled noise keeps it."""
    cfg = variant_config(base)
    fixed, s_fixed = walk(cfg, 850.0, 0.015)
    scaled, s_scaled = walk(replace(cfg, **SCALED), 850.0, 0.015)
    locked = lambda s: sum(x == TrackState.LOCKED for x in s)  # noqa: E731
    assert fixed.stats.created > 1 or locked(s_fixed) < 0.8 * len(s_fixed)
    assert scaled.stats.created == 1 and locked(s_scaled) >= 0.9 * len(s_scaled)


def test_gate_radius_scales_with_box_height():
    """A detection 25 px from a converged prediction: outside the gate for an 850 px box with fixed noise, inside with
    scaled noise; for a 100 px box both settings give the identical (fixed) gate."""
    def gate(cfg, h, off):
        tr = MultiTracker(cfg)
        for k in range(30):
            tr.update(k / 30, [det(900.0, 540.0, h)])
        track = tr.get(1)
        tr._predict(track, 1 / 30)
        return tr._gated(track, det(900.0 + off, 540.0, h))
    base = variant_config("VAB")
    assert gate(base, 850.0, 25.0) is None
    assert gate(replace(base, **SCALED), 850.0, 25.0) is not None
    assert gate(base, 100.0, 15.0) == gate(replace(base, **SCALED), 100.0, 15.0)
    assert gate(base, 100.0, 25.0) is None and gate(replace(base, **SCALED), 100.0, 25.0) is None


def test_small_boxes_bit_identical_and_library_default_unchanged():
    """Below 6 px / meas_noise_frac (240 px for VABS) and with accel_noise_frac 0 nothing changes; the library default
    TrackerConfig() has both fractions 0 (original strict tracker)."""
    a, sa = walk(variant_config("VAB"), 110.0, 0.02, seed=3)
    b, sb = walk(replace(variant_config("VAB"), **SCALED), 110.0, 0.02, seed=3)
    assert sa == sb and np.array_equal(a.get(1).x, b.get(1).x) and np.array_equal(a.get(1).P, b.get(1).P)
    d = TrackerConfig()
    assert d.meas_noise_frac == 0.0 and d.accel_noise_frac == 0.0 and d.assoc == "strict"
    assert "noise scaled" not in d.describe() and "noise scaled" in replace(d, **SCALED).describe()


def test_scaled_noise_keeps_the_contested_reconnect_guard():
    """F-33 rule at 1080p scale: after an AMBIGUOUS spell a lone detection 0.2 x h from the prediction is inside the
    (now larger) Mahalanobis gate but beyond 0.15 x h, so it must be refused (track stays AMBIGUOUS, no trusted hit);
    one 0.1 x h away is accepted. Same with and without scaled noise: the guard does not depend on the noise model."""
    h = 800.0
    for cfg in (variant_config("VAB"), variant_config("VABS")):
        for off, accepted in ((0.2, False), (0.1, True)):
            tr = MultiTracker(cfg)
            for k in range(30):
                tr.update(k / 30, [det(900.0, 540.0, h)])
            track = tr.get(1)
            track.contested, track.state = True, TrackState.AMBIGUOUS
            track.P[:2, :2] += np.eye(2) * 120.0 ** 2  # covariance grown while coasting through the ambiguous spell
            assert tr._gated(track, det(900.0 + off * h, 540.0, h)) is not None  # inside the gate either way
            tr.update(30 / 30, [det(900.0 + off * h, 540.0, h)])
            assert (track.last_hit_t == 1.0) is accepted
            assert (track.state == TrackState.LOCKED) is accepted and (tr.stats.contested_reconnect_refused == 1) is not accepted


def test_truly_contested_large_boxes_still_stop():
    """Two equal 800 px boxes swap places through the same point: whatever the noise, motion cannot tell them apart
    there, so the target must not stay LOCKED on the other person."""
    cfg = replace(variant_config("VAB"), **SCALED)
    h, fps = 800.0, 30.0
    tr, sel = MultiTracker(cfg), TargetSelector()
    bad = 0
    for k in range(120):
        t = k / fps
        a, b = (700 + 300 * (t - 2.0), 540.0), (1220 - 300 * (t - 2.0), 540.0)
        tr.update(t, [det(*a, h), det(*b, h)])
        if sel.target_id is None and t >= 0.5:
            sel.select(min(tr.active(), key=lambda x: np.hypot(*(x.center - np.array(a)))).id)
        state, track = sel.evaluate(tr)
        if state == TargetState.LOCKED and track is not None and t > 2.2:
            bad += np.hypot(*(track.center - np.array(b))) + 1 < np.hypot(*(track.center - np.array(a)))
    assert bad == 0


def test_cli_selects_scaled_noise_and_fixed_noise_turns_it_off():
    import argparse
    from vision import tracker_setup
    ap = argparse.ArgumentParser()
    tracker_setup.add_args(ap)
    assert tracker_setup.config_from_args(ap.parse_args(["--tracker", "VABS"])) == variant_config("VABS")
    assert tracker_setup.config_from_args(ap.parse_args(["--tracker", "VABS", "--fixed-noise"])) == variant_config("VAB")
    assert tracker_setup.config_from_args(ap.parse_args(["--tracker", "VABS", "--reid"])).meas_noise_frac == SCALED["meas_noise_frac"]
