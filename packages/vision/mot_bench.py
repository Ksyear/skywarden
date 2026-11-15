"""Local benchmark run on MOT17 train sequences (ground-truth boxes and ids) for comparing tracker variants.

Evaluation-only use of a CC BY-NC-SA 3.0 research dataset kept under data/public/mot17/ (gitignored): frames are
read locally, nothing derived from them (images, crops, embeddings) is written anywhere. What is written:
  <out>/dets/<seq>_<detector>.json         detection boxes + scores (cache; numbers only)
  <out>/<variant>_<detector>/data/<seq>.txt  MOT-format results of LOCKED tracks (frame,id,x,y,w,h,1,-1,-1,-1)
  <out>/<variant>_<detector>/run.json      tracker config, stats, timing (detector, tracker, ReID ms)
Scores come from TrackEval in a separate evaluation environment (scripts/mot_trackeval.py).

Detector protocols: "yolox_nano" (ours, CPU, threshold 0.10 so the second association stage has low-score boxes;
stage 1 still uses >= 0.35 like every other run in this project) or "frcnn" (the public MOT17 FRCNN detections).
python -m vision.mot_bench --variants V0 VA VAB VABC --seqs MOT17-02 ... [--detector yolox_nano|frcnn]
"""
from __future__ import annotations

import argparse
import configparser
import json
import time
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np

from sim.crossing_ext import variant_config
from tracking.tracker import Detection, MultiTracker, TrackState

from .tracker_setup import TimedEmbedder

ROOT = Path(__file__).resolve().parents[2]
MOT = ROOT / "data" / "public" / "mot17"
DET_THR_LOW, DET_THR_HIGH = 0.10, 0.35
CALIB = ["MOT17-02", "MOT17-05", "MOT17-09"]
HELDOUT = ["MOT17-04", "MOT17-10", "MOT17-11", "MOT17-13"]


def seq_info(seq: str) -> dict:
    c = configparser.ConfigParser()
    c.read(MOT / "labels" / "train" / f"{seq}-FRCNN" / "seqinfo.ini")
    s = c["Sequence"]
    return {"fps": float(s["frameRate"]), "n": int(s["seqLength"]), "w": int(s["imWidth"]), "h": int(s["imHeight"])}


def frame_path(seq: str, k: int) -> Path:
    return MOT / "mot16" / "train" / seq.replace("MOT17", "MOT16") / "img1" / f"{k:06d}.jpg"


def detections(seq: str, detector: str, out: Path) -> dict[int, list[list[float]]]:
    """{frame: [[x1, y1, x2, y2, score], ...]} cached as JSON (boxes only)."""
    cache = out / "dets" / f"{seq}_{detector}.json"
    if cache.is_file():
        return {int(k): v for k, v in json.loads(cache.read_text())["frames"].items()}
    info = seq_info(seq)
    frames: dict[int, list[list[float]]] = {k: [] for k in range(1, info["n"] + 1)}
    meta = {"seq": seq, "detector": detector}
    if detector == "frcnn":
        for ln in (MOT / "labels" / "train" / f"{seq}-FRCNN" / "det" / "det.txt").read_text().splitlines():
            f, _, x, y, w, h, s = (float(v) for v in ln.split(",")[:7])
            frames[int(f)].append([x, y, x + w, y + h, s])
        meta["source"] = "MOT17 public FRCNN det.txt"
    else:
        from .person_detector import make_detector
        det = make_detector(detector, score_thr=DET_THR_LOW)
        ms = []
        for k in frames:
            img = cv2.imread(str(frame_path(seq, k)))
            frames[k] = [[*d.bbox_xyxy, d.score] for d in det.detect(img)]
            ms.append(det.last_ms)
        meta.update(det.info(), detect_ms_p50=round(float(np.percentile(ms, 50)), 2), detect_ms_p95=round(float(np.percentile(ms, 95)), 2))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({**meta, "frames": frames}))
    return frames


