"""Local map console server: static UI + JSON API on 127.0.0.1 (stdlib only).

Builds run in a child process (`python -m mapping.cli build`), so SfM never blocks the UI. Job progress is read from
the session's status.json written by that process; the server only keeps the Popen handle needed to cancel it.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).resolve().parent / "static"
sys.path.insert(0, str(ROOT / "packages"))

from mapping.frame_source import IMAGE_SUFFIXES, VIDEO_SUFFIXES  # noqa: E402
from mapping.labels import DataOrigin, is_synthetic_input  # noqa: E402
from mapping import metric_scale  # noqa: E402
from mapping.export import PlyError, parse_ply_header  # noqa: E402
from control.virtual_replay import replay_control  # noqa: E402
from vision.person_detector import MODELS  # noqa: E402
from vision.live_run import verify_run  # noqa: E402

HARDWARE_ARMED = False  # this app never talks to a drone; kept explicit for the UI contract
DRY_RUN = True
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")  # no leading dot: blocks "." and ".."
SESSION_FILES = {"sparse.ply", "trajectory.json", "frames.json", "map_report.json", "log.txt", "evaluation.json",
                 "coverage.json", "shots.json", "sparse_filtered.ply", "diagnosis_segments.json",
                 "dense/dense_verified.ply", "dense/dense_report.json",
                 "dense_experimental/dense_verified.ply", "dense_experimental/dense_report.json",
                 "dense_static/dense_verified.ply", "dense_static/dense_report.json",
                 "dense_static_v2/dense_verified.ply", "dense_static_v2/dense_report.json",
                 "dense_static_v3/dense_verified.ply", "dense_static_v3/dense_report.json",
                 "dense_learned/dense_verified.ply", "dense_learned/dense_report.json",
                 "model_estimated/dense_verified.ply", "model_estimated/dense_report.json",
                 "dense_static_local/dense_verified.ply", "dense_static_local/dense_report.json",
                 "dense_zipstyle/dense_verified.ply", "dense_zipstyle/dense_report.json",
                 "dense_hybrid/dense_verified.ply", "dense_hybrid/dense_report.json",
                 "dense_zipdemo/dense_verified.ply", "dense_zipdemo/dense_report.json",
                 "dense_onpath/dense_verified.ply", "dense_onpath/dense_report.json", "scale.json"}
MODEL_FILE = re.compile(r"^models/\d{1,3}/(sparse\.ply|sparse_filtered\.ply|trajectory\.json|model_report\.json)$")
TRACK_FILES = {"tracks.json", "tracking_report.json"}
RUN_FILES = {"tracks.json", "run_report.json", "timeline.jsonl", "frames/latest.jpg"}
RUN_OPS = {"pause", "resume", "seek", "stop", "select", "clear"}
FRAME_FILE = re.compile(r"^frames/f\d{6}\.jpg$")
MAX_BODY = 65536
COORD_LIMIT = 1e6  # map coordinates beyond this are not points of a stored map
STALE_AFTER_S = 20.0  # a running build that has not updated status.json for this long is shown as stale
FINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED", "INCOMPLETE"}
FINISH_GRACE_S = 5.0  # a child that already wrote its final state gets this long to exit before it still counts as busy
CHUNK = 1 << 20  # file responses are streamed in 1 MiB pieces, never read whole into memory
MAX_POINTS_LIMIT = 20_000_000
DECIMATED_CACHE = 4  # decimated PLY bodies kept in memory (a few layers x one size each)
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _rel(path: Path) -> str:
    """Stable input id: project-relative when possible (ids are matched against the server's own list)."""
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None  # missing or being replaced; caller decides what that means


@dataclass(frozen=True)
class JobKind:
    """A family of jobs that share the session/status.json/report pattern: map builds and tracking runs."""
    name: str
    root: Path
    report_file: str
    files: frozenset


class Console:
    def __init__(self, maps_root: Path, input_roots: dict[Path, DataOrigin], tracks_root: Path | None = None,
                 runs_root: Path | None = None):
        self.maps_root = maps_root
        self.input_roots = input_roots
        self.kinds = {"maps": JobKind("maps", maps_root, "map_report.json", frozenset(SESSION_FILES)),
                      "tracks": JobKind("tracks", tracks_root or maps_root.parent / "tracks", "tracking_report.json",
                                        frozenset(TRACK_FILES)),
                      "runs": JobKind("runs", runs_root or maps_root.parent / "runs", "run_report.json", frozenset(RUN_FILES))}
        self._verify_cache: dict[str, tuple[float, dict]] = {}
        self._control_seq: dict[str, int] = {}
        self._procs: dict[tuple[str, str], subprocess.Popen] = {}
        self._ply_cache: OrderedDict[tuple, tuple[bytes, int, int]] = OrderedDict()
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()  # serialises check-then-spawn so two requests cannot both start
        self._scale_lock = threading.Lock()  # scale.json is read-modify-write; one update at a time

    # ---------- inputs ----------
    def inputs(self) -> list[dict]:
        out = []
        for root, origin in self.input_roots.items():
            if not root.is_dir():
                continue
            for p in sorted(root.iterdir()):
                if p.name.startswith("."):
                    continue
                item = self._describe_input(p, origin)
                if item:
                    out.append(item)
        return out

    def _describe_input(self, p: Path, origin: DataOrigin) -> dict | None:
        if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES:
            kind, target = "video", p
        elif p.is_dir() and (vids := sorted(f for f in p.iterdir() if f.suffix.lower() in VIDEO_SUFFIXES
                                            and not f.name.startswith("."))):
            kind, target = "video", vids[0]  # a dataset folder holding one clip plus its SOURCE.md
        elif p.is_dir():
            target = next((d for d in [p, *sorted(p.iterdir())] if d.is_dir() and (
                (d / "rgb").is_dir() or any(f.suffix.lower() in IMAGE_SUFFIXES for f in d.iterdir()))), None)
            if target is None:
                return None
            kind = "images"
        else:
            return None
        if is_synthetic_input(target):
            origin = DataOrigin.SYNTHETIC
        cams = [c for c in (p / "camera_opencv4.json", p / "camera.json", target / "camera.json") if c.is_file()]
        return {"id": _rel(target), "label": p.name, "kind": kind, "origin": origin.value,
                "camera_json": _rel(cams[0]) if cams else None}

    # ---------- sessions ----------
    def session_dir(self, sid: str, kind: str = "maps") -> Path:
        if not SESSION_ID.match(sid):
            raise KeyError(sid)
        return self.kinds[kind].root / sid

    def session_state(self, sid: str, kind: str = "maps") -> dict:
        sd = self.session_dir(sid, kind)
        status = _read_json(sd / "status.json")
        report = _read_json(sd / self.kinds[kind].report_file)
        if status is None and report is None:
            if not (sd / "process.log").exists():
                raise KeyError(sid)
            with self._lock:  # child started but has not written status.json yet (or died first)
                proc = self._procs.get((kind, sid))
            if proc and proc.poll() is None:
                return {"id": sid, "state": "STARTING", "status": None, "evaluation": None}
            tail = (sd / "process.log").read_text(errors="replace").strip().splitlines()[-1:] or ["no output"]
            return {"id": sid, "state": "INTERRUPTED", "status": {"message": tail[0]}, "evaluation": None}
        state = (status or {}).get("state") or report.get("status")
        if kind == "runs" and state != "RUNNING":
            # a finished live run counts as complete only if its files check out (rows, sha256, frames manifest)
            key = (sd / "run_report.json").stat().st_mtime if (sd / "run_report.json").exists() else 0.0
            cached = self._verify_cache.get(sid)
            if not cached or cached[0] != key:
                cached = (key, verify_run(sd))
                self._verify_cache[sid] = cached
            state = cached[1]["state"]
            status = {**(status or {}), "verify": cached[1]}
        info = {"id": sid, "state": state, "status": status}
        if kind == "maps":
            info["evaluation"] = _read_json(sd / "evaluation.json")
            if state == "SUCCEEDED":
                info["metric_scale"] = metric_scale.summary(metric_scale.current_scale(sd))
        if state == "RUNNING":
            with self._lock:
                proc = self._procs.get((kind, sid))
            alive = proc.poll() is None if proc else _pid_alive(status.get("pid"))
            age = time.time() - datetime.fromisoformat(status["updated_at"]).timestamp()
            info["status_age_s"] = round(age, 1)
            if not alive:
                info["state"] = "INTERRUPTED"  # process gone without writing a final state (crash / server restart)
            elif age > STALE_AFTER_S:
                info["stale"] = True
        if report and info["state"] != "RUNNING":
            info["report"] = report
        return info

    def sessions(self, kind: str = "maps") -> list[dict]:
        root = self.kinds[kind].root
        if not root.is_dir():
            return []
        out = []
        for d in sorted(root.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True):
            try:
                s = self.session_state(d.name, kind)
            except KeyError:
                continue
            rep = s.get("report") or {}
            if kind == "runs":
                out.append({"id": d.name, "state": s["state"], "kind": "runs", "labels": rep.get("labels"),
                            "model": (rep.get("detector") or {}).get("name"),
                            "frames": (rep.get("counts") or {}).get("processed"),
                            "tracks_confirmed": None, "end_reason": rep.get("end_reason"),
                            "created_at": (s.get("status") or {}).get("started_at")})
                continue
            if kind == "tracks":
                out.append({"id": d.name, "state": s["state"], "labels": rep.get("labels"),
                            "model": (rep.get("detector") or {}).get("name"), "frames": rep.get("frames_processed"),
                            "tracks_confirmed": (rep.get("tracks") or {}).get("tracks_confirmed"),
                            "created_at": rep.get("created_at") or (s.get("status") or {}).get("started_at")})
                continue
            q = rep.get("quality") or {}
            out.append({"id": d.name, "state": s["state"], "labels": rep.get("labels"),
                        "quality": q.get("verdict") or ("UNRATED" if s["state"] == "SUCCEEDED" else None),
                        "num_models": (rep.get("reconstruction") or {}).get("num_models"),
                        "registered": (rep.get("reconstruction") or {}).get("registered_images"),
                        "points3D": (rep.get("reconstruction") or {}).get("points3D"),
                        "created_at": rep.get("created_at") or (s.get("status") or {}).get("started_at")})
        return out

    # ---------- jobs ----------
    def running_job(self, kind: str = "maps") -> str | None:
        with self._lock:
            live = []
            for (k, sid), p in list(self._procs.items()):
                if p.poll() is not None:
                    del self._procs[(k, sid)]  # reap finished children
                elif k == kind:
                    live.append((sid, p))
        for sid, p in live:
            if not self._finishing(kind, sid, p):
                return sid
        return None

    def _finishing(self, kind: str, sid: str, p: subprocess.Popen) -> bool:
        """True when a child has already published its final state and exits within FINISH_GRACE_S.

        A child writes its last status.json and then still closes files and tears down the interpreter; without this
        the UI shows SUCCEEDED while a new job is refused with 409 for that moment."""
        state = (_read_json(self.kinds[kind].root / sid / "status.json") or {}).get("state")
        if state not in FINAL_STATES:
            return False
        try:
            p.wait(timeout=FINISH_GRACE_S)
        except subprocess.TimeoutExpired:
            return False  # wrote a final state but hangs: still holds the machine, keep refusing
        with self._lock:
            self._procs.pop((kind, sid), None)
        return True

    def _input(self, body: dict) -> dict:
        item = next((i for i in self.inputs() if i["id"] == body.get("input_id")), None)
        if item is None:
            raise ValueError("Unknown input. Pick one from the input list (files under assets/input or data/public).")
        return item

    def _new_session(self, kind: str, body: dict, item: dict) -> str:
        root = self.kinds[kind].root
        sid = body.get("session")
        if not sid:  # auto id: second resolution, so two quick runs get -2, -3, ... instead of colliding
            base = f"{re.sub(r'[^A-Za-z0-9_-]', '_', item['label'])[:40]}-{datetime.now():%Y%m%d-%H%M%S}"
            sid, n = base, 1
            while (root / sid).exists():
                n += 1
                sid = f"{base}-{n}"
        if not SESSION_ID.match(sid) or (root / sid).exists():
            raise ValueError(f"Invalid or existing session id: {sid}")
        (root / sid).mkdir(parents=True)
        return sid

    def _spawn(self, kind: str, sid: str, cmd: list[str]) -> None:
        sd = self.kinds[kind].root / sid
        log = open(sd / "process.log", "wb")
        env = dict(os.environ, PYTHONPATH=str(ROOT / "packages"))
        with self._lock:
            self._procs[(kind, sid)] = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                                        start_new_session=True)
        log.close()

    def start_job(self, body: dict) -> str:
        with self._start_lock:
            return self._start_job(body)

    def _start_job(self, body: dict) -> str:
        if self.running_job() or self.running_job("tracks") or self.running_job("runs"):
            raise RuntimeError("Another job is running (map build, tracking or live run). Cancel it or wait for it to finish.")
        item = self._input(body)
        fps = float(body.get("fps", 3.0))
        max_frames = int(body.get("max_frames", 150))
        if not (0.5 <= fps <= 30 and 3 <= max_frames <= 600):
            raise ValueError("fps must be 0.5–30 and max_frames 3–600")
        sid = self._new_session("maps", body, item)
        cmd = [sys.executable, "-m", "mapping.cli", "build", str(ROOT / item["id"]), "--session", sid,
               "--maps-root", str(self.maps_root), "--origin", item["origin"], "--fps", str(fps),
               "--max-frames", str(max_frames)]
        if body.get("use_calibration", True) and item["camera_json"]:
            cmd += ["--camera-json", str(ROOT / item["camera_json"])]
        self._spawn("maps", sid, cmd)
        return sid

    def start_track_job(self, body: dict) -> str:
        with self._start_lock:
            # One job at a time across both kinds: COLMAP and the detector would otherwise skew each other's timings.
            if self.running_job("tracks") or self.running_job() or self.running_job("runs"):
                raise RuntimeError("Another job is running (map build, tracking or live run). Cancel it or wait for it to finish.")
            item = self._input(body)
            model = body.get("model", "yolox_nano")
            fps = float(body.get("fps", 10.0))
            max_frames = int(body.get("max_frames", 600))
            if model not in MODELS:
                raise ValueError(f"Unknown model; choose one of {sorted(MODELS)}")
            if not (1 <= fps <= 30 and 10 <= max_frames <= 3000):
                raise ValueError("fps must be 1–30 and max_frames 10–3000")
            sid = self._new_session("tracks", body, item)
            self._spawn("tracks", sid, [sys.executable, "-m", "vision.track_video", str(ROOT / item["id"]),
                                        "--session", sid, "--tracks-root", str(self.kinds["tracks"].root),
                                        "--origin", item["origin"], "--model", model, "--fps", str(fps),
                                        "--max-frames", str(max_frames)])
            return sid

    def start_run_job(self, body: dict) -> str:
        with self._start_lock:
            if self.running_job() or self.running_job("tracks") or self.running_job("runs"):
                raise RuntimeError("Another job is running (map build, tracking or live run). Stop it or wait.")
            src = str(body.get("input_id") or "")
            if re.fullmatch(r"webcam:\d{1,2}", src):
                item, target = {"label": src.replace(":", "_"), "origin": "REAL_VIDEO"}, src
            else:
                item = self._input(body)
                if item["kind"] != "video":
                    raise ValueError("Live runs need a video file (or webcam:N)")
                target = str(ROOT / item["id"])
            rate = body.get("rate", "realtime")
            if rate not in ("realtime", "max"):
                raise ValueError("rate must be realtime or max")
            sid = self._new_session("runs", body, item)
            cmd = [sys.executable, "-m", "vision.live_run", target, "--session", sid,
                   "--runs-root", str(self.kinds["runs"].root), "--rate", rate]
            if body.get("max_seconds"):
                cmd += ["--max-seconds", str(float(body["max_seconds"]))]
            self._spawn("runs", sid, cmd)
            self._control_seq[sid] = 0
            return sid

    def add_scale(self, sid: str, body: dict) -> dict:
        """A two-point tape length picked in the viewer: becomes a scale observation (role fit) or an independent
        check (role verify). Judgement happens in mapping.metric_scale; this only validates the request."""
        sd = self.session_dir(sid)
        if not sd.is_dir():
            raise KeyError(sid)
        if self.session_state(sid)["state"] != "SUCCEEDED" or not (sd / "trajectory.json").is_file():
            raise RuntimeError("Only a finished map can get a scale")
        if body.get("method", "TWO_POINT_TAPE") != "TWO_POINT_TAPE":
            raise ValueError("method must be TWO_POINT_TAPE")
        if body.get("model_rank", 0) != 0:
            raise ValueError("only model rank 0 (the largest model) can get a scale")
        role, note = body.get("role"), body.get("note", "")
        if role not in ("fit", "verify"):
            raise ValueError("role must be fit or verify")
        if not isinstance(note, str) or len(note) > 80:
            raise ValueError("note must be a string of at most 80 characters")

        def num(v, lo, hi, what):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi):
                raise ValueError(f"{what} must be a number in [{lo:g}, {hi:g}]")
            return float(v)
        pts = []
        for k in ("p1", "p2"):
            v = body.get(k)
            if not isinstance(v, list) or len(v) != 3:
                raise ValueError(f"{k} must be [x, y, z]")
            pts.append([num(x, -COORD_LIMIT, COORD_LIMIT, k) for x in v])
        true_m = num(body.get("true_m"), 1e-3, 1e4, "true_m")
        tol = num(body.get("pick_tolerance_units", 0.0), 0.0, COORD_LIMIT, "pick_tolerance_units")
        with self._scale_lock:
            s = metric_scale.add_two_point(sd, pts[0], pts[1], true_m, role, note, tol, command=f"POST /api/sessions/{sid}/scale")
        return metric_scale.summary(s)

    def run_control(self, sid: str, body: dict) -> dict:
        """Queue a control command for a running live run (atomic control.json with increasing seq)."""
        op = body.get("op")
        if op not in RUN_OPS:
            raise ValueError(f"op must be one of {sorted(RUN_OPS)}")
        sd = self.session_dir(sid, "runs")
        if not sd.is_dir():
            raise KeyError(sid)
        with self._lock:
            proc = self._procs.get(("runs", sid))
        status = _read_json(sd / "status.json") or {}
        if proc is None or proc.poll() is not None or status.get("state") in FINAL_STATES:
            raise RuntimeError("This run is not running (controls only apply to a live run)")
        applied = int(status.get("control_seq_applied") or 0)
        seq = max(self._control_seq.get(sid, 0), applied) + 1
        self._control_seq[sid] = seq
        args = body.get("args") if isinstance(body.get("args"), dict) else {}
        prev = _read_json(sd / "control.json") or {}
        queue = [c for c in prev.get("queue", []) if c.get("seq", 0) > applied] + [{"seq": seq, "op": op, "args": args}]
        tmp = sd / ".control.json.tmp"
        tmp.write_text(json.dumps({"queue": queue}))
        tmp.replace(sd / "control.json")
        return {"seq": seq, "queued": len(queue), "applied_so_far": applied}

    def run_latest(self, sid: str) -> dict:
        """Status plus the newest frame row of the timeline (read from the file tail, never the whole file)."""
        sd = self.session_dir(sid, "runs")
        st = self.session_state(sid, "runs")
        row = None
        tl = sd / "timeline.jsonl"
        if tl.exists():
            with open(tl, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 262144))
                for line in reversed(fh.read().splitlines()):
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if r.get("type") == "frame":
                        row = r
                        break
        header = None
        if tl.exists():
            with open(tl) as fh:
                try:
                    header = json.loads(fh.readline())
                except json.JSONDecodeError:
                    header = None
        return {"state": st, "row": row, "image_wh": (header or {}).get("image_wh"),
                "labels": (header or {}).get("labels"), "server_time": time.time()}

    def run_vo_path(self, sid: str) -> dict:
        """Camera positions of the newest VO segment of the newest epoch (relative scale; not a map frame)."""
        sd = self.session_dir(sid, "runs")
        rows = []
        tl = sd / "timeline.jsonl"
        if tl.exists():
            for line in tl.read_text().splitlines():
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("type") == "frame":
                    rows.append(r)
        if not rows:
            return {"segment": None, "points": []}
        last = rows[-1]
        seg = [r for r in rows if r.get("epoch") == last.get("epoch") and r.get("vo_segment") == last.get("vo_segment")
               and r.get("T_world_camera")]
        pts = [[r["run_t"], r["T_world_camera"][0][3], r["T_world_camera"][1][3], r["T_world_camera"][2][3], r.get("vo_state")]
               for r in seg[:: max(1, len(seg) // 600)]]
        return {"epoch": last.get("epoch"), "segment": last.get("vo_segment"), "points": pts,
                "label": "VO path of the current segment: relative scale, no loop closure, not registered to any map"}

    def control_replay(self, sid: str, body: dict, kind: str = "tracks") -> dict:
        state = self.session_state(sid, kind)
        if state["state"] != "SUCCEEDED":
            raise RuntimeError(f"Tracking run {sid} is {state['state']}; only finished runs can be replayed.")
        rec = _read_json(self.session_dir(sid, kind) / "tracks.json")
        if rec is None:
            raise RuntimeError("tracks.json is missing or unreadable")
        try:
            target_id, select_k = int(body["target_id"]), int(body.get("select_k", 0))
        except (KeyError, TypeError, ValueError):
            raise ValueError("expected {target_id: int, select_k: int}")
        return replay_control(rec["frames"], int(rec["image_wh"][0]), target_id, select_k)

    def cancel_job(self, sid: str, kind: str = "maps") -> bool:
        with self._lock:
            p = self._procs.get((kind, sid))
        if p is None or p.poll() is not None or self._finishing(kind, sid, p):
            return False  # nothing to cancel: never started, exited, or already wrote its final state
        p.send_signal(signal.SIGTERM)  # pipeline turns this into a COLMAP cancellation + CANCELLED status
        return True

    def decimated_ply(self, path: Path, max_points: int) -> tuple[bytes, int, int] | None:
        """At most `max_points` vertices of a PLY: every (n / max_points)-th point in file order (deterministic, so
        the same request always returns the same points). None when the file already fits (serve it as is).
        Returns (body, points served, points in the file). Only the sampled rows are read from disk."""
        st = path.stat()
        with open(path, "rb") as fh:
            head = fh.read(65536)
        count, dtype, body_at = parse_ply_header(head, path.name)
        if st.st_size - body_at != count * dtype.itemsize:
            raise PlyError(f"{path.name}: vertex data size does not match the header")
        if count <= max_points:
            return None
        key = (str(path), st.st_mtime_ns, st.st_size, max_points)
        with self._lock:
            if key in self._ply_cache:
                self._ply_cache.move_to_end(key)
                return self._ply_cache[key]
        idx = (np.arange(max_points, dtype=np.int64) * count) // max_points
        rows = np.empty(max_points, dtype)
        block = max(1, 16 * CHUNK // dtype.itemsize)  # read ~16 MiB at a time: memory stays near the output size
        with open(path, "rb") as fh:
            fh.seek(body_at)
            for b0 in range(0, count, block):
                b1 = min(count, b0 + block)
                part = np.frombuffer(fh.read((b1 - b0) * dtype.itemsize), dtype)
                lo, hi = np.searchsorted(idx, [b0, b1])
                rows[lo:hi] = part[idx[lo:hi] - b0]
        lines = head[:body_at].decode("ascii").splitlines()
        lines = [f"element vertex {max_points}" if ln.startswith("element vertex ") else ln for ln in lines]
        lines.insert(2, f"comment decimated: {max_points} of {count} points, every {count / max_points:.3g}-th in file order")
        out = ("\n".join(lines) + "\n").encode("ascii") + rows.tobytes()
        with self._lock:
            self._ply_cache[key] = (out, max_points, count)
            while len(self._ply_cache) > DECIMATED_CACHE:
                self._ply_cache.popitem(last=False)
        return out, max_points, count

    def shutdown(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


class Handler(BaseHTTPRequestHandler):
    console: Console  # set by make_server
    server_version = "Skywarden/1"

    def log_request(self, code="-", size="-"):  # log only failures; polling would flood the terminal
        try:
            failed = int(code) >= 400
        except (TypeError, ValueError):
            failed = True
        if failed:
            super().log_request(code, size)

    def _headers(self, code: int, ctype: str, length: int, cache: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self._headers_sent = True

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store", extra: dict | None = None) -> None:
        self._headers(code, ctype, len(body), cache, extra)
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message}, code)

    def _file(self, path: Path, cache: str = "no-store") -> None:
        """Streams a file in CHUNK pieces. Supports one `Range: bytes=a-b` and, for PLY files, `?max_points=N`
        (a deterministic subsample, see Console.decimated_ply)."""
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".ply":
            ctype = "application/octet-stream"
            q = parse_qs(urlparse(self.path).query).get("max_points")
            if q:
                if not re.fullmatch(r"\d{1,9}", q[0]) or not 1 <= int(q[0]) <= MAX_POINTS_LIMIT:
                    return self._error(400, f"max_points must be an integer in [1, {MAX_POINTS_LIMIT}]")
                try:
                    dec = self.console.decimated_ply(path, int(q[0]))
                except FileNotFoundError:
                    return self._error(404, f"{path.name} not found")
                except PlyError as e:
                    return self._error(422, str(e))
                if dec is not None:
                    body, served, total = dec
                    return self._send(200, body, ctype, cache, {"X-Points-Total": str(total), "X-Points-Served": str(served)})
        try:
            fh = open(path, "rb")
        except (FileNotFoundError, IsADirectoryError):
            return self._error(404, f"{path.name} not found")
        with fh:
            size = os.fstat(fh.fileno()).st_size
            start, end, code = 0, size - 1, 200
            extra = {"Accept-Ranges": "bytes"}
            rng = self.headers.get("Range")
            if rng:
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
                if not m or m.groups() == ("", ""):
                    return self._error(416, "only a single bytes=a-b range is supported")
                a, b = m.groups()
                if a == "":  # suffix range: the last b bytes
                    start, end = max(0, size - int(b)), size - 1
                else:
                    start, end = int(a), min(int(b), size - 1) if b else size - 1
                if start >= size or start > end:
                    return self._send(416, b"", ctype, cache, {"Content-Range": f"bytes */{size}"})
                code = 206
                extra["Content-Range"] = f"bytes {start}-{end}/{size}"
            if path.suffix == ".ply":
                extra["X-Points-Served"] = "all"
            self._headers(code, ctype, end - start + 1, cache, extra)
            fh.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    buf = fh.read(min(CHUNK, left))
                    if not buf:
                        break  # file shrank while sending; the client sees a short body
                    self.wfile.write(buf)
                    left -= len(buf)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True  # the browser dropped the request (switched layer); nothing to report

    def _local_request(self, post: bool) -> str | None:
        """None if the request may proceed, else the reason for a 403/415.

        Host must name this console (127.0.0.1 / localhost / [::1] on our port): blocks DNS rebinding, where another
        site's name resolves to 127.0.0.1. POSTs also need a local Origin (when a browser sends one) and a JSON body:
        a cross-site page cannot send application/json without a preflight, which this server never grants."""
        port = self.server.server_address[1]
        allowed = {f"{h}:{port}" for h in LOCAL_HOSTS}
        if (self.headers.get("Host") or "").lower() not in allowed:
            return "host"
        if post:
            origin = self.headers.get("Origin")
            if origin is not None and origin.lower() not in {f"http://{h}" for h in allowed}:
                return "origin"
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return "content-type"
        return None

    def do_GET(self):
        self._guarded(self._get, post=False)

    def do_POST(self):
        self._guarded(self._post, post=True)

    def _guarded(self, fn, post: bool):
        self._headers_sent = False
        why = self._local_request(post)
        if why == "content-type":
            return self._error(415, "POST bodies must be sent as Content-Type: application/json")
        if why:
            return self._error(403, f"request refused: {why} is not this local console")
        try:
            fn()
        except Exception as e:  # never drop the connection silently; the UI shows the message
            self.log_error("unhandled %s: %s", type(e).__name__, e)
            if self._headers_sent:
                self.close_connection = True  # a status line is already out; the client sees a short body
                return
            self._error(500, f"internal error: {type(e).__name__}: {e}")

    def _get(self):
        path = urlparse(self.path).path
        c = self.console
        try:
            if path in ("/", "/index.html"):
                return self._file(STATIC / "index.html")
            if path.startswith("/static/"):
                rel = path[len("/static/"):]
                target = (STATIC / rel).resolve()
                if STATIC not in target.parents:
                    return self._error(404, "not found")
                return self._file(target, "no-cache")
            if path == "/api/health":
                return self._json({"ok": True, "hardware_armed": HARDWARE_ARMED, "dry_run": DRY_RUN,
                                   "server_time": datetime.now(timezone.utc).isoformat(), "running_job": c.running_job(),
                                   "running_track_job": c.running_job("tracks"), "running_run_job": c.running_job("runs")})
            if path == "/api/models":
                return self._json([{"name": m.name, "license": m.license, "input_size": m.input_size,
                                    "available": (ROOT / "data/models/yolox" / m.file).is_file()} for m in MODELS.values()])
            if path == "/api/tracks":
                return self._json(c.sessions("tracks"))
            if path == "/api/runs":
                return self._json(c.sessions("runs"))
            m = re.match(r"^/api/runs/([^/]+)$", path)
            if m:
                return self._json(c.session_state(m.group(1), "runs"))
            m = re.match(r"^/api/runs/([^/]+)/latest$", path)
            if m:
                return self._json(c.run_latest(m.group(1)))
            m = re.match(r"^/api/runs/([^/]+)/vo$", path)
            if m:
                return self._json(c.run_vo_path(m.group(1)))
            m = re.match(r"^/api/runs/([^/]+)/files/(.+)$", path)
            if m:
                sid, name = m.groups()
                if name not in RUN_FILES and not FRAME_FILE.match(name):
                    return self._error(404, "file not served")
                return self._file(c.session_dir(sid, "runs") / name, "no-store" if name == "frames/latest.jpg" or not name.startswith("frames/") else "private, max-age=3600")
            m = re.match(r"^/api/tracks/([^/]+)$", path)
            if m:
                return self._json(c.session_state(m.group(1), "tracks"))
            m = re.match(r"^/api/tracks/([^/]+)/files/(.+)$", path)
            if m:
                sid, name = m.groups()
                if name not in TRACK_FILES and not FRAME_FILE.match(name):
                    return self._error(404, "file not served")
                return self._file(c.session_dir(sid, "tracks") / name, "private, max-age=3600" if name.startswith("frames/") else "no-store")
            if path == "/api/inputs":
                return self._json(c.inputs())
            if path == "/api/sessions":
                return self._json(c.sessions())
            m = re.match(r"^/api/sessions/([^/]+)$", path)
            if m:
                return self._json(c.session_state(m.group(1)))
            m = re.match(r"^/api/sessions/([^/]+)/files/(.+)$", path)
            if m:
                sid, name = m.groups()
                if name not in SESSION_FILES and not FRAME_FILE.match(name) and not MODEL_FILE.match(name):
                    return self._error(404, "file not served")
                return self._file(c.session_dir(sid) / name, "private, max-age=3600" if name.startswith("frames/") else "no-store")
            return self._error(404, "not found")
        except KeyError:
            return self._error(404, "unknown session")

    def _post(self):
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > MAX_BODY:  # a negative length would make read() wait for the client to close
                return self._error(400, "expected a small JSON object")
            body = json.loads(self.rfile.read(n) or b"{}")
            if body is None or not isinstance(body, dict):
                return self._error(400, "expected a small JSON object")
            if path == "/api/jobs":
                sid = self.console.start_job(body)
                return self._json({"session": sid}, 202)
            m = re.match(r"^/api/jobs/([^/]+)/cancel$", path)
            if m:
                ok = self.console.cancel_job(m.group(1))
                return self._json({"cancelled": ok}, 202 if ok else 409)
            m = re.match(r"^/api/sessions/([^/]+)/scale$", path)
            if m:
                return self._json(self.console.add_scale(m.group(1), body))
            if path == "/api/track-jobs":
                return self._json({"session": self.console.start_track_job(body)}, 202)
            m = re.match(r"^/api/track-jobs/([^/]+)/cancel$", path)
            if m:
                ok = self.console.cancel_job(m.group(1), "tracks")
                return self._json({"cancelled": ok}, 202 if ok else 409)
            m = re.match(r"^/api/tracks/([^/]+)/control$", path)
            if m:
                return self._json(self.console.control_replay(m.group(1), body))
            if path == "/api/run-jobs":
                return self._json({"session": self.console.start_run_job(body)}, 202)
            m = re.match(r"^/api/runs/([^/]+)/control$", path)
            if m:
                return self._json(self.console.run_control(m.group(1), body), 202)
            m = re.match(r"^/api/runs/([^/]+)/replay$", path)
            if m:
                return self._json(self.console.control_replay(m.group(1), body, "runs"))
            m = re.match(r"^/api/run-jobs/([^/]+)/cancel$", path)
            if m:
                ok = self.console.cancel_job(m.group(1), "runs")
                return self._json({"cancelled": ok}, 202 if ok else 409)
            return self._error(404, "not found")
        except KeyError:
            return self._error(404, "unknown session")
        except json.JSONDecodeError:
            return self._error(400, "invalid JSON")
        except ValueError as e:
            return self._error(400, str(e))
        except RuntimeError as e:
            return self._error(409, str(e))


def make_server(host: str, port: int, maps_root: Path, input_roots: dict[Path, DataOrigin] | None = None,
                tracks_root: Path | None = None, runs_root: Path | None = None):
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Refusing to bind beyond localhost; this console has no authentication.")
    roots = input_roots or {ROOT / "assets" / "input": DataOrigin.REAL_VIDEO,
                            ROOT / "data" / "public": DataOrigin.PUBLIC_DATASET,
                            ROOT / "outputs" / "synthetic": DataOrigin.SYNTHETIC}
    handler = type("BoundHandler", (Handler,), {"console": Console(maps_root, roots, tracks_root, runs_root)})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--maps-root", default=str(ROOT / "outputs" / "maps"))
    ap.add_argument("--tracks-root", default=str(ROOT / "outputs" / "tracks"))
    ap.add_argument("--runs-root", default=str(ROOT / "outputs" / "runs"))
    a = ap.parse_args()
    try:
        httpd = make_server(a.host, a.port, Path(a.maps_root), tracks_root=Path(a.tracks_root), runs_root=Path(a.runs_root))
    except OSError as e:
        raise SystemExit(f"Cannot listen on {a.host}:{a.port} ({e.strerror}). Another program uses this port; "
                         f"run scripts/run_demo.sh --port <free port>.")
    print(f"Map console: http://{a.host}:{httpd.server_address[1]}/  (HARDWARE_ARMED={HARDWARE_ARMED}, DRY_RUN={DRY_RUN})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.RequestHandlerClass.console.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
