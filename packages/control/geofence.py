"""Geofence guard for MOCK commands: blocks translation that would leave a polygon, keeps yaw.

Works in a SIMULATED_FRAME_ONLY metric frame (MockTello.sim_pose). A Tello EDU has no absolute position, so on real
hardware this guard would need a pose source it does not have; it exists to test the stopping logic without flight.
Prediction uses both the commanded velocity and the current (lagging) velocity, so the margin covers coasting.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from .yaw_controller import DroneCommand


def _point_in_polygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _dist_to_edges(x: float, y: float, poly: list[tuple[float, float]]) -> float:
    best = math.inf
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        dx, dy = x2 - x1, y2 - y1
        t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)))
    return best


@dataclass(frozen=True)
class Geofence:
    polygon: tuple[tuple[float, float], ...]  # metres, SIMULATED_FRAME_ONLY, counter- or clockwise
    margin_m: float = 1.0

    def signed_distance(self, x: float, y: float) -> float:
        """> 0 inside (distance to the nearest edge), < 0 outside."""
        d = _dist_to_edges(x, y, list(self.polygon))
        return d if _point_in_polygon(x, y, list(self.polygon)) else -d


@dataclass
class GeofenceGuard:
    fence: Geofence
    mps_per_rc: float = 0.01
    horizon_s: float = 1.0

    def predict(self, pose: tuple[float, float, float], fwd: float, right: float) -> tuple[float, float]:
        x, y, heading = pose
        psi = math.radians(heading)
        return (x + (fwd * math.cos(psi) - right * math.sin(psi)) * self.horizon_s,
                y + (fwd * math.sin(psi) + right * math.cos(psi)) * self.horizon_s)

    def filter(self, cmd: DroneCommand, pose: tuple[float, float, float] | None,
               velocity_body: tuple[float, float] = (0.0, 0.0)) -> DroneCommand:
        if cmd.forward == 0 and cmd.lateral == 0:
            return cmd
        if pose is None:
            return replace(cmd, forward=0, lateral=0, stop_reasons=cmd.stop_reasons + ("NO_POSE_FOR_GEOFENCE",))
        # worst of commanded and current velocity, per axis, so a lagging aircraft is still covered
        vf = max((cmd.forward * self.mps_per_rc, velocity_body[0]), key=abs)
        vr = max((cmd.lateral * self.mps_per_rc, velocity_body[1]), key=abs)
        px, py = self.predict(pose, vf, vr)
        if self.fence.signed_distance(px, py) < self.fence.margin_m:
            # allow motion that clearly moves back inward (predicted point deeper inside than now)
            now = self.fence.signed_distance(pose[0], pose[1])
            if self.fence.signed_distance(px, py) <= now:
                return replace(cmd, forward=0, lateral=0, stop_reasons=cmd.stop_reasons + ("GEOFENCE",))
        return cmd
