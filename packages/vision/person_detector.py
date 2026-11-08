"""Person detectors that run locally on CPU. Every Detection carries its source so real-model results are never
mixed up with synthetic ones.

YoloxDetector: Megvii YOLOX ONNX (Apache-2.0) through OpenCV DNN, no extra Python packages. The exported graph
returns raw head outputs (box offsets, sigmoid objectness, sigmoid class scores) for every grid cell, so letterbox,
grid decoding and NMS are done here, following YOLOX's own `preproc` / `demo_postprocess`.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from tracking.tracker import Detection

ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = ROOT / "data" / "models" / "yolox"
PERSON = 0  # COCO class index
PAD_VALUE = 114
STRIDES = (8, 16, 32)


@dataclass(frozen=True)
class ModelSpec:
    name: str
    file: str
    sha256: str
    input_size: int
    url: str
    license: str = "Apache-2.0"


MODELS = {
    "yolox_nano": ModelSpec("yolox_nano", "yolox_nano.onnx",
                            "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d", 416,
                            "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx"),
    "yolox_tiny": ModelSpec("yolox_tiny", "yolox_tiny.onnx",
                            "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7", 416,
                            "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx"),
}


class ModelUnavailable(RuntimeError):
    """Weights missing or not the file we verified. The message says how to fix it."""


class PersonDetector(Protocol):
    name: str
    source: str

    def detect(self, image_bgr: np.ndarray) -> list[Detection]: ...


def letterbox(img: np.ndarray, size: int) -> tuple[np.ndarray, float]:
    """Resize keeping aspect ratio into the top-left of a size x size canvas (YOLOX convention). Returns NCHW float32
    blob in 0..255 BGR and the scale factor r (model px = image px * r)."""
    h, w = img.shape[:2]
    r = min(size / h, size / w)
    nh, nw = int(round(h * r)), int(round(w * r))
    canvas = np.full((size, size, 3), PAD_VALUE, np.uint8)
    canvas[:nh, :nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    return canvas.transpose(2, 0, 1)[None].astype(np.float32), r


def _grids(size: int) -> tuple[np.ndarray, np.ndarray]:
    grids, strides = [], []
    for s in STRIDES:
        n = size // s
        yv, xv = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
        grids.append(np.stack([xv, yv], -1).reshape(-1, 2))
        strides.append(np.full((n * n, 1), s))
    return np.concatenate(grids).astype(np.float32), np.concatenate(strides).astype(np.float32)


def decode(raw: np.ndarray, size: int, ratio: float, image_wh: tuple[int, int], score_thr: float,
           nms_iou: float, cls: int = PERSON) -> list[tuple[tuple[float, float, float, float], float]]:
    """raw: (N, 5+C) head output for one image. Returns [(xyxy in image px, score)] for class `cls` after NMS."""
    grid, stride = _grids(size)
    if raw.shape[0] != grid.shape[0]:
        raise ValueError(f"model output has {raw.shape[0]} cells, expected {grid.shape[0]} for input {size}")
    xy = (raw[:, :2] + grid) * stride
    wh = np.exp(np.clip(raw[:, 2:4], -10, 10)) * stride
    scores = raw[:, 4] * raw[:, 5 + cls]
    keep = scores >= score_thr
    if not keep.any():
        return []
    xy, wh, scores = xy[keep] / ratio, wh[keep] / ratio, scores[keep]
    w, h = image_wh
    x1 = np.clip(xy[:, 0] - wh[:, 0] / 2, 0, w)
    y1 = np.clip(xy[:, 1] - wh[:, 1] / 2, 0, h)
    x2 = np.clip(xy[:, 0] + wh[:, 0] / 2, 0, w)
    y2 = np.clip(xy[:, 1] + wh[:, 1] / 2, 0, h)
    boxes = np.stack([x1, y1, x2 - x1, y2 - y1], 1)
    idx = cv2.dnn.NMSBoxes(boxes.tolist(), scores.tolist(), score_thr, nms_iou)
    out = []
    for i in np.asarray(idx).reshape(-1):
        if boxes[i, 2] > 1 and boxes[i, 3] > 1:
            out.append(((float(x1[i]), float(y1[i]), float(x2[i]), float(y2[i])), float(scores[i])))
    return sorted(out, key=lambda d: -d[1])


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class YoloxDetector:
    source = "REAL_MODEL"

    def __init__(self, model: str = "yolox_nano", score_thr: float = 0.35, nms_iou: float = 0.45,
                 models_dir: Path | None = None, threads: int | None = None):
        if model not in MODELS:
            raise ModelUnavailable(f"unknown model '{model}'; choose one of {sorted(MODELS)}")
        self.spec = MODELS[model]
        self.name = model
        path = (models_dir or MODELS_DIR) / self.spec.file
        if not path.is_file():
            raise ModelUnavailable(f"{path} is missing. Download it with scripts/fetch_models.sh "
                                   f"(source: {self.spec.url}).")
        digest = sha256_file(path)
        if digest != self.spec.sha256:
            raise ModelUnavailable(f"{path} sha256 {digest[:12]}… does not match the verified {self.spec.sha256[:12]}…; "
                                   f"delete it and run scripts/fetch_models.sh.")
        if threads:
            cv2.setNumThreads(threads)
        self.net = cv2.dnn.readNetFromONNX(str(path))
        self.score_thr, self.nms_iou = score_thr, nms_iou
        self.last_ms = 0.0

    def info(self) -> dict:
        return {"name": self.name, "source": self.source, "license": self.spec.license, "sha256": self.spec.sha256,
                "input_size": self.spec.input_size, "url": self.spec.url, "score_thr": self.score_thr,
                "nms_iou": self.nms_iou, "runtime": f"opencv-dnn {cv2.__version__} CPU"}

    def detect(self, image_bgr: np.ndarray) -> list[Detection]:
        if image_bgr is None or image_bgr.ndim != 3 or image_bgr.shape[2] != 3 or image_bgr.size == 0:
            raise ValueError("expected an HxWx3 BGR image")
        t0 = time.perf_counter()
        blob, r = letterbox(image_bgr, self.spec.input_size)
        self.net.setInput(blob)
        raw = self.net.forward()[0]
        h, w = image_bgr.shape[:2]
        found = decode(raw, self.spec.input_size, r, (w, h), self.score_thr, self.nms_iou)
        self.last_ms = (time.perf_counter() - t0) * 1000
        return [Detection(b, s, self.source) for b, s in found]


def make_detector(name: str, **kw) -> PersonDetector:
    return YoloxDetector(name, **kw)
