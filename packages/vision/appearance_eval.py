"""Experiment E4: can a clothing-colour cue tell people apart where motion cannot? (no ground-truth identities)

Pseudo-labels from recorded tracking runs (conservative tracker, so LOCKED = a trusted, uncontested hit):
  positive pair  = the same track at two times >= MIN_GAP_S apart (both frames LOCKED)
  negative pair  = two different LOCKED tracks in the SAME frame (certainly two different people)
Boxes smaller than MIN_BOX_H_PX (at the stored frame resolution) are skipped. Positives can contain tracker errors
and negatives include people dressed alike, so the numbers are an estimate, not ground truth.

Go / no-go fixed BEFORE running: implement the cue only if TPR >= 0.60 at FPR <= 1 % on at least two of the clips.
python -m vision.appearance_eval --sessions outputs/tracks/A outputs/tracks/B ... --out outputs/evidence/e4_appearance
"""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import cv2
import numpy as np

from .appearance import appearance_distance, clothing_descriptor

MIN_GAP_S = 1.0
MIN_BOX_H_PX = 48
MAX_POS_PER_TRACK = 40
MAX_NEG_PER_FRAME = 6
GO_TPR, GO_FPR, GO_CLIPS = 0.60, 0.01, 2


def _descriptors(session: Path) -> tuple[dict, list[dict]]:
    rec = json.loads((session / "tracks.json").read_text())
    W0 = rec["image_wh"][0]
    desc: dict[tuple[int, int], np.ndarray] = {}
    frames = rec["frames"]
    for f in frames:
        locked = [t for t in f["tracks"] if t["state"] == "LOCKED"]
        if not locked:
            continue
        img = cv2.imread(str(session / f["image"]))
        if img is None:
            continue
        s = img.shape[1] / W0
        for t in locked:
            b = [v * s for v in t["bbox"]]
            if b[3] - b[1] < MIN_BOX_H_PX:
                continue
            d = clothing_descriptor(img, b)
            if d is not None:
                desc[(f["k"], t["id"])] = d
    return desc, frames


def _rates(pos: np.ndarray, neg: np.ndarray) -> dict:
    """Lower distance = same person. AUC = P(d_pos < d_neg)."""
    if len(pos) == 0 or len(neg) == 0:
        return {"n_pos": int(len(pos)), "n_neg": int(len(neg))}
    auc = float(np.mean([np.mean(p < neg) + 0.5 * np.mean(p == neg) for p in pos]))
    out = {"n_pos": int(len(pos)), "n_neg": int(len(neg)), "auc": round(auc, 3),
           "pos_dist_p50": round(float(np.median(pos)), 3), "neg_dist_p50": round(float(np.median(neg)), 3)}
    for fpr in (0.01, 0.001):
        thr = float(np.quantile(neg, fpr)) if len(neg) >= 1 / fpr else None  # largest threshold keeping FPR <= target
        out[f"threshold_at_fpr_{fpr}"] = None if thr is None else round(thr, 4)
        out[f"tpr_at_fpr_{fpr}"] = None if thr is None else round(float(np.mean(pos < thr)), 3)
    return out


def evaluate(session: Path, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    desc, frames = _descriptors(session)
    t_of = {f["k"]: f["t"] for f in frames}
    by_track: dict[int, list[int]] = {}
    for (k, tid) in desc:
        by_track.setdefault(tid, []).append(k)
    pos = []
    for tid, ks in by_track.items():
        pairs = [(a, b) for a, b in itertools.combinations(sorted(ks), 2) if t_of[b] - t_of[a] >= MIN_GAP_S]
        if len(pairs) > MAX_POS_PER_TRACK:
            pairs = [pairs[i] for i in rng.choice(len(pairs), MAX_POS_PER_TRACK, replace=False)]
        pos += [appearance_distance(desc[(a, tid)], desc[(b, tid)]) for a, b in pairs]
    neg = []
    for f in frames:
        ids = [tid for (k, tid) in desc if k == f["k"]]
        pairs = list(itertools.combinations(ids, 2))
        if len(pairs) > MAX_NEG_PER_FRAME:
            pairs = [pairs[i] for i in rng.choice(len(pairs), MAX_NEG_PER_FRAME, replace=False)]
        neg += [appearance_distance(desc[(f["k"], a)], desc[(f["k"], b)]) for a, b in pairs]
    r = _rates(np.array(pos), np.array(neg))
    r.update({"session": session.name, "descriptors": len(desc), "tracks_with_descriptors": len(by_track)})
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vision.appearance_eval")
    ap.add_argument("--sessions", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    clips = [evaluate(Path(s)) for s in a.sessions]
    passing = [c["session"] for c in clips if (c.get("tpr_at_fpr_0.01") or 0) >= GO_TPR]
    result = {"experiment": "E4 clothing-colour separability (pseudo-labels, no ground truth)",
              "descriptor": "HSV histograms of torso (22-50 % of box height) and legs (58-90 %); head excluded; Hellinger distance",
              "settings": {"min_gap_s": MIN_GAP_S, "min_box_h_px": MIN_BOX_H_PX, "max_pos_per_track": MAX_POS_PER_TRACK,
                           "max_neg_per_frame": MAX_NEG_PER_FRAME},
              "go_criterion_fixed_before_run": f"TPR >= {GO_TPR} at FPR <= {GO_FPR:.0%} on >= {GO_CLIPS} clips",
              "clips": clips, "clips_passing": passing,
              "decision": "GO" if len(passing) >= GO_CLIPS else "NO_GO",
              "limitations": ["positives are tracker pseudo-labels (may contain switches)",
                              "negatives include people in similar clothes (the hard case, kept on purpose)",
                              "frames are the stored JPEGs (<= 960 px long side), so far people are small"]}
    (out / "e4_appearance.json").write_text(json.dumps(result, indent=1))
    print(json.dumps({"clips": clips, "decision": result["decision"], "passing": passing}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
