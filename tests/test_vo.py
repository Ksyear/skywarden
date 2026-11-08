"""Tests for packages/vision/vo.py (monocular VO, RELATIVE scale, no loop closure, NOT_NAVIGATION_SAFE).

Acceptance criteria — written BEFORE the first run (2026-11-09) and not changed afterwards:
  S1 synthetic room (sim.synthetic_scene, seed 7, 40 frames, 480x360, exact K):
       one segment, no LOST frame, >= 80 % of frames with a pose, and
       ATE RMSE after Sim(3) Umeyama alignment <= 5 % of the ground-truth path length.
  S2 pure rotation (camera fixed at the room centre, yaw 0 -> 20 deg over 25 frames):
       >= 1 keyframe step, no TRACKING frame, final state LOW_CONFIDENCE,
       every reported camera centre within 1e-6 of the origin (no invented translation).
  S3 inserted unrelated image (frame 15 of S1 replaced by a frame from a seed-99 room):
       frame 15 is LOST, >= 2 segments, segment ids never decrease.
  Real public clips (run by hand, numbers in outputs/evidence/vo/README.txt):
  R1 Sumida forward walk (60 s): >= 50 % TRACKING frames; longest segment, Sim(3)-aligned to the stored
       COLMAP trajectory on matching timestamps, RMSE <= 15 % of the COLMAP path length over the matched
       poses (consistency with SfM, NOT ground truth).
  R2 Bangkok edited vlog (60 s): >= 80 % of the 18 cuts found by mapping/shots (map_quality/shots evidence)
       coincide (+-0.3 s) with a VO LOST frame; >= 10 segments.
  R3 Montreal tripod (60 s): <= 10 % TRACKING frames and >= 80 % INITIALIZING/LOW_CONFIDENCE frames.
  T  timing: p95 VO processing <= 66 ms per frame at 640 px width (keeps up with 15 Hz processing).
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from mapping.evaluate import associate, load_tum_groundtruth, umeyama
from vision.vo import VOParams, run


def _load_dir(d):
    from vision.vo import iter_image_dir
    meta, frames = iter_image_dir(d)
    return meta, list(frames)


@pytest.fixture(scope="module")
def room(tmp_path_factory):
    from sim.synthetic_scene import generate
    return generate(tmp_path_factory.mktemp("vo") / "room", n_frames=40, size=(480, 360), seed=7)


K_SYN = np.array([[400.0, 0, 240.0], [0, 400.0, 180.0], [0, 0, 1]])


def synthetic_ate(res, gt_path):
    poses = [f for f in res["frames"] if f["T_world_camera"] is not None]
    est_t = np.array([p["t"] for p in poses])
    est_c = np.array([np.array(p["T_world_camera"])[:3, 3] for p in poses])
    gt = load_tum_groundtruth(gt_path)
    pairs = associate(est_t, gt[:, 0])
    src, dst = est_c[[i for i, _ in pairs]], gt[[k for _, k in pairs], 1:4]
    s, R, t = umeyama(src, dst)
    err = np.linalg.norm((s * (R @ src.T)).T + t - dst, axis=1)
    path = float(np.linalg.norm(np.diff(gt[:, 1:4], axis=0), axis=1).sum())
    return float(np.sqrt((err ** 2).mean())), path, len(pairs)


def test_s1_synthetic_ate(room):
    meta, frames = _load_dir(room)
    res = run(frames, K_SYN, meta["work_size"])
    states = [f["state"] for f in res["frames"]]
    assert res["summary"]["num_segments"] == 1
    assert "LOST" not in states
    n_pose = sum(f["T_world_camera"] is not None for f in res["frames"])
    assert n_pose >= 0.8 * len(frames)
    rmse, path, n = synthetic_ate(res, room / "groundtruth.txt")
    assert rmse <= 0.05 * path, (rmse, path, n)


def _rotation_frames(n=25, yaw_deg=20.0):
    from sim import synthetic_scene as ss
    rng = np.random.default_rng(7)
    tex = [ss._texture(rng) for _ in ss._PLANES]
    out = []
    for i, a in enumerate(np.linspace(0, yaw_deg, n)):
        R = Rotation.from_euler("y", np.deg2rad(a)).as_matrix()
        out.append((i / 10.0, ss.render(K_SYN, R, np.zeros(3), (480, 360), tex)))
    return out


def test_s2_pure_rotation_no_translation():
    res = run(_rotation_frames(), K_SYN, (480, 360))
    fr = res["frames"]
    assert res["summary"]["steps"]["keyframe_steps"] >= 1
    assert all(f["state"] != "TRACKING" for f in fr)
    assert fr[-1]["state"] == "LOW_CONFIDENCE"
    for f in fr:
        if f["T_world_camera"] is not None:
            assert np.linalg.norm(np.array(f["T_world_camera"])[:3, 3]) <= 1e-6


def test_s3_inserted_image_starts_new_segment(room, tmp_path):
    from sim.synthetic_scene import generate
    other = generate(tmp_path / "other", n_frames=2, size=(480, 360), seed=99)
    meta, frames = _load_dir(room)
    _, oframes = _load_dir(other)
    frames[15] = (frames[15][0], oframes[0][1])
    res = run(frames, K_SYN, meta["work_size"], VOParams())
    fr = res["frames"]
    assert fr[15]["state"] == "LOST", fr[15]
    segs = [f["segment"] for f in fr]
    assert res["summary"]["num_segments"] >= 2
    assert all(b >= a for a, b in zip(segs, segs[1:]))
