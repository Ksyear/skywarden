"""Occlusion handling (docs/PLAN_OCCLUSION_TRACKING.md): margin association, low-score stage, ORU, camera motion
compensation and in-memory appearance. Safety intent of tests/test_tracker_ambiguity.py is re-checked for every
variant: a hidden target never inherits someone else's detection, a contested case stops, LOST is never revived."""
import json
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

from control.yaw_controller import YawController
from sim.crossing_ext import variant_config
from tracking.gmc import GlobalMotion
from tracking.tracker import Detection, MultiTracker, TargetSelector, TargetState, TrackerConfig, TrackState

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = ["V0", "VA", "VA_full", "VAB", "VABC"]


def det(cx, cy, h=180.0, score=0.9):
    return Detection((cx - 0.2 * h, cy - h / 2, cx + 0.2 * h, cy + h / 2), score, "SYNTHETIC")


def crossing(cfg, hA=180.0, hB=150.0, hidden=(3.25, 4.75), jitter_px=4.0, fps=15.0, t_end=6.5, xc=440.0, embed=None):
    """tests/test_tracker_ambiguity.crossing_run with configurable heights and tracker."""
    tr, sel, ctl = MultiTracker(cfg, embedder=embed), TargetSelector(), YawController(640)
    rows = []
    for k in range(int(t_end * fps) + 1):
        t = k / fps
        a, b = (xc + 90 * (t - 4.0), 240.0), (xc - 90 * (t - 4.0), 240.0)
        a_vis = not (hidden[0] <= t < hidden[1]) and 0 < a[0] < 640
        dets = [det(*a, hA)] if a_vis else []
        if 0 < b[0] < 640:
            dets.append(det(b[0] + (jitter_px if abs(t - 4.0) < 1e-9 else 0.0), b[1], hB))
        frame = ({"A": (*a, hA)} if a_vis else {}) | ({"B": (*b, hB)} if 0 < b[0] < 640 else {}) if embed else None
        tr.update(t, dets, frame=frame)
        if sel.target_id is None and t >= 1.0:
            sel.select(min(tr.active(), key=lambda x: np.hypot(*(x.center - np.array(a)))).id)
        state, track = sel.evaluate(tr)
        cmd = ctl.command(t, state, track, t, t, 85)
        on = None
        if track is not None:
            da, db = (float(np.hypot(*(track.center - np.array(p)))) for p in (a, b))
            on = "A" if da < 25 and da <= db else "B" if db < 25 else None
        rows.append({"t": t, "state": state, "on": on, "a_visible": a_vis, "yaw": cmd.yaw, "stop": cmd.stop_reasons})
    return rows, sel, tr


