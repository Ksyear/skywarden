import json
from pathlib import Path
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
        with urllib.request.urlopen(f"{base}/api/runs/lv1/files/timeline.jsonl", timeout=10) as resp:
            assert resp.status == 200 and resp.read().count(b"\n") >= 3
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


# ---------- metric scale (two-point tape) ----------
def _fake_map_session(root, sid):
    sd = root / sid
    sd.mkdir(parents=True)
    poses = [{"order": k, "image_name": f"f{k:06d}.jpg", "T_world_camera": [[1, 0, 0, k], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]}
             for k in range(3)]
    (sd / "trajectory.json").write_text(json.dumps({"scale": "RELATIVE", "poses": poses}))
    (sd / "map_report.json").write_text(json.dumps({"status": "SUCCEEDED", "labels": {"scale": "RELATIVE"},
                                                    "reconstruction": {"chosen_model_dir": "sparse/0"}}))
    return sd


def test_scale_post_fit_then_two_checks_verifies(console):
    base, maps, _ = console
    sd = _fake_map_session(maps, "m1")
    code, info = call(f"{base}/api/sessions/m1")
    assert code == 200 and info["metric_scale"]["status"] == "NONE"
    assert call(f"{base}/api/sessions/m1/files/scale.json")[0] == 404  # not written yet
    fit = {"method": "TWO_POINT_TAPE", "p1": [0, 0, 0], "p2": [2, 0, 0], "true_m": 0.9, "role": "fit",
           "note": "door", "pick_tolerance_units": 0.01}
    code, s = call(f"{base}/api/sessions/m1/scale", fit)
    assert code == 200 and s["status"] == "MEASURED_UNVERIFIED" and abs(s["metres_per_unit"] - 0.45) < 1e-9
    code, s = call(f"{base}/api/sessions/m1/scale", {**fit, "p2": [0, 3, 0], "true_m": 1.37, "role": "verify", "note": "desk"})
    assert code == 200 and s["status"] == "MEASURED_UNVERIFIED" and s["checks_passed"] == 1
    code, s = call(f"{base}/api/sessions/m1/scale", {**fit, "p1": [1, 1, 1], "p2": [1, 1, 6], "true_m": 2.2, "role": "verify", "note": "wall"})
    assert code == 200 and s["status"] == "MEASURED_VERIFIED" and s["derived_scale_label"] == "METRIC_ANCHORED"
    code, f = call(f"{base}/api/sessions/m1/files/scale.json")
    assert code == 200 and f["schema"] == "skywarden/scale@1" and f["status"] == "MEASURED_VERIFIED"
    assert call(f"{base}/api/sessions/m1")[1]["metric_scale"]["status"] == "MEASURED_VERIFIED"
    rep = json.loads((sd / "map_report.json").read_text())
    assert rep["labels"]["scale"] == "RELATIVE"  # the map's own files are never rewritten
    assert json.loads((sd / "trajectory.json").read_text())["scale"] == "RELATIVE"


def test_scale_post_rejects_bad_input(console):
    base, maps, _ = console
    _fake_map_session(maps, "m2")
    good = {"p1": [0, 0, 0], "p2": [1, 0, 0], "true_m": 1.0, "role": "fit"}
    for bad in ({"role": "both"}, {"true_m": -1}, {"true_m": 0}, {"true_m": 1e9}, {"true_m": "1"}, {"true_m": True},
                {"p1": [0, 0]}, {"p1": [0, 0, "x"]}, {"p2": [0, 0, 0]}, {"p2": [1e12, 0, 0]},
                {"pick_tolerance_units": -0.1}, {"note": "x" * 81}, {"method": "TELLO_TELEMETRY"}, {"model_rank": 1}):
        assert call(f"{base}/api/sessions/m2/scale", {**good, **bad})[0] == 400, bad
    assert call(f"{base}/api/sessions/nope/scale", good)[0] == 404
    assert call(f"{base}/api/sessions/..%2Fm2/scale", good)[0] == 404
    (maps / "m3").mkdir()
    (maps / "m3" / "map_report.json").write_text('{"status": "FAILED"}')
    assert call(f"{base}/api/sessions/m3/scale", good)[0] == 409
    assert not (maps / "m2" / "scale.json").exists()
    import http.client
    host, port = base.split("//")[1].split(":")
    for length, payload in (("-1", b""), ("70000", b"{}"), ("abc", b"{}")):
        conn = http.client.HTTPConnection(host, int(port), timeout=5)
        conn.putrequest("POST", "/api/sessions/m2/scale")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", length)
        conn.endheaders()
        conn.send(payload)
        assert conn.getresponse().status == 400, length
        conn.close()


