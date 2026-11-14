"""Experiment E2 (SIMULATED): does the chosen target keep its id when another person crosses during an occlusion?

Fixed camera, synthetic detections with known identities, real MultiTracker + TargetSelector (no control, no network).
Person A (the operator's target) walks left->right, person B walks right->left; their paths cross mid-frame. A is
hidden for `occ` seconds centred on the crossing, so B walks through the spot where A's track is coasting — the
worst case for a motion-only tracker. Control condition: same occlusion without B.

Criteria fixed BEFORE running:
  K1 occ 0.5 s and 1.5 s: target re-locked on A with the same id in >= 95 % of seeds
  K2 occ 2.5 s: target LOST (no re-lock) in 100 % of seeds  (max coast is 2.0 s)
  K3 every condition: the target id NEVER ends up on B (0 switches over all seeds)
python -m sim.two_person_crossing [--out outputs/evidence/e2_two_person] [--seeds 50]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from control.yaw_controller import YawController
from tracking.tracker import Detection, MultiTracker, TargetSelector, TargetState

W, H, FPS = 640, 480, 15.0
SPEED = 90.0          # px/s, both walkers
T_CROSS = 4.0         # s, paths cross at the image centre
SELECT_AT = 1.0       # s, operator picks A
T_END = 9.0
T_EVAL = 6.5          # s, judge the outcome here: after every occlusion has ended (<= 5.5 s) and before A leaves the frame (7.6 s)
FOLLOW_PX = 25.0      # a track "is on" a person when its centre is this close to the person's true centre
OCCLUSIONS = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
CRITERIA = {"K1_same_id_rate_min": 0.95, "K1_occ": [0.5, 1.5], "K2_occ": 2.5, "K2_lost_rate": 1.0, "K3_switches_to_B": 0}


def truth(t: float, who: str, y_sep: float, x_cross: float = W / 2) -> tuple[float, float, float]:
    """Centre x, centre y, box height for person A or B at time t."""
    if who == "A":
        return x_cross + SPEED * (t - T_CROSS), H / 2, 180.0
    return x_cross - SPEED * (t - T_CROSS), H / 2 + y_sep, 150.0


def run_once(occ: float, with_b: bool, seed: int, y_sep: float = 12.0, noise_px: float = 2.0,
             dropout: float = 0.05, trace_on: bool = False, x_cross: float = W / 2) -> dict:
    rng = np.random.default_rng(seed)
    tr, sel = MultiTracker(), TargetSelector()
    # Control path (socket-free): the real YawController with virtual, always-fresh telemetry. Reported separately
    # from the tracker's id accuracy: it measures whether a non-zero yaw is produced for the wrong person or while
    # the target is not LOCKED.
    ctl = YawController(W)
    ctrl = {"yaw_nonzero_frames": 0, "yaw_nonzero_not_locked": 0, "yaw_nonzero_on_B": 0, "yaw_nonzero_A_hidden": 0,
            "locked_while_A_hidden": 0, "ambiguous_frames": 0, "frames_after_select": 0}
    hide = (T_CROSS - occ / 2, T_CROSS + occ / 2)
    on_b = 0
    first_relock = None
    target_id = None
    final_on = None
    trace = []
    for k in range(int(T_EVAL * FPS) + 1):
        t = k / FPS
        people = {"A": truth(t, "A", y_sep, x_cross)}
        if with_b:
            people["B"] = truth(t, "B", y_sep, x_cross)
        dets = []
        for who, (cx, cy, h) in people.items():
            visible = 0 < cx < W and not (who == "A" and hide[0] <= t < hide[1])
            if visible and rng.random() > dropout:
                cx_n, cy_n = cx + rng.normal(0, noise_px), cy + rng.normal(0, noise_px)
                dets.append(Detection((cx_n - h * 0.2, cy_n - h / 2, cx_n + h * 0.2, cy_n + h / 2), 0.9, "SYNTHETIC"))
        tr.update(t, dets)
        if target_id is None and t >= SELECT_AT:
            cands = [x for x in tr.active() if x.state.value == "LOCKED"
                     and np.hypot(*(x.center - np.array(people["A"][:2]))) < FOLLOW_PX]
            if cands:
                target_id = cands[0].id
                sel.select(target_id)
        state, track = sel.evaluate(tr)
        a_hidden = hide[0] <= t < hide[1]
        if target_id is not None:
            cmd = ctl.command(t, state, track, t, t, 85)
            ctrl["frames_after_select"] += 1
            ctrl["ambiguous_frames"] += state.value == "AMBIGUOUS"
            ctrl["locked_while_A_hidden"] += state == TargetState.LOCKED and a_hidden
            if cmd.yaw != 0:
                ctrl["yaw_nonzero_frames"] += 1
                ctrl["yaw_nonzero_not_locked"] += state != TargetState.LOCKED
                ctrl["yaw_nonzero_A_hidden"] += a_hidden
                if track is not None and "B" in people:
                    d = {w: float(np.hypot(*(track.center - np.array(p[:2])))) for w, p in people.items()}
                    ctrl["yaw_nonzero_on_B"] += min(d, key=d.get) == "B" and d["B"] < FOLLOW_PX
        if track is not None:
            d = {w: float(np.hypot(*(track.center - np.array(p[:2])))) for w, p in people.items()}
            near = min(d, key=d.get)
            final_on = near if d[near] < FOLLOW_PX else None
            if state == TargetState.LOCKED and final_on == "B":
                on_b += 1
            if t >= hide[1] and state == TargetState.LOCKED and first_relock is None and occ > 0:
                first_relock = round(t - hide[1], 3)
        else:
            final_on = None
        if trace_on:
            trace.append({"t": round(t, 3), "state": state.value, "on": final_on,
                          "tracks": [(x.id, x.state.value, round(float(x.center[0]))) for x in tr.active()],
                          "dets_x": [round(float(d.center[0])) for d in dets]})
    st = sel.history[-1][1] if sel.history else "SEARCHING"
    return {"occ": occ, "with_b": with_b, "seed": seed, "target_id": target_id, "final_state": st,
            "final_on": final_on, "locked_frames_on_B": on_b, "relock_delay_s": first_relock,
            # re-locked after the occlusion (or never hidden), same id, still positioned on A at T_EVAL. A single dropped
            # detection at T_EVAL (PREDICTING) does not count as failure: the first version did, and the no-occlusion
            # control then scored 96 % — a measurement artifact, fixed 2026-11-09 before the verdict was recorded.
            "same_id_on_A": st in ("LOCKED", "PREDICTING") and final_on == "A" and (occ == 0 or first_relock is not None),
            "switched_to_B": on_b > 0 or final_on == "B",
            "on_B_at_eval": final_on == "B",
            "lost": st == "LOST", "ids_created": tr.stats.created, **ctrl, **({"trace": trace} if trace_on else {})}


def summarise(rows: list[dict]) -> dict:
    out = {}
    for occ in OCCLUSIONS:
        for with_b in (True, False):
            rs = [r for r in rows if r["occ"] == occ and r["with_b"] == with_b]
            delays = [r["relock_delay_s"] for r in rs if r["relock_delay_s"] is not None]
            out[f"occ{occ}_{'crossing' if with_b else 'alone'}"] = {
                "n": len(rs), "same_id_on_A": round(float(np.mean([r["same_id_on_A"] for r in rs])), 3),
                "lost": round(float(np.mean([r["lost"] for r in rs])), 3),
                "switched_to_B": int(sum(r["switched_to_B"] for r in rs)),
                "on_B_at_eval": int(sum(r["on_B_at_eval"] for r in rs)),
                "locked_frames_on_B_total": int(sum(r["locked_frames_on_B"] for r in rs)),
                "control": {k: int(sum(r[k] for r in rs)) for k in ("yaw_nonzero_frames", "yaw_nonzero_not_locked",
                            "yaw_nonzero_on_B", "yaw_nonzero_A_hidden", "locked_while_A_hidden", "ambiguous_frames",
                            "frames_after_select")},
                "relock_delay_s_p50": round(float(np.median(delays)), 3) if delays else None}
    return out


def verdict(summary: dict) -> dict:
    k1 = {o: summary[f"occ{o}_crossing"]["same_id_on_A"] for o in CRITERIA["K1_occ"]}
    k2 = summary[f"occ{CRITERIA['K2_occ']}_crossing"]["lost"]
    k3 = sum(v["switched_to_B"] for v in summary.values())  # strict: any LOCKED frame on B counts
    k3_final = sum(v["on_B_at_eval"] for v in summary.values())
    res = {"K1_same_id_rate": k1, "K1": bool(all(v >= CRITERIA["K1_same_id_rate_min"] for v in k1.values())),
           "K2_lost_rate": k2, "K2": bool(k2 >= CRITERIA["K2_lost_rate"]),
           "K3_total_switches_to_B": int(k3), "K3_note": "strict reading: a single LOCKED frame on B counts (the controller would yaw toward B for that frame)",
           "K3_final_on_B_at_eval_info": int(k3_final), "K3": bool(k3 == CRITERIA["K3_switches_to_B"])}
    res["result"] = "PASS" if res["K1"] and res["K2"] and res["K3"] else "FAIL"
    return res


def run(out_dir: Path, seeds: int = 50) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [run_once(o, b, s) for o in OCCLUSIONS for b in (True, False) for s in range(seeds)]
    summ = summarise(rows)
    # sensitivity: same crossing with the two walkers' rows almost identical (worst geometry), not part of the verdict
    tight = [run_once(o, True, s, y_sep=0.0) for o in (0.5, 1.0, 1.5) for s in range(seeds)]
    tight_s = {f"occ{o}": {"same_id_on_A": round(float(np.mean([r["same_id_on_A"] for r in tight if r["occ"] == o])), 3),
                           "switched_to_B": int(sum(r["switched_to_B"] for r in tight if r["occ"] == o))}
               for o in (0.5, 1.0, 1.5)}
    # Control-path check with the crossing moved right of centre (x=440 px) so that a wrong lock gives a non-zero
    # yaw error; not part of the K1-K3 verdict.
    off = [run_once(o, True, s, x_cross=440.0) for o in OCCLUSIONS for s in range(seeds)]
    ctrl_keys = ("yaw_nonzero_frames", "yaw_nonzero_not_locked", "yaw_nonzero_on_B", "yaw_nonzero_A_hidden",
                 "locked_while_A_hidden", "ambiguous_frames", "frames_after_select")
    control_total = {"centre_crossing_all_700_runs": {k: int(sum(r[k] for r in rows)) for k in ctrl_keys},
                     "off_centre_crossing_x440_350_runs": {k: int(sum(r[k] for r in off)) for k in ctrl_keys},
                     "off_centre_same_id_on_A": {f"occ{o}": round(float(np.mean([r["same_id_on_A"] for r in off if r["occ"] == o])), 3) for o in OCCLUSIONS}}
    result = {"control_path": control_total,
              "measurement_change_log": ["same_id_on_A originally required LOCKED exactly at T_EVAL; changed to re-locked + same id + on A (LOCKED or PREDICTING) because the no-occlusion control scored 0.96 from single dropped detections. Criteria values unchanged."],
              "experiment": "E2 two-person crossing during occlusion", "label": "SIMULATED (synthetic detections, known identities)",
              "setup": {"fps": FPS, "speed_px_s": SPEED, "noise_px": 2.0, "dropout": 0.05, "y_separation_px": 12.0,
                        "box_h_A_B": [180, 150], "seeds": seeds, "follow_px": FOLLOW_PX,
                        "tracker": "MultiTracker defaults (CV Kalman, Mahalanobis gate chi2 9.21, Hungarian, max coast 2.0 s, no appearance Re-ID)"},
              "criteria_fixed_before_run": CRITERIA, "summary": summ, "verdict": verdict(summ),
              "sensitivity_same_row_crossing_not_in_verdict": tight_s}
    (out_dir / "e2_two_person.json").write_text(json.dumps(result, indent=1))
    with open(out_dir / "e2_runs.csv", "w") as fh:
        keys = list(rows[0])
        fh.write(",".join(keys) + "\n")
        for r in rows:
            fh.write(",".join(str(r[k]) for k in keys) + "\n")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="outputs/evidence/e2_two_person")
    ap.add_argument("--seeds", type=int, default=50)
    a = ap.parse_args()
    r = run(Path(a.out), a.seeds)
    print(json.dumps({"summary": r["summary"], "verdict": r["verdict"], "sensitivity": r["sensitivity_same_row_crossing_not_in_verdict"]}, indent=1))
