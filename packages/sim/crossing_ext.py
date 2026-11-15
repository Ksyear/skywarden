"""Experiment E2X (SIMULATED): harder two-person cases than E2, with ground-truth identities, for comparing tracker
association modes (docs/PLAN_OCCLUSION_TRACKING.md section 3). Conditions were fixed before any run:

  X1 same_row      A and B cross on the same row (y separation 0), A hidden 0 / 0.5 / 1.0 / 1.5 s at the crossing
  X2 score_drop    A walks BEHIND B (A smaller box); while their boxes overlap (IoU > 0.3) A's score is 0.12-0.30,
                   as real detectors score a partly hidden person lower. No forced hiding.
  X3 side_by_side  B joins A, walks 40 px beside A for 3 s in the same direction, then splits off
  X4 camera_pan    E2 geometry (occ 0.5 / 1.0 s) seen by a panning camera (up to ~120 px/s); a textured background
                   frame moving with the camera is rendered so camera-motion compensation has something to track
  X5 appearance    X1 (occ 0.5 / 1.0) and X2 with synthetic appearance vectors: "distinct" (two people ~0.4 cosine
                   distance apart) or "identical" (appearance cannot tell them apart). Tests the decision logic only.

Fixed camera 640x480, 15 fps, Gaussian position noise 2 px, 5 % dropout, seeds 0..N-1, real MultiTracker +
TargetSelector + YawController (virtual telemetry, nothing transmitted). Synthetic data, labelled SYNTHETIC.
python -m sim.crossing_ext --variant V0|VA|VAB|VABC [--seeds 50] [--out outputs/evidence/occlusion_tracking/e2x_<variant>]
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import cv2
import numpy as np

from control.yaw_controller import YawController
from tracking.tracker import Detection, MultiTracker, TargetSelector, TargetState, TrackerConfig

W, H, FPS = 640, 480, 15.0
SPEED, T_CROSS, SELECT_AT, T_EVAL, FOLLOW_PX = 90.0, 4.0, 1.0, 6.5, 25.0


def variant_config(name: str) -> TrackerConfig:
    """The four compared trackers (+ VA_full, informational). Parameters fixed from E2 + the MOT17 calibration
    sequences only (outputs/evidence/occlusion_tracking/calib*/, decision in calibration_decision.json): margin 4,
    size term on; low-score stage, ORU and direction term did not improve calibration IDF1 and are off."""
    if name == "V0":
        return TrackerConfig()
    if name == "VA_full":
        return TrackerConfig(assoc="margin", low_score=0.1, oru=True, ocm_weight=2.0)
    a = dict(assoc="margin", low_score=None, oru=False, ocm_weight=0.0)
    if name == "VA":
        return TrackerConfig(**a)
    if name == "VAB":
        return TrackerConfig(**a, gmc=True)
    if name == "VABC":
        return TrackerConfig(**a, gmc=True, reid=True)
    raise ValueError(name)


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix * iy
    return ix * iy / u if u > 0 else 0.0


def box(cx, cy, h):
    return (cx - 0.2 * h, cy - h / 2, cx + 0.2 * h, cy + h / 2)


def people_at(cond: str, t: float, occ: float) -> dict:
    """{who: (cx, cy, h, hidden)} in WORLD pixels (before the camera pan)."""
    hide = (T_CROSS - occ / 2, T_CROSS + occ / 2)
    if cond in ("same_row", "camera_pan"):
        y_sep = 0.0 if cond == "same_row" else 12.0
        return {"A": (W / 2 + SPEED * (t - T_CROSS), H / 2, 180.0, hide[0] <= t < hide[1]),
                "B": (W / 2 - SPEED * (t - T_CROSS), H / 2 + y_sep, 150.0, False)}
    if cond == "score_drop":  # A behind (smaller, slightly higher in the image), B in front
        return {"A": (W / 2 + SPEED * (t - T_CROSS), H / 2 - 10, 150.0, False),
                "B": (W / 2 - SPEED * (t - T_CROSS), H / 2 + 8, 180.0, False)}
    if cond == "side_by_side":
        ax = 80 + 70 * t
        # B's offset from A: 200 px -> 40 px (t 1.5..2.5), 40 px until 5.0, back to 200 px by 6.0
        off = 200 if t < 1.5 else 200 - 160 * min(1, (t - 1.5)) if t < 2.5 else 40 if t < 5.0 else 40 + 160 * min(1, t - 5.0)
        return {"A": (ax, H / 2, 180.0, False), "B": (ax + off, H / 2 + 6, 165.0, False)}
    raise ValueError(cond)


def pan_dx(t: float) -> float:
    return 76.0 * math.sin(math.pi * t / 2)  # peak camera speed 76 * pi / 2 = 119 px/s


class Background:
    """Textured world wider than the view; the camera crop slides over it (X4 only)."""

    def __init__(self, seed: int = 7):
        rng = np.random.default_rng(seed)
        self.pad = 160
        noise = rng.integers(0, 255, (H, W + 2 * self.pad), dtype=np.uint8)
        self.img = cv2.GaussianBlur(noise, (0, 0), 2.0)
        self.img = cv2.cvtColor(self.img, cv2.COLOR_GRAY2BGR)

    def frame(self, dx: float, boxes: list) -> np.ndarray:
        M = np.float32([[1, 0, -(self.pad + dx)], [0, 1, 0]])
        f = cv2.warpAffine(self.img, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        for (x1, y1, x2, y2), col in boxes:
            cv2.rectangle(f, (int(x1), int(y1)), (int(x2), int(y2)), col, -1)
        return f


def make_appearance(kind: str, seed: int):
    """Synthetic appearance: per-person base vectors; 'distinct' ~0.4 cosine distance apart, 'identical' equal.
    Each call adds per-detection noise (~0.05-0.15 cosine distance to the base)."""
    rng = np.random.default_rng(1000 + seed)
    a = rng.normal(size=128)
    a /= np.linalg.norm(a)
    r = rng.normal(size=128)
    r -= (r @ a) * a
    r /= np.linalg.norm(r)
    b = a.copy() if kind == "identical" else 0.6 * a + 0.8 * r  # cos(a, b) = 0.6 -> distance 0.4
    base = {"A": a, "B": b / np.linalg.norm(b)}

    def embed(frame, boxes):
        out = []
        for bx in boxes:
            c = np.array([(bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2])
            who = min(frame, key=lambda w: float(np.hypot(*(c - np.array(frame[w][:2])))))
            v = base[who] + rng.normal(0, 0.035, 128)
            out.append(v / np.linalg.norm(v))
        return np.array(out, np.float32)
    return embed


def run_once(cond: str, occ: float, seed: int, cfg: TrackerConfig, appearance: str | None = None,
             noise_px: float = 2.0, dropout: float = 0.05) -> dict:
    rng = np.random.default_rng(seed)
    embed = make_appearance(appearance, seed) if appearance else None
    tr, sel, ctl = MultiTracker(cfg, embedder=embed), TargetSelector(), YawController(W)
    bg = Background() if cond == "camera_pan" else None
    target_id, first_relock, final_on = None, None, None
    m = {"wrong_locked_frames": 0, "yaw_on_B": 0, "yaw_not_locked": 0, "ambiguous_frames": 0, "not_locked_frames": 0,
         "frames_after_select": 0, "locked_while_A_hidden": 0}
    gt_last: dict[str, int] = {}
    gt_greedy: dict[str, int] = {}
    idsw = idsw_greedy = 0
    hide_end = T_CROSS + occ / 2
    for k in range(int(T_EVAL * FPS) + 1):
        t = k / FPS
        dx = pan_dx(t) if bg is not None else 0.0
        world = people_at(cond, t, occ)
        view = {w: (cx - dx, cy, h, hid) for w, (cx, cy, h, hid) in world.items()}
        boxes = {w: box(cx, cy, h) for w, (cx, cy, h, _) in view.items()}
        dets, truth_frame = [], {}
        for who, (cx, cy, h, hid) in view.items():
            if hid or not (0 < cx < W) or rng.random() <= dropout:
                continue
            score = 0.9
            if cond == "score_drop" and who == "A" and _iou(boxes["A"], boxes["B"]) > 0.3:
                score = float(rng.uniform(0.12, 0.30))
            cxn, cyn = cx + rng.normal(0, noise_px), cy + rng.normal(0, noise_px)
            dets.append(Detection(box(cxn, cyn, h), score, "SYNTHETIC"))
            truth_frame[who] = (cxn, cyn)
        if bg is not None:  # front person drawn last
            order = sorted(view, key=lambda w: view[w][2])
            frame = bg.frame(dx, [(boxes[w], (40, 40, 200) if w == "A" else (200, 60, 40)) for w in order if not view[w][3]])
        else:
            frame = dict(truth_frame) if embed else None  # only people actually detected this frame
        tr.update(t, dets, frame=frame)
        # ground-truth id switches over LOCKED tracks, CLEAR-MOT style: a person keeps last frame's track while it is
        # still LOCKED within FOLLOW_PX; the rest are matched one-to-one (nearest first); a switch is a person matched
        # to a different track than last time. (Round 1 matched each person greedily and independently, so two people
        # 0-5 px apart at the crossing could both count the same track: idsw_gt_greedy, kept for comparison.)
        locked = {x.id: x.center for x in tr.active() if x.state.value == "LOCKED"}
        pos = {w: np.array(p[:2]) for w, p in view.items()}
        match, used = {}, set()
        for who in pos:
            tid = gt_last.get(who)
            if tid in locked and np.hypot(*(locked[tid] - pos[who])) < FOLLOW_PX:
                match[who] = tid
                used.add(tid)
        pairs = sorted((float(np.hypot(*(c - pos[w]))), w, i) for w in pos if w not in match for i, c in locked.items() if i not in used)
        for dd, w, i in pairs:
            if dd < FOLLOW_PX and w not in match and i not in used:
                match[w] = i
                used.add(i)
        for who, tid in match.items():
            if who in gt_last and gt_last[who] != tid:
                idsw += 1
            gt_last[who] = tid
        for who, (cx, cy, _, _) in view.items():
            near = [(float(np.hypot(*(c - np.array([cx, cy])))), i) for i, c in locked.items()]
            near = [n for n in near if n[0] < FOLLOW_PX]
            if near:
                tid = min(near)[1]
                if who in gt_greedy and gt_greedy[who] != tid:
                    idsw_greedy += 1
                gt_greedy[who] = tid
        if target_id is None and t >= SELECT_AT:
            ax, ay = view["A"][:2]
            cands = [x for x in tr.active() if x.state.value == "LOCKED" and np.hypot(*(x.center - np.array([ax, ay]))) < FOLLOW_PX]
            if cands:
                target_id = cands[0].id
                sel.select(target_id)
        state, track = sel.evaluate(tr)
        if target_id is None:
            continue
        a_hidden = view["A"][3]
        cmd = ctl.command(t, state, track, t, t, 85)
        m["frames_after_select"] += 1
        m["ambiguous_frames"] += state == TargetState.AMBIGUOUS
        m["not_locked_frames"] += state != TargetState.LOCKED
        m["locked_while_A_hidden"] += state == TargetState.LOCKED and a_hidden
        if cmd.yaw != 0 and state != TargetState.LOCKED:
            m["yaw_not_locked"] += 1
        if track is not None:
            d = {w: float(np.hypot(*(track.center - np.array(p[:2])))) for w, p in view.items()}
            near = min(d, key=d.get)
            final_on = near if d[near] < FOLLOW_PX else None
            if state == TargetState.LOCKED and (final_on == "B" or a_hidden):
                m["wrong_locked_frames"] += 1
            if cmd.yaw != 0 and final_on == "B":
                m["yaw_on_B"] += 1
            if t >= hide_end and state == TargetState.LOCKED and first_relock is None:
                first_relock = round(t - hide_end, 3)
        else:
            final_on = None
    st = sel.history[-1][1] if sel.history else "SEARCHING"
    return {"cond": cond, "occ": occ, "appearance": appearance, "seed": seed, "target_id": target_id, "final_state": st,
            "final_on": final_on, "same_id_on_A": target_id is not None and st in ("LOCKED", "PREDICTING") and final_on == "A"
            and (occ == 0 or first_relock is not None), "idsw_gt": idsw, "idsw_gt_greedy": idsw_greedy, "ids_created": tr.stats.created,
            "relock_delay_s": first_relock, **m,
            "stats": {k: v for k, v in tr.stats.__dict__.items() if k in ("margin_resolved", "low_score_hits", "oru_reupdates",
                                                                            "gmc_applied", "reid_vetoes", "reid_contested_uses",
                                                                            "contested_reconnect_refused")}}


CONDITIONS = ([("same_row", o, None) for o in (0.0, 0.5, 1.0, 1.5)] + [("score_drop", 0.0, None), ("side_by_side", 0.0, None)]
              + [("camera_pan", o, None) for o in (0.5, 1.0)]
              + [(c, o, a) for a in ("distinct", "identical") for c, o in (("same_row", 0.5), ("same_row", 1.0), ("score_drop", 0.0))])


def key(cond, occ, app) -> str:
    return f"{cond}_occ{occ}" + (f"_app-{app}" if app else "")


def run(variant: str, seeds: int, out: Path) -> dict:
    cfg = variant_config(variant)
    rows = [run_once(c, o, s, cfg, a) for c, o, a in CONDITIONS for s in range(seeds)]
    summ = {}
    for c, o, a in CONDITIONS:
        rs = [r for r in rows if (r["cond"], r["occ"], r["appearance"]) == (c, o, a)]
        summ[key(c, o, a)] = {"n": len(rs), "same_id_on_A": round(float(np.mean([r["same_id_on_A"] for r in rs])), 3),
                              "selected": sum(r["target_id"] is not None for r in rs),
                              **{k: int(sum(r[k] for r in rs)) for k in ("wrong_locked_frames", "yaw_on_B", "yaw_not_locked",
                                                                          "ambiguous_frames", "not_locked_frames", "frames_after_select",
                                                                          "locked_while_A_hidden", "idsw_gt", "idsw_gt_greedy", "ids_created")},
                              "stats": {k: int(sum(r["stats"][k] for r in rs)) for k in rs[0]["stats"]}}
    res = {"experiment": "E2X harder two-person cases", "label": "SIMULATED (synthetic detections, known identities)",
           "variant": variant, "tracker": cfg.describe(), "tracker_config": cfg.__dict__, "seeds": seeds,
           "setup": {"fps": FPS, "W_H": [W, H], "noise_px": 2.0, "dropout": 0.05, "follow_px": FOLLOW_PX, "t_eval": T_EVAL},
           "measurement_change_log": ["2026-11-15 round 2: idsw_gt changed from greedy per-person nearest matching to CLEAR-MOT-style "
                                      "persistent one-to-one matching (round-1 values kept as idsw_gt_greedy). Criteria unchanged."],
           "summary": summ}
    out.mkdir(parents=True, exist_ok=True)
    (out / "e2x.json").write_text(json.dumps(res, indent=1))
    with open(out / "e2x_runs.csv", "w") as fh:
        ks = [k for k in rows[0] if k != "stats"]
        fh.write(",".join(ks) + "\n")
        for r in rows:
            fh.write(",".join(str(r[k]) for k in ks) + "\n")
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="V0", choices=["V0", "VA", "VA_full", "VAB", "VABC"])
    ap.add_argument("--seeds", type=int, default=50)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    r = run(a.variant, a.seeds, Path(a.out or f"outputs/evidence/occlusion_tracking/e2x_{a.variant}"))
    print(json.dumps({k: {kk: v[kk] for kk in ("same_id_on_A", "wrong_locked_frames", "ambiguous_frames", "not_locked_frames", "idsw_gt")}
                      for k, v in r["summary"].items()}, indent=0))
