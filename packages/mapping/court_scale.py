"""Metric scale from regulation tennis-court lines seen in a finished sparse map (method 2 with known-size objects).

Inputs: the session (frames + poses of model rank 0) and a seed file with, per court, the four outer (doubles)
corners clicked roughly in ONE frame. From there everything is measured in the images:
  1. the seed frame's corners give a homography from the court plan, which only predicts where the other line
     junctions are in that frame; each junction is then re-found from the white line pixels (two line fits, their
     intersection), so the regulation plan never enters a measured position;
  2. the refined seed-frame junction's viewing ray meets a plane fitted (RANSAC) to the sparse points around the
     court, giving a first 3D guess that is projected into every other registered frame and re-found there the
     same way;
  3. every junction seen in >= 3 frames is triangulated with metric_scale.triangulate (same code as the markers).
One length fixes the scale, the others are predicted with it. Court dimensions are the ITF regulation values,
not a survey of this court (a real court may differ by a few cm), and they are measured to the outside of the
lines while junctions here are line centres; true lengths are therefore reduced by one nominal line width (5 cm).

  PYTHONPATH=packages .venv/bin/python -m mapping.court_scale <session_dir> <seeds.json> <out.json> [--write-scale]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from . import metric_scale

# ITF court plan (metres): x along the court from one baseline, y across from one doubles sideline.
LENGTH, WIDTH_D, WIDTH_S, SERVICE_FROM_NET = 23.77, 10.97, 8.23, 6.40
_ys = (WIDTH_D - WIDTH_S) / 2
_xv = LENGTH / 2 - SERVICE_FROM_NET
PLAN = {"D00": (0, 0), "DL0": (LENGTH, 0), "DLW": (LENGTH, WIDTH_D), "D0W": (0, WIDTH_D),
        "S0a": (0, _ys), "S0b": (0, WIDTH_D - _ys), "SLa": (LENGTH, _ys), "SLb": (LENGTH, WIDTH_D - _ys),
        "V1a": (_xv, _ys), "V1b": (_xv, WIDTH_D - _ys), "V2a": (LENGTH - _xv, _ys), "V2b": (LENGTH - _xv, WIDTH_D - _ys),
        "T1": (_xv, WIDTH_D / 2), "T2": (LENGTH - _xv, WIDTH_D / 2)}
# the two lines through each junction, each given by another junction on the same line
LINES = {"D00": ("DL0", "D0W"), "DL0": ("D00", "DLW"), "DLW": ("D0W", "DL0"), "D0W": ("DLW", "D00"),
         "S0a": ("SLa", "D00"), "S0b": ("SLb", "D0W"), "SLa": ("S0a", "DL0"), "SLb": ("S0b", "DLW"),
         "V1a": ("SLa", "V1b"), "V1b": ("SLb", "V1a"), "V2a": ("S0a", "V2b"), "V2b": ("S0b", "V2a"),
         "T1": ("T2", "V1a"), "T2": ("T1", "V2a")}
LENGTHS = {"doubles_sideline": (("D00", "DL0"), LENGTH), "doubles_sideline_far": (("D0W", "DLW"), LENGTH),
           "singles_sideline": (("S0a", "SLa"), LENGTH), "singles_sideline_far": (("S0b", "SLb"), LENGTH),
           "doubles_width": (("D00", "D0W"), WIDTH_D), "doubles_width_far": (("DL0", "DLW"), WIDTH_D),
           "singles_width": (("S0a", "S0b"), WIDTH_S), "singles_width_far": (("SLa", "SLb"), WIDTH_S),
           "service_line": (("V1a", "V1b"), WIDTH_S), "service_line_far": (("V2a", "V2b"), WIDTH_S),
           "centre_service_line": (("T1", "T2"), 2 * SERVICE_FROM_NET)}
LINE_WIDTH_M = 0.05  # regulation lines 2.5-5 cm (baseline up to 10 cm); lengths are to the outer edges
MIN_VIEWS = 3
MAX_REPROJ_PX = 2.0
MAX_SHIFT_PX = 8.0


# ---------------- image measurement ----------------
def _line_centres(gray: np.ndarray, p: np.ndarray, d: np.ndarray, half_len: float, band: float, gap: float) -> np.ndarray:
    """Centre points of a bright line running through p along d: for each step along the line (|s| > gap, so the
    crossing line is not sampled), the intensity-weighted centre of the across-line profile, kept when the line is
    clearly brighter than its surroundings."""
    import cv2
    n = np.array([-d[1], d[0]])
    ts = np.arange(-band, band + 0.5, 0.5)
    out = []
    for s in np.arange(-half_len, half_len + 1, 1.0):
        if abs(s) < gap:
            continue
        q = p + s * d + ts[:, None] * n
        prof = cv2.remap(gray, q[:, 0].astype(np.float32).reshape(1, -1), q[:, 1].astype(np.float32).reshape(1, -1),
                         cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT).reshape(-1).astype(float)
        base = np.percentile(prof, 30)
        w = np.clip(prof - base, 0, None)
        if prof.max() - base < 25 or w.sum() == 0:
            continue
        w[w < 0.5 * w.max()] = 0
        t = float((w * ts).sum() / w.sum())
        if abs(t) > band - 1:
            continue
        out.append(p + s * d + t * n)
    return np.array(out)


def _fit_line(pts: np.ndarray) -> tuple[np.ndarray, np.ndarray, float] | None:
    """Total-least-squares line with two rounds of rejecting points > 1.5 px off. Returns (point, direction, rms)."""
    if len(pts) < 8:
        return None
    keep = np.ones(len(pts), bool)
    for _ in range(3):
        P = pts[keep]
        if len(P) < 8:
            return None
        mu = P.mean(0)
        d = np.linalg.svd(P - mu)[2][0]
        r = np.abs((pts - mu) @ np.array([-d[1], d[0]]))
        keep = r <= max(1.5, 2.5 * np.median(r[keep]))
    P = pts[keep]
    mu = P.mean(0)
    d = np.linalg.svd(P - mu)[2][0]
    rms = float(np.sqrt(np.mean(((P - mu) @ np.array([-d[1], d[0]])) ** 2)))
    return mu, d, rms


def refine_junction(gray: np.ndarray, p0: np.ndarray, dirs: list[np.ndarray], spacing_px: float) -> dict | None:
    """Re-find a line junction near p0 from the two lines along `dirs`. spacing_px: image distance to the nearest
    other junction (limits how far along the lines we look). Returns {"uv", "rms", "n"} or None."""
    p = np.asarray(p0, float)
    half = float(np.clip(0.6 * spacing_px, 15, 60))
    for band, gap in ((7.0, 6.0), (4.0, 4.0)):
        fits = []
        for d in dirs:
            d = np.asarray(d, float) / np.linalg.norm(d)
            f = _fit_line(_line_centres(gray, p, d, half, band, gap))
            if f is None:
                return None
            fits.append(f)
        (a, da, _), (b, db, _) = fits
        A = np.array([da, -db]).T
        if abs(np.linalg.det(A)) < 0.3:  # nearly parallel
            return None
        s = np.linalg.solve(A, b - a)
        p = a + s[0] * da
    if np.linalg.norm(p - p0) > MAX_SHIFT_PX:
        return None
    return {"uv": p, "rms": max(f[2] for f in fits), "n": [len(_line_centres(gray, p, f[1], half, 4.0, 4.0)) for f in fits]}


# ---------------- geometry helpers ----------------
def _centre(T: np.ndarray) -> np.ndarray:
    return -T[:3, :3].T @ T[:3, 3]


def _project(cam, T: np.ndarray, X: np.ndarray) -> np.ndarray | None:
    pc = T[:3, :3] @ X + T[:3, 3]
    if pc[2] <= 0:
        return None
    return np.asarray(cam.img_from_cam(pc[None]), float).reshape(2)


def _ray(cam, T: np.ndarray, uv: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    xy = np.asarray(cam.cam_from_img(np.asarray(uv, float)[None]), float).reshape(2)
    d = T[:3, :3].T @ np.array([xy[0], xy[1], 1.0])
    return _centre(T), d / np.linalg.norm(d)


def _homography(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    import cv2
    return cv2.findHomography(np.asarray(src, np.float32), np.asarray(dst, np.float32))[0]


def _apply_h(H: np.ndarray, xy) -> np.ndarray:
    v = H @ np.array([xy[0], xy[1], 1.0])
    return v[:2] / v[2]


def measure_court(court: dict, model: dict, frames_dir: Path, points_xyz: np.ndarray) -> dict:
    """Triangulated junctions of one court. court: {"name", "seed_frame", "corners_px": {"D00": [u, v], ...}}."""
    import cv2
    views = model["views"]
    names = sorted(views)
    gray_cache: dict[str, np.ndarray] = {}

    def gray(name):
        if name not in gray_cache:
            gray_cache[name] = cv2.cvtColor(cv2.imread(str(frames_dir / name)), cv2.COLOR_BGR2GRAY)
        return gray_cache[name]

    seed = court["seed_frame"]
    cam0, T0 = views[seed]
    corners = court["corners_px"]
    H = _homography([PLAN[k] for k in corners], [corners[k] for k in corners])
    pred0 = {k: _apply_h(H, xy) for k, xy in PLAN.items()}
    # plane under the court: sparse points that project inside the (enlarged) court outline in the seed frame
    quad = np.array([pred0[k] for k in ("D00", "DL0", "DLW", "D0W")], np.float32)
    big = (quad - quad.mean(0)) * 1.4 + quad.mean(0)
    pc = points_xyz @ T0[:3, :3].T + T0[:3, 3]
    front = pc[:, 2] > 0
    uv = np.full((len(points_xyz), 2), -1e9)
    uv[front] = np.asarray(cam0.img_from_cam(pc[front]), float)
    inside = np.array([cv2.pointPolygonTest(big, (float(u), float(v)), False) >= 0 for u, v in uv])
    plane = metric_scale.ground_plane_ransac(points_xyz[inside], seed=0)
    n, dpl = np.asarray(plane["normal"]), plane["d"]

    def dirs_in(frame_pred: dict, k: str) -> list[np.ndarray]:
        return [frame_pred[o] - frame_pred[k] for o in LINES[k]]

    def spacing(frame_pred: dict, k: str) -> float:
        return min(np.linalg.norm(frame_pred[o] - frame_pred[k]) for o in frame_pred if o != k)

    rough, seed_obs = {}, {}
    for k in PLAN:
        r = refine_junction(gray(seed), pred0[k], dirs_in(pred0, k), spacing(pred0, k))
        # a junction hidden in the seed frame (pole, shadow) starts from its predicted pixel: only a first guess,
        # it is measured in the other frames like every junction
        uv0 = r["uv"] if r is not None else pred0[k]
        C, d = _ray(cam0, T0, uv0)
        lam = -(n @ C + dpl) / (n @ d)
        if lam > 0:
            rough[k] = C + lam * d
            seed_obs[k] = {"uv": uv0, "refined_in_seed_frame": r is not None}
    out = {"name": court["name"], "seed_frame": seed, "seed_corners_px": corners, "plane": plane,
           "plane_points": int(inside.sum()), "junctions": {}}
    for k in PLAN:
        if k not in rough:
            out["junctions"][k] = {"status": "not found in seed frame"}
            continue
        obs = []
        for name in names:
            cam, T = views[name]
            pr = {o: _project(cam, T, X) for o, X in rough.items()}
            if pr.get(k) is None or any(v is None for v in pr.values()):
                continue
            u, v = pr[k]
            if not (30 <= u < cam.width - 30 and 30 <= v < cam.height - 30):
                continue
            need = [o for o in LINES[k] if o in pr]
            if len(need) < 2:
                continue
            r = refine_junction(gray(name), pr[k], [pr[o] - pr[k] for o in need], spacing(pr, k))
            if r is not None:
                obs.append({"image": name, "uv": r["uv"], "line_rms_px": r["rms"], "cam": cam, "T": T})
        rec = {"frames_found": len(obs)}
        if len(obs) >= MIN_VIEWS:
            for _ in range(3):  # drop frames that disagree (occlusion, wrong line) and re-triangulate
                tri = metric_scale.triangulate([o["cam"] for o in obs], [o["T"] for o in obs], [o["uv"] for o in obs])
                err = [np.linalg.norm(_project(o["cam"], o["T"], tri["X"]) - o["uv"]) for o in obs]
                bad = int(np.argmax(err))
                if err[bad] <= MAX_REPROJ_PX or len(obs) <= MIN_VIEWS:
                    break
                obs.pop(bad)
            ok = tri["reproj_rms_px"] <= MAX_REPROJ_PX and tri["min_depth"] > 0 and len(obs) >= MIN_VIEWS
            rec.update(status="ok" if ok else "rejected", X=tri["X"].round(7).tolist(),
                       reproj_rms_px=round(tri["reproj_rms_px"], 3), max_angle_deg=round(tri["max_angle_deg"], 2),
                       frames_used=len(obs), height_above_plane_units=round(float(n @ tri["X"] + dpl), 5),
                       observations=[{"image": o["image"], "uv": np.round(o["uv"], 2).tolist(),
                                      "line_rms_px": round(o["line_rms_px"], 3)} for o in obs])
        else:
            rec["status"] = f"seen in {len(obs)} frame(s) < {MIN_VIEWS}"
        rec["seed_frame_uv"] = np.round(seed_obs[k]["uv"], 2).tolist()
        rec["refined_in_seed_frame"] = seed_obs[k]["refined_in_seed_frame"]
        out["junctions"][k] = rec
    return out


def court_geometry(court: dict, model: dict) -> dict:
    """Diagnostics of one triangulated court: corner angles, along/across scale ratio, camera tilt and view azimuth
    relative to the court's long axis (a wrong focal length under oblique views stretches the ground along the tilt)."""
    J = {k: np.array(j["X"]) for k, j in court["junctions"].items() if j.get("status") == "ok"}
    out = {"court": court["name"]}
    if not all(k in J for k in ("D00", "DL0", "DLW", "D0W")):
        return out
    along = (J["DL0"] - J["D00"] + J["DLW"] - J["D0W"]) / 2
    across = (J["D0W"] - J["D00"] + J["DLW"] - J["DL0"]) / 2
    n = np.asarray(court["plane"]["normal"], float)
    ang = []
    for a, b, c in (("D0W", "D00", "DL0"), ("D00", "DL0", "DLW"), ("DL0", "DLW", "D0W"), ("DLW", "D0W", "D00")):
        u, v = J[a] - J[b], J[c] - J[b]
        ang.append(round(float(np.degrees(np.arccos(u @ v / np.linalg.norm(u) / np.linalg.norm(v)))), 2))
    ax = along / np.linalg.norm(along)
    tilts, az = [], []
    for name, j in court["junctions"].items():
        if j.get("status") != "ok":
            continue
        for o in j["observations"]:
            T = model["views"][o["image"]][1]
            z = T[2, :3]  # optical axis in world
            tilts.append(np.degrees(np.arccos(min(1.0, abs(z @ n)))))
            zp = z - (z @ n) * n
            if np.linalg.norm(zp) > 1e-6:
                az.append(np.degrees(np.arccos(min(1.0, abs(zp @ ax) / np.linalg.norm(zp)))))
        break
    out.update(corner_angles_deg=ang,
               across_over_along_map=round(float(np.linalg.norm(across) / np.linalg.norm(along)), 5),
               across_over_along_regulation=round((WIDTH_D - LINE_WIDTH_M) / (LENGTH - LINE_WIDTH_M), 5),
               camera_tilt_from_nadir_deg_median=round(float(np.median(tilts)), 1) if tilts else None,
               view_azimuth_vs_long_axis_deg_median=round(float(np.median(az)), 1) if az else None)
    out["anisotropy_pct"] = round(100 * (out["across_over_along_map"] / out["across_over_along_regulation"] - 1), 3)
    return out