def run_variant(variant: str, seqs: list[str], detector: str, out: Path, reid_model=None) -> dict:
    return run_variant_cfg(variant, variant_config(variant), seqs, detector, out, out, reid_model)


def run_variant_cfg(variant: str, cfg, seqs: list[str], detector: str, out: Path, det_dir: Path, reid_model=None) -> dict:
    if detector == "yolox_nano":  # stage 1 sees exactly the boxes the project's 0.35 detector threshold would give
        cfg = replace(cfg, min_score=max(cfg.min_score, DET_THR_HIGH))
    vdir = out / f"{variant}_{detector}"
    (vdir / "data").mkdir(parents=True, exist_ok=True)
    runs = {}
    for seq in seqs:
        info = seq_info(seq)
        dets = detections(seq, detector, det_dir)
        emb = TimedEmbedder(reid_model) if (cfg.reid and reid_model is not None) else None
        trk = MultiTracker(cfg, embedder=emb)
        need_frame = cfg.gmc or cfg.reid
        lines, trk_ms, n_locked = [], [], 0
        for k in range(1, info["n"] + 1):
            frame = cv2.imread(str(frame_path(seq, k))) if need_frame else None
            ds = [Detection(tuple(d[:4]), float(min(1.0, d[4])), "REAL_MODEL" if detector != "frcnn" else "PUBLIC_DET") for d in dets[k]]
            t0 = time.perf_counter()
            tracks = trk.update((k - 1) / info["fps"], ds, frame=frame)
            trk_ms.append((time.perf_counter() - t0) * 1000)
            for tr in tracks:
                if tr.state != TrackState.LOCKED:
                    continue
                x1, y1, x2, y2 = tr.bbox()
                lines.append(f"{k},{tr.id},{x1:.1f},{y1:.1f},{x2 - x1:.1f},{y2 - y1:.1f},1,-1,-1,-1")
                n_locked += 1
        (vdir / "data" / f"{seq}-FRCNN.txt").write_text("\n".join(lines) + "\n")
        runs[seq] = {"frames": info["n"], "locked_boxes": n_locked, "tracker_stats": trk.stats.__dict__,
                     "track_ms_p50": round(float(np.percentile(trk_ms, 50)), 3), "track_ms_p95": round(float(np.percentile(trk_ms, 95)), 3),
                     "track_ms_mean": round(float(np.mean(trk_ms)), 3),
                     "reid": None if emb is None else emb.summary()}
        print(f"{variant} {detector} {seq}: locked boxes {n_locked}, track ms mean {runs[seq]['track_ms_mean']}", flush=True)
    rep = {"variant": variant, "detector": detector, "tracker": cfg.describe(), "tracker_config": cfg.__dict__,
           "export_rule": "LOCKED tracks only (trusted detection this frame)", "sequences": runs,
           "label": "REAL_MODEL detections on MOT17 train (CC BY-NC-SA 3.0, local evaluation only)" if detector != "frcnn"
           else "MOT17 public FRCNN detections (CC BY-NC-SA 3.0, local evaluation only)"}
    (vdir / "run.json").write_text(json.dumps(rep, indent=1))
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vision.mot_bench")
    ap.add_argument("--variants", nargs="+", default=["V0"])
    ap.add_argument("--seqs", nargs="+", default=CALIB)
    ap.add_argument("--detector", default="yolox_nano", choices=["yolox_nano", "frcnn"])
    ap.add_argument("--out", default="outputs/evidence/occlusion_tracking/mot17")
    a = ap.parse_args(argv)
    seqs = CALIB + HELDOUT if a.seqs == ["all"] else HELDOUT if a.seqs == ["heldout"] else CALIB if a.seqs == ["calib"] else a.seqs
    reid = None
    if any(variant_config(v).reid for v in a.variants):
        from .reid import OsnetReid
        reid = OsnetReid()
    for v in a.variants:
        run_variant(v, seqs, a.detector, Path(a.out), reid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
