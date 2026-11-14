"""Display / cleanup filters on small synthetic clouds: points_filter (sparse C5 rule), clean_cloud (lonely cubes),
far_cleanup (vote rule for far points). No COLMAP run, no files beyond tmp_path."""
from types import SimpleNamespace

import numpy as np

from mapping import clean_cloud, far_cleanup, points_filter
from mapping.dense_eval import Cam
from mapping.export import read_ply


def pinhole(w=64, h=48, f=50.0, R=None, t=None) -> Cam:
    """A real dense_eval.Cam without a reconstruction behind it (identity pose by default)."""
    c = object.__new__(Cam)
    c.K = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1]], float)
    c.dist, c.w, c.h, c.scale = np.zeros(5), w, h, 1.0
    c.R = np.eye(3) if R is None else R
    c.t = np.zeros(3) if t is None else np.asarray(t, float)
    return c


# ---------- points_filter ----------
class _Image(SimpleNamespace):
    def projection_center(self):
        return self.center


def _rec(points):
    images = {i: _Image(name=f"f{i:06d}.jpg", has_pose=True, center=np.array([x, 0.0, 0.0]))
              for i, x in enumerate([0.0, 1.0, 2.0, 3.0])}
    pts = {}
    for pid, (xyz, ids, err) in enumerate(points):
        track = SimpleNamespace(elements=[SimpleNamespace(image_id=i) for i in ids])
        pts[pid] = SimpleNamespace(xyz=np.array(xyz, float), color=np.array([10, 20, 30]), error=err, track=track)
    return SimpleNamespace(images=images, points3D=pts)


def test_points_filter_applies_each_rule_and_keeps_the_rest(tmp_path):
    good = ([1.5, 0, 5], [0, 1, 2, 3], 0.5)
    rec = _rec([good, good, good,
                ([1.5, 0, 5], [0, 1], 0.5),           # track of 2 < 3
                ([1000.0, 0, 1e5], [0, 1, 2], 0.5),   # all rays nearly parallel: angle < 1.5 deg
                ([1.5, 0, 5], [1, 2, 3], 5.0)])       # reprojection 5.0 > 2 x median (0.5)
    res = points_filter.filter_model(rec, tmp_path / "f.ply")
    assert res["raw"] == 6 and res["filtered"] == 3
    assert res["removed_by"] == {"angle": 1, "track": 1, "reproj": 1}
    assert res["rule"]["max_reprojection_px"] == 1.0
    ply = read_ply(tmp_path / "f.ply")
    assert len(ply) == 3 and set(ply["first_seen"]) == {0} and ply["red"].tolist() == [10, 10, 10]


def test_points_filter_first_seen_is_trajectory_order_of_the_first_observer(tmp_path):
    rec = _rec([([1.5, 0, 5], [3, 2, 1], 0.5), ([1.5, 0, 5], [0, 1, 2], 0.5)])
    points_filter.filter_model(rec, tmp_path / "f.ply")
    assert sorted(read_ply(tmp_path / "f.ply")["first_seen"].tolist()) == [0, 1]


# ---------- clean_cloud ----------
def test_clean_drops_lonely_cubes_and_keeps_every_other_point_unchanged():
    g = np.stack(np.meshgrid(np.arange(6), np.arange(6), [0.0]), -1).reshape(-1, 3) * 0.1 + 0.05  # 36 cubes, one layer
    dense = np.repeat(g, 3, axis=0) + np.tile([[0.0, 0.0, 0.0], [0.01, 0, 0], [0, 0.01, 0]], (36, 1))
    lonely = np.array([[5.0, 5.0, 5.0], [9.0, 9.0, 9.0]])
    X = np.concatenate([dense, lonely])
    C = np.arange(len(X) * 3).reshape(-1, 3) % 256
    P, Q, n_cubes, n_lonely = clean_cloud.clean(X, C, vox=0.1, min_nb=3)
    assert n_cubes == 38 and n_lonely == 2
    assert len(P) == len(dense) and np.array_equal(P, dense) and np.array_equal(Q, C[: len(dense)])


def test_clean_merge_replaces_each_kept_cube_by_its_mean():
    X = np.stack(np.meshgrid(np.arange(4), np.arange(4), [0.0]), -1).reshape(-1, 3) * 0.1 + 0.05
    X = np.concatenate([X, X + [0.02, 0, 0]])
    P, Q, n_cubes, n_lonely = clean_cloud.clean(X, np.full((len(X), 3), 100), vox=0.1, min_nb=2, merge=True)
    assert n_cubes == 16 and n_lonely == 0 and len(P) == 16
    assert np.allclose(np.sort(P[:, 0])[:4], 0.06) and np.allclose(Q, 100)


# ---------- far_cleanup ----------
def test_far_points_are_relative_to_the_median_camera_distance():
    centers = np.zeros((1, 3))
    X = np.array([[0, 0, 1.0], [0, 0, 1.0], [0, 0, 1.1], [0, 0, 2.5], [0, 0, 9.0]])
    far, med = far_cleanup.far_points(X, centers)
    assert med == 1.1 and far.tolist() == [False, False, False, True, True]


def test_far_votes_support_conflict_and_removal_rule():
    cam = pinhole()
    X = np.array([[0.0, 0.0, 10.0], [0.0, 0.0, 10.0], [100.0, 0.0, 10.0]])  # third point projects outside the image
    agree = np.full((48, 64), 10.5)       # within 10 %
    past = np.full((48, 64), 12.0)        # sees 20 % beyond the point: free space
    unknown = np.full((48, 64), np.nan)   # no confident depth
    s, c = far_cleanup.votes(X, [(cam, agree), (cam, agree), (cam, past), (cam, unknown)])
    assert s.tolist() == [2, 2, 0] and c.tolist() == [1, 1, 0]
    assert far_cleanup.removed(s, c).tolist() == [False, False, True]
    s, c = far_cleanup.votes(X[:1], [(cam, agree), (cam, past), (cam, past)])
    assert far_cleanup.removed(s, c).tolist() == [True]  # one view agrees, two see past it
