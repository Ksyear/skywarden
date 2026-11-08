"""Evaluate map quality for a session built before quality checks existed (report schema @1).

Reads the session's own files (frames.json, sparse/<i> COLMAP models), applies the same frozen thresholds as new
builds, and stores the result in map_report.json under "quality" with "rated_offline": true. Nothing is re-run.
"""
from __future__ import annotations

import json
from pathlib import Path

import pycolmap

from .quality import THRESHOLDS, evaluate
from .replay import ReplayError


def rate_session(sd: Path) -> dict:
    try:
        report = json.loads((sd / "map_report.json").read_text())
        frames = json.loads((sd / "frames.json").read_text())
    except FileNotFoundError as e:
        raise ReplayError(f"{sd.name}: {e.filename} missing")
    if report.get("status") != "SUCCEEDED":
        raise ReplayError(f"{sd.name}: status {report.get('status')}, nothing to rate")
    index_of = {f["file"]: i for i, f in enumerate(frames)}
    models = []
    for d in sorted((sd / "sparse").iterdir()) if (sd / "sparse").is_dir() else []:
        if d.is_dir() and (d / "images.bin").exists():
            r = pycolmap.Reconstruction(str(d))
            ims = sorted((im for im in r.images.values() if im.has_pose), key=lambda im: index_of[im.name])
            models.append((r, [index_of[im.name] for im in ims], [list(map(float, im.projection_center())) for im in ims]))
    if not models:
        raise ReplayError(f"{sd.name}: no COLMAP models under sparse/")
    models.sort(key=lambda m: -len(m[1]))
    times = [f["timestamp_s"] if f.get("timestamp_s") is not None else float(i) for i, f in enumerate(frames)]
    q = evaluate(times, [m[1] for m in models], float(models[0][0].compute_mean_reprojection_error()),
                 models[0][0].num_points3D(), models[0][2], THRESHOLDS)
    q["rated_offline"] = True
    report["quality"] = q
    report.setdefault("labels", {})["completeness"] = "COMPLETE" if q["verdict"] == "GOOD" else "PARTIAL_RECONSTRUCTION"
    (sd / "map_report.json").write_text(json.dumps(report, indent=1))
    return q
