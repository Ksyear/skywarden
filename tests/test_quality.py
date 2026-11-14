"""Map quality is separate from job completion: a below-quality map must never look like a normal completion."""
import json
import sqlite3

import cv2
import numpy as np

from mapping.quality import THRESHOLDS, evaluate, trajectory_jumps
from mapping.shots import detect_shots
from mapping.sparse_pipeline import PAIR_ID_BASE, _drop_cross_shot_pairs


def line(n, step=1.0):
    return [[i * step, 0.0, 0.0] for i in range(n)]


def test_one_third_registered_is_partial_never_good():
    t = [i / 3 for i in range(150)]
    q = evaluate(t, [list(range(50, 100)), list(range(0, 20))], 0.85, 10_000, line(50))
    assert q["verdict"] == "PARTIAL" and "registered_ratio" in q["failed_checks"]
    assert q["thresholds"]["min_registered_ratio"] == THRESHOLDS.min_registered_ratio == 0.6


def test_two_large_models_are_not_good_even_with_high_ratio():
    t = [i / 3 for i in range(100)]
    q = evaluate(t, [list(range(0, 70)), list(range(70, 100))], 0.8, 5000, line(70))
    assert q["verdict"] != "GOOD" and "models_ge_10pct" in q["failed_checks"]


def test_jump_in_camera_path_fails_the_map():
    c = line(60)
    for i in range(30, 60):
        c[i][0] += 50.0  # the path teleports between frames 29 and 30 (two scenes chained together)
    assert len(trajectory_jumps([i / 3 for i in range(60)], c, 5.0)) == 1
    q = evaluate([i / 3 for i in range(60)], [list(range(60))], 0.7, 5000, c)
    assert q["verdict"] != "GOOD" and q["checks"]["trajectory_jumps"]["value"] == 1


def test_continuous_full_registration_is_good():
    q = evaluate([i / 3 for i in range(60)], [list(range(60))], 0.7, 5000, line(60))
    assert q["verdict"] == "GOOD" and q["failed_checks"] == []


def test_bad_reprojection_or_tiny_ratio_is_poor():
    t = [i / 3 for i in range(100)]
    assert evaluate(t, [list(range(100))], 3.0, 5000, line(100))["verdict"] == "POOR"
    assert evaluate(t, [list(range(20))], 0.5, 5000, line(20))["verdict"] == "POOR"


def _db(path, names, pairs):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE images (image_id INTEGER PRIMARY KEY, name TEXT)")
    con.execute("CREATE TABLE matches (pair_id INTEGER PRIMARY KEY, rows INTEGER)")
    con.execute("CREATE TABLE two_view_geometries (pair_id INTEGER PRIMARY KEY, rows INTEGER)")
    for i, n in enumerate(names, 1):
        con.execute("INSERT INTO images VALUES (?, ?)", (i, n))
    for (a, b), r in pairs.items():
        pid = a * PAIR_ID_BASE + b
        con.execute("INSERT INTO matches VALUES (?, ?)", (pid, r))
        con.execute("INSERT INTO two_view_geometries VALUES (?, ?)", (pid, r))
    con.commit()
    con.close()


def test_matches_across_a_shot_change_are_removed(tmp_path):
    names = [f"f{i:06d}.jpg" for i in range(4)]
    _db(tmp_path / "d.db", names, {(1, 2): 300, (2, 3): 250, (3, 4): 280, (1, 3): 90})
    shot_of = {names[0]: 0, names[1]: 0, names[2]: 1, names[3]: 1}  # cut between frame 1 and 2
    r = _drop_cross_shot_pairs(tmp_path / "d.db", shot_of)
    assert r["cross_shot_pairs_removed"] == 2  # (2,3) and (1,3) cross the cut
    assert r["inliers_to_prev"] == [None, 300, 0, 280]
    con = sqlite3.connect(tmp_path / "d.db")
    assert sorted(p for (p,) in con.execute("SELECT pair_id FROM two_view_geometries")) == [1 * PAIR_ID_BASE + 2, 3 * PAIR_ID_BASE + 4]


def _video(path, frames, fps=30):
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (320, 240))
    for f in frames:
        vw.write(f)
    vw.release()


def _scene(seed, tint):
    """Smooth textured scene (like real footage: neighbouring pixels correlate) with a scene-specific colour cast."""
    rng = np.random.default_rng(seed)
    img = cv2.GaussianBlur(cv2.resize(rng.integers(0, 255, (60, 80, 3), dtype=np.uint8), (960, 720)), (31, 31), 0)
    return np.clip(img.astype(np.int16) // 2 + np.array(tint, np.int16), 0, 255).astype(np.uint8)


def test_two_scene_video_splits_exactly_once_and_static_camera_is_detected(tmp_path):
    a, b = _scene(1, (20, 90, 30)), _scene(2, (120, 30, 20))
    pan = [a[100:340, 10 + 4 * k:330 + 4 * k] for k in range(45)] + [b[100:340, 10 + 4 * k:330 + 4 * k] for k in range(45)]
    _video(tmp_path / "pan.mp4", pan)
    shots, meta = detect_shots(tmp_path / "pan.mp4")
    assert meta["cuts"] == 1 and len(shots) == 2 and abs(shots[1].start_s - 1.5) < 0.05
    assert all(s.camera_motion == "moving" for s in shots)
    _video(tmp_path / "still.mp4", [a[100:340, 100:420].copy() for _ in range(60)])
    shots, meta = detect_shots(tmp_path / "still.mp4")
    assert meta["cuts"] == 0 and shots[0].camera_motion == "static"


def test_server_lists_quality_and_never_ready(tmp_path):
    from desktop.server import Console
    maps = tmp_path / "maps"
    for sid, quality in (("partial", {"verdict": "PARTIAL", "failed_checks": ["registered_ratio"]}), ("legacy", None)):
        d = maps / sid
        d.mkdir(parents=True)
        rep = {"status": "SUCCEEDED", "labels": {}, "reconstruction": {"registered_images": 5, "points3D": 9, "num_models": 2}}
        if quality:
            rep["quality"] = quality
        (d / "map_report.json").write_text(json.dumps(rep))
        (d / "status.json").write_text(json.dumps({"state": "SUCCEEDED"}))
    got = {s["id"]: s["quality"] for s in Console(maps, {}).sessions()}
    assert got == {"partial": "PARTIAL", "legacy": "UNRATED"}
