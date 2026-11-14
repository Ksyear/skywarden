"""Compare the checkout's own demo (run once over all frames on the Mac) with our chunked reproduction.

Inputs: the demo's saved predictions (port_zip_demo_mac --save_npz) and our chunk runs of the same frames.
  1. per frame: depth agreement single-pass vs chunk (after one scale per chunk: each run is up to scale),
     share of confident (>= 1.5) pixels in each
  2. both clouds brought into our SfM frame (one Sim3 to our camera path each), then nearest-neighbour distances
     between them (as a share of our path length), and to our multi-view map if given
Writes compare.json and the single-pass cloud (dense_verified.ply, as the app layer) to --out.

python -m learned.compare_zip_demo --session outputs/maps/mqg_corridor --demo-npz <predictions.npz> \
    --chunks <chunk dirs...> --frames-dir <demo frames> --src-fps 59.8246 [--mvs <ply>] --out <dir>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

from learned import zip_style_cloud
from mapping.export import read_ply

CONF = 1.5


def _xyz(path, n=400_000, seed=0):
    M = read_ply(path); X = np.stack([M["x"], M["y"], M["z"]], 1).astype(np.float64)
    if len(X) > n:
        X = X[np.random.default_rng(seed).choice(len(X), n, replace=False)]
    return X


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="learned.compare_zip_demo")
    ap.add_argument("--session", required=True); ap.add_argument("--demo-npz", required=True)
    ap.add_argument("--chunks", nargs="*", default=[]); ap.add_argument("--frames-dir", required=True)
    ap.add_argument("--src-fps", type=float, required=True); ap.add_argument("--mvs"); ap.add_argument("--out", required=True)
    ap.add_argument("--chunked-cloud", help="our earlier chunked cloud (dense_verified.ply) to compare against")
    a = ap.parse_args(argv)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    P = np.load(a.demo_npz)
    names = [Path(str(p)).name for p in P["paths"]]
    D = P["depth"].reshape(P["depth"].shape[:3]); C = P["depth_conf"].reshape(D.shape)
    # the demo's postprocess stores WORLD-TO-CAMERA poses under "extrinsic" (its viewer unprojects with them as such);
    # zip_style_cloud wants camera-to-world, so invert (checked: camera centres then fit our path 6-8x better)
    E = P["extrinsic"][:, :3, :4].astype(np.float64)
    Rt = np.transpose(E[:, :, :3], (0, 2, 1))
    c2w = np.concatenate([Rt, -(Rt @ E[:, :, 3:4])], axis=2)
    sp = out / "single_pass"; sp.mkdir(exist_ok=True)
    np.savez(sp / "predictions.npz", depth=D, depth_conf=C, c2w=c2w.astype(np.float32), intrinsic=P["intrinsic"])
    (sp / "names.json").write_text(json.dumps(names))
    zip_style_cloud.main([a.session, str(sp), "--src-fps", str(a.src_fps), "--frames-dir", a.frames_dir, "--out", str(out), "--stride", "2"])
    rep = json.loads((out / "dense_report.json").read_text())
    rep["label"] = "DEMO AS SHIPPED, RUN ON THIS MAC: all frames in one pass, every pixel with confidence >= 1.5, model cameras, no verification"
    rep["note"] = "the checkout's demo.py with Mac-only edits (Apple GPU, bfloat16 trunk, cache release per frame); one Sim3 to our path"
    (out / "dense_report.json").write_text(json.dumps(rep, indent=1))
    idx = {n: i for i, n in enumerate(names)}
    frames, seen = [], set()
    for cd in map(Path, a.chunks):
        Q = np.load(cd / "predictions.npz"); cn = json.loads((cd / "names.json").read_text())
        both = [(j, idx[n]) for j, n in enumerate(cn) if n in idx and n not in seen]
        if not both:
            continue
        r = [np.median((Q["depth"][j] / D[i])[(Q["depth_conf"][j] >= CONF) & (C[i] >= CONF)]) for j, i in both]
        s = float(np.median(r))  # one scale per chunk
        for j, i in both:
            n = cn[j]; seen.add(n)
            m = (Q["depth_conf"][j] >= CONF) & (C[i] >= CONF)
            rel = np.abs(Q["depth"][j][m] / s - D[i][m]) / D[i][m]
            frames.append({"frame": n, "chunk": cd.name, "median_rel_depth_diff": round(float(np.median(rel)), 4),
                           "share_within_5pct": round(float((rel <= 0.05).mean()), 3),
                           "confident_px_single": round(float((C[i] >= CONF).mean()), 3), "confident_px_chunk": round(float((Q["depth_conf"][j] >= CONF).mean()), 3)})
    res = {"frames_compared": len(frames), "per_frame": frames,
           "median_rel_depth_diff": round(float(np.median([f["median_rel_depth_diff"] for f in frames])), 4) if frames else None,
           "confident_share_single": round(float((C >= CONF).mean()), 3),
           "single_pass_fit": rep["chunks"]}
    import pycolmap
    from mapping.dense_cpu import _pose
    rec = pycolmap.Reconstruction(str(Path(a.session) / "sparse" / "0"))
    cams = np.array([-_pose(im)[0].T @ _pose(im)[1] for im in sorted(rec.images.values(), key=lambda im: im.name)])
    L = float(np.sum(np.linalg.norm(np.diff(cams, axis=0), axis=1)))
    S = _xyz(out / "dense_verified.ply")
    def nn(A, B):
        d, _ = cKDTree(B).query(A, k=1)
        return {"median_over_path": round(float(np.median(d)) / L, 4), "p90_over_path": round(float(np.percentile(d, 90)) / L, 4)}
    if a.chunked_cloud:
        Cc = _xyz(a.chunked_cloud)
        res["single_to_chunked"] = nn(S, Cc); res["chunked_to_single"] = nn(Cc, S)
    if a.mvs:
        Mv = _xyz(a.mvs)
        res["mvs_to_single"] = nn(Mv, S)
        if a.chunked_cloud:
            res["mvs_to_chunked"] = nn(Mv, Cc)
    res["path_length"] = L
    (out / "compare.json").write_text(json.dumps(res, indent=1))
    print(json.dumps({k: v for k, v in res.items() if k != "per_frame"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
