import numpy as np
import pytest

from control.yaw_controller import PID, YawController
from tracking.tracker import Track, TargetState


def track_at(cx):
    return Track(1, np.array([cx, 240.0, 0, 0]), np.eye(4), np.array([60.0, 160.0]))


def step(ctl, t, cx, state=TargetState.LOCKED, frame_age=0.0, tel_age=0.0, bat=80):
    return ctl.command(t, state, track_at(cx) if cx is not None else None, t - frame_age, t - tel_age, bat)


def test_yaw_sign_follows_target_side():
    for cx, sign in [(600, 1), (40, -1)]:
        ctl, t, cmd = YawController(640), 0.0, None
        for _ in range(10):
            t += 1 / 30
            cmd = step(ctl, t, cx)
        assert np.sign(cmd.yaw) == sign
    ctl = YawController(640)
    assert step(ctl, 0.1, 320).yaw == 0


def test_output_is_clamped_and_slew_limited():
    ctl, t, yaws = YawController(640), 0.0, []
    for _ in range(60):
        t += 1 / 30
        yaws.append(step(ctl, t, 640).yaw)
    assert max(yaws) == ctl.limits.max_yaw_rc
    assert all(b - a <= ctl.limits.max_yaw_slew_per_s / 30 + 1 for a, b in zip([0] + yaws, yaws))


def test_anti_windup_recovers_quickly_after_saturation():
    pid = PID(kp=60, ki=8, kd=0, out_limit=40, i_limit=2)
    for _ in range(300):  # 10 s saturated
        pid.step(1.0, 1 / 30)
    assert abs(pid._i) <= 2.0
    out = pid.step(-0.2, 1 / 30)
    assert out < 0  # sign follows the new error immediately, not stuck by a huge integral


@pytest.mark.parametrize("kw,reason", [
    ({"frame_age": 0.5}, "STALE_FRAME"),
    ({"tel_age": 1.0}, "STALE_TELEMETRY"),
    ({"bat": 10}, "LOW_BATTERY"),
    ({"bat": None}, "NO_BATTERY_READING"),
    ({"state": TargetState.PREDICTING}, "TARGET_PREDICTING"),
    ({"state": TargetState.LOST, "cx": None}, "TARGET_LOST"),
])
def test_unsafe_conditions_force_stop_and_reset(kw, reason):
    ctl, t = YawController(640), 0.0
    for _ in range(20):
        t += 1 / 30
        step(ctl, t, 600)
    cx = kw.pop("cx", 600)
    cmd = step(ctl, t + 1 / 30, cx, **kw)
    assert cmd.is_stop and any(r.startswith(reason) for r in cmd.stop_reasons)
    assert ctl.pid._i == 0.0


def test_no_telemetry_means_stop():
    ctl = YawController(640)
    assert ctl.command(1.0, TargetState.LOCKED, track_at(600), 1.0, None, 80).stop_reasons == ("NO_TELEMETRY",)


def test_only_yaw_is_ever_commanded():
    rng = np.random.default_rng(0)
    ctl, t = YawController(640), 0.0
    for _ in range(500):
        t += rng.uniform(0.01, 0.1)
        cmd = step(ctl, t, rng.uniform(-100, 740), state=list(TargetState)[rng.integers(len(TargetState))], frame_age=rng.uniform(0, 0.5))
        assert cmd.lateral == cmd.forward == cmd.vertical == 0 and cmd.source == "MOCK"
        assert -100 <= cmd.yaw <= 100


def test_pid_rejects_bad_dt():
    with pytest.raises(ValueError):
        PID(1, 0, 0, 1, 1).step(0.1, 0.0)


def test_derivative_term_damps_approach_to_centre():
    pid = PID(kp=0, ki=0, kd=1.0, out_limit=100, i_limit=1)
    pid.step(0.5, 0.1)
    assert pid.step(0.3, 0.1) < 0  # error shrinking (target moving to centre): D brakes the turn
    assert pid.step(0.6, 0.1) > 0  # error growing: D adds to the turn
