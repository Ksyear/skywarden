"""Live run (docs/PLAN_LIVE_RUN_TIMELINE.md M1-M2): buffer accounting, clocks, control, safety, completeness.

MOCK only: no drone adapter is imported, HARDWARE_ARMED=False, nothing is sent anywhere."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from control.virtual_replay import replay_control
from tracking.tracker import Detection
from vision import live_run
from vision.live_run import RunRequest, read_timeline, run_live, verify_run
from vision.live_source import FileSource, LatestFrameBuffer, NoDevice, SourceFrame, open_live

ROOT = Path(__file__).resolve().parents[1]
VTEST = ROOT / "data/public/opencv_vtest/vtest.avi"
MODEL = ROOT / "data/models/yolox/yolox_nano.onnx"
need_vtest = pytest.mark.skipif(not VTEST.is_file(), reason="vtest.avi not downloaded")


def write_control(sd: Path, seq: int, op: str, **args) -> float:
    tmp = sd / ".control.tmp"
    tmp.write_text(json.dumps({"seq": seq, "op": op, "args": args}))
    os.replace(tmp, sd / "control.json")
    return time.monotonic()


def frame_rows(rows):
    return [r for r in rows if r["type"] == "frame"]


# ------------------------------------------------------------------------------------------------ T1 buffer
def test_buffer_drop_accounting_exact():
    buf = LatestFrameBuffer()
    stop = threading.Event()
    put_seq = []

    def producer():
        seq = 0
        nxt = time.monotonic()
        while not stop.is_set():
            buf.put(SourceFrame(seq, seq, seq / 100, time.monotonic(), 0, None))
            put_seq.append(seq)
            seq += 1
            nxt += 0.01
            time.sleep(max(0.0, nxt - time.monotonic()))

    th = threading.Thread(target=producer)
    th.start()
    taken = []
    t_end = time.monotonic() + 1.5
    while time.monotonic() < t_end:
        f = buf.take(timeout=0.1)
        if f is not None:
            assert f.seq == put_seq[-1] or f.seq == put_seq[-2]  # newest (a put may race in right after the take)
            taken.append(f.seq)
        time.sleep(0.05)
    stop.set()
    th.join()
    buf.finalize()
    c = buf.counts()
    assert c["produced"] == len(put_seq) >= 100
    assert c["consumed"] == len(taken)
    assert c["dropped"] == c["produced"] - c["consumed"]
    assert taken == sorted(taken) and len(set(taken)) == len(taken)  # never older than one already returned
    assert c["dropped"] > c["consumed"]  # 100 Hz in, ~20 Hz out


def test_buffer_put_returns_overwrite_and_take_latest():
    buf = LatestFrameBuffer()
    assert buf.put(SourceFrame(0, 0, 0, 0, 0, None)) is False
    assert buf.put(SourceFrame(1, 1, 0, 0, 0, None)) is True
    assert buf.take().seq == 1 and buf.take() is None
    assert buf.counts() == {"produced": 2, "consumed": 1, "dropped": 1, "waiting": 0}


# ------------------------------------------------------------------------------------------------ scripted source
class ScriptedSource:
    """LiveSource with a realtime media clock (monotonic), scripted stalls and actions at media times."""
    kind = "scripted"
    rate = "realtime"

    def __init__(self, fps=20.0, size=(640, 360), stalls=(), actions=None, duration=None):
        self.fps, self.size, self.stalls = fps, list(size), stalls
        self.actions = sorted((actions or {}).items())
        self.epoch, self.eof, self.paused = 0, False, False
        self._i, self._seq = 0, 0
        self._started = False
        self._m0 = self._w0 = 0.0
        self.duration = duration
        rng = np.random.default_rng(1)
        self.tex = (rng.random((size[1] + 40, size[0] + 200)) * 255).astype(np.uint8)

    def meta(self):
        return {"kind": self.kind, "fps": self.fps, "size": self.size, "media_clock": "scripted realtime"}

    def start_clock(self, media_t=None):
        self._m0, self._w0, self._started = 0.0, time.monotonic(), True

    def media_now(self):
        if not self._started:
            return None
        return self._m0 if self.paused else self._m0 + time.monotonic() - self._w0

    def pause(self):
        if not self.paused:
            self._m0, self.paused = self.media_now(), True

    def resume(self):
        if self.paused:
            self._w0, self.paused = time.monotonic(), False

    def read(self):
        if self.paused or not self._started:
            time.sleep(0.005)
            return None
        t = self._i / self.fps
        if self.duration is not None and t >= self.duration:
            self.eof = True
            time.sleep(0.005)
            return None
        wait = t - self.media_now()  # read the clock once: a second read can already be past t (negative sleep)
        if wait > 0:
            time.sleep(min(0.005, wait))
            return None
        self._i += 1
        while self.actions and self.actions[0][0] <= t:
            self.actions.pop(0)[1]()
        if any(a <= t < b for a, b in self.stalls):
            return None  # the camera delivered nothing for this slot
        x = self._i % 200
        img = np.dstack([self.tex[:self.size[1], x:x + self.size[0]]] * 3)
        f = SourceFrame(self._seq, self._i - 1, round(t, 6), time.monotonic(), self.epoch, img)
        self._seq += 1
        return f

    def seek(self, media_t):
        self._i = int(round(media_t * self.fps))
        self._m0, self._w0 = media_t, time.monotonic()
        self.epoch += 1
        return True

    def close(self):
        pass


def person(cx, cy=180.0, h=120.0):
    return Detection((cx - h / 4, cy - h / 2, cx + h / 4, cy + h / 2), 0.9, "SYNTHETIC")


# ------------------------------------------------------------------------------------------------ T4 safety
def test_safety_ambiguous_predicting_lost_stall_pause(tmp_path):
    sd = tmp_path / "run"
    sd.mkdir()

    def script(img, f):
        t = f.media_t
        cx = 420 + 10 * t  # right of centre (320) so a LOCKED target gives non-zero yaw
        if t < 1.0 or 1.5 <= t < 2.0 or 2.6 <= t < 4.3:
            return [person(cx)]
        if 1.0 <= t < 1.5:
            return [person(cx - 6), person(cx + 6)]  # two people inside the target's gate
        if 7.0 <= t:
            return [person(cx)]  # someone reappears where the target was: must NOT be re-acquired
        return []  # 2.0-2.6 short miss (PREDICTING); 4.3-7.0 long miss -> LOST after 2 s coast

    resume_timer = []

    def do_pause():
        write_control(sd, 2, "pause")
        tm = threading.Timer(1.0, lambda: write_control(sd, 3, "resume"))
        resume_timer.append(tm)
        tm.start()

    src = ScriptedSource(stalls=[(3.1, 3.6)], duration=8.0, actions={
        0.5: lambda: write_control(sd, 1, "select", track_id=1),
        4.0: do_pause,
    })
    rep = run_live(RunRequest("scripted", sd, vo=False), source=src, pipeline=script)
    assert rep["status"] == "SUCCEEDED", rep.get("completeness")
    rows, _ = read_timeline(sd / "timeline.jsonl")
    fr = frame_rows(rows)

    # non-zero yaw only on LIVE frame rows with a LOCKED target and no stop reason
    for r in rows[1:]:
        if r["yaw"] != 0:
            assert r["type"] == "frame" and r["target_state"] == "LOCKED" and not r["stop_reasons"]
        if r.get("target_state") not in (None, "LOCKED"):
            assert r["yaw"] == 0
    assert rep["yaw_nonzero_while_not_locked_or_stop"] == 0
    assert any(r["yaw"] != 0 for r in fr), "a LOCKED, fresh target should produce some MOCK yaw"

    states = [r["target_state"] for r in fr]
    for s, reason in [("AMBIGUOUS", "TARGET_AMBIGUOUS"), ("PREDICTING", "TARGET_PREDICTING"), ("LOST", "TARGET_LOST")]:
        sel = [r for r in fr if r["target_state"] == s]
        assert sel, f"script never produced {s}: {sorted(set(states))}"
        assert all(r["yaw"] == 0 and reason in r["stop_reasons"] for r in sel)
    # LOST is sticky: once lost, every later frame row says LOST although a person is visible again
    first_lost = states.index("LOST")
    assert set(states[first_lost:]) == {"LOST"}
    assert any(r["tracks"] for r in fr[first_lost:] if r["media_t"] >= 7.5)

    stall = [r for r in rows if r["type"] == "stall"]
    assert stall and all(r["yaw"] == 0 and "INPUT_STALL" in r["stop_reasons"] and
                         any(s.startswith("STALE_FRAME") for s in r["stop_reasons"]) for r in stall)
    hb = [r for r in rows if r["type"] == "heartbeat"]
    assert hb and all(r["yaw"] == 0 and r["stop_reasons"] == ["PAUSED"] and r["input_state"] == "PAUSED" for r in hb)
    assert len({r["media_now"] for r in hb}) == 1  # media clock frozen while paused
    assert not [r for r in fr if hb[0]["run_t"] <= r["run_t"] <= hb[-1]["run_t"]]
    assert rep["control_seq_applied"] == 3


# ------------------------------------------------------------------------------------------------ T2 clocks (FileSource)
def _fixed_person(img, f):
    return [Detection((300, 150, 360, 330), 0.9, "SYNTHETIC")]


@need_vtest
def test_file_realtime_pause_resume_clock(tmp_path):
    sd = tmp_path / "run"
    sd.mkdir()
    timers = [threading.Timer(1.5, lambda: write_control(sd, 1, "pause")),
              threading.Timer(3.5, lambda: write_control(sd, 2, "resume"))]
    for t in timers:
        t.start()
    t0 = time.monotonic()
    rep = run_live(RunRequest(str(VTEST), sd, rate="realtime", max_seconds=4.0, vo=False),
                   source=FileSource(VTEST, "realtime"), pipeline=_fixed_person)
    wall = time.monotonic() - t0
    assert rep["status"] == "SUCCEEDED"
    rows, _ = read_timeline(sd / "timeline.jsonl")
    fr = frame_rows(rows)
    mt = [r["media_t"] for r in fr]
    assert all(b > a for a, b in zip(mt, mt[1:]))  # strictly increasing within the epoch
    hb = [r for r in rows if r["type"] == "heartbeat"]
    assert len(hb) >= 2 and len({r["media_now"] for r in hb}) == 1
    pause_row = next(r for r in rows if r["type"] == "event" and r["events"][0]["type"] == "PAUSE")
    resume_row = next(r for r in rows if r["type"] == "event" and r["events"][0]["type"] == "RESUME")
    assert not [r for r in fr if pause_row["run_t"] < r["run_t"] < resume_row["run_t"]]
    before = [r for r in fr if r["run_t"] < pause_row["run_t"]][-1]
    after = next(r for r in fr if r["run_t"] > resume_row["run_t"])
    assert 0 < after["media_t"] - before["media_t"] <= 0.25  # continues from the same media_t (10 fps file)
    assert abs(resume_row["media_now"] - pause_row["media_now"]) < 0.05
    assert 5.5 < wall < 7.5  # 4 s of media + 2 s pause + model-free setup


@need_vtest
def test_file_realtime_pacing_10s(tmp_path):
    """T2: a 10 s window of vtest.avi at --rate realtime takes 10 s +- 0.5 s of run time."""
    sd = tmp_path / "run"
    rep = run_live(RunRequest(str(VTEST), sd, rate="realtime", max_seconds=10.0, vo=False),
                   source=FileSource(VTEST, "realtime"), pipeline=_fixed_person)
    rows, _ = read_timeline(sd / "timeline.jsonl")
    fr = frame_rows(rows)
    assert abs(rep["run_seconds"] - 10.0) <= 0.5, rep["run_seconds"]
    assert abs((fr[-1]["run_t"] - fr[0]["run_t"]) - (fr[-1]["media_t"] - fr[0]["media_t"])) <= 0.5
    (sd / "pacing.json").write_text(json.dumps({"run_seconds": rep["run_seconds"], "media_seconds": rep["media_seconds"],
                                                "processed": rep["counts"]["processed"]}))


# ------------------------------------------------------------------------------------------------ T3 control / seek
@need_vtest
def test_seek_epoch_reset_and_control_latency(tmp_path):
    sd = tmp_path / "run"
    sd.mkdir()
    lat = {}

    def driver():
        time.sleep(1.0)
        write_control(sd, 1, "select", track_id=1)
        time.sleep(0.4)
        write_control(sd, 2, "no_such_op")
        time.sleep(0.3)
        sent = write_control(sd, 3, "seek", media_t=30.0)
        while time.monotonic() - sent < 2.0:
            try:
                if json.loads((sd / "status.json").read_text()).get("control_seq_applied", 0) >= 3:
                    lat["seek_ms"] = (time.monotonic() - sent) * 1000
                    break
            except (json.JSONDecodeError, FileNotFoundError):
                pass
            time.sleep(0.005)

    th = threading.Thread(target=driver)
    th.start()
    rep = run_live(RunRequest(str(VTEST), sd, rate="realtime", max_seconds=3.0, vo=True),
                   source=FileSource(VTEST, "realtime"), pipeline=_fixed_person)
    th.join()
    assert rep["status"] == "SUCCEEDED", rep.get("completeness")
    rows, _ = read_timeline(sd / "timeline.jsonl")
    ev = {r["events"][0]["type"]: r for r in rows if r["type"] == "event"}
    assert {"SELECT", "CONTROL_IGNORED", "SEEK"} <= set(ev)
    seek = ev["SEEK"]
    assert seek["epoch"] == 1 and seek["tracks"] == [] and seek["target_state"] == "SEARCHING"
    pre = [r for r in frame_rows(rows) if r["epoch"] == 0]
    post = [r for r in frame_rows(rows) if r["epoch"] == 1]
    assert pre[-1]["target_state"] == "LOCKED"
    nxt = post[0]
    assert nxt["target_state"] == "SEARCHING" and nxt["target_id"] is None
    assert all(t["state"] == "TENTATIVE" for t in nxt["tracks"])  # nothing carried over from the old epoch
    assert min(t["id"] for t in nxt["tracks"]) > max(t["id"] for r in pre for t in r["tracks"])  # ids unique per run
    assert nxt["vo_segment"] == pre[-1]["vo_segment"] + 1
    assert nxt["T_world_camera"] == np.eye(4).tolist()
    assert 29.9 <= nxt["media_t"] <= 30.2
    assert all(b["media_t"] > a["media_t"] for a, b in zip(post, post[1:]))
    assert lat.get("seek_ms", 1e9) <= 200, lat
    assert rep["epochs"] == 2 and rep["control_seq_applied"] == 3
    (sd / "control_latency.json").write_text(json.dumps(lat))


# ------------------------------------------------------------------------------------------------ T5 completeness
@need_vtest
def test_completeness_and_incomplete_detection(tmp_path):
    sd = tmp_path / "run"
    rep = run_live(RunRequest(str(VTEST), sd, rate="max", max_seconds=2.0), source=FileSource(VTEST, "max"),
                   pipeline=_fixed_person)
    assert rep["status"] == "SUCCEEDED"
    assert verify_run(sd)["state"] == "SUCCEEDED"
    c = rep["counts"]
    assert c["frames_written"] == c["processed"] and c["captured"] == c["processed"] + c["dropped"]
    with open(sd / "timeline.jsonl", "ab") as fh:  # a row nobody accounted for
        fh.write(b'{"row": 999}\n')
    v = verify_run(sd)
    assert v["state"] == "INCOMPLETE" and any("lines" in p for p in v["problems"])
    rep2 = run_live(RunRequest(str(VTEST), sd, rate="max", max_seconds=1.0), source=FileSource(VTEST, "max"),
                    pipeline=_fixed_person)
    (sd / rep2["frames_manifest"][3]["name"]).unlink()
    assert verify_run(sd)["state"] == "INCOMPLETE"
    (sd / "run_report.json").unlink()
    assert verify_run(sd)["state"] == "INCOMPLETE"


def _spawn(sd: Path, extra=()):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "packages")}
    return subprocess.Popen([sys.executable, "-m", "vision.live_run", str(VTEST), "--session", sd.name,
                             "--runs-root", str(sd.parent), "--rate", "realtime", *extra],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _wait_rows(sd: Path, n: int, timeout=20.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if len(read_timeline(sd / "timeline.jsonl")[0]) >= n:
            return
        time.sleep(0.05)
    raise TimeoutError


@need_vtest
@pytest.mark.skipif(not MODEL.is_file(), reason="yolox_nano weights missing")
def test_sigkill_leaves_incomplete_and_sigterm_cancels(tmp_path):
    sd = tmp_path / "killed"
    p = _spawn(sd)
    _wait_rows(sd, 25)
    p.send_signal(signal.SIGKILL)
    p.wait(10)
    st = json.loads((sd / "status.json").read_text())
    assert st["state"] == "RUNNING" and not (sd / "run_report.json").exists()
    v = verify_run(sd)
    assert v["state"] == "INCOMPLETE" and v["rows_readable"] >= 20

    sd2 = tmp_path / "cancelled"
    p = _spawn(sd2)
    _wait_rows(sd2, 15)
    p.send_signal(signal.SIGTERM)
    assert p.wait(15) == 130
    rep = json.loads((sd2 / "run_report.json").read_text())
    assert rep["status"] == "CANCELLED" and json.loads((sd2 / "status.json").read_text())["state"] == "CANCELLED"
    assert rep["counts"]["processed"] > 0 and verify_run(sd2)["state"] == "CANCELLED"


# ------------------------------------------------------------------------------------------------ T6 webcam
def test_webcam_no_device_fails_fast(tmp_path):
    t0 = time.monotonic()
    with pytest.raises(NoDevice, match="NO_DEVICE"):
        open_live("webcam:9")
    assert time.monotonic() - t0 < 5.5
    sd = tmp_path / "cam"
    t0 = time.monotonic()
    rep = run_live(RunRequest("webcam:9", sd), pipeline=_fixed_person)
    assert time.monotonic() - t0 < 5.5
    assert rep["status"] == "FAILED" and rep["failure"]["reason"] == "NO_DEVICE"
    assert json.loads((sd / "status.json").read_text())["state"] == "FAILED"


# ------------------------------------------------------------------------------------------------ e2e real model
@need_vtest
@pytest.mark.skipif(not MODEL.is_file(), reason="yolox_nano weights missing")
def test_end_to_end_vtest_real_model_and_replay(tmp_path):
    sd = tmp_path / "e2e"
    rep = run_live(RunRequest(str(VTEST), sd, rate="max", max_seconds=5.0))
    assert rep["status"] == "SUCCEEDED" and verify_run(sd)["state"] == "SUCCEEDED"
    assert rep["labels"]["detector"] == "REAL_MODEL" and rep["labels"]["map_position"] == "NOT_AVAILABLE"
    assert not live_run.HARDWARE_ARMED and live_run.DRY_RUN
    c = rep["counts"]
    assert c["processed"] >= 45 and c["frames_written"] == c["processed"]
    assert c["captured"] == c["processed"] + c["dropped"]
    assert rep["yaw_nonzero_while_not_locked_or_stop"] == 0
    tj = json.loads((sd / "tracks.json").read_text())
    assert tj["labels"]["replay"] == "OPEN_LOOP_REPLAY" and len(tj["frames"]) == c["processed"]
    k, tid = next((f["k"], t["id"]) for f in tj["frames"] for t in f["tracks"] if t["state"] == "LOCKED")
    out = replay_control(tj["frames"], tj["image_wh"][0], tid, k)
    assert out["labels"]["loop"].startswith("OPEN_LOOP_REPLAY")
    assert out["summary"]["nonzero_yaw_while_not_locked"] == 0
    assert (sd / "frames" / "latest.jpg").is_file()


# ------------------------------------------------------------------------------------------------ T1 through the worker
def test_worker_drops_with_slow_pipeline(tmp_path):
    """100 fps source, pipeline sleeping 50 ms: drops are the mechanism; every captured frame is accounted for."""
    sd = tmp_path / "slow"

    def slow(img, f):
        time.sleep(0.05)
        return []

    rep = run_live(RunRequest("scripted", sd, vo=False), source=ScriptedSource(fps=100.0, duration=2.0), pipeline=slow)
    assert rep["status"] == "SUCCEEDED", rep.get("completeness")
    c = rep["counts"]
    rows, _ = read_timeline(sd / "timeline.jsonl")
    fr = frame_rows(rows)
    assert c["dropped"] == c["captured"] - c["processed"] and c["dropped"] > c["processed"]
    assert sum(r["dropped_since_last"] for r in fr) <= c["dropped"]
    seqs = [r["seq"] for r in fr]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert all(r["yaw"] == 0 for r in rows if "yaw" in r)
