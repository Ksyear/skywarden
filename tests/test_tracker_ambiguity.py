"""Regression tests for the E2 findings: a coasting target must not inherit another person's detection, and an id
whose last trusted observation is older than max_coast_s (2.0 s) must never come back. Written to fail on the
tracker before the fix (see outputs/evidence/e2_two_person_baseline_v1) and pass after it.

Boundary policy under test: a track is alive while (t - last_trusted_hit) <= max_coast_s; strictly greater expires,
and expiry is applied before any new detection is associated. The same rule holds for irregular frame intervals.
"""
import numpy as np
import pytest

from control.yaw_controller import YawController
from tracking.tracker import Detection, MultiTracker, TargetSelector, TargetState

W = 640
FOLLOW_PX = 25.0
XC = 440.0  # crossing point right of centre, so a wrong lock produces a non-zero yaw error


def det(cx, cy, h=180.0):
    return Detection((cx - 0.2 * h, cy - h / 2, cx + 0.2 * h, cy + h / 2), 0.9, "SYNTHETIC")


def crossing_run(hidden=(3.25, 4.75), jitter_px=4.0, fps=15.0, t_end=6.5):
    """A walks right, B walks left on the same row, paths cross at x=XC, t=4.0 while A is hidden. At the crossing frame B's
    detection is jittered by `jitter_px` so it matches A's prediction exactly and B's own prediction slightly worse
    (the situation that let the old tracker hand B's detection to A). Returns per-frame records with the real
    YawController in the loop (virtual telemetry, no sockets)."""
    tr, sel, ctl = MultiTracker(), TargetSelector(), YawController(W)
    rows = []
    for k in range(int(t_end * fps) + 1):
        t = k / fps
        a = (XC + 90 * (t - 4.0), 240.0)
        b = (XC - 90 * (t - 4.0), 240.0)
        a_visible = not (hidden[0] <= t < hidden[1]) and 0 < a[0] < W
        dets = []
        if a_visible:
            dets.append(det(*a))
        if 0 < b[0] < W:
            bx = b[0] + (jitter_px if abs(t - 4.0) < 1e-9 else 0.0)
            dets.append(det(bx, b[1], 150.0))
        tr.update(t, dets)
        if sel.target_id is None and t >= 1.0:
            sel.select(min(tr.active(), key=lambda x: np.hypot(*(x.center - np.array(a)))).id)
        state, track = sel.evaluate(tr)
        cmd = ctl.command(t, state, track, t, t, 85)
        on = None
        if track is not None:
            da, db = (float(np.hypot(*(track.center - np.array(p)))) for p in (a, b))
            on = "A" if da < FOLLOW_PX and da <= db else "B" if db < FOLLOW_PX else None
        # while A is hidden, a LOCKED target can only have been confirmed by someone else's detection
        rows.append({"t": t, "state": state, "on": on, "a_visible": a_visible, "yaw": cmd.yaw, "stop": cmd.stop_reasons})
    return rows, sel


def test_crossing_person_never_inherits_the_hidden_target():
    rows, _ = crossing_run()
    wrong = [round(r["t"], 3) for r in rows if r["state"] == TargetState.LOCKED and (not r["a_visible"] or r["on"] == "B")]
    assert wrong == [], f"target LOCKED on someone else's detection at t={wrong}"


def test_no_yaw_toward_wrong_person_and_none_while_not_locked():
    rows, _ = crossing_run()
    assert not [r["t"] for r in rows if r["yaw"] != 0 and (not r["a_visible"] or r["on"] == "B")]
    assert not [r["t"] for r in rows if r["yaw"] != 0 and r["state"] != TargetState.LOCKED]


def test_contested_detection_is_reported_as_ambiguous_and_stops_control():
    rows, _ = crossing_run()
    amb = [r for r in rows if r["state"] == TargetState.AMBIGUOUS]
    assert amb, "the crossing frame must surface an explicit AMBIGUOUS target state"
    assert all(r["yaw"] == 0 and "TARGET_AMBIGUOUS" in r["stop"] for r in amb)


def _locked_then_gap(gap_s: float, empty_updates: list[float] = ()):
    """Lock a walker at 10 Hz for 2 s, optionally run updates without detections, then the detection returns at the
    predicted spot after `gap_s` since the last trusted hit."""
    tr, sel = MultiTracker(), TargetSelector()
    t = 0.0
    for k in range(21):
        t = k / 10
        tr.update(t, [det(100 + 40 * t, 240)])
    sel.select(1)
    last = t
    for dt in empty_updates:
        tr.update(last + dt, [])
    tr.update(last + gap_s, [det(100 + 40 * (last + gap_s), 240)])
    return tr, sel


def test_detection_returning_after_more_than_2s_without_frames_gets_a_new_id():
    tr, sel = _locked_then_gap(2.5)
    assert tr.get(1) is None, "id 1 was revived by a detection 2.5 s after its last trusted hit"
    assert [x.id for x in tr.active()] != [1]
    state, _ = sel.evaluate(tr)
    assert state == TargetState.LOST and sel.target_id == 1  # no automatic re-selection


@pytest.mark.parametrize("gap,alive", [(2.0, True), (2.0 + 1e-3, False), (1.999, True)])
def test_coast_limit_boundary(gap, alive):
    tr, _ = _locked_then_gap(gap)
    assert (tr.get(1) is not None) == alive


def test_irregular_intervals_do_not_extend_the_limit():
    # empty updates at +0.03, +0.43, +1.33 s, detection back at +2.03 s (> 2.0 s since the last trusted hit)
    tr, _ = _locked_then_gap(2.03, empty_updates=[0.03, 0.43, 1.33])
    assert tr.get(1) is None


def test_lost_is_sticky_until_the_operator_selects_again():
    tr, sel = _locked_then_gap(2.5)
    for k in range(1, 6):  # the person keeps walking; a new track gets confirmed
        t = 2.0 + 2.5 + k / 10
        tr.update(t, [det(100 + 40 * t, 240)])
    state, track = sel.evaluate(tr)
    assert state == TargetState.LOST and track is None and sel.target_id == 1
    new_id = tr.active()[0].id
    sel.select(new_id)  # explicit operator action
    assert sel.evaluate(tr)[0] == TargetState.LOCKED


def test_virtual_control_is_zero_while_lost():
    tr, sel = _locked_then_gap(2.5)
    ctl = YawController(W)
    state, track = sel.evaluate(tr)
    cmd = ctl.command(tr.t, state, track, tr.t, tr.t, 85)
    assert cmd.yaw == 0 and "TARGET_LOST" in cmd.stop_reasons


def test_recorded_replay_stops_on_ambiguous_target():
    """The console's control replay (server path) must also STOP on a recorded AMBIGUOUS target."""
    from control.virtual_replay import replay_control
    frames = [{"k": k, "t": k / 10, "tracks": [{"id": 1, "state": st, "bbox": [480, 100, 520, 300]}]}
              for k, st in enumerate(["LOCKED"] * 4 + ["AMBIGUOUS"] * 3 + ["LOCKED"] * 3)]
    r = replay_control(frames, W, target_id=1, select_k=0)
    amb = [f for f in r["frames"] if f["target_state"] == "AMBIGUOUS"]
    assert len(amb) == 3 and all(f["yaw"] == 0 and "TARGET_AMBIGUOUS" in f["stop"] for f in amb)
    assert r["summary"]["ambiguous"] == 3 and r["summary"]["nonzero_yaw_while_not_locked"] == 0
