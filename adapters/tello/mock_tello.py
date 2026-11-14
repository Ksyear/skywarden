"""Loopback MockTello: answers SDK 2.0 commands and streams simulated state. Never a real vehicle.

Fault injection (seeded): reply drop probability, reply latency, battery drain. Flight is a kinematic toy:
takeoff sets h=80 cm, rc yaw integrates heading, a 15 s command silence while flying triggers auto-land
(mirrors the documented SDK safety behaviour).

SIMULATED planar pose: rc forward/lateral drive a velocity with a first-order lag (`vel_tau_s`, so the toy keeps
moving briefly after a STOP like a real aircraft) at `mps_per_rc` m/s per rc unit. `sim_pose()` exposes it in a
SIMULATED_FRAME_ONLY frame (x forward at take-off heading, y to the right, metres). A real Tello reports no such
position (only mission-pad x/y/z), so this exists to test logic such as a geofence guard, not localisation.

Scripted altitude (`altitude_profile`, [(seconds since take-off, metres), ...], linear in between, last value held):
while flying, `h` follows it in whole cm like the real state packet and `tof` = h + `tof_offset_cm` (flat floor below,
the sensor sitting that far under the reference point). `true_height_m(t)` gives the exact profile value at a
monotonic time, for tests that compare a telemetry-derived scale with the truth.
"""
from __future__ import annotations

import math
import random
import socket
import threading
import time

from .protocol import format_state, verb

QUERY_REPLIES = {"battery?": lambda s: str(s["bat"]), "sdk?": lambda s: "30", "sn?": lambda s: "MOCK0000000000",
                 "height?": lambda s: f"{s['h']}dm", "time?": lambda s: f"{s['time']}s"}


