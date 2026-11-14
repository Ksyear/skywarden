import json

import cv2
import numpy as np
import pycolmap
import pytest
from scipy.spatial.transform import Rotation

from mapping import markers, metric_scale as ms

K = (400.0, 400.0, 320.0, 240.0)


def _cam():
    return pycolmap.Camera(model="PINHOLE", width=640, height=480, params=list(K))


def _poses(n=6):
    """cam_from_world 3x4 for cameras moving sideways in front of z = 3, looking at +z."""
    out = []
    for s in np.linspace(-0.6, 0.6, n):
        R = Rotation.from_euler("y", np.deg2rad(-8 * s)).as_matrix()  # R_world_cam
        C = np.array([s, 0.05 * s, 0.0])
        out.append(np.hstack([R.T, (-R.T @ C)[:, None]]))
    return out


def _square(side=0.4, centre=(0.1, -0.05, 3.0)):
    c, h = np.asarray(centre), side / 2
    return np.array([c + [-h, -h, 0], c + [h, -h, 0], c + [h, h, 0], c + [-h, h, 0]])


def _project(T, X):
    pc = T[:3, :3] @ X + T[:3, 3]
    return np.array([K[0] * pc[0] / pc[2] + K[2], K[1] * pc[1] / pc[2] + K[3]])


def _views(sq, noise=0.3, seed=0, n=6):
    rng = np.random.default_rng(seed)
    cam = _cam()
    return [{"image": f"f{k:06d}.jpg", "cam": cam, "cam_from_world": T,
             "corners": np.array([_project(T, X) for X in sq]) + rng.uniform(-noise, noise, (4, 2))}
            for k, T in enumerate(_poses(n))]


def _session(tmp_path, name="s1"):
    sd = tmp_path / name
    sd.mkdir()
    poses = [{"order": k, "image_name": f"f{k:06d}.jpg", "T_world_camera": np.eye(4).tolist()} for k in range(3)]
    (sd / "trajectory.json").write_text(json.dumps({"schema": "skywarden/trajectory@1", "scale": "RELATIVE", "poses": poses}))
    (sd / "map_report.json").write_text(json.dumps({"status": "SUCCEEDED", "labels": {"scale": "RELATIVE"},
                                                    "reconstruction": {"chosen_model_dir": "sparse/0",
                                                                       "models": [{"rank": 0, "colmap_dir": "sparse/0"}]}}))
    return sd


def test_triangulated_square_sides_within_1pct_and_reprojection_below_1px():
    sq = _square()
    obs, X = ms.marker_observation(0, "4X4_50", 0.4, _views(sq))
    assert obs["accepted"], obs["reject_reason"]
    side_err = np.abs(np.array(obs["side_lengths_units"]) - 0.4) / 0.4
    assert side_err.max() <= 0.01, side_err
    assert obs["corner_reproj_rms_px"] <= 1.0
    assert abs(obs["metres_per_unit"] - 1.0) <= 0.01
    assert obs["min_triangulation_angle_deg"] >= 2.0
    assert np.abs(X - sq).max() < 0.02


def test_marker_rejections_follow_preregistered_conditions():
    sq = _square()
    obs, _ = ms.marker_observation(0, "4X4_50", 0.4, _views(sq)[:2])
    assert not obs["accepted"] and "frames_used" in obs["reject_reason"]
    skewed = sq.copy()
    skewed[1] += [0.05, 0, 0]  # a non-square quadrilateral (e.g. wrong corner)
    obs, _ = ms.marker_observation(0, "4X4_50", 0.4, _views(skewed, noise=0))
    assert not obs["accepted"] and "side_ratio_max_dev" in obs["reject_reason"]
    near = _square(centre=(0.1, -0.05, 60.0), side=6.0)  # far away: rays almost parallel
    obs, _ = ms.marker_observation(0, "4X4_50", 0.4, _views(near, noise=0))
    assert not obs["accepted"] and "triangulation_angle" in obs["reject_reason"]


def test_triangulation_handles_lens_distortion_through_camera_model():
    cam = pycolmap.Camera(model="SIMPLE_RADIAL", width=640, height=480, params=[400.0, 320.0, 240.0, 0.08])
    sq = _square()
    views = []
    for k, T in enumerate(_poses()):
        uv = np.array([np.asarray(cam.img_from_cam((T[:3, :3] @ X + T[:3, 3])[None])).reshape(2) for X in sq])
        views.append({"image": f"f{k}.jpg", "cam": cam, "cam_from_world": T, "corners": uv})
    obs, _ = ms.marker_observation(0, "4X4_50", 0.4, views)
    assert obs["accepted"] and abs(obs["metres_per_unit"] - 1) < 1e-4 and obs["corner_reproj_rms_px"] < 1e-3


