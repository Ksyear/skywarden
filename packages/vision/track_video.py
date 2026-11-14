"""Offline person-tracking run: video/image folder -> real detector -> MultiTracker, recorded per frame.

python -m vision.track_video <input> --session X [--tracks-root outputs/tracks] [--model yolox_nano] [--fps 10]

Writes into <tracks-root>/<session>/:
  status.json            progress for the UI (atomic rewrite, same shape as map builds)
  frames/f000000.jpg     processed frames (long side <= 960 px) so the UI can replay with overlays
  tracks.json            per-frame detections + track states, boxes in ORIGINAL image pixels
  tracking_report.json   labels, model provenance, latency p50/p95, track statistics, MOTA/IDF1 = null (no GT)
This is a recording of a camera clip, not a live feed, and never produces drone commands.
"""
from __future__ import annotations

import argparse
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

import cv2
import numpy as np

from mapping.frame_source import InputError, open_source
from mapping.labels import DataOrigin
from tracking.tracker import MultiTracker, TrackerConfig, TrackState

from .person_detector import ModelUnavailable, make_detector

STAGES = ["LOAD_MODEL", "DETECT_TRACK", "EXPORT"]
JPEG_LONG_SIDE = 960


class Cancelled(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _atomic_json(path: Path, obj: dict, compact: bool = False) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")) if compact else json.dumps(obj, indent=1))
    tmp.replace(path)


class StatusWriter:
    def __init__(self, session_dir: Path):
        self.path = session_dir / "status.json"
        self.state = {"state": "RUNNING", "kind": "tracking", "stage": None, "stage_index": -1, "stages": STAGES,
                      "progress": None, "message": "starting", "pid": os.getpid(), "started_at": _now(),
                      "updated_at": _now()}
        self.write()

    def write(self, **changes) -> None:
        self.state.update(changes, updated_at=_now())
        _atomic_json(self.path, self.state)

    def stage(self, name: str, message: str) -> None:
        self.write(stage=name, stage_index=STAGES.index(name), progress=None, message=message)


@dataclass
class TrackRequest:
    input_path: Path
    session_dir: Path
    origin: DataOrigin = DataOrigin.UNSPECIFIED
    model: str = "yolox_nano"
    target_fps: float = 10.0
    max_frames: int = 600
    score_thr: float = 0.35
    tracker: TrackerConfig = field(default_factory=TrackerConfig)


def _pct(xs: list[float], p: float) -> float | None:
    return round(float(np.percentile(xs, p)), 2) if xs else None


def _track_stats(frames: list[dict], duration_s: float) -> dict:
    """Statistics we can state without ground truth. Short confirmed tracks are a proxy for fragmentation."""
    life: dict[int, dict] = {}
    for f in frames:
        for tr in f["tracks"]:
            e = life.setdefault(tr["id"], {"first": f["t"], "last": f["t"], "locked": 0, "predicting": 0})
            e["last"] = f["t"]
            e["locked"] += tr["state"] == "LOCKED"
            e["predicting"] += tr["state"] == "PREDICTING"
    confirmed = {k: v for k, v in life.items() if v["locked"] > 0}
    spans = [v["last"] - v["first"] for v in confirmed.values()]
    return {
        "track_ids_created": len(life),
        "tracks_confirmed": len(confirmed),
        "tracks_never_confirmed": len(life) - len(confirmed),
        "confirmed_track_span_s": {"p50": _pct(spans, 50), "max": round(max(spans), 2) if spans else None},
        "confirmed_tracks_shorter_than_1s": sum(1 for s in spans if s < 1.0),
        "frames_with_predicting_track": sum(1 for f in frames if any(t["state"] == "PREDICTING" for t in f["tracks"])),
        "frames_with_ambiguous_track": sum(1 for f in frames if any(t["state"] == "AMBIGUOUS" for t in f["tracks"])),
        "mean_locked_tracks_per_frame": round(float(np.mean([sum(t["state"] == "LOCKED" for t in f["tracks"]) for f in frames])), 2) if frames else None,
        "new_ids_per_minute": round(len(life) / duration_s * 60, 1) if duration_s > 0 else None,
    }


def run_tracking(req: TrackRequest, cancel: threading.Event | None = None) -> dict:
    cancel = cancel or threading.Event()
    sd = req.session_dir
    (sd / "frames").mkdir(parents=True, exist_ok=True)
    for old in (sd / "frames").glob("f*.jpg"):  # a rerun into the same folder must not mix old frames in
        old.unlink()
    status = StatusWriter(sd)
    t_start = time.monotonic()
    report: dict = {"session_id": sd.name, "kind": "tracking", "status": "RUNNING", "created_at": _now(),
                    "input": {"path": str(req.input_path)},
                    "labels": {"detector_source": "REAL_MODEL", "data_origin": req.origin.value,
                               "playback": "RECORDED_CLIP", "control": "NONE (commands are computed only in the UI replay, MOCK)"},
                    "settings": {"target_fps": req.target_fps, "max_frames": req.max_frames, "score_thr": req.score_thr,
                                 "tracker": req.tracker.__dict__}}
    frames: list[dict] = []
    image_wh = None
    det_ms: list[float] = []
    trk_ms: list[float] = []
    try:
        status.stage("LOAD_MODEL", f"loading {req.model} (verifying sha256)")
        det = make_detector(req.model, score_thr=req.score_thr)
        report["detector"] = det.info()
        src = open_source(req.input_path)
        nominal = src.nominal_fps()
        step = max(1, int(round(nominal / req.target_fps))) if nominal else 1
        total = src.frame_count()
        expected = min(req.max_frames, (total + step - 1) // step) if total else req.max_frames
        report["input"].update({"kind": src.kind, "nominal_fps": nominal, "frames_total": total, "step": step})

        status.stage("DETECT_TRACK", f"detecting people in {expected} frames ({req.model}, CPU)")
        tracker = MultiTracker(req.tracker)
        t_first = None
        for k, fr in enumerate(src.frames(step)):
            if k >= req.max_frames:
                break
            if cancel.is_set():
                raise Cancelled("cancelled by user")
            ts = fr.timestamp_s if fr.timestamp_s is not None else fr.index / (nominal or req.target_fps)
            t_first = ts if t_first is None else t_first
            t = round(ts - t_first, 4)
            h, w = fr.image.shape[:2]
            image_wh = image_wh or [w, h]
            dets = det.detect(fr.image)
            det_ms.append(det.last_ms)
            p0 = time.perf_counter()
            tracks = tracker.update(t, dets) if k == 0 or t > frames[-1]["t"] else tracker.active()
            trk_ms.append((time.perf_counter() - p0) * 1000)
            s = min(1.0, JPEG_LONG_SIDE / max(w, h))
            img = cv2.resize(fr.image, (int(round(w * s)), int(round(h * s))), interpolation=cv2.INTER_AREA) if s < 1 else fr.image
            name = f"frames/f{k:06d}.jpg"
            cv2.imwrite(str(sd / name), img, [cv2.IMWRITE_JPEG_QUALITY, 82])
            frames.append({
                "k": k, "source_index": fr.index, "t": t, "image": name, "detect_ms": round(det.last_ms, 2),
                "detections": [[*(round(v, 1) for v in d.bbox_xyxy), round(d.score, 3)] for d in dets],
                "tracks": [{"id": tr.id, "state": tr.state.value, "bbox": [round(v, 1) for v in tr.bbox()],
                            "sigma_px": round(tr.position_sigma_px(), 1), "hits": tr.hits} for tr in tracks
                           if tr.state != TrackState.LOST],
            })
            if k % 5 == 0:
                status.write(progress=[k + 1, expected], message=f"frame {k + 1}/{expected} · {len(dets)} people · "
                                                                  f"{det.last_ms:.0f} ms")
        if not frames:
            raise InputError("the input produced no frames")

        status.stage("EXPORT", "writing tracks.json and tracking_report.json")
        wall = time.monotonic() - t_start
        duration = frames[-1]["t"]
        report["input"]["image_wh"] = image_wh
        report["frames_processed"] = len(frames)
        report["clip_duration_s"] = duration
        report["timing"] = {"detect_ms_p50": _pct(det_ms, 50), "detect_ms_p95": _pct(det_ms, 95),
                            "track_ms_p50": _pct(trk_ms, 50), "track_ms_p95": _pct(trk_ms, 95),
                            "processing_fps": round(len(frames) / sum((a + b) / 1000 for a, b in zip(det_ms, trk_ms)), 1),
                            "wall_s_including_io": round(wall, 2)}
        report["detections"] = {"total": sum(len(f["detections"]) for f in frames),
                                "per_frame_mean": round(float(np.mean([len(f["detections"]) for f in frames])), 2),
                                "frames_without_person": sum(1 for f in frames if not f["detections"])}
        report["tracks"] = _track_stats(frames, duration)
        report["tracker_stats"] = tracker.stats.__dict__
        report["accuracy"] = {"MOTA": None, "IDF1": None, "reason": "undefined: this input has no ground-truth person boxes"}
        report["status"] = "SUCCEEDED"
        msg = f"{len(frames)} frames · {report['tracks']['tracks_confirmed']} confirmed tracks · detect p50 {report['timing']['detect_ms_p50']} ms"
    except Cancelled as e:
        report["status"], msg = "CANCELLED", str(e)
    except (InputError, ModelUnavailable) as e:
        report["status"], msg = "FAILED", str(e)
        report["failure"] = {"stage": status.state["stage"], "message": msg}
    except Exception as e:  # report every failure; the traceback goes to process.log
        traceback.print_exc()
        report["status"], msg = "FAILED", f"{type(e).__name__}: {e}"
        report["failure"] = {"stage": status.state["stage"], "message": msg}
    report["frames_processed"] = len(frames)
    _atomic_json(sd / "tracks.json", {"session_id": sd.name, "image_wh": image_wh, "frames": frames}, compact=True)
    _atomic_json(sd / "tracking_report.json", report)
    status.write(state=report["status"], message=msg)
    print(f"{report['status']}: {msg}", flush=True)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vision.track_video")
    ap.add_argument("input")
    ap.add_argument("--session", default=None)
    ap.add_argument("--tracks-root", default="outputs/tracks")
    ap.add_argument("--origin", choices=[o.value for o in DataOrigin], default=DataOrigin.UNSPECIFIED.value)
    ap.add_argument("--model", default="yolox_nano")
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--max-frames", type=int, default=600)
    ap.add_argument("--score-thr", type=float, default=0.35)
    a = ap.parse_args(argv)
    sid = a.session or datetime.now().strftime("track-%Y%m%d-%H%M%S")
    req = TrackRequest(Path(a.input), Path(a.tracks_root) / sid, DataOrigin(a.origin), a.model, a.fps, a.max_frames, a.score_thr)
    cancel = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: cancel.set())
    rep = run_tracking(req, cancel)
    print(json.dumps({k: rep.get(k) for k in ("session_id", "status", "frames_processed", "timing", "tracks", "accuracy")}, indent=1))
    return {"SUCCEEDED": 0, "CANCELLED": 130}.get(rep["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
