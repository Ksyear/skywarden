import numpy as np
import pytest

from tracking.tracker import Detection, MultiTracker, TargetSelector, TargetState, TrackState


def det(cx, cy, w=60, h=160, score=0.9):
    return Detection((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), score, "SYNTHETIC")


def run_occlusion(gap_s, fps=15):
    trk, t, ids = MultiTracker(), 0.0, []
    states = []
    for k in range(60):  # visible 4 s walking right at 40 px/s
        t = k / fps
        visible = not (1.0 <= t < 1.0 + gap_s)
        tracks = trk.update(t, [det(100 + 40 * t, 240)] if visible else [])
        ids += [tr.id for tr in tracks]
        states.append((t, [tr.state for tr in tracks]))
    return trk, set(ids), states


@pytest.mark.parametrize("gap", [0.5, 1.5])
def test_short_occlusion_keeps_identity(gap):
    trk, ids, states = run_occlusion(gap)
    assert ids == {1}
    assert any(TrackState.PREDICTING in s for _, s in states)
    assert trk.get(1).state == TrackState.LOCKED


def test_occlusion_longer_than_2s_drops_track_and_new_id_appears():
    trk, ids, _ = run_occlusion(2.5)
    assert 1 in ids and trk.get(1) is None and trk.stats.lost == 1
    assert len(ids) == 2  # reappearing person is a new track: no re-identification


def test_uncertainty_grows_while_predicting():
    trk = MultiTracker()
    for k in range(10):
        trk.update(k / 10, [det(100 + 5 * k, 200)])
    sig = [trk.get(1).position_sigma_px()]
    for k in range(10, 25):
        trk.update(k / 10, [])
        sig.append(trk.get(1).position_sigma_px())
    assert all(b > a for a, b in zip(sig, sig[1:]))


def test_two_people_crossing_keep_ids():
    trk = MultiTracker()
    a_ids, b_ids = set(), set()
    for k in range(90):
        t = k / 30
        a, b = det(100 + 120 * t, 240), det(460 - 120 * t, 270)
        tracks = trk.update(t, [a, b])
        for tr in tracks:
            if tr.state == TrackState.LOCKED:
                (a_ids if abs(tr.center[1] - 240) < abs(tr.center[1] - 270) else b_ids).add(tr.id)  # lanes differ in y
    assert len(a_ids) == 1 and len(b_ids) == 1 and a_ids != b_ids


def test_variable_dt_and_jitter():
    # Irregular frame spacing (20-150 ms) with 3 px noise: one identity, unbiased velocity, small position error.
    vx, pos_err = [], []
    for seed in range(5):
        rng = np.random.default_rng(seed)
        trk, t = MultiTracker(), 0.0
        for k in range(80):
            t += rng.uniform(0.02, 0.15)
            trk.update(t, [det(200 + 30 * t + rng.normal(0, 3), 240 + rng.normal(0, 3))])
            if k >= 20:
                vx.append(trk.get(1).x[2])
                pos_err.append(abs(trk.get(1).x[0] - (200 + 30 * t)))
        assert [tr.id for tr in trk.tracks] == [1] and trk.get(1).state == TrackState.LOCKED
    assert abs(np.mean(vx) - 30) < 5 and np.mean(pos_err) < 4


def test_out_of_order_and_invalid_detections_are_rejected():
    trk = MultiTracker()
    trk.update(1.0, [det(100, 100)])
    trk.update(0.9, [det(500, 500)])  # older frame arrives late
    trk.update(1.0, [det(500, 500)])  # duplicate timestamp
    assert trk.stats.rejected_stale == 2 and trk.t == 1.0 and len(trk.tracks) == 1
    trk.update(1.1, [Detection((10, 10, 5, 50), 0.9, "SYNTHETIC"), Detection((0, 0, float("nan"), 1), 0.9, "SYNTHETIC")])
    assert trk.stats.rejected_invalid == 2 and trk.stats.created == 1


def test_target_selector_never_switches_person():
    trk, sel = MultiTracker(), TargetSelector()
    for k in range(5):
        trk.update(k / 10, [det(100, 200), det(400, 200)])
    assert sel.evaluate(trk)[0] == TargetState.SEARCHING
    sel.select(1)
    assert sel.evaluate(trk)[0] == TargetState.LOCKED
    t = 0.5
    while t < 3.0:  # person 1 leaves, person 2 stays
        trk.update(t, [det(400, 200)])
        sel.evaluate(trk)
        t += 0.1
    st, tr = sel.evaluate(trk)
    assert st == TargetState.LOST and tr is None and sel.target_id == 1
    assert [s for _, s in sel.history] == ["SEARCHING", "LOCKED", "PREDICTING", "LOST"]