# ---------- a child that already wrote its final state ----------
def test_child_that_wrote_its_final_state_no_longer_blocks_or_takes_controls(tmp_path):
    """The child writes its last status.json and only then exits (closing files, interpreter teardown). In that
    window the UI already shows SUCCEEDED, so a new job must be accepted and a control or cancel refused."""
    import sys
    from desktop.server import Console
    con = Console(tmp_path / "maps", {}, runs_root=tmp_path / "runs")
    for kind in ("maps", "runs"):
        sd = con.kinds[kind].root / "done"
        sd.mkdir(parents=True)
        (sd / "process.log").write_text("")
        (sd / "status.json").write_text(json.dumps({"state": "SUCCEEDED", "updated_at": "2026-01-01T00:00:00+00:00"}))
        con._spawn(kind, "done", [sys.executable, "-c", "import time; time.sleep(0.6)"])
    t0 = time.monotonic()
    assert con.running_job("maps") is None and time.monotonic() - t0 >= 0.3  # waited for the exit, did not guess
    assert con.cancel_job("done", "maps") is False
    with pytest.raises(RuntimeError):
        con.run_control("done", {"op": "pause"})
    assert con.running_job("runs") is None


def test_child_still_running_counts_as_busy(tmp_path):
    import sys
    from desktop.server import Console
    con = Console(tmp_path / "maps", {})
    sd = con.kinds["maps"].root / "busy"
    sd.mkdir(parents=True)
    (sd / "status.json").write_text(json.dumps({"state": "RUNNING", "updated_at": "2026-01-01T00:00:00+00:00"}))
    con._spawn("maps", "busy", [sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        t0 = time.monotonic()
        assert con.running_job("maps") == "busy" and time.monotonic() - t0 < 1.0  # RUNNING: no grace wait
    finally:
        con.shutdown()


# ---------- request origin checks ----------
def _raw(base, method, path, headers, body=b""):
    import http.client
    host, port = base.split("//")[1].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    conn.putrequest(method, path, skip_host="Host" in headers)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.putheader("Content-Length", str(len(body)))
    conn.endheaders()
    conn.send(body)
    r = conn.getresponse()
    out = r.status, r.read()
    conn.close()
    return out


def test_post_needs_local_host_local_origin_and_json(console):
    base, maps, _ = console
    port = base.rsplit(":", 1)[1]
    body = json.dumps({"input_id": "/etc"}).encode()  # would be a 400 if it got through
    js = {"Content-Type": "application/json"}
    assert _raw(base, "POST", "/api/jobs", js, body)[0] == 400  # baseline: reaches the handler
    for name in ("127.0.0.1", "localhost"):
        assert _raw(base, "POST", "/api/jobs", {**js, "Host": f"{name}:{port}", "Origin": f"http://{name}:{port}"}, body)[0] == 400
    assert _raw(base, "POST", "/api/jobs", {**js, "Origin": "https://evil.example"}, body)[0] == 403
    assert _raw(base, "POST", "/api/jobs", {**js, "Origin": f"http://127.0.0.1:{int(port) + 1}"}, body)[0] == 403
    assert _raw(base, "POST", "/api/jobs", {**js, "Origin": "null"}, body)[0] == 403
    assert _raw(base, "POST", "/api/jobs", {**js, "Host": f"evil.example:{port}"}, body)[0] == 403  # DNS rebinding
    assert _raw(base, "POST", "/api/jobs", {**js, "Host": "127.0.0.1:1"}, body)[0] == 403
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"):
        assert _raw(base, "POST", "/api/jobs", {"Content-Type": ctype}, body)[0] == 415, ctype
    assert _raw(base, "POST", "/api/jobs", {}, body)[0] == 415
    assert _raw(base, "POST", "/api/jobs", {"Content-Type": "application/json; charset=utf-8"}, body)[0] == 400
    # reads are checked for the Host only (a rebinding page could otherwise read the session list)
    assert _raw(base, "GET", "/api/health", {"Host": f"evil.example:{port}"})[0] == 403
    assert _raw(base, "GET", "/api/health", {"Origin": "https://evil.example"})[0] == 200
    assert not maps.exists() or not any(maps.iterdir())  # nothing was started


# ---------- PLY transfer ----------
def _ply_session(maps, n=1000):
    import numpy as np
    from mapping.export import PLY_DTYPE, write_ply
    sd = maps / "p1"
    (sd / "dense").mkdir(parents=True)
    pts = np.zeros(n, dtype=PLY_DTYPE)
    pts["x"] = np.arange(n, dtype=np.float32)
    pts["first_seen"] = np.arange(n) % 7
    write_ply(sd / "sparse.ply", pts)
    write_ply(sd / "dense" / "dense_verified.ply", pts)
    (sd / "map_report.json").write_text('{"status": "SUCCEEDED"}')
    return sd, pts


def _get_bytes(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_ply_streams_whole_file_and_ranges(console):
    base, maps, _ = console
    sd, _ = _ply_session(maps)
    raw = (sd / "sparse.ply").read_bytes()
    code, h, body = _get_bytes(f"{base}/api/sessions/p1/files/sparse.ply")
    assert code == 200 and body == raw and h["Accept-Ranges"] == "bytes" and int(h["Content-Length"]) == len(raw)
    code, h, body = _get_bytes(f"{base}/api/sessions/p1/files/sparse.ply", {"Range": "bytes=10-19"})
    assert code == 206 and body == raw[10:20] and h["Content-Range"] == f"bytes 10-19/{len(raw)}"
    code, _, body = _get_bytes(f"{base}/api/sessions/p1/files/sparse.ply", {"Range": "bytes=-5"})
    assert code == 206 and body == raw[-5:]
    code, _, body = _get_bytes(f"{base}/api/sessions/p1/files/sparse.ply", {"Range": f"bytes={len(raw) - 3}-"})
    assert code == 206 and body == raw[-3:]
    assert _get_bytes(f"{base}/api/sessions/p1/files/sparse.ply", {"Range": f"bytes={len(raw)}-"})[0] == 416
    assert _get_bytes(f"{base}/api/sessions/p1/files/sparse.ply", {"Range": "bytes=0-1,4-5"})[0] == 416
    assert _get_bytes(f"{base}/api/sessions/p1/files/sparse_filtered.ply")[0] == 404


def test_ply_max_points_is_a_deterministic_valid_subsample(console):
    import numpy as np
    from mapping.export import parse_ply_header
    base, maps, con = console
    _, pts = _ply_session(maps, n=1000)
    url = f"{base}/api/sessions/p1/files/dense/dense_verified.ply?max_points=300"
    code, h, body = _get_bytes(url)
    assert code == 200 and h["X-Points-Total"] == "1000" and h["X-Points-Served"] == "300"
    count, dtype, at = parse_ply_header(body)
    rows = np.frombuffer(body[at:], dtype=dtype)
    assert count == 300 and len(rows) == 300 and len(body) == at + 300 * dtype.itemsize
    assert b"comment decimated: 300 of 1000 points" in body[:at]
    idx = (np.arange(300) * 1000) // 300
    assert np.array_equal(rows["x"], pts["x"][idx]) and np.array_equal(rows["first_seen"], pts["first_seen"][idx])
    assert _get_bytes(url)[2] == body  # same request, same bytes (served from the cache the second time)
    code, h, whole = _get_bytes(f"{base}/api/sessions/p1/files/dense/dense_verified.ply?max_points=5000")
    assert code == 200 and h["X-Points-Served"] == "all" and whole == (maps / "p1/dense/dense_verified.ply").read_bytes()
    for bad in ("0", "-1", "abc", "1e6", str(10 ** 9)):
        assert _get_bytes(f"{base}/api/sessions/p1/files/sparse.ply?max_points={bad}")[0] == 400, bad
    (maps / "p1" / "sparse_filtered.ply").write_bytes(b"ply\nformat ascii 1.0\nend_header\n")
    assert _get_bytes(f"{base}/api/sessions/p1/files/sparse_filtered.ply?max_points=10")[0] == 422
    # a rewritten file is never answered from the cache of the old one
    import os as _os
    from mapping.export import write_ply
    newer = pts[:500].copy()
    newer["x"] += 1e4
    write_ply(maps / "p1/dense/dense_verified.ply", newer)
    st = (maps / "p1/dense/dense_verified.ply").stat()
    _os.utime(maps / "p1/dense/dense_verified.ply", ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
    code, h, body = _get_bytes(url)
    assert h["X-Points-Total"] == "500" and np.frombuffer(body[parse_ply_header(body)[2]:], dtype=dtype)["x"].min() >= 1e4
