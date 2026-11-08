"""Export contract with non-trivial poses: camera centre C = -R^T t, axes, one frame for points and cameras, time order."""
import json
from types import SimpleNamespace as NS

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from mapping.export import export_reconstruction, read_ply


class _Rigid:
    def __init__(self, R, t):
        self.rotation = NS(matrix=lambda: R)
        self.translation = t

    def __mul__(self, X):
        return self.rotation.matrix() @ X + self.translation


def _fake(cams, pts, names, ids, K=(500.0, 320.0, 240.0)):
    """cams: list of (R_cw, t_cw); pts: Nx3 world points (all seen by all cameras)."""
    images = {}
    for (R, t), name, iid in zip(cams, names, ids):
        images[iid] = NS(name=name, image_id=iid, camera_id=1, cam_from_world=lambda R=R, t=t: _Rigid(R, t), num_points3D=len(pts))
    points = {k: NS(xyz=np.asarray(X, float), color=np.array([10, 20, 30]),
                    track=NS(elements=[NS(image_id=i) for i in images])) for k, X in enumerate(pts)}
    cam = NS(model=NS(name="SIMPLE_PINHOLE"), width=640, height=480, params=np.array(K))
    return NS(reg_image_ids=lambda: list(images), image=lambda i: images[i], points3D=points,
              num_points3D=lambda: len(points), camera=lambda cid: cam)


def _setup(seed=0):
    rng = np.random.default_rng(seed)
    axis = np.array([0.3, -0.8, 0.52]); axis /= np.linalg.norm(axis)
    cams = []
    for k in range(3):
        R = Rotation.from_rotvec(axis * np.radians(37 + 11 * k)).as_matrix()
        t = rng.normal(0, 3, 3)
        cams.append((R, t))
    pts = rng.normal(0, 2, (5, 3))
    return cams, pts


def test_camera_centre_rotation_and_projection(tmp_path):
    cams, pts = _setup()
    names, ids = ["f000002.jpg", "f000000.jpg", "f000001.jpg"], [1, 2, 3]
    stamps = {"f000000.jpg": 0.0, "f000001.jpg": 0.5, "f000002.jpg": 1.0}  # id 1 is the LATEST frame
    export_reconstruction(_fake(cams, pts, names, ids), tmp_path, {n: {"timestamp_s": s} for n, s in stamps.items()})
    traj = json.loads((tmp_path / "trajectory.json").read_text())
    assert [p["image_name"] for p in traj["poses"]] == ["f000000.jpg", "f000001.jpg", "f000002.jpg"]
    by_name = dict(zip(names, cams))
    for p in traj["poses"]:
        R, t = by_name[p["image_name"]]
        T = np.array(p["T_world_camera"])
        assert np.allclose(T[:3, :3], R.T, atol=1e-6) and np.allclose(T[:3, 3], -R.T @ t, atol=1e-6)
        assert np.allclose(np.linalg.inv(T)[:3, :3], R, atol=1e-6) and np.allclose(np.linalg.inv(T)[:3, 3], t, atol=1e-5)
        # a world point expressed with the exported camera-to-world pose lands where cam_from_world puts it
        for X in pts:
            Xc_export = T[:3, :3].T @ (X - T[:3, 3])
            assert np.allclose(Xc_export, R @ X + t, atol=1e-5)


def test_first_seen_follows_time_not_image_id(tmp_path):
    cams, pts = _setup(1)
    names, ids = ["b.jpg", "a.jpg", "c.jpg"], [1, 2, 3]
    stamps = {"b.jpg": 2.0, "a.jpg": 0.0, "c.jpg": 1.0}
    rec = _fake(cams, pts, names, ids)
    for p in rec.points3D.values():
        p.track.elements = [NS(image_id=1)]  # only seen by the latest frame
    export_reconstruction(rec, tmp_path, {n: {"timestamp_s": s} for n, s in stamps.items()})
    traj = json.loads((tmp_path / "trajectory.json").read_text())
    assert [p["image_name"] for p in traj["poses"]] == ["a.jpg", "c.jpg", "b.jpg"]
    assert set(read_ply(tmp_path / "sparse.ply")["first_seen"]) == {2}


def test_points_and_cameras_share_one_frame_under_similarity(tmp_path):
    cams, pts = _setup(2)
    s, Rs, ts = 2.5, Rotation.from_euler("xyz", [20, -35, 50], degrees=True).as_matrix(), np.array([1.0, -2.0, 0.5])
    # world' = s Rs world + ts  =>  cam_from_world' = (R Rs^T, s t - R Rs^T ts)
    cams2 = [(R @ Rs.T, s * t - R @ Rs.T @ ts) for R, t in cams]
    pts2 = (s * (Rs @ pts.T)).T + ts
    names, ids = ["x0.jpg", "x1.jpg", "x2.jpg"], [1, 2, 3]
    meta = {n: {"timestamp_s": float(i)} for i, n in enumerate(names)}
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    export_reconstruction(_fake(cams, pts, names, ids), a, meta)
    export_reconstruction(_fake(cams2, pts2, names, ids), b, meta)

    def dists(d):
        C = np.array([np.array(p["T_world_camera"])[:3, 3] for p in json.loads((d / "trajectory.json").read_text())["poses"]])
        P = read_ply(d / "sparse.ply")
        X = np.stack([P["x"], P["y"], P["z"]], 1).astype(float)
        return np.linalg.norm(C[:, None, :] - X[None], axis=2)
    assert np.allclose(dists(b), s * dists(a), rtol=1e-5)
