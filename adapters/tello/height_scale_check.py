"""MOCK-only check of the telemetry height cross-check (docs plan S5): fly a scripted altitude profile on the loopback
MockTello, record the state stream, build a synthetic map of a floor (known metres per map unit) with cameras at
the true heights, then recover the scale from floor plane + recorded `h`. No real vehicle, nothing leaves 127.0.0.1.

  PYTHONPATH=packages:. .venv/bin/python -m adapters.tello.height_scale_check outputs/evidence/metric_scale/tello_mock_height.json
"""
from __future__ import annotations

import ipaddress
import json
import socket
import sys
import time
from pathlib import Path

import numpy as np

from .client import HardwareDisarmed, TelloClient, hardware_enabled
from .mock_tello import MockTello
from .telemetry import TelemetryRecorder, height_at, read_telemetry

PROFILE = [(0.0, 0.8), (1.5, 0.8), (3.5, 1.2), (5.0, 1.2), (6.5, 1.0), (8.0, 1.0)]
TRUE_MPU = 0.37  # metres per synthetic map unit
MAX_ERR_PCT = 1.0  # plan S5: MOCK has no sensor noise, so <= 1 %


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SendAudit:
    """Counts every UDP sendto in this process by destination while active (socket.socket.sendto wrapped)."""

    def __init__(self):
        self.loopback = 0
        self.other: list[str] = []
        self._orig = None

    def __enter__(self):
        self._orig = socket.socket.sendto
        audit = self

        def sendto(sock, data, *args):
            addr = args[-1]
            host = addr[0] if isinstance(addr, tuple) else str(addr)
            try:
                lo = ipaddress.ip_address(host).is_loopback
            except ValueError:
                lo = False
            if lo:
                audit.loopback += 1
            else:
                audit.other.append(host)
            return audit._orig(sock, data, *args)
        socket.socket.sendto = sendto
        return self

    def __exit__(self, *exc):
        socket.socket.sendto = self._orig


def fly_profile(profile, telemetry_path: Path, frame_hz: float = 5.0, settle_s: float = 0.3) -> dict:
    """Take off on the mock, record telemetry, and note (frame time, exact profile height) pairs like a camera would."""
    port = free_udp_port()
    frames = []
    with MockTello(state_port=port, altitude_profile=profile) as mock:
        c = TelloClient(mock.addr, state_port=port)
        c.start_state_listener()
        assert c.simulated
        rec = TelemetryRecorder(c, telemetry_path, note="MockTello altitude profile, SIMULATED").start()
        c.command("command")
        c.command("takeoff")
        t_end = mock.takeoff_t + profile[-1][0]
        time.sleep(settle_s)
        while time.monotonic() < t_end:
            t = time.monotonic()
            frames.append((t, mock.true_height_m(t)))
            time.sleep(1.0 / frame_hz)
        c.command("land")
        rec.stop()
        c.close()
    return {"frames": frames, "telemetry": read_telemetry(telemetry_path)}


def synthetic_map(heights_m: np.ndarray, seed: int = 0, n_floor: int = 3000, outlier_frac: float = 0.2) -> dict:
    """Floor z=0 (metres) with 2 mm noise plus off-floor clutter, cameras at the given heights; everything moved by a
    random rotation/translation and divided by TRUE_MPU, i.e. what a RELATIVE map of that room would hold."""
    rng = np.random.default_rng(seed)
    n_out = int(n_floor * outlier_frac)
    floor = np.c_[rng.uniform(-3, 3, (n_floor, 2)), rng.normal(0, 0.002, n_floor)]
    clutter = np.c_[rng.uniform(-3, 3, (n_out, 2)), rng.uniform(0.05, 2.0, n_out)]
    k = len(heights_m)
    cams = np.c_[np.linspace(-1.5, 1.5, k), 0.3 * np.sin(np.linspace(0, 3, k)), heights_m]
    q = rng.normal(size=4)
    q /= np.linalg.norm(q)
    w, x, y, z = q
    R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    t = rng.normal(0, 5, 3)
    to_map = lambda P: (P @ R.T + t) / TRUE_MPU  # noqa: E731
    return {"points": to_map(np.r_[floor, clutter]), "centres": to_map(cams)}


