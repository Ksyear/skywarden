"""Export a nearby subset of the static trial cloud using the saved camera positions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from scipy.spatial import cKDTree

from .export import read_ply, write_ply


def create(session: Path, out: Path, source_layer: str = "dense_static_v3") -> dict:
    if out.exists():
        raise FileExistsError(out)
    poses = json.loads((session / "trajectory.json").read_text())["poses"]
    source = session / source_layer / "dense_verified.ply"
    points = read_ply(source)
    if not poses or not len(points):
        raise ValueError("A populated static v3 map and its camera poses are required")

    centers = np.array([p["T_world_camera"] for p in poses])[:, :3, 3]
    xyz = np.column_stack([points[axis] for axis in "xyz"])
    distances, _ = cKDTree(centers).query(xyz)
    median = float(np.median(distances))
    if median <= 0:
        raise ValueError("The median distance to the camera path must be positive")
    # The stronger 2× crop removed whole structures in Sumida, so keep a wider review corridor.
    radius = 4.5 * median
    local = points[distances <= radius]
    report = {
        "label": "DENSE_STATIC_LOCAL_TRIAL (relative scale)",
        "source_layer": source_layer,
        "purpose": "comparison only: a distance-only crop removes real far structures (structure coverage at t=30 s fell 85.1 % -> 70.0 % in an earlier check)",
        "source_points": len(points),
        "dense_verified_points": len(local),
        "hidden_points": len(points) - len(local),
        "median_camera_distance": median,
        "radius": radius,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=out.parent) as tmp:
        layer = Path(tmp) / out.name
        layer.mkdir()
        write_ply(layer / "dense_verified.ply", local)
        (layer / "dense_report.json").write_text(json.dumps(report, indent=2) + "\n")
        layer.rename(out)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--source", default="dense_static_v3", help="layer folder to crop")
    args = parser.parse_args()
    print(json.dumps(create(args.session, args.out or args.session / "dense_static_local", args.source), indent=2))


if __name__ == "__main__":
    main()