class MockTello:
    def __init__(self, state_port: int, drop_prob: float = 0.0, latency_s: float = 0.0, battery: int = 90,
                 seed: int = 0, state_hz: float = 10.0, mps_per_rc: float = 0.01, vel_tau_s: float = 0.4,
                 altitude_profile: list[tuple[float, float]] | None = None, tof_offset_cm: int = 10):
        self.cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cmd_sock.bind(("127.0.0.1", 0))
        self.cmd_sock.settimeout(0.05)
        self.addr = self.cmd_sock.getsockname()
        self.state_port = state_port
        self.drop_prob, self.latency_s, self.state_hz = drop_prob, latency_s, state_hz
        self.rng = random.Random(seed)
        self.s = {"mid": -1, "x": -100, "y": -100, "z": -100, "mpry": (0, 0, 0), "pitch": 0, "roll": 0, "yaw": 0,
                  "vgx": 0, "vgy": 0, "vgz": 0, "templ": 60, "temph": 63, "tof": 10, "h": 0, "bat": battery,
                  "baro": 120.0, "time": 0, "agx": 0.0, "agy": 0.0, "agz": -1000.0}
        self.sdk_mode = False
        self.flying = False
        self.rc_yaw = 0
        self.rc_fb = self.rc_lr = 0
        self.mps_per_rc, self.vel_tau_s = mps_per_rc, vel_tau_s
        self._pos = [0.0, 0.0]   # SIMULATED_FRAME_ONLY, metres
        self._vel = [0.0, 0.0]   # body-frame forward, right (m/s)
        self._pose_lock = threading.Lock()
        self._heading = 0.0  # float; the state packet reports it rounded like the real SDK
        self.received: list[str] = []
        self.last_cmd_t = time.monotonic()
        self.auto_landed = False
        self.altitude_profile = sorted((float(t), float(h)) for t, h in altitude_profile) if altitude_profile else None
        self.tof_offset_cm = int(tof_offset_cm)
        self.takeoff_t: float | None = None
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._serve, daemon=True), threading.Thread(target=self._state_loop, daemon=True)]

    def __enter__(self):
        for t in self._threads:
            t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1)
        self.cmd_sock.close()

    def _reply(self, cmd: str) -> str | None:
        v = verb(cmd)
        if cmd == "command":
            self.sdk_mode = True
            return "ok"
        if not self.sdk_mode:
            return None  # real Tello ignores everything before "command"
        if cmd in QUERY_REPLIES:
            return QUERY_REPLIES[cmd](self.s)
        if v == "rc":
            vals = [int(p) for p in cmd.split()[1:]]
            if len(vals) == 4:
                self.rc_lr, self.rc_fb, _, self.rc_yaw = vals
            else:
                self.rc_lr = self.rc_fb = self.rc_yaw = 0
            return None  # no reply for rc
        if v == "takeoff":
            if self.s["bat"] < 10:
                return "error Not enough battery"
            self.flying, self.s["h"] = True, 80
            self.takeoff_t = time.monotonic()
            self._apply_altitude(self.takeoff_t)
            return "ok"
        if v in ("land", "emergency"):
            self.flying, self.s["h"], self.rc_yaw = False, 0, 0
            self.rc_fb = self.rc_lr = 0
            return "ok"
        if v in ("streamon", "streamoff", "mon", "moff", "stop"):
            if v == "stop":
                self.rc_yaw = self.rc_fb = self.rc_lr = 0
            return "ok"
        return "error Unknown command"

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, src = self.cmd_sock.recvfrom(1518)
            except socket.timeout:
                continue
            except OSError:
                return
            cmd = data.decode("ascii", errors="replace").strip()
            self.received.append(cmd)
            self.last_cmd_t = time.monotonic()
            reply = self._reply(cmd)
            if reply is None or self.rng.random() < self.drop_prob:
                continue
            if self.latency_s:
                time.sleep(self.latency_s)
            self.cmd_sock.sendto(reply.encode(), src)

    def _step_pose(self, dt: float) -> None:
        a = min(1.0, dt / self.vel_tau_s) if self.vel_tau_s > 0 else 1.0
        cmd = (self.rc_fb * self.mps_per_rc, self.rc_lr * self.mps_per_rc)
        with self._pose_lock:
            self._vel = [v + (c - v) * a for v, c in zip(self._vel, cmd)]
            psi = math.radians(self._heading)
            fwd, right = self._vel
            self._pos[0] += (fwd * math.cos(psi) - right * math.sin(psi)) * dt
            self._pos[1] += (fwd * math.sin(psi) + right * math.cos(psi)) * dt

    def sim_pose(self) -> tuple[float, float, float]:
        """(x_m, y_m, heading_deg) in the SIMULATED_FRAME_ONLY frame."""
        with self._pose_lock:
            return self._pos[0], self._pos[1], self._heading

    def true_height_m(self, t: float) -> float | None:
        """Exact scripted height (metres) at monotonic time t, or None without a profile / before take-off."""
        if not self.altitude_profile or self.takeoff_t is None:
            return None
        x = t - self.takeoff_t
        ts = [p[0] for p in self.altitude_profile]
        hs = [p[1] for p in self.altitude_profile]
        if x <= ts[0]:
            return hs[0]
        for (t0, h0), (t1, h1) in zip(self.altitude_profile, self.altitude_profile[1:]):
            if x <= t1:
                return h0 + (h1 - h0) * (x - t0) / (t1 - t0) if t1 > t0 else h1
        return hs[-1]

    def _apply_altitude(self, t: float) -> None:
        h = self.true_height_m(t)
        if h is not None:
            self.s["h"] = int(round(100 * h))
            self.s["tof"] = self.s["h"] + self.tof_offset_cm

    def _state_loop(self) -> None:
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        period, t0 = 1.0 / self.state_hz, time.monotonic()
        while not self._stop.wait(period):
            if self.flying:
                self._heading = (self._heading + self.rc_yaw * 1.0 * period + 180) % 360 - 180  # ~1 deg/s per rc unit
                self.s["yaw"] = int(round(self._heading))
                self._step_pose(period)
                self._apply_altitude(time.monotonic())
                if time.monotonic() - self.last_cmd_t > 15.0:
                    self.flying, self.s["h"], self.auto_landed = False, 0, True
            self.s["time"] = int(time.monotonic() - t0)
            out.sendto(format_state(self.s).encode(), ("127.0.0.1", self.state_port))
        out.close()