def run(out: Path) -> dict:
    from mapping import metric_scale  # needs packages/ on the path (see the command above)
    tel_path = out.with_name(out.stem + "_telemetry.jsonl")
    with SendAudit() as audit:
        flight = fly_profile(PROFILE, tel_path)
        real = TelloClient(("192.168.10.1", 8889), env={})  # unarmed: must refuse before any packet
        refused = 0
        for send in (lambda: real.command("command", 0.01), lambda: real.command("takeoff", 0.01),
                     lambda: real.rc(0, 0, 0, 0), lambda: real.command("land", 0.01)):
            try:
                send()
            except HardwareDisarmed:
                refused += 1
        real.close()
    tel = flight["telemetry"]
    frames = [(t, h) for t, h in flight["frames"] if h is not None]
    tel_h = np.array([height_at(tel, t) if height_at(tel, t) is not None else np.nan for t, _ in frames])
    true_h = np.array([h for _, h in frames])
    m = synthetic_map(true_h)
    plane = metric_scale.ground_plane_ransac(m["points"], seed=1)
    res = metric_scale.scale_from_height(m["centres"], tel_h, plane)
    err = 100 * (res["metres_per_unit"] / TRUE_MPU - 1) if res["metres_per_unit"] else None
    exact = metric_scale.scale_from_height(m["centres"], true_h, plane)
    return {
        "test": "S5 telemetry height cross-check, MOCK only",
        "data": "SIMULATED: loopback MockTello state packets + synthetic floor map with known metres per unit",
        "hardware": {"HARDWARE_ARMED_default_false": not hardware_enabled({}), "real_vehicle_used": False,
                     "udp_sends_loopback": audit.loopback, "udp_sends_non_loopback": len(audit.other),
                     "non_loopback_destinations": audit.other, "unarmed_real_address_refused": refused},
        "profile_m": PROFILE, "telemetry_file": str(tel_path), "telemetry_packets": len(tel),
        "telemetry_h_values_cm": sorted({s["h"] for s in tel}),
        "frames": len(frames), "frames_with_telemetry": int(np.isfinite(tel_h).sum()),
        "telemetry_vs_true_height_cm": {"median_abs": round(float(np.nanmedian(np.abs(tel_h - true_h)) * 100), 3),
                                        "max_abs": round(float(np.nanmax(np.abs(tel_h - true_h)) * 100), 3)},
        "ground_plane": plane, "true_metres_per_unit": TRUE_MPU,
        "height_scale": res, "error_pct": round(err, 4) if err is not None else None,
        "height_scale_with_exact_heights": exact["metres_per_unit"],
        "criterion": f"|error| <= {MAX_ERR_PCT} % (MOCK, no sensor noise)",
        "result": "PASS" if err is not None and abs(err) <= MAX_ERR_PCT and not audit.other and refused == 4 else "FAIL",
        "role": "cross_checks only (TELLO_TELEMETRY); never the primary scale, never changes the status",
        "limits": ["real Tello h/tof are coarse and may mix barometer values (not measured here)",
                   "tof reads the surface right below (a table is not the floor)",
                   "camera offset above the height reference is unknown until measured on a real aircraft",
                   "real flight needs user approval and is NOT_RUN"],
        "created_at": metric_scale._now(),
        "command": " ".join(["python -m adapters.tello.height_scale_check", *sys.argv[1:]]),
    }


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "outputs/evidence/metric_scale/tello_mock_height.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    res = run(out)
    out.write_text(json.dumps(res, indent=1))
    print(json.dumps({k: res[k] for k in ("hardware", "telemetry_packets", "frames_with_telemetry", "height_scale",
                                          "error_pct", "result")}, indent=1))
    return 0 if res["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
