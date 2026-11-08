"""Ray-cast a textured box room from a known camera path (TUM-style output) for pipeline tests.

Output folder: rgb/<timestamp>.png, groundtruth.txt (TUM: t tx ty tz qx qy qz qw, world_from_camera),
camera.json (exact PINHOLE intrinsics) and the SYNTHETIC_DATA marker that the mapping pipeline enforces.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

from mapping.labels import SYNTHETIC_MARKER as MARKER

# Planes n.X = c (camera axes: x right, y down, z forward). Room interior around the origin.
_PLANES = [((0, 0, 1), 4.0), ((0, 1, 0), 1.2), ((0, 1, 0), -1.6), ((1, 0, 0), -3.0), ((1, 0, 0), 3.0)]
_PX_PER_UNIT = 170.0
_TEX = 1400


def _texture(rng: np.random.Generator) -> np.ndarray:
    tex = (rng.random((_TEX // 8, _TEX // 8, 3)) * 255).astype(np.uint8)
    tex = cv2.resize(tex, (_TEX, _TEX), interpolation=cv2.INTER_CUBIC)
    for _ in range(900):
        c = tuple(int(v) for v in rng.integers(0, 256, 3))
        x, y = (int(v) for v in rng.integers(0, _TEX, 2))
        if rng.random() < 0.5:
            cv2.circle(tex, (x, y), int(rng.integers(4, 26)), c, -1)
        else:
            w, h = (int(v) for v in rng.integers(6, 40, 2))
            cv2.rectangle(tex, (x, y), (x + w, y + h), c, -1)
    return tex


def camera_path(n: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """(R_world_cam, C_world) pairs: sideways translation with gentle yaw, giving parallax for SfM."""
    out = []
    for s in np.linspace(0.0, 1.0, n):
        C = np.array([-1.2 + 2.4 * s, 0.1 * np.sin(4 * s), 0.6 * s])
        R = Rotation.from_euler("yx", [np.deg2rad(-12 + 24 * s), np.deg2rad(4 * np.sin(3 * s))]).as_matrix()
        out.append((R, C))
    return out


def render(K: np.ndarray, R: np.ndarray, C: np.ndarray, size: tuple[int, int], textures: list[np.ndarray]) -> np.ndarray:
    w, h = size
    u, v = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    rays = np.stack([(u - K[0, 2]) / K[0, 0], (v - K[1, 2]) / K[1, 1], np.ones_like(u)], -1) @ R.T
    best_t = np.full((h, w), np.inf)
    img = np.zeros((h, w, 3), np.uint8)
    for (n, c), tex in zip(_PLANES, textures):
        n = np.asarray(n, float)
        denom = rays @ n
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (c - n @ C) / denom
        t[~np.isfinite(t) | (t <= 0)] = np.inf
        X = C + rays * t[..., None]
        a, b = [i for i in range(3) if n[i] == 0]  # in-plane axes
        mx = ((X[..., a] * _PX_PER_UNIT) % _TEX).astype(np.float32)
        my = ((X[..., b] * _PX_PER_UNIT) % _TEX).astype(np.float32)
        sample = cv2.remap(tex, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
        closer = t < best_t
        img[closer] = sample[closer]
        best_t = np.minimum(best_t, t)
    return img


def generate(out_dir: Path, n_frames: int = 24, size: tuple[int, int] = (480, 360), seed: int = 7) -> Path:
    rng = np.random.default_rng(seed)
    textures = [_texture(rng) for _ in _PLANES]
    w, h = size
    K = np.array([[400.0, 0, w / 2], [0, 400.0, h / 2], [0, 0, 1]])
    rgb = out_dir / "rgb"
    rgb.mkdir(parents=True, exist_ok=True)
    lines = ["# synthetic ground truth (SIMULATED)", "# timestamp tx ty tz qx qy qz qw"]
    for i, (R, C) in enumerate(camera_path(n_frames)):
        ts = 1000.0 + i / 10.0
        cv2.imwrite(str(rgb / f"{ts:.6f}.png"), render(K, R, C, size, textures))
        q = Rotation.from_matrix(R).as_quat()
        lines.append(f"{ts:.6f} " + " ".join(f"{v:.6f}" for v in (*C, *q)))
    (out_dir / "groundtruth.txt").write_text("\n".join(lines) + "\n")
    (out_dir / "camera.json").write_text(json.dumps({"model": "PINHOLE", "params": [400.0, 400.0, w / 2, h / 2],
                                                     "source": "synthetic renderer (exact)"}))
    (out_dir / MARKER).write_text(f"seed={seed} frames={n_frames} size={w}x{h}\n")
    return out_dir


if __name__ == "__main__":
    import sys
    print(generate(Path(sys.argv[1] if len(sys.argv) > 1 else "outputs/synthetic/room_seed7")))
