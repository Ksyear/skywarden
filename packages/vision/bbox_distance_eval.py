"""Experiment E1: how wrong is "box size -> distance" for a real detector on real footage?

Compares a pinhole estimate from the person box height, Z = fy * H_person / h_px, with the measured depth of the
same person from the registered depth image (median over the torso core of the box). Uses the detections already
recorded by a tracking run (REAL_MODEL), so the detector is not re-run.

Two estimators:
  prior      H_person = 1.70 m (what a "box size -> distance" rule has to assume without calibration)
  calibrated H_person fitted on the first 20 % of samples (by time), evaluated on the remaining 80 %
The threshold was fixed before running: a "box size -> distance" rule FAILS if the calibrated p95 relative error
exceeds 25 %. The project rule (no distance from box size) does not depend on the outcome.

python -m vision.bbox_distance_eval --session outputs/tracks/tum_walking_xyz_nano \
    --dataset data/public/tum_fr3_walking_xyz/rgbd_dataset_freiburg3_walking_xyz --out outputs/evidence/e1_bbox_distance
"""
from __future__ import annotations

import argparse
import bisect
import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

DEPTH_SCALE = 5000.0       # TUM: 16-bit PNG, 5000 = 1 m, 0 = no data
FY_FR3 = 539.2             # TUM fr3 colour camera (images already undistorted)
PRIOR_HEIGHT_M = 1.70
RULE_P95_LIMIT = 0.25     # fixed before running (rule fails above this p95 relative error)
MAX_ASSOC_DT = 0.02        # s between RGB and depth stamps
MIN_DEPTH_PX = 50
EDGE_PX = 2


@dataclass(frozen=True)
class Sample:
    k: int
    t: float
    rgb: str
    bbox: tuple[float, float, float, float]
    score: float
    z_true: float
    depth_px: int
    truncated: bool
    depth_iqr_rel: float = 0.0  # spread of torso depths / median: large when the core mixes person and background


def read_list(path: Path) -> list[tuple[float, str]]:
    """TUM rgb.txt / depth.txt: '<timestamp> <relative file>' lines, '#' comments."""
    out = []
    for line in path.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            ts, name = line.split()[:2]
            out.append((float(ts), name))
    return sorted(out)


def nearest(stamps: list[float], t: float) -> int:
    i = bisect.bisect_left(stamps, t)
    cands = [j for j in (i - 1, i) if 0 <= j < len(stamps)]
    return min(cands, key=lambda j: abs(stamps[j] - t))


def torso_depth(depth_m: np.ndarray, bbox, min_px: int = MIN_DEPTH_PX, with_iqr: bool = False):
    """Median depth (m) over the torso core: central 40 % of width, 15–60 % of height. Background around the
    person and limbs are excluded; zero (missing) pixels are ignored."""
    h, w = depth_m.shape
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    xa, xb = int(max(0, x1 + 0.30 * bw)), int(min(w, x1 + 0.70 * bw))
    ya, yb = int(max(0, y1 + 0.15 * bh)), int(min(h, y1 + 0.60 * bh))
    core = depth_m[ya:yb, xa:xb]
    valid = core[core > 0]
    if valid.size < min_px:
        return (None, int(valid.size), None) if with_iqr else (None, int(valid.size))
    med = float(np.median(valid))
    if with_iqr:
        q1, q3 = np.percentile(valid, [25, 75])
        return med, int(valid.size), float((q3 - q1) / med)
    return med, int(valid.size)


def is_truncated(bbox, image_wh, edge: int = EDGE_PX) -> bool:
    x1, y1, x2, y2 = bbox
    w, h = image_wh
    return x1 <= edge or y1 <= edge or x2 >= w - edge or y2 >= h - edge


def z_from_height(h_px: np.ndarray, person_h_m: float, fy: float = FY_FR3) -> np.ndarray:
    return fy * person_h_m / h_px


