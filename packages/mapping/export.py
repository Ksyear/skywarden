"""Write a COLMAP reconstruction as files the UI and tests read: sparse.ply and trajectory.json."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

PLY_DTYPE = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
                      ("red", "u1"), ("green", "u1"), ("blue", "u1"),
                      ("first_seen", "<u4")])

COORDINATE_NOTE = ("COLMAP world frame, arbitrary origin and RELATIVE scale. Camera axes: x right, y down, "
                   "z forward. center = -R^T t for cam_from_world [R|t].")


def write_ply(path: Path, points: np.ndarray) -> None:
    """points: structured array with PLY_DTYPE. first_seen = trajectory order of the first camera observing the point."""
    header = ("ply\nformat binary_little_endian 1.0\n"
              "comment Skywarden sparse map (RELATIVE scale, NOT_NAVIGATION_SAFE)\n"
              f"element vertex {len(points)}\n"
              "property float x\nproperty float y\nproperty float z\n"
              "property uchar red\nproperty uchar green\nproperty uchar blue\n"
              "property uint first_seen\nend_header\n")
    tmp = path.with_suffix(".ply.tmp")
    with open(tmp, "wb") as fh:
        fh.write(header.encode("ascii"))
        fh.write(points.astype(PLY_DTYPE).tobytes())
    tmp.replace(path)


class PlyError(ValueError):
    pass


_PLY_TYPES = {"float": "<f4", "float32": "<f4", "double": "<f8", "uchar": "u1", "uint8": "u1",
              "int": "<i4", "uint": "<u4", "short": "<i2", "ushort": "<u2"}


def read_ply(path: Path) -> np.ndarray:
    """Strict reader for binary little-endian vertex-only PLY files (what this project writes)."""
    data = Path(path).read_bytes()
    end = data.find(b"end_header\n")
    if not data.startswith(b"ply\n") or end < 0:
        raise PlyError(f"{path}: not a PLY file")
    header = data[:end].decode("ascii", errors="replace").splitlines()
    if "format binary_little_endian 1.0" not in header:
        raise PlyError(f"{path}: only binary_little_endian PLY is supported")
    count, fields, in_vertex = None, [], False
    for line in header:
        parts = line.split()
        if parts[:2] == ["element", "vertex"]:
            count, in_vertex = int(parts[2]), True
        elif parts[:1] == ["element"]:
            in_vertex = False
        elif parts[:1] == ["property"] and in_vertex:
            if parts[1] == "list" or parts[1] not in _PLY_TYPES:
                raise PlyError(f"{path}: unsupported property '{line}'")
            fields.append((parts[2], _PLY_TYPES[parts[1]]))
    if count is None or not fields:
        raise PlyError(f"{path}: missing vertex element")
    dtype = np.dtype(fields)
    body = data[end + len(b"end_header\n"):]
    if len(body) != count * dtype.itemsize:
        raise PlyError(f"{path}: expected {count * dtype.itemsize} bytes of vertex data, found {len(body)}")
    return np.frombuffer(body, dtype=dtype, count=count)


def export_reconstruction(rec, out_dir: Path, frames_by_name: dict[str, dict]) -> dict:
    """Writes sparse.ply + trajectory.json for one pycolmap.Reconstruction. Returns basic counts."""
    images = sorted((rec.image(i) for i in rec.reg_image_ids()), key=lambda im: im.name)
    rank = {im.image_id: k for k, im in enumerate(images)}

    poses = []
    for k, im in enumerate(images):
        cfw = im.cam_from_world() if callable(im.cam_from_world) else im.cam_from_world
        R = np.asarray(cfw.rotation.matrix())
        t = np.asarray(cfw.translation)
        T = np.eye(4)
        T[:3, :3], T[:3, 3] = R.T, -R.T @ t  # world_from_camera
        meta = frames_by_name.get(im.name, {})
        poses.append({
            "order": k,
            "image_name": im.name,
            "source_index": meta.get("source_index"),
            "timestamp_s": meta.get("timestamp_s"),
            "T_world_camera": np.round(T, 7).tolist(),
            "num_points3D": im.num_points3D,
        })

    pts = np.zeros(rec.num_points3D(), dtype=PLY_DTYPE)
    for j, (pid, p) in enumerate(rec.points3D.items()):
        pts[j]["x"], pts[j]["y"], pts[j]["z"] = p.xyz
        pts[j]["red"], pts[j]["green"], pts[j]["blue"] = p.color
        pts[j]["first_seen"] = min(rank[e.image_id] for e in p.track.elements)
    write_ply(out_dir / "sparse.ply", pts)

    cam = rec.camera(images[0].camera_id) if images else None
    traj = {
        "schema": "skywarden/trajectory@1",
        "coordinate_frame": COORDINATE_NOTE,
        "scale": "RELATIVE",
        "camera": None if cam is None else {
            "model": cam.model.name, "width": cam.width, "height": cam.height,
            "params_refined": [round(float(v), 6) for v in cam.params],
        },
        "poses": poses,
    }
    (out_dir / "trajectory.json").write_text(json.dumps(traj, indent=1))
    return {"registered_images": len(images), "points3D": len(pts)}
