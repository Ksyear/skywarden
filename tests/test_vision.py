import json
import socket
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest

from control.virtual_replay import replay_control
from vision.person_detector import MODELS, MODELS_DIR, ModelUnavailable, YoloxDetector, decode, letterbox
from vision.track_video import TrackRequest, run_tracking
from mapping.labels import DataOrigin

ROOT = Path(__file__).resolve().parents[1]
VTEST = ROOT / "data/public/opencv_vtest/vtest.avi"
HAVE_NANO = (MODELS_DIR / MODELS["yolox_nano"].file).is_file()
needs_model = pytest.mark.skipif(not HAVE_NANO, reason="weights missing: run scripts/fetch_models.sh")
needs_clip = pytest.mark.skipif(not VTEST.is_file(), reason="data/public/opencv_vtest/vtest.avi missing")


# ---------- preprocessing / decoding (fixed tensors, no weights needed) ----------
def test_letterbox_keeps_aspect_and_pads_bottom_right_with_114():
    img = np.full((300, 600, 3), 7, np.uint8)
    blob, r = letterbox(img, 416)
    assert blob.shape == (1, 3, 416, 416) and blob.dtype == np.float32
    assert r == pytest.approx(416 / 600)
    assert blob[0, :, 0, 0].tolist() == [7, 7, 7]          # image at top-left
    assert blob[0, :, 415, 0].tolist() == [114, 114, 114]  # padding below
    assert blob[0, 0, :, :].max() <= 255                   # no normalisation


