"""One command for a map made with the LingBot-Map model (the user's checkout), as in its demo, on this Mac.

Runs the model on a stored session in memory-safe chunks (learned.chunked_run), keeps every chunk that finished (for
display, alignment-rejected chunks are kept too and listed), joins them on our camera path with one Sim3 per chunk and
writes the layer <session>/dense_zipstyle (every confident pixel). Our camera path is the reference and is unchanged.

PYTHONPATH=packages .venv/bin/python -m learned.model_map outputs/maps/<session> [--stride 2]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path("outputs/experiments/learned_depth")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="learned.model_map")
    ap.add_argument("session"); ap.add_argument("--stride", type=int, default=2)
    a = ap.parse_args(argv)
    sd = Path(a.session); tag = sd.name
    frames = json.loads((sd / "frames.json").read_text())
    t_end = max(f["timestamp_s"] for f in frames)
    meta = json.loads((sd / "map_report.json").read_text())["input"]
    env = {"PYTHONPATH": "packages", "PATH": "/usr/bin:/bin:/usr/sbin"}
    r = subprocess.run([sys.executable, "-m", "learned.chunked_run", "--session", str(sd), "--tag", tag, "--t-end", f"{t_end:.3f}"], env=env)
    st = json.loads((ROOT / f"chunks_{tag}.json").read_text())
    dirs = []
    for t0, t1 in st["plan"]:
        d = ROOT / f"lingbot_v2_{tag}_t{t0:g}_{t1:g}"
        if (d / "predictions.npz").exists():
            dirs.append(str(d)); continue
        inc = sorted((ROOT / "incomplete").glob(f"lingbot_v2_{tag}_t{t0:g}_{t1:g}.*"))
        inc = [p for p in inc if "aligned" not in p.name and (p / "predictions.npz").exists()]
        if inc:
            dirs.append(str(inc[-1]))
    if not dirs:
        print("no finished chunk; see", ROOT / f"chunks_{tag}.json"); return 1
    r = subprocess.run([sys.executable, "-m", "learned.zip_style_cloud", str(sd), *dirs, "--src-fps", str(meta.get("nominal_fps") or 30),
                        "--frames-dir", str(sd / "frames"), "--stride", str(a.stride), "--out", str(sd / "dense_zipstyle")], env=env)
    rep = json.loads((sd / "dense_zipstyle" / "dense_report.json").read_text()); rep["session"] = tag; rep["stopped"] = st.get("stopped")
    (sd / "dense_zipstyle" / "dense_report.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps({"layer": str(sd / "dense_zipstyle"), "points": rep["dense_verified_points"], "frames": rep["frames"], "stopped": st.get("stopped")}, indent=1))
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
