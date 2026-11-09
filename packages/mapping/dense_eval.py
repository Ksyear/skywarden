"""Measurement harness for docs/PLAN_DENSE_COVERAGE.md: where do floor / structure pixels get lost between depth
candidates and the fused cloud, and did a change add real surface without adding errors.

Inputs: a dense run made with `--diag-frames` (stage_<frame>.npz, depth_maps.npz, raw_kept.npz, dense_verified.ply,
dense_report.json) and the frozen region polygons. Outputs: stage_metrics_<run>.json, rows appended to
coverage_table.csv and same-view images sameview_<frame>_<run>.jpg (source / before / this run).

python -m mapping.dense_eval <session_dir> <run_dir> --name M0 --regions packages/mapping/regions/regions_sumida.json \
    --out outputs/evidence/dense_cov [--before-dir <run_dir> --before-name M0]
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

import cv2
import numpy as np
import pycolmap
from scipy.spatial import cKDTree

from .dense_cpu import REL_TOL, V_DISAGREE, V_KEPT, V_NO_DEPTH, V_OUTSIDE, V_VIEW_REJECTED, MAX_COST, MIN_MARGIN, \
    TEXTURE_VAR, _pose, camera_distortion
from .export import read_ply

CLASSES = ("floor", "floor_far", "structure", "wall")
HELDOUT_OFFSET = 5
HELDOUT_MAX_CANDIDATES = 30
KNN_K, KNN_R_VOX = 4, 2.0
ZBUF_VOX = 2.0


class Cam:
    """Camera of one registered image at the dense working resolution: pinhole K (undistorted stage maps) plus the
    distortion vector (projection into the stored, distorted frame where the regions were drawn)."""

    def __init__(self, rec, im, work_width):
        cam = rec.cameras[im.camera_id]
        self.scale = min(1.0, work_width / cam.width)
        self.K = np.asarray(cam.calibration_matrix(), float).copy()
        self.K[:2] *= self.scale
        self.dist = camera_distortion(cam)
        self.w, self.h = int(round(cam.width * self.scale)), int(round(cam.height * self.scale))
        self.R, self.t = _pose(im)

    def cam_coords(self, Xw):
        return (self.R @ Xw.T).T + self.t

    def pixels_undistorted(self, Xc):
        z = Xc[:, 2]
        uv = (self.K @ (Xc / np.maximum(z[:, None], 1e-9)).T).T[:, :2]
        return uv, z

    def pixels_distorted(self, Xc):
        z = Xc[:, 2]
        front = z > 1e-9
        uv = np.full((len(Xc), 2), -1.0)
        if front.any():
            p, _ = cv2.projectPoints(Xc[front].reshape(-1, 1, 3), np.zeros(3), np.zeros(3), self.K, self.dist)
            uv[front] = p.reshape(-1, 2)
        return uv, z


def region_masks(regions_frame, cam: Cam):
    """{class: (distorted mask, undistorted mask)} at working resolution."""
    W, H = regions_frame["width"], regions_frame["height"]
    sx, sy = cam.w / W, cam.h / H
    mx, my = cv2.initUndistortRectifyMap(cam.K, cam.dist, None, cam.K, (cam.w, cam.h), cv2.CV_32FC1)
    out = {}
    for cls in CLASSES:
        md = np.zeros((cam.h, cam.w), np.uint8)
        for r in regions_frame["regions"]:
            if r["class"] == cls:
                p = np.round(np.array(r["polygon"], float) * [sx, sy]).astype(np.int32)
                cv2.fillPoly(md, [p], 1)
        if md.any():
            mu = cv2.remap(md, mx, my, cv2.INTER_NEAREST)
            out[cls] = (md.astype(bool), mu.astype(bool))
    return out


def stage_metrics(npz, mask_u):
    """S1-S3 at the working resolution (undistorted), shares of region pixels; a pixel is counted once, first failing
    gate in the order no_candidate, cost, margin, texture, boundary_near, boundary_far."""
    fams = list(npz["families"])
    n_planes = dict(zip(fams, npz["n_planes"]))
    tot = int(mask_u.sum())
    conf = npz["conf"] & mask_u
    best = np.min(np.stack([npz[f"{f}_best"] for f in fams]), 0)
    cand = (best < 2.0 - 1e-6) & mask_u
    # rejection reasons from the fronto family (the only family in M0); with two families a pixel is rejected only
    # when both reject it, and the fronto reason is reported
    f = "fronto"
    b, s2, idx = npz[f"{f}_best"], npz[f"{f}_second"], npz[f"{f}_idx"]
    left = mask_u & ~conf
    reasons = {}
    for name, fail in (("no_candidate", ~(b < 2.0 - 1e-6)), ("cost", ~(b < MAX_COST)), ("margin", ~(s2 - b > MIN_MARGIN)),
                       ("texture", ~(npz["v0"] > TEXTURE_VAR)), ("boundary_near", idx == n_planes[f] - 1),
                       ("boundary_far", idx == 0)):
        hit = left & fail
        reasons[name] = round(float(hit.sum()) / max(1, tot), 4)
        left &= ~hit
    reasons["other"] = round(float(left.sum()) / max(1, tot), 4)
    code = npz["verify_code"]
    nconf = int(conf.sum())
    vr = {nm: round(float(((code == c) & conf).sum()) / max(1, nconf), 4)
          for nm, c in (("outside_partner", V_OUTSIDE), ("no_partner_depth", V_NO_DEPTH), ("disagree", V_DISAGREE),
                        ("view_rejected", V_VIEW_REJECTED))}
    kept = int(((code == V_KEPT) & conf).sum())
    res = {"region_px": tot, "cand": round(cand.sum() / max(1, tot), 4), "conf": round(nconf / max(1, tot), 4),
           "conf_rejections": reasons, "verif_of_conf": round(kept / max(1, nconf), 4),
           "verif": round(kept / max(1, tot), 4), "verify_drops_of_conf": vr}
    if "ground" in fams:
        res["ground_chosen_share_of_conf"] = round(float(((npz["chosen"] == 1) & conf).sum()) / max(1, nconf), 4)
    return res
# NOTE on plane order: inverse depths run from 1/dmax (index 0, far) to 1/dmin (last index, near).


def project_cover(cam: Cam, X, voxel, mask_d):
    """S5: 1-pixel splat of the whole fused cloud into the stored frame (distorted) at working resolution."""
    Xc = cam.cam_coords(X)
    uv, z = cam.pixels_distorted(Xc)
    u, v = np.floor(uv[:, 0]).astype(np.int64), np.floor(uv[:, 1]).astype(np.int64)
    ins = (z > 1e-9) & (u >= 0) & (u < cam.w) & (v >= 0) & (v < cam.h)
    hit = np.zeros((cam.h, cam.w), bool)
    hit[v[ins], u[ins]] = True
    zmin = np.full((cam.h, cam.w), np.inf)
    np.minimum.at(zmin, (v[ins], u[ins]), z[ins])
    local = cv2.erode(zmin.astype(np.float32), np.ones((3, 3), np.uint8), borderType=cv2.BORDER_REPLICATE)  # 3x3 min
    front_ok = z[ins] <= local[v[ins], u[ins]] + ZBUF_VOX * voxel
    hitz = np.zeros((cam.h, cam.w), bool)
    hitz[v[ins][front_ok], u[ins][front_ok]] = True
    return hit, hitz, ins, u, v, z


def heldout_inconsistency(rec, ims, raw, dmaps, verifier_sets, work_width, region_src=None):
    """Share of verified (pre-fusion) points whose depth disagrees > REL_TOL with a held-out view: the reference view
    nearest to k+-5 that is neither k nor in k's verifier set, sees the point and has its own confident depth there."""
    refs = [int(x) for x in dmaps["refs"]]
    cams = {k: Cam(rec, ims[k], work_width) for k in refs}
    checked = np.zeros(len(raw["ref"]), bool)
    bad = np.zeros(len(raw["ref"]), bool)
    for k in np.unique(raw["ref"]):
        k = int(k)
        sel = np.nonzero(raw["ref"] == k)[0]
        excl = set(verifier_sets.get(str(k), verifier_sets.get(k, []))) | {k}
        cand = sorted((j for j in refs if j not in excl), key=lambda j: min(abs(j - (k + HELDOUT_OFFSET)), abs(j - (k - HELDOUT_OFFSET))))
        todo = sel
        for j in cand[:HELDOUT_MAX_CANDIDATES]:
            if not len(todo):
                break
            c = cams[j]
            uv, z = c.pixels_undistorted(c.cam_coords(raw["P"][todo].astype(float)))
            u, v = np.round(uv[:, 0]).astype(np.int64), np.round(uv[:, 1]).astype(np.int64)
            ins = (z > 0) & (u >= 0) & (u < c.w) & (v >= 0) & (v < c.h)
            d = np.full(len(todo), np.nan)
            dm = dmaps[f"d{j}"]
            d[ins] = dm[v[ins], u[ins]]
            has = ~np.isnan(d)
            idx = todo[has]
            checked[idx] = True
            bad[idx] = np.abs(d[has] - z[has]) / np.maximum(z[has], 1e-9) > REL_TOL
            todo = todo[~has]
    out = {"points": int(len(checked)), "checked": int(checked.sum()),
           "inconsist": round(float(bad.sum()) / max(1, int(checked.sum())), 4),
           "inconsist_unchecked": round(1 - float(checked.mean()), 4) if len(checked) else None}
    if region_src is not None:
        for cls, m in region_src.items():
            out[f"inconsist_{cls}"] = round(float((bad & m).sum()) / max(1, int((checked & m).sum())), 4)
            out[f"checked_{cls}"] = int((checked & m).sum())
    return out


