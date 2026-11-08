"""Load a finished session from disk (STORED_RECONSTRUCTION / SIMULATED) and cross-check its files."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .export import PlyError, read_ply


class ReplayError(RuntimeError):
    pass


@dataclass
class StoredSession:
    path: Path
    report: dict
    trajectory: dict
    points: np.ndarray

    def summary(self) -> dict:
        return {"session_id": self.path.name, "map_source": self.report["labels"]["map_source"],
                "labels": self.report["labels"], "registered_images": len(self.trajectory["poses"]),
                "points3D": int(len(self.points)), "report_status": self.report["status"]}


def load_session(path: Path) -> StoredSession:
    try:
        report = json.loads((path / "map_report.json").read_text())
    except FileNotFoundError:
        raise ReplayError(f"{path} has no map_report.json (not a finished build)")
    if report.get("status") != "SUCCEEDED":
        raise ReplayError(f"{path.name}: build status is {report.get('status')}, nothing to replay")
    try:
        traj = json.loads((path / "trajectory.json").read_text())
        points = read_ply(path / "sparse.ply")
    except (FileNotFoundError, PlyError, json.JSONDecodeError) as e:
        raise ReplayError(f"{path.name}: damaged outputs: {e}")
    rec = report["reconstruction"]
    if len(points) != rec["points3D"] or len(traj["poses"]) != rec["registered_images"]:
        raise ReplayError(f"{path.name}: files disagree with report (ply {len(points)} vs {rec['points3D']}, "
                          f"poses {len(traj['poses'])} vs {rec['registered_images']})")
    return StoredSession(path, report, traj, points)
