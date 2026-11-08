"""Input adapters: a video file or a folder of images, both read as an ordered stream of frames.

The rest of the pipeline only sees `SourceFrame`, so a webcam or Tello stream can be added later
as one more adapter without touching frame selection or SfM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

import cv2
import numpy as np

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


class InputError(ValueError):
    """The input cannot be read. The message says what the user can fix."""


@dataclass(frozen=True)
class SourceFrame:
    index: int  # position in the source stream
    timestamp_s: float | None  # source time; None when the source has no clock
    image: np.ndarray  # BGR uint8
    source_name: str  # file name (images) or "<video>#<index>"


class FrameSource(Protocol):
    kind: str
    path: Path

    def nominal_fps(self) -> float | None: ...
    def frame_count(self) -> int: ...
    def frames(self, step: int = 1) -> Iterator[SourceFrame]: ...


_TIMESTAMP_NAME = re.compile(r"^(\d+\.\d+)$")


class ImageFolderSource:
    """Sorted images. File stems like `1305031452.791720` (TUM style) are used as timestamps."""

    kind = "image_folder"

    def __init__(self, path: Path):
        self.path = path
        self._files = sorted(p for p in path.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES and not p.name.startswith("._"))
        if not self._files:
            raise InputError(f"No .png/.jpg images in {path}")

    def nominal_fps(self) -> float | None:
        stamps = [self._stamp(p) for p in self._files[:50]]
        if None in stamps or len(stamps) < 2:
            return None
        span = stamps[-1] - stamps[0]
        return (len(stamps) - 1) / span if span > 0 else None

    def frame_count(self) -> int:
        return len(self._files)

    @staticmethod
    def _stamp(p: Path) -> float | None:
        m = _TIMESTAMP_NAME.match(p.stem)
        return float(m.group(1)) if m else None

    def frames(self, step: int = 1) -> Iterator[SourceFrame]:
        for i in range(0, len(self._files), step):
            p = self._files[i]
            img = cv2.imread(str(p), cv2.IMREAD_COLOR)
            if img is None:
                raise InputError(f"Unreadable image: {p}")
            yield SourceFrame(i, self._stamp(p), img, p.name)


class VideoFileSource:
    kind = "video_file"

    def __init__(self, path: Path):
        self.path = path
        cap = cv2.VideoCapture(str(path))
        try:
            if not cap.isOpened():
                raise InputError(f"Cannot open video (unsupported codec or corrupt file): {path}")
            self._fps = cap.get(cv2.CAP_PROP_FPS) or None
            self._count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            ok, _ = cap.read()
            if not ok:
                raise InputError(f"Video has no decodable frames: {path}")
        finally:
            cap.release()

    def nominal_fps(self) -> float | None:
        return self._fps

    def frame_count(self) -> int:
        return self._count

    def frames(self, step: int = 1) -> Iterator[SourceFrame]:
        cap = cv2.VideoCapture(str(self.path))
        try:
            i = 0
            while True:
                if i % step:  # grab() skips decoding of frames we will not keep
                    if not cap.grab():
                        return
                    i += 1
                    continue
                ok, img = cap.read()
                if not ok:
                    return
                ts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
                yield SourceFrame(i, ts, img, f"{self.path.name}#{i}")
                i += 1
        finally:
            cap.release()


def open_source(path: str | Path) -> FrameSource:
    p = Path(path).expanduser()
    if not p.exists():
        raise InputError(f"Input does not exist: {p}")
    if p.is_dir():
        rgb = p / "rgb"  # TUM RGB-D layout keeps colour frames in rgb/
        return ImageFolderSource(rgb if rgb.is_dir() else p)
    if p.suffix.lower() in VIDEO_SUFFIXES:
        return VideoFileSource(p)
    raise InputError(f"Unsupported input type '{p.suffix}'. Use a video ({', '.join(sorted(VIDEO_SUFFIXES))}) or an image folder.")
