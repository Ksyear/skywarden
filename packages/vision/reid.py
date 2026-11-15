"""Person appearance embeddings for the tracker's contested-component decisions (CPU, OpenCV DNN, local only).

Model: OSNet x0.25 (Zhou et al., "Omni-Scale Feature Learning for Person Re-Identification", ICCV 2019), weights
trained on Market-1501 (non-commercial research distribution; see data/models/reid/SOURCE.md), exported once to
ONNX by scripts/export_reid_onnx.py in a separate environment. The project runtime needs only OpenCV.

Privacy rules enforced here and in tracking.tracker (docs/PLAN_OCCLUSION_TRACKING.md section 2):
  * the head band (top HEAD_FRAC of every person box) is blanked before inference: no face pixels reach the model;
  * embed() only returns vectors; nothing is cached, logged or written, and the tracker keeps them inside Track
    objects for the life of the track (one session) — there is no gallery and no cross-session matching.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .person_detector import ModelUnavailable, sha256_file

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "data" / "models" / "reid" / "osnet_x0_25_market1501.onnx"
SHA256 = "3900a122fe9a02c1fc7a6f9cf3a4b03d61670d6527d953e34723f29188385c58"
INPUT_HW = (256, 128)
HEAD_FRAC = 0.15
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)


class OsnetReid:
    name = "osnet_x0_25_market1501"
    license = "weights: Market-1501 research use (non-commercial); architecture code MIT"

    def __init__(self, path: Path | None = None, max_batch: int = 16):
        path = path or MODEL
        if not path.is_file():
            raise ModelUnavailable(f"{path} is missing. Create it with scripts/export_reid_onnx.py (see scripts/fetch_models.sh reid).")
        digest = sha256_file(path)
        if digest != SHA256:
            raise ModelUnavailable(f"{path} sha256 {digest[:12]}… does not match the verified {SHA256[:12]}….")
        self.net = cv2.dnn.readNetFromONNX(str(path))
        self.max_batch = max_batch

    def info(self) -> dict:
        return {"name": self.name, "sha256": SHA256, "input_hw": INPUT_HW, "head_band_blanked": HEAD_FRAC,
                "license": self.license, "runtime": f"opencv-dnn {cv2.__version__} CPU", "stored": "never (memory only)"}

    @staticmethod
    def preprocess(frame: np.ndarray, box) -> np.ndarray | None:
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = (int(round(v)) for v in box)
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
        if x2 - x1 < 8 or y2 - y1 < 16:
            return None
        crop = cv2.resize(frame[y1:y2, x1:x2], (INPUT_HW[1], INPUT_HW[0]), interpolation=cv2.INTER_LINEAR)
        x = (crop[:, :, ::-1].astype(np.float32) / 255.0 - MEAN) / STD
        x[: int(round(HEAD_FRAC * INPUT_HW[0]))] = 0.0  # head band -> dataset mean: no face pixels
        return x.transpose(2, 0, 1)

    def embed(self, frame: np.ndarray, boxes) -> np.ndarray:
        """(N, 512) unit vectors; rows of NaN where the box is unusable."""
        out = np.full((len(boxes), 512), np.nan, np.float32)
        xs = [(i, self.preprocess(frame, b)) for i, b in enumerate(boxes)]
        xs = [(i, x) for i, x in xs if x is not None]
        for s in range(0, len(xs), self.max_batch):
            chunk = xs[s:s + self.max_batch]
            self.net.setInput(np.stack([x for _, x in chunk]))
            f = self.net.forward()
            f /= np.maximum(np.linalg.norm(f, axis=1, keepdims=True), 1e-9)
            for (i, _), v in zip(chunk, f):
                out[i] = v
        return out
