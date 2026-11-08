"""Multi-person tracker: constant-velocity Kalman filter per track, Mahalanobis gating, conservative association.

Track states: TENTATIVE -> LOCKED (trusted detection this update) <-> PREDICTING (no detection in the gate) /
AMBIGUOUS (detections in the gate are contested) -> LOST (removed). There is no appearance model and no
re-identification: a person who reappears after LOST gets a new id.

Association (motion only, so it cannot tell two people apart when their gates overlap):
  * Expiry first. A track is alive while t - last_hit_t <= max_coast_s, where last_hit_t is the time of its last
    TRUSTED detection; strictly greater expires. Expired tracks are removed before any detection is considered, so
    an id can never be revived by a late detection, whatever the frame spacing.
  * Gate = squared Mahalanobis distance within chi2 AND pixel distance within max_gate_box_frac x box height.
  * Confirmed tracks first (LOCKED / PREDICTING / AMBIGUOUS; a selectable target is always one of these). Tracks and
    detections are linked when the squared Mahalanobis distance is within the chi2 gate. In a connected component
    with exactly one confirmed track and one detection the detection is trusted (Kalman correction, hits,
    last_hit_t). Any component with more than one track or more than one detection is AMBIGUOUS: its tracks are not
    corrected and their last_hit_t is not refreshed (so they still expire on time), and its detections are withheld.
  * TENTATIVE tracks then take the detections no confirmed track claimed, with gated Hungarian assignment as
    before; whatever is left starts new tracks. Tentative tracks have wide gates, so letting them make confirmed
    tracks ambiguous would only churn ids without protecting the target.
This deliberately trades re-lock rate for safety: a target walking right next to another person stays AMBIGUOUS
(and the controller stops) until they separate, and a re-lock after a crossing is not guaranteed.
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
    AMBIGUOUS = "AMBIGUOUS"
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
    # Physical cap on the association radius: a detection further than this fraction of the track's box height from
    # the prediction is never linked, however large the coasting covariance has grown. Without it a track coasting
    # for 1.5 s (gate ~500 px) swallows unrelated people and makes everyone around it ambiguous.
    max_gate_box_frac: float = 0.5
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
    expired_before_association: int = 0
    ambiguous_track_updates: int = 0
    detections_withheld: int = 0  # contested detections that neither corrected a track nor started one


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

        # 1. Expire before association: no detection may revive a track older than the coast limit.
        for tr in self.tracks:
            if t - tr.last_hit_t > self.cfg.max_coast_s:
                tr.state = TrackState.LOST
                self.stats.lost += 1
                self.stats.expired_before_association += 1
        self.tracks = [tr for tr in self.tracks if tr.state != TrackState.LOST]

        def gated(tr: Track, d: Detection) -> float | None:
            y, S = self._innovation(tr, d.center)
            m2 = float(y @ np.linalg.solve(S, y))
            near = float(np.hypot(*y)) <= self.cfg.max_gate_box_frac * float(tr.size[1])
            return m2 if m2 <= self.cfg.gate_chi2 and near else None

        confirmed = [tr for tr in self.tracks if tr.state != TrackState.TENTATIVE]
        tentative = [tr for tr in self.tracks if tr.state == TrackState.TENTATIVE]

        # 2. Gate graph of confirmed tracks vs all detections; connected components via union-find
        #    (tracks 0..n-1, detections n..n+m-1).
        n, m = len(confirmed), len(dets)
        parent = list(range(n + m))

        def root(a: int) -> int:
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for i, tr in enumerate(confirmed):
            for j, d in enumerate(dets):
                if gated(tr, d) is not None:
                    parent[root(i)] = root(n + j)
        comp_tracks: dict[int, list[int]] = {}
        comp_dets: dict[int, list[int]] = {}
        for i in range(n):
            comp_tracks.setdefault(root(i), []).append(i)
        for j in range(m):
            comp_dets.setdefault(root(n + j), []).append(j)

        # 3. Update confirmed tracks per component.
        free = []
        for c in set(comp_tracks) | set(comp_dets):
            ti, dj = comp_tracks.get(c, []), comp_dets.get(c, [])
            if not ti:
                free += dj  # no confirmed track claims these detections
            elif len(ti) == 1 and len(dj) == 1:
                tr = confirmed[ti[0]]
                self._correct(tr, dets[dj[0]])
                tr.hits += 1
                tr.last_hit_t = t
                tr.state = TrackState.LOCKED if tr.hits >= self.cfg.confirm_hits else TrackState.TENTATIVE
            elif not dj:
                for i in ti:
                    confirmed[i].state = TrackState.PREDICTING
            else:  # contested: no correction, no trusted time, detections withheld
                self.stats.detections_withheld += len(dj)
                for i in ti:
                    confirmed[i].state = TrackState.AMBIGUOUS
                    self.stats.ambiguous_track_updates += 1

        # 4. Tentative tracks take unclaimed detections (gated Hungarian); the rest start new tracks.
        spawn, hit_now = set(free), set()
        if tentative and free:
            big = 1e6
            cost = np.full((len(tentative), len(free)), big)
            for i, tr in enumerate(tentative):
                for jj, j in enumerate(free):
                    m2 = gated(tr, dets[j])
                    if m2 is not None:
                        cost[i, jj] = m2
            for i, jj in zip(*linear_sum_assignment(cost)):
                if cost[i, jj] < big:
                    tr = tentative[i]
                    self._correct(tr, dets[free[jj]])
                    tr.hits += 1
                    tr.last_hit_t = t
                    tr.state = TrackState.LOCKED if tr.hits >= self.cfg.confirm_hits else TrackState.TENTATIVE
                    hit_now.add(tr.id)
                    spawn.discard(free[jj])
        for tr in tentative:
            if tr.id not in hit_now:
                tr.state = TrackState.LOST  # an unconfirmed track that misses a frame is dropped
        for tr in self.tracks:
            if tr.state == TrackState.LOST:
                self.stats.lost += 1

        self.tracks = [tr for tr in self.tracks if tr.state != TrackState.LOST]
        for j, d in enumerate(dets):
            if j in spawn:
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
    AMBIGUOUS = "AMBIGUOUS"  # detections near the target are contested: motion alone cannot tell who is who
    LOST = "LOST"  # the chosen id is gone; the operator must pick again (no automatic re-acquisition)


@dataclass
class TargetSelector:
    """The operator picks one track id. The selector never switches to another person on its own, and once the
    target is LOST it stays LOST until the operator calls select() again."""
    target_id: int | None = None
    history: list[tuple[float, str]] = field(default_factory=list)
    _lost: bool = False

    def select(self, track_id: int | None) -> None:
        self.target_id = track_id
        self._lost = False

    def evaluate(self, tracker: MultiTracker) -> tuple[TargetState, Track | None]:
        if self.target_id is None:
            st, tr = TargetState.SEARCHING, None
        else:
            tr = None if self._lost else tracker.get(self.target_id)
            if tr is None:
                st, self._lost = TargetState.LOST, True
            elif tr.state == TrackState.LOCKED:
                st = TargetState.LOCKED
            elif tr.state == TrackState.PREDICTING:
                st = TargetState.PREDICTING
            elif tr.state == TrackState.AMBIGUOUS:
                st = TargetState.AMBIGUOUS
            else:
                st = TargetState.SEARCHING  # still tentative
        if not self.history or self.history[-1][1] != st.value:
            self.history.append((tracker.t or 0.0, st.value))
        return st, tr