def rel_error_stats(z_est: np.ndarray, z_true: np.ndarray) -> dict:
    if len(z_true) == 0:
        return {"n": 0}
    rel = (z_est - z_true) / z_true
    a = np.abs(rel)
    ratio = z_est / z_true
    return {"n": int(len(z_true)),
            "abs_rel_err_p50": round(float(np.percentile(a, 50)), 3),
            "abs_rel_err_p95": round(float(np.percentile(a, 95)), 3),
            "abs_rel_err_max": round(float(a.max()), 3),
            "signed_rel_err_median": round(float(np.median(rel)), 3),
            "within_10pct": round(float((a <= 0.10).mean()), 3),
            "within_25pct": round(float((a <= 0.25).mean()), 3),
            "ratio_est_over_true_p5": round(float(np.percentile(ratio, 5)), 3),
            "ratio_est_over_true_p95": round(float(np.percentile(ratio, 95)), 3),
            "abs_err_m_p50": round(float(np.percentile(np.abs(z_est - z_true), 50)), 2),
            "abs_err_m_p95": round(float(np.percentile(np.abs(z_est - z_true), 95)), 2)}


def collect(session: Path, dataset: Path) -> tuple[list[Sample], dict]:
    rec = json.loads((session / "tracks.json").read_text())
    rgb = read_list(dataset / "rgb.txt")
    dep = read_list(dataset / "depth.txt")
    dstamps = [d[0] for d in dep]
    wh = tuple(rec["image_wh"])
    samples, skipped = [], {"no_depth_match": 0, "too_few_depth_px": 0}
    for f in rec["frames"]:
        ts, rgb_name = rgb[f["source_index"]]
        j = nearest(dstamps, ts)
        if abs(dstamps[j] - ts) > MAX_ASSOC_DT:
            skipped["no_depth_match"] += len(f["detections"])
            continue
        depth = cv2.imread(str(dataset / dep[j][1]), cv2.IMREAD_UNCHANGED)
        if depth is None or depth.dtype != np.uint16:
            raise ValueError(f"unexpected depth image {dep[j][1]}")
        depth_m = depth.astype(np.float32) / DEPTH_SCALE
        for x1, y1, x2, y2, score in f["detections"]:
            z, n, iqr = torso_depth(depth_m, (x1, y1, x2, y2), with_iqr=True)
            if z is None:
                skipped["too_few_depth_px"] += 1
                continue
            samples.append(Sample(f["k"], f["t"], rgb_name, (x1, y1, x2, y2), score, z, n,
                                  is_truncated((x1, y1, x2, y2), wh), iqr))
    return samples, {"image_wh": list(wh), "frames": len(rec["frames"]), "skipped": skipped}


def evaluate(samples: list[Sample]) -> dict:
    h = np.array([s.bbox[3] - s.bbox[1] for s in samples])
    zt = np.array([s.z_true for s in samples])
    trunc = np.array([s.truncated for s in samples])
    order = np.argsort([s.t for s in samples], kind="stable")
    n_fit = max(1, int(round(0.2 * len(samples))))
    fit_idx, ev_idx = order[:n_fit], order[n_fit:]
    fitted_h = float(np.median(zt[fit_idx] * h[fit_idx] / FY_FR3))  # effective person height that best explains fit set
    z_prior = z_from_height(h, PRIOR_HEIGHT_M)
    z_cal = z_from_height(h, fitted_h)

    def block(z_est, idx):
        idx = np.asarray(idx)
        return {"all": rel_error_stats(z_est[idx], zt[idx]),
                "box_inside_frame": rel_error_stats(z_est[idx][~trunc[idx]], zt[idx][~trunc[idx]]),
                "box_touches_frame_edge": rel_error_stats(z_est[idx][trunc[idx]], zt[idx][trunc[idx]])}

    res = {"prior_1.70m": block(z_prior, np.arange(len(samples))),
           "calibrated": {"fitted_person_height_m": round(fitted_h, 3), "fit_samples": int(n_fit),
                          **block(z_cal, ev_idx)}}
    # Best case (secondary, chosen after seeing the data, so informational only): only boxes fully inside the frame
    # whose torso depth is consistent (IQR/median <= 0.15), height fitted on the first 20 % of that subset.
    clean = np.array([(not s.truncated) and s.depth_iqr_rel <= 0.15 for s in samples])
    c_idx = order[clean[order]]
    if len(c_idx) >= 10:
        n_c = max(1, int(round(0.2 * len(c_idx))))
        h_c = float(np.median(zt[c_idx[:n_c]] * h[c_idx[:n_c]] / FY_FR3))
        z_c = z_from_height(h, h_c)
        res["best_case_full_body_clean_depth"] = {
            "selection": "box inside frame AND torso depth IQR/median <= 0.15; fit first 20 % of this subset by time",
            "post_hoc": True, "fitted_person_height_m": round(h_c, 3), "fit_samples": n_c,
            "subset_size": int(len(c_idx)), "eval": rel_error_stats(z_c[c_idx[n_c:]], zt[c_idx[n_c:]])}
    res["gt_depth_inconsistent_samples"] = int(sum(s.depth_iqr_rel > 0.15 for s in samples))
    p95 = res["calibrated"]["all"]["abs_rel_err_p95"]
    res["verdict"] = {"rule_under_test": "box size -> distance (used to keep a safe distance)",
                      "criterion": f"rule FAILS if calibrated p95 relative error > {RULE_P95_LIMIT:.0%} (fixed before running)",
                      "calibrated_p95": p95, "prior_p95": res["prior_1.70m"]["all"]["abs_rel_err_p95"],
                      "result": "FAIL" if p95 > RULE_P95_LIMIT else "PASS"}
    lo, hi = res["calibrated"]["all"]["ratio_est_over_true_p5"], res["calibrated"]["all"]["ratio_est_over_true_p95"]
    res["what_an_8m_rule_means"] = {
        "note": "If the estimate says 8.0 m, the true distance (5th–95th percentile of this data, calibrated estimator) "
                "lies in this range. Extrapolated: this clip only covers ~1–4 m indoors.",
        "true_m_range_when_estimate_is_8m": [round(8.0 / hi, 2), round(8.0 / lo, 2)]}
    return res, z_prior, z_cal, ev_idx


