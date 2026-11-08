"""Closed-loop SIMULATED scenario: synthetic detections -> tracker -> target selector -> yaw PID -> TelloClient
-> MockTello over loopback UDP. The virtual camera heading comes back from MockTello's state stream, so the yaw
sign and loop behaviour are exercised end to end. Runs in real time; nothing here touches real hardware.

python -m sim.tracking_scenario [--out outputs/evidence/m3]
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import socket
import time
from pathlib import Path

import numpy as np

from adapters.tello.client import TelloClient
from adapters.tello.mock_tello import MockTello
from control.yaw_controller import YawController
from tracking.tracker import Detection, MultiTracker, TargetSelector, TargetState

W, H, F_PX, FPS, DURATION = 640, 480, 500.0, 15.0, 20.0
OCCLUSIONS = [(6.0, 6.5), (9.0, 10.5), (14.0, 16.5)]  # target hidden: 0.5 s, 1.5 s, 2.5 s
FREEZE = (12.0, 12.6)  # camera delivers no new frame
SELECT_AT = 1.5


def bearing_a(t: float) -> float:  # degrees, person A walks from +20 deg toward -10 deg and back
    return 20.0 - 30.0 * math.sin(min(t, 18.0) / 18.0 * math.pi)


def bearing_b(t: float) -> float:
    return -25.0 + 2.0 * math.sin(t)


def project(bearing_deg: float, cam_yaw_deg: float, depth_px_h: float) -> Detection | None:
    rel = math.radians(bearing_deg - cam_yaw_deg)
    if abs(rel) >= math.radians(60):
        return None
    cx = W / 2 + F_PX * math.tan(rel)
    if not (0 < cx < W):
        return None
    return Detection((cx - 30, H / 2 - depth_px_h / 2, cx + 30, H / 2 + depth_px_h / 2), 0.9, "SYNTHETIC")


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def run(out_dir: Path, seed: int = 0) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    port = free_port()
    rows, proc_ms = [], []
    with MockTello(state_port=port, battery=85) as mock:
        client = TelloClient(mock.addr, state_port=port)
        client.start_state_listener()
        assert client.simulated, "scenario must only ever talk to loopback"
        assert client.command("command") == "ok" and client.command("takeoff") == "ok"
        tracker, selector, ctl = MultiTracker(), TargetSelector(), YawController(W)
        t0 = time.monotonic()
        last_frame_t = None
        k = 0
        while (now_rel := time.monotonic() - t0) < DURATION:
            k += 1
            st = client.latest_state()
            cam_yaw = st.data["yaw"] if st else 0.0
            frozen = FREEZE[0] <= now_rel < FREEZE[1]
            p0 = time.perf_counter()
            if not frozen:
                last_frame_t = time.monotonic()
                hidden = any(a <= now_rel < b for a, b in OCCLUSIONS)
                dets = [d for d in (None if hidden else project(bearing_a(now_rel), cam_yaw, 180),
                                    project(bearing_b(now_rel), cam_yaw, 150)) if d is not None]
                dets = [Detection(tuple(v + rng.normal(0, 2) for v in d.bbox_xyxy), d.score, d.source) for d in dets]
                tracker.update(last_frame_t - t0, dets)
                if selector.target_id is None and now_rel >= SELECT_AT:
                    # operator picks the track nearest person A's expected position (one-time manual choice)
                    exp = project(bearing_a(now_rel), cam_yaw, 180)
                    cands = [tr for tr in tracker.active() if tr.state.value == "LOCKED"]
                    if exp and cands:
                        selector.select(min(cands, key=lambda tr: abs(tr.center[0] - exp.center[0])).id)
            tstate, track = selector.evaluate(tracker)
            now = time.monotonic()
            cmd = ctl.command(now, tstate, track, last_frame_t or now - 99, st.t if st else None, st.data["bat"] if st else None)
            proc_ms.append((time.perf_counter() - p0) * 1000)
            client.rc(cmd.lateral, cmd.forward, cmd.vertical, cmd.yaw)
            true_a = project(bearing_a(now_rel), cam_yaw, 180)
            rows.append({"t": round(now_rel, 3), "cam_yaw_deg": cam_yaw, "target_state": tstate.value,
                         "target_id": selector.target_id, "rc_yaw": cmd.yaw, "stop": "|".join(cmd.stop_reasons),
                         "true_err_px": None if true_a is None else round(true_a.center[0] - W / 2, 1),
                         "frozen": frozen})
            time.sleep(max(0.0, k / FPS - (time.monotonic() - t0)))
        client.rc(0, 0, 0, 0)
        assert client.command("land") == "ok"
        client.close()
        received_rc = sum(1 for c in mock.received if c.startswith("rc "))

    with open(out_dir / "tracking_timeline.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    def phase(a, b):
        return [r for r in rows if a <= r["t"] < b]

    def first_state_after(t, state):
        return next((r["t"] for r in rows if r["t"] >= t and r["target_state"] == state), None)

    locked_err = [abs(r["true_err_px"]) for r in phase(4.0, 6.0) if r["target_state"] == "LOCKED" and r["true_err_px"] is not None]
    summary = {
        "label": "SIMULATED closed loop (synthetic detector, MockTello over loopback UDP)",
        "frames": len(rows), "duration_s": DURATION, "target_fps": FPS,
        "measured_loop_hz": round(len(rows) / rows[-1]["t"], 2),
        "processing_ms_p50": round(float(np.percentile(proc_ms, 50)), 3),
        "processing_ms_p95": round(float(np.percentile(proc_ms, 95)), 3),
        "target_ids_used": sorted({r["target_id"] for r in rows if r["target_id"] is not None}),
        "state_history": selector.history,
        "centering_mean_abs_err_px_4_6s": round(float(np.mean(locked_err)), 1) if locked_err else None,
        "initial_err_px": next((r["true_err_px"] for r in rows if r["t"] >= SELECT_AT), None),
        "occlusions": [{
            "hidden_s": round(b - a, 1),
            "predicting_at": first_state_after(a, "PREDICTING"),
            "relocked_at": first_state_after(b, "LOCKED") if first_state_after(b, "LOCKED") and first_state_after(b, "LOCKED") < b + 1.0 else None,
            "lost_at": first_state_after(a, "LOST") if (first_state_after(a, "LOST") or 1e9) < b + 1.0 else None,
            "nonzero_yaw_while_not_locked": sum(1 for r in phase(a, b) if r["target_state"] != "LOCKED" and r["rc_yaw"] != 0),
        } for a, b in OCCLUSIONS],
        "freeze": {"stale_frame_stops": sum(1 for r in phase(*FREEZE) if "STALE_FRAME" in r["stop"]),
                   "nonzero_yaw_during_freeze_after_0.3s": sum(1 for r in phase(FREEZE[0] + 0.35, FREEZE[1]) if r["rc_yaw"] != 0)},
        "final_state": rows[-1]["target_state"],
        "nonzero_yaw_after_lost": sum(1 for r in rows if r["target_state"] == "LOST" and r["rc_yaw"] != 0),
        "mock_rc_packets": received_rc,
        "real_hardware_packets": 0 if client.simulated else None,
    }
    (out_dir / "tracking_sim.json").write_text(json.dumps(summary, indent=1))
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="outputs/evidence/m3")
    print(json.dumps(run(Path(ap.parse_args().out)), indent=1))
