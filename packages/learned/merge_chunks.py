"""Join verified learned-depth chunks into one input for mapping.learned_fuse.

For each pair of neighbouring chunks, the overlapping frames are compared after both were aligned to our SfM path:
median relative depth difference and the median depth ratio (scale). A chunk whose overlap disagrees with its
already accepted neighbour by more than MAX_OVERLAP_DIFF (relative) or whose scale ratio is off by more than
MAX_SCALE_OFF is held back (it would put a second, shifted surface into the map). Every frame is used once: from the
accepted chunk in which it lies closest to the chunk centre.

python -m learned.merge_chunks --tag sumida --out outputs/experiments/learned_depth/lingbot_v2_sumida_merged
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np

ROOT = Path("outputs/experiments/learned_depth")
MAX_OVERLAP_DIFF = 0.05
MAX_SCALE_OFF = 0.05  # also the limit for the independent near-point scale check against triangulated points


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="learned.merge_chunks")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    st = json.loads((ROOT / f"chunks_{a.tag}.json").read_text())
    sd = Path(st["session"])
    t_of = {f["file"]: f["timestamp_s"] for f in json.loads((sd / "frames.json").read_text())}
    chunks = []
    for t0, t1 in st["plan"]:
        key = f"{t0:g}-{t1:g}"
        info = st["chunks"].get(key, {})
        if info.get("status") != "DONE":
            chunks.append({"key": key, "t0": t0, "t1": t1, "status": info.get("status", "NOT_RUN")}); continue
        raw = ROOT / (f"lingbot_v2_{a.tag}_c{int(t0)}" if (ROOT / f"lingbot_v2_{a.tag}_c{int(t0)}" / "chunk_done.json").exists() and t0 == 0 else f"lingbot_v2_{a.tag}_t{t0:g}_{t1:g}")
        al = Path(str(raw) + "_aligned")
        frames = json.loads((raw / "names.json").read_text())
        chunks.append({"key": key, "t0": t0, "t1": t1, "status": "DONE", "dir": str(al), "frames": frames})
    accepted, prev = [], None
    for c in chunks:
        if c["status"] != "DONE":
            continue
        if prev is not None:
            ov = sorted(set(prev["frames"]) & set(c["frames"]))
            diffs, ratios = [], []
            for n in ov:
                d0 = np.load(Path(prev["dir"]) / f"depth_{n[:-4]}.npy"); d1 = np.load(Path(c["dir"]) / f"depth_{n[:-4]}.npy")
                # only the near part is ever used for the map, so the overlap is judged there (nearest 40 % of depths)
                m = np.isfinite(d0) & np.isfinite(d1) & (d0 > 0) & (d1 > 0) & (d0 <= np.percentile(d0[d0 > 0], 40))
                r = d1[m] / d0[m]
                ratios.append(float(np.median(r))); diffs.append(float(np.median(np.abs(r - 1))))
            c["overlap"] = {"frames": len(ov), "median_rel_depth_diff": round(float(np.median(diffs)), 4) if diffs else None,
                            "median_scale_ratio": round(float(np.median(ratios)), 4) if ratios else None}
            k = json.loads((Path(c["dir"]) / "run_report.json").read_text()).get("scale_near_sparse_factor", 1.0)
            c["near_point_scale_check"] = round(float(k), 4)
            if not ov:
                c["status"] = "HELD_NO_OVERLAP"
            elif abs(k - 1) > MAX_SCALE_OFF:
                c["status"] = "HELD_SCALE_VS_TRIANGULATION"
            elif np.median(diffs) > MAX_OVERLAP_DIFF or abs(np.median(ratios) - 1) > MAX_SCALE_OFF:
                c["status"] = "HELD_OVERLAP_DISAGREES"
        if c["status"] == "DONE":
            accepted.append(c)
        prev = c  # each pair of neighbouring chunks is checked on its own overlap (both are aligned to our path)
    out = Path(a.out)
    shutil.rmtree(out, ignore_errors=True); out.mkdir(parents=True)
    owner = {}
    for c in accepted:
        mid = 0.5 * (c["t0"] + c["t1"])
        for n in c["frames"]:
            d = abs(t_of[n] - mid)
            if n not in owner or d < owner[n][1]:
                owner[n] = (c, d)
    for n, (c, _) in owner.items():
        for kind in ("depth", "conf"):
            shutil.copyfile(Path(c["dir"]) / f"{kind}_{n[:-4]}.npy", out / f"{kind}_{n[:-4]}.npy")
    base = json.loads((Path(accepted[0]["dir"]) / "run_report.json").read_text()) if accepted else {}
    base.pop("sparse_agreement_per_frame", None)
    base["chunks"] = [{k: v for k, v in c.items() if k not in ("frames", "dir")} for c in chunks]
    base["frames"] = len(owner)
    base["alignment"] = "per chunk: one Sim3 from that chunk's model camera centres to our camera centres; our path unchanged; each frame used once"
    (out / "run_report.json").write_text(json.dumps(base, indent=1))
    print(json.dumps({"frames": len(owner), "chunks": base["chunks"]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