def test_pnp_diagnostic_recovers_camera_distance():
    # S0 saw 70-100 % errors from solvePnP in a throwaway script; this pins down the corner order and K = I use.
    sq = _square()
    for v in _views(sq, noise=0.0):
        r = ms.pnp_scale(v["cam"], v["cam_from_world"], v["corners"], 0.4, sq.mean(0))
        assert abs(r["metres_per_unit"] - 1.0) < 0.002, r


def test_scale_file_roundtrip_and_status_transitions(tmp_path):
    sd = _session(tmp_path)
    s = ms.current_scale(sd)
    assert s["status"] == "NONE" and s["schema"] == ms.SCHEMA and not (sd / "scale.json").exists()
    # fit: 2.0 m tape over 4 map units -> 0.5 m/unit, unverified
    s = ms.add_two_point(sd, [0, 0, 0], [4, 0, 0], 2.0, "fit", note="door", pick_tolerance_units=0.02)
    assert s["status"] == "MEASURED_UNVERIFIED" and s["metres_per_unit"] == pytest.approx(0.5)
    assert s["uncertainty"]["rel_std"] == pytest.approx(0.01)
    assert ms.read_scale(sd)["status"] == "MEASURED_UNVERIFIED"
    # one check only: not verified
    s = ms.add_two_point(sd, [0, 0, 0], [0, 3, 0], 1.52, "verify", note="desk")
    assert s["status"] == "MEASURED_UNVERIFIED" and s["verification"]["checks"][0]["pass"] is True
    # second independent check within 5 % -> verified
    s = ms.add_two_point(sd, [1, 1, 1], [1, 1, 5], 2.05, "verify", note="wall")
    assert s["status"] == "MEASURED_VERIFIED" and s["verification"]["passed"]
    assert ms.summary(s)["derived_scale_label"] == "METRIC_ANCHORED"
    # map rebuilt -> NONE, metres hidden
    (sd / "trajectory.json").write_text((sd / "trajectory.json").read_text() + " ")
    s = ms.current_scale(sd)
    assert s["status"] == "NONE" and "map_hash" in s["status_reason"]
    assert ms.summary(s)["metres_per_unit"] is None
    # adding after a rebuild starts a fresh file and keeps the old one aside
    s = ms.add_two_point(sd, [0, 0, 0], [1, 0, 0], 1.0, "fit")
    assert len(s["observations"]) == 1 and list((sd / "scale_work").glob("scale_stale_*.json"))


def test_failed_check_and_same_object_check_do_not_verify(tmp_path):
    sd = _session(tmp_path)
    ms.add_two_point(sd, [0, 0, 0], [4, 0, 0], 2.0, "fit", note="door")
    ms.add_two_point(sd, [0, 0, 0], [0, 3, 0], 1.5, "verify", note="desk")
    s = ms.add_two_point(sd, [0, 0, 0], [0, 0, 2], 1.2, "verify", note="shelf")  # predicted 1.0 m: 16.7 % off
    assert s["status"] == "MEASURED_UNVERIFIED" and s["verification"]["checks"][1]["pass"] is False
    sd2 = _session(tmp_path, "s2")
    ms.add_two_point(sd2, [0, 0, 0], [4, 0, 0], 2.0, "fit", note="door")
    ms.add_two_point(sd2, [4, 0, 0], [0, 0, 0], 2.0, "verify", note="")  # the fit segment again
    s = ms.add_two_point(sd2, [0, 0, 0], [0, 3, 0], 1.5, "verify", note="desk")
    assert s["status"] == "MEASURED_UNVERIFIED" and "independent" in s["verification"]["checks"][0]["reason"]


def test_regulation_lengths_are_evaluated_but_never_verify(tmp_path):
    sd = _session(tmp_path)
    ms.add_two_point(sd, [0, 0, 0], [2, 0, 0], 23.77, "fit", note="court length", length_source="REGULATION")
    mpu = 23.77 / 2
    ms.add_two_point(sd, [0, 0, 0], [0, 10.97 / mpu, 0], 10.97 * 1.01, "verify", note="court width", length_source="REGULATION")
    s = ms.add_two_point(sd, [0, 0, 0], [0, 0, 8.23 / mpu], 8.23, "verify", note="singles width", length_source="REGULATION")
    assert all(c["pass"] is not None and c["error_pct"] < 5 for c in s["verification"]["checks"])
    assert s["status"] == "MEASURED_UNVERIFIED" and "REGULATION_DIMENSIONS" in s["flags"]
    assert s["verification"]["n_checks_done"] == 0 and s["verification"]["n_checks_not_counted"] == 2
    # two tape-measured checks on top make it verified; the regulation ones still do not count
    ms.add_two_point(sd, [0, 0, 0], [0, 2, 0], 23.77, "verify", note="tape A")
    s = ms.add_two_point(sd, [1, 0, 0], [1, 0, 2], 23.77, "verify", note="tape B")
    assert s["status"] == "MEASURED_VERIFIED" and s["verification"]["n_checks_done"] == 2
    with pytest.raises(ValueError):
        ms.add_two_point(sd, [0, 0, 0], [1, 0, 0], 1.0, "fit", length_source="GUESS")


