import numpy as np

from learned.metric_model_trial import (depth_ratio, gate, model_per_unit, nearest, path_length, pct_diff, quaternion_to_R,
                                        ratio_stats, scale_K, scale_flag, sim3_scale)


def test_scale_K_plain_resize():
    K = scale_K([[1209.25, 0, 800], [0, 1209.25, 450], [0, 0, 1]], (1600, 900), (518, 294))
    assert np.isclose(K[0, 0], 1209.25 * 518 / 1600) and np.isclose(K[1, 1], 1209.25 * 294 / 900)
    assert np.isclose(K[0, 2], 259.0) and np.isclose(K[1, 2], 147.0)


def test_depth_ratio_and_stats():
    gt = np.full((20, 20), 2.0); gt[0] = 0  # invalid ground truth row ignored
    pred = gt * 1.1
    r = depth_ratio(pred, gt)
    assert len(r) == 380 and np.allclose(r, 1.1)
    s = ratio_stats(r)
    assert np.isclose(s["median"], 1.1) and s["n"] == 380
    assert ratio_stats([1.0] * 5) is None


def test_path_length_and_sim3_scale():
    G = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [1, 1, 1.0]])
    assert np.isclose(path_length(G), 3.0)
    R = quaternion_to_R(0, 0, np.sin(0.3), np.cos(0.3))
    P = 2.5 * G @ R.T + np.array([3, -1, 2])
    assert np.isclose(sim3_scale(G, P), 2.5) and np.isclose(path_length(P) / path_length(G), 2.5)


def test_scale_flag_threshold():
    assert scale_flag(1.10, 1.0)["flag"] == "WITHIN_15PCT"
    assert scale_flag(0.80, 1.0)["flag"] == "MODEL_MISMATCH"
    assert np.isclose(pct_diff(1.2, 1.0), 20.0)


def test_model_per_unit_recovers_scale():
    K = np.array([[100.0, 0, 50], [0, 100, 50], [0, 0, 1]]); R, t = np.eye(3), np.zeros(3)
    rng = np.random.default_rng(0)
    X = np.c_[rng.uniform(-1, 1, (200, 2)), rng.uniform(2, 4, 200)]
    depth = np.zeros((100, 100))
    u = np.round(100 * X[:, 0] / X[:, 2] + 50).astype(int); v = np.round(100 * X[:, 1] / X[:, 2] + 50).astype(int)
    depth[v, u] = 3.0 * X[:, 2]  # model says 3 model-units per map unit
    r = model_per_unit(depth, np.ones_like(depth, np.uint8), K, R, t, X, 100, 100)
    assert np.isclose(np.median(r), 3.0)


def test_nearest_association():
    assert nearest([1.0, 2.0, 5.0], [0.99, 2.03, 3.0], 0.02) == [0, -1, -1]


def _m(fs, cov):
    return {"errors": {"float_common": {"float_fs": fs}},
            "frames": {fn: {"classes": {c: {"cov1px": v} for c, v in cl.items()}} for fn, cl in cov.items()}}


def test_gate_pre_registered_rule():
    base = _m(0.15, {"a": {"floor": 0.5, "structure": 0.4, "far": 0.2}})
    assert gate(_m(0.159, {"a": {"floor": 0.5, "structure": 0.41, "far": 0.0}}), base)["verdict"] == "REPLACEMENT_CANDIDATE"
    assert gate(_m(0.161, {"a": {"floor": 0.6, "structure": 0.5}}), base)["verdict"] == "REFERENCE_ONLY"   # float_fs +1.1 pp
    g = gate(_m(0.10, {"a": {"floor": 0.49, "structure": 0.9}}), base)
    assert g["verdict"] == "REFERENCE_ONLY" and any("floor" in f for f in g["fails"])