def floating(rec, ims, X, voxel, dmaps, work_width):
    tree = cKDTree(X)
    d, _ = tree.query(X, k=KNN_K + 1, distance_upper_bound=KNN_R_VOX * voxel)
    knn = np.isfinite(d[:, 1:]).sum(1) < KNN_K
    fs = np.zeros(len(X), bool)
    seen = np.zeros(len(X), bool)
    for j in (int(x) for x in dmaps["refs"]):
        c = Cam(rec, ims[j], work_width)
        uv, z = c.pixels_undistorted(c.cam_coords(X))
        u, v = np.round(uv[:, 0]).astype(np.int64), np.round(uv[:, 1]).astype(np.int64)
        ins = (z > 0) & (u >= 0) & (u < c.w) & (v >= 0) & (v < c.h)
        dv = np.full(len(X), np.nan)
        dv[ins] = dmaps[f"d{j}"][v[ins], u[ins]]
        has = ~np.isnan(dv)
        seen |= has
        fs |= has & (dv > (1 + REL_TOL) * z)
    return {"float_knn": round(float(knn.mean()), 4), "float_fs": round(float(fs.mean()), 4),
            "float_fs_of_checked": round(float(fs.sum()) / max(1, int(seen.sum())), 4), "fs_checked": int(seen.sum())}


