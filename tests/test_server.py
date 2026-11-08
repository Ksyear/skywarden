import json
from pathlib import Path
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from desktop.server import make_server
from mapping.labels import DataOrigin


@pytest.fixture
def console(tmp_path, synthetic_room):
    maps = tmp_path / "maps"
    httpd = make_server("127.0.0.1", 0, maps, {synthetic_room.parent: DataOrigin.PUBLIC_DATASET})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, maps, httpd.RequestHandlerClass.console
    httpd.RequestHandlerClass.console.shutdown()
    httpd.shutdown()


def call(url, body=None):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def wait_state(base, sid, states, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        code, info = call(f"{base}/api/sessions/{sid}")
        if code == 200 and info["state"] in states:
            return info
        time.sleep(0.2)
    raise AssertionError(f"{sid} never reached {states}; last={info}")


def test_health_reports_unarmed(console):
    base, *_ = console
    code, h = call(f"{base}/api/health")
    assert code == 200 and h["hardware_armed"] is False and h["dry_run"] is True


def test_refuses_non_local_bind(tmp_path):
    with pytest.raises(SystemExit):
        make_server("0.0.0.0", 0, tmp_path)


def test_synthetic_input_is_labelled_synthetic_even_under_a_public_root(console):
    base, *_ = console
    code, inputs = call(f"{base}/api/inputs")
    assert code == 200 and [i["origin"] for i in inputs] == ["SYNTHETIC"]


def test_rejects_unknown_input_and_bad_settings(console):
    base, *_ = console
    assert call(f"{base}/api/jobs", {"input_id": "/etc"})[0] == 400
    _, inputs = call(f"{base}/api/inputs")
    assert call(f"{base}/api/jobs", {"input_id": inputs[0]["id"], "fps": 99})[0] == 400


def test_only_whitelisted_session_files_are_served(console):
    base, maps, _ = console
    (maps / "s1").mkdir(parents=True)
    (maps / "s1" / "map_report.json").write_text('{"status": "FAILED"}')
    (maps / "s1" / "database.db").write_text("x")
    assert call(f"{base}/api/sessions/s1/files/map_report.json")[0] == 200
    assert call(f"{base}/api/sessions/s1/files/database.db")[0] == 404
    assert call(f"{base}/api/sessions/s1/files/frames/..%2Fdatabase.db")[0] == 404


def test_build_then_second_build_conflicts_then_cancel(console):
    base, maps, con = console
    _, inputs = call(f"{base}/api/inputs")
    code, r = call(f"{base}/api/jobs", {"input_id": inputs[0]["id"], "fps": 10, "session": "run1"})
    assert code == 202
    info = wait_state(base, "run1", {"SUCCEEDED", "FAILED"}, timeout=120)
    assert info["state"] == "SUCCEEDED", info
    assert info["report"]["labels"]["map_source"] == "SIMULATED"

    code, _ = call(f"{base}/api/jobs", {"input_id": inputs[0]["id"], "fps": 10, "session": "run2"})
    assert code == 202
    assert call(f"{base}/api/jobs", {"input_id": inputs[0]["id"], "session": "run3"})[0] == 409
    # A 12-frame synthetic build can finish before the first poll sees RUNNING; then there is nothing to cancel.
    # (Cancelling a long build is covered by the UI smoke test on the TUM input.)
    seen = wait_state(base, "run2", {"RUNNING", "SUCCEEDED", "FAILED"})
    if seen["state"] == "RUNNING":
        assert call(f"{base}/api/jobs/run2/cancel", {})[0] in (202, 409)  # 409 if it finished in between
    info = wait_state(base, "run2", {"CANCELLED", "SUCCEEDED"}, timeout=60)
    assert info["state"] in {"CANCELLED", "SUCCEEDED"}  # a tiny synthetic job may finish before SIGTERM lands
    assert call(f"{base}/api/jobs/run2/cancel", {})[0] == 409  # nothing left to cancel


def test_dead_build_process_is_reported_interrupted(console):
    base, maps, _ = console
    sd = maps / "ghost"
    sd.mkdir(parents=True)
    dead_pid = 2 ** 22 + 12345  # far above macOS pid range
    (sd / "status.json").write_text(json.dumps({"state": "RUNNING", "pid": dead_pid, "stage_index": 3,
                                                "updated_at": "2026-01-01T00:00:00+00:00"}))
    code, info = call(f"{base}/api/sessions/ghost")
    assert code == 200 and info["state"] == "INTERRUPTED"


def test_dot_session_ids_cannot_escape_maps_root(console):
    base, maps, _ = console
    maps.mkdir(parents=True, exist_ok=True)
    (maps.parent / "map_report.json").write_text('{"status": "SUCCEEDED"}')
    import http.client
    host, port = base.split("//")[1].split(":")
    for path in ("/api/sessions/..", "/api/sessions/../files/map_report.json", "/api/sessions/./files/map_report.json"):
        conn = http.client.HTTPConnection(host, int(port), timeout=5)
        conn.request("GET", path)  # raw, un-normalised path
        assert conn.getresponse().status == 404, path
        conn.close()


# ---------- tracking runs ----------
def _fake_track_session(root, sid, state="SUCCEEDED"):
    sd = root / sid
    (sd / "frames").mkdir(parents=True)
    frames = [{"k": k, "t": k / 10, "image": f"frames/f{k:06d}.jpg", "detections": [],
               "tracks": [{"id": 4, "state": "LOCKED", "bbox": [400, 100, 440, 300], "sigma_px": 5, "hits": 9}]}
              for k in range(5)]
    (sd / "tracks.json").write_text(json.dumps({"image_wh": [640, 480], "frames": frames}))
    (sd / "tracking_report.json").write_text(json.dumps({"status": state, "labels": {"detector_source": "REAL_MODEL"}}))
    (sd / "status.json").write_text(json.dumps({"state": state, "updated_at": "2026-01-01T00:00:00+00:00"}))
    (sd / "process.log").write_text("secret")


def test_track_files_whitelist_and_control_replay(console):
    base, maps, con = console
    troot = con.kinds["tracks"].root
    _fake_track_session(troot, "t1")
    _fake_track_session(troot, "t2", state="FAILED")
    code, lst = call(f"{base}/api/tracks")
    assert code == 200 and {s["id"] for s in lst} == {"t1", "t2"}
    assert call(f"{base}/api/tracks/t1/files/tracks.json")[0] == 200
    assert call(f"{base}/api/tracks/t1/files/process.log")[0] == 404
    assert call(f"{base}/api/tracks/nope")[0] == 404
    code, r = call(f"{base}/api/tracks/t1/control", {"target_id": 4, "select_k": 0})
    assert code == 200 and r["labels"]["transmitted"] is False and r["frames"][-1]["yaw"] > 0
    assert call(f"{base}/api/tracks/t1/control", {"target_id": 9, "select_k": 0})[0] == 400
    assert call(f"{base}/api/tracks/t1/control", {"select_k": 0})[0] == 400
    assert call(f"{base}/api/tracks/t2/control", {"target_id": 4})[0] == 409
    assert call(f"{base}/api/tracks/..%2F/control", {"target_id": 4})[0] == 404


def test_track_job_validation_and_one_job_at_a_time(console):
    base, maps, con = console
    _, inputs = call(f"{base}/api/inputs")
    iid = inputs[0]["id"]
    assert call(f"{base}/api/track-jobs", {"input_id": iid, "model": "yolov99"})[0] == 400
    assert call(f"{base}/api/track-jobs", {"input_id": iid, "fps": 0})[0] == 400
    assert call(f"{base}/api/track-jobs", {"input_id": "/etc"})[0] == 400
    code, r = call(f"{base}/api/jobs", {"input_id": iid, "fps": 10, "session": "busy"})
    assert code == 202
    assert call(f"{base}/api/track-jobs", {"input_id": iid, "session": "tx"})[0] == 409  # map build running
    wait_state(base, "busy", {"SUCCEEDED", "FAILED"}, timeout=120)


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / "data/models/yolox/yolox_nano.onnx").is_file(),
                    reason="weights missing: run scripts/fetch_models.sh")
