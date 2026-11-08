"""Yaw-only visual servo: keep the selected person horizontally centred. Produces MOCK commands only.

Deliberately limited: lateral/forward/vertical are always 0. Bounding-box size is NOT used as a distance
estimate, so this controller can never approach or chase a person.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from tracking.tracker import TargetState, Track


@dataclass
class PID:
    kp: float
    ki: float
    kd: float
    out_limit: float
    i_limit: float
    _i: float = 0.0
    _prev_err: float | None = None

    def reset(self) -> None:
        self._i, self._prev_err = 0.0, None

    def step(self, error: float, dt: float) -> float:
        """error = desired-direction signal (here: target offset from centre). The setpoint is constant, so the
        derivative of the error equals the derivative of the measurement and causes no setpoint kick."""
        if not (dt > 0 and math.isfinite(dt) and math.isfinite(error)):
            raise ValueError(f"invalid PID input dt={dt} error={error}")
        d = 0.0 if self._prev_err is None else (error - self._prev_err) / dt  # damps the approach to centre
        self._prev_err = error
        unsat = self.kp * error + self.ki * self._i + self.kd * d
        # Anti-windup: integrate only when not pushing further into saturation.
        if abs(unsat) < self.out_limit or (unsat > 0) != (error > 0):
            self._i = max(-self.i_limit, min(self.i_limit, self._i + error * dt))
        out = self.kp * error + self.ki * self._i + self.kd * d
        return max(-self.out_limit, min(self.out_limit, out))


@dataclass(frozen=True)
class DroneCommand:
    t: float
    yaw: int  # Tello rc d: -100..100
    lateral: int = 0
    forward: int = 0
    vertical: int = 0
    source: str = "MOCK"
    stop_reasons: tuple[str, ...] = ()

    @property
    def is_stop(self) -> bool:
        return self.yaw == self.lateral == self.forward == self.vertical == 0


@dataclass
class SafetyLimits:
    max_frame_age_s: float = 0.3
    max_telemetry_age_s: float = 0.5
    min_battery_pct: int = 20
    max_yaw_rc: int = 40
    max_yaw_slew_per_s: float = 120.0  # rc units per second


@dataclass
class YawController:
    image_width: int
    pid: PID = field(default_factory=lambda: PID(kp=60.0, ki=8.0, kd=6.0, out_limit=40.0, i_limit=2.0))
    limits: SafetyLimits = field(default_factory=SafetyLimits)
    _last_t: float | None = None
    _last_yaw: float = 0.0

    def _stop(self, t: float, reasons: list[str]) -> DroneCommand:
        self.pid.reset()
        self._last_yaw = 0.0
        self._last_t = t
        return DroneCommand(t, 0, stop_reasons=tuple(reasons))

    def command(self, now: float, target_state: TargetState, track: Track | None, frame_t: float,
                telemetry_t: float | None, battery_pct: int | None) -> DroneCommand:
        reasons = []
        if now - frame_t > self.limits.max_frame_age_s:
            reasons.append(f"STALE_FRAME({now - frame_t:.2f}s)")
        if telemetry_t is None or now - telemetry_t > self.limits.max_telemetry_age_s:
            reasons.append("STALE_TELEMETRY" if telemetry_t is not None else "NO_TELEMETRY")
        if battery_pct is None:
            reasons.append("NO_BATTERY_READING")
        elif battery_pct < self.limits.min_battery_pct:
            reasons.append(f"LOW_BATTERY({battery_pct}%)")
        if target_state != TargetState.LOCKED or track is None:
            reasons.append(f"TARGET_{target_state.value}")
        if reasons:
            return self._stop(now, reasons)

        dt = now - self._last_t if self._last_t is not None and now > self._last_t else 1 / 30
        half = self.image_width / 2
        error = (float(track.center[0]) - half) / half  # +1: target at right edge
        raw = self.pid.step(error, dt)
        max_step = self.limits.max_yaw_slew_per_s * dt
        yaw = max(self._last_yaw - max_step, min(self._last_yaw + max_step, raw))
        yaw = max(-self.limits.max_yaw_rc, min(self.limits.max_yaw_rc, yaw))
        self._last_yaw, self._last_t = yaw, now
        return DroneCommand(now, int(round(yaw)))
