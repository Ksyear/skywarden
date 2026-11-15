"""Score MOT-format results with TrackEval (HOTA, CLEAR, Identity) against MOT17 train ground truth.

Runs in a separate evaluation environment, never the project .venv:
    python3 -m venv outputs/tools/venv_trackeval
    outputs/tools/venv_trackeval/bin/pip install numpy scipy "git+https://github.com/JonathonLuiten/TrackEval.git"
    outputs/tools/venv_trackeval/bin/python scripts/mot_trackeval.py <results_root> <tracker_dir> [<tracker_dir> ...] --seqs MOT17-04 ...
<results_root>/<tracker_dir>/data/<seq>-FRCNN.txt are scored; MOT17 conventions (pedestrian class only, distractor
classes removed by matching) are applied by TrackEval's MotChallenge2DBox. Writes <results_root>/scores.json.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

# TrackEval still uses the aliases NumPy 2 removed; restoring them to the builtins changes no behaviour.
for _alias, _t in (("float", float), ("int", int), ("bool", bool)):
    if not hasattr(np, _alias):
        setattr(np, _alias, _t)
import trackeval  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GT = ROOT / "data" / "public" / "mot17" / "labels" / "train"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("trackers", nargs="+")
    ap.add_argument("--seqs", nargs="+", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    root = Path(a.root)
    seqs = [f"{s}-FRCNN" for s in a.seqs]
    ev = trackeval.Evaluator({**trackeval.Evaluator.get_default_eval_config(), "PRINT_RESULTS": False, "PRINT_CONFIG": False,
                              "OUTPUT_SUMMARY": False, "OUTPUT_DETAILED": False, "PLOT_CURVES": False, "TIME_PROGRESS": False,
                              "USE_PARALLEL": False})
    ds_cfg = {**trackeval.datasets.MotChallenge2DBox.get_default_dataset_config(), "GT_FOLDER": str(GT), "TRACKERS_FOLDER": str(root),
              "TRACKERS_TO_EVAL": a.trackers, "BENCHMARK": "MOT17", "SPLIT_TO_EVAL": "train", "SKIP_SPLIT_FOL": True,
              "SEQ_INFO": {s: None for s in seqs}, "TRACKER_SUB_FOLDER": "data", "OUTPUT_FOLDER": str(root / "_trackeval")}
    metrics = [trackeval.metrics.HOTA(), trackeval.metrics.CLEAR(), trackeval.metrics.Identity()]
    res, msgs = ev.evaluate([trackeval.datasets.MotChallenge2DBox(ds_cfg)], metrics)
    out = {}
    for trk in a.trackers:
        r = res["MotChallenge2DBox"][trk]
        out[trk] = {}
        for seq in [*seqs, "COMBINED_SEQ"]:
            p = r[seq]["pedestrian"]
            out[trk][seq] = {"HOTA": round(100 * float(p["HOTA"]["HOTA"].mean()), 2), "DetA": round(100 * float(p["HOTA"]["DetA"].mean()), 2),
                             "AssA": round(100 * float(p["HOTA"]["AssA"].mean()), 2), "IDF1": round(100 * float(p["Identity"]["IDF1"]), 2),
                             "MOTA": round(100 * float(p["CLEAR"]["MOTA"]), 2), "IDSW": int(p["CLEAR"]["IDSW"]),
                             "Frag": int(p["CLEAR"]["Frag"]), "GT_IDs": int(p["CLEAR"].get("GT_IDs", 0) or 0) if "GT_IDs" in p["CLEAR"] else None,
                             "CLR_TP": int(p["CLEAR"]["CLR_TP"]), "CLR_FN": int(p["CLEAR"]["CLR_FN"]), "CLR_FP": int(p["CLEAR"]["CLR_FP"])}
    meta = {"trackeval": "JonathonLuiten/TrackEval", "gt": str(GT.relative_to(ROOT)), "sequences": seqs, "messages": [str(m) for m in msgs]}
    dst = Path(a.out) if a.out else root / "scores.json"
    dst.write_text(json.dumps({"meta": meta, "scores": out}, indent=1))
    print(json.dumps({t: v["COMBINED_SEQ"] for t, v in out.items()}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
