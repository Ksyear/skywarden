"""Independent dense comparison (docs/PLAN_STATIC_MAP.md S6): OpenMVS DensifyPointCloud on the SAME fixed poses.

OpenMVS (AGPL-3.0) is used only as a separate command-line tool from its official release build; nothing is linked or
redistributed. The workspace is made from the frozen SfM model (frames outside the segment deregistered, images
undistorted by pycolmap) and imported with InterfaceCOLMAP, so camera poses are unchanged.

python -m mapping.openmvs_compare <workspace with scene.mvs> <out run dir> --bin <openmvs bin dir> [--masks <dir>] [-- extra args]
The output run dir holds dense_verified.ply (our PLY layout) and a minimal dense_report.json, so mapping.static_eval
can score it exactly like the other runs.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import numpy as np

from .export import PLY_DTYPE, write_ply


_T = {"float32": "<f4", "float": "<f4", "float64": "<f8", "double": "<f8", "uint8": "u1", "uchar": "u1",
      "int32": "<i4", "int": "<i4", "uint32": "<u4", "uint": "<u4", "int8": "i1", "char": "i1", "uint16": "<u2", "int16": "<i2"}


def read_openmvs_ply(path: Path):
    """Vertices of an OpenMVS point cloud (fixed scalar properties followed by per-vertex view lists)."""
    data = path.read_bytes()
    end = data.index(b"end_header\n") + len(b"end_header\n")
    n, fixed, lists = 0, [], []
    for ln in data[:end].decode("latin1").splitlines():
        q = ln.split()
        if q[:2] == ["element", "vertex"]:
            n = int(q[2])
        elif q[:2] == ["property", "list"]:
            lists.append((np.dtype(_T[q[2]]), np.dtype(_T[q[3]])))
        elif q and q[0] == "property":
            fixed.append((q[2], _T[q[1]]))
    fdt = np.dtype(fixed)
    out = np.zeros(n, fdt)
    off = end
    for i in range(n):
        out[i] = np.frombuffer(data, fdt, 1, off)[0]
        off += fdt.itemsize
        for cdt, idt in lists:
            k = int(np.frombuffer(data, cdt, 1, off)[0])
            off += cdt.itemsize + k * idt.itemsize
    return out


def run(ws: Path, out: Path, bindir: Path, masks: Path | None, extra: list[str]) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    tag = out.name
    cmd = [str(bindir.resolve() / "DensifyPointCloud"), "-i", "scene.mvs", "-o", f"{tag}.mvs", "--max-threads", "0", *extra]
    if masks is not None:
        cmd += ["--mask-path", str(masks.resolve()), "--ignore-mask-label", "0"]
    t0 = time.monotonic()
    r = subprocess.run(cmd, cwd=ws, capture_output=True, text=True)
    secs = round(time.monotonic() - t0, 1)
    (out / "openmvs.log").write_text(r.stdout + r.stderr)
    ply = ws / f"{tag}.ply"
    if r.returncode != 0 or not ply.exists():
        raise RuntimeError(f"DensifyPointCloud failed ({r.returncode}); see {out / 'openmvs.log'}")
    V = read_openmvs_ply(ply)
    pts = np.zeros(len(V), dtype=PLY_DTYPE)
    pts["x"], pts["y"], pts["z"] = V["x"], V["y"], V["z"]
    for c in ("red", "green", "blue"):
        if c in V.dtype.names:
            pts[c] = V[c]
    write_ply(out / "dense_verified.ply", pts)
    rep = {"session": "mqg_sumida_0025_0125", "model_rank": 0, "label": "OPENMVS_COMPARISON (same fixed poses; not a Skywarden layer)",
           "method": "OpenMVS v2.4.0 DensifyPointCloud (PatchMatch, CPU), official macOS arm64 release build",
           "command": cmd, "masks": str(masks) if masks else None, "config": {"work_width": 640},
           "dense_verified_points": int(len(V)), "seconds": secs, "views": [], "voxel": 0.030154332701281598}
    (out / "dense_report.json").write_text(json.dumps(rep, indent=1))
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.openmvs_compare")
    ap.add_argument("workspace")
    ap.add_argument("out")
    ap.add_argument("--bin", required=True)
    ap.add_argument("--masks")
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = argv[argv.index("--") + 1:] if "--" in argv else []
    a = ap.parse_args(argv[:argv.index("--")] if "--" in argv else argv)
    r = run(Path(a.workspace), Path(a.out), Path(a.bin), Path(a.masks) if a.masks else None, extra)
    print(json.dumps({k: r[k] for k in ("dense_verified_points", "seconds", "command")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