def _raw(size=416, n_cls=80):
    n = sum((size // s) ** 2 for s in (8, 16, 32))
    raw = np.zeros((n, 5 + n_cls), np.float32)
    raw[:, 2:4] = -20  # tiny boxes everywhere
    return raw


def _set(raw, cell, dx, dy, w_px, h_px, stride, obj, cls_scores):
    raw[cell, 0:2] = [dx, dy]
    raw[cell, 2:4] = [np.log(w_px / stride), np.log(h_px / stride)]
    raw[cell, 4] = obj
    for c, s in cls_scores.items():
        raw[cell, 5 + c] = s


def test_decode_grid_offsets_stride_and_scale_back_to_image():
    raw = _raw()
    # stride-8 grid is 52x52, cell (row 10, col 20) -> index 10*52+20; centre = (20+0.5)*8, (10+0.5)*8
    _set(raw, 10 * 52 + 20, 0.5, 0.5, 40, 80, 8, 0.9, {0: 0.9})
    out = decode(raw, 416, 0.5, (2000, 2000), 0.3, 0.45)
    assert len(out) == 1
    (x1, y1, x2, y2), score = out[0]
    assert score == pytest.approx(0.81)
    assert ((x1 + x2) / 2, (y1 + y2) / 2) == pytest.approx((164 / 0.5, 84 / 0.5), abs=1e-3)
    assert (x2 - x1, y2 - y1) == pytest.approx((80, 160), rel=1e-4)


def test_decode_keeps_only_person_class_and_applies_threshold_and_nms():
    raw = _raw()
    _set(raw, 100, 0.5, 0.5, 40, 80, 8, 0.9, {0: 0.9})
    _set(raw, 101, 0.4, 0.5, 40, 80, 8, 0.9, {0: 0.8})   # overlapping duplicate -> suppressed
    _set(raw, 900, 0.5, 0.5, 40, 80, 8, 0.9, {2: 0.95})  # a car -> ignored
    _set(raw, 1500, 0.5, 0.5, 40, 80, 8, 0.5, {0: 0.5})  # 0.25 < threshold
    out = decode(raw, 416, 1.0, (416, 416), 0.3, 0.45)
    assert len(out) == 1 and out[0][1] == pytest.approx(0.81)


def test_decode_clips_to_image_and_rejects_wrong_cell_count():
    raw = _raw()
    _set(raw, 0, 0.0, 0.0, 100, 100, 8, 1.0, {0: 1.0})  # centred on (0,0): half outside
    (x1, y1, x2, y2), _ = decode(raw, 416, 1.0, (416, 416), 0.3, 0.45)[0]
    assert x1 == 0 and y1 == 0 and x2 == pytest.approx(50)
    with pytest.raises(ValueError):
        decode(raw[:-1], 416, 1.0, (416, 416), 0.3, 0.45)


def test_missing_or_tampered_weights_are_refused(tmp_path):
    with pytest.raises(ModelUnavailable, match="missing"):
        YoloxDetector("yolox_nano", models_dir=tmp_path)
    (tmp_path / "yolox_nano.onnx").write_bytes(b"not the model")
    with pytest.raises(ModelUnavailable, match="sha256"):
        YoloxDetector("yolox_nano", models_dir=tmp_path)
    with pytest.raises(ModelUnavailable, match="unknown"):
        YoloxDetector("yolov99")


# ---------- real model on a real public frame ----------
@needs_model
@needs_clip
def test_real_model_finds_pedestrians_in_public_clip():
    cap = cv2.VideoCapture(str(VTEST))
    cap.set(cv2.CAP_PROP_POS_FRAMES, 90)
    ok, img = cap.read()
    assert ok
    det = YoloxDetector("yolox_nano")
    found = det.detect(img)
    h, w = img.shape[:2]
    assert len(found) >= 5  # 8 pedestrians are visible in this frame (checked by drawing the boxes)
    assert all(d.source == "REAL_MODEL" and d.valid() for d in found)
    assert all(0 <= d.bbox_xyxy[0] < d.bbox_xyxy[2] <= w and 0 <= d.bbox_xyxy[1] < d.bbox_xyxy[3] <= h for d in found)
    assert all(d.size[1] > d.size[0] for d in found)  # standing people are taller than wide
    assert 0 < det.last_ms < 500


@needs_model
@needs_clip
def test_tracking_run_writes_report_frames_and_undefined_accuracy(tmp_path):
    rep = run_tracking(TrackRequest(VTEST, tmp_path / "s", DataOrigin.PUBLIC_DATASET, max_frames=30))
    assert rep["status"] == "SUCCEEDED" and rep["frames_processed"] == 30
    assert rep["labels"]["detector_source"] == "REAL_MODEL"
    assert rep["detector"]["sha256"] == MODELS["yolox_nano"].sha256
    assert rep["accuracy"]["MOTA"] is None and "no ground-truth" in rep["accuracy"]["reason"]
    assert rep["timing"]["detect_ms_p95"] >= rep["timing"]["detect_ms_p50"] > 0
    rec = json.loads((tmp_path / "s/tracks.json").read_text())
    assert len(rec["frames"]) == 30 and rec["image_wh"] == [768, 576]
    assert [f["t"] for f in rec["frames"]] == sorted({f["t"] for f in rec["frames"]})  # strictly increasing clock
    assert all((tmp_path / "s" / f["image"]).is_file() for f in rec["frames"])
    assert json.loads((tmp_path / "s/status.json").read_text())["state"] == "SUCCEEDED"
    assert rep["tracks"]["tracks_confirmed"] >= 3


@needs_model
@needs_clip
def test_tracking_run_cancel_and_bad_input(tmp_path):
    ev = threading.Event()
    ev.set()
    assert run_tracking(TrackRequest(VTEST, tmp_path / "c"), ev)["status"] == "CANCELLED"
    bad = run_tracking(TrackRequest(tmp_path / "nope.mp4", tmp_path / "b"))
    assert bad["status"] == "FAILED" and "does not exist" in bad["failure"]["message"]
    # rerun into the same folder drops old frames
    run_tracking(TrackRequest(VTEST, tmp_path / "r", max_frames=12))
    run_tracking(TrackRequest(VTEST, tmp_path / "r", max_frames=4))
    assert len(list((tmp_path / "r/frames").glob("*.jpg"))) == 4


def test_missing_weights_fail_the_job_with_a_fix_hint(tmp_path, monkeypatch):
    import vision.person_detector as pd
    monkeypatch.setattr(pd, "MODELS_DIR", tmp_path / "empty")
    rep = run_tracking(TrackRequest(tmp_path, tmp_path / "s"))
    assert rep["status"] == "FAILED" and "fetch_models.sh" in rep["failure"]["message"]
    assert rep["failure"]["stage"] == "LOAD_MODEL"


# ---------- virtual PID replay over a recorded timeline ----------
def _frames(spec):
    """spec: list of {id: (cx, state)} per frame at 10 Hz."""
    return [{"k": k, "t": round(k * 0.1, 3), "tracks": [
        {"id": i, "state": st, "bbox": [cx - 20, 100, cx + 20, 300]} for i, (cx, st) in f.items()]} for k, f in enumerate(spec)]


def test_replay_turns_toward_target_and_stops_when_not_locked(monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no sockets")))
    spec = [{1: (500, "LOCKED"), 2: (100, "LOCKED")}] * 10 + [{1: (500, "PREDICTING"), 2: (100, "LOCKED")}] * 3 \
        + [{2: (100, "LOCKED")}] * 3 + [{2: (100, "LOCKED"), 3: (500, "LOCKED")}] * 5
    r = replay_control(_frames(spec), 640, target_id=1, select_k=2)
    F = r["frames"]
    assert all(f["target_state"] == "SEARCHING" and f["yaw"] == 0 for f in F[:2])
    assert all(f["target_state"] == "LOCKED" for f in F[2:10]) and F[9]["yaw"] > 0  # target right of centre -> yaw right
    assert all(f["yaw"] == 0 and f["target_state"] == "PREDICTING" for f in F[10:13])
    assert all(f["target_state"] == "LOST" and f["yaw"] == 0 for f in F[13:])  # never re-acquires id 3
    assert r["summary"]["nonzero_yaw_while_not_locked"] == 0 and r["summary"]["lost_from_k"] == 13
    assert r["labels"]["transmitted"] is False and r["labels"]["command_source"] == "MOCK"
    assert abs(max(f["yaw"] for f in F)) <= r["limits"]["max_yaw_rc"]


def test_replay_rejects_target_not_visible_at_select_frame():
    with pytest.raises(ValueError, match="not present"):
        replay_control(_frames([{1: (300, "LOCKED")}]), 640, target_id=7, select_k=0)
    with pytest.raises(ValueError):
        replay_control(_frames([{1: (300, "LOCKED")}]), 640, target_id=1, select_k=5)