def scatter_png(path: Path, zt, z_prior, z_cal, trunc, ev_idx) -> None:
    W, H, m = 760, 560, 60
    img = np.full((H, W, 3), (22, 18, 15), np.uint8)
    zmax = float(max(zt.max(), z_prior.max(), z_cal.max()) * 1.05)
    px = lambda v: int(m + v / zmax * (W - 2 * m))  # noqa: E731
    py = lambda v: int(H - m - v / zmax * (H - 2 * m))  # noqa: E731
    grey = (110, 100, 95)
    for v in np.arange(0, zmax, 1.0):
        cv2.line(img, (px(v), H - m), (px(v), m), (40, 36, 32), 1)
        cv2.line(img, (m, py(v)), (W - m, py(v)), (40, 36, 32), 1)
        cv2.putText(img, f"{v:.0f}", (px(v) - 5, H - m + 18), 0, 0.45, grey, 1)
        cv2.putText(img, f"{v:.0f}", (m - 22, py(v) + 4), 0, 0.45, grey, 1)
    for f, c in ((1.0, (200, 200, 200)), (1.25, (60, 60, 160)), (0.75, (60, 60, 160))):
        cv2.line(img, (px(0), py(0)), (px(zmax), py(zmax * f)), c, 1, cv2.LINE_AA)
    ev = np.zeros(len(zt), bool)
    ev[ev_idx] = True
    for i in range(len(zt)):
        if ev[i]:
            col = (32, 176, 255) if trunc[i] else (255, 195, 76)  # BGR: orange = box at frame edge, blue = inside
            cv2.circle(img, (px(zt[i]), py(z_cal[i])), 3, col, -1, cv2.LINE_AA)
    cv2.putText(img, "E1: box-height distance (calibrated) vs measured depth - TUM fr3/walking_xyz, YOLOX-nano", (m - 40, 28), 0, 0.5, (230, 230, 230), 1, cv2.LINE_AA)
    cv2.putText(img, "x: measured torso depth [m]   y: estimate from box height [m]   grey: exact   red: +/-25 %", (m - 40, H - 18), 0, 0.45, grey, 1, cv2.LINE_AA)
    cv2.putText(img, "blue: box inside frame   orange: box touches frame edge (cut off)", (m - 40, 48), 0, 0.45, grey, 1, cv2.LINE_AA)
    cv2.imwrite(str(path), img)


