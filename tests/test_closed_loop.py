import pytest

from sim.tracking_scenario import run


@pytest.mark.slow
def test_closed_loop_scenario_meets_safety_and_tracking_contract(tmp_path):
    s = run(tmp_path)  # real time, ~20 s, loopback only
    assert s["real_hardware_packets"] == 0 and s["mock_rc_packets"] > 200
    assert s["target_ids_used"] == [1]  # no identity switch, no automatic re-acquisition
    short, medium, long_ = s["occlusions"]
    assert short["relocked_at"] and medium["relocked_at"] and not short["lost_at"] and not medium["lost_at"]
    assert long_["lost_at"] and not long_["relocked_at"]
    assert all(o["nonzero_yaw_while_not_locked"] == 0 for o in s["occlusions"])
    assert s["freeze"]["stale_frame_stops"] > 0 and s["freeze"]["nonzero_yaw_during_freeze_after_0.3s"] == 0
    assert s["nonzero_yaw_after_lost"] == 0 and s["final_state"] == "LOST"
    assert s["centering_mean_abs_err_px_4_6s"] < 0.5 * abs(s["initial_err_px"])  # yaw sign drives error down
