"""Metric scale for a finished sparse map, kept beside it in `<session>/scale.json` (schema skywarden/scale@1).

The map files (PLY, trajectory.json, map_report.json) stay RELATIVE; this file says how many metres one map unit is
and how well that is known. Status is decided here and nowhere else (server and UI only display it):

  NONE                 no accepted measurement, or the map was rebuilt since (map_hash differs)
  MODEL_ESTIMATED      only a learned metric-depth model gave a value (a guess, not a check)
  MEASURED_UNVERIFIED  >= 1 accepted marker / tape measurement, but the verification below has not passed
  MEASURED_VERIFIED    >= 2 independent tape lengths predicted within 5 % and markers agree within 5 %

Thresholds are the pre-registered ones of docs/PLAN_METRIC_SCALE.md section 3 and must not be tuned to results.
Even with a verified scale the map stays NOT_NAVIGATION_SAFE.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

SCHEMA = "skywarden/scale@1"
MEASURED_METHODS = ("ARUCO_MARKER", "TWO_POINT_TAPE")
MODEL_METHOD = "METRIC_DEPTH_MODEL"
# 3.1: a marker observation is used only if every one of these holds
MARKER_ACCEPT = {"min_frames": 3, "min_triangulation_angle_deg": 2.0, "max_corner_reproj_rms_px": 1.5,
                 "max_side_ratio_dev": 0.03, "max_diag_over_side_dev": 0.03}
# Frames where the marker's shortest side is below this are not used. S0 (outputs/evidence/metric_scale/
# opencv_aruco_check.txt): >= 43 px gave 0.7-2.3 % side error, ~26 px about 5 %, <= 11 px no detection.
MIN_MARKER_SIDE_PX = 45.0
# 3.2 / 3.3
VERIFY = {"min_checks": 2, "max_error_pct": 5.0, "max_spread_between_markers_rel": 0.05}
CRITERION = "fit에 쓰지 않은 독립 길이 ≥ 2개가 각각 오차 ≤ 5 %"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def map_hash(session_dir: Path) -> str | None:
    """SHA-256 of trajectory.json: any rebuild of the map changes it and so invalidates the scale."""
    try:
        return hashlib.sha256((Path(session_dir) / "trajectory.json").read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def new_scale(session_dir: Path, model_rank: int = 0) -> dict:
    sd = Path(session_dir)
    try:
        rep = json.loads((sd / "map_report.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        rep = {}
    models = (rep.get("reconstruction") or {}).get("models") or []
    colmap_dir = models[model_rank]["colmap_dir"] if model_rank < len(models) else (rep.get("reconstruction") or {}).get("chosen_model_dir")
    return {"schema": SCHEMA, "session_id": sd.name, "model_rank": model_rank, "colmap_dir": colmap_dir,
            "map_hash": map_hash(sd), "status": "NONE", "metres_per_unit": None,
            "uncertainty": {"rel_std": None, "n_observations": 0, "spread_between_methods_rel": None,
                            "spread_between_markers_rel": None},
            "primary_method": None, "observations": [],
            "verification": {"criterion": CRITERION, "checks": [], "passed": False},
            "cross_checks": [], "ground_plane": None, "flags": [], "status_reason": "no measurement yet",
            "thresholds": {"marker_accept": MARKER_ACCEPT, "min_marker_side_px": MIN_MARKER_SIDE_PX, "verify": VERIFY},
            "tools": _tools(), "commands": [], "created_at": _now(), "updated_at": _now()}


def _tools() -> dict:
    out = {"numpy": np.__version__, "python": sys.version.split()[0]}
    try:
        import cv2
        out["opencv"] = cv2.__version__
    except ImportError:
        pass
    try:
        import pycolmap
        out["pycolmap"] = pycolmap.__version__
    except ImportError:
        pass
    return out


def read_scale(session_dir: Path) -> dict | None:
    try:
        data = json.loads((Path(session_dir) / "scale.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA else None


def write_scale(session_dir: Path, scale: dict) -> None:
    path = Path(session_dir) / "scale.json"
    scale["updated_at"] = _now()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(scale, indent=1, ensure_ascii=False))
    tmp.replace(path)


def current_scale(session_dir: Path) -> dict:
    """The stored scale re-judged against the map on disk (a rebuilt map turns it into NONE), or an empty one."""
    s = read_scale(session_dir) or new_scale(session_dir)
    return evaluate(s, map_hash(session_dir))


def _for_update(session_dir: Path, command: str | None) -> dict:
    """Stored scale to add to; a scale made for an older build of the map is set aside, never mixed in."""
    sd = Path(session_dir)
    s = read_scale(sd)
    h = map_hash(sd)
    if h is None:
        raise ValueError(f"{sd}: no trajectory.json (not a finished map)")
    if s is not None and s.get("map_hash") != h:
        work = sd / "scale_work"
        work.mkdir(exist_ok=True)
        (work / f"scale_stale_{str(s.get('map_hash'))[:12]}.json").write_text(json.dumps(s, indent=1, ensure_ascii=False))
        s = None
    s = s or new_scale(sd)
    if command:
        s["commands"].append(command)
    s["tools"] = _tools()
    return s


# ---------------- geometry ----------------
def triangulate(cams: list, cam_from_world: list[np.ndarray], uv_px: np.ndarray) -> dict:
    """One 3D point from its pixel positions in >= 2 posed views.

    cams: per-view camera objects with cam_from_img / img_from_cam (pycolmap.Camera: distortion handled by the model);
    cam_from_world: per-view 3x4 [R|t]. Linear DLT on undistorted normalised coordinates, then pixel reprojection
    error minimised (3 unknowns). Returns X, reprojection RMS in px, the largest angle between viewing rays and the
    smallest depth (<= 0 means the point is behind a camera)."""
    from scipy.optimize import least_squares
    uv_px = np.asarray(uv_px, float).reshape(-1, 2)
    P = [np.asarray(T, float).reshape(3, 4) for T in cam_from_world]
    if len(P) < 2:
        raise ValueError("need >= 2 views to triangulate")
    xy = np.array([np.asarray(c.cam_from_img(u[None]), float).reshape(2) for c, u in zip(cams, uv_px)])
    A = np.concatenate([[x * T[2] - T[0], y * T[2] - T[1]] for (x, y), T in zip(xy, P)])
    Xh = np.linalg.svd(A)[2][-1]
    X0 = Xh[:3] / Xh[3]

    def resid(X):
        out = []
        for c, T, u in zip(cams, P, uv_px):
            pc = T[:3, :3] @ X + T[:3, 3]
            if pc[2] <= 1e-9:
                out.append([1e3, 1e3])
                continue
            out.append(np.asarray(c.img_from_cam(pc[None]), float).reshape(2) - u)
        return np.concatenate(out)

    X = least_squares(resid, X0, method="lm").x if np.all(np.isfinite(X0)) else X0
    r = resid(X).reshape(-1, 2)
    centres = [-T[:3, :3].T @ T[:3, 3] for T in P]
    rays = [(X - C) / (np.linalg.norm(X - C) or 1.0) for C in centres]
    ang = max(np.degrees(np.arccos(np.clip(a @ b, -1, 1))) for i, a in enumerate(rays) for b in rays[i + 1:])
    depth = min(float((T[:3, :3] @ X + T[:3, 3])[2]) for T in P)
    return {"X": X, "reproj_rms_px": float(np.sqrt((r ** 2).sum(1).mean())), "max_angle_deg": float(ang),
            "min_depth": depth}


def square_metrics(corners_xyz: np.ndarray, side_m: float) -> dict:
    """Side and diagonal lengths of a triangulated square (corners in order) and the scale its sides give."""
    c = np.asarray(corners_xyz, float).reshape(4, 3)
    sides = np.linalg.norm(c - np.roll(c, -1, axis=0), axis=1)
    diags = np.array([np.linalg.norm(c[0] - c[2]), np.linalg.norm(c[1] - c[3])])
    scales = side_m / sides
    return {"side_lengths_units": [round(float(v), 7) for v in sides],
            "diagonal_lengths_units": [round(float(v), 7) for v in diags],
            "squareness": {"side_ratio_max_dev": round(float(sides.max() / sides.min() - 1), 5),
                           "diag_over_side_dev": round(float(abs(diags.mean() / sides.mean() - np.sqrt(2)) / np.sqrt(2)), 5)},
            "metres_per_unit": float(np.median(scales)),
            "rel_std_sides": round(float(scales.std() / scales.mean()), 5)}


def marker_reject_reason(obs: dict) -> str | None:
    """First failed acceptance condition of 3.1, or None."""
    a = MARKER_ACCEPT
    if len(obs.get("frames_used") or []) < a["min_frames"]:
        return f"frames_used {len(obs.get('frames_used') or [])} < {a['min_frames']}"
    if obs.get("corners_triangulated", 0) < 4:
        return "not all 4 corners triangulated"
    if obs["min_triangulation_angle_deg"] < a["min_triangulation_angle_deg"]:
        return f"min_triangulation_angle_deg {obs['min_triangulation_angle_deg']:.2f} < {a['min_triangulation_angle_deg']}"
    if obs["corner_reproj_rms_px"] > a["max_corner_reproj_rms_px"]:
        return f"corner_reproj_rms_px {obs['corner_reproj_rms_px']:.3f} > {a['max_corner_reproj_rms_px']}"
    sq = obs["squareness"]
    if sq["side_ratio_max_dev"] > a["max_side_ratio_dev"]:
        return f"side_ratio_max_dev {sq['side_ratio_max_dev']:.4f} > {a['max_side_ratio_dev']}"
    if sq["diag_over_side_dev"] > a["max_diag_over_side_dev"]:
        return f"diag_over_side_dev {sq['diag_over_side_dev']:.4f} > {a['max_diag_over_side_dev']}"
    return None


def marker_observation(marker_id: int, dict_name: str, side_m: float, views: list[dict]) -> tuple[dict, np.ndarray | None]:
    """views: [{"image": name, "cam": camera, "cam_from_world": 3x4, "corners": 4x2 px}], one per frame.
    Returns (observation for scale.json, triangulated corners 4x3 or None)."""
    obs = {"method": "ARUCO_MARKER", "marker_id": int(marker_id), "dictionary": dict_name, "side_m": float(side_m),
           "frames_used": [v["image"] for v in views], "corners_triangulated": 0, "corner_reproj_rms_px": None,
           "corner_reproj_rms_px_each": None, "min_triangulation_angle_deg": None, "side_lengths_units": None,
           "squareness": {"side_ratio_max_dev": None, "diag_over_side_dev": None}, "metres_per_unit": None,
           "rel_std_sides": None, "accepted": False, "reject_reason": None}
    if len(views) < max(2, MARKER_ACCEPT["min_frames"]):
        obs["reject_reason"] = f"frames_used {len(views)} < {MARKER_ACCEPT['min_frames']}"
        return obs, None
    cams = [v["cam"] for v in views]
    poses = [v["cam_from_world"] for v in views]
    tri = [triangulate(cams, poses, [v["corners"][k] for v in views]) for k in range(4)]
    if any(t["min_depth"] <= 0 or not np.all(np.isfinite(t["X"])) for t in tri):
        obs["reject_reason"] = "a corner triangulated behind a camera"
        return obs, None
    X = np.array([t["X"] for t in tri])
    obs["corners_triangulated"] = 4
    obs["corner_reproj_rms_px_each"] = [round(t["reproj_rms_px"], 4) for t in tri]
    obs["corner_reproj_rms_px"] = round(float(np.sqrt(np.mean([t["reproj_rms_px"] ** 2 for t in tri]))), 4)
    obs["min_triangulation_angle_deg"] = round(min(t["max_angle_deg"] for t in tri), 3)
    obs.update(square_metrics(X, side_m))
    obs["corners_units"] = np.round(X, 7).tolist()
    obs["reject_reason"] = marker_reject_reason(obs)
    obs["accepted"] = obs["reject_reason"] is None
    return obs, X


def pnp_scale(cam, cam_from_world: np.ndarray, corners_px: np.ndarray, side_m: float, centre_units: np.ndarray) -> dict | None:
    """Diagnostic only (not independent of the triangulation, never used for the scale): camera-to-marker distance
    in metres from the known side (IPPE on undistorted normalised corners, so K = I), divided by the same distance
    in map units."""
    import cv2
    xy = np.asarray(cam.cam_from_img(np.asarray(corners_px, float)), float).reshape(4, 1, 2)
    h = side_m / 2  # IPPE_SQUARE corner order: (-h, h), (h, h), (h, -h), (-h, -h) = marker TL, TR, BR, BL
    obj = np.array([[-h, h, 0], [h, h, 0], [h, -h, 0], [-h, -h, 0]], float)
    ok, rvec, tvec = cv2.solvePnP(obj, xy, np.eye(3), None, flags=cv2.SOLVEPNP_IPPE_SQUARE)
    if not ok:
        return None
    T = np.asarray(cam_from_world, float)
    C = -T[:3, :3].T @ T[:3, 3]
    d_units = float(np.linalg.norm(np.asarray(centre_units) - C))
    d_m = float(np.linalg.norm(tvec))
    return {"distance_m": round(d_m, 5), "distance_units": round(d_units, 6),
            "metres_per_unit": round(d_m / d_units, 6) if d_units > 0 else None}


def ground_plane_ransac(xyz: np.ndarray, inlier_units: float | None = None, iters: int = 500, seed: int = 0) -> dict:
    """Dominant plane of a sparse cloud (RANSAC on 3-point samples, then least squares on the inliers).

    inlier_units: distance threshold in map units (default 1 % of the cloud's median spread). The normal has unit
    length and no particular sign (scale_from_height uses unsigned distances). Returns {"normal", "d" (n.x + d = 0), "inlier_ratio", "inlier_units", "rms_units", "n_points"}."""
    P = np.asarray(xyz, float).reshape(-1, 3)
    P = P[np.all(np.isfinite(P), axis=1)]
    if len(P) < 3:
        raise ValueError("need >= 3 points for a plane")
    if inlier_units is None:
        inlier_units = 0.01 * float(np.median(np.linalg.norm(P - np.median(P, 0), axis=1)))
    rng = np.random.default_rng(seed)
    best = None
    for _ in range(iters):
        a, b, c = P[rng.choice(len(P), 3, replace=False)]
        n = np.cross(b - a, c - a)
        if np.linalg.norm(n) < 1e-12:
            continue
        n /= np.linalg.norm(n)
        inl = np.abs(P @ n - n @ a) <= inlier_units
        if best is None or inl.sum() > best.sum():
            best = inl
    if best is None or best.sum() < 3:
        raise ValueError("no plane found")
    Q = P[best]
    mu = Q.mean(0)
    n = np.linalg.svd(Q - mu)[2][-1]
    d = float(-n @ mu)
    r = Q @ n + d
    return {"normal": n.round(7).tolist(), "d": round(d, 7), "inlier_ratio": round(float(best.mean()), 4),
            "inlier_units": float(inlier_units), "rms_units": round(float(np.sqrt((r ** 2).mean())), 7), "n_points": int(len(P))}


def scale_from_height(centres_units: np.ndarray, heights_m: np.ndarray, plane: dict, camera_offset_m: float = 0.0,
                      min_height_m: float = 0.3) -> dict:
    """Cross-check scale (method 3, never the primary scale): telemetry height of the camera above the floor (plus a
    fixed offset between the height reference and the camera) divided by the map distance from the camera centre to
    the floor plane, per synchronised frame; the median is the value. Frames lower than min_height_m are left out
    (a few cm of quantisation is a large fraction there)."""
    C = np.asarray(centres_units, float).reshape(-1, 3)
    H = np.asarray(heights_m, float).reshape(-1) + camera_offset_m
    n, d = np.asarray(plane["normal"], float), float(plane["d"])
    dist = np.abs(C @ n + d)
    ok = np.isfinite(H) & (H >= min_height_m) & (dist > 0)
    if ok.sum() == 0:
        return {"metres_per_unit": None, "n_frames": 0, "reason": "no synchronised frame above min height"}
    ratios = H[ok] / dist[ok]
    m = float(np.median(ratios))
    q1, q3 = np.percentile(ratios, [25, 75])
    return {"metres_per_unit": m, "n_frames": int(ok.sum()), "n_frames_dropped": int((~ok).sum()),
            "iqr_rel": round(float((q3 - q1) / m), 5), "camera_offset_m": camera_offset_m, "min_height_m": min_height_m}


# ---------------- observations ----------------
def _load_model(session_dir: Path, scale: dict) -> dict:
    """{image_name: (camera, cam_from_world 3x4)} of the model the scale is for (COLMAP files when present, else the
    exported trajectory.json, which holds the same poses and refined intrinsics)."""
    import pycolmap
    sd = Path(session_dir)
    cdir = sd / scale["colmap_dir"] if scale.get("colmap_dir") else None
    if cdir is not None and ((cdir / "images.bin").exists() or (cdir / "images.txt").exists()):
        rec = pycolmap.Reconstruction(str(cdir))
        out = {}
        for im in rec.images.values():
            if not im.has_pose:
                continue
            cfw = im.cam_from_world() if callable(im.cam_from_world) else im.cam_from_world
            out[im.name] = (rec.cameras[im.camera_id], np.asarray(cfw.matrix(), float))
        return {"source": str(cdir.relative_to(sd)), "views": out}
    traj = json.loads((sd / "trajectory.json").read_text())
    c = traj["camera"]
    cam = pycolmap.Camera(model=c["model"], width=c["width"], height=c["height"], params=c["params_refined"])
    out = {}
    for p in traj["poses"]:
        T = np.asarray(p["T_world_camera"], float)
        R, C = T[:3, :3], T[:3, 3]
        out[p["image_name"]] = (cam, np.hstack([R.T, (-R.T @ C)[:, None]]))
    return {"source": "trajectory.json", "views": out}


def observe_markers(session_dir: Path, measurements: dict, model_rank: int = 0, dict_name: str = "4X4_50",
                    min_side_px: float = MIN_MARKER_SIDE_PX, command: str | None = None) -> dict:
    """Detect printed markers in the session's selected frames, triangulate their corners with the map's own poses
    and record one observation per measured marker id. measurements: {"markers": {"<id>": {"side_m": ..}},
    "checks": [{"name": .., "true_m": ..}]} (checks become pending verification lengths, measured later in the app).
    Writes scale.json and scale_work/{marker_detections,pnp_scales}.json; returns the judged scale."""
    import cv2
    from .markers import detect, detector, side_lengths_px
    if model_rank != 0:
        raise ValueError("only model rank 0 (the largest model) gets a scale in this version")
    sd = Path(session_dir)
    s = _for_update(sd, command)
    model = _load_model(sd, s)
    want = {int(k): float(v["side_m"]) for k, v in (measurements.get("markers") or {}).items()}
    for mid, side in want.items():
        if not (0.005 <= side <= 10):
            raise ValueError(f"marker {mid}: side_m {side} outside 0.005-10 m")
    det = detector(dict_name)
    detections, per_id = [], {}
    for name in sorted(model["views"]):
        img = cv2.imread(str(sd / "frames" / name))
        if img is None:
            detections.append({"image": name, "error": "frame missing"})
            continue
        cam, T = model["views"][name]
        for mid, corners in detect(img, dict_name, det):
            side_px = float(side_lengths_px(corners).min())
            row = {"image": name, "marker_id": mid, "corners_px": np.round(corners, 3).tolist(),
                   "min_side_px": round(side_px, 2), "used": side_px >= min_side_px and mid in want}
            if side_px < min_side_px:
                row["skip"] = f"min side {side_px:.1f} px < {min_side_px}"
            elif mid not in want:
                row["skip"] = "no measured side length for this id"
            detections.append(row)
            if row["used"]:
                per_id.setdefault(mid, []).append({"image": name, "cam": cam, "cam_from_world": T, "corners": corners})
    work = sd / "scale_work"
    work.mkdir(exist_ok=True)
    (work / "marker_detections.json").write_text(json.dumps(
        {"dictionary": dict_name, "min_side_px": min_side_px, "model_source": model["source"],
         "frames_scanned": len(model["views"]), "detections": detections}, indent=1))
    obs_list, pnp = [], []
    for mid, side in sorted(want.items()):
        views = per_id.get(mid, [])
        obs, X = marker_observation(mid, dict_name, side, views)
        obs["min_marker_side_px"] = min_side_px
        obs["frames_detected"] = sum(1 for d in detections if d.get("marker_id") == mid)
        if not views:
            obs["reject_reason"] = (f"detected in {obs['frames_detected']} frame(s), all smaller than {min_side_px} px"
                                    if obs["frames_detected"] else "marker not detected in any registered frame")
        if X is not None:
            rows = [r for v in views if (r := pnp_scale(v["cam"], v["cam_from_world"], v["corners"], side, X.mean(0)))]
            for v, r in zip(views, rows):
                r["image"] = v["image"]
            vals = [r["metres_per_unit"] for r in rows if r["metres_per_unit"]]
            pnp.append({"marker_id": mid, "frames": rows,
                        "median_metres_per_unit": float(np.median(vals)) if vals else None,
                        "diff_vs_triangulated_pct": round(100 * (np.median(vals) / obs["metres_per_unit"] - 1), 3) if vals else None})
        obs_list.append(obs)
    (work / "pnp_scales.json").write_text(json.dumps(
        {"note": "diagnostic only: same frames and corners as the triangulation, not an independent check",
         "markers": pnp}, indent=1))
    s["observations"] = [o for o in s["observations"] if o["method"] != "ARUCO_MARKER"] + obs_list
    names = {c["name"] for c in s["verification"]["checks"]}
    for c in measurements.get("checks") or []:
        if c.get("name") and c["name"] not in names:
            s["verification"]["checks"].append({"name": str(c["name"])[:80], "true_m": float(c["true_m"]), "map_units": None,
                                                "predicted_m": None, "error_pct": None, "pass": None})
    s = evaluate(s, map_hash(sd))
    write_scale(sd, s)
    return s


def _segment(p1, p2) -> tuple[np.ndarray, np.ndarray, float]:
    a, b = np.asarray(p1, float).reshape(3), np.asarray(p2, float).reshape(3)
    if not (np.all(np.isfinite(a)) and np.all(np.isfinite(b))):
        raise ValueError("points must be finite")
    d = float(np.linalg.norm(b - a))
    if d <= 0:
        raise ValueError("the two points coincide")
    return a, b, d


LENGTH_SOURCES = ("TAPE", "REGULATION")


def add_two_point(session_dir: Path, p1, p2, true_m: float, role: str, note: str = "",
                  pick_tolerance_units: float = 0.0, command: str | None = None, length_source: str = "TAPE") -> dict:
    """A tape-measured length between two map points picked in the viewer: role "fit" adds a scale observation,
    role "verify" an independent check (fills a pending check of the same name if there is one).
    length_source "REGULATION": the true length is a rule-book dimension of a standard object (e.g. a tennis court),
    not measured on site; such a check is evaluated but never counts towards MEASURED_VERIFIED (3.3 asks for
    tape-measured lengths)."""
    if role not in ("fit", "verify"):
        raise ValueError("role must be fit or verify")
    if length_source not in LENGTH_SOURCES:
        raise ValueError(f"length_source must be one of {LENGTH_SOURCES}")
    true_m = float(true_m)
    if not (np.isfinite(true_m) and 0 < true_m <= 10000):
        raise ValueError("true_m must be in (0, 10000] metres")
    tol = float(pick_tolerance_units or 0.0)
    if not (np.isfinite(tol) and tol >= 0):
        raise ValueError("pick_tolerance_units must be >= 0")
    a, b, d = _segment(p1, p2)
    sd = Path(session_dir)
    s = _for_update(sd, command)
    note = str(note or "")[:80]
    seg = {"p1": np.round(a, 7).tolist(), "p2": np.round(b, 7).tolist(), "true_m": true_m, "map_units": round(d, 7),
           "pick_tolerance_units": round(tol, 7), "length_source": length_source}
    if role == "fit":
        s["observations"].append({"method": "TWO_POINT_TAPE", **seg, "metres_per_unit": true_m / d,
                                  "rel_uncertainty": round(2 * tol / d, 5), "note": note, "accepted": True,
                                  "reject_reason": None, "added_at": _now()})
    else:
        pending = next((c for c in s["verification"]["checks"] if note and c["name"] == note and c.get("map_units") is None), None)
        if pending is not None:
            pending.update(seg)
        else:
            n = len(s["verification"]["checks"]) + 1
            s["verification"]["checks"].append({"name": note or f"length {n}", **seg, "predicted_m": None,
                                                "error_pct": None, "pass": None})
    s = evaluate(s, map_hash(sd))
    write_scale(sd, s)
    return s


def set_cross_check(session_dir: Path, method: str, metres_per_unit: float | None, note: str = "", **info) -> dict:
    """Tello telemetry or a metric-depth model: recorded next to the measured scale, never averaged into it."""
    if method not in ("TELLO_TELEMETRY", MODEL_METHOD):
        raise ValueError("cross-check method must be TELLO_TELEMETRY or METRIC_DEPTH_MODEL")
    sd = Path(session_dir)
    s = _for_update(sd, None)
    s["cross_checks"] = [c for c in s["cross_checks"] if c["method"] != method] + [
        {"method": method, "metres_per_unit": metres_per_unit, "diff_vs_primary_pct": None, "note": note, **info}]
    s = evaluate(s, map_hash(sd))
    write_scale(sd, s)
    return s


# ---------------- judgement ----------------
def _same_object(check: dict, fits: list[dict]) -> bool:
    """A verification length must not be a fit length again (same name, or both ends at the same map points)."""
    for f in fits:
        if check.get("name") and f.get("note") and check["name"].strip().casefold() == f["note"].strip().casefold():
            return True
        if "p1" in check and "p1" in f:
            tol = 2 * max(check.get("pick_tolerance_units") or 0, f.get("pick_tolerance_units") or 0, 1e-9)
            a, b = np.asarray(check["p1"]), np.asarray(check["p2"])
            c, d = np.asarray(f["p1"]), np.asarray(f["p2"])
            if (np.linalg.norm(a - c) <= tol and np.linalg.norm(b - d) <= tol) or \
               (np.linalg.norm(a - d) <= tol and np.linalg.norm(b - c) <= tol):
                return True
    return False


def evaluate(s: dict, current_hash: str | None) -> dict:
    """Recompute the scale, its uncertainty, the verification and the status from the observations (in place)."""
    obs = s.get("observations") or []
    acc = [o for o in obs if o["method"] in MEASURED_METHODS and o.get("accepted") and o.get("metres_per_unit")]
    vals = np.array([o["metres_per_unit"] for o in acc], float)
    mpu = float(np.median(vals)) if len(vals) else None
    markers = [o for o in acc if o["method"] == "ARUCO_MARKER"]
    tapes = [o for o in acc if o["method"] == "TWO_POINT_TAPE"]
    mv = np.array([o["metres_per_unit"] for o in markers], float)
    spread_markers = float((mv.max() - mv.min()) / mv.mean()) if len(mv) >= 2 else None
    by_method = [float(np.median([o["metres_per_unit"] for o in g])) for g in (markers, tapes) if g]
    spread_methods = float((max(by_method) - min(by_method)) / np.mean(by_method)) if len(by_method) == 2 else None
    if len(vals) >= 2:
        rel_std = float(vals.std() / vals.mean())
    elif acc:
        rel_std = acc[0].get("rel_std_sides") if acc[0]["method"] == "ARUCO_MARKER" else acc[0].get("rel_uncertainty")
    else:
        rel_std = None
    s["metres_per_unit"] = mpu
    s["uncertainty"] = {"rel_std": rel_std, "n_observations": len(acc), "spread_between_methods_rel": spread_methods,
                        "spread_between_markers_rel": spread_markers}
    s["primary_method"] = "ARUCO_MARKER" if markers else "TWO_POINT_TAPE" if tapes else None
    flags = []
    if spread_markers is not None and spread_markers > VERIFY["max_spread_between_markers_rel"]:
        flags.append("SCALE_DIFFERS_BETWEEN_MARKERS")  # 구간별 스케일 다름 (단안 드리프트 의심)

    fits = [o for o in obs if o["method"] == "TWO_POINT_TAPE"]
    done, not_counted = [], []
    for c in s["verification"]["checks"]:
        c.pop("reason", None)
        if c.get("map_units") is None or mpu is None:
            c.update(predicted_m=None, error_pct=None, **{"pass": None})
            continue
        c["predicted_m"] = round(c["map_units"] * mpu, 5)
        c["error_pct"] = round(100 * abs(c["predicted_m"] - c["true_m"]) / c["true_m"], 3)
        if _same_object(c, fits):
            c["pass"], c["reason"] = False, "same object as a fit length; a check must be independent"
        else:
            c["pass"] = c["error_pct"] <= VERIFY["max_error_pct"]
        if c.get("length_source", "TAPE") != "TAPE":
            c["reason"] = "regulation dimension, not measured on site: shown, not counted for verification"
            not_counted.append(c)
            continue
        done.append(c)
    n_pass = sum(1 for c in done if c["pass"])
    if not_counted or any(o.get("length_source", "TAPE") != "TAPE" for o in acc):
        flags.append("REGULATION_DIMENSIONS")  # 규정 치수 기반, 현장 실측 아님
    criteria = {"independent_checks_ge_2_all_within_5pct": len(done) >= VERIFY["min_checks"] and n_pass == len(done),
                "markers_agree_within_5pct": spread_markers is None or spread_markers <= VERIFY["max_spread_between_markers_rel"],
                "has_accepted_measurement": bool(acc)}
    s["verification"].update(criterion=CRITERION, passed=all(criteria.values()), criteria=criteria,
                             n_checks_done=len(done), n_checks_passed=n_pass, n_checks_not_counted=len(not_counted),
                             max_error_pct=max((c["error_pct"] for c in done), default=None))

    for c in s.get("cross_checks") or []:
        c["diff_vs_primary_pct"] = round(100 * (c["metres_per_unit"] / mpu - 1), 3) if mpu and c.get("metres_per_unit") else None
        if c["method"] == "TELLO_TELEMETRY" and c["diff_vs_primary_pct"] is not None and abs(c["diff_vs_primary_pct"]) > 15:
            flags.append("TELEMETRY_DISAGREES")
        if c["method"] == MODEL_METHOD and c["diff_vs_primary_pct"] is not None and abs(c["diff_vs_primary_pct"]) > 15:
            flags.append("MODEL_DISAGREES")
    model = next((c for c in s.get("cross_checks") or [] if c["method"] == MODEL_METHOD and c.get("metres_per_unit")), None)

    if current_hash is None or s.get("map_hash") != current_hash:
        status, reason = "NONE", "지도가 다시 만들어져 스케일 무효 (map_hash differs)"
    elif acc:
        if s["verification"]["passed"]:
            status, reason = "MEASURED_VERIFIED", "verification passed"
        else:
            failed = [k for k, v in criteria.items() if not v]
            status, reason = "MEASURED_UNVERIFIED", f"verification not passed: {', '.join(failed)}"
    elif model:
        status, reason = "MODEL_ESTIMATED", "모델 추정, 검증 아님"
        s["primary_method"] = MODEL_METHOD
    else:
        rej = [o for o in obs if o["method"] in MEASURED_METHODS]
        status, reason = "NONE", "no accepted measurement" + (f" ({len(rej)} rejected)" if rej else "")
    s["status"], s["status_reason"], s["flags"] = status, reason, flags
    return s


def summary(s: dict) -> dict:
    """What the API sends to the UI (the UI shows this, it does not judge)."""
    from .labels import Scale
    v = s.get("verification") or {}
    usable = s["status"] != "NONE"
    model = next((c for c in s.get("cross_checks") or [] if c["method"] == MODEL_METHOD), None)
    return {"status": s["status"], "status_reason": s.get("status_reason"),
            "metres_per_unit": (s["metres_per_unit"] if s["status"] != "MODEL_ESTIMATED" else (model or {}).get("metres_per_unit")) if usable else None,
            "primary_method": s.get("primary_method") if usable else None,
            "n_observations": (s.get("uncertainty") or {}).get("n_observations", 0),
            "rel_std": (s.get("uncertainty") or {}).get("rel_std"),
            "checks_done": v.get("n_checks_done", 0), "checks_passed": v.get("n_checks_passed", 0),
            "checks_required": VERIFY["min_checks"], "max_error_pct": v.get("max_error_pct"),
            "flags": s.get("flags") or [], "model_rank": s.get("model_rank", 0),
            "derived_scale_label": Scale.METRIC_ANCHORED.value if s["status"] == "MEASURED_VERIFIED" else Scale.RELATIVE.value,
            "updated_at": s.get("updated_at")}