def render(cam: Cam, X, C, size=(800, 450)):
    """1 pixel per point at working resolution, nearest point wins, then nearest-neighbour upscaled for viewing."""
    Xc = cam.cam_coords(X)
    uv, z = cam.pixels_distorted(Xc)
    u, v = np.floor(uv[:, 0]).astype(np.int64), np.floor(uv[:, 1]).astype(np.int64)
    ins = (z > 1e-9) & (u >= 0) & (u < cam.w) & (v >= 0) & (v < cam.h)
    o = np.argsort(-z[ins])
    img = np.full((cam.h, cam.w, 3), 24, np.uint8)
    img[v[ins][o], u[ins][o]] = C[ins][o][:, ::-1]
    return cv2.resize(img, size, interpolation=cv2.INTER_NEAREST), int(ins.sum())


def load_cloud(run_dir):
    D = read_ply(run_dir / "dense_verified.ply")
    return np.stack([D["x"], D["y"], D["z"]], 1).astype(np.float64), np.stack([D["red"], D["green"], D["blue"]], 1).astype(np.uint8)


def evaluate(sd: Path, run_dir: Path, name: str, regions: dict, out: Path, before: tuple[Path, str] | None = None) -> dict:
    t0 = time.monotonic()
    rep = json.loads((run_dir / "dense_report.json").read_text())
    mrep = json.loads((sd / "map_report.json").read_text())
    models = (mrep.get("reconstruction") or {}).get("models")
    rec = pycolmap.Reconstruction(str(sd / (models[rep["model_rank"]]["colmap_dir"] if models else mrep["reconstruction"]["chosen_model_dir"])))
    names = rep["diagnostics"]["image_names"]
    by_name = {im.name: im for im in rec.images.values()}
    ims = [by_name[n] for n in names]
    idx_of = {n: i for i, n in enumerate(names)}
    ww = rep["config"]["work_width"]
    voxel = rep["voxel"]
    X, C = load_cloud(run_dir)
    raw = dict(np.load(run_dir / "diag" / "raw_kept.npz"))
    dmaps = np.load(run_dir / "diag" / "depth_maps.npz")
    dmaps = {k: dmaps[k] for k in dmaps.files}
    Xb = Cb = None
    if before:
        Xb, Cb = load_cloud(before[0])
    frames_out = {}
    region_src = {cls: np.zeros(len(raw["ref"]), bool) for cls in CLASSES}
    for rf in regions["frames"]:
        fn = rf["frame"]
        k = idx_of[fn]
        cam = Cam(rec, ims[k], ww)
        masks = region_masks(rf, cam)
        st_path = run_dir / "diag" / f"stage_{fn}.npz"
        npz = dict(np.load(st_path)) if st_path.exists() else None
        hit, hitz, ins, u, v, z = project_cover(cam, X, voxel, np.ones((cam.h, cam.w), bool))
        fo = {"ref_index": k, "is_reference_view": bool(np.any(raw["ref"] == k)), "classes": {}}
        for cls, (md, mu) in masks.items():
            m = {}
            if npz is not None:
                m.update(stage_metrics(npz, mu))
            src = (raw["ref"] == k)
            if src.any():
                sm = np.zeros(len(src), bool)
                sm[src] = mu[raw["v"][src].astype(np.int64), raw["u"][src].astype(np.int64)]
                region_src[cls] |= sm
                m["fused_n_src"] = int(len(np.unique(raw["inv"][sm])))
            else:
                m["fused_n_src"] = 0
            m["fused_n_proj"] = int(md[v[ins], u[ins]].sum())
            m["cov1px"] = round(float((hit & md).sum()) / max(1, int(md.sum())), 4)
            m["cov1px_zbuf"] = round(float((hitz & md).sum()) / max(1, int(md.sum())), 4)
            fo["classes"][cls] = m
        frames_out[fn] = fo
        # same-view panel: source / before / this run, region outlines, captions
        src_img = cv2.imread(str(sd / "frames" / fn))
        panels = [cv2.resize(src_img, (800, 450), interpolation=cv2.INTER_AREA)]
        caps = [f"source {fn}"]
        if before:
            rb, nb = render(cam, Xb, Cb)
            panels.append(rb)
            caps.append(f"{before[1]}: DENSE_VERIFIED, {nb:,} pts in view")
        rn, nn_ = render(cam, X, C)
        panels.append(rn)
        caps.append(f"{name}: DENSE_VERIFIED, {nn_:,} pts in view")
        sx, sy = 800 / rf["width"], 450 / rf["height"]
        colr = {"floor": (0, 200, 255), "floor_far": (255, 200, 0), "structure": (0, 255, 0), "wall": (255, 0, 255)}
        for p, cap in zip(panels, caps):
            for r in rf["regions"]:
                cv2.polylines(p, [np.round(np.array(r["polygon"], float) * [sx, sy]).astype(np.int32)], True, colr[r["class"]], 1)
            cv2.rectangle(p, (0, 0), (800, 22), (0, 0, 0), -1)
            cv2.putText(p, cap, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
        cl = fo["classes"]
        foot = " | ".join(f"{c} cov1px {cl[c]['cov1px']:.3f}" for c in CLASSES if c in cl)
        strip = np.zeros((24, 800 * len(panels), 3), np.uint8)
        cv2.putText(strip, f"{name}: {foot}  (1 px per point at {cam.w}x{cam.h}, nearest-upscaled; outlines = frozen regions)",
                    (6, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
        cv2.imwrite(str(out / f"sameview_{fn.split('.')[0]}_{name}.jpg"), np.vstack([np.hstack(panels), strip]), [cv2.IMWRITE_JPEG_QUALITY, 92])
    vs = rep["diagnostics"]["verifier_sets"]
    inc = heldout_inconsistency(rec, ims, raw, dmaps, vs, ww, region_src)
    flo = floating(rec, ims, X, voxel, dmaps, ww)
    agrees = [r["sparse_agreement"] for r in rep["views"] if r.get("status") == "VERIFIED"]
    res = {"run": name, "run_dir": str(run_dir), "config": rep["config"], "t_range": rep.get("t_range"),
           "reference_views": rep["reference_views"], "views_verified": rep["views_verified"],
           "dense_verified_points": rep["dense_verified_points"], "dense_raw_kept_points": rep.get("dense_raw_kept_points"),
           "seconds_dense": rep["seconds"], "voxel": voxel, "frames": frames_out, "errors": {**inc, **flo},
           "sparse_agree": {"min_verified": min(agrees) if agrees else None, "median_verified": float(np.median(agrees)) if agrees else None},
           "forced": rep["diagnostics"]["forced"], "eval_seconds": None}
    res["eval_seconds"] = round(time.monotonic() - t0, 1)
    (out / f"stage_metrics_{name}.json").write_text(json.dumps(res, indent=1))
    table = out / "coverage_table.csv"
    new = not table.exists()
    with table.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["run", "frame", "class", "metric", "value"])
        for fn, fo in frames_out.items():
            for cls, m in fo["classes"].items():
                for key, val in m.items():
                    if isinstance(val, dict):
                        for k2, v2 in val.items():
                            w.writerow([name, fn, cls, f"{key}.{k2}", v2])
                    else:
                        w.writerow([name, fn, cls, key, val])
        for key, val in res["errors"].items():
            w.writerow([name, "*", "*", key, val])
        for key in ("views_verified", "dense_verified_points", "seconds_dense"):
            w.writerow([name, "*", "*", key, res[key]])
        w.writerow([name, "*", "*", "sparse_agree_median", res["sparse_agree"]["median_verified"]])
    return res


def same_maps_fs(sd: Path, run_dir: Path, before_dir: Path) -> dict:
    """Supplementary (docs/PLAN_DENSE_COVERAGE.md A5): float_fs of both clouds against the SAME depth maps (this run's),
    so a change that adds reference views is not judged with more checks than its before."""
    rep = json.loads((run_dir / "dense_report.json").read_text())
    mrep = json.loads((sd / "map_report.json").read_text())
    models = (mrep.get("reconstruction") or {}).get("models")
    rec = pycolmap.Reconstruction(str(sd / (models[rep["model_rank"]]["colmap_dir"] if models else mrep["reconstruction"]["chosen_model_dir"])))
    by_name = {im.name: im for im in rec.images.values()}
    ims = [by_name[n] for n in rep["diagnostics"]["image_names"]]
    dm = np.load(run_dir / "diag" / "depth_maps.npz")
    dm = {k: dm[k] for k in dm.files}
    out = {}
    for tag, d in (("after", run_dir), ("before", before_dir)):
        X, _ = load_cloud(d)
        out[tag] = floating(rec, ims, X, rep["voxel"], dm, rep["config"]["work_width"])["float_fs"]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.dense_eval")
    ap.add_argument("session_dir")
    ap.add_argument("run_dir")
    ap.add_argument("--name", required=True)
    ap.add_argument("--regions", required=True)
    ap.add_argument("--out", default="outputs/evidence/dense_cov")
    ap.add_argument("--before-dir")
    ap.add_argument("--before-name")
    ap.add_argument("--frames", nargs="*", help="evaluate only these region frames (short segment runs)")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    regions = json.loads(Path(a.regions).read_text())
    if a.frames:
        regions = {**regions, "frames": [f for f in regions["frames"] if f["frame"] in a.frames]}
    r = evaluate(Path(a.session_dir), Path(a.run_dir), a.name, regions, out,
                 (Path(a.before_dir), a.before_name) if a.before_dir else None)
    if a.before_dir:
        r["errors"]["float_fs_same_maps"] = same_maps_fs(Path(a.session_dir), Path(a.run_dir), Path(a.before_dir))
        (out / f"stage_metrics_{a.name}.json").write_text(json.dumps(r, indent=1))
    short = {fn: {c: {k: m.get(k) for k in ("cand", "conf", "verif", "fused_n_src", "fused_n_proj", "cov1px", "cov1px_zbuf")}
                  for c, m in fo["classes"].items()} for fn, fo in r["frames"].items()}
    print(json.dumps({"run": r["run"], "points": r["dense_verified_points"], "views_verified": r["views_verified"],
                      "seconds": r["seconds_dense"], "errors": r["errors"], "sparse_agree": r["sparse_agree"], "frames": short}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
