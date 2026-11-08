"""Experiment E3 (SIMULATED): does the geofence guard stop a drone at a virtual property line?

Real-time loop at 10 Hz over loopback UDP: scripted chase demand (forward 50 = 0.5 m/s plus a slow yaw sweep and
some lateral drift) -> GeofenceGuard -> TelloClient.rc -> MockTello (kinematic toy with velocity lag). The fence is a
10 m x 10 m square around the take-off point (SIMULATED_FRAME_ONLY). Runs:
  guarded        guard on                                  -> must never cross, must report GEOFENCE
  unguarded      same demand, guard off (control)          -> must cross (shows the check can detect a violation)
  rotated        guard on, start heading 30 deg            -> geometry not tied to the x axis
  pose_dropout   guard on, pose unavailable for 1 s        -> translation stops with NO_POSE_FOR_GEOFENCE
Criteria fixed BEFORE running: 0 boundary crossings and GEOFENCE present in guarded runs; the control
crosses; 0 non-loopback packets; HARDWARE_ARMED stays false.
python -m sim.geofence_scenario [--out outputs/evidence/e3_geofence]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import time
from pathlib import Path

from adapters.tello.client import TelloClient, hardware_enabled
from adapters.tello.mock_tello import MockTello
from control.geofence import Geofence, GeofenceGuard
from control.yaw_controller import DroneCommand

HZ, DURATION = 10.0, 16.0
FENCE = Geofence(((-5.0, -5.0), (5.0, -5.0), (5.0, 5.0), (-5.0, 5.0)), margin_m=1.0)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def demand(t: float) -> DroneCommand:
    """Scripted chase demand: keep pushing forward, slow yaw sweep, slight drift right."""
    yaw = 20 if (t // 4) % 2 == 0 else -20
    return DroneCommand(t, yaw=yaw, forward=50, lateral=10)


def run_case(name: str, guard_on: bool, heading0: float = 0.0, pose_gap: tuple[float, float] | None = None) -> dict:
    port = _free_port()
    guard = GeofenceGuard(FENCE, horizon_s=1.0)
    rows = []
    with MockTello(state_port=port, battery=85) as mock:
        mock._heading = heading0
        client = TelloClient(mock.addr, state_port=port)
        client.start_state_listener()
        assert client.simulated, "E3 must only ever talk to loopback"
        assert client.command("command") == "ok" and client.command("takeoff") == "ok"
        t0 = time.monotonic()
        k = 0
        while (t := time.monotonic() - t0) < DURATION:
            k += 1
            pose = mock.sim_pose()
            gap = pose_gap is not None and pose_gap[0] <= t < pose_gap[1]
            want = demand(t)
            vel = tuple(mock._vel)
            cmd = guard.filter(want, None if gap else pose, vel) if guard_on else want
            client.rc(cmd.lateral, cmd.forward, cmd.vertical, cmd.yaw)
            rows.append({"t": round(t, 3), "x": round(pose[0], 3), "y": round(pose[1], 3), "heading": round(pose[2], 1),
                         "signed_dist_m": round(FENCE.signed_distance(pose[0], pose[1]), 3),
                         "sent_fwd": cmd.forward, "sent_lat": cmd.lateral, "sent_yaw": cmd.yaw,
                         "stop": "|".join(cmd.stop_reasons), "pose_gap": gap})
            time.sleep(max(0.0, k / HZ - (time.monotonic() - t0)))
        client.rc(0, 0, 0, 0)
        assert client.command("land") == "ok"
        client.close()
        packets = len(mock.received)
    gf = lambda r: "GEOFENCE" in r["stop"].split("|")  # noqa: E731  exact reason, not NO_POSE_FOR_GEOFENCE
    first_gf = next((r["t"] for r in rows if gf(r)), None)
    after = [r for r in rows if first_gf is not None and r["t"] >= first_gf]
    return {"case": name, "guard_on": guard_on, "heading0_deg": heading0, "pose_gap_s": pose_gap,
            "frames": len(rows), "loop_hz": round(len(rows) / rows[-1]["t"], 2),
            "min_signed_dist_m": min(r["signed_dist_m"] for r in rows),
            "crossed": any(r["signed_dist_m"] < 0 for r in rows),
            "first_geofence_t": first_gf,
            "dist_at_first_geofence_m": next((r["signed_dist_m"] for r in rows if r["t"] == first_gf), None),
            "geofence_blocks": sum(1 for r in rows if gf(r)),
            "translation_allowed_after_first_geofence": sum(1 for r in after if r["sent_fwd"] or r["sent_lat"]),
            "max_inward_dist_lost_after_first_geofence_m": round(max(0.0, (next((r["signed_dist_m"] for r in rows if r["t"] == first_gf), 0) or 0) - min((r["signed_dist_m"] for r in after), default=0)), 3) if after else None,
            "nonzero_yaw_after_geofence": sum(1 for r in after if r["sent_yaw"]),
            "no_pose_stops": sum(1 for r in rows if "NO_POSE_FOR_GEOFENCE" in r["stop"]),
            "translation_during_pose_gap": sum(1 for r in rows if r["pose_gap"] and (r["sent_fwd"] or r["sent_lat"])),
            "mock_packets": packets, "non_loopback_packets": 0 if client.simulated else None,
            "rows": rows}


def run(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    armed_before = hardware_enabled()
    cases = [run_case("guarded", True), run_case("unguarded_control", False),
             run_case("rotated_30deg", True, heading0=30.0), run_case("pose_dropout", True, pose_gap=(3.0, 4.0))]
    g, u, r, p = cases
    checks = {
        "guarded_never_crosses": not g["crossed"] and not r["crossed"] and not p["crossed"],
        "geofence_reported": all(c["first_geofence_t"] is not None for c in (g, r, p)),
        "control_crosses": u["crossed"],
        "no_translation_during_pose_gap": p["translation_during_pose_gap"] == 0 and p["no_pose_stops"] > 0,
        "zero_non_loopback_packets": all(c["non_loopback_packets"] == 0 for c in cases),
        "hardware_armed_false": armed_before is False and hardware_enabled() is False,
    }
    result = {"experiment": "E3 geofence stop", "label": "SIMULATED_FRAME_ONLY (MockTello kinematic toy, loopback UDP)",
              "setup": {"hz": HZ, "duration_s": DURATION, "fence_m": FENCE.polygon, "margin_m": FENCE.margin_m,
                        "horizon_s": 1.0, "mps_per_rc": 0.01, "vel_tau_s": 0.4, "demand": "forward 50 (0.5 m/s), lateral 10, yaw +/-20 every 4 s",
                        "env": {"HARDWARE_ARMED": os.environ.get("HARDWARE_ARMED"), "DRY_RUN": os.environ.get("DRY_RUN")}},
              "checks": checks, "result": "PASS" if all(checks.values()) else "FAIL",
              "cases": [{k: v for k, v in c.items() if k != "rows"} for c in cases],
              "limitations": ["Tello EDU has no absolute position; the guard here reads the simulator's pose",
                              "velocity lag 0.4 s is a toy model, not identified from a real aircraft; wind and pose noise not modelled",
                              "fence is a square in the take-off frame; real property lines need a surveyed map anchor"]}
    (out_dir / "e3_geofence.json").write_text(json.dumps(result, indent=1))
    for c in cases:
        with open(out_dir / f"e3_{c['case']}.csv", "w") as fh:
            keys = list(c["rows"][0])
            fh.write(",".join(keys) + "\n")
            fh.writelines(",".join(str(row[k]) for k in keys) + "\n" for row in c["rows"])
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="outputs/evidence/e3_geofence")
    r = run(Path(ap.parse_args().out))
    print(json.dumps({k: r[k] for k in ("checks", "result", "cases")}, indent=1))
