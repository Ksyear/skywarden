"""Appearance-distance calibration on the MOT17 CALIBRATION sequences (02, 05, 09) with ground-truth boxes.

Same-person pairs: one ground-truth id seen 0.5-2.0 s apart (the span a re-connection after a crossing needs).
Different-person pairs: two ids in the same frame. Only fully usable boxes (pedestrian class, visibility >= 0.5,
height >= 48 px). Embeddings exist only in memory while the pairs are formed; the output holds percentiles only.
python -m vision.reid_calibrate [--out outputs/evidence/occlusion_tracking/reid_calibration.json]
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np

from .mot_bench import CALIB, MOT, frame_path, seq_info
from .reid import OsnetReid


def gt_boxes(seq: str) -> dict[int, list[tuple[int, tuple]]]:
    out: dict[int, list] = {}
    for ln in (MOT / "labels" / "train" / f"{seq}-FRCNN" / "gt" / "gt.txt").read_text().splitlines():
        f, i, x, y, w, h, conf, cls, vis = ln.split(",")[:9]
        if int(float(conf)) == 1 and int(float(cls)) == 1 and float(vis) >= 0.5 and float(h) >= 48:
            out.setdefault(int(f), []).append((int(i), (float(x), float(y), float(x) + float(w), float(y) + float(h))))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vision.reid_calibrate")
    ap.add_argument("--out", default="outputs/evidence/occlusion_tracking/reid_calibration.json")
    ap.add_argument("--stride-s", type=float, default=0.5)
    a = ap.parse_args(argv)
    model = OsnetReid()
    rng = np.random.default_rng(0)
    same, diff, ms, people = [], [], [], 0
    for seq in CALIB:
        info, gt = seq_info(seq), gt_boxes(seq)
        stride = max(1, int(round(info["fps"] * a.stride_s)))
        feats: dict[int, dict[int, np.ndarray]] = {}
        for k in range(1, info["n"] + 1, stride):
            if k not in gt:
                continue
            img = cv2.imread(str(frame_path(seq, k)))
            ids, boxes = zip(*gt[k])
            t0 = time.perf_counter()
            E = model.embed(img, list(boxes))
            ms.append((time.perf_counter() - t0) * 1000)
            people += len(boxes)
            feats[k] = {i: e for i, e in zip(ids, E) if np.all(np.isfinite(e))}
            vs = list(feats[k].values())
            for p in range(len(vs)):
                for q in range(p + 1, len(vs)):
                    diff.append(float(1 - vs[p] @ vs[q]))
        ks = sorted(feats)
        for x, k1 in enumerate(ks):
            for k2 in ks[x + 1:]:
                dt = (k2 - k1) / info["fps"]
                if dt > 2.0:
                    break
                if dt < 0.5:
                    continue
                for i, e in feats[k1].items():
                    if i in feats[k2]:
                        same.append(float(1 - e @ feats[k2][i]))
        del feats  # embeddings never leave this function
    same, diff = np.array(same), np.array(diff)
    if len(diff) > 200000:
        diff = rng.choice(diff, 200000, replace=False)
    pct = lambda v: {str(p): round(float(np.percentile(v, p)), 4) for p in (1, 5, 10, 25, 50, 75, 90, 95, 99)}
    thr = np.linspace(0, 1, 201)
    roc = [{"thr": round(float(t), 3), "TPR_same_below": round(float(np.mean(same <= t)), 4), "FPR_diff_below": round(float(np.mean(diff <= t)), 4)} for t in thr]
    best_gap = float(np.percentile(diff, 5) - np.percentile(same, 50))
    res = {"label": "REAL frames, MOT17 calibration split (CC BY-NC-SA 3.0, local only); numbers only, no embeddings stored",
           "model": model.info(), "sequences": CALIB, "pairs": {"same": int(len(same)), "different": int(len(diff))},
           "same_person_cosine_distance": pct(same), "different_people_cosine_distance": pct(diff),
           "auc": round(float(np.mean([np.mean(diff > s) for s in rng.choice(same, min(4000, len(same)), replace=False)])), 4),
           "roc": roc, "median_same_to_p5_diff_gap": round(best_gap, 4),
           "timing": {"ms_per_call_p50": round(float(np.percentile(ms, 50)), 2), "ms_per_person": round(sum(ms) / max(people, 1), 3), "people": people}}
    Path(a.out).write_text(json.dumps(res, indent=1))
    print(json.dumps({k: res[k] for k in ("pairs", "same_person_cosine_distance", "different_people_cosine_distance", "auc", "timing")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