def test_ground_plane_ransac_ignores_clutter():
    rng = np.random.default_rng(2)
    floor = np.c_[rng.uniform(-2, 2, (500, 2)), rng.normal(0, 0.001, 500)]
    clutter = np.c_[rng.uniform(-2, 2, (150, 2)), rng.uniform(0.1, 1.5, 150)]
    R = Rotation.from_euler("xyz", [20, -35, 60], degrees=True).as_matrix()
    P = np.r_[floor, clutter] @ R.T + [1, 2, 3]
    pl = ms.ground_plane_ransac(P, inlier_units=0.01)
    assert abs(abs(np.asarray(pl["normal"]) @ R[:, 2]) - 1) < 1e-4 and pl["inlier_ratio"] == pytest.approx(500 / 650, abs=0.01)
    C = np.array([[0, 0, 1.0], [0.5, 0, 1.5]]) @ R.T + [1, 2, 3]
    assert ms.scale_from_height(C / 2, [1.0, 1.5], {**pl, "d": pl["d"] / 2})["metres_per_unit"] == pytest.approx(2.0, rel=1e-3)


def test_marker_spread_above_5pct_flags_drift(tmp_path):
    sd = _session(tmp_path)
    s = ms.read_scale(sd) or ms.new_scale(sd)
    for mid, mpu in ((0, 1.0), (1, 1.08)):
        s["observations"].append({"method": "ARUCO_MARKER", "marker_id": mid, "metres_per_unit": mpu, "accepted": True,
                                  "rel_std_sides": 0.01})
    for name, units in (("a", 2.0), ("b", 3.0)):
        s["verification"]["checks"].append({"name": name, "true_m": units * 1.04, "map_units": units})
    s = ms.evaluate(s, ms.map_hash(sd))
    assert s["verification"]["n_checks_passed"] == 2
    assert s["status"] == "MEASURED_UNVERIFIED" and "SCALE_DIFFERS_BETWEEN_MARKERS" in s["flags"]
    assert not s["verification"]["criteria"]["markers_agree_within_5pct"]


def test_model_estimate_alone_is_model_estimated_and_never_averaged(tmp_path):
    sd = _session(tmp_path)
    s = ms.set_cross_check(sd, "METRIC_DEPTH_MODEL", 0.8, model="test")
    assert s["status"] == "MODEL_ESTIMATED" and s["metres_per_unit"] is None
    assert ms.summary(s)["metres_per_unit"] == 0.8
    s = ms.add_two_point(sd, [0, 0, 0], [1, 0, 0], 1.0, "fit")
    assert s["status"] == "MEASURED_UNVERIFIED" and s["metres_per_unit"] == 1.0
    assert s["cross_checks"][0]["diff_vs_primary_pct"] == pytest.approx(-20) and "MODEL_DISAGREES" in s["flags"]


def test_input_validation(tmp_path):
    sd = _session(tmp_path)
    for bad in (dict(true_m=0), dict(true_m=-1), dict(true_m=float("nan")), dict(role="guess"),
                dict(p2=[0, 0, 0]), dict(p2=[float("inf"), 0, 0]), dict(pick_tolerance_units=-1)):
        args = {"p1": [0, 0, 0], "p2": [1, 0, 0], "true_m": 1.0, "role": "fit", **bad}
        with pytest.raises(ValueError):
            ms.add_two_point(sd, **args)
    with pytest.raises(ValueError):
        ms.add_two_point(tmp_path / "nomap", [0, 0, 0], [1, 0, 0], 1.0, "fit")


def test_marker_sheet_reads_back_its_id(tmp_path):
    man = markers.make_sheets("4X4_50", [0, 1], 160, tmp_path)
    assert all(s["readback_ok"] for s in man["sheets"]) and man["page_px"] == [2480, 3508]
    img = cv2.imread(str(tmp_path / "aruco_4x4_50_id1.png"))
    found = markers.detect(cv2.resize(img, (620, 877), interpolation=cv2.INTER_AREA), "4X4_50")
    assert [i for i, _ in found] == [1]
    assert markers.side_lengths_px(found[0][1]).min() == pytest.approx(160 / 25.4 * 300 / 4, rel=0.02)
