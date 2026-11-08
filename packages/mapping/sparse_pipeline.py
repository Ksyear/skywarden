"""Video / image folder -> selected frames -> COLMAP (pycolmap) sparse SfM -> PLY + trajectory + map_report.json.

Progress is published only through `<session>/status.json` (atomic rewrite). The UI server reads that file
and never keeps its own copy of the job state, so the two cannot drift apart.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
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
from .quality import THRESHOLDS, evaluate as evaluate_quality
from .shots import STATIC_FLOW_PX, detect_shots

STAGES = ["SELECT_FRAMES", "EXTRACT_FEATURES", "MATCH_FEATURES", "MAP", "EXPORT"]
REPORT_SCHEMA = "skywarden/map_report@2"  # @2: job status and map quality are separate; every model exported
MAX_KEPT_GAP_S = 1.0  # consecutive selected frames further apart than this lose overlap; fail instead of thinning
PAIR_ID_BASE = 2147483647


@dataclass
class BuildRequest:
    input_path: Path
    session_dir: Path
    origin: DataOrigin = DataOrigin.UNSPECIFIED
    camera: dict | None = None  # {"model": ..., "params": [...], "source": ...}
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    source_note: str = ""
    seed: int = 0
    # "global" (rotation averaging + global positioning) is the default: on the FSC aerial clip it removed the late
    # scale drift of the incremental mapper, and on TUM fr1/desk it lowered ATE 2.05 -> 1.66 cm (all 53 frames).
    # "incremental" stays available; it is the only mapper that honours cancellation inside the MAP stage.
    mapper: str = "global"


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


def _drop_cross_shot_pairs(db: Path, shot_of: dict[str, int]) -> dict:
    """Remove matches between frames of different shots so the mapper can never chain two scenes, and return the
    verified inlier count between each selected frame and its predecessor (for the coverage report)."""
    con = sqlite3.connect(db)
    try:
        name_of = dict(con.execute("SELECT image_id, name FROM images"))
        removed = 0
        for table in ("matches", "two_view_geometries"):
            for (pid,) in list(con.execute(f"SELECT pair_id FROM {table}")):
                i2 = pid % PAIR_ID_BASE
                i1 = (pid - i2) // PAIR_ID_BASE
                a, b = name_of.get(i1), name_of.get(i2)
                if a in shot_of and b in shot_of and shot_of[a] != shot_of[b]:
                    con.execute(f"DELETE FROM {table} WHERE pair_id = ?", (pid,))
                    removed += table == "two_view_geometries"
        con.commit()
        inliers = {}
        for pid, rows in con.execute("SELECT pair_id, rows FROM two_view_geometries"):
            i2 = pid % PAIR_ID_BASE
            inliers[((pid - i2) // PAIR_ID_BASE, i2)] = rows
        id_of = {v: k for k, v in name_of.items()}
        names = sorted(shot_of)  # frames are named in selection order (f000000.jpg, ...)
        prev = [None] + [inliers.get(tuple(sorted((id_of[a], id_of[b]))), 0) for a, b in zip(names, names[1:])]
    finally:
        con.close()
    return {"cross_shot_pairs_removed": removed, "inliers_to_prev": prev}


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
        stamps = [k.timestamp_s for k in sel.kept]
        max_gap = max((b - a for a, b in zip(stamps, stamps[1:])), default=0.0) if None not in stamps else None
        report["frames"]["max_gap_s"] = None if max_gap is None else round(max_gap, 3)

        # input check: shots (continuous takes) and camera motion; frames on different shots are never linked
        shot_of = {k.file: 0 for k in sel.kept}
        if source.kind == "video_file":
            status.write(message="detecting shot changes and camera motion")
            shots, shot_meta = detect_shots(req.input_path)
            (sd / "shots.json").write_text(json.dumps({**shot_meta, "shots": [vars(x) for x in shots]}, indent=1))
            starts = [x.start_frame for x in shots]
            for k in sel.kept:
                shot_of[k.file] = max(i for i, st in enumerate(starts) if st <= k.source_index)
            dur = sum(x.duration_s for x in shots) or 1.0
            static_share = sum(x.duration_s for x in shots if x.camera_motion == "static") / dur
            report["input_check"] = {"shots": len(shots), "cuts": shot_meta["cuts"], "rule": shot_meta["rule"],
                                     "static_duration_share": round(static_share, 3),
                                     "shots_with_selected_frames": len(set(shot_of.values()))}
            if static_share > 0.9:
                med = [x.median_flow_px_per_frame for x in shots if x.median_flow_px_per_frame is not None]
                raise SelectionError(
                    f"camera is static (median flow {np.median(med) if med else 0:.3f} px/frame < {STATIC_FLOW_PX}) for "
                    f"{static_share:.0%} of the input: structure from motion needs a moving camera. Use this clip for "
                    f"tracking; for mapping use a clip where the camera moves sideways.")
        else:
            report["input_check"] = {"shots": 1, "cuts": 0, "note": "image folder: treated as one continuous take"}
        # Fatal only when the frame cap caused the gap (silent thinning). Gaps left by blur/duplicate rejection are
        # recorded and judged by the quality check (max_internal_gap_s): rejecting them here refused TUM fr1/desk,
        # a ground-truth input that reconstructs GOOD (ATE 1.66-2.05 cm) with 1.34 s gaps.
        report["frames"]["gap_guard"] = {"max_gap_s": report["frames"]["max_gap_s"], "limit_s": MAX_KEPT_GAP_S,
                                         "fatal_only_when_caused_by_cap": True, "cap_dropped": sel.dropped_by_cap}
        if max_gap is not None and max_gap > MAX_KEPT_GAP_S and sel.dropped_by_cap:
            cause = (f"the cap of {req.selection.max_frames} frames dropped {sel.dropped_by_cap} frames; use a shorter "
                     f"section or a higher --max-frames") if sel.dropped_by_cap else (
                     f"blur/low-texture/duplicate rejections ({sel.rejected_blur}/{sel.rejected_low_texture}/"
                     f"{sel.rejected_duplicate}) left the gap; sample more densely with a higher --fps")
            raise SelectionError(f"selected frames are up to {max_gap:.3f} s apart (> {MAX_KEPT_GAP_S} s), which breaks "
                                 f"overlap: {cause}.")
        (sd / "frames.json").write_text(json.dumps([{**vars(k), "shot": shot_of[k.file]} for k in sel.kept], indent=1))
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
        report["matching"] = _drop_cross_shot_pairs(db, shot_of)
        timings[stage] = round(time.monotonic() - t0, 2)
        check_cancel()

        # 4. mapping
        stage = "MAP"
        t0 = time.monotonic()
        status.stage(stage, f"{req.mapper} mapping")
        sparse_dir = sd / "sparse"
        sparse_dir.mkdir(exist_ok=True)
        report["mapper"] = req.mapper
        if req.mapper == "global":
            gopts = pycolmap.GlobalPipelineOptions()
            gopts.random_seed = req.seed
            gopts.num_threads = os.cpu_count() or 1
            if req.camera:  # calibrated intrinsics stay fixed, as with the incremental mapper
                ba = gopts.mapper.bundle_adjustment
                ba.refine_focal_length = ba.refine_principal_point = ba.refine_extra_params = False
            status.write(message="global mapping (rotation averaging + positioning); cancel takes effect after this stage")
            models = pycolmap.global_mapping(db, frames_dir, sparse_dir, gopts)
        else:
            models = None
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

        if models is None:
            models = pycolmap.incremental_mapping(db, frames_dir, sparse_dir, options=opts,
                                                  initial_image_pair_callback=on_image, next_image_callback=on_image,
                                                  cancellation_token=token)
        timings[stage] = round(time.monotonic() - t0, 2)
        check_cancel()
        if not models:
            raise SelectionError("COLMAP registered no image pair. Typical causes: pure rotation, low texture, "
                                 "motion blur or too little overlap. Re-capture with sideways translation.")
        ranked = sorted(models.items(), key=lambda kv: -kv[1].num_reg_images())
        best_idx, best = ranked[0]
        say(f"models: {[m.num_reg_images() for _, m in ranked]}, best={best_idx}")

        # 5. export: every model separately (models/<rank>/, rank 0 = largest); the top-level files are the largest
        stage = "EXPORT"
        t0 = time.monotonic()
        status.stage(stage, "writing every model, coverage and quality")
        frames_by_name = {k.file: vars(k) for k in sel.kept}
        counts = export_reconstruction(best, sd, frames_by_name)
        index_of = {k.file: i for i, k in enumerate(sel.kept)}
        model_frames, model_info = [], []
        largest_centers = []
        shutil.rmtree(sd / "models", ignore_errors=True)
        for rank, (idx, m) in enumerate(ranked):
            mdir = sd / "models" / str(rank)
            mdir.mkdir(parents=True)
            c = export_reconstruction(m, mdir, frames_by_name)
            ims = sorted((im for im in m.images.values() if im.has_pose), key=lambda im: index_of[im.name])
            fidx = [index_of[im.name] for im in ims]
            if rank == 0:
                largest_centers = [list(map(float, im.projection_center())) for im in ims]
            model_frames.append(fidx)
            ts = [sel.kept[i].timestamp_s for i in fidx]
            info = {"rank": rank, "colmap_dir": f"sparse/{idx}", "registered_images": c["registered_images"],
                    "points3D": c["points3D"], "mean_reprojection_error_px": round(float(m.compute_mean_reprojection_error()), 4),
                    "first_frame": fidx[0], "last_frame": fidx[-1],
                    "time_range_s": [ts[0], ts[-1]] if None not in ts else None,
                    "shots": sorted({shot_of[sel.kept[i].file] for i in fidx}),
                    "focal_px": round(float(list(m.cameras.values())[0].params[0]), 1)}
            (mdir / "model_report.json").write_text(json.dumps(info, indent=1))
            model_info.append(info)
        times = [k.timestamp_s if k.timestamp_s is not None else float(i) for i, k in enumerate(sel.kept)]
        member: dict[int, list[int]] = {}
        for rank, fidx in enumerate(model_frames):
            for i in fidx:
                member.setdefault(i, []).append(rank)
        inl = report["matching"].get("inliers_to_prev", [])
        cov_rows = [{"frame": i, "file": k.file, "timestamp_s": k.timestamp_s, "shot": shot_of[k.file],
                     "models": member.get(i, []), "inliers_to_prev": inl[i] if i < len(inl) else None}
                    for i, k in enumerate(sel.kept)]
        missing, run = [], None
        for r in cov_rows:
            if 0 not in r["models"]:
                run = run or [r["timestamp_s"], r["timestamp_s"]]
                run[1] = r["timestamp_s"]
            elif run:
                missing.append(run)
                run = None
        if run:
            missing.append(run)
        quality = evaluate_quality(times, model_frames, float(best.compute_mean_reprojection_error()),
                                   counts["points3D"], largest_centers, THRESHOLDS)
        (sd / "coverage.json").write_text(json.dumps({"frames": cov_rows, "missing_in_largest_model_s": missing,
                                                      "jumps_in_largest_model": quality["jumps"]}, indent=1))
        timings[stage] = round(time.monotonic() - t0, 2)

        n_sel = len(sel.kept)
        report["reconstruction"] = {
            "num_models": len(models),
            "model_sizes": [m.num_reg_images() for _, m in ranked],
            "models": model_info,
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
        report["quality_gate"] = gate  # formal minimum only: decides whether the job wrote a usable file set
        report["quality"] = quality    # whether the map is one continuous, consistent reconstruction of the input
        report["labels"]["completeness"] = "COMPLETE" if quality["verdict"] == "GOOD" else "PARTIAL_RECONSTRUCTION"
        report["outputs"] = {"ply": "sparse.ply", "trajectory": "trajectory.json", "frames_dir": "frames",
                             "frames_manifest": "frames.json", "log": "log.txt", "coverage": "coverage.json",
                             "models_dir": "models", "shots": "shots.json" if (sd / "shots.json").exists() else None,
                             "top_level_files": "largest model only (models/0); other models are separate coordinate frames"}
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