def worst_montage(path: Path, dataset: Path, samples: list[Sample], z_cal: np.ndarray, ev_idx, n: int = 6) -> list[dict]:
    zt = np.array([s.z_true for s in samples])
    ev = np.asarray(ev_idx)
    rel = np.abs(z_cal[ev] - zt[ev]) / zt[ev]
    picks = ev[np.argsort(-rel)[:n]]
    tiles, info = [], []
    for i in picks:
        s = samples[i]
        im = cv2.imread(str(dataset / s.rgb))
        x1, y1, x2, y2 = map(int, s.bbox)
        cv2.rectangle(im, (x1, y1), (x2, y2), (76, 195, 255), 2)
        txt = f"est {z_cal[i]:.2f} m / depth {s.z_true:.2f} m ({(z_cal[i] - s.z_true) / s.z_true:+.0%})"
        cv2.rectangle(im, (0, 0), (640, 26), (0, 0, 0), -1)
        cv2.putText(im, txt, (6, 18), 0, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
        tiles.append(cv2.resize(im, (480, 360)))
        info.append({"k": s.k, "rgb": s.rgb, "bbox": [round(v, 1) for v in s.bbox], "z_true_m": round(s.z_true, 2),
                     "z_est_m": round(float(z_cal[i]), 2), "truncated": s.truncated})
    while len(tiles) % 3:
        tiles.append(np.zeros_like(tiles[0]))
    cv2.imwrite(str(path), np.vstack([np.hstack(tiles[r:r + 3]) for r in range(0, len(tiles), 3)]))
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vision.bbox_distance_eval")
    ap.add_argument("--session", required=True, help="tracking run folder with tracks.json (REAL_MODEL detections)")
    ap.add_argument("--dataset", required=True, help="TUM sequence folder with rgb.txt, depth.txt, depth/")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    session, dataset, out = Path(a.session), Path(a.dataset), Path(a.out)
    if not (dataset / "depth.txt").is_file():
        print(json.dumps({"status": "BLOCKED", "reason": f"{dataset}/depth.txt missing (depth not extracted)"}))
        return 2
    out.mkdir(parents=True, exist_ok=True)
    samples, meta = collect(session, dataset)
    report = json.loads((session / "tracking_report.json").read_text())
    res, z_prior, z_cal, ev_idx = evaluate(samples)
    zt = np.array([s.z_true for s in samples])
    trunc = np.array([s.truncated for s in samples])
    scatter_png(out / "e1_scatter.png", zt, z_prior, z_cal, trunc, ev_idx)
    worst = worst_montage(out / "e1_worst_cases.jpg", dataset, samples, z_cal, ev_idx)
    result = {"experiment": "E1 bbox-size distance error", "status": "RUN",
              "label": "REAL_MODEL detections on PUBLIC_DATASET; ground truth = registered Kinect depth (torso median)",
              "inputs": {"session": str(session), "detector": report["detector"]["name"],
                         "detector_sha256": report["detector"]["sha256"], "dataset": str(dataset),
                         "fy": FY_FR3, "depth_scale": DEPTH_SCALE, "prior_person_height_m": PRIOR_HEIGHT_M},
              "samples": len(samples), "measured_depth_range_m": [round(float(zt.min()), 2), round(float(zt.max()), 2)],
              "samples_box_touches_frame_edge": int(trunc.sum()), **meta, **res, "worst_cases": worst,
              "limitations": ["indoor 1–4 m only; an 8 m keep-out distance is extrapolated, not measured",
                              "Kinect depth is itself noisy (cm-level, worse with range) and z-depth, not slant range",
                              "torso-core median can hit the desk/chair when the person is occluded",
                              "two people, one camera, one scene: not a population estimate"]}
    (out / "e1_bbox_distance.json").write_text(json.dumps(result, indent=1))
    with open(out / "e1_samples.csv", "w") as fh:
        fh.write("k,t,rgb,x1,y1,x2,y2,score,box_h_px,z_true_m,z_prior_m,z_cal_m,truncated,eval_set,depth_px\n")
        ev = set(int(i) for i in ev_idx)
        for i, s in enumerate(samples):
            fh.write(f"{s.k},{s.t},{s.rgb},{','.join(f'{v:.1f}' for v in s.bbox)},{s.score:.3f},{s.bbox[3] - s.bbox[1]:.1f},"
                     f"{s.z_true:.3f},{z_prior[i]:.3f},{z_cal[i]:.3f},{int(s.truncated)},{int(i in ev)},{s.depth_px}\n")
    print(json.dumps({k: result[k] for k in ("samples", "measured_depth_range_m", "samples_box_touches_frame_edge", "verdict",
                                             "what_an_8m_rule_means")}, indent=1))
    print(json.dumps({"best_case": res.get("best_case_full_body_clean_depth"), "gt_inconsistent": res["gt_depth_inconsistent_samples"]}, indent=1))
    print(json.dumps({"prior": res["prior_1.70m"]["all"], "calibrated": res["calibrated"]["all"],
                      "calibrated_inside_frame": res["calibrated"]["box_inside_frame"],
                      "fitted_h": res["calibrated"]["fitted_person_height_m"]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
