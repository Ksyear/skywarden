"""Parameter calibration for the margin association on the MOT17 CALIBRATION sequences only (02, 05, 09), with
cached detections, before the held-out sequences are run once (docs/PLAN_OCCLUSION_TRACKING.md section 3).
Every grid point is recorded, not only the chosen one.
python -m vision.mot_calibrate [--detector yolox_nano]
python -m vision.mot_calibrate --grid noise --out outputs/evidence/scale_aware_noise/calib
    (docs/PLAN_SCALE_AWARE_NOISE.md section 2: VAB + scale-aware noise, same calibration sequences)
"""
from __future__ import annotations

import argparse
import itertools
import json
import subprocess
from dataclasses import replace
from pathlib import Path

from sim.crossing_ext import variant_config

from . import mot_bench

ROOT = Path(__file__).resolve().parents[2]
EVAL_PY = ROOT / "outputs" / "tools" / "venv_trackeval" / "bin" / "python"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vision.mot_calibrate")
    ap.add_argument("--detector", default="yolox_nano")
    ap.add_argument("--out", default="outputs/evidence/occlusion_tracking/calib")
    ap.add_argument("--margins", type=float, nargs="+", default=[2.0, 4.0, 6.0])
    ap.add_argument("--grid", choices=["assoc", "noise"], default="assoc")
    ap.add_argument("--meas-fracs", type=float, nargs="+", default=[0.0125, 0.025, 0.05])
    ap.add_argument("--accel-fracs", type=float, nargs="+", default=[0.0, 1.0, 2.0])
    a = ap.parse_args(argv)
    out = Path(a.out)
    cache = Path("outputs/evidence/occlusion_tracking/mot17")
    names = []
    if a.grid == "noise":
        base_name = "VAB"
        for km, ka in itertools.product(a.meas_fracs, a.accel_fracs):
            name = f"n{km:g}_a{ka:g}"
            cfg = replace(variant_config("VAB"), meas_noise_frac=km, accel_noise_frac=ka)
            mot_bench.run_variant_cfg(name, cfg, mot_bench.CALIB, a.detector, out, cache)
            names.append(f"{name}_{a.detector}")
    else:
        base_name = "V0"
        grid = list(itertools.product(a.margins, [0.1], [0.0, 2.0], [None, 0.1], [False, True]))
        for margin, ss, ocm, low, oru in grid:
            name = f"m{margin:g}_s{ss:g}_o{ocm:g}_l{low or 0:g}_r{int(oru)}"
            base = variant_config("VA")
            cfg = replace(base, margin=margin, size_sigma=ss, ocm_weight=ocm, low_score=low, oru=oru)
            mot_bench.run_variant_cfg(name, cfg, mot_bench.CALIB, a.detector, out, cache)
            names.append(f"{name}_{a.detector}")
    names.append(f"{base_name}_{a.detector}")
    mot_bench.run_variant_cfg(base_name, variant_config(base_name), mot_bench.CALIB, a.detector, out, cache)
    subprocess.run([str(EVAL_PY), "-I", str(ROOT / "scripts" / "mot_trackeval.py"), str(out), *names, "--seqs", *mot_bench.CALIB,
                    "--out", str(out / f"scores_{a.detector}.json")], check=True, stdout=subprocess.DEVNULL)
    sc = json.loads((out / f"scores_{a.detector}.json").read_text())["scores"]
    rows = sorted(((n, sc[n]["COMBINED_SEQ"]) for n in names), key=lambda r: -r[1]["IDF1"])
    for n, s in rows:
        print(f"{n:40s} IDF1 {s['IDF1']:6.2f} HOTA {s['HOTA']:6.2f} IDSW {s['IDSW']:4d} MOTA {s['MOTA']:6.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
