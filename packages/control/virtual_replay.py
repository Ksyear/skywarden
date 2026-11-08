"""Replay the yaw PID over a RECORDED tracking run for one operator-chosen track id.

Telemetry is VIRTUAL (always fresh, fixed battery) because a recorded clip has no drone. The camera did not turn in
response to these commands, so the output shows what the controller would have asked for, not a closed loop.
Commands are MOCK values for display only; nothing here opens a socket.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tracking.tracker import TargetSelector, TargetState, TrackState

from .yaw_controller import YawController

VIRTUAL_BATTERY_PCT = 85


@dataclass
class _RecordedTrack:
    id: int
    state: object
    center: np.ndarray


class _RecordedTracker:
    """Just enough of MultiTracker for TargetSelector.evaluate: the tracks recorded for one frame."""

    def __init__(self):
        self.t = None
        self._by_id: dict[int, _RecordedTrack] = {}

    def load(self, frame: dict) -> None:
        self.t = frame["t"]
        self._by_id = {}
        for tr in frame["tracks"]:
            x1, y1, x2, y2 = tr["bbox"]
            self._by_id[tr["id"]] = _RecordedTrack(tr["id"], TrackState(tr["state"]), np.array([(x1 + x2) / 2, (y1 + y2) / 2]))

    def get(self, track_id: int):
        return self._by_id.get(track_id)


def replay_control(frames: list[dict], image_width: int, target_id: int, select_k: int) -> dict:
    """Operator selects `target_id` at frame index `select_k`. Returns per-frame target state + MOCK yaw command."""
    if not frames:
        raise ValueError("no frames recorded")
    if not 0 <= select_k < len(frames):
        raise ValueError(f"select_k must be within 0..{len(frames) - 1}")
    if not any(tr["id"] == target_id for tr in frames[select_k]["tracks"]):
        raise ValueError(f"track {target_id} is not present at frame {select_k}; pick a track visible in that frame")
    rec, sel, ctl = _RecordedTracker(), TargetSelector(), YawController(image_width)
    out = []
    for f in frames:
        rec.load(f)
        if f["k"] == select_k:
            sel.select(target_id)
        tstate, track = sel.evaluate(rec)
        t = f["t"]
        cmd = ctl.command(t, tstate, track if tstate == TargetState.LOCKED else None, t, t, VIRTUAL_BATTERY_PCT)
        err = None if track is None else round((float(track.center[0]) - image_width / 2) / (image_width / 2), 3)
        out.append({"k": f["k"], "t": t, "target_state": tstate.value, "error_norm": err, "yaw": cmd.yaw,
                    "stop": list(cmd.stop_reasons)})
    after = out[select_k:]
    return {
        "target_id": target_id, "select_k": select_k, "image_width": image_width,
        "labels": {"command_source": "MOCK", "telemetry": f"VIRTUAL (fresh, battery {VIRTUAL_BATTERY_PCT}%)",
                   "loop": "OPEN_LOOP_REPLAY (the recorded camera did not turn)", "transmitted": False},
        "limits": {"max_yaw_rc": ctl.limits.max_yaw_rc, "axes_always_zero": ["lateral", "forward", "vertical"]},
        "summary": {
            "frames_after_select": len(after),
            "locked": sum(1 for r in after if r["target_state"] == "LOCKED"),
            "predicting": sum(1 for r in after if r["target_state"] == "PREDICTING"),
            "ambiguous": sum(1 for r in after if r["target_state"] == "AMBIGUOUS"),
            "lost_from_k": next((r["k"] for r in after if r["target_state"] == "LOST"), None),
            "nonzero_yaw": sum(1 for r in after if r["yaw"] != 0),
            "nonzero_yaw_while_not_locked": sum(1 for r in after if r["yaw"] != 0 and r["target_state"] != "LOCKED"),
        },
        "frames": out,
    }
