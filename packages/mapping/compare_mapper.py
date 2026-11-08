"""Experiment: re-run only the mapping step of an existing session with another mapper, same frames and matches.

The session's database (features + verified matches, already restricted to single shots) and frames are reused, so
the only difference is the mapper. Output goes to outputs/experiments/<name>/ with the same report fields
(quality, per-model exports, coverage) and the geometry diagnosis, so before/after is a like-for-like comparison.

python -m mapping.compare_mapper <session_dir> --mapper global|incremental --name <experiment>
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import pycolmap

from .diagnose_geometry import diagnose
from .export import export_reconstruction
from .quality import THRESHOLDS, evaluate


def run(sd: Path, mapper: str, out: Path, seed: int = 0) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sd / "database.db", out / "database.db")
    shutil.copy2(sd / "frames.json", out / "frames.json")
    frames_dir = sd / "frames"
    frames = json.loads((sd / "frames.json").read_text())
    shutil.rmtree(out / "sparse", ignore_errors=True)
    (out / "sparse").mkdir()
    t0 = time.monotonic()
    if mapper == "global":
        opts = pycolmap.GlobalPipelineOptions()
        opts.random_seed = seed
        models = pycolmap.global_mapping(out / "database.db", frames_dir, out / "sparse", opts)
    else:
        opts = pycolmap.IncrementalPipelineOptions()
        opts.random_seed = seed
        models = pycolmap.incremental_mapping(out / "database.db", frames_dir, out / "sparse", options=opts)
    secs = time.monotonic() - t0
    ranked = sorted(models.items(), key=lambda kv: -kv[1].num_reg_images())
    by_name = {f["file"]: f for f in frames}
    index_of = {f["file"]: i for i, f in enumerate(frames)}
    model_frames, info = [], []
    for rank, (idx, m) in enumerate(ranked):
        mdir = out / "models" / str(rank)
        mdir.mkdir(parents=True, exist_ok=True)
        c = export_reconstruction(m, mdir, by_name)
        fidx = sorted(index_of[im.name] for im in m.images.values() if im.has_pose)
        model_frames.append(fidx)
        info.append({"rank": rank, "colmap_dir": f"sparse/{idx}", "registered_images": c["registered_images"],
                     "points3D": c["points3D"], "mean_reprojection_error_px": round(float(m.compute_mean_reprojection_error()), 4)})
    best = ranked[0][1]
    ims = sorted((im for im in best.images.values() if im.has_pose), key=lambda im: index_of[im.name])
    times = [f["timestamp_s"] for f in frames]
    q = evaluate(times, model_frames, float(best.compute_mean_reprojection_error()), best.num_points3D(),
                 [list(map(float, im.projection_center())) for im in ims], THRESHOLDS)
    rep = {"experiment": out.name, "source_session": sd.name, "mapper": mapper, "seed": seed, "map_seconds": round(secs, 1),
           "status": "SUCCEEDED",
           "reconstruction": {"num_models": len(models), "model_sizes": [m.num_reg_images() for _, m in ranked],
                              "models": info, "selected_frames": len(frames),
                              "registered_images": info[0]["registered_images"], "points3D": info[0]["points3D"],
                              "chosen_model_dir": info[0]["colmap_dir"]},
           "quality": q}
    (out / "map_report.json").write_text(json.dumps(rep, indent=1))
    shutil.copytree(frames_dir, out / "frames", dirs_exist_ok=True)
    d = diagnose(out, 0)
    (out / "diagnosis.json").write_text(json.dumps(d, indent=1))
    rep["diagnosis_summary"] = {k: d[k] for k in ("points", "consistency_r")}
    (out / "map_report.json").write_text(json.dumps(rep, indent=1))
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.compare_mapper")
    ap.add_argument("session_dir")
    ap.add_argument("--mapper", choices=["global", "incremental"], required=True)
    ap.add_argument("--name", required=True)
    a = ap.parse_args(argv)
    rep = run(Path(a.session_dir), a.mapper, Path("outputs/experiments") / a.name)
    q = rep["quality"]
    print(json.dumps({"mapper": a.mapper, "secs": rep["map_seconds"], "models": rep["reconstruction"]["model_sizes"],
                      "verdict": q["verdict"], "failed": q["failed_checks"],
                      "checks": {k: v["value"] for k, v in q["checks"].items()},
                      "r_by_third": rep["diagnosis_summary"]["consistency_r"]["by_third"]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
