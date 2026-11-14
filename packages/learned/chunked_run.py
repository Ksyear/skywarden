"""Resumable, memory-aware chunked learned-depth runs (project environment; the model runs in its own process).

Each chunk is one separate process of packages/learned/run_lingbot_map.py (external model, separate interpreter),
followed by mapping.learned_align. A chunk is marked done only after its frame list, weight hash, settings and files
check out; verified chunks are skipped on re-runs, failed ones are moved to <root>/incomplete/. Before a chunk starts,
macOS memory pressure is read and the run stops (cleanly) if free memory is below --min-free-pct.

python -m learned.chunked_run --session outputs/maps/mqg_sumida_0025_0125 --tag sumida --t-end 60 \
    [--chunk 8 --overlap 2 --max-chunks 1]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("outputs/experiments/learned_depth")
LEARNED_PY = Path("outputs/tools/venv_learned/bin/python")
MODEL = Path("outputs/tools/lingbot_map_src/lingbot-map/models/lingbot-map.pt")
WEIGHTS_SHA_PREFIX = "ee665103348e07e6"


def free_pct() -> int | None:
    try:
        q = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True, timeout=15).stdout
        return int(q.strip().splitlines()[-1].split(":")[-1].strip().rstrip("%"))
    except Exception:
        return None


def swap_used_mb() -> float | None:
    try:
        sw = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=10).stdout.split()
        return float(sw[sw.index("used") + 2].rstrip("M"))
    except Exception:
        return None


def plan(t_end: float, chunk: float, overlap: float, first: tuple[float, float] | None):
    out = [first] if first else []
    t0 = (first[1] - overlap) if first else 0.0
    while t0 < t_end - overlap:
        out.append((round(t0, 3), round(min(t0 + chunk, t_end), 3)))
        t0 += chunk - overlap
    return out


def verify(sd: Path, raw: Path, al: Path, t0: float, t1: float) -> dict:
    fr = {f["file"]: f["timestamp_s"] for f in json.loads((sd / "frames.json").read_text())}
    expect = sorted(n for n, t in fr.items() if t0 <= t <= t1)
    names = json.loads((raw / "names.json").read_text())
    rep = json.loads((raw / "run_report.json").read_text())
    ar = json.loads((al / "run_report.json").read_text())
    fit = ar["pose_convention_fit"][ar["pose_convention_used"]]
    problems = []
    if names != expect:
        problems.append("frame list differs from frames.json for the time range")
    if not rep.get("weights_sha256", "").startswith(WEIGHTS_SHA_PREFIX):
        problems.append("weight hash differs")
    if len(list(al.glob("depth_*.npy"))) != len(names) or len(list(al.glob("conf_*.npy"))) != len(names):
        problems.append("aligned depth/conf files missing")
    if fit["rms_over_path_length"] > 0.02:
        problems.append(f"alignment residual {fit['rms_over_path_length']:.3f} of path length > 0.02")
    return {"frames": len(names), "expected": len(expect), "weights_sha256": rep.get("weights_sha256"), "code_commit": rep.get("code_commit"),
            "device": rep.get("device"), "seconds_per_frame": rep.get("seconds_per_frame"), "peak_rss_gb": rep.get("peak_rss_gb"),
            "peak_mps_driver_gb": rep.get("peak_mps_driver_gb"), "memory": rep.get("memory"),
            "alignment_scale": fit["scale"], "alignment_rms_over_path": fit["rms_over_path_length"],
            "sparse_agreement_median": ar.get("sparse_agreement_median"), "problems": problems}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="learned.chunked_run")
    ap.add_argument("--session", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--t-end", type=float, required=True)
    ap.add_argument("--chunk", type=float, default=8.0)
    ap.add_argument("--overlap", type=float, default=2.0)
    ap.add_argument("--first", type=float, nargs=2, help="an already finished first chunk (e.g. 0 10) to keep in the plan")
    ap.add_argument("--max-chunks", type=int, default=99, help="stop after this many NEW chunks")
    ap.add_argument("--min-free-pct", type=int, default=35, help="do not start a chunk below this macOS free-memory percentage")
    a = ap.parse_args(argv)
    sd = Path(a.session)
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "incomplete").mkdir(exist_ok=True)
    chunks = plan(a.t_end, a.chunk, a.overlap, tuple(a.first) if a.first else None)
    state_path = ROOT / f"chunks_{a.tag}.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"session": str(sd), "tag": a.tag, "chunks": {}}
    state["plan"] = [list(c) for c in chunks]
    new = 0
    for t0, t1 in chunks:
        key = f"{t0:g}-{t1:g}"
        raw = ROOT / f"lingbot_v2_{a.tag}_t{t0:g}_{t1:g}"
        if a.first and (t0, t1) == tuple(a.first):  # reuse the earlier chunk directory name
            raw = ROOT / f"lingbot_v2_{a.tag}_c{int(t0)}"
        al = Path(str(raw) + "_aligned")
        done = raw / "chunk_done.json"
        if done.exists():
            state["chunks"][key] = json.loads(done.read_text())
            continue
        if raw.exists() and (raw / "run_report.json").exists() and al.exists():  # finished earlier, not yet verified
            v = verify(sd, raw, al, t0, t1)
            if not v["problems"]:
                v["status"] = "DONE"; done.write_text(json.dumps(v, indent=1)); state["chunks"][key] = v
                continue
        if new >= a.max_chunks:
            state["stopped"] = f"max-chunks {a.max_chunks} reached before {key}"; break
        fp, sw = free_pct(), swap_used_mb()
        if fp is not None and fp < a.min_free_pct:
            state["stopped"] = f"memory pressure: free {fp}% < {a.min_free_pct}% before {key} (swap used {sw} MB)"; break
        for d in (raw, al):
            if d.exists():
                shutil.move(str(d), str(ROOT / "incomplete" / f"{d.name}.{int(time.time())}"))
        t_start = time.monotonic()
        r = subprocess.run([str(LEARNED_PY), "packages/learned/run_lingbot_map.py", "--frames", str(sd / "frames"),
                            "--frames-json", str(sd / "frames.json"), "--t-range", str(t0), str(t1),
                            "--model", str(MODEL), "--out", str(raw)], capture_output=True, text=True)
        (ROOT / f"{raw.name}.log").write_text(r.stdout[-20000:] + r.stderr[-20000:])
        if r.returncode != 0:
            shutil.move(str(raw), str(ROOT / "incomplete" / f"{raw.name}.{int(time.time())}")) if raw.exists() else None
            state["chunks"][key] = {"status": "FAILED_INFERENCE", "returncode": r.returncode, "free_pct_before": fp}
            state["stopped"] = f"inference failed for {key}"; break
        r2 = subprocess.run([sys.executable, "-m", "mapping.learned_align", str(sd), str(raw), "--out", str(al)],
                            capture_output=True, text=True, env={"PYTHONPATH": "packages", "PATH": "/usr/bin:/bin"})
        if r2.returncode != 0:
            state["chunks"][key] = {"status": "FAILED_ALIGN", "stderr": r2.stderr[-2000:]}; state["stopped"] = f"align failed for {key}"; break
        v = verify(sd, raw, al, t0, t1)
        v.update(free_pct_before=fp, swap_used_mb_before=sw, wall_seconds=round(time.monotonic() - t_start, 1),
                 free_pct_after=free_pct(), swap_used_mb_after=swap_used_mb())
        if v["problems"]:
            v["status"] = "REJECTED"
            for d in (raw, al):
                shutil.move(str(d), str(ROOT / "incomplete" / f"{d.name}.{int(time.time())}"))
        else:
            v["status"] = "DONE"; done.write_text(json.dumps(v, indent=1))
        state["chunks"][key] = v
        new += 1
        state_path.write_text(json.dumps(state, indent=1))
    else:
        state.pop("stopped", None)
    state_path.write_text(json.dumps(state, indent=1))
    print(json.dumps({k: (v.get("status"), v.get("peak_rss_gb"), v.get("peak_mps_driver_gb"), v.get("seconds_per_frame")) for k, v in state["chunks"].items()} | {"stopped": state.get("stopped")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
