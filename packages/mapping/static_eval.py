"""Static-map evaluation (docs/PLAN_STATIC_MAP.md): the dense_eval harness plus sky / person / boundary / streak
measures, with every run judged against the SAME frozen depth maps.

python -m mapping.static_eval <session_dir> <run_dir> --name S1_seg --regions packages/mapping/regions/regions_sumida_v2.json \
    --judge-maps outputs/experiments/dense_cov/ESVFN2x25_full/diag/depth_maps.npz --out outputs/evidence/static_map \
    [--frames f000240.jpg ...] [--compare B1=<run_dir> B2=<run_dir>]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from pathlib import Path

import cv2
import numpy as np
import pycolmap

from .dense_cpu import REL_TOL, View, ground_plane
from .dense_eval import Cam, floating, heldout_inconsistency, load_cloud, region_masks, render, stage_metrics
from .static_masks import MEAN, MODEL_SHA256, STD

CLASSES = ("floor", "floor_far", "structure", "wall", "far")
WEIGHTS = Path("data/models/static/skywater_fp32.onnx")
BAND_PX_1600 = 20          # boundary band width in 1600-px units
PERSON_CONF = 0.5
BORDER_PX = 3
STREAK_COLS, CONTROL_COLS, STREAK_ROWS = (576, 590), ((540, 554), (600, 614)), (190, 300)  # f000240, 640x360 px (plan section 2)
BEHIND = 1.10  # "more than 10 % behind the fitted floor depth"


def labels_for(frame_path: Path, cache: Path) -> dict:
    """Undilated sky / person label maps at full resolution from the segmentation model, plus YOLOX person boxes."""
    cache.mkdir(parents=True, exist_ok=True)
    c = cache / (frame_path.name + ".npz")
    if c.exists():
        z = np.load(c, allow_pickle=False)
        return {"sky": z["sky"], "person_seg": z["person_seg"], "boxes": z["boxes"]}
    if hashlib.sha256(WEIGHTS.read_bytes()).hexdigest() != MODEL_SHA256:
        raise ValueError("unexpected segmentation weights")
    img = cv2.imread(str(frame_path))
    net = cv2.dnn.readNetFromONNX(str(WEIGHTS))
    x = (cv2.cvtColor(cv2.resize(img, (384, 384)), cv2.COLOR_BGR2RGB).astype(np.float32) / 255 - MEAN) / STD
    net.setInput(np.ascontiguousarray(x.transpose(2, 0, 1)[None]))
    lab = cv2.resize(net.forward()[0].argmax(0).astype(np.uint8), img.shape[1::-1], interpolation=cv2.INTER_NEAREST)
    from vision.person_detector import make_detector
    dets = [d for d in make_detector("yolox_nano").detect(img) if d.score >= PERSON_CONF]
    boxes = np.array([list(d.bbox_xyxy) + [d.score] for d in dets], float).reshape(-1, 5)
    np.savez_compressed(c, sky=lab == 1, person_seg=lab == 3, boxes=boxes)
    return {"sky": lab == 1, "person_seg": lab == 3, "boxes": boxes}


def _project(cam: Cam, X):
    uv, z = cam.pixels_distorted(cam.cam_coords(X))
    u, v = np.floor(uv[:, 0]).astype(np.int64), np.floor(uv[:, 1]).astype(np.int64)
    ins = (z > 1e-9) & (u >= 0) & (u < cam.w) & (v >= 0) & (v < cam.h)
    return u, v, z, ins


def fs_flags(rec, ims, X, maps, ww):
    """Per-point free-space flag: some judge view sees past the point by more than REL_TOL."""
    flag = np.zeros(len(X), bool)
    for j in (int(x) for x in maps["refs"]):
        c = Cam(rec, ims[j], ww)
        uv, z = c.pixels_undistorted(c.cam_coords(X))
        u, v = np.round(uv[:, 0]).astype(np.int64), np.round(uv[:, 1]).astype(np.int64)
        ins = (z > 0) & (u >= 0) & (u < c.w) & (v >= 0) & (v < c.h)
        d = np.full(len(X), np.nan)
        d[ins] = maps[f"d{j}"][v[ins], u[ins]]
        flag |= ~np.isnan(d) & (d > (1 + REL_TOL) * z)
    return flag


def frame_metrics(sd, rec, im, rf, cam: Cam, X, lab, clean_flags=None, ims=None):
    """Coverage per class, boundary band, sky false points, person classification for one eval frame."""
    small = lambda m: cv2.resize(m.astype(np.uint8), (cam.w, cam.h), interpolation=cv2.INTER_NEAREST).astype(bool)  # noqa: E731
    sky = small(lab["sky"])
    person = lab["person_seg"].copy()
    for x1, y1, x2, y2, _ in lab["boxes"]:
        person[int(max(0, y1)):int(y2), int(max(0, x1)):int(x2)] = True
    person = small(person)
    masks = region_masks(rf, cam)
    u, v, z, ins = _project(cam, X)
    hit = np.zeros((cam.h, cam.w), bool)
    hit[v[ins], u[ins]] = True
    out = {"classes": {}}
    band_r = max(1, int(round(BAND_PX_1600 * cam.w / rf["width"])))
    near_sky = cv2.dilate(sky.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * band_r + 1, 2 * band_r + 1))).astype(bool)
    solid = np.zeros_like(sky)
    for cls, (md, _) in masks.items():
        md2 = md & ~sky & ~person  # coverage counts static, visible pixels of the region only
        out["classes"][cls] = {"px": int(md2.sum()), "cov1px": round(float((hit & md2).sum()) / max(1, int(md2.sum())), 4),
                               "cov1px_region": round(float((hit & md).sum()) / max(1, int(md.sum())), 4),  # incl. sky/person px (dense_eval definition)
                               "fused_n_proj": int(md2[v[ins], u[ins]].sum())}
        if cls in ("structure", "wall", "far"):
            solid |= md2
    band = solid & near_sky
    out["boundary_band"] = {"px": int(band.sum()), "cov1px": round(float((hit & band).sum()) / max(1, int(band.sum())), 4)}
    n_view = int(ins.sum())
    in_sky = ins.copy()
    in_sky[ins] = sky[v[ins], u[ins]]
    out["sky_false"] = {"points": int(in_sky.sum()), "share_of_points_in_view": round(float(in_sky.sum()) / max(1, n_view), 5)}
    # person pixels: compare point depth with the person's depth (box bottom-centre on the fitted ground plane)
    pc = {"background": 0, "person_surface": 0, "front_floater": 0, "unclassified": 0, "persons": int(len(lab["boxes"]))}
    if len(lab["boxes"]):
        vw = View(rec, im, sd / "frames", cam.scale)
        gp = ground_plane(vw, rec)
        for x1, y1, x2, y2, _ in lab["boxes"]:
            s = cam.scale
            bx = np.zeros((cam.h, cam.w), bool)
            bx[int(max(0, y1 * s)):int(y2 * s), int(max(0, x1 * s)):int(x2 * s)] = True
            sel = ins.copy()
            sel[ins] = bx[v[ins], u[ins]]
            if gp is None:
                pc["unclassified"] += int(sel.sum()); continue
            n, h = gp[0], gp[1]
            foot = np.linalg.inv(cam.K) @ np.array([(x1 + x2) / 2 * s, y2 * s, 1.0])
            zp = h / float(n @ foot) if n @ foot > 1e-6 else np.inf
            zz = z[sel]
            pc["background"] += int((zz > 1.15 * zp).sum())
            pc["person_surface"] += int(((zz >= 0.85 * zp) & (zz <= 1.15 * zp)).sum())
            pc["front_floater"] += int((zz < 0.85 * zp).sum())
    out["person_points"] = pc
    out["points_in_view"] = n_view
    # supplementary (A2): coverage by points that agree with the observed surface
    if clean_flags is not None:
        hc = np.zeros((cam.h, cam.w), bool)
        ok = ins & ~clean_flags
        hc[v[ok], u[ok]] = True
        for cls, (md, _) in masks.items():
            md2 = md & ~sky & ~person
            out["classes"][cls]["cov1px_clean"] = round(float((hc & md2).sum()) / max(1, int(md2.sum())), 4)
        out["boundary_band"]["cov1px_clean"] = round(float((hc & band).sum()) / max(1, int(band.sum())), 4)
    vw = View(rec, im, sd / "frames", cam.scale)
    gp = ground_plane(vw, rec)
    if gp is not None and "floor" in masks:
        n, h = gp[0], gp[1]
        md2 = masks["floor"][0] & ~sky & ~person
        sel = ins.copy()
        sel[ins] = md2[v[ins], u[ins]]
        ray = np.linalg.inv(cam.K) @ np.stack([u[sel] + 0.5, v[sel] + 0.5, np.ones(sel.sum())])
        zg = h / np.maximum(n @ ray, 1e-6)
        on = np.abs(z[sel] - zg) <= 0.15 * zg
        hs = np.zeros((cam.h, cam.w), bool)
        hs[v[sel][on], u[sel][on]] = True
        out["classes"]["floor"]["cov1px_on_ground"] = round(float(hs.sum()) / max(1, int(md2.sum())), 4)
        out["classes"]["floor"]["points_off_ground_share"] = round(1 - float(on.mean()) if sel.any() else 0.0, 4)
    return out, hit, sky, person


def behind_floor_shares(sd, rec, im, cam, X):
    v = View(rec, im, sd / "frames", cam.scale)
    gp = ground_plane(v, rec)
    if gp is None:
        return None
    n, h = gp[0], gp[1]
    uu, vv, z, ins = _project(cam, X)
    out = {}
    for name, spans in (("streak_cols", (STREAK_COLS,)), ("control_cols", CONTROL_COLS)):
        colsel = np.zeros(len(uu), bool)
        for c0, c1 in spans:
            colsel |= (uu >= c0) & (uu <= c1)
        sel = ins & colsel & (vv >= STREAK_ROWS[0]) & (vv < STREAK_ROWS[1])
        ray = np.linalg.inv(cam.K) @ np.stack([uu[sel] + 0.5, vv[sel] + 0.5, np.ones(sel.sum())])
        zg = h / (n @ ray)
        out[name] = {"points": int(sel.sum()), "behind_floor_share": round(float(np.mean(z[sel] > BEHIND * zg)) if sel.any() else 0.0, 4)}
    return out


def _judge(path, idx_of):
    J = np.load(path)
    m = {"refs": np.array([idx_of[str(nm)] for nm in J["names"]], np.int32)}
    for r_old, nm in zip(J["refs"], J["names"]):
        m[f"d{idx_of[str(nm)]}"] = J[f"d{int(r_old)}"]
    return m


def evaluate(sd: Path, run_dir: Path, name: str, regions: dict, out: Path, judge: Path, compare: dict,
             neutral: Path | None = None) -> dict:
    t0 = time.monotonic()
    rep = json.loads((run_dir / "dense_report.json").read_text())
    mrep = json.loads((sd / "map_report.json").read_text())
    models = (mrep.get("reconstruction") or {}).get("models")
    rec = pycolmap.Reconstruction(str(sd / (models[rep["model_rank"]]["colmap_dir"] if models else mrep["reconstruction"]["chosen_model_dir"])))
    by_name = {im.name: im for im in rec.images.values()}
    diag = rep.get("diagnostics")
    names = diag["image_names"] if diag else sorted(by_name, key=lambda n: n)
    frames = json.loads((sd / "frames.json").read_text())
    order = {f["file"]: i for i, f in enumerate(frames)}
    names = sorted(names, key=lambda n: order[n])
    ims = [by_name[n] for n in names]
    idx_of = {n: i for i, n in enumerate(names)}
    ww = rep["config"]["work_width"] if "config" in rep else 640
    X, C = load_cloud(run_dir)
    raw = dict(np.load(run_dir / "diag" / "raw_kept.npz")) if (run_dir / "diag" / "raw_kept.npz").exists() else None
    # judge maps: frozen depth maps addressed by frame name, re-indexed onto this run's image order
    jmaps = _judge(judge, idx_of)
    neutral_maps = _judge(neutral, idx_of) if neutral is not None else None
    res = {"run": name, "run_dir": str(run_dir), "label": rep.get("label"), "config": rep.get("config"),
           "static_masks": rep.get("static_masks"), "t_range": rep.get("t_range"), "points": rep.get("dense_verified_points"),
           "views_verified": rep.get("views_verified"), "reference_views": rep.get("reference_views"), "seconds": rep.get("seconds"),
           "judge_maps": str(judge), "judge_sha256": hashlib.sha256(Path(judge).read_bytes()).hexdigest(), "frames": {}}
    agrees = [r["sparse_agreement"] for r in rep.get("views", []) if r.get("status") == "VERIFIED"]
    res["sparse_agree"] = {"min": min(agrees) if agrees else None, "median": float(np.median(agrees)) if agrees else None}
    clouds = {**{k: load_cloud(Path(p)) for k, p in compare.items()}, name: (X, C)}
    for rf in regions["frames"]:
        fn = rf["frame"]
        k = idx_of[fn]
        cam = Cam(rec, ims[k], ww)
        lab = labels_for(sd / "frames" / fn, out / "labels")
        flags = None
        if neutral is not None:
            uu, vv, zz, ins0 = _project(cam, X)
            flags = np.zeros(len(X), bool)
            flags[ins0] = fs_flags(rec, ims, X[ins0], neutral_maps, ww)
        fm, _, sky, person = frame_metrics(sd, rec, ims[k], rf, cam, X, lab, flags)
        fm["role"] = rf.get("role", "tuning")
        st = run_dir / "diag" / f"stage_{fn}.npz"
        if st.exists():
            npz = dict(np.load(st))
            fm["stages"] = {cls: stage_metrics(npz, mu) for cls, (_, mu) in region_masks(rf, cam).items()}
        if fn == "f000240.jpg":
            fm["streak"] = behind_floor_shares(sd, rec, ims[k], cam, X)
        res["frames"][fn] = fm
        # same view: source / compare runs / this run
        panels = [cv2.resize(cv2.imread(str(sd / "frames" / fn)), (800, 450), interpolation=cv2.INTER_AREA)]
        caps = [f"source {fn} ({rf.get('role', 'tuning')})"]
        for tag, (Xc, Cc) in clouds.items():
            img, n_in = render(cam, Xc, Cc)
            panels.append(img)
            caps.append(f"{tag}: {n_in:,} pts in view")
        sx, sy = 800 / rf["width"], 450 / rf["height"]
        colr = {"floor": (0, 200, 255), "floor_far": (255, 200, 0), "structure": (0, 255, 0), "wall": (255, 0, 255), "far": (255, 0, 0)}
        for p, cap in zip(panels, caps):
            for r in rf["regions"]:
                cv2.polylines(p, [np.round(np.array(r["polygon"], float) * [sx, sy]).astype(np.int32)], True, colr[r["class"]], 1)
            cv2.rectangle(p, (0, 0), (800, 22), (0, 0, 0), -1)
            cv2.putText(p, cap, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
        cl = fm["classes"]
        foot = " | ".join(f"{c} {cl[c]['cov1px']:.3f}" for c in CLASSES if c in cl) + f" | sky-edge band {fm['boundary_band']['cov1px']:.3f} | sky false {fm['sky_false']['points']}"
        grid = np.hstack(panels) if len(panels) <= 3 else np.vstack([np.hstack(panels[:2]), np.hstack(panels[2:4] + ([np.zeros_like(panels[0])] if len(panels) == 3 else []))])
        strip = np.zeros((24, grid.shape[1], 3), np.uint8)
        cv2.putText(strip, f"{name}: {foot} (1 px per point at {cam.w}x{cam.h}; outlines = frozen regions)", (6, 17),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
        cv2.imwrite(str(out / f"sameview_{fn.split('.')[0]}_{name}.jpg"), np.vstack([grid, strip]), [cv2.IMWRITE_JPEG_QUALITY, 92])
    errs = {}
    errs["float_common"] = floating(rec, ims, X, rep.get("voxel", 0.03), jmaps, ww)
    if neutral is not None:  # A1: depth maps no candidate was verified against (B0)
        errs["float_neutral"] = floating(rec, ims, X, rep.get("voxel", 0.03), neutral_maps, ww)
        errs["neutral_maps"] = str(neutral)
    if raw is not None:
        vs = (diag or {}).get("verifier_sets", {})
        errs["inconsist_common"] = heldout_inconsistency(rec, ims, raw, jmaps, vs, ww)
        own = run_dir / "diag" / "depth_maps.npz"
        if own.exists():
            dm = np.load(own)
            dm = {kk: dm[kk] for kk in dm.files}
            errs["float_own"] = floating(rec, ims, X, rep.get("voxel", 0.03), dm, ww)
            errs["inconsist_own"] = heldout_inconsistency(rec, ims, raw, dm, vs, ww)
        u, v = raw["u"].astype(int), raw["v"].astype(int)
        hh = Cam(rec, ims[0], ww).h
        b = (u < BORDER_PX) | (u >= ww - BORDER_PX) | (v < BORDER_PX) | (v >= hh - BORDER_PX)
        errs["raw_border_share"] = round(float(b.mean()), 5)
    res["errors"] = errs
    res["eval_seconds"] = round(time.monotonic() - t0, 1)
    (out / f"static_metrics_{name}.json").write_text(json.dumps(res, indent=1))
    table = out / "static_table.csv"
    new = not table.exists()
    with table.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["run", "frame", "role", "metric", "value"])
        for fn, fm in res["frames"].items():
            for cls, m in fm["classes"].items():
                w.writerow([name, fn, fm["role"], f"cov1px.{cls}", m["cov1px"]])
            w.writerow([name, fn, fm["role"], "cov1px.boundary_band", fm["boundary_band"]["cov1px"]])
            w.writerow([name, fn, fm["role"], "sky_false.share", fm["sky_false"]["share_of_points_in_view"]])
            for kk, vv in fm["person_points"].items():
                w.writerow([name, fn, fm["role"], f"person.{kk}", vv])
        w.writerow([name, "*", "*", "float_fs_common", errs["float_common"]["float_fs"]])
        w.writerow([name, "*", "*", "float_knn", errs["float_common"]["float_knn"]])
        if "float_neutral" in errs:
            w.writerow([name, "*", "*", "float_fs_neutral", errs["float_neutral"]["float_fs"]])
        if "inconsist_common" in errs:
            w.writerow([name, "*", "*", "inconsist_common", errs["inconsist_common"]["inconsist"]])
        w.writerow([name, "*", "*", "points", res["points"]])
        w.writerow([name, "*", "*", "seconds", res["seconds"]])
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.static_eval")
    ap.add_argument("session_dir")
    ap.add_argument("run_dir")
    ap.add_argument("--name", required=True)
    ap.add_argument("--regions", required=True)
    ap.add_argument("--judge-maps", required=True)
    ap.add_argument("--neutral-maps", help="A1: second judge (depth maps no candidate was verified against)")
    ap.add_argument("--out", default="outputs/evidence/static_map")
    ap.add_argument("--frames", nargs="*")
    ap.add_argument("--compare", nargs="*", default=[], help="TAG=run_dir clouds rendered next to this run")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    regions = json.loads(Path(a.regions).read_text())
    if a.frames:
        regions = {**regions, "frames": [f for f in regions["frames"] if f["frame"] in a.frames]}
    compare = dict(c.split("=", 1) for c in a.compare)
    r = evaluate(Path(a.session_dir), Path(a.run_dir), a.name, regions, out, Path(a.judge_maps), compare,
                 Path(a.neutral_maps) if a.neutral_maps else None)
    short = {fn: {"cov": {c: m["cov1px"] for c, m in fm["classes"].items()}, "band": fm["boundary_band"]["cov1px"],
                  "sky_false": fm["sky_false"]["points"], "person": fm["person_points"]} for fn, fm in r["frames"].items()}
    e = r["errors"]
    print(json.dumps({"run": r["run"], "points": r["points"], "seconds": r["seconds"], "views_verified": r["views_verified"],
                      "float_fs_common": e["float_common"]["float_fs"], "float_knn": e["float_common"]["float_knn"],
                      "inconsist_common": e.get("inconsist_common", {}).get("inconsist"), "raw_border_share": e.get("raw_border_share"),
                      "sparse_agree": r["sparse_agree"], "frames": short}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
