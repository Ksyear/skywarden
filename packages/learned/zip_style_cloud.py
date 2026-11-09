"""Reproduce the user's earlier LingBot-Map demo output as a point cloud we can compare against.

The demo unprojects every pixel of every frame with the model's OWN cameras and shows points whose confidence is
>= 1.5 - no geometric verification, no sky/person removal. We do the same from the runner's raw predictions, then
bring the cloud into our SfM frame with one Sim3 between the model's camera centres and ours for the same video
frames (demo frame k = source frame round(k * src_fps / demo_fps)). Our path is the reference and is not changed.

python -m learned.zip_style_cloud <session_dir> <runner_out_dir> --src-fps 59.8246 --demo-fps 1 --out <dir>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pycolmap

from mapping.dense_cpu import _pose
from mapping.export import PLY_DTYPE, write_ply
from mapping.learned_align import umeyama

CONF = 1.5


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="learned.zip_style_cloud")
    ap.add_argument("session_dir"); ap.add_argument("model_out")
    ap.add_argument("--src-fps", type=float, required=True); ap.add_argument("--demo-fps", type=float, default=1.0)
    ap.add_argument("--frames-dir", required=True, help="the demo's input frames (for colours)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    sd, md, out = Path(a.session_dir), Path(a.model_out), Path(a.out)
    P = np.load(md / "predictions.npz")
    names = json.loads((md / "names.json").read_text())
    rec = pycolmap.Reconstruction(str(sd / "sparse" / "0"))
    by_src = {}
    frames = json.loads((sd / "frames.json").read_text())
    by_name = {im.name: im for im in rec.images.values()}
    for f in frames:
        if f["file"] in by_name:
            by_src[f["source_index"]] = by_name[f["file"]]
    interval = max(1, round(a.src_fps / a.demo_fps))
    T = P["c2w"]
    pairs = [(i, by_src[int(Path(n).stem) * interval]) for i, n in enumerate(names) if int(Path(n).stem) * interval in by_src]
    C_m = np.array([T[i, :3, 3] for i, _ in pairs])
    C_o = np.array([-_pose(im)[0].T @ _pose(im)[1] for _, im in pairs])
    s, R, t = umeyama(C_m, C_o)
    res = np.linalg.norm((s * (R @ C_m.T)).T + t - C_o, axis=1)
    path = float(np.sum(np.linalg.norm(np.diff(C_o, axis=0), axis=1)))
    import cv2
    D, Cf, K = P["depth"], P["depth_conf"], P["intrinsic"]
    H, W = D.shape[1:]
    vv, uu = np.mgrid[0:H, 0:W]
    pts, cols = [], []
    for i, n in enumerate(names):
        m = (Cf[i] >= CONF) & np.isfinite(D[i]) & (D[i] > 0)
        z = D[i][m]; u = uu[m]; v = vv[m]
        Xc = np.stack([(u - K[i, 0, 2]) / K[i, 0, 0] * z, (v - K[i, 1, 2]) / K[i, 1, 1] * z, z], 1)
        Xw_m = (T[i, :3, :3] @ Xc.T).T + T[i, :3, 3]
        pts.append((s * (R @ Xw_m.T)).T + t)
        img = cv2.resize(cv2.imread(str(Path(a.frames_dir) / n)), (W, H), interpolation=cv2.INTER_AREA)
        cols.append(img[v, u][:, ::-1])
    X = np.concatenate(pts); Cc = np.concatenate(cols)
    arr = np.zeros(len(X), dtype=PLY_DTYPE)
    arr["x"], arr["y"], arr["z"] = X.T; arr["red"], arr["green"], arr["blue"] = Cc.T
    out.mkdir(parents=True, exist_ok=True)
    write_ply(out / "dense_verified.ply", arr)
    rep = {"session": sd.name, "model_rank": 0, "label": "ZIP-STYLE REFERENCE: every pixel with model confidence >= 1.5, model cameras, no verification, no masks (as in the earlier demo run)",
           "dense_verified_points": int(len(X)), "config": {"work_width": 640}, "views": [], "voxel": None,
           "frames": len(names), "matched_to_our_frames": len(pairs), "sim3_scale": s,
           "sim3_rms_over_path_length": float(np.sqrt(np.mean(res ** 2)) / max(path, 1e-9))}
    (out / "dense_report.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps({k: rep[k] for k in ("dense_verified_points", "frames", "matched_to_our_frames", "sim3_rms_over_path_length")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
