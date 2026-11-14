"""Live frame sources for one-time-axis runs: a video file played at its own rate (or as fast as possible) and a
local webcam, behind one `LiveSource` interface, plus a 1-slot latest-frame buffer with exact drop accounting.

Clocks (see docs/PLAN_LIVE_RUN_TIMELINE.md):
  media_t   source time of a frame. File: frame index / nominal fps. Webcam: capture wall time (monotonic, since open).
  media_now the source clock "now". File realtime: media_t_at_resume + (monotonic - wall_at_resume) while playing,
            frozen while paused. File max: media_t of the last frame handed out. Webcam: monotonic since open.
  wall_captured  monotonic time at which the frame left the decoder / camera (latency = processed - captured).

Nothing here sends anything anywhere; a webcam is only read, and only when the operator asks for `webcam:N`.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from mapping.frame_source import InputError, VideoFileSource

WEBCAM = re.compile(r"^webcam:(\d{1,2})$")
WEBCAM_OPEN_TIMEOUT_S = 5.0
MAX_SLEEP_S = 0.02  # a pacing sleep never exceeds this, so seek/pause/stop are seen quickly


class NoDevice(InputError):
    """No camera at that index (or it could not be opened within the timeout). Message starts with NO_DEVICE."""


@dataclass(frozen=True)
class SourceFrame:
    seq: int                # capture order within the run (0, 1, 2, ... across epochs)
    index: int              # frame index in the source (file) or capture counter (webcam)
    media_t: float          # source time, seconds
    wall_captured: float    # time.monotonic() when the frame was decoded / grabbed
    epoch: int              # bumped by every successful seek
    image: np.ndarray       # BGR uint8, native resolution


class LiveSource(Protocol):
    rate: str

    def meta(self) -> dict: ...
    def read(self) -> SourceFrame | None: ...       # None = no new frame yet (or paused / EOF; see .eof)
    def seek(self, media_t: float) -> bool: ...     # False when the source cannot seek (webcam)
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def media_now(self) -> float | None: ...        # None until the first frame (max) / clock start
    def close(self) -> None: ...


# ---------------------------------------------------------------------------------------------------- buffer
class LatestFrameBuffer:
    """One slot. put() overwrites an unread item (dropped += 1). take() returns the newest item or None.
    Invariant (exact, under the lock): produced == consumed + dropped + (1 if an item is waiting else 0).
    drain() and finalize() count whatever is waiting as dropped, so at the end produced == consumed + dropped."""

    def __init__(self):
        self._cv = threading.Condition()
        self._item = None
        self._full = False
        self.produced = self.consumed = self.dropped = 0
        self.last_taken_seq = -1

    def put(self, item) -> bool:
        with self._cv:
            overwrote = self._full
            self.produced += 1
            self.dropped += int(overwrote)
            self._item, self._full = item, True
            self._cv.notify_all()
            return overwrote

    def put_wait(self, item, stop: threading.Event, timeout: float = 0.05) -> bool:
        """Back-pressure put (rate=max): wait until the slot is free so nothing is dropped. False if stopped."""
        with self._cv:
            while self._full:
                if stop.is_set():
                    return False
                self._cv.wait(timeout)
            self.produced += 1
            self._item, self._full = item, True
            self._cv.notify_all()
            return True

    def take(self, timeout: float | None = None):
        with self._cv:
            if not self._full and timeout:
                self._cv.wait_for(lambda: self._full, timeout)
            if not self._full:
                return None
            item, self._item, self._full = self._item, None, False
            self.consumed += 1
            seq = getattr(item, "seq", None)
            if seq is not None:
                assert seq > self.last_taken_seq, "buffer returned a frame older than one already taken"
                self.last_taken_seq = seq
            self._cv.notify_all()
            return item

    def drain(self) -> int:
        with self._cv:
            n = int(self._full)
            self.dropped += n
            self._item, self._full = None, False
            self._cv.notify_all()
            return n

    finalize = drain

    def counts(self) -> dict:
        with self._cv:
            return {"produced": self.produced, "consumed": self.consumed, "dropped": self.dropped,
                    "waiting": int(self._full)}


# ---------------------------------------------------------------------------------------------------- file
class FileSource:
    """A video file. rate='realtime' hands out each frame when the media clock reaches its timestamp (monotonic
    deadline, sleeps <= 20 ms per call); rate='max' hands out the next frame immediately."""
    kind = "file"

    def __init__(self, path: str | Path, rate: str = "realtime", start_t: float = 0.0):
        if rate not in ("realtime", "max"):
            raise ValueError(f"rate must be 'realtime' or 'max', not {rate!r}")
        self.path = Path(path)
        probe = VideoFileSource(self.path)  # raises InputError with a user-facing message
        self.rate = rate
        self.fps = probe.nominal_fps() or 30.0
        self.frame_count = probe.frame_count()
        self._lock = threading.Lock()
        self.cap = cv2.VideoCapture(str(self.path))
        if not self.cap.isOpened():
            raise InputError(f"Cannot open video: {self.path}")
        self.size = [int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))]
        self.duration_s = self.frame_count / self.fps if self.frame_count else None
        self.epoch = 0
        self._seq = 0
        self._next_index = 0
        self._pending: SourceFrame | None = None
        self.eof = False
        self.paused = False
        self._clock_started = False
        self._media_at_resume = 0.0
        self._wall_at_resume = 0.0
        self._last_media_t: float | None = None
        if start_t:
            self.seek(start_t, bump_epoch=False)

    def meta(self) -> dict:
        return {"kind": self.kind, "path": str(self.path), "fps": self.fps, "size": self.size,
                "frame_count": self.frame_count, "duration_s": self.duration_s, "rate": self.rate,
                "media_clock": "frame_index / nominal_fps"}

    # -- clock
    def start_clock(self, media_t: float | None = None) -> None:
        with self._lock:
            self._media_at_resume = self._next_index / self.fps if media_t is None else media_t
            self._wall_at_resume = time.monotonic()
            self._clock_started = True

    def media_now(self) -> float | None:
        if self.rate == "max":
            return self._last_media_t
        if not self._clock_started:
            return None
        if self.paused:
            return self._media_at_resume
        return self._media_at_resume + (time.monotonic() - self._wall_at_resume)

    def pause(self) -> None:
        with self._lock:
            if not self.paused:
                if self.rate == "realtime" and self._clock_started:
                    self._media_at_resume = self.media_now()  # freeze the media clock here
                self.paused = True

    def resume(self) -> None:
        with self._lock:
            if self.paused:
                self.paused = False
                self._wall_at_resume = time.monotonic()

    # -- frames
    def _decode(self) -> SourceFrame | None:
        ok, img = self.cap.read()
        if not ok:
            self.eof = True
            return None
        i = self._next_index
        self._next_index += 1
        f = SourceFrame(self._seq, i, round(i / self.fps, 6), time.monotonic(), self.epoch, img)
        self._seq += 1
        return f

    def read(self) -> SourceFrame | None:
        if self.paused:
            time.sleep(MAX_SLEEP_S)
            return None
        with self._lock:
            if self._pending is None and not self.eof:
                self._pending = self._decode()
            f = self._pending
            if f is None:
                return None
            if self.rate == "realtime":
                if not self._clock_started:
                    self._media_at_resume, self._wall_at_resume, self._clock_started = f.media_t, time.monotonic(), True
                wait = f.media_t - self.media_now()
                if wait > 0:
                    f = None
            if f is not None:
                self._pending = None
                self._last_media_t = f.media_t
                # the frame leaves the "camera" now: captured wall time is when it became due, not when decoded early
                return SourceFrame(f.seq, f.index, f.media_t, time.monotonic(), f.epoch, f.image)
        time.sleep(min(MAX_SLEEP_S, max(0.0, wait)))
        return None

    def seek(self, media_t: float, bump_epoch: bool = True) -> bool:
        with self._lock:
            idx = int(round(max(0.0, media_t) * self.fps))
            if self.frame_count:
                idx = min(idx, max(0, self.frame_count - 1))
            if not self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx):
                return False
            self._next_index = idx
            self._pending = None
            self.eof = False
            self._last_media_t = None
            self._media_at_resume = idx / self.fps
            self._wall_at_resume = time.monotonic()
            if bump_epoch:
                self.epoch += 1
            return True

    def close(self) -> None:
        with self._lock:
            self.cap.release()


# ---------------------------------------------------------------------------------------------------- webcam
class WebcamSource:
    """Local camera N. Frames are paced by the camera; media_t = monotonic seconds since open. Cannot seek.
    Pause keeps grabbing (so the device buffer stays fresh) but hands nothing out."""
    kind = "webcam"
    rate = "realtime"

    def __init__(self, index: int, open_timeout_s: float = WEBCAM_OPEN_TIMEOUT_S):
        self.index = index
        self.epoch = 0
        self.eof = False
        self.paused = False
        self._seq = 0
        box: dict = {}
        # never let a background worker trigger the macOS camera-permission prompt: unauthorised -> NO_DEVICE
        os.environ.setdefault("OPENCV_AVFOUNDATION_SKIP_AUTH", "1")

        def _open():
            try:
                cap = cv2.VideoCapture(index)
                ok = cap.isOpened()
                img = cap.read()[1] if ok else None
                box.update(cap=cap, ok=ok and img is not None, img=img)
            except Exception as e:  # backend errors are reported as NO_DEVICE with the reason
                box.update(ok=False, err=str(e))

        th = threading.Thread(target=_open, daemon=True)
        th.start()
        th.join(open_timeout_s)
        if th.is_alive():
            raise NoDevice(f"NO_DEVICE: webcam:{index} did not open within {open_timeout_s:.0f} s")
        if not box.get("ok"):
            if box.get("cap") is not None:
                box["cap"].release()
            raise NoDevice(f"NO_DEVICE: no camera at webcam:{index} (or camera access not authorised)" + (f" ({box['err']})" if box.get("err") else ""))
        self.cap = box["cap"]
        h, w = box["img"].shape[:2]
        self.size = [w, h]
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or None
        self.t0 = time.monotonic()

    def meta(self) -> dict:
        return {"kind": self.kind, "index": self.index, "fps": self.fps, "size": self.size, "rate": self.rate,
                "media_clock": "monotonic seconds since the camera opened"}

    def start_clock(self, media_t: float | None = None) -> None:
        pass

    def media_now(self) -> float:
        return time.monotonic() - self.t0

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def read(self) -> SourceFrame | None:
        ok, img = self.cap.read()
        now = time.monotonic()
        if not ok or img is None:
            time.sleep(MAX_SLEEP_S)
            return None
        if self.paused:
            return None
        f = SourceFrame(self._seq, self._seq, round(now - self.t0, 6), now, self.epoch, img)
        self._seq += 1
        return f

    def seek(self, media_t: float) -> bool:
        return False

    def close(self) -> None:
        self.cap.release()


def open_live(spec: str, rate: str = "realtime") -> LiveSource:
    """'webcam:N' -> WebcamSource(N) (rate is always realtime); anything else is a video file path."""
    m = WEBCAM.match(spec)
    if m:
        return WebcamSource(int(m.group(1)))
    return FileSource(spec, rate)
