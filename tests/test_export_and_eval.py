import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from mapping.evaluate import associate, umeyama
from mapping.export import PLY_DTYPE, PlyError, read_ply, write_ply


def test_ply_roundtrip(tmp_path):
    pts = np.zeros(3, PLY_DTYPE)
    pts["x"], pts["red"], pts["first_seen"] = [1.5, -2, 3], [255, 0, 7], [0, 4, 9]
    write_ply(tmp_path / "a.ply", pts)
    back = read_ply(tmp_path / "a.ply")
    assert back.dtype.names == PLY_DTYPE.names and np.array_equal(back, pts)


def test_truncated_ply_is_rejected(tmp_path):
    pts = np.zeros(4, PLY_DTYPE)
    write_ply(tmp_path / "a.ply", pts)
    data = (tmp_path / "a.ply").read_bytes()
    (tmp_path / "b.ply").write_bytes(data[:-5])
    with pytest.raises(PlyError, match="expected"):
        read_ply(tmp_path / "b.ply")
    (tmp_path / "c.ply").write_text("ply\nformat ascii 1.0\nend_header\n")
    with pytest.raises(PlyError, match="binary_little_endian"):
        read_ply(tmp_path / "c.ply")


def test_umeyama_recovers_known_similarity():
    rng = np.random.default_rng(0)
    src = rng.normal(size=(30, 3))
    R = Rotation.from_euler("xyz", [10, -40, 75], degrees=True).as_matrix()
    dst = 0.31 * src @ R.T + [1, 2, -3]
    s, R_est, t = umeyama(src, dst)
    assert s == pytest.approx(0.31) and np.allclose(R_est, R) and np.allclose(t, [1, 2, -3])


def test_umeyama_rejects_degenerate_input():
    with pytest.raises(ValueError):
        umeyama(np.zeros((5, 3)), np.ones((5, 3)))


def test_associate_respects_max_dt():
    pairs = associate(np.array([1.0, 2.0, 3.5]), np.array([0.99, 2.03, 3.0, 4.0]), max_dt=0.02)
    assert pairs == [(0, 0)]