def test_track_job_runs_real_model_end_to_end(console):
    base, maps, con = console
    _, inputs = call(f"{base}/api/inputs")
    code, r = call(f"{base}/api/track-jobs", {"input_id": inputs[0]["id"], "session": "tr1", "max_frames": 10})
    assert code == 202
    t0 = time.time()
    while time.time() - t0 < 60:
        code, info = call(f"{base}/api/tracks/tr1")
        if code == 200 and info["state"] in {"SUCCEEDED", "FAILED"}:
            break
        time.sleep(0.2)
    assert info["state"] == "SUCCEEDED", info
    rep = info["report"]
    assert rep["labels"]["detector_source"] == "REAL_MODEL" and rep["labels"]["data_origin"] == "SYNTHETIC"
    assert rep["detections"]["total"] == 0  # the synthetic room has no people: no false positives expected
    code, h = call(f"{base}/api/health")
    assert h["running_track_job"] is None


# ---------- live runs ----------
def _video(path, n=40, fps=10):
    import cv2
    import numpy as np
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (320, 240))
    rng = np.random.default_rng(0)
    base = cv2.GaussianBlur(cv2.resize(rng.integers(0, 255, (60, 80, 3), dtype=np.uint8), (640, 480)), (21, 21), 0)
    for k in range(n):
        vw.write(base[100:340, 10 + 3 * k:330 + 3 * k].copy())
    vw.release()


