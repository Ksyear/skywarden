"""Multi-person tracker: constant-velocity Kalman filter per track, Mahalanobis gating, conservative association.

Track states: TENTATIVE -> LOCKED (trusted detection this update) <-> PREDICTING (no detection in the gate) /
AMBIGUOUS (detections in the gate are contested) -> LOST (removed). A person who reappears after LOST always gets
a new id: nothing (motion or appearance) ever revives an expired track.

Common to both association modes:
  * Expiry first. A track is alive while t - last_hit_t <= max_coast_s, where last_hit_t is the time of its last
    TRUSTED detection; strictly greater expires. Expired tracks are removed before any detection is considered, so
    an id can never be revived by a late detection, whatever the frame spacing.
  * Gate = squared Mahalanobis distance within chi2 AND pixel distance within max_gate_box_frac x box height.
  * Confirmed tracks first (LOCKED / PREDICTING / AMBIGUOUS; a selectable target is always one of these), linked to
    detections by the gate into connected components. A component with exactly one confirmed track and one
    detection is trusted (Kalman correction, hits, last_hit_t).
  * TENTATIVE tracks then take the detections no confirmed track claimed, with gated Hungarian assignment;
    whatever is left starts new tracks.

assoc="strict" (the original rule): any component with more than one track or more than one detection is
AMBIGUOUS — its tracks are not corrected, their last_hit_t is not refreshed (so they still expire on time), and its
detections are withheld. Motion alone cannot tell two people apart inside one gate, so this trades re-lock rate for
safety: a target next to another person stays AMBIGUOUS (and the controller stops) until they separate.

assoc="margin" resolves a contested component only when the evidence clearly separates the candidates:
  * cost(track, det) = Mahalanobis^2 + box-height consistency ((log h_det/h_track) / size_sigma)^2
    + direction consistency ocm_weight * angle/pi (observation-centric momentum, OC-SORT, Cao et al. 2023)
    [+ appearance reid_weight * cosine distance, only when appearance is enabled].
  * Gated Hungarian on the component; an assigned pair is trusted only if its cost + margin is <= every other
    candidate of that track AND of that detection (mutual margin). Undecided tracks stay AMBIGUOUS exactly as in
    strict mode; a track whose only candidate was decisively taken by another track is PREDICTING (unobserved).
  * Second stage for low-score detections (ByteTrack, Zhang et al. 2022): confirmed tracks that saw no high-score
    detection may be corrected by a detection with low_score <= score < min_score under the same gate and margin
    rule. Low-score detections never start a track.
Optional, independent of the mode:
  * oru: after a gap, re-run the filter from the last trusted state along a straight virtual path to the new
    observation (observation-centric re-update, OC-SORT), so the velocity error accumulated while coasting is undone.
  * gmc: camera motion compensation (tracking.gmc) applied to every track after prediction, when a frame is given.
  * reid: appearance embeddings from an injected `embedder(frame, boxes) -> (N, D)` (unit rows, NaN = unusable).
    Used ONLY to decide contested components and to veto the re-connection of a coasting track to a detection that
    looks like someone else. Embeddings live in memory inside Track objects and die with them; nothing is written.
  * scale-aware noise (docs/PLAN_SCALE_AWARE_NOISE.md): measurement noise max(meas_noise_px, meas_noise_frac x h)
    and process noise max(accel_noise_px_s2, accel_noise_frac x h), h = the track's box height, as SORT-family
    filters do. With a fixed 6 px a person 850 px tall gets a ~20 px gate and normal box jitter falls outside it.
With assoc="strict", oru, gmc and reid off and both noise fractions 0 (the defaults) the results are identical to the
original tracker.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

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
    # Scale-aware noise (docs/PLAN_SCALE_AWARE_NOISE.md); 0 = off (fixed pixels). The pixel values above stay minimums.
    meas_noise_frac: float = 0.0     # position measurement std as a fraction of box height
    accel_noise_frac: float = 0.0    # process (white-acceleration) std in box heights per s^2
    gate_chi2: float = CHI2_2DOF_99
    max_coast_s: float = 2.0
    # Physical cap on the association radius: a detection further than this fraction of the track's box height from
    # the prediction is never linked, however large the coasting covariance has grown. Without it a track coasting
    # for 1.5 s (gate ~500 px) swallows unrelated people and makes everyone around it ambiguous.
    max_gate_box_frac: float = 0.5
    confirm_hits: int = 3
    min_score: float = 0.3
    # --- occlusion handling (docs/PLAN_OCCLUSION_TRACKING.md); defaults reproduce the original tracker ---
    assoc: str = "strict"           # "strict" | "margin"
    margin: float = 4.0             # cost units (Mahalanobis^2 scale) by which a pair must beat every alternative
    size_sigma: float = 0.10        # std of log box-height ratio in the size term (0 = off); margin mode only
    ocm_weight: float = 2.0         # direction-consistency weight, cost at a full reversal (0 = off); margin mode only
    low_score: float | None = None  # second-stage threshold (e.g. 0.1); None = off; margin mode only
    low_dup_iou: float = 0.7        # a low-score box overlapping a trusted box this much is a duplicate, not a person
    low_min_iou: float = 0.5        # and it must overlap the track's predicted box this much (ByteTrack's IoU match)
    # After an AMBIGUOUS spell the coasting prediction covers both people, so any single detection in the gate is a
    # coin flip (a neighbour's box when the target's own box drops out for one frame). Until its next trusted hit
    # such a track only re-connects to a detection this close to the prediction (fraction of box height; ~half a
    # shoulder width). Margin mode only; 0 = off.
    contested_reconnect_frac: float = 0.15
    oru: bool = False
    gmc: bool = False
    reid: bool = False
    reid_weight: float = 12.5       # cost per unit cosine distance (= margin / median gap different-same on MOT17 calibration)
    reid_margin: float = 0.10       # appearance alone must not prefer another candidate by more than this
    reid_veto: float = 0.35         # re-connection after a gap is refused above this (same-person p99 on MOT17 calibration)
    reid_ema: float = 0.9           # track embedding = ema * old + (1 - ema) * new
    reid_refresh_s: float = 0.2     # clean, unambiguous hits refresh the track embedding at most this often
    reid_min_h_px: float = 48.0     # smaller boxes give no usable appearance
    reid_clean_iou: float = 0.3     # a box overlapping another detection more than this mixes two people

    def describe(self) -> str:
        """One-line summary for reports and the UI."""
        parts = [f"assoc {self.assoc}"]
        if self.assoc == "margin":
            parts.append(f"low-score {'on' if self.low_score is not None else 'off'}")
        parts += [f"ORU {'on' if self.oru else 'off'}", f"GMC {'on' if self.gmc else 'off'}", f"ReID {'on' if self.reid else 'off'}"]
        if self.meas_noise_frac > 0 or self.accel_noise_frac > 0:
            parts.append(f"noise scaled ({self.meas_noise_frac:g} h, {self.accel_noise_frac:g} h/s²)")
        return " · ".join(parts)


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
    # observation history (trusted hits only) for direction consistency and the re-update after a gap
    obs: deque = field(default_factory=lambda: deque(maxlen=8), repr=False)
    snap: tuple | None = field(default=None, repr=False)  # (x, P) right after the last trusted correction
    miss_ts: list = field(default_factory=list, repr=False)  # update times since the last trusted hit
    # appearance (memory only, never serialised; see module docstring)
    emb: np.ndarray | None = field(default=None, repr=False, compare=False)
    emb_t: float = -1e9
    contested: bool = False  # was AMBIGUOUS since its last trusted hit

    @property
    def center(self) -> np.ndarray:
        return self.x[:2]

    def bbox(self) -> tuple[float, float, float, float]:
        (cx, cy), (w, h) = self.x[:2], self.size
        return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)

    def position_sigma_px(self) -> float:
        return float(np.sqrt(np.trace(self.P[:2, :2]) / 2))


_H = np.array([[1.0, 0, 0, 0], [0, 1.0, 0, 0]])
Embedder = Callable[[object, list[tuple[float, float, float, float]]], np.ndarray]


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
    margin_resolved: int = 0      # contested track-detection pairs trusted because they beat every alternative
    low_score_hits: int = 0       # trusted second-stage (low-score) corrections
    oru_reupdates: int = 0
    gmc_applied: int = 0
    gmc_skipped: dict = field(default_factory=dict)
    reid_embeddings: int = 0      # count only; the vectors themselves are never stored outside Track objects
    reid_vetoes: int = 0
    reid_contested_uses: int = 0
    contested_reconnect_refused: int = 0  # margin mode: post-ambiguity re-connection too far from the prediction


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


class MultiTracker:
    def __init__(self, cfg: TrackerConfig | None = None, embedder: Embedder | None = None):
        self.cfg = cfg or TrackerConfig()
        self.tracks: list[Track] = []
        self.t: float | None = None
        self._next_id = 1
        self.stats = TrackerStats()
        self.embedder = embedder if self.cfg.reid else None
        self._gmc = None
        if self.cfg.gmc:
            from .gmc import GlobalMotion
            self._gmc = GlobalMotion()
        self._frame = None
        self._emb_cache: dict[int, np.ndarray | None] = {}
        self._all_dets: list[Detection] = []

    # -- Kalman pieces --
    def _meas_sigma(self, h: float) -> float:
        return max(self.cfg.meas_noise_px, self.cfg.meas_noise_frac * h)

    def _predict(self, tr: Track, dt: float) -> None:
        F = np.eye(4)
        F[0, 2] = F[1, 3] = dt
        q = max(self.cfg.accel_noise_px_s2, self.cfg.accel_noise_frac * float(tr.size[1])) ** 2
        G = np.array([[dt * dt / 2, 0], [0, dt * dt / 2], [dt, 0], [0, dt]])
        tr.x = F @ tr.x
        tr.P = F @ tr.P @ F.T + G @ G.T * q

    def _innovation(self, tr: Track, z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        S = _H @ tr.P @ _H.T + np.eye(2) * self._meas_sigma(float(tr.size[1])) ** 2
        return z - _H @ tr.x, S

    def _correct_z(self, tr: Track, z: np.ndarray) -> None:
        y, S = self._innovation(tr, z)
        K = tr.P @ _H.T @ np.linalg.inv(S)
        tr.x = tr.x + K @ y
        tr.P = (np.eye(4) - K @ _H) @ tr.P

    def _correct(self, tr: Track, det: Detection) -> None:
        self._correct_z(tr, det.center)
        tr.size = 0.7 * tr.size + 0.3 * det.size

    def _reupdate(self, tr: Track, det: Detection, t: float) -> None:
        """Observation-centric re-update: restart from the state at the last trusted hit and feed a straight virtual
        path (last observation -> new detection) at the update times that had no trusted observation."""
        x0, P0 = tr.snap
        t0, z0 = tr.obs[-1][0], np.asarray(tr.obs[-1][1:], float)
        z1 = det.center
        tr.x, tr.P = x0.copy(), P0.copy()
        prev = t0
        for ti in tr.miss_ts:
            self._predict(tr, ti - prev)
            self._correct_z(tr, z0 + (z1 - z0) * (ti - t0) / (t - t0))
            prev = ti
        self._predict(tr, t - prev)
        self.stats.oru_reupdates += 1

    def _trust(self, tr: Track, det: Detection, t: float) -> None:
        if self.cfg.oru and tr.miss_ts and tr.snap is not None and tr.obs:
            self._reupdate(tr, det, t)
        self._correct(tr, det)
        tr.hits += 1
        tr.last_hit_t = t
        tr.state = TrackState.LOCKED if tr.hits >= self.cfg.confirm_hits else TrackState.TENTATIVE
        self._note_hit(tr, t)

    def _note_hit(self, tr: Track, t: float) -> None:
        tr.contested = False
        tr.obs.append((t, float(tr.x[0]), float(tr.x[1])))
        tr.snap = (tr.x.copy(), tr.P.copy())
        tr.miss_ts = []

    def _warp(self, A: np.ndarray) -> None:
        M, T = A[:, :2], A[:, 2]
        s = float(np.sqrt(abs(np.linalg.det(M))))
        B = np.zeros((4, 4))
        B[:2, :2] = B[2:, 2:] = M
        for tr in self.tracks:
            tr.x = B @ tr.x + np.r_[T, 0.0, 0.0]
            tr.P = B @ tr.P @ B.T
            tr.size = tr.size * s
            if tr.snap is not None:
                tr.snap = (B @ tr.snap[0] + np.r_[T, 0.0, 0.0], B @ tr.snap[1] @ B.T)
            tr.obs = deque(((ot, *(M @ np.array([ox, oy]) + T)) for ot, ox, oy in tr.obs), maxlen=tr.obs.maxlen)

    # -- association helpers (margin mode) --
    def _gated(self, tr: Track, d: Detection) -> float | None:
        y, S = self._innovation(tr, d.center)
        m2 = float(y @ np.linalg.solve(S, y))
        near = float(np.hypot(*y)) <= self.cfg.max_gate_box_frac * float(tr.size[1])
        return m2 if m2 <= self.cfg.gate_chi2 and near else None

    def _motion_cost(self, tr: Track, d: Detection, m2: float) -> float:
        c = m2
        if self.cfg.size_sigma > 0:
            c += (math.log(max(d.size[1], 1.0) / max(tr.size[1], 1.0)) / self.cfg.size_sigma) ** 2
        if self.cfg.ocm_weight > 0 and len(tr.obs) >= 2:
            h = float(tr.size[1])
            last = np.array(tr.obs[-1][1:])
            older = next((np.array(o[1:]) for o in list(tr.obs)[-4::-1] if np.hypot(*(last - np.array(o[1:]))) > 0.05 * h), None)
            step = d.center - last
            if older is not None and np.hypot(*step) > 0.05 * h:
                u, v = last - older, step
                cosang = float(np.clip(u @ v / (np.linalg.norm(u) * np.linalg.norm(v)), -1.0, 1.0))
                c += self.cfg.ocm_weight * math.acos(cosang) / math.pi
        return c

    def _embeddings(self, dets: list[Detection], idx: list[int]) -> None:
        """Compute (once per update) appearance for the requested detections. Memory only."""
        need = [j for j in idx if j not in self._emb_cache]
        if not need or self.embedder is None or self._frame is None:
            for j in need:
                self._emb_cache[j] = None
            return
        ok = [j for j in need if dets[j].size[1] >= self.cfg.reid_min_h_px]
        for j in need:
            self._emb_cache[j] = None
        if ok:
            E = np.asarray(self.embedder(self._frame, [dets[j].bbox_xyxy for j in ok]), dtype=np.float32)
            for j, e in zip(ok, E):
                if np.all(np.isfinite(e)):
                    self._emb_cache[j] = e
                    self.stats.reid_embeddings += 1

    @staticmethod
    def _cosd(a: np.ndarray, b: np.ndarray) -> float:
        return float(1.0 - np.clip(a @ b, -1.0, 1.0))

    def _resolve(self, trs: list[Track], dts: list[Detection], dj: list[int], cost: np.ndarray, app: np.ndarray | None
                 ) -> list[tuple[int, int]]:
        """Mutual-margin decision inside one contested component. Returns accepted (row, col) pairs."""
        big = 1e9
        c = np.where(np.isfinite(cost), cost, big)
        out = []
        m = self.cfg.margin
        for i, j in zip(*linear_sum_assignment(c)):
            if c[i, j] >= big:
                continue
            row = np.delete(cost[i], j)
            col = np.delete(cost[:, j], i)
            if not (np.all(cost[i, j] + m <= row) and np.all(cost[i, j] + m <= col)):
                continue
            if app is not None and np.isfinite(app[i, j]):
                arow, acol = np.delete(app[i], j), np.delete(app[:, j], i)
                rm = self.cfg.reid_margin
                if np.any(np.isfinite(arow) & (arow + rm < app[i, j])) or np.any(np.isfinite(acol) & (acol + rm < app[i, j])):
                    continue  # appearance clearly disagrees with the motion decision: stay ambiguous
            out.append((int(i), int(j)))
        return out

    # -- public API --
    def update(self, t: float, detections: list[Detection], frame=None) -> list[Track]:
        """Advance to time t (seconds, monotonic) and fuse detections. `frame` (optional, the image the detections
        came from) is used only by camera motion compensation and appearance. Returns tracks that are not LOST."""
        if self.t is not None and t <= self.t:
            self.stats.rejected_stale += 1  # out-of-order / duplicate frame: never rewind the filter
            return self.active()
        dt = 0.0 if self.t is None else t - self.t
        self.t = t
        self.stats.updates += 1
        valid = [d for d in detections if d.valid()]
        dets = [d for d in valid if d.score >= self.cfg.min_score]
        self.stats.rejected_invalid += sum(1 for d in detections if not d.valid())
        self._frame, self._emb_cache = frame, {}

        for tr in self.tracks:
            self._predict(tr, dt)
        if self._gmc is not None and frame is not None and hasattr(frame, "shape"):
            A, why = self._gmc.estimate(frame, [d.bbox_xyxy for d in valid])
            if A is not None:
                self._warp(A)
                self.stats.gmc_applied += 1
            else:
                self.stats.gmc_skipped[why] = self.stats.gmc_skipped.get(why, 0) + 1

        # 1. Expire before association: no detection may revive a track older than the coast limit.
        for tr in self.tracks:
            if t - tr.last_hit_t > self.cfg.max_coast_s:
                tr.state = TrackState.LOST
                self.stats.lost += 1
                self.stats.expired_before_association += 1
        self.tracks = [tr for tr in self.tracks if tr.state != TrackState.LOST]

        gated = self._gated
        confirmed = [tr for tr in self.tracks if tr.state != TrackState.TENTATIVE]
        tentative = [tr for tr in self.tracks if tr.state == TrackState.TENTATIVE]
        prev_state = {tr.id: tr.state for tr in confirmed}

        if self.cfg.assoc == "margin":
            free = self._associate_margin(t, confirmed, dets, valid)
        else:
            free = self._associate_strict(t, confirmed, dets)

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
                    self._trust(tr, dets[free[jj]], t)
                    hit_now.add(tr.id)
                    spawn.discard(free[jj])
        for tr in tentative:
            if tr.id not in hit_now:
                tr.state = TrackState.LOST  # an unconfirmed track that misses a frame is dropped
        for tr in self.tracks:
            if tr.state == TrackState.LOST:
                self.stats.lost += 1

        self.tracks = [tr for tr in self.tracks if tr.state != TrackState.LOST]
        for tr in self.tracks:
            if tr.last_hit_t != t:
                tr.miss_ts.append(t)
        if self.cfg.reid and self.embedder is not None and self.cfg.assoc == "margin":
            self._refresh_embeddings(t, dets, prev_state)
        for j, d in enumerate(dets):
            if j in spawn:
                P = np.diag([self._meas_sigma(float(d.size[1])) ** 2] * 2 + [200.0 ** 2] * 2)
                tr = Track(self._next_id, np.r_[d.center, 0.0, 0.0], P, d.size.copy(), last_hit_t=t, created_t=t)
                self._note_hit(tr, t)
                self.tracks.append(tr)
                self._next_id += 1
                self.stats.created += 1
        self._frame, self._emb_cache = None, {}
        return self.active()

    def _associate_strict(self, t: float, confirmed: list[Track], dets: list[Detection]) -> list[int]:
        gated = self._gated
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
                self._trust(confirmed[ti[0]], dets[dj[0]], t)
            elif not dj:
                for i in ti:
                    confirmed[i].state = TrackState.PREDICTING
            else:  # contested: no correction, no trusted time, detections withheld
                self.stats.detections_withheld += len(dj)
                for i in ti:
                    confirmed[i].state = TrackState.AMBIGUOUS
                    confirmed[i].contested = True
                    self.stats.ambiguous_track_updates += 1
        return free

    def _components(self, trs: list[Track], dts: list[Detection], only: set | None = None) -> tuple[list[tuple[list[int], list[int]]], dict]:
        n, m = len(trs), len(dts)
        parent = list(range(n + m))

        def root(a: int) -> int:
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        m2s = {}
        for i, tr in enumerate(trs):
            for j, d in enumerate(dts):
                g = self._gated(tr, d) if only is None or (i, j) in only else None
                if g is not None:
                    m2s[i, j] = g
                    parent[root(i)] = root(n + j)
        ct: dict[int, list[int]] = {}
        cd: dict[int, list[int]] = {}
        for i in range(n):
            ct.setdefault(root(i), []).append(i)
        for j in range(m):
            cd.setdefault(root(n + j), []).append(j)
        return [(ct.get(c, []), cd.get(c, [])) for c in sorted(set(ct) | set(cd))], m2s

    def _guard(self, tr: Track, d: Detection) -> bool:
        """True if a trusted hit is allowed for a track coming out of an AMBIGUOUS spell (margin mode)."""
        f = self.cfg.contested_reconnect_frac
        if not (f > 0 and tr.contested):
            return True
        if float(np.hypot(*(d.center - tr.center))) <= f * float(tr.size[1]):
            return True
        self.stats.contested_reconnect_refused += 1
        return False

    def _stage(self, t: float, trs: list[Track], dts: list[Detection], det_index: list[int], low: bool
               ) -> tuple[list[int], set[int], set[int]]:
        """One association stage in margin mode. Returns (free detection indices into dts, ids of tracks left
        undecided (AMBIGUOUS), ids of tracks trusted)."""
        comps, m2s = self._components(trs, dts)
        if low:  # second stage: also require box overlap with the prediction, as ByteTrack's low-score match does
            keep = {k for k in m2s if _iou(trs[k[0]].bbox(), dts[k[1]].bbox_xyxy) >= self.cfg.low_min_iou}
            if len(keep) != len(m2s):
                comps, m2s = self._components(trs, dts, only=keep)
        free, undecided, trusted = [], set(), set()
        use_app = self.cfg.reid and self.embedder is not None
        if use_app:  # appearance is needed only for contested components and for re-connections after a gap
            need = set()
            for ti, dj in comps:
                if len(ti) > 1 or len(dj) > 1:
                    need |= {det_index[j] for j in dj}
                elif len(ti) == 1 and len(dj) == 1 and trs[ti[0]].miss_ts and trs[ti[0]].emb is not None:
                    need.add(det_index[dj[0]])
            if need:
                self._embeddings(self._all_dets, sorted(need))
        for ti, dj in comps:
            if not ti:
                free += dj
                continue
            if not dj:
                for i in ti:
                    if trs[i].id not in trusted:
                        trs[i].state = TrackState.PREDICTING
                continue
            if len(ti) == 1 and len(dj) == 1:
                tr, j = trs[ti[0]], dj[0]
                e = self._emb_cache.get(det_index[j]) if use_app else None
                if use_app and tr.miss_ts and tr.emb is not None and e is not None and self._cosd(tr.emb, e) > self.cfg.reid_veto:
                    self.stats.reid_vetoes += 1  # coasting track re-connecting to someone who looks different
                    self.stats.detections_withheld += 1
                    self.stats.ambiguous_track_updates += 1
                    tr.state = TrackState.AMBIGUOUS
                    tr.contested = True
                    undecided.add(tr.id)
                    continue
                if not self._guard(tr, dts[j]):
                    self.stats.detections_withheld += 1
                    self.stats.ambiguous_track_updates += 1
                    tr.state = TrackState.AMBIGUOUS
                    tr.contested = True
                    undecided.add(tr.id)
                    continue
                self._trust(tr, dts[j], t)
                trusted.add(tr.id)
                self.stats.low_score_hits += low
                continue
            cost = np.full((len(ti), len(dj)), np.inf)
            app = np.full((len(ti), len(dj)), np.inf) if use_app else None
            for a, i in enumerate(ti):
                for b, j in enumerate(dj):
                    if (i, j) in m2s:
                        cost[a, b] = self._motion_cost(trs[i], dts[j], m2s[i, j])
                        if use_app:
                            e = self._emb_cache.get(det_index[j])
                            if e is not None and trs[i].emb is not None:
                                app[a, b] = self._cosd(trs[i].emb, e)
                                cost[a, b] += self.cfg.reid_weight * app[a, b]
            if use_app and np.isfinite(app).any():
                self.stats.reid_contested_uses += 1
            acc = [(a_, b_) for a_, b_ in self._resolve([trs[i] for i in ti], [dts[j] for j in dj], dj, cost, app)
                   if self._guard(trs[ti[a_]], dts[dj[b_]])]
            taken_t, taken_d = {a for a, _ in acc}, {b for _, b in acc}
            for a, b in acc:
                self._trust(trs[ti[a]], dts[dj[b]], t)
                trusted.add(trs[ti[a]].id)
                self.stats.margin_resolved += 1
                self.stats.low_score_hits += low
            # tracks left: AMBIGUOUS if they still have an untaken candidate, otherwise simply unobserved
            for a, i in enumerate(ti):
                if a in taken_t:
                    continue
                if any(np.isfinite(cost[a, b]) for b in range(len(dj)) if b not in taken_d):
                    trs[i].state = TrackState.AMBIGUOUS
                    trs[i].contested = True
                    undecided.add(trs[i].id)
                    self.stats.ambiguous_track_updates += 1
                else:
                    trs[i].state = TrackState.PREDICTING
            # detections left: free only if no undecided track could still claim them
            for b, j in enumerate(dj):
                if b in taken_d:
                    continue
                if any(np.isfinite(cost[a, b]) for a in range(len(ti)) if a not in taken_t):
                    self.stats.detections_withheld += 1
                else:
                    free.append(j)
        return free, undecided, trusted

    def _associate_margin(self, t: float, confirmed: list[Track], dets: list[Detection], valid: list[Detection]) -> list[int]:
        self._all_dets = list(dets)
        free, undecided, trusted = self._stage(t, confirmed, dets, list(range(len(dets))), low=False)
        lo = self.cfg.low_score
        if lo is not None:
            rest = [tr for tr in confirmed if tr.id not in trusted and tr.id not in undecided]
            used = [dets[j].bbox_xyxy for j in range(len(dets)) if j not in free]
            lows = [d for d in valid if lo <= d.score < self.cfg.min_score
                    and all(_iou(d.bbox_xyxy, b) < self.cfg.low_dup_iou for b in used)]
            if rest and lows:
                base = len(self._all_dets)
                self._all_dets += lows
                self._stage(t, rest, lows, list(range(base, base + len(lows))), low=True)
                # low-score detections never start tracks; a track that saw nothing stays PREDICTING
        return free

    def _refresh_embeddings(self, t: float, dets: list[Detection], prev_state: dict) -> None:
        """Update track embeddings from clean, unambiguous, high-score trusted hits (at most every reid_refresh_s)."""
        cand = []
        for tr in self.tracks:
            if tr.last_hit_t != t or tr.state != TrackState.LOCKED or t - tr.emb_t < self.cfg.reid_refresh_s:
                continue
            j = min(range(len(dets)), key=lambda k: float(np.hypot(*(dets[k].center - tr.center))), default=None)
            if j is None:
                continue
            d = dets[j]
            if any(_iou(d.bbox_xyxy, o.bbox_xyxy) > self.cfg.reid_clean_iou for k, o in enumerate(dets) if k != j):
                continue
            if float(np.hypot(*(d.center - tr.center))) > 0.25 * float(tr.size[1]):
                continue
            cand.append((tr, j))
        if not cand:
            return
        self._embeddings(self._all_dets, [j for _, j in cand])
        a = self.cfg.reid_ema
        for tr, j in cand:
            e = self._emb_cache.get(j)
            if e is None:
                continue
            tr.emb = e.copy() if tr.emb is None else a * tr.emb + (1 - a) * e
            tr.emb /= max(float(np.linalg.norm(tr.emb)), 1e-9)
            tr.emb_t = t

    def active(self) -> list[Track]:
        return list(self.tracks)

    def get(self, track_id: int) -> Track | None:
        return next((tr for tr in self.tracks if tr.id == track_id), None)


class TargetState(str, Enum):
    SEARCHING = "SEARCHING"  # no target chosen
    LOCKED = "LOCKED"
    PREDICTING = "PREDICTING"
    AMBIGUOUS = "AMBIGUOUS"  # detections near the target are contested: the evidence cannot tell who is who
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
