import pytest

from control.geofence import Geofence, GeofenceGuard
from control.yaw_controller import DroneCommand
from sim.two_person_crossing import run_once

SQ = Geofence(((-5.0, -5.0), (5.0, -5.0), (5.0, 5.0), (-5.0, 5.0)), margin_m=1.0)


def test_signed_distance_inside_outside():
    assert SQ.signed_distance(0, 0) == pytest.approx(5.0)
    assert SQ.signed_distance(4.5, 0) == pytest.approx(0.5)
    assert SQ.signed_distance(6.0, 0) == pytest.approx(-1.0)


def test_guard_blocks_outward_translation_but_keeps_yaw():
    g = GeofenceGuard(SQ, horizon_s=1.0)
    c = g.filter(DroneCommand(0, yaw=20, forward=50), (3.8, 0.0, 0.0))  # 0.5 m/s ahead -> 4.3 m, inside margin
    assert c.forward == 0 and c.yaw == 20 and "GEOFENCE" in c.stop_reasons
    assert g.filter(DroneCommand(0, yaw=0, forward=50), (0.0, 0.0, 0.0)).forward == 50  # far from the line
    back = g.filter(DroneCommand(0, yaw=0, forward=50), (4.2, 0.0, 180.0))  # facing inward near the line
    assert back.forward == 50
    # coasting velocity alone (commanded 10) must still trigger the guard
    assert g.filter(DroneCommand(0, yaw=0, forward=10), (3.4, 0.0, 0.0), velocity_body=(0.8, 0.0)).forward == 0


def test_guard_without_pose_stops_translation():
    c = GeofenceGuard(SQ).filter(DroneCommand(0, yaw=5, lateral=20), None)
    assert c.lateral == 0 and c.yaw == 5 and c.stop_reasons == ("NO_POSE_FOR_GEOFENCE",)


def test_e2_harness_controls_behave_as_designed():
    # control conditions (no second person): short occlusion keeps the id, long occlusion is LOST
    assert all(run_once(0.5, False, s)["same_id_on_A"] for s in range(5))
    assert all(run_once(2.5, False, s)["lost"] for s in range(5))
    assert not any(run_once(0.0, True, s)["switched_to_B"] for s in range(5))
