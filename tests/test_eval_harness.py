"""Measurement harness functions (dense_eval / static_eval) on hand-built cameras, depth maps and clouds."""
import numpy as np

from mapping import dense_eval, static_eval
from mapping.dense_cpu import MAX_COST, V_DISAGREE, V_KEPT, V_OUTSIDE

from test_display_filters import pinhole


def _npz(h=4, w=5):
    """One fronto family, every pixel a clean candidate unless a test breaks it."""
    best = np.full((h, w), 0.1)
    return {"families": np.array(["fronto"]), "n_planes": np.array([64]), "conf": np.zeros((h, w), bool),
            "fronto_best": best, "fronto_second": best + 0.5, "fronto_idx": np.full((h, w), 10), "v0": np.full((h, w), 100.0),
            "verify_code": np.full((h, w), V_OUTSIDE, np.int8)}


def test_stage_metrics_counts_each_pixel_once_at_its_first_failing_gate():
    z = _npz()
    z["fronto_best"][0, 0] = 2.0                    # no candidate
    z["fronto_best"][0, 1] = MAX_COST + 0.1          # cost (and margin: reported as cost only)
    z["fronto_second"][0, 1] = MAX_COST + 0.11
    z["fronto_second"][0, 2] = 0.1                   # margin
    z["v0"][0, 3] = 1.0                              # texture
    z["fronto_idx"][0, 4] = 63                       # boundary_near (last plane is the nearest)
    z["fronto_idx"][1, 0] = 0                        # boundary_far
    z["conf"][2:, :] = True                          # 10 confident pixels
    z["verify_code"][2, :] = V_KEPT                  # 5 kept
    z["verify_code"][3, :2] = V_DISAGREE             # 2 disagree, 3 outside
    m = dense_eval.stage_metrics(z, np.ones((4, 5), bool))
    r = m["conf_rejections"]
    assert m["region_px"] == 20 and m["conf"] == 0.5 and m["cand"] == 0.95
    assert [r[k] for k in ("no_candidate", "cost", "margin", "texture", "boundary_near", "boundary_far")] == [0.05] * 6
    assert r["other"] == 0.2  # (1,1)-(1,4): good candidates that were not confident for another reason
    assert m["verif_of_conf"] == 0.5 and m["verif"] == 0.25
    assert m["verify_drops_of_conf"]["disagree"] == 0.2 and m["verify_drops_of_conf"]["outside_partner"] == 0.3
    m = dense_eval.stage_metrics(z, np.zeros((4, 5), bool))
    assert m["region_px"] == 0 and m["conf"] == 0.0  # empty region: no division by zero


def test_project_cover_and_render_keep_the_nearest_point_per_pixel():
    cam = pinhole()
    X = np.array([[0, 0, 5.0], [0, 0, 10.0], [0.5, 0, 5.0], [0, 0, -1.0]])  # last point is behind the camera
    hit, hitz, ins, u, v, z = dense_eval.project_cover(cam, X, voxel=0.1, mask_d=None)
    assert ins.tolist() == [True, True, True, False] and hit.sum() == 2
    assert hitz.sum() == 2  # the far point shares the near point's pixel and fails the z-buffer test
    C = np.array([[255, 0, 0], [0, 255, 0], [0, 0, 255], [9, 9, 9]], np.uint8)
    img, n = dense_eval.render(cam, X, C, size=(64, 48))
    assert n == 3 and img[24, 32].tolist() == [0, 0, 255]  # RGB red stored as BGR; the near red point wins


def test_floating_flags_points_a_view_sees_past_and_isolated_points():
    cam = pinhole()
    X = np.concatenate([np.array([[0, 0, 10.0]]) + np.random.default_rng(0).normal(0, 0.001, (5, 3)),
                        np.array([[3.0, 0, 10.0]])])
    maps = {"refs": np.array([0]), "d0": np.full((48, 64), 10.0)}
    maps["d0"][24, 47] = 20.0  # the view sees far past the isolated point at u = 32 + 50 * 0.3 = 47
    res = _with_cams(dense_eval, {0: cam}, lambda: dense_eval.floating(None, [0], X, 0.01, maps, 64))
    # five clustered points have 4 neighbours within 2 voxels each; only the isolated one is a kNN floater
    assert res["fs_checked"] == 6 and res["float_fs"] == round(1 / 6, 4) and res["float_knn"] == round(1 / 6, 4)


def test_heldout_inconsistency_uses_a_view_outside_the_verifier_set():
    X = np.array([[0, 0, 10.0], [0.1, 0, 10.0]])
    raw = {"ref": np.array([0, 0]), "P": X}
    agree, wrong = np.full((48, 64), 10.2), np.full((48, 64), 13.0)
    maps = {"refs": np.array([0, 1, 5]), "d0": agree, "d1": agree, "d5": wrong}
    cams = {k: pinhole() for k in (0, 1, 5)}
    res = _with_cams(dense_eval, cams, lambda: dense_eval.heldout_inconsistency(None, [0, 1, 2, 3, 4, 5], raw, maps, {"0": [5]}, 64))
    assert res == {"points": 2, "checked": 2, "inconsist": 0.0, "inconsist_unchecked": 0.0}  # view 5 is a verifier: view 1 judges
    res = _with_cams(dense_eval, cams, lambda: dense_eval.heldout_inconsistency(None, [0, 1, 2, 3, 4, 5], raw, maps, {"0": [1]}, 64))
    assert res["inconsist"] == 1.0  # now view 5 (k + 5) is the held-out judge and disagrees by 30 %


def test_static_eval_free_space_flags_and_judge_reindexing(tmp_path):
    cam = pinhole()
    X = np.array([[0, 0, 10.0], [0, 0, 10.0 / 1.2]])
    maps = {"refs": np.array([3]), "d3": np.full((48, 64), 10.5)}
    flags = _with_cams(static_eval, {3: cam}, lambda: static_eval.fs_flags(None, {3: 3}, X, maps, 64))
    assert flags.tolist() == [False, True]
    u, v, z, ins = static_eval._project(cam, X)
    assert ins.all() and u.tolist() == [32, 32]
    np.savez(tmp_path / "judge.npz", refs=np.array([7, 9]), names=np.array(["a.jpg", "b.jpg"]),
             d7=np.zeros((2, 2)), d9=np.ones((2, 2)))
    m = static_eval._judge(tmp_path / "judge.npz", {"a.jpg": 0, "b.jpg": 4})
    assert m["refs"].tolist() == [0, 4] and m["d4"].sum() == 4 and m["d0"].sum() == 0


def _with_cams(module, cams, fn):
    """Run fn with module.Cam replaced by a lookup of pre-built cameras (ims[k] is the key)."""
    orig = module.Cam
    module.Cam = lambda rec, im, ww: cams[im]
    try:
        return fn()
    finally:
        module.Cam = orig
