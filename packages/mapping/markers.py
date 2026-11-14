"""Printed square markers (OpenCV ArUco / AprilTag dictionaries): print sheets and detect corners in frames.

python -m mapping.markers make --dict 4X4_50 --ids 0 1 --side-mm 160 --out assets/markers/

The printed side length is a design value only: printers rescale, so the side measured on paper with a ruler is the
one used for metric scale (docs/PLAN_METRIC_SCALE.md 4.1).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import cv2
import numpy as np

DICTS = {"4X4_50": cv2.aruco.DICT_4X4_50, "5X5_100": cv2.aruco.DICT_5X5_100,
         "APRILTAG_36h11": cv2.aruco.DICT_APRILTAG_36h11}
A4_MM = (210.0, 297.0)


def dictionary(name: str):
    if name not in DICTS:
        raise ValueError(f"unknown marker dictionary {name!r}; use one of {sorted(DICTS)}")
    return cv2.aruco.getPredefinedDictionary(DICTS[name])


def marker_image(dict_name: str, marker_id: int, side_px: int) -> np.ndarray:
    """Black-bordered marker bitmap (uint8, 0 = black), side_px x side_px, without quiet zone."""
    return cv2.aruco.generateImageMarker(dictionary(dict_name), int(marker_id), int(side_px), borderBits=1)


def detector(dict_name: str):
    p = cv2.aruco.DetectorParameters()
    p.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX  # sub-pixel corners: the scale is read from them
    return cv2.aruco.ArucoDetector(dictionary(dict_name), p)


def detect(image: np.ndarray, dict_name: str, det=None) -> list[tuple[int, np.ndarray]]:
    """[(marker_id, corners 4x2 float64 px)], corners in marker order (top-left, top-right, bottom-right, bottom-left
    of the printed marker), so corner k is the same physical corner in every frame."""
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    corners, ids, _ = (det or detector(dict_name)).detectMarkers(gray)
    if ids is None:
        return []
    return [(int(i), np.asarray(c, float).reshape(4, 2)) for i, c in zip(np.ravel(ids), corners)]


def side_lengths_px(corners: np.ndarray) -> np.ndarray:
    return np.linalg.norm(corners - np.roll(corners, -1, axis=0), axis=1)


def make_sheets(dict_name: str, ids: list[int], side_mm: float, out_dir: Path, dpi: int = 300) -> dict:
    """One A4 sheet (portrait) per marker id, marker centred with a white margin and a printed instruction line."""
    W, H = (round(v / 25.4 * dpi) for v in A4_MM)
    side_px = round(side_mm / 25.4 * dpi)
    if side_mm + 20 > A4_MM[0]:
        raise ValueError(f"side {side_mm} mm leaves less than 1 cm white margin on A4")
    out_dir.mkdir(parents=True, exist_ok=True)
    sheets = []
    for mid in ids:
        page = np.full((H, W), 255, np.uint8)
        y0, x0 = (H - side_px) // 2, (W - side_px) // 2
        page[y0:y0 + side_px, x0:x0 + side_px] = marker_image(dict_name, mid, side_px)
        lines = [f"Skywarden scale marker  {dict_name}  id {mid}",
                 f"design side {side_mm:g} mm (black square). Print at 100 % (no fit-to-page).",
                 "Measure the printed black side with a ruler (mm) and use THAT value.",
                 "Keep the sheet flat and matte; leave the white margin visible."]
        for k, t in enumerate(lines):
            cv2.putText(page, t, (round(0.06 * W), round(0.06 * H) + k * round(0.022 * H)), cv2.FONT_HERSHEY_SIMPLEX,
                        0.0011 * W, 0, max(1, round(0.0012 * W)), cv2.LINE_AA)
        cv2.putText(page, f"id {mid}", (x0, y0 + side_px + round(0.04 * H)), cv2.FONT_HERSHEY_SIMPLEX, 0.0016 * W, 0,
                    max(1, round(0.0016 * W)), cv2.LINE_AA)
        name = f"aruco_{dict_name.lower()}_id{mid}.png"
        path = out_dir / name
        cv2.imwrite(str(path), page)
        # the sheet must read back as the id it was made for (else the printout is useless)
        found = [i for i, _ in detect(cv2.resize(page, (W // 4, H // 4), interpolation=cv2.INTER_AREA), dict_name)]
        sheets.append({"file": name, "id": mid, "side_mm_design": side_mm, "side_px": side_px,
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "readback_ids": found,
                       "readback_ok": found == [mid]})
    manifest = {"schema": "skywarden/marker_sheets@1", "dictionary": dict_name, "dpi": dpi, "page_mm": list(A4_MM),
                "page_px": [W, H], "opencv": cv2.__version__, "sheets": sheets,
                "note": "side_mm_design is not a measurement; measure the printed side",
                "command": " ".join(["python -m mapping.markers", *sys.argv[1:]])}
    (out_dir / "sheets.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mapping.markers")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make", help="write printable A4 marker sheets")
    m.add_argument("--dict", default="4X4_50", choices=sorted(DICTS))
    m.add_argument("--ids", type=int, nargs="+", default=[0, 1])
    m.add_argument("--side-mm", type=float, default=160.0)
    m.add_argument("--dpi", type=int, default=300)
    m.add_argument("--out", default="assets/markers")
    a = ap.parse_args(argv)
    man = make_sheets(a.dict, a.ids, a.side_mm, Path(a.out), a.dpi)
    print(json.dumps({s["file"]: s["readback_ok"] for s in man["sheets"]}))
    return 0 if all(s["readback_ok"] for s in man["sheets"]) else 1


if __name__ == "__main__":
    sys.exit(main())
