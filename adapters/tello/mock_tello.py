"""Loopback MockTello: answers SDK 2.0 commands and streams simulated state. Never a real vehicle.

Fault injection (seeded): reply drop probability, reply latency, battery drain. Flight is a kinematic toy:
takeoff sets h=80 cm, rc yaw integrates heading, a 15 s command silence while flying triggers auto-land
(mirrors the documented SDK safety behaviour).
"""
from __future__ import annotations

import random
import socket
import threading
import time

from .protocol import format_state, verb

QUERY_REPLIES = {"battery?": lambda s: str(s["bat"]), "sdk?": lambda s: "30", "sn?": lambda s: "MOCK0000000000",
                 "height?": lambda s: f"{s['h']}dm", "time?": lambda s: f"{s['time']}s"}


class MockTello:
    def __init__(self, state_port: int, drop_prob: float = 0.0, latency_s: float = 0.0, battery: int = 90,
                 seed: int = 0, state_hz: float = 10.0):
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
        self._heading = 0.0  # float; the state packet reports it rounded like the real SDK
        self.received: list[str] = []
        self.last_cmd_t = time.monotonic()
        self.auto_landed = False
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
            self.rc_yaw = vals[3] if len(vals) == 4 else 0
            return None  # no reply for rc
        if v == "takeoff":
            if self.s["bat"] < 10:
                return "error Not enough battery"
            self.flying, self.s["h"] = True, 80
            return "ok"
        if v in ("land", "emergency"):
            self.flying, self.s["h"], self.rc_yaw = False, 0, 0
            return "ok"
        if v in ("streamon", "streamoff", "mon", "moff", "stop"):
            if v == "stop":
                self.rc_yaw = 0
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

    def _state_loop(self) -> None:
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        period, t0 = 1.0 / self.state_hz, time.monotonic()
        while not self._stop.wait(period):
            if self.flying:
                self._heading = (self._heading + self.rc_yaw * 1.0 * period + 180) % 360 - 180  # ~1 deg/s per rc unit
                self.s["yaw"] = int(round(self._heading))
                if time.monotonic() - self.last_cmd_t > 15.0:
                    self.flying, self.s["h"], self.auto_landed = False, 0, True
            self.s["time"] = int(time.monotonic() - t0)
            out.sendto(format_state(self.s).encode(), ("127.0.0.1", self.state_port))
        out.close()
