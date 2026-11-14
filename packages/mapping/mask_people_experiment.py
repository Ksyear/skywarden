"""Experiment: does masking detected people (YOLOX boxes) change the reconstruction of a person-following clip?

Same selected frames as an existing session; features are extracted with person boxes masked out (COLMAP ignores
zero-valued mask pixels), matched sequentially within the same shots, then mapped with the global mapper. Output in
outputs/experiments/<name>/ with the usual quality block, so before/after is like-for-like. Not a pipeline default.

python -m mapping.mask_people_experiment <session_dir> --name <experiment>
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import cv2
import numpy as np
import pycolmap

from vision.person_detector import make_detector

from .export import export_reconstruction
from .quality import THRESHOLDS, evaluate
from .sparse_pipeline import _drop_cross_shot_pairs


def run(sd: Path, out: Path, pad: float = 0.1) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    frames = json.loads((sd / "frames.json").read_text())
    masks = out / "masks"
    shutil.rmtree(masks, ignore_errors=True)
    masks.mkdir()
    det = make_detector("yolox_nano")
    masked_share = []
    for f in frames:
        img = cv2.imread(str(sd / "frames" / f["file"]))
        m = np.full(img.shape[:2], 255, np.uint8)
        for d in det.detect(img):
            x1, y1, x2, y2 = d.bbox_xyxy
            w, h = x2 - x1, y2 - y1
            cv2.rectangle(m, (int(x1 - pad * w), int(y1 - pad * h)), (int(x2 + pad * w), int(y2 + pad * h)), 0, -1)
        masked_share.append(float(np.mean(m == 0)))
        cv2.imwrite(str(masks / (f["file"] + ".png")), m)
    db = out / "database.db"
    db.unlink(missing_ok=True)
    reader = pycolmap.ImageReaderOptions()
    reader.camera_model = "SIMPLE_RADIAL"
    reader.mask_path = str(masks)
    ex = pycolmap.FeatureExtractionOptions()
    ex.use_gpu = False
    t0 = time.monotonic()
    pycolmap.extract_features(db, sd / "frames", camera_mode=pycolmap.CameraMode.SINGLE, reader_options=reader,
                              extraction_options=ex, device=pycolmap.Device.cpu)
    pairing = pycolmap.SequentialPairingOptions()
    pairing.overlap = 10
    pairing.loop_detection = False
    pycolmap.match_sequential(db, pairing_options=pairing, device=pycolmap.Device.cpu)
    _drop_cross_shot_pairs(db, {f["file"]: f.get("shot", 0) for f in frames})
    (out / "sparse").mkdir(exist_ok=True)
    opts = pycolmap.GlobalPipelineOptions()
    opts.random_seed = 0
    models = pycolmap.global_mapping(db, sd / "frames", out / "sparse", opts)
    ranked = sorted(models.items(), key=lambda kv: -kv[1].num_reg_images())
    index_of = {f["file"]: i for i, f in enumerate(frames)}
    by_name = {f["file"]: f for f in frames}
    model_frames = []
    for rank, (idx, m) in enumerate(ranked):
        d = out / "models" / str(rank)
        d.mkdir(parents=True, exist_ok=True)
        export_reconstruction(m, d, by_name)
        model_frames.append(sorted(index_of[im.name] for im in m.images.values() if im.has_pose))
    best = ranked[0][1]
    ims = sorted((im for im in best.images.values() if im.has_pose), key=lambda im: index_of[im.name])
    q = evaluate([f["timestamp_s"] for f in frames], model_frames, float(best.compute_mean_reprojection_error()),
                 best.num_points3D(), [list(map(float, im.projection_center())) for im in ims], THRESHOLDS)
    rep = {"experiment": out.name, "source_session": sd.name, "variation": "person boxes masked (YOLOX-nano, +10 % pad)",
           "masked_pixel_share": {"p50": round(float(np.median(masked_share)), 3), "max": round(float(np.max(masked_share)), 3)},
           "seconds": round(time.monotonic() - t0, 1), "model_sizes": [m.num_reg_images() for _, m in ranked],
           "quality": q, "jumps": q["jumps"]}
    (out / "map_report.json").write_text(json.dumps(rep, indent=1))
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.mask_people_experiment")
    ap.add_argument("session_dir")
    ap.add_argument("--name", required=True)
    a = ap.parse_args(argv)
    r = run(Path(a.session_dir), Path("outputs/experiments") / a.name)
    print(json.dumps({k: r[k] for k in ("masked_pixel_share", "seconds", "model_sizes")} | {
        "verdict": r["quality"]["verdict"], "failed": r["quality"]["failed_checks"],
        "checks": {k: v["value"] for k, v in r["quality"]["checks"].items()},
        "jumps": [(round(j["t_from"], 1), j["speed_x_median"]) for j in r["jumps"]]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