def synthetic_embedder(identical=False):
    rng = np.random.default_rng(0)
    a = rng.normal(size=64)
    a /= np.linalg.norm(a)
    b = a.copy() if identical else rng.normal(size=64)
    b /= np.linalg.norm(b)
    base = {"A": a, "B": b}
    calls = []

    def embed(frame, boxes):
        calls.append(len(boxes))
        out = []
        for bx in boxes:
            c = np.array([(bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2])
            who = min(frame, key=lambda w: float(np.hypot(*(c - np.array(frame[w][:2])))))
            v = base[who] + rng.normal(0, 0.02, 64)
            out.append(v / np.linalg.norm(v))
        return np.array(out)
    embed.calls = calls
    return embed


@pytest.mark.parametrize("variant", VARIANTS)
def test_hidden_target_never_inherits_and_never_yaws_toward_b(variant):
    rows, _, _ = crossing(variant_config(variant))
    assert not [r["t"] for r in rows if r["state"] == TargetState.LOCKED and (not r["a_visible"] or r["on"] == "B")]
    assert not [r["t"] for r in rows if r["yaw"] != 0 and (r["state"] != TargetState.LOCKED or r["on"] == "B")]


@pytest.mark.parametrize("variant", ["VA", "VA_full", "VAB"])
def test_truly_contested_detection_stays_ambiguous_in_margin_mode(variant):
    """Same heights, B's detection exactly on A's prediction: motion cannot decide, so the target must surface
    AMBIGUOUS (or stay unobserved) and the controller must stop."""
    rows, _, _ = crossing(variant_config(variant), hA=170.0, hB=170.0, hidden=(3.85, 4.15), jitter_px=0.0)
    at = [r for r in rows if 3.8 < r["t"] < 4.3]
    assert any(r["state"] == TargetState.AMBIGUOUS for r in at)
    assert all(r["yaw"] == 0 for r in at if r["state"] != TargetState.LOCKED)


def test_margin_resolves_clearly_different_people_and_keeps_ids():
    """Two walkers on one row cross without occlusion; box heights differ (near / far). Strict mode stops at the
    crossing; margin mode keeps both ids and the target stays on A."""
    def run(cfg):
        tr, ids_a = MultiTracker(cfg), []
        for k in range(100):
            t = k / 15
            a, b = (100 + 90 * t, 240.0), (540 - 90 * t, 244.0)
            tr.update(t, [det(*a, 200.0), det(*b, 120.0)])
            near = [(float(np.hypot(*(x.center - np.array(a)))), x.id) for x in tr.active() if x.state == TrackState.LOCKED]
            ids_a += [min(near)[1]] if near and min(near)[0] < 25 else []
        return tr, ids_a
    strict, ids_s = run(TrackerConfig())
    margin, ids_m = run(variant_config("VA"))
    assert margin.stats.margin_resolved > 0 and margin.stats.created == 2 and len(set(ids_m)) == 1
    assert len(ids_m) > len(ids_s)  # more frames with a trusted lock on A


def test_low_score_stage_corrects_confirmed_tracks_but_never_spawns():
    cfg = replace(variant_config("VA_full"), min_score=0.35)
    tr = MultiTracker(cfg)
    for k in range(20):
        tr.update(k / 10, [det(100 + 4 * k, 240)])
    for k in range(20, 30):  # partly hidden: score drops below the high threshold
        tr.update(k / 10, [det(100 + 4 * k, 240, score=0.2)])
    assert tr.get(1).state == TrackState.LOCKED and tr.stats.low_score_hits == 10
    tr.update(3.1, [det(500, 240, score=0.2)])  # a low-score box far from every track
    assert [x.id for x in tr.active()] == [1] and tr.stats.created == 1
    strict = MultiTracker(TrackerConfig(min_score=0.35))
    for k in range(20):
        strict.update(k / 10, [det(100 + 4 * k, 240)])
    for k in range(20, 30):
        strict.update(k / 10, [det(100 + 4 * k, 240, score=0.2)])
    assert strict.get(1).state == TrackState.PREDICTING  # original behaviour: low boxes ignored


def test_oru_undoes_velocity_error_accumulated_while_coasting():
    """Walker stops behind an occluder; when seen again the re-updated filter's velocity is closer to the truth."""
    def run(oru):
        tr = MultiTracker(replace(variant_config("VA"), oru=oru))
        for k in range(30):
            tr.update(k / 15, [det(100 + 60 * k / 15, 240)])
        for k in range(30, 45):  # hidden 1 s; truly standing still at x = 220
            tr.update(k / 15, [])
        for k in range(45, 48):
            tr.update(k / 15, [det(220, 240)])
        return abs(float(tr.get(1).x[2]))
    assert run(True) < run(False)


def textured(seed=1, size=(480, 640)):
    rng = np.random.default_rng(seed)
    img = cv2.GaussianBlur(rng.integers(0, 255, size, dtype=np.uint8), (0, 0), 2.0)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def test_gmc_recovers_a_known_affine_and_refuses_flat_frames():
    big = textured(size=(600, 800))
    f0 = big[60:540, 80:720].copy()
    ang, s, tx, ty = 1.5, 1.02, 7.0, -4.0
    M = cv2.getRotationMatrix2D((320, 240), ang, s)
    M[:, 2] += (tx, ty)
    f1 = cv2.warpAffine(f0, M, (640, 480), borderMode=cv2.BORDER_REFLECT)
    g = GlobalMotion()
    assert g.estimate(f0, [])[0] is None
    A, why = g.estimate(f1, [(300, 150, 360, 330)])
    assert why == "OK"
    pts = np.array([[100, 100], [500, 120], [320, 400]], float)
    want = pts @ M[:, :2].T + M[:, 2]
    got = pts @ A[:, :2].T + A[:, 2]
    assert np.abs(got - want).max() < 1.0
    flat = np.full((480, 640, 3), 128, np.uint8)
    g2 = GlobalMotion()
    g2.estimate(flat, [])
    assert g2.estimate(flat, []) == (None, "LOW_TEXTURE")


def test_gmc_keeps_a_standing_person_on_track_while_the_camera_pans():
    big = textured(size=(480, 1200))[:, ::-1].copy()

    def run(cfg):
        tr, err = MultiTracker(cfg), []
        for k in range(40):
            t = k / 15
            dx = 150 * t if k < 20 else 150 * 20 / 15 - 120 * (t - 20 / 15)  # pans right, then turns back while hidden
            frame = big[:, int(dx):int(dx) + 640].copy()
            person_x = 500 - dx
            if not 0 < person_x < 640:
                break
            if 20 <= k < 30:  # hidden for 0.67 s while the camera keeps panning
                tr.update(t, [], frame=frame)
            else:
                tr.update(t, [det(person_x, 240)], frame=frame)
            if k == 29:
                err.append(abs(float(tr.get(1).x[0]) - person_x) if tr.get(1) else 1e9)
        return err[0], tr
    e_off, _ = run(variant_config("VA"))
    e_on, tr = run(variant_config("VAB"))
    assert tr.stats.gmc_applied > 30 and e_on < 5.0 and e_off > 3 * e_on


@pytest.mark.parametrize("variant", ["V0", "VA", "VAB", "VABC"])
def test_expired_id_is_never_revived_even_with_matching_appearance(variant):
    emb = synthetic_embedder()
    tr = MultiTracker(variant_config(variant), embedder=emb)
    for k in range(21):
        tr.update(k / 10, [det(100 + 40 * k / 10, 240)], frame={"A": (100 + 40 * k / 10, 240, 180)})
    t = 2.0 + 2.5
    tr.update(t, [det(100 + 40 * t, 240)], frame={"A": (100 + 40 * t, 240, 180)})
    assert tr.get(1) is None and [x.id for x in tr.active()] == [2]


def test_appearance_resolves_only_when_it_agrees_and_identical_appearance_changes_nothing():
    cfg = variant_config("VABC")
    base, _, base_tr = crossing(variant_config("VAB"), hA=170.0, hB=170.0, hidden=(3.85, 4.15), jitter_px=0.0)
    same, _, same_tr = crossing(cfg, hA=170.0, hB=170.0, hidden=(3.85, 4.15), jitter_px=0.0, embed=synthetic_embedder(identical=True))
    diff, _, diff_tr = crossing(cfg, hA=170.0, hB=170.0, hidden=(3.85, 4.15), jitter_px=0.0, embed=synthetic_embedder())
    for rows in (same, diff):
        assert not [r for r in rows if r["state"] == TargetState.LOCKED and (not r["a_visible"] or r["on"] == "B")]
    amb = lambda rows: sum(r["state"] == TargetState.AMBIGUOUS for r in rows)
    assert amb(same) >= amb(base) - 1  # identical clothes: appearance cannot (and must not) resolve anything
    assert diff_tr.stats.reid_contested_uses > 0 and diff_tr.stats.reid_embeddings > 0


def test_reid_veto_blocks_reconnection_to_someone_who_looks_different():
    """A coasting target, a different-looking person appears exactly at the predicted spot: 1x1 component, which
    the motion-only tracker would trust. With appearance the re-connection is refused (AMBIGUOUS, stop)."""
    emb = synthetic_embedder()
    tr, sel = MultiTracker(variant_config("VABC"), embedder=emb), TargetSelector()
    for k in range(20):
        x = 100 + 60 * k / 10
        tr.update(k / 10, [det(x, 240)], frame={"A": (x, 240, 180), "B": (900, 240, 180)})
    sel.select(1)
    for k in range(20, 25):
        tr.update(k / 10, [], frame={"A": (-500, 240, 180), "B": (900, 240, 180)})
    x = 100 + 60 * 2.5
    tr.update(2.5, [det(x, 240)], frame={"A": (-500, 240, 180), "B": (x, 240, 180)})  # B, not A, at A's spot
    st, _ = sel.evaluate(tr)
    assert tr.stats.reid_vetoes == 1 and st == TargetState.AMBIGUOUS


def test_embeddings_are_never_written_by_a_recorded_tracking_run(tmp_path, monkeypatch):
    """Recorded run with appearance on: no output file may contain the embedding values (marker check) or an
    embedding key. The embedder returns vectors whose components are an unmistakable marker value."""
    from vision import track_video, tracker_setup
    marker = 0.0123456789

    class FakeDet:
        name, source = "fake", "SYNTHETIC"
        last_ms = 1.0

        def info(self):
            return {"name": "fake"}

        def detect(self, img):
            k = int(img[0, 0, 0])
            return [det(100 + 3 * k, 240, 160), det(400 - 3 * k, 250, 160)]

    class FakeReid:
        def embed(self, frame, boxes):
            v = np.full((len(boxes), 16), marker, np.float32)
            v[:, 0] = np.arange(len(boxes)) + 1
            return v / np.linalg.norm(v, axis=1, keepdims=True)

        def info(self):
            return {"name": "fake-reid"}

    monkeypatch.setattr(track_video, "make_detector", lambda *a, **k: FakeDet())
    monkeypatch.setattr(tracker_setup, "make_embedder", lambda cfg: tracker_setup.TimedEmbedder(FakeReid()) if cfg.reid else None)
    src = tmp_path / "frames"
    src.mkdir()
    for k in range(40):
        img = textured(seed=k % 3)
        img[0, 0] = k
        cv2.imwrite(str(src / f"{k:04d}.png"), img)
    req = track_video.TrackRequest(src, tmp_path / "sess", target_fps=10, tracker=variant_config("VABC"))
    rep = track_video.run_tracking(req)
    assert rep["status"] == "SUCCEEDED" and rep["reid"]["calls"] > 0 and rep["reid"]["embeddings_written"] == 0
    normed = marker / np.linalg.norm(np.r_[1.0, np.full(15, marker)])
    needles = [repr(float(np.float32(normed)))[:8], f"{normed:.6f}"[:8]]
    raw = np.float32(normed).tobytes()
    for p in (tmp_path / "sess").rglob("*"):
        if p.is_file():
            b = p.read_bytes()
            assert raw not in b, p
            if p.suffix == ".json":
                txt = b.decode()
                assert not any(n in txt for n in needles), p
                keys = set()
                walk = [json.loads(txt)]
                while walk:
                    o = walk.pop()
                    if isinstance(o, dict):
                        keys |= set(o)
                        walk += list(o.values())
                    elif isinstance(o, list):
                        walk += o
                assert not keys & {"emb", "embedding", "embeddings", "feature", "features", "appearance"}, p
    assert json.loads((tmp_path / "sess" / "tracking_report.json").read_text())["settings"]["tracker"]["reid"] is True


MODEL = ROOT / "data/models/reid/osnet_x0_25_market1501.onnx"


@pytest.mark.skipif(not MODEL.is_file(), reason="ReID ONNX not exported (scripts/export_reid_onnx.py)")
def test_real_reid_model_ignores_the_head_band():
    from vision.reid import HEAD_FRAC, OsnetReid
    m = OsnetReid()
    img = textured(seed=5)
    box = (200, 100, 280, 340)
    other = img.copy()
    head_rows = int(0.9 * HEAD_FRAC * (box[3] - box[1]))
    other[box[1]:box[1] + head_rows, box[0]:box[2]] = 255 - other[box[1]:box[1] + head_rows, box[0]:box[2]]
    e1, e2 = m.embed(img, [box]), m.embed(other, [box])
    assert np.allclose(e1, e2, atol=1e-5) and abs(float(np.linalg.norm(e1)) - 1) < 1e-4
    body = img.copy()
    body[box[1] + 60:box[3], box[0]:box[2]] = (30, 30, 220)  # repaint torso and legs
    assert float(1 - m.embed(body, [box])[0] @ e1[0]) > 0.05  # the body does change it


def test_cli_default_is_the_adopted_preset_and_reid_is_opt_in():
    import argparse
    from vision import tracker_setup
    ap = argparse.ArgumentParser()
    tracker_setup.add_args(ap)
    cfg = tracker_setup.config_from_args(ap.parse_args([]))
    assert cfg == variant_config("VAB") and not cfg.reid and cfg.gmc and cfg.assoc == "margin"
    assert tracker_setup.config_from_args(ap.parse_args(["--reid"])).reid is True
    assert tracker_setup.config_from_args(ap.parse_args(["--tracker", "V0"])) == TrackerConfig()
    assert TrackerConfig().assoc == "strict" and not TrackerConfig().gmc  # library default unchanged
