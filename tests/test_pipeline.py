import json
import threading

import pytest

from mapping.evaluate import evaluate_session
from mapping.frame_selection import SelectionConfig
from mapping.labels import DataOrigin
from mapping.replay import ReplayError, load_session
from mapping.sparse_pipeline import BuildRequest, build_sparse_map


def _req(src, out, **kw):
    cam = json.loads((src / "camera.json").read_text())
    return BuildRequest(src, out, kw.pop("origin", DataOrigin.SYNTHETIC), cam, SelectionConfig(target_fps=0), **kw)


@pytest.mark.slow
def test_synthetic_end_to_end_recovers_known_path(synthetic_room, tmp_path):
    rep = build_sparse_map(_req(synthetic_room, tmp_path / "s1"))
    assert rep["status"] == "SUCCEEDED", rep.get("failure")
    assert rep["labels"]["map_source"] == "SIMULATED" and rep["labels"]["scale"] == "RELATIVE"
    assert rep["reconstruction"]["registered_images"] >= 10 and rep["reconstruction"]["points3D"] > 500
    ate = evaluate_session(tmp_path / "s1", synthetic_room / "groundtruth.txt")
    assert ate["ate_rmse_m"] < 0.01  # renderer path is exact; a real-data number is not implied
    stored = load_session(tmp_path / "s1")
    assert stored.summary()["points3D"] == rep["reconstruction"]["points3D"]
    assert json.loads((tmp_path / "s1" / "status.json").read_text())["state"] == "SUCCEEDED"


def test_synthetic_input_cannot_be_labelled_real(synthetic_room, tmp_path):
    rep = build_sparse_map(_req(synthetic_room, tmp_path / "s2", origin=DataOrigin.PUBLIC_DATASET))
    assert rep["status"] == "FAILED" and "synthetic" in rep["failure"]["message"]
    with pytest.raises(ReplayError, match="FAILED"):
        load_session(tmp_path / "s2")


def test_missing_input_fails_with_report(tmp_path):
    rep = build_sparse_map(BuildRequest(tmp_path / "nope", tmp_path / "s3"))
    assert rep["status"] == "FAILED" and rep["failure"]["stage"] == "SELECT_FRAMES"
    assert (tmp_path / "s3" / "map_report.json").exists()


def test_cancel_marks_session_cancelled(synthetic_room, tmp_path):
    cancel = threading.Event()
    cancel.set()
    rep = build_sparse_map(_req(synthetic_room, tmp_path / "s4"), cancel_event=cancel)
    assert rep["status"] == "CANCELLED" and rep["failure"]["stage"] == "SELECT_FRAMES"
    assert json.loads((tmp_path / "s4" / "status.json").read_text())["state"] == "CANCELLED"
    assert not (tmp_path / "s4" / "sparse.ply").exists()


def test_scale_camera_per_model_and_resolution():
    from mapping.frame_source import InputError
    from mapping.sparse_pipeline import scale_camera
    p, _ = scale_camera({"model": "SIMPLE_RADIAL", "params": [500, 320, 240, 0.1]}, (320, 240), 0.5)
    assert p == [250, 160, 120, 0.1]  # distortion k untouched
    p, note = scale_camera({"model": "OPENCV", "params": [500, 501, 320, 240, 0.1, 0.2, 0, 0], "width": 640, "height": 480}, (1280, 960), 1.0)
    assert p[:4] == [1000, 1002, 640, 480] and p[4:] == [0.1, 0.2, 0, 0] and "x2" in note
    with pytest.raises(InputError, match="aspect"):
        scale_camera({"model": "PINHOLE", "params": [1, 1, 1, 1], "width": 640, "height": 480}, (1280, 720), 1.0)
    with pytest.raises(InputError, match="Unsupported camera model"):
        scale_camera({"model": "FOV", "params": [1]}, (10, 10), 1.0)


@pytest.mark.slow
def test_rerun_into_same_session_does_not_mix_runs(synthetic_room, tmp_path):
    first = build_sparse_map(_req(synthetic_room, tmp_path / "same"))
    req = _req(synthetic_room, tmp_path / "same")
    req.selection = SelectionConfig(target_fps=0, max_frames=6)
    second = build_sparse_map(req)
    assert first["status"] == second["status"] == "SUCCEEDED"
    assert len(list((tmp_path / "same" / "frames").glob("*.jpg"))) == second["frames"]["selected"] == 6
    assert second["reconstruction"]["registered_images"] <= 6


@pytest.mark.slow
def test_report_separates_job_status_from_map_quality(synthetic_room, tmp_path):
    rep = build_sparse_map(_req(synthetic_room, tmp_path / "q1"))
    assert rep["schema"].endswith("@2") and rep["status"] == "SUCCEEDED"
    q = rep["quality"]
    assert q["verdict"] in {"GOOD", "PARTIAL", "POOR"} and set(q["checks"]) >= {"registered_ratio", "time_coverage", "models_ge_10pct", "trajectory_jumps"}
    assert rep["labels"]["completeness"] == ("COMPLETE" if q["verdict"] == "GOOD" else "PARTIAL_RECONSTRUCTION")
    n = rep["reconstruction"]["num_models"]
    assert len(rep["reconstruction"]["models"]) == n
    for i in range(n):
        assert (tmp_path / "q1" / "models" / str(i) / "sparse.ply").is_file()
    cov = json.loads((tmp_path / "q1" / "coverage.json").read_text())
    assert len(cov["frames"]) == rep["frames"]["selected"]
    assert load_session(tmp_path / "q1").summary()["quality"] == q["verdict"]
