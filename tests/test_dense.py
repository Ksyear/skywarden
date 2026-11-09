"""Dense coverage changes (docs/PLAN_DENSE_COVERAGE.md): ground-plane sweep on an obliquely seen floor, multi-view
verification semantics and the ground-plane fit, on synthetic data with known geometry."""
from types import SimpleNamespace

import cv2
import numpy as np

from mapping.dense_cpu import (V_DISAGREE, V_KEPT, V_NO_DEPTH, V_OUTSIDE, View, _verify_multi, ground_plane,
                               plane_sweep)

W, H, F, HEIGHT = 320, 180, 200.0, 1.0
K = np.array([[F, 0, W / 2], [0, F, H / 2], [0, 0, 1.0]])
rng = np.random.default_rng(3)
TEX = cv2.GaussianBlur((rng.random((2048, 2048)) * 255).astype(np.float32), (0, 0), 2.0)


def render_floor(C):
    """Camera at C (world = camera axes, y down), floor plane y = HEIGHT, textured; sky above the horizon is flat."""
    u, v = np.meshgrid(np.arange(W), np.arange(H))
    rays = np.stack([(u - W / 2) / F, (v - H / 2) / F, np.ones_like(u, float)], -1)
    img = np.full((H, W), 128.0, np.float32)
    hit = rays[..., 1] > 1e-3
    s = (HEIGHT - C[1]) / np.where(hit, rays[..., 1], 1)
    X = C[0] + rays[..., 0] * s
    Z = C[2] + rays[..., 2] * s
    mx = np.where(hit, X * 100 + 1024, -1).astype(np.float32)
    mz = np.where(hit, Z * 100 + 100, -1).astype(np.float32)
    tex = cv2.remap(TEX, mx, mz, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    img[hit] = tex[hit]
    depth = np.where(hit, s, np.nan)  # z-depth equals the ray scale because rays have z = 1
    return cv2.cvtColor(np.clip(img, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR), depth


def fake_view(C):
    v = View.__new__(View)
    v.K, v.dist = K.copy(), np.zeros(5)
    v.img, v.true_depth = render_floor(np.asarray(C, float))
    v.R, v.t = np.eye(3), -np.asarray(C, float)
    v.C = np.asarray(C, float)
    v.depth = None
    return v


def test_ground_sweep_recovers_oblique_floor_that_fronto_sweep_misses():
    ref = fake_view([0, 0, 0])
    nbrs = [fake_view([0.05, 0, z]) for z in (0.25, -0.25, 0.5, -0.5)]  # walking forward, like the production 4 neighbours
    floor = ~np.isnan(ref.true_depth) & (np.arange(H)[:, None] > H / 2 + 15)  # clearly below the horizon
    d_f, c_f, _, _ = plane_sweep(ref, nbrs, 1.5, 20.0)
    d_g, c_g, _, dg = plane_sweep(ref, nbrs, 1.5, 20.0, ground=(np.array([0, 1.0, 0]),
                                  [1.0 / x for x in np.linspace(1 / (2 * HEIGHT), 1 / (0.5 * HEIGHT), 64)]))
    share_f, share_g = (c_f & floor).sum() / floor.sum(), (c_g & floor).sum() / floor.sum()
    assert share_g > share_f + 0.1, (share_f, share_g)
    m = c_g & floor & (dg["chosen"] == 1)
    err = np.abs(d_g[m] - ref.true_depth[m]) / ref.true_depth[m]
    assert np.median(err) < 0.03 and np.mean(err < 0.10) > 0.9
    # flat sky never gets depth; within a window half-width (3 px) of the horizon the 7x7 window still sees floor
    # texture, so those rows can inherit floor depth (known edge fattening of window matching)
    sky = np.isnan(ref.true_depth) & (np.arange(H)[:, None] < H / 2 - 4)
    assert not (c_g & sky).any()


def _view_with_depth(C, depth_value):
    v = fake_view(C)
    v.depth = np.full((H, W), depth_value, np.float32)
    return v


def test_static_masks_exclude_reference_pixels_and_require_neighbour_support():
    ref = fake_view([0, 0, 0])
    neighbours = [fake_view([0.05, 0, z]) for z in (0.25, -0.25, 0.5, -0.5)]
    ref.static_keep = np.ones((H, W), dtype=np.float32)
    ref.static_keep[120:150, 125:195] = 0
    for neighbour in neighbours:
        neighbour.static_keep = np.ones((H, W), dtype=np.float32)
    _, confident, _, _ = plane_sweep(ref, neighbours, 1.5, 20.0)
    assert confident.any()
    assert not confident[117:153, 122:198].any()
    for neighbour in neighbours[1:]:
        neighbour.static_keep[:] = 0
    _, confident, _, _ = plane_sweep(ref, neighbours, 1.5, 20.0)
    assert not confident.any()


def test_multi_verification_keeps_agreeing_drops_contradicted_and_unobserved():
    # world points seen by a verifier whose depth map reads 5 everywhere: depth 5 agrees; depth 3 lies in space the
    # verifier saw through (it sees 5 behind it) -> contradiction; depth 8 is behind what it sees (occlusion: neutral,
    # but without an agreeing view it is dropped); the last point is outside the image
    Xw = np.array([[0, 0, 5.0], [-1.0, 0, 3.0], [4.0, 0, 8.0], [100.0, 0, 1.0]])
    st = {"Xw": Xw}
    ver = _view_with_depth([0, 0, 0], 5.0)
    good, code, checked = _verify_multi(st, [ver], min_agree=1)
    assert list(good) == [True, False, False, False]
    assert code[0] == V_KEPT and code[1] == V_DISAGREE and code[2] == V_DISAGREE and code[3] == V_OUTSIDE
    # a second view that sees depth 8 only where the occluded point projects keeps that point; it has no depth at the
    # other points, so it cannot rescue the contradicted one
    ver2 = _view_with_depth([0, 0, 0], np.nan)
    u2 = int(round(F * 4.0 / 8.0 + W / 2))
    ver2.depth[:, u2 - 2:u2 + 3] = 8.0
    good, _, _ = _verify_multi(st, [ver, ver2], min_agree=1)
    assert list(good[:3]) == [True, False, True]
    # no depth at the pixel -> NO_DEPTH, dropped
    ver3 = _view_with_depth([0, 0, 0], np.nan)
    good, code, _ = _verify_multi(st, [ver3], min_agree=1)
    assert not good.any() and code[0] == V_NO_DEPTH


def test_ground_plane_fit_from_sparse_points():
    pts = {}
    r = np.random.default_rng(0)
    for i in range(200):  # floor points below the camera plus clutter above the horizon
        pts[i] = SimpleNamespace(xyz=np.array([r.uniform(-3, 3), HEIGHT + r.normal(0, 0.005), r.uniform(2, 10)]))
    for i in range(200, 260):
        pts[i] = SimpleNamespace(xyz=np.array([r.uniform(-3, 3), r.uniform(-2, 0.3), r.uniform(2, 10)]))
    rec = SimpleNamespace(points3D=pts)
    v = fake_view([0, 0, 0])
    v.obs = set(pts)
    n, d, share, _ = ground_plane(v, rec)
    assert abs(d - HEIGHT) < 0.02 and n[1] > 0.999 and share > 0.7


def test_border_exclusion_drops_windows_that_leave_the_image():
    ref = fake_view([0, 0, 0])
    nbrs = [fake_view([0.05, 0, z]) for z in (0.25, -0.25, 0.5, -0.5)]
    _, conf, _, _ = plane_sweep(ref, nbrs, 1.5, 20.0, border_exclusion=True)
    assert conf.any()
    assert not conf[:, :3].any() and not conf[:, -3:].any() and not conf[:3].any() and not conf[-3:].any()


def test_valid_pixel_cost_keeps_surface_next_to_a_mask_and_never_inside_it():
    ref = fake_view([0, 0, 0])
    nbrs = [fake_view([0.05, 0, z]) for z in (0.25, -0.25, 0.5, -0.5)]
    keep = np.ones((H, W), np.float32)
    keep[130:160, 120:200] = 0  # a masked object standing on the floor
    ref.static_keep = keep
    for nb in nbrs:
        nb.static_keep = np.ones((H, W), np.float32)
    ring = np.zeros((H, W), bool)
    ring[127:163, 117:203] = True
    ring[130:160, 120:200] = False  # pixels 1-3 px from the mask: their 7x7 window touches it
    G = (np.array([0, 1.0, 0]), [1.0 / x for x in np.linspace(0.5, 2.0, 64)])
    plain = fake_view([0, 0, 0])
    _, c_plain, _, _ = plane_sweep(plain, [fake_view([0.05, 0, z]) for z in (0.25, -0.25, 0.5, -0.5)], 1.5, 20.0, ground=G)
    _, c_erode, _, _ = plane_sweep(ref, nbrs, 1.5, 20.0, ground=G)
    d_val, c_val, _, _ = plane_sweep(ref, nbrs, 1.5, 20.0, ground=(np.array([0, 1.0, 0]), [1.0 / x for x in np.linspace(0.5, 2.0, 64)]),
                                     mask_cost="valid_pixels")
    assert not c_val[130:160, 120:200].any() and not c_erode[130:160, 120:200].any()
    # erosion removes the whole ring; valid-pixel scoring keeps at least half of what an unmasked scene gives there
    assert c_erode[ring].mean() < 0.05 and c_val[ring].mean() >= 0.5 * c_plain[ring].mean(), \
        (c_erode[ring].mean(), c_val[ring].mean(), c_plain[ring].mean())
    m = c_val & ring
    err = np.abs(d_val[m] - ref.true_depth[m]) / ref.true_depth[m]
    assert np.mean(err < 0.10) > 0.9
