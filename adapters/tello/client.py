"""Tello SDK 2.0 UDP client with a hard transmit gate.

Any destination that is not loopback counts as real hardware. Sending to it requires the environment to say
HARDWARE_ARMED=true AND DRY_RUN=false; otherwise every send raises HardwareDisarmed before touching the socket.
Loopback destinations (MockTello) are always allowed so the full command path can be tested without a drone.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from dataclasses import dataclass

from .protocol import ProtocolError, format_rc, parse_state, validate_command, verb

TELLO_ADDR = ("192.168.10.1", 8889)


class HardwareDisarmed(PermissionError):
    pass


class TelloTimeout(TimeoutError):
    pass


def hardware_enabled(env=os.environ) -> bool:
    return env.get("HARDWARE_ARMED", "false").lower() == "true" and env.get("DRY_RUN", "true").lower() == "false"


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(socket.gethostbyname(host)).is_loopback
    except (socket.gaierror, ValueError):
        return False


@dataclass
class StateSample:
    t: float  # monotonic receive time
    data: dict


class TelloClient:
    def __init__(self, addr: tuple[str, int] = TELLO_ADDR, state_port: int = 8890, sock: socket.socket | None = None,
                 env=os.environ):
        self.addr = addr
        self.simulated = is_loopback(addr[0])
        self.hardware_ok = hardware_enabled(env)
        self.sent: list[str] = []
        self._sock = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if sock is None:
            self._sock.bind(("127.0.0.1" if self.simulated else "0.0.0.0", 0))
        self._sock.settimeout(0.1)
        self._lock = threading.Lock()
        self._state_port = state_port
        self._state: StateSample | None = None
        self.state_errors = 0
        self._stop = threading.Event()
        self._state_thread: threading.Thread | None = None

    # -- transmit gate --
    def _authorize(self, cmd: str) -> str:
        cmd = validate_command(cmd)
        if not self.simulated and not self.hardware_ok:
            raise HardwareDisarmed(f"refusing to send {verb(cmd)!r} to {self.addr[0]}: HARDWARE_ARMED/DRY_RUN not enabled")
        return cmd

    def _send(self, cmd: str) -> None:
        cmd = self._authorize(cmd)  # checked again right before the only sendto call
        self._sock.sendto(cmd.encode("ascii"), self.addr)
        self.sent.append(cmd)

    def command(self, cmd: str, timeout: float = 2.0) -> str:
        """Send and wait for the text reply ('ok', 'error ...', or a query value)."""
        self._authorize(cmd)  # refuse before touching the socket at all
        with self._lock:
            self._drain()  # a late reply to an earlier, timed-out command must not answer this one
            self._send(cmd)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    data, src = self._sock.recvfrom(1518)
                except socket.timeout:
                    continue
                if src[0] == socket.gethostbyname(self.addr[0]):
                    return data.decode("ascii", errors="replace").strip()
            raise TelloTimeout(f"no reply to {verb(cmd)!r} within {timeout}s")

    def _drain(self) -> None:
        self._sock.setblocking(False)
        try:
            while True:
                self._sock.recvfrom(1518)
        except (BlockingIOError, socket.timeout, OSError):
            pass
        finally:
            self._sock.settimeout(0.1)

    def rc(self, lateral: int, forward: int, vertical: int, yaw: int) -> None:
        """rc has no reply in SDK 2.0; fire-and-forget through the same gate."""
        with self._lock:
            self._send(format_rc(lateral, forward, vertical, yaw))

    # -- state stream (UDP 8890) --
    def start_state_listener(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1" if self.simulated else "0.0.0.0", self._state_port))
        sock.settimeout(0.2)
        self._state_sock = sock

        def loop():
            while not self._stop.is_set():
                try:
                    data, _ = sock.recvfrom(2048)
                    self._state = StateSample(time.monotonic(), parse_state(data))
                except socket.timeout:
                    continue
                except (ProtocolError, UnicodeDecodeError):
                    self.state_errors += 1  # keep the last good sample; staleness is judged by its age
        self._state_thread = threading.Thread(target=loop, daemon=True, name="tello-state")
        self._state_thread.start()

    def latest_state(self) -> StateSample | None:
        return self._state

    def close(self) -> None:
        self._stop.set()
        if self._state_thread:
            self._state_thread.join(timeout=1)
            self._state_sock.close()
        self._sock.close()
