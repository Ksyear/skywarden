"""Video / image folder -> selected frames -> COLMAP (pycolmap) sparse SfM -> PLY + trajectory + map_report.json.

Progress is published only through `<session>/status.json` (atomic rewrite). The UI server reads that file
and never keeps its own copy of the job state, so the two cannot drift apart.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import pycolmap

from .export import export_reconstruction
from .frame_selection import Cancelled, SelectionConfig, SelectionError, select_frames
from .frame_source import InputError, open_source
from .labels import NAV_SAFETY, is_synthetic_input, CalibrationStatus, DataOrigin, Geometry, MapSource, Scale, map_source_for

STAGES = ["SELECT_FRAMES", "EXTRACT_FEATURES", "MATCH_FEATURES", "MAP", "EXPORT"]
REPORT_SCHEMA = "skywarden/map_report@1"


@dataclass
class BuildRequest:
    input_path: Path
    session_dir: Path
    origin: DataOrigin = DataOrigin.UNSPECIFIED
    camera: dict | None = None  # {"model": ..., "params": [...], "source": ...}
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    source_note: str = ""
    seed: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _atomic_json(path: Path, obj: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(path)


class StatusWriter:
    def __init__(self, session_dir: Path):
        self.path = session_dir / "status.json"
        self._lock = threading.Lock()
        self.state = {"state": "RUNNING", "stage": None, "stage_index": -1, "stages": STAGES,
                      "progress": None, "message": "starting", "pid": os.getpid(),
                      "map_source": MapSource.CURRENT_SESSION.value, "started_at": _now(), "updated_at": _now()}
        self.write()

    def write(self, **changes) -> None:
        with self._lock:
            self.state.update(changes, updated_at=_now())
            _atomic_json(self.path, self.state)

    def stage(self, name: str, message: str) -> None:
        self.write(stage=name, stage_index=STAGES.index(name), progress=None, message=message)


def hash_input(path: Path, should_stop=lambda: False) -> str:
    """SHA-256 over the whole input (provenance). Checks for cancellation every MiB."""
    h = hashlib.sha256()
    files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file() and not p.name.startswith("._"))
    for p in files:
        if path.is_dir():
            h.update(str(p.relative_to(path)).encode())
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                if should_stop():
                    raise Cancelled("cancelled while hashing input")
                h.update(chunk)
    return h.hexdigest()


# Number of leading parameters that are pixel quantities (focal lengths, principal point) per COLMAP model.
_PIXEL_PARAMS = {"SIMPLE_PINHOLE": 3, "SIMPLE_RADIAL": 3, "RADIAL": 3, "PINHOLE": 4, "OPENCV": 4,
                 "FULL_OPENCV": 4, "OPENCV_FISHEYE": 4}


def scale_camera(camera: dict, frame_wh: tuple[int, int], resize_scale: float) -> tuple[list[float], str]:
    """Adapt calibrated intrinsics to the frames COLMAP will see. Returns (params, note).

    With width/height in the calibration, the scale comes from calibration size -> frame size (aspect must match).
    Without them, the calibration is assumed to be at the source resolution and only the selection resize applies."""
    model = camera["model"]
    if model not in _PIXEL_PARAMS:
        raise InputError(f"Unsupported camera model {model!r}; use one of {sorted(_PIXEL_PARAMS)}")
    params = list(map(float, camera["params"]))
    if "width" in camera and "height" in camera:
        sx, sy = frame_wh[0] / camera["width"], frame_wh[1] / camera["height"]
        if abs(sx - sy) > 1e-3 * max(sx, sy):
            raise InputError(f"Calibration is {camera['width']}x{camera['height']} but frames are "
                             f"{frame_wh[0]}x{frame_wh[1]} (different aspect ratio); recalibrate for this video mode")
        scale, note = sx, f"scaled x{sx:.4g} from {camera['width']}x{camera['height']} to {frame_wh[0]}x{frame_wh[1]}"
    else:
        scale, note = resize_scale, "calibration size unknown; assumed equal to source resolution"
    n = _PIXEL_PARAMS[model]
    params[:n] = [v * scale for v in params[:n]]
    return params, note


def build_sparse_map(req: BuildRequest, cancel_event: threading.Event | None = None) -> dict:
    """Runs the full build. Always writes map_report.json; returns it. Raises nothing for expected failures.

    Cancel by setting `cancel_event` from any thread; a watcher forwards it to COLMAP's cancellation token so that
    long C++ stages stop too (the CLI maps SIGTERM/SIGINT onto this event)."""
    sd = req.session_dir
    sd.mkdir(parents=True, exist_ok=True)
    status = StatusWriter(sd)
    log = open(sd / "log.txt", "a", buffering=1)
    token = pycolmap.CancellationToken()
    cancel_flag = cancel_event or threading.Event()

    finished = threading.Event()

    def forward_cancel():
        while not finished.is_set():
            if cancel_flag.wait(0.1):
                token.cancel()
                return
    threading.Thread(target=forward_cancel, daemon=True, name="cancel-forwarder").start()

    def check_cancel():
        if cancel_flag.is_set():
            raise Cancelled("cancelled by user")

    def say(msg: str) -> None:
        log.write(f"{_now()} {msg}\n")

    timings: dict[str, float] = {}
    report: dict = {
        "schema": REPORT_SCHEMA,
        "session_id": sd.name,
        "created_at": _now(),
        "status": "RUNNING",
        "labels": {"map_source": MapSource.CURRENT_SESSION.value, "geometry": Geometry.SPARSE_SFM.value,
                   "scale": Scale.RELATIVE.value, "nav_safety": NAV_SAFETY, "data_origin": req.origin.value},
        "input": {"path": str(req.input_path), "source_note": req.source_note},
        "tools": {"pycolmap": pycolmap.__version__, "opencv": cv2.__version__, "numpy": np.__version__,
                  "python": sys.version.split()[0], "platform": platform.platform(), "machine": platform.machine(),
                  "device": "cpu"},
        "commands": [" ".join(sys.argv)],
        "timings_s": timings,
    }
    stage = "SELECT_FRAMES"
    try:
        # 1. frames
        t0 = time.monotonic()
        status.stage(stage, "scanning input")
        if is_synthetic_input(req.input_path) and req.origin != DataOrigin.SYNTHETIC:
            raise InputError(f"{req.input_path} is marked as synthetic data; rerun with --origin SYNTHETIC")
        source = open_source(req.input_path)
        status.write(message="hashing input for provenance")
        report["input"].update(kind=source.kind, sha256=hash_input(req.input_path, cancel_flag.is_set),
                               source_frames=source.frame_count(), nominal_fps=source.nominal_fps())
        frames_dir = sd / "frames"
        for stale in (frames_dir, sd / "sparse"):  # a rerun of the same session must start clean
            shutil.rmtree(stale, ignore_errors=True)
        sel = select_frames(source, frames_dir, req.selection, should_stop=cancel_flag.is_set,
                            on_progress=lambda d, t: status.write(progress=[d, t], message=f"scanned {d}/{t} candidate frames"))
        report["frames"] = sel.summary()
        (sd / "frames.json").write_text(json.dumps([vars(k) for k in sel.kept], indent=1))
        timings[stage] = round(time.monotonic() - t0, 2)
        say(f"selected {len(sel.kept)} frames: {sel.summary()}")
        check_cancel()

        # 2. features
        stage = "EXTRACT_FEATURES"
        t0 = time.monotonic()
        status.stage(stage, f"SIFT features on {len(sel.kept)} frames (CPU)")
        db = sd / "database.db"
        db.unlink(missing_ok=True)  # a rerun must not mix with stale features
        reader = pycolmap.ImageReaderOptions()
        if req.camera:
            reader.camera_model = req.camera["model"]
            first = cv2.imread(str(frames_dir / sel.kept[0].file))
            params, note = scale_camera(req.camera, (first.shape[1], first.shape[0]), sel.resize_scale)
            reader.camera_params = ",".join(f"{v:.8g}" for v in params)
            calib = {"status": CalibrationStatus.PROVIDED.value, "model": req.camera["model"],
                     "params_input": reader.camera_params, "scaling": note, "source": req.camera.get("source", "")}
        else:
            reader.camera_model = "SIMPLE_RADIAL"
            calib = {"status": CalibrationStatus.UNCALIBRATED_ESTIMATED.value, "model": "SIMPLE_RADIAL",
                     "params_input": None, "source": "estimated by COLMAP from EXIF-less frames"}
        report["calibration"] = calib
        extraction = pycolmap.FeatureExtractionOptions()
        extraction.use_gpu = False
        pycolmap.extract_features(db, frames_dir, camera_mode=pycolmap.CameraMode.SINGLE, reader_options=reader,
                                  extraction_options=extraction, device=pycolmap.Device.cpu, cancellation_token=token)
        timings[stage] = round(time.monotonic() - t0, 2)
        check_cancel()

        # 3. matching (sequential: frames come from a continuous camera path)
        stage = "MATCH_FEATURES"
        t0 = time.monotonic()
        status.stage(stage, "sequential matching + geometric verification")
        pairing = pycolmap.SequentialPairingOptions()
        pairing.overlap = 10
        pairing.loop_detection = False  # needs a vocabulary tree file; not shipped
        pycolmap.match_sequential(db, pairing_options=pairing, device=pycolmap.Device.cpu, cancellation_token=token)
        timings[stage] = round(time.monotonic() - t0, 2)
        check_cancel()

        # 4. incremental mapping
        stage = "MAP"
        t0 = time.monotonic()
        status.stage(stage, "incremental mapping")
        opts = pycolmap.IncrementalPipelineOptions()
        opts.random_seed = req.seed
        opts.num_threads = os.cpu_count() or 1
        if req.camera:
            opts.ba_refine_focal_length = False
            opts.ba_refine_principal_point = False
            opts.ba_refine_extra_params = False
        registered = [0]

        def on_image():
            registered[0] += 1
            status.write(progress=[registered[0], len(sel.kept)], message=f"registered {registered[0]}/{len(sel.kept)} frames")

        sparse_dir = sd / "sparse"
        sparse_dir.mkdir(exist_ok=True)
        models = pycolmap.incremental_mapping(db, frames_dir, sparse_dir, options=opts,
                                              initial_image_pair_callback=on_image, next_image_callback=on_image,
                                              cancellation_token=token)
        timings[stage] = round(time.monotonic() - t0, 2)
        check_cancel()
        if not models:
            raise SelectionError("COLMAP registered no image pair. Typical causes: pure rotation, low texture, "
                                 "motion blur or too little overlap. Re-capture with sideways translation.")
        best_idx, best = max(models.items(), key=lambda kv: kv[1].num_reg_images())
        say(f"models: {[m.num_reg_images() for m in models.values()]}, best={best_idx}")

        # 5. export
        stage = "EXPORT"
        t0 = time.monotonic()
        status.stage(stage, "writing sparse.ply and trajectory.json")
        frames_by_name = {k.file: vars(k) for k in sel.kept}
        counts = export_reconstruction(best, sd, frames_by_name)
        timings[stage] = round(time.monotonic() - t0, 2)

        n_sel = len(sel.kept)
        report["reconstruction"] = {
            "num_models": len(models),
            "model_sizes": sorted((m.num_reg_images() for m in models.values()), reverse=True),
            "chosen_model_dir": str((sparse_dir / str(best_idx)).relative_to(sd)),
            "registered_images": counts["registered_images"],
            "selected_frames": n_sel,
            "registered_ratio": round(counts["registered_images"] / n_sel, 3),
            "points3D": counts["points3D"],
            "mean_reprojection_error_px": round(float(best.compute_mean_reprojection_error()), 4),
            "mean_track_length": round(float(best.compute_mean_track_length()), 3),
            "mean_observations_per_image": round(float(best.compute_mean_observations_per_reg_image()), 1),
        }
        gate = {"points3D_gt_0": counts["points3D"] > 0, "registered_gt_2": counts["registered_images"] > 2,
                "ply_nonempty": (sd / "sparse.ply").stat().st_size > 200}
        gate["formal_minimum"] = all(gate.values())
        gate["registered_ratio_ge_0.5"] = counts["registered_images"] >= 0.5 * n_sel
        report["quality_gate"] = gate
        report["outputs"] = {"ply": "sparse.ply", "trajectory": "trajectory.json", "frames_dir": "frames",
                             "frames_manifest": "frames.json", "log": "log.txt"}
        report["labels"]["map_source"] = map_source_for(req.origin).value
        report["status"] = "SUCCEEDED" if gate["formal_minimum"] else "FAILED"
        if not gate["formal_minimum"]:
            report["failure"] = {"stage": "EXPORT", "message": f"quality gate not met: {gate}"}
    except Cancelled as e:
        report["status"] = "CANCELLED"
        report["failure"] = {"stage": stage, "message": str(e)}
    except (InputError, SelectionError) as e:
        report["status"] = "FAILED"
        report["failure"] = {"stage": stage, "message": str(e), "recoverable": True}
    except Exception as e:  # unexpected: keep full traceback in the log, short message in the report
        if cancel_flag.is_set():  # COLMAP may abort with its own exception once the token is cancelled
            report["status"] = "CANCELLED"
            report["failure"] = {"stage": stage, "message": f"cancelled by user ({type(e).__name__})"}
            log.write(traceback.format_exc())
        else:
            report["status"] = "FAILED"
            report["failure"] = {"stage": stage, "message": f"{type(e).__name__}: {e}", "recoverable": False}
            log.write(traceback.format_exc())
    finally:
        finished.set()
        report["finished_at"] = _now()
        _atomic_json(sd / "map_report.json", report)
        msg = report.get("failure", {}).get("message", "done")
        say(f"{report['status']}: {msg}")
        status.write(state=report["status"], message=msg, map_source=report["labels"]["map_source"])
        log.close()
    return report
