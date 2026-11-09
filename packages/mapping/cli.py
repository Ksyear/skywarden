"""python -m mapping.cli build|replay ... (see scripts/build_sparse_map.sh, scripts/replay_map.sh)."""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path

from .frame_selection import SelectionConfig
from .labels import DataOrigin
from .replay import ReplayError, load_session
from .sparse_pipeline import BuildRequest, build_sparse_map


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mapping")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build a sparse map from a video or image folder")
    b.add_argument("input")
    b.add_argument("--session", help="session id (default: timestamp)")
    b.add_argument("--maps-root", default="outputs/maps")
    b.add_argument("--origin", choices=[o.value for o in DataOrigin], default=DataOrigin.UNSPECIFIED.value)
    b.add_argument("--camera-json", help='{"model": "...", "params": [...]} known intrinsics; omit to let COLMAP estimate')
    b.add_argument("--fps", type=float, default=3.0, help="target sampling rate for video sources")
    b.add_argument("--max-frames", type=int, default=150)
    b.add_argument("--source-note", default="")
    b.add_argument("--mapper", choices=["global", "incremental"], default="global")
    b.add_argument("--selection", choices=["fixed", "adaptive"], default="fixed", help="frame selection (adaptive = F1, docs/PLAN_FRAMES_AND_DEPTH.md)")
    b.add_argument("--force-frames", type=int, nargs="*", default=[], help="source frame indices that must be kept (evaluation frames)")
    b.add_argument("--selection-masks", help="sky/person segmentation ONNX used to ignore features on sky and people during adaptive selection")
    r = sub.add_parser("replay", help="load a stored map and print its summary")
    r.add_argument("session_dir")
    q = sub.add_parser("rate", help="evaluate map quality of an existing session (built before quality checks)")
    q.add_argument("session_dir")
    a = ap.parse_args(argv)

    if a.cmd == "rate":
        from .rate import rate_session
        try:
            res = rate_session(Path(a.session_dir))
        except ReplayError as e:
            print(f"rate failed: {e}", file=sys.stderr)
            return 2
        print(json.dumps({"verdict": res["verdict"], "failed_checks": res["failed_checks"],
                          "checks": {k: v["value"] for k, v in res["checks"].items()}}, indent=1))
        return 0

    if a.cmd == "replay":
        try:
            s = load_session(Path(a.session_dir))
        except ReplayError as e:
            print(f"replay failed: {e}", file=sys.stderr)
            return 2
        print(json.dumps(s.summary(), indent=1))
        return 0

    camera = json.loads(Path(a.camera_json).read_text()) if a.camera_json else None
    session = a.session or datetime.now().strftime("%Y%m%d-%H%M%S")
    req = BuildRequest(Path(a.input), Path(a.maps_root) / session, DataOrigin(a.origin), camera,
                       SelectionConfig(target_fps=a.fps, max_frames=a.max_frames, mode=a.selection, force_indices=tuple(a.force_frames), mask_weights=a.selection_masks), a.source_note,
                       mapper=a.mapper)
    # Run the build in a worker so this (main) thread stays in Python and can receive SIGTERM/SIGINT while
    # COLMAP is busy in C++; the signal only sets the event, the pipeline forwards it to COLMAP.
    cancel = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: cancel.set())
    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(build_sparse_map(req, cancel)), name="build")
    worker.start()
    while worker.is_alive():
        worker.join(0.2)
    rep = result
    q = rep.get("quality") or {}
    print(json.dumps({**{k: rep.get(k) for k in ("session_id", "status", "labels", "frames", "input_check",
                                                 "reconstruction", "failure", "timings_s")},
                      "quality": {"verdict": q.get("verdict"), "failed_checks": q.get("failed_checks")}}, indent=1))
    return {"SUCCEEDED": 0, "CANCELLED": 130}.get(rep["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
