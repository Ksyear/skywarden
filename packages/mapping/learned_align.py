"""Bring a learned model's per-frame depth into our SfM frame without touching our camera path.

The model predicts its own cameras in its own scale. We fit one similarity (Umeyama Sim3) from the model's camera
centres to OUR camera centres for the same frames, report the residual (a sanity check of the model's path against
ours), and scale the model depth by the Sim3 scale. Both pose conventions are tried (decoded pose as camera-to-world,
or its inverse) and the one that fits our path is recorded. Our poses stay the reference; the model's poses are
only used for this check and are never written into the map.

python -m mapping.learned_align <session_dir> <model_out_dir> --out <dir for mapping.learned_fuse>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import pycolmap

from .dense_cpu import _pose


def umeyama(src: np.ndarray, dst: np.ndarray):
    """s, R, t minimising |dst - (s R src + t)|."""
    mu_s, mu_d = src.mean(0), dst.mean(0)
    a, b = src - mu_s, dst - mu_d
    U, S, Vt = np.linalg.svd(b.T @ a / len(src))
    D = np.eye(3)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        D[2, 2] = -1
    R = U @ D @ Vt
    s = float(np.trace(np.diag(S) @ D) / (a ** 2).sum(1).mean())
    return s, R, mu_d - s * R @ mu_s


def run(sd: Path, md: Path, out: Path) -> dict:
    P = np.load(md / "predictions.npz")
    names = json.loads((md / "names.json").read_text())
    rep_m = json.loads((md / "run_report.json").read_text())
    rec = pycolmap.Reconstruction(str(sd / "sparse" / "0"))
    by = {im.name: im for im in rec.images.values()}
    C_ours = np.array([-_pose(by[n])[0].T @ _pose(by[n])[1] for n in names])
    T = P["c2w"]
    fits = {}
    for conv in ("c2w", "inverse"):
        if conv == "c2w":
            C_m = T[:, :3, 3]
        else:
            C_m = np.array([-T[i, :3, :3].T @ T[i, :3, 3] for i in range(len(T))])
        s, R, t = umeyama(C_m, C_ours)
        res = np.linalg.norm((s * (R @ C_m.T)).T + t - C_ours, axis=1)
        path = float(np.sum(np.linalg.norm(np.diff(C_ours, axis=0), axis=1)))
        fits[conv] = {"scale": s, "rms_residual": float(np.sqrt(np.mean(res ** 2))), "rms_over_path_length": float(np.sqrt(np.mean(res ** 2)) / max(path, 1e-9))}
    conv = min(fits, key=lambda k: fits[k]["rms_residual"])
    s = fits[conv]["scale"]
    depth = P["depth"] * s
    conf = P["depth_conf"]
    H, W = depth.shape[1:]
    cam = rec.cameras[by[names[0]].camera_id]
    K = np.asarray(cam.calibration_matrix(), float)
    sx, sy = W / cam.width, H / cam.height
    intr = {"fx": K[0, 0] * sx, "fy": K[1, 1] * sy, "cx": K[0, 2] * sx, "cy": K[1, 2] * sy, "width": W, "height": H}
    Km = P["intrinsic"]
    focal_ratio = float(np.median(Km[:, 0, 0] / intr["fx"]))
    out.mkdir(parents=True, exist_ok=True)
    pts = {pid: p.xyz for pid, p in rec.points3D.items()}
    agree = []
    for i, n in enumerate(names):
        np.save(out / f"depth_{n[:-4]}.npy", depth[i].astype(np.float32))
        np.save(out / f"conf_{n[:-4]}.npy", conf[i].astype(np.float32))
        R_, t_ = _pose(by[n])
        ids = [p2.point3D_id for p2 in by[n].points2D if p2.has_point3D()]
        X = np.array([pts[j] for j in ids]); Xc = (R_ @ X.T).T + t_
        z = Xc[:, 2]; u = np.round(intr["fx"] * Xc[:, 0] / z + intr["cx"]).astype(int); v = np.round(intr["fy"] * Xc[:, 1] / z + intr["cy"]).astype(int)
        ins = (z > 0) & (u >= 0) & (u < W) & (v >= 0) & (v < H)
        d = depth[i][v[ins], u[ins]]
        agree.append(float(np.mean(np.abs(d - z[ins]) / z[ins] <= 0.10)) if ins.sum() else 0.0)
    rep = {"model": rep_m.get("model"), "licence": rep_m.get("licence"), "code_commit": rep_m.get("code_commit"),
           "weights_sha256": rep_m.get("weights_sha256"), "device": rep_m.get("device"),
           "seconds_per_frame": rep_m.get("seconds_per_frame"), "frames": len(names),
           "intrinsics": intr, "pose_convention_fit": fits, "pose_convention_used": conv,
           "alignment": f"one Sim3 from the model's camera centres to ours (scale {s:.4f}); our path unchanged",
           "model_focal_over_ours": round(focal_ratio, 4),
           "sparse_agreement_per_frame": dict(zip(names, [round(a, 3) for a in agree])),
           "sparse_agreement_median": round(float(np.median(agree)), 3)}
    (out / "run_report.json").write_text(json.dumps(rep, indent=1))
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.learned_align")
    ap.add_argument("session_dir"); ap.add_argument("model_out"); ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    r = run(Path(a.session_dir), Path(a.model_out), Path(a.out))
    print(json.dumps({k: r[k] for k in ("pose_convention_fit", "pose_convention_used", "model_focal_over_ours", "sparse_agreement_median", "seconds_per_frame")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