def _length(court: dict, key: str) -> dict | None:
    (a, b), nominal = LENGTHS[key]
    ja, jb = court["junctions"].get(a, {}), court["junctions"].get(b, {})
    if ja.get("status") != "ok" or jb.get("status") != "ok":
        return None
    pa, pb = np.array(ja["X"]), np.array(jb["X"])
    return {"court": court["name"], "length": key, "ends": [a, b], "regulation_m": nominal,
            "true_m": round(nominal - LINE_WIDTH_M, 4), "p1": pa.tolist(), "p2": pb.tolist(),
            "map_units": round(float(np.linalg.norm(pb - pa)), 7)}


def run(session_dir: Path, seeds_path: Path, write_scale: bool, command: str) -> dict:
    import pycolmap
    sd = Path(session_dir)
    seeds = json.loads(Path(seeds_path).read_text())
    base = metric_scale.new_scale(sd)
    model = metric_scale._load_model(sd, base)
    rec = pycolmap.Reconstruction(str(sd / base["colmap_dir"]))
    xyz = np.array([p.xyz for p in rec.points3D.values()], float)
    courts = [measure_court(c, model, sd / "frames", xyz) for c in seeds["courts"]]
    lengths = {(c["name"], k): L for c in courts for k in LENGTHS if (L := _length(c, k))}
    fit_key = tuple(seeds["fit"])
    check_keys = [tuple(k) for k in seeds["checks"]]
    fit = lengths.get(fit_key)
    if fit is None:
        raise ValueError(f"fit length {fit_key} not measured")
    mpu = fit["true_m"] / fit["map_units"]
    checks = []
    for k in check_keys:
        L = lengths.get(k)
        if L is None:
            checks.append({"court": k[0], "length": k[1], "status": "not measured"})
            continue
        pred = L["map_units"] * mpu
        checks.append({**{x: L[x] for x in ("court", "length", "ends", "regulation_m", "true_m", "map_units")},
                       "predicted_m": round(pred, 4), "error_pct": round(100 * abs(pred - L["true_m"]) / L["true_m"], 3),
                       "within_5pct": abs(pred - L["true_m"]) / L["true_m"] <= 0.05})
    every = [{**{x: L[x] for x in ("court", "length", "regulation_m", "map_units")},
              "metres_per_unit": round(L["true_m"] / L["map_units"], 6),
              "diff_vs_fit_pct": round(100 * (L["true_m"] / L["map_units"] / mpu - 1), 3)} for L in lengths.values()]
    # how much the answer depends on which length fixes the scale: every measured length as the fit, errors of all
    # lengths on the other court(s)
    sens = []
    for (cf, kf), F in lengths.items():
        m = F["true_m"] / F["map_units"]
        errs = [100 * abs(L["map_units"] * m - L["true_m"]) / L["true_m"] for (c, _), L in lengths.items() if c != cf]
        sens.append({"fit": f"{cf} {kf}", "max_error_other_court_pct": round(max(errs), 3) if errs else None,
                     "n_within_5pct": sum(e <= 5 for e in errs), "n": len(errs)})
    stored = None
    if write_scale:
        if (sd / "scale.json").exists():
            (sd / "scale.json").unlink()
        note = f"{fit['court']} {fit['length']} (regulation {fit['regulation_m']} m)"
        metric_scale.add_two_point(sd, fit["p1"], fit["p2"], fit["true_m"], "fit", note=note, command=command,
                                   length_source="REGULATION")
        for k in check_keys:
            L = lengths.get(k)
            if L:
                stored = metric_scale.add_two_point(sd, L["p1"], L["p2"], L["true_m"], "verify", command=command,
                                                    note=f"{L['court']} {L['length']} (regulation {L['regulation_m']} m)",
                                                    length_source="REGULATION")
        stored["notes"] = ["lengths are ITF regulation court dimensions minus one 5 cm line width (line centres), "
                           "not measured on this court; a real court can differ by a few cm",
                           "status stays MEASURED_UNVERIFIED: the plan's verification needs tape-measured lengths",
                           "source: YouTube a5mun9m1CKY by Zack Dowell (CC BY), PUBLIC_DATASET"]
        metric_scale.write_scale(sd, stored)
    for c in courts:  # positions of rejected junctions are not evidence of anything
        for j in c["junctions"].values():
            if j.get("status") != "ok":
                j.pop("X", None)
    ok = [c for c in checks if "within_5pct" in c]
    return {"test": "metric scale from regulation tennis-court lines (one length fits, others predicted)",
            "data": {"kind": "REAL_VIDEO (public CC BY drone video), map data_origin PUBLIC_DATASET",
                     "source": "YouTube a5mun9m1CKY, Zack Dowell, CC BY, whole 29 s video (data/public/yt_drone_tennis_complex/SOURCE.md)",
                     "true_lengths": "ITF regulation dimensions minus one 5 cm line width; NOT measured on these courts"},
            "label_judgement": "MEASURED_UNVERIFIED: plan 3.3 asks for independent tape-measured lengths; regulation "
                               "dimensions are not measured on site, so they are evaluated but never counted "
                               "(metric_scale length_source=REGULATION, flag REGULATION_DIMENSIONS)",
            "limits": ["a real court may differ from the rule book by a few cm (0.1-0.5 % of these lengths)",
                       "line centres vs outer edges: baselines may be up to 10 cm wide, so the 5 cm correction is "
                       "uncertain by about 0.2 %",
                       "uncalibrated intrinsics (SIMPLE_RADIAL estimated by COLMAP): across-court lengths come out "
                       "about 4-5 % longer than along-court lengths on both courts while corner angles stay 90 deg; "
                       "the view direction is perpendicular to the courts' long axis and about 35 deg from nadir, which "
                       "fits a focal-length error stretching the ground along the viewing direction (hypothesis, not "
                       "tested)", "the map stays NOT_NAVIGATION_SAFE"],
            "seeds_content": seeds,
            "session": str(sd), "map_hash": metric_scale.map_hash(sd), "seeds": str(seeds_path),
            "plan_m": {k: list(v) for k, v in PLAN.items()}, "line_width_m": LINE_WIDTH_M,
            "courts": courts, "fit": {**fit, "metres_per_unit": mpu},
            "checks": checks, "n_checks_within_5pct": sum(c["within_5pct"] for c in ok), "n_checks": len(checks),
            "numeric_result": "PASS" if len(ok) >= 2 and all(c["within_5pct"] for c in ok) and len(ok) == len(checks) else "FAIL",
            "all_lengths": every, "fit_sensitivity": sens,
            "geometry": [court_geometry(c, model) for c in courts],
            "scale_json": {"status": stored["status"], "status_reason": stored["status_reason"],
                           "metres_per_unit": stored["metres_per_unit"]} if stored else None,
            "tools": metric_scale._tools(), "command": command, "created_at": metric_scale._now()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.court_scale")
    ap.add_argument("session_dir")
    ap.add_argument("seeds")
    ap.add_argument("out")
    ap.add_argument("--write-scale", action="store_true")
    a = ap.parse_args(argv)
    res = run(Path(a.session_dir), Path(a.seeds), a.write_scale, " ".join(["python -m mapping.court_scale", *sys.argv[1:]]))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1, ensure_ascii=False))
    print(json.dumps({"junctions": {c["name"]: {k: (j["status"], j.get("frames_used"), j.get("reproj_rms_px"))
                                                for k, j in c["junctions"].items()} for c in res["courts"]},
                      "fit": {k: res["fit"][k] for k in ("court", "length", "map_units", "metres_per_unit")},
                      "checks": res["checks"], "numeric_result": res["numeric_result"], "scale_json": res["scale_json"],
                      "all_lengths": res["all_lengths"]}, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