def test_live_run_lifecycle_control_and_integrity(tmp_path):
    vids = tmp_path / "in"
    vids.mkdir()
    _video(vids / "pan.mp4")
    httpd = make_server("127.0.0.1", 0, tmp_path / "maps", {vids: DataOrigin.PUBLIC_DATASET}, runs_root=tmp_path / "runs")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    con = httpd.RequestHandlerClass.console
    try:
        _, inputs = call(f"{base}/api/inputs")
        iid = next(i["id"] for i in inputs if i["kind"] == "video")
        assert call(f"{base}/api/run-jobs", {"input_id": iid, "rate": "warp"})[0] == 400
        code, r = call(f"{base}/api/run-jobs", {"input_id": iid, "rate": "realtime", "session": "lv1"})
        assert code == 202
        assert call(f"{base}/api/jobs", {"input_id": iid, "session": "m1"})[0] == 409  # one job at a time
        assert call(f"{base}/api/runs/lv1/control", {"op": "dance"})[0] == 400
        t0 = time.time()
        while time.time() - t0 < 60:
            code, info = call(f"{base}/api/runs/lv1")
            if code == 200 and info["state"] not in ("RUNNING", "STARTING"):
                break
            time.sleep(0.2)
        assert info["state"] == "SUCCEEDED", info
        assert info["status"]["verify"]["state"] == "SUCCEEDED"
        assert call(f"{base}/api/runs/lv1/control", {"op": "pause"})[0] == 409  # not running any more
        code, latest = call(f"{base}/api/runs/lv1/latest")
        assert code == 200 and latest["row"]["type"] == "frame" and latest["row"]["map_position"] == "NOT_AVAILABLE"
        with urllib.request.urlopen(f"{base}/api/runs/lv1/files/timeline.jsonl", timeout=10) as r:
            assert r.status == 200 and r.read().count(b"\n") >= 3
        assert call(f"{base}/api/runs/lv1/files/process.log")[0] == 404
        # tamper with the saved timeline: the run must no longer count as finished
        tl = tmp_path / "runs" / "lv1" / "timeline.jsonl"
        tl.write_text(tl.read_text() + '{"row": 99999}\n')
        (tmp_path / "runs" / "lv1" / "run_report.json").touch()
        con._verify_cache.clear()
        code, info = call(f"{base}/api/runs/lv1")
        assert info["state"] == "INCOMPLETE" and info["status"]["verify"]["problems"]
    finally:
        con.shutdown()
        httpd.shutdown()
