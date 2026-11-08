"""Multi-person tracker: constant-velocity Kalman filter per track, Mahalanobis gating, Hungarian assignment.

Track states: TENTATIVE -> LOCKED (detected this update) <-> PREDICTING (coasting on the model, at most
`max_coast_s`) -> LOST (removed). There is no appearance model and no re-identification: a person who
reappears after LOST gets a new id.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
from scipy.optimize import linear_sum_assignment

CHI2_2DOF_99 = 9.21


class TrackState(str, Enum):
    TENTATIVE = "TENTATIVE"
    LOCKED = "LOCKED"
    PREDICTING = "PREDICTING"
    LOST = "LOST"


@dataclass(frozen=True)
class Detection:
    bbox_xyxy: tuple[float, float, float, float]  # pixels
    score: float
    source: str  # "REAL_MODEL" or "SYNTHETIC" — kept so results are never mixed up
    cls: str = "person"

    def valid(self) -> bool:
        x1, y1, x2, y2 = self.bbox_xyxy
        return all(map(math.isfinite, self.bbox_xyxy)) and x2 > x1 and y2 > y1 and 0.0 <= self.score <= 1.0

    @property
    def center(self) -> np.ndarray:
        x1, y1, x2, y2 = self.bbox_xyxy
        return np.array([(x1 + x2) / 2, (y1 + y2) / 2])

    @property
    def size(self) -> np.ndarray:
        x1, y1, x2, y2 = self.bbox_xyxy
        return np.array([x2 - x1, y2 - y1])


@dataclass
class TrackerConfig:
    accel_noise_px_s2: float = 150.0  # white-acceleration process noise (walking person at ~1-5 m)
    meas_noise_px: float = 6.0
    gate_chi2: float = CHI2_2DOF_99
    max_coast_s: float = 2.0
    confirm_hits: int = 3
    min_score: float = 0.3


@dataclass
class Track:
    id: int
    x: np.ndarray  # [cx, cy, vx, vy]
    P: np.ndarray
    size: np.ndarray
    state: TrackState = TrackState.TENTATIVE
    hits: int = 1
    last_hit_t: float = 0.0
    created_t: float = 0.0

    @property
    def center(self) -> np.ndarray:
        return self.x[:2]

    def bbox(self) -> tuple[float, float, float, float]:
        (cx, cy), (w, h) = self.x[:2], self.size
        return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)

    def position_sigma_px(self) -> float:
        return float(np.sqrt(np.trace(self.P[:2, :2]) / 2))


_H = np.array([[1.0, 0, 0, 0], [0, 1.0, 0, 0]])


@dataclass
class TrackerStats:
    updates: int = 0
    rejected_stale: int = 0
    rejected_invalid: int = 0
    created: int = 0
    lost: int = 0


class MultiTracker:
    def __init__(self, cfg: TrackerConfig = TrackerConfig()):
        self.cfg = cfg
        self.tracks: list[Track] = []
        self.t: float | None = None
        self._next_id = 1
        self.stats = TrackerStats()

    # -- Kalman pieces --
    def _predict(self, tr: Track, dt: float) -> None:
        F = np.eye(4)
        F[0, 2] = F[1, 3] = dt
        q = self.cfg.accel_noise_px_s2 ** 2
        G = np.array([[dt * dt / 2, 0], [0, dt * dt / 2], [dt, 0], [0, dt]])
        tr.x = F @ tr.x
        tr.P = F @ tr.P @ F.T + G @ G.T * q

    def _innovation(self, tr: Track, z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        S = _H @ tr.P @ _H.T + np.eye(2) * self.cfg.meas_noise_px ** 2
        return z - _H @ tr.x, S

    def _correct(self, tr: Track, det: Detection) -> None:
        y, S = self._innovation(tr, det.center)
        K = tr.P @ _H.T @ np.linalg.inv(S)
        tr.x = tr.x + K @ y
        tr.P = (np.eye(4) - K @ _H) @ tr.P
        tr.size = 0.7 * tr.size + 0.3 * det.size

    # -- public API --
    def update(self, t: float, detections: list[Detection]) -> list[Track]:
        """Advance to time t (seconds, monotonic) and fuse detections. Returns tracks that are not LOST."""
        if self.t is not None and t <= self.t:
            self.stats.rejected_stale += 1  # out-of-order / duplicate frame: never rewind the filter
            return self.active()
        dt = 0.0 if self.t is None else t - self.t
        self.t = t
        self.stats.updates += 1
        dets = [d for d in detections if d.valid() and d.score >= self.cfg.min_score]
        self.stats.rejected_invalid += sum(1 for d in detections if not d.valid())

        for tr in self.tracks:
            self._predict(tr, dt)

        # Gated Hungarian assignment on squared Mahalanobis distance.
        matched_t, matched_d = set(), set()
        if self.tracks and dets:
            big = 1e6
            cost = np.full((len(self.tracks), len(dets)), big)
            for i, tr in enumerate(self.tracks):
                for j, d in enumerate(dets):
                    y, S = self._innovation(tr, d.center)
                    m2 = float(y @ np.linalg.solve(S, y))
                    if m2 <= self.cfg.gate_chi2:
                        cost[i, j] = m2
            for i, j in zip(*linear_sum_assignment(cost)):
                if cost[i, j] < big:
                    self._correct(self.tracks[i], dets[j])
                    tr = self.tracks[i]
                    tr.hits += 1
                    tr.last_hit_t = t
                    tr.state = TrackState.LOCKED if tr.hits >= self.cfg.confirm_hits else TrackState.TENTATIVE
                    matched_t.add(i)
                    matched_d.add(j)

        for i, tr in enumerate(self.tracks):
            if i in matched_t:
                continue
            if tr.state == TrackState.TENTATIVE or t - tr.last_hit_t > self.cfg.max_coast_s:
                tr.state = TrackState.LOST
                self.stats.lost += 1
            else:
                tr.state = TrackState.PREDICTING

        self.tracks = [tr for tr in self.tracks if tr.state != TrackState.LOST]
        for j, d in enumerate(dets):
            if j not in matched_d:
                P = np.diag([self.cfg.meas_noise_px ** 2] * 2 + [200.0 ** 2] * 2)
                self.tracks.append(Track(self._next_id, np.r_[d.center, 0.0, 0.0], P, d.size.copy(),
                                         last_hit_t=t, created_t=t))
                self._next_id += 1
                self.stats.created += 1
        return self.active()

    def active(self) -> list[Track]:
        return list(self.tracks)

    def get(self, track_id: int) -> Track | None:
        return next((tr for tr in self.tracks if tr.id == track_id), None)


class TargetState(str, Enum):
    SEARCHING = "SEARCHING"  # no target chosen
    LOCKED = "LOCKED"
    PREDICTING = "PREDICTING"
    LOST = "LOST"  # the chosen id is gone; the operator must pick again (no automatic re-acquisition)


@dataclass
class TargetSelector:
    """The operator picks one track id. The selector never switches to another person on its own."""
    target_id: int | None = None
    history: list[tuple[float, str]] = field(default_factory=list)

    def select(self, track_id: int | None) -> None:
        self.target_id = track_id

    def evaluate(self, tracker: MultiTracker) -> tuple[TargetState, Track | None]:
        if self.target_id is None:
            st, tr = TargetState.SEARCHING, None
        else:
            tr = tracker.get(self.target_id)
            if tr is None:
                st = TargetState.LOST
            elif tr.state == TrackState.LOCKED:
                st = TargetState.LOCKED
            elif tr.state == TrackState.PREDICTING:
                st = TargetState.PREDICTING
            else:
                st = TargetState.SEARCHING  # still tentative
        if not self.history or self.history[-1][1] != st.value:
            self.history.append((tracker.t or 0.0, st.value))
        return st, tr
