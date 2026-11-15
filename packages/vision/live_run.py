"""Live run on ONE time axis: a video file played at its own rate (or as fast as possible) or a local webcam ->
latest-frame buffer -> person detector -> MultiTracker -> TargetSelector -> YawController (MOCK, VIRTUAL telemetry)
-> monocular VO, all recorded in one append-only timeline.

python -m vision.live_run <video|webcam:N> --session X [--runs-root outputs/runs] [--rate realtime|max]
       [--model yolox_nano] [--fx F | --calib camera.json] [--vo-width 640] [--no-vo] [--max-seconds S]
python -m vision.live_run --verify outputs/runs/X        # file-backed completeness check (prints JSON)

Two clocks (docs/PLAN_LIVE_RUN_TIMELINE.md): `media_t` (source time) drives the tracker, the selector and the
controller; `run_t` (monotonic seconds since the run started, pauses included) is the single axis of
timeline.jsonl and tracks.json. Seek bumps `epoch` and resets tracker / selector / controller / VO segment.

Writes into <runs-root>/<session>/:
  status.json        progress for the UI (atomic rewrite, same shape as map / tracking jobs + live fields)
  control.json       written by the SERVER (atomic): {"seq": n, "op": ..., "args": {...}} or {"queue": [...]}
  timeline.jsonl     row 0 header, then frame / heartbeat / event / end rows; append-only, fsync every N rows
  frames/f%06d.jpg   processed frames (long side 640, JPEG q75); frames/latest.jpg (atomic rename) for the live view
  tracks.json        same shape the Tracking tab reads (t := run_t); replay of it is OPEN_LOOP_REPLAY
  run_report.json    written last; status SUCCEEDED only after row count + sha256 + frame manifest checks

Safety: commands are MOCK values computed for display; HARDWARE_ARMED=False, DRY_RUN=True; this module imports no
drone adapter and opens no socket. Map position is NOT_AVAILABLE (VO is relative, per segment, never in a map frame).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from mapping.frame_source import InputError
from tracking.tracker import TargetSelector, TargetState, TrackerConfig, TrackState
from control.yaw_controller import YawController

from . import tracker_setup
from . import vo as vo_mod
from .live_source import LatestFrameBuffer, LiveSource, NoDevice, SourceFrame, open_live
from .person_detector import ModelUnavailable, make_detector, sha256_file

HARDWARE_ARMED = False
DRY_RUN = True
SCHEMA = "skywarden/live-run@1"
STAGES = ["OPEN_SOURCE", "LOAD_MODEL", "LIVE", "FINALIZE"]
VIRTUAL_BATTERY_PCT = 85
CONTROL_OPS = {"pause", "resume", "seek", "stop", "select", "clear"}
LABELS = {"detector": "REAL_MODEL", "vo": "RELATIVE / no loop closure / NOT_NAVIGATION_SAFE",
          "commands": "MOCK", "telemetry": f"VIRTUAL (always fresh, battery {VIRTUAL_BATTERY_PCT}%)",
          "map_position": "NOT_AVAILABLE", "relocalization": "NOT_IMPLEMENTED", "loop": "OPEN_LOOP (camera never turns)",
          "hardware_armed": HARDWARE_ARMED, "dry_run": DRY_RUN, "transmitted": False}

Pipeline = Callable[[np.ndarray, SourceFrame], list]  # (image BGR native, frame) -> list[Detection]


class Cancelled(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _atomic_json(path: Path, obj: dict, compact: bool = False) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")) if compact else json.dumps(obj, indent=1))
    tmp.replace(path)


def _pct(xs, p):
    return round(float(np.percentile(xs, p)), 2) if len(xs) else None


@dataclass
class RunRequest:
    source: str                      # video path or "webcam:N"
    session_dir: Path
    rate: str = "realtime"           # realtime | max (file only; webcam is always realtime)
    model: str = "yolox_nano"
    score_thr: float = 0.35
    max_seconds: float | None = None  # media seconds actually played (summed over epochs; pauses excluded)
    vo: bool = True
    vo_width: int = 640
    fx: float | None = None
    calib: Path | None = None
    frame_long_side: int = 640
    jpeg_quality: int = 75
    fsync_every: int = 10
    stall_after_s: float = 0.3       # = YawController max_frame_age_s
    heartbeat_s: float = 0.5         # PAUSED / STALLED rows at <= 2 Hz
    control_poll_s: float = 0.05
    status_every_s: float = 0.5
    tracker: TrackerConfig = field(default_factory=TrackerConfig)


# ---------------------------------------------------------------------------------------------------- writers
class StatusWriter:
    def __init__(self, sd: Path):
        self.path = sd / "status.json"
        self.state = {"state": "RUNNING", "kind": "runs", "stage": None, "stage_index": -1, "stages": STAGES,
                      "progress": None, "message": "starting", "pid": os.getpid(), "started_at": _now(),
                      "updated_at": _now(), "control_seq_applied": 0, "input_state": None, "last_row": 0,
                      "last_run_t": 0.0, "dropped_total": 0, "processed_total": 0, "epoch": 0}
        self.write()

    def write(self, **changes) -> None:
        self.state.update(changes, updated_at=_now())
        _atomic_json(self.path, self.state)

    def stage(self, name: str, message: str) -> None:
        self.write(stage=name, stage_index=STAGES.index(name), progress=None, message=message)


class Timeline:
    """Append-only JSON lines with an incremental sha256; flush+fsync every N rows and whenever asked."""

    def __init__(self, path: Path, fsync_every: int):
        self.path = path
        self.fh = open(path, "wb")
        self.sha = hashlib.sha256()
        self.rows = 0
        self.every = fsync_every
        self._since = 0

    def append(self, row: dict, sync: bool = False) -> int:
        row = {"row": self.rows, **row}
        b = (json.dumps(row, separators=(",", ":")) + "\n").encode()
        self.fh.write(b)
        self.sha.update(b)
        self.rows += 1
        self._since += 1
        if sync or self._since >= self.every:
            self.sync()
        return row["row"]

    def sync(self) -> None:
        self.fh.flush()
        os.fsync(self.fh.fileno())
        self._since = 0

    def close(self) -> None:
        if not self.fh.closed:
            self.sync()
            self.fh.close()


# ---------------------------------------------------------------------------------------------------- verify
def read_timeline(path: Path) -> tuple[list[dict], int]:
    """Rows that parse, and the number of raw lines (a SIGKILL can leave a torn last line)."""
    rows, n = [], 0
    if not path.is_file():
        return rows, 0
    with open(path, "rb") as fh:
        for line in fh:
            n += 1
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows, n


def verify_run(session_dir: str | Path) -> dict:
    """File-backed completeness check. Returns {"state", "problems", "rows_readable"}. state is the report's status
    when everything checks out, "RUNNING" while the worker is alive and has not finished, else "INCOMPLETE"."""
    sd = Path(session_dir)
    problems: list[str] = []
    status = _read(sd / "status.json")
    report = _read(sd / "run_report.json")
    tl = sd / "timeline.jsonl"
    rows, nlines = read_timeline(tl)
    out = {"rows_readable": len(rows), "lines": nlines}
    if report is None:
        if status and status.get("state") == "RUNNING" and _pid_alive(status.get("pid")):
            return {**out, "state": "RUNNING", "problems": []}
        return {**out, "state": "INCOMPLETE", "problems": ["run_report.json missing (worker killed or crashed)"]}
    if nlines != report.get("timeline_rows"):
        problems.append(f"timeline has {nlines} lines, report says {report.get('timeline_rows')}")
    if len(rows) != nlines:
        problems.append(f"{nlines - len(rows)} unparsable timeline lines")
    if [r.get("row") for r in rows] != list(range(len(rows))):
        problems.append("row numbers are not 0..n-1")
    if tl.is_file() and sha256_file(tl) != report.get("timeline_sha256"):
        problems.append("timeline sha256 does not match the report")
    for f in report.get("frames_manifest", []):
        p = sd / f["name"]
        if not p.is_file() or p.stat().st_size != f["size"]:
            problems.append(f"frame {f['name']} missing or wrong size")
            if len(problems) > 20:
                break
    if not (sd / "tracks.json").is_file():
        problems.append("tracks.json missing")
    state = report.get("status") if not problems else "INCOMPLETE"
    return {**out, "state": state, "problems": problems}


def _read(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------------------------------------------------------------------------------------------------- worker
class _Capture(threading.Thread):
    """Source -> buffer. realtime: overwrite (drops are the mechanism); max: back-pressure (no drops)."""

    def __init__(self, src: LiveSource, buf: LatestFrameBuffer, stop: threading.Event):
        super().__init__(daemon=True, name="live-capture")
        self.src, self.buf, self.stop = src, buf, stop
        self.eof = False
        self.error: str | None = None

    def run(self) -> None:
        try:
            while not self.stop.is_set():
                f = self.src.read()
                if f is None:
                    self.eof = bool(getattr(self.src, "eof", False))
                    if self.eof:
                        time.sleep(0.01)
                    continue
                self.eof = False
                if self.src.rate == "max":
                    self.buf.put_wait(f, self.stop)
                else:
                    self.buf.put(f)
        except Exception as e:  # a decoder crash is reported, the worker sees a stall then ends FAILED
            traceback.print_exc()
            self.error = f"{type(e).__name__}: {e}"


def _track_dict(tr) -> dict:
    return {"id": tr.id, "state": tr.state.value, "bbox": [round(float(v), 1) for v in tr.bbox()],
            "sigma_px": round(tr.position_sigma_px(), 1), "hits": tr.hits}


def _resize_long(img: np.ndarray, long_side: int) -> np.ndarray:
    h, w = img.shape[:2]
    s = min(1.0, long_side / max(w, h))
    return cv2.resize(img, (int(round(w * s)), int(round(h * s))), interpolation=cv2.INTER_AREA) if s < 1 else img


def _resize_width(img: np.ndarray, width: int) -> np.ndarray:
    h, w = img.shape[:2]
    if w <= width:
        return img
    return cv2.resize(img, (width, int(round(h * width / w))), interpolation=cv2.INTER_AREA)


def run_live(req: RunRequest, source: LiveSource | None = None, pipeline: Pipeline | None = None,
             cancel: threading.Event | None = None, detector_info: dict | None = None) -> dict:
    """Test seam: `source` and `pipeline` (image -> detections) may be injected; otherwise they are opened from
    `req`. Returns the run report (also written to run_report.json)."""
    assert not HARDWARE_ARMED and DRY_RUN
    return _LiveRun(req, source, pipeline, cancel or threading.Event(), detector_info).run()


class _LiveRun:
    """State of one run_live call, split by stage: _open (source, model, VO, header row), _loop (control, status,
    stall / pause rows, _process per frame), _finalize (end row, tracks.json, report, completeness check)."""

    def __init__(self, req: RunRequest, source: LiveSource | None, pipeline: Pipeline | None,
                 cancel: threading.Event, detector_info: dict | None):
        self.req, self.source, self.pipeline, self.cancel, self.detector_info = req, source, pipeline, cancel, detector_info
        sd = self.sd = req.session_dir
        (sd / "frames").mkdir(parents=True, exist_ok=True)
        for old in list((sd / "frames").glob("f*.jpg")) + [sd / "run_report.json", sd / "tracks.json", sd / "control.json"]:
            old.unlink(missing_ok=True)  # a rerun into the same folder must not mix old files in
        self.status = StatusWriter(sd)
        self.tl = Timeline(sd / "timeline.jsonl", req.fsync_every)
        self.labels = dict(LABELS)
        self.report: dict = {"session_id": sd.name, "kind": "runs", "schema": SCHEMA, "status": "RUNNING",
                             "created_at": _now(), "labels": self.labels}
        self.frames_out: list[dict] = []
        self.manifest: list[dict] = []
        self.events: list[dict] = []
        self.counts = {"heartbeat_rows": 0, "stall_rows": 0, "event_rows": 0, "discarded_old_epoch": 0}
        self.proc_ms: list[float] = []
        self.lat_ms: list[float] = []
        self.det_ms: list[float] = []
        self.vo_ms: list[float] = []
        self.stop_cap = threading.Event()
        self.cap_thread: _Capture | None = None
        self.buf = LatestFrameBuffer()
        self.end_reason, self.msg = None, ""
        self.t0 = time.monotonic()
        self.epoch = 0
        self.vo = None

    def run(self) -> dict:
        report, status = self.report, self.status
        try:
            self._open()
            self._loop()
        except Cancelled:
            self.end_reason = "CANCELLED"
        except NoDevice as e:
            self.end_reason, self.msg = "FAILED", str(e)
            report["failure"] = {"stage": status.state["stage"], "reason": "NO_DEVICE", "message": self.msg}
        except (InputError, ModelUnavailable) as e:
            self.end_reason, self.msg = "FAILED", str(e)
            report["failure"] = {"stage": status.state["stage"], "reason": type(e).__name__, "message": self.msg}
        except Exception as e:  # every failure is reported; the traceback goes to process.log
            traceback.print_exc()
            self.end_reason, self.msg = "FAILED", f"{type(e).__name__}: {e}"
            report["failure"] = {"stage": status.state["stage"], "reason": "EXCEPTION", "message": self.msg}
        return self._finalize()  # also for cancel / failure: partial files stay readable

    # ------------------------------------------------------------------------------------------------ open
    def _open(self) -> None:
        req, report, status, labels = self.req, self.report, self.status, self.labels
        # open source first so a missing webcam fails fast (NO_DEVICE within 5 s)
        status.stage("OPEN_SOURCE", f"opening {req.source}")
        if self.source is None:
            self.source = open_live(req.source, req.rate)
        source = self.source
        smeta = self.smeta = source.meta()
        if smeta.get("kind") == "file" and smeta.get("path"):
            smeta["sha256"] = sha256_file(Path(smeta["path"]))
        report["source"] = smeta
        self.native_w, native_h = smeta["size"]

        status.stage("LOAD_MODEL", f"loading {req.model}" if self.pipeline is None else "using injected pipeline")
        if self.pipeline is None:
            det = make_detector(req.model, score_thr=tracker_setup.detector_threshold(req.score_thr, req.tracker))
            self.pipeline = lambda img, _f: det.detect(img)  # noqa: E731
            report["detector"] = det.info()
            self.last_ms = lambda: det.last_ms  # noqa: E731
        else:
            report["detector"] = self.detector_info or {"name": "injected", "source": "STUB (test pipeline)"}
            labels["detector"] = report["detector"].get("source", "STUB")
            self.last_ms = None

        intr = None
        if req.vo:
            ww = min(req.vo_width, self.native_w)
            wh = int(round(native_h * ww / self.native_w))
            K, intr = vo_mod.intrinsics((ww, wh), (self.native_w, native_h), fx=req.fx, calib=req.calib)
            self.vo = vo_mod.VisualOdometry(K, ww, wh)
        report["intrinsics"] = intr
        settings = {k: (str(v) if isinstance(v, Path) else v) for k, v in req.__dict__.items() if k != "tracker"}
        settings["tracker"] = req.tracker.__dict__
        settings["tracker_mode"] = req.tracker.describe()
        report["settings"] = settings

        self.embedder = tracker_setup.make_embedder(req.tracker)
        self.tracker_cfg = tracker_setup.stage_config(req.tracker, req.score_thr)
        self.tracker = tracker_setup.make_tracker(self.tracker_cfg, self.embedder)
        self.selector = TargetSelector()
        self.ctl = YawController(self.native_w)

        # header row, then the clock starts
        self.t0 = time.monotonic()
        self.tl.append({"type": "header", "schema": SCHEMA, "run_t": 0.0, "wall_t": round(time.time(), 3),
                        "source": smeta, "intrinsics": intr, "intrinsics_provenance": (intr or {}).get("source"),
                        "labels": labels, "detector": report["detector"], "settings": settings,
                        "image_wh": [self.native_w, native_h],
                        "clocks": {"run_t": "monotonic s since run start (pauses included); the timeline axis",
                                   "media_t": smeta.get("media_clock"),
                                   "media_now": "source clock when the row was written (frozen while paused)"},
                        "vo": vo_mod.header() if req.vo else None,
                        "map_position": "NOT_AVAILABLE", "relocalization": "NOT_IMPLEMENTED"}, sync=True)
        source.start_clock()
        self.cap_thread = _Capture(source, self.buf, self.stop_cap)
        self.cap_thread.start()
        status.stage("LIVE", "running")

    # ------------------------------------------------------------------------------------------------ helpers
    def run_t(self) -> float:
        return round(time.monotonic() - self.t0, 4)

    def mnow(self) -> float | None:
        m = self.source.media_now()
        return None if m is None else round(m, 4)

    def base_row(self, kind: str, istate: str, reasons: list[str], ev: list | None = None) -> dict:
        return {"type": kind, "epoch": self.epoch, "run_t": self.run_t(), "media_t": None, "media_now": self.mnow(),
                "wall_t": round(time.time(), 3), "proc_ms": None, "latency_ms": None, "dropped_since_last": 0,
                "input_state": istate, "image": None, "detections": [], "tracks": [],
                "target_id": self.selector.target_id, "target_state": self.target_state.value, "yaw": 0,
                "stop_reasons": reasons, "command_source": "MOCK", "vo_state": None, "vo_segment": None,
                "T_world_camera": None, "vo_reason": None, "shot_change": False,
                "map_position": "NOT_AVAILABLE", "events": ev or []}

    def event(self, kind: str, **data) -> dict:
        e = {"type": kind, "run_t": self.run_t(), "media_now": self.mnow(), "epoch": self.epoch, **data}
        self.events.append(e)
        return e

    # ------------------------------------------------------------------------------------------------ control
    def apply(self, cmd: dict) -> dict:
        source, event = self.source, self.event
        op, args, seq = cmd.get("op"), cmd.get("args") or {}, cmd.get("seq")
        if op not in CONTROL_OPS:
            return event("CONTROL_IGNORED", seq=seq, op=op, reason="unknown op")
        if op == "pause":
            source.pause()
            self.paused = True
            return event("PAUSE", seq=seq)
        if op == "resume":
            source.resume()
            self.paused = False
            self.last_take_wall = time.monotonic()
            return event("RESUME", seq=seq)
        if op == "stop":
            self.stop_requested = True
            return event("STOP", seq=seq)
        if op == "clear":
            self.selector.select(None)
            self.target_state = TargetState.SEARCHING
            self.ctl = YawController(self.native_w)
            return event("CLEAR", seq=seq)
        if op == "select":
            tid = args.get("track_id")
            tr = self.tracker.get(tid) if isinstance(tid, int) else None
            if tr is None or tr.state == TrackState.TENTATIVE:
                return event("SELECT_REJECTED", seq=seq, track_id=tid,
                             reason="no confirmed track with that id in the current frame")
            self.selector.select(tid)
            self.target_state, _ = self.selector.evaluate(self.tracker)
            return event("SELECT", seq=seq, track_id=tid, track_state=tr.state.value)
        # seek
        to = args.get("media_t")
        if not isinstance(to, (int, float)) or not np.isfinite(to):
            return event("CONTROL_IGNORED", seq=seq, op=op, reason="seek needs a finite args.media_t")
        frm = self.mnow()
        if not source.seek(float(to)):
            return event("SEEK_REJECTED", seq=seq, reason="source cannot seek (webcam)")
        if frm is not None and self.media_t0 is not None:
            self.played_before += max(0.0, frm - self.media_t0)
        self.media_t0 = source.media_now()  # None for rate=max until the first frame of the new epoch
        self.epoch = getattr(source, "epoch", self.epoch + 1)
        self.buf.drain()
        nxt = self.tracker._next_id
        self.tracker = tracker_setup.make_tracker(self.tracker_cfg, self.embedder)  # appearance dies with the old tracks
        self.tracker._next_id = nxt  # ids stay unique within the run, so a replay can never confuse two epochs
        self.selector = TargetSelector()
        self.target_state = TargetState.SEARCHING
        self.ctl = YawController(self.native_w)
        if self.vo is not None:
            self.vo.need_reset, self.vo.prev_hist = True, None  # next frame starts a new segment (identity origin)
        self.last_frame_media_t = None
        self.last_take_wall = time.monotonic()
        return event("SEEK", seq=seq, **{"from": frm, "to": round(float(to), 4)})

    def poll_control(self) -> None:
        p = self.sd / "control.json"
        try:
            st = p.stat()
        except FileNotFoundError:
            return
        sig = (st.st_mtime_ns, st.st_size)
        if sig == self.ctl_sig:
            return
        try:
            obj = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return  # torn write; the server writes atomically, so retry on the next poll
        self.ctl_sig = sig
        cmds = obj.get("queue") if isinstance(obj, dict) and "queue" in obj else [obj]
        cmds = [c for c in cmds if isinstance(c, dict) and isinstance(c.get("seq"), int) and c["seq"] > self.applied_seq]
        for c in sorted(cmds, key=lambda c: c["seq"]):
            try:
                e = self.apply(c)
            except Exception as ex:  # a bad command never crashes the run
                e = self.event("CONTROL_ERROR", seq=c["seq"], op=c.get("op"), reason=f"{type(ex).__name__}: {ex}")
            self.applied_seq = c["seq"]
            self.counts["event_rows"] += 1
            istate = "PAUSED" if self.paused else self.input_state
            self.tl.append(self.base_row("event", istate, [str(e["type"])], [e]), sync=True)
            self.status.write(control_seq_applied=self.applied_seq, epoch=self.epoch, input_state=istate,
                              last_row=self.tl.rows - 1, last_run_t=self.run_t(), target_id=self.selector.target_id,
                              target_state=self.target_state.value, last_event=e)

    def end_check(self) -> str | None:
        if self.cancel.is_set():
            return "CANCELLED"
        if self.stop_requested:
            return "STOP_REQUESTED"
        if self.cap_thread.error:
            return "SOURCE_ERROR"
        m = self.source.media_now()
        if (self.req.max_seconds is not None and m is not None and self.media_t0 is not None
                and self.played_before + m - self.media_t0 >= self.req.max_seconds):
            return "MAX_SECONDS"
        return None

    # ------------------------------------------------------------------------------------------------ loop
    def _loop(self) -> None:
        req, buf = self.req, self.buf
        self.paused = False
        self.stop_requested = False
        self.applied_seq = 0
        self.ctl_sig = None
        self.last_hb = -1e9
        self.last_take_wall = time.monotonic()
        self.last_frame_media_t: float | None = None
        self.media_t0 = self.source.media_now()  # media time at which the current epoch started playing
        self.played_before = 0.0  # media seconds played in earlier epochs
        self.last_dropped = 0
        self.last_status = 0.0
        last_ctl_poll = 0.0
        self.recent: list[float] = []
        self.target_state = TargetState.SEARCHING
        self.input_state = "LIVE"
        self.prev_vo_seg = None
        while True:
            now = time.monotonic()
            if now - last_ctl_poll >= req.control_poll_s:
                self.poll_control()
                last_ctl_poll = now
            self.end_reason = self.end_check()
            if self.end_reason:
                return
            if self.media_t0 is None:
                self.media_t0 = self.source.media_now()
            if now - self.last_status >= req.status_every_s:
                self._write_status(now)
            if self.paused:
                if now - self.last_hb >= req.heartbeat_s:
                    self.counts["heartbeat_rows"] += 1
                    self.tl.append(self.base_row("heartbeat", "PAUSED", ["PAUSED"]))
                    self.last_hb = now
                time.sleep(req.control_poll_s)
                continue
            f: SourceFrame | None = buf.take(timeout=req.control_poll_s)
            if f is None:
                if self.cap_thread.eof and buf.counts()["waiting"] == 0:
                    self.end_reason = "EOF"
                    return
                self._maybe_stall_row()
                continue
            if f.epoch != self.epoch:
                self.counts["discarded_old_epoch"] += 1  # decoded before the seek; never processed
                continue
            self._process(f)

    def _write_status(self, now: float) -> None:
        recent = self.recent = [x for x in self.recent if x > now - 2.0]
        fps = len(recent) / 2.0
        dur = self.smeta.get("duration_s")
        m = self.source.media_now()
        istate = "PAUSED" if self.paused else self.input_state
        self.status.write(input_state=istate, last_row=self.tl.rows - 1,
                          last_run_t=self.run_t(), processed_total=len(self.frames_out),
                          dropped_total=self.buf.dropped + self.counts["discarded_old_epoch"],
                          captured_total=self.buf.produced, epoch=self.epoch, control_seq_applied=self.applied_seq,
                          target_id=self.selector.target_id, target_state=self.target_state.value, recent_fps=fps,
                          latency_ms_p95_recent=_pct(self.lat_ms[-30:], 95), media_now=None if m is None else round(m, 3),
                          progress=[round(m or 0, 2), round(dur, 2)] if dur else None,
                          message=f"{istate} · {len(self.frames_out)} processed · {fps:.1f} fps · dropped {self.buf.dropped}")
        self.last_status = now

    def _maybe_stall_row(self) -> None:
        req = self.req
        if time.monotonic() - self.last_take_wall <= req.stall_after_s:
            return
        self.input_state = "STALLED"
        if time.monotonic() - self.last_hb < req.heartbeat_s:
            return
        m = self.source.media_now()
        ft = self.last_frame_media_t if self.last_frame_media_t is not None else (m or 0.0) - req.stall_after_s - 1e-3
        # the controller is asked with NO track, so it can only stop; STALE_FRAME comes from it
        cmd = self.ctl.command(m or 0.0, self.target_state, None, ft, m, VIRTUAL_BATTERY_PCT)
        assert cmd.yaw == 0
        r = self.base_row("stall", "STALLED", [*cmd.stop_reasons, "INPUT_STALL"])
        self.counts["stall_rows"] += 1
        self.tl.append(r)
        self.last_hb = time.monotonic()

    def _process(self, f: SourceFrame) -> None:
        req, sd, vo, native_w = self.req, self.sd, self.vo, self.native_w
        p0 = time.monotonic()
        self.last_take_wall = p0
        self.input_state = "LIVE"
        dets = self.pipeline(f.image, f)
        dms = self.last_ms() if self.last_ms else (time.monotonic() - p0) * 1000
        self.det_ms.append(dms)
        tracks = self.tracker.update(f.media_t, dets, frame=f.image)
        self.target_state, ttrack = self.selector.evaluate(self.tracker)
        target_state = self.target_state
        m_now = self.source.media_now()
        m_now = f.media_t if m_now is None else m_now
        cmd = self.ctl.command(m_now, target_state, ttrack if target_state == TargetState.LOCKED else None,
                               f.media_t, m_now, VIRTUAL_BATTERY_PCT)
        if cmd.yaw != 0 and (target_state != TargetState.LOCKED or cmd.stop_reasons):
            raise AssertionError("non-zero yaw without a LOCKED target")  # cannot happen; checked anyway
        small = _resize_long(f.image, req.frame_long_side)
        vrec = None
        if vo is not None:
            v0 = time.perf_counter()
            vimg = small if small.shape[1] == vo.w else _resize_width(f.image, vo.w)
            vrec = vo.process(f.media_t, vimg)
            self.vo_ms.append((time.perf_counter() - v0) * 1000)
        k = len(self.frames_out)
        name = f"frames/f{k:06d}.jpg"
        ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, req.jpeg_quality])
        data = jpg.tobytes()
        (sd / name).write_bytes(data)
        tmp = sd / "frames" / ".latest.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, sd / "frames" / "latest.jpg")
        self.manifest.append({"name": name, "size": len(data)})

        shot = bool(vrec and vrec["state"] == "LOST" and vrec["reason"].startswith("scene change"))
        ev = []
        if vrec and self.prev_vo_seg is not None and vrec["segment"] != self.prev_vo_seg:
            ev.append(self.event("VO_NEW_SEGMENT", segment=vrec["segment"], reason=vrec["reason"]))
        if shot:
            ev.append(self.event("SHOT_CHANGE", media_t=f.media_t, reason=vrec["reason"]))
        self.prev_vo_seg = vrec["segment"] if vrec else None
        dropped = self.buf.dropped
        p1 = time.monotonic()
        pms, lms = (p1 - p0) * 1000, (p1 - f.wall_captured) * 1000
        self.proc_ms.append(pms)
        self.lat_ms.append(lms)
        self.recent.append(p1)
        trk = [_track_dict(tr) for tr in tracks if tr.state != TrackState.LOST]
        dlist = [[*(round(float(v), 1) for v in d.bbox_xyxy), round(float(d.score), 3)] for d in dets]
        row = self.base_row("frame", "LIVE", list(cmd.stop_reasons), ev)
        row.update({"k": k, "seq": f.seq, "source_index": f.index, "media_t": round(f.media_t, 4),
                    "media_now": round(m_now, 4), "proc_ms": round(pms, 2), "detect_ms": round(dms, 2),
                    "latency_ms": round(lms, 2), "dropped_since_last": dropped - self.last_dropped, "image": name,
                    "detections": dlist, "tracks": trk, "target_id": self.selector.target_id,
                    "target_state": target_state.value, "yaw": cmd.yaw,
                    "error_norm": None if ttrack is None else round((float(ttrack.center[0]) - native_w / 2) / (native_w / 2), 3),
                    "shot_change": shot})
        if vrec:
            row.update({"vo_state": vrec["state"], "vo_segment": vrec["segment"],
                        "T_world_camera": vrec["T_world_camera"], "vo_reason": vrec["reason"],
                        "vo_inliers": vrec["inliers"]})
        self.last_dropped = dropped
        self.tl.append(row, sync=bool(ev))
        self.frames_out.append({"k": k, "t": row["run_t"], "media_t": row["media_t"], "epoch": self.epoch,
                                "source_index": f.index, "image": name, "detect_ms": round(dms, 2),
                                "detections": dlist, "tracks": trk})
        self.last_frame_media_t = f.media_t

    # ------------------------------------------------------------------------------------------------ finalize
    def _finalize(self) -> dict:
        sd, tl, report, status, buf, end_reason = self.sd, self.tl, self.report, self.status, self.buf, self.end_reason
        self.stop_cap.set()
        if self.cap_thread is not None:
            self.cap_thread.join(2.0)
            if self.cap_thread.error and end_reason == "SOURCE_ERROR":
                report["failure"] = {"stage": "LIVE", "reason": "SOURCE_ERROR", "message": self.cap_thread.error}
        if self.source is not None:
            try:
                self.source.close()
            except Exception:
                pass
        buf.finalize()
        status.write(stage="FINALIZE", stage_index=STAGES.index("FINALIZE"), message="writing tracks.json and run_report.json")
        end_state = {"EOF": "EOF", "MAX_SECONDS": "EOF", "STOP_REQUESTED": "STOPPED", "CANCELLED": "CANCELLED"}.get(end_reason, "FAILED")
        if tl.rows:  # header exists -> close the timeline with an end row
            tl.append({"type": "end", "epoch": self.epoch, "run_t": round(time.monotonic() - self.t0, 4), "media_t": None,
                       "wall_t": round(time.time(), 3), "input_state": end_state, "yaw": 0,
                       "stop_reasons": ["END"], "end_reason": end_reason, "map_position": "NOT_AVAILABLE", "events": []},
                      sync=True)
        tl.close()
        image_wh = report.get("source", {}).get("size")
        _atomic_json(sd / "tracks.json", {"session_id": sd.name, "image_wh": image_wh, "time_axis": "run_t",
                                          "labels": {**self.labels, "replay": "OPEN_LOOP_REPLAY", "source_kind": "live_run"},
                                          "frames": self.frames_out}, compact=True)
        rows, _ = read_timeline(tl.path)
        frame_rows = [r for r in rows if r.get("type") == "frame"]
        captured = buf.produced
        dropped_total = buf.dropped + self.counts["discarded_old_epoch"]
        self._summarize(rows, frame_rows, captured, dropped_total)
        problems = self._completeness(rows, frame_rows, captured, dropped_total)
        msg = self.msg
        if end_reason == "CANCELLED":
            report["status"], msg = "CANCELLED", "cancelled by user (SIGTERM); partial files are readable"
        elif end_reason in ("EOF", "MAX_SECONDS", "STOP_REQUESTED"):
            report["status"] = "SUCCEEDED" if not problems else "INCOMPLETE"
            msg = (f"{len(frame_rows)} frames processed · {report['processed_fps']} fps · latency p95 "
                   f"{report['latency_ms']['p95']} ms · dropped {dropped_total} · end {end_reason}")
            if problems:
                msg += " · INCOMPLETE: " + "; ".join(problems)
        else:
            report["status"] = "FAILED"
            msg = msg or (report.get("failure") or {}).get("message") or str(end_reason)
            report.setdefault("failure", {"stage": "LIVE", "reason": str(end_reason), "message": msg})
        _atomic_json(sd / "run_report.json", report)  # the report is the last data file
        status.write(state=report["status"], message=msg, input_state=end_state, last_row=tl.rows - 1,
                     processed_total=len(frame_rows), dropped_total=dropped_total, failure=report.get("failure"))
        print(f"{report['status']}: {msg}", flush=True)
        return report

    def _summarize(self, rows: list[dict], frame_rows: list[dict], captured: int, dropped_total: int) -> None:
        req, buf, tl, vo, counts = self.req, self.buf, self.tl, self.vo, self.counts
        lat_ms, proc_ms, det_ms, vo_ms = self.lat_ms, self.proc_ms, self.det_ms, self.vo_ms
        span = (frame_rows[-1]["run_t"] - frame_rows[0]["run_t"]) if len(frame_rows) > 1 else 0.0
        paused_s, p_at = 0.0, None  # operator pauses are not processing time
        for e in self.events:
            if e["type"] == "PAUSE" and p_at is None:
                p_at = e["run_t"]
            elif e["type"] == "RESUME" and p_at is not None:
                paused_s, p_at = paused_s + e["run_t"] - p_at, None
        if p_at is not None and frame_rows:
            paused_s += max(0.0, frame_rows[-1]["run_t"] - p_at)
        active = span - paused_s
        media_played = 0.0
        for ep in sorted({r["epoch"] for r in frame_rows}):
            mts = [r["media_t"] for r in frame_rows if r["epoch"] == ep]
            media_played += mts[-1] - mts[0]
        yaws = [r["yaw"] for r in rows if "yaw" in r]
        bad_yaw = sum(1 for r in rows if r.get("yaw", 0) != 0 and (r.get("target_state") != "LOCKED" or r.get("stop_reasons")
                                                                    or r.get("input_state") != "LIVE" or r.get("type") != "frame"))
        no_pose, cur = [], None
        for r in frame_rows:
            if r.get("vo_state") is None:
                continue
            if r["T_world_camera"] is None:
                if cur is None:
                    cur = {"run_t0": r["run_t"], "run_t1": r["run_t"], "rows": 0, "reason": r.get("vo_reason")}
                cur["run_t1"], cur["rows"] = r["run_t"], cur["rows"] + 1
            elif cur is not None:
                no_pose.append(cur)
                cur = None
        if cur is not None:
            no_pose.append(cur)

        def cnt(key, rs):
            out: dict = {}
            for r in rs:
                out[str(r.get(key))] = out.get(str(r.get(key)), 0) + 1
            return out

        self.report.update({
            "end_reason": self.end_reason,
            "counts": {"captured": captured, "processed": len(frame_rows), "dropped": dropped_total,
                       "dropped_by_buffer": buf.dropped, "discarded_old_epoch": counts["discarded_old_epoch"],
                       "captured_minus_processed": captured - len(frame_rows), "rows": tl.rows,
                       "frames_written": len(self.manifest), **{k: v for k, v in counts.items() if k != "discarded_old_epoch"}},
            "latency_ms": {"p50": _pct(lat_ms, 50), "p95": _pct(lat_ms, 95), "max": round(max(lat_ms), 2) if lat_ms else None,
                           "definition": "monotonic processed-row time minus time the frame left the source"},
            "proc_ms": {"p50": _pct(proc_ms, 50), "p95": _pct(proc_ms, 95)},
            "detect_ms": {"p50": _pct(det_ms, 50), "p95": _pct(det_ms, 95)},
            "vo_ms": {"p50": _pct(vo_ms, 50), "p95": _pct(vo_ms, 95)},
            "tracker_mode": self.req.tracker.describe(),
            "tracker_stats": dict(self.tracker.stats.__dict__) if getattr(self, "tracker", None) else None,
            "reid": self.embedder.summary() if getattr(self, "embedder", None) is not None else None,
            "processed_fps": round((len(frame_rows) - 1) / active, 2) if active > 0 else None,
            "processed_fps_definition": "(frame rows - 1) / (run_t first->last frame row minus operator-paused seconds)",
            "paused_seconds": round(paused_s, 3),
            "run_seconds": round(time.monotonic() - self.t0, 3),
            "media_seconds": round(media_played, 3) if len(frame_rows) > 1 else None,
            "yaw": {"nonzero_rows": sum(1 for y in yaws if y), "min": min(yaws) if yaws else None,
                    "max": max(yaws) if yaws else None,
                    "mean_abs_nonzero": round(float(np.mean([abs(y) for y in yaws if y])), 2) if any(yaws) else None,
                    "source": "MOCK", "transmitted": False},
            "yaw_nonzero_while_not_locked_or_stop": bad_yaw,
            "target_state_counts": cnt("target_state", frame_rows),
            "input_state_counts": cnt("input_state", rows[1:]),
            "row_type_counts": cnt("type", rows),
            "stop_reason_counts": _reason_counts(rows),
            "epochs": self.epoch + 1,
            "vo": None if not req.vo else {
                "segments": sorted({r["vo_segment"] for r in frame_rows if r.get("vo_segment") is not None}),
                "state_counts": cnt("vo_state", frame_rows), "no_pose_spans": no_pose, "label": LABELS["vo"],
                "steps": vo.stats if vo is not None else None},
            "scene_changes": [{"run_t": e["run_t"], "media_t": e.get("media_t"), "reason": e["reason"]}
                              for e in self.events if e["type"] == "SHOT_CHANGE"],
            "events": self.events,
            "control_seq_applied": self.status.state.get("control_seq_applied", 0),
            "timeline_rows": tl.rows,
            "timeline_sha256": tl.sha.hexdigest(),
            "frames_manifest": self.manifest,
        })

    def _completeness(self, rows: list[dict], frame_rows: list[dict], captured: int, dropped_total: int) -> list[str]:
        """Row count, sha256 and every manifest frame, checked against the files on disk."""
        sd, tl, manifest = self.sd, self.tl, self.manifest
        problems = []
        _, nlines = read_timeline(tl.path)
        if nlines != tl.rows or len(rows) != tl.rows:
            problems.append(f"timeline lines {nlines} / parsed {len(rows)} != rows written {tl.rows}")
        if sha256_file(tl.path) != self.report["timeline_sha256"]:
            problems.append("timeline sha256 mismatch")
        if any(not (sd / m["name"]).is_file() or (sd / m["name"]).stat().st_size != m["size"] for m in manifest):
            problems.append("frame manifest mismatch")
        if len(manifest) != len(frame_rows):
            problems.append(f"frames_written {len(manifest)} != frame rows {len(frame_rows)}")
        if captured != len(frame_rows) + dropped_total:
            problems.append(f"captured {captured} != processed {len(frame_rows)} + dropped {dropped_total}")
        self.report["completeness"] = {"ok": not problems, "problems": problems}
        return problems


def _reason_counts(rows: list[dict]) -> dict:
    out: dict = {}
    for r in rows:
        for s in r.get("stop_reasons") or []:
            key = s.split("(")[0]
            out[key] = out.get(key, 0) + 1
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vision.live_run")
    ap.add_argument("source", nargs="?", help="video file or webcam:N")
    ap.add_argument("--session", default=None)
    ap.add_argument("--runs-root", default="outputs/runs")
    ap.add_argument("--rate", choices=["realtime", "max"], default="realtime")
    ap.add_argument("--model", default="yolox_nano")
    ap.add_argument("--score-thr", type=float, default=0.35)
    ap.add_argument("--fx", type=float)
    ap.add_argument("--calib", type=Path)
    ap.add_argument("--vo-width", type=int, default=640)
    ap.add_argument("--no-vo", action="store_true")
    ap.add_argument("--max-seconds", type=float)
    ap.add_argument("--verify", metavar="SESSION_DIR", help="only run the completeness check on a finished run")
    tracker_setup.add_args(ap)
    a = ap.parse_args(argv)
    if a.verify:
        res = verify_run(a.verify)
        print(json.dumps(res, indent=1))
        return 0 if res["state"] == "SUCCEEDED" else 1
    if not a.source:
        ap.error("source is required")
    sid = a.session or datetime.now().strftime("run-%Y%m%d-%H%M%S")
    req = RunRequest(a.source, Path(a.runs_root) / sid, a.rate, a.model, a.score_thr, a.max_seconds, not a.no_vo,
                     a.vo_width, a.fx, a.calib, tracker=tracker_setup.config_from_args(a))
    cancel = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: cancel.set())
    rep = run_live(req, cancel=cancel)
    print(json.dumps({k: rep.get(k) for k in ("session_id", "status", "end_reason", "counts", "processed_fps",
                                               "latency_ms", "yaw_nonzero_while_not_locked_or_stop")}, indent=1))
    return {"SUCCEEDED": 0, "CANCELLED": 130}.get(rep["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
