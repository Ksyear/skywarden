"""Tello SDK 2.0 text protocol helpers (UDP 8889 commands, UDP 8890 state).

State packet example: "mid:-1;x:-100;y:-100;z:-100;mpry:0,0,0;pitch:0;roll:0;yaw:0;vgx:0;vgy:0;vgz:0;
templ:60;temph:63;tof:10;h:0;bat:87;baro:120.31;time:0;agx:-2.00;agy:1.00;agz:-999.00;\\r\\n"
"""
from __future__ import annotations

import re

# Commands that make the vehicle move or cut motors. Never sent to real hardware while unarmed.
FLIGHT_COMMANDS = {"takeoff", "land", "emergency", "motoron", "motoroff", "throwfly", "up", "down", "left", "right",
                   "forward", "back", "cw", "ccw", "flip", "go", "stop", "curve", "jump", "rc", "speed"}
INT_FIELDS = {"mid", "x", "y", "z", "pitch", "roll", "yaw", "vgx", "vgy", "vgz", "templ", "temph", "tof", "h", "bat", "time"}
FLOAT_FIELDS = {"baro", "agx", "agy", "agz"}
_CMD = re.compile(r"^[a-z]+\??( -?\d+(\.\d+)?)*$|^[a-z]+ [a-z0-9]+$")


class ProtocolError(ValueError):
    pass


def verb(cmd: str) -> str:
    return cmd.strip().split(" ", 1)[0].rstrip("?")


def validate_command(cmd: str) -> str:
    cmd = cmd.strip()
    if not _CMD.match(cmd) or len(cmd) > 64:
        raise ProtocolError(f"malformed command: {cmd!r}")
    return cmd


def format_rc(lateral: int, forward: int, vertical: int, yaw: int) -> str:
    vals = [int(max(-100, min(100, round(v)))) for v in (lateral, forward, vertical, yaw)]
    return "rc " + " ".join(map(str, vals))


def parse_state(packet: bytes | str) -> dict:
    text = packet.decode("ascii", errors="strict") if isinstance(packet, bytes) else packet
    out: dict = {}
    for item in text.strip().strip(";").split(";"):
        if not item:
            continue
        if ":" not in item:
            raise ProtocolError(f"bad state field {item!r}")
        k, v = item.split(":", 1)
        try:
            if k in INT_FIELDS:
                out[k] = int(v)
            elif k in FLOAT_FIELDS:
                out[k] = float(v)
            elif k == "mpry":
                out[k] = tuple(int(p) for p in v.split(","))
            else:
                out[k] = v
        except ValueError:
            raise ProtocolError(f"bad value for {k}: {v!r}")
    if "bat" not in out or "yaw" not in out:
        raise ProtocolError("state packet missing bat/yaw")
    # Mission Pad fields are only meaningful while a pad is detected (mid >= 1).
    out["mission_pad_valid"] = out.get("mid", -1) >= 1
    return out


def format_state(s: dict) -> str:
    keys = ["mid", "x", "y", "z", "mpry", "pitch", "roll", "yaw", "vgx", "vgy", "vgz", "templ", "temph", "tof", "h",
            "bat", "baro", "time", "agx", "agy", "agz"]
    parts = []
    for k in keys:
        v = s[k]
        parts.append(f"{k}:{','.join(map(str, v)) if isinstance(v, tuple) else (f'{v:.2f}' if isinstance(v, float) else v)}")
    return ";".join(parts) + ";\r\n"
