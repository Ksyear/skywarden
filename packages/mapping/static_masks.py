"""Generate COLMAP-compatible keep masks for the sky/person segmentation model."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np

MODEL_SHA256 = "e4e9a6927c2d910c3243f86e392b18da715b41c03e6e6f41672f8f6b8eaa71b5"
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def generate(session: Path, weights: Path, output: Path, dilate: int = 3, refine_sky_delta: float = 0) -> dict:
    if hashlib.sha256(weights.read_bytes()).hexdigest() != MODEL_SHA256:
        raise ValueError("Unexpected SkyWater model SHA-256")
    net = cv2.dnn.readNetFromONNX(str(weights))
    frames = json.loads((session / "frames.json").read_text())
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for frame in frames:
        name = frame["file"]
        if Path(name).name != name or name in ("", ".", ".."):
            raise ValueError(f"Unexpected frame filename: {name}")
        image = cv2.imread(str(session / "frames" / name))
        if image is None:
            raise ValueError(f"Could not read {name}")
        rgb = cv2.cvtColor(cv2.resize(image, (384, 384)), cv2.COLOR_BGR2RGB)
        x = (rgb.astype(np.float32) / 255 - MEAN) / STD
        net.setInput(np.ascontiguousarray(x.transpose(2, 0, 1)[None]))
        logits = net.forward()
        if logits.shape != (1, 4, 384, 384) or not np.isfinite(logits).all():
            raise ValueError(f"Unexpected segmentation output for {name}")
        labels = logits[0].argmax(axis=0).astype(np.uint8)
        sky, person = labels == 1, labels == 3
        restored = 0.0
        if refine_sky_delta > 0:
            # S3r (docs/PLAN_STATIC_MAP.md A1): branches against the sky are often labelled sky; a pixel much darker than
            # the surrounding sky is restored as a depth candidate (multi-view checks still decide)
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
            sky_full = cv2.resize(sky.astype(np.uint8), image.shape[1::-1], interpolation=cv2.INTER_NEAREST).astype(np.float32)
            k = (61, 61)
            local = cv2.boxFilter(gray * sky_full, -1, k) / np.maximum(cv2.boxFilter(sky_full, -1, k), 1e-3)
            back = (sky_full > 0) & (gray < local - refine_sky_delta)
            restored = float(back.mean())
            sky_full[back] = 0
            sky_hi, person_hi = sky_full > 0, cv2.resize(person.astype(np.uint8), image.shape[1::-1], interpolation=cv2.INTER_NEAREST) > 0
            excluded_hi = (sky_hi | person_hi).astype(np.uint8)
            if dilate > 1:
                excluded_hi = cv2.dilate(excluded_hi, np.ones((dilate * 4 + 1, dilate * 4 + 1), np.uint8))
            keep = (1 - excluded_hi) * 255
            if not cv2.imwrite(str(output / (name + ".png")), keep.astype(np.uint8)):
                raise OSError(f"Could not write mask for {name}")
            rows.append({"frame": name, "sky": float(sky_hi.mean()), "person": float(person_hi.mean()),
                         "excluded": float(excluded_hi.mean()), "sky_restored": restored})
            continue
        excluded = (sky | person).astype(np.uint8)
        if dilate > 1:  # S3 (docs/PLAN_STATIC_MAP.md) compares dilate=1 (none) with the original 3x3
            excluded = cv2.dilate(excluded, np.ones((dilate, dilate), np.uint8))
        keep = cv2.resize((1 - excluded) * 255, image.shape[1::-1], interpolation=cv2.INTER_NEAREST)
        if not cv2.imwrite(str(output / (name + ".png")), keep):
            raise OSError(f"Could not write mask for {name}")
        rows.append({"frame": name, "sky": float(sky.mean()), "person": float(person.mean()),
                     "excluded": float(excluded.mean())})
    report = {"model": "Realcat/skywater_seg", "revision": "ec20751", "sha256": MODEL_SHA256,
              "classes_excluded": ["sky", "person"], "dilation_px_at_384": dilate,
              "refine_sky_delta": refine_sky_delta, "frames": rows}
    (output / "mask_report.json").write_text(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dilate", type=int, default=3, help="square dilation at 384 px (1 = none)")
    parser.add_argument("--refine-sky", type=float, default=0, help="restore sky-labelled pixels darker than the local sky by this many grey levels")
    args = parser.parse_args()
    cv2.setNumThreads(3)
    result = generate(args.session, args.weights, args.out, args.dilate, args.refine_sky)
    print(f"Wrote {len(result['frames'])} masks to {args.out}")
