# Implementation Plan — Skywarden, Iteration 1 (2026-11-08)

Author: fable-planner (claude-fable-5-1). Implementer: Opus 5.5. Supersedes nothing; obeys `docs/CAMERA_ONLY_EXECUTION_PLAN.md`, `docs/VERIFICATION_CONTRACT.md`, `docs/RECOVERY_MATRIX.md`.

Environment facts (Opus audit, real): macOS 27.2 arm64 M4 Pro, Python 3.13.16 (mise), uv, ffmpeg 9.0.2, node 24.21, cmake, clang, brew. **No `colmap` binary.** No project code, no `assets/`, no `data/`, not a git repo. codex CLI 0.165 present (image generation unverified).
Fixed decisions: `pycolmap` wheel in project `.venv` (no system change); `brew install colmap` only with user approval. `HARDWARE_ARMED=false`, `DRY_RUN=true`. Map labels: `map_source` STORED_RECONSTRUCTION / CURRENT_SESSION / SIMULATED; `geometry` SPARSE_SFM; `scale` RELATIVE; `NOT_NAVIGATION_SAFE` always present in iteration 1. Every result is PASS / FAIL / BLOCKED / NOT_RUN with raw evidence under `outputs/evidence/<milestone>/`.

## Layout (fixed)
```
packages/{vision,mapping,tracking,control,sim}   adapters/tello   apps/desktop   tests/
scripts/{run_demo.sh,test_all.sh,build_sparse_map.sh,replay_map.sh}
assets/input/ (user clips, gitignored)   data/public/<name>/ (downloaded, gitignored, SOURCE.md kept)
outputs/maps/<session>/{frames/,colmap_db.db,sparse/0/,sparse.ply,trajectory.json,map_report.json,log.txt}
```
Interfaces (dataclasses, `packages/vision/types.py` etc.): Frame(ts, camera_id, image, intrinsics|None), Detection(bbox, score, cls, source=REAL|SYNTHETIC), Track(id, state, x, P), CameraPose(frame_id, ts, T_world_camera 4x4, scale_type, confidence), MapPoint(xyz, rgb, track_ids), DroneCommand(yaw_rate, lateral, forward, vertical, source=MOCK). `map_report.json` schema: tool/version, input path+sha256, source label (REAL_VIDEO|PUBLIC_DATASET|SYNTHETIC), frames_total, frames_extracted, frames_registered, points3d, mean_reproj_error_px (null if unavailable), map_source, geometry, scale, nav_safety, commands[], status.

## M0 — env, venv, skeleton, test runner (dependency: none)
Tasks: `git init` + `.gitignore` (`.venv`, `outputs/`, `assets/input/`, `data/`, `*.db`, `._*`); `uv venv .venv` (Python 3.13; if the `pycolmap` wheel is missing for cp313-macosx-arm64, create the venv with `uv venv --python 3.12` instead and record which); `uv pip install pycolmap numpy opencv-python-headless pytest`; create package skeleton with the dataclasses above; `scripts/test_all.sh` = pytest + import smoke; `docs/ENV_AUDIT.md` data left as raw `outputs/evidence/m0/env.txt` for fable-documenter.
Exit tests:
- M0-T1 PASS iff `.venv/bin/python -c "import pycolmap, cv2, numpy; print(pycolmap.__version__)"` exits 0 and the version string is saved to `outputs/evidence/m0/pycolmap_version.txt`. FAIL → fallback route F1 below.
- M0-T2 PASS iff `scripts/test_all.sh` exits 0 with ≥1 collected test (skeleton import test) and writes `outputs/evidence/m0/pytest.log`.
- M0-T3 PASS iff `scripts/run_demo.sh --check` prints resolved paths and `HARDWARE_ARMED=false DRY_RUN=true` without launching anything.

## M1 — real video → pycolmap sparse SfM → PLY + trajectory + report (depends on M0)
Pipeline (`packages/mapping/`): (1) `extract_frames`: ffmpeg at target fps from auto-diagnosis (default 2 fps for 30–90 s clips, hard cap 300 frames, long side ≤ 1600 px); (2) `filter_frames`: drop Laplacian-variance blur below adaptive threshold (percentile-based, never fixed constant) and near-duplicates (ORB match ratio or mean abs diff), log kept/dropped counts; (3) SfM via pycolmap: feature extraction (SIFT, CPU; verify option names against the installed version with `help()` before use), sequential matching with loop-detection off, incremental mapping, camera model `SIMPLE_RADIAL` single shared camera when K is unknown (mark `UNCALIBRATED_ESTIMATED`); (4) export: `sparse.ply` (xyz+rgb), `trajectory.json` (ordered registered frames, T_world_camera, scale=RELATIVE), `map_report.json`. `scripts/build_sparse_map.sh <input.mp4|frames_dir> <session>` wraps all steps, preserves intermediates, exits non-zero on failure with status in the report.
Synthetic-vs-real: a seed-fixed synthetic generator in `packages/sim/` renders a textured planar/box scene from known poses (OpenCV projection, no external renderer). Its output is labelled `SIMULATED` and lives in a separate session dir; it never counts toward the real-video gate.
Real-data acquisition order: (a) user's own clip in `assets/input/*.mp4` (indoor, static, 30–90 s, translation not pure rotation; Opus writes `docs/CAPTURE_GUIDE.md` data points as raw text for fable-documenter); (b) if absent, one small public SfM/VSLAM image sequence with explicit license — candidates to verify, not assumed: TUM RGB-D `fr1/xyz` (CC BY 4.0, static desk, RGB only needed) or a COLMAP-documented demo set if its license is explicit. Record URL, license text, file size, SHA256 in `data/public/<name>/SOURCE.md`; download into an empty dir; keep under ~1 GB; no third-party crawling.
Exit tests (all write `outputs/evidence/m1/`):
- M1-T1 synthetic smoke: `build_sparse_map.sh` on the synthetic sequence → PASS iff frames_registered ≥ 3, points3d > 0, `sparse.ply` non-empty and parseable by the project PLY reader, report labelled SIMULATED. Checks the pipeline only, not the MVP gate.
- M1-T2 **real-video gate** (MVP-required): same command on (a) or (b) → PASS iff frames_registered ≥ 3 (and ≥ 50 % of filtered frames, otherwise PASS_LOW_QUALITY), points3d > 0, PLY non-empty, mean_reproj_error_px recorded when pycolmap exposes it, source label REAL_VIDEO or PUBLIC_DATASET. 0 registered → FAIL, apply F2; no data available → BLOCKED with exact missing input named.
- M1-T3 schema/parsers: pytest validates `map_report.json` keys, `trajectory.json` 4x4 orthonormal rotations, PLY vertex count == points3d.
- M1-T4 replay: `scripts/replay_map.sh outputs/maps/<session>` loads stored PLY+trajectory and prints counts; PASS iff counts match report and `map_source=STORED_RECONSTRUCTION`.

## M2 — local dashboard (depends on M1 outputs; may start UI shell in parallel)
Server: Python stdlib `http.server` on `127.0.0.1:<port>` serving `apps/desktop/static/` plus JSON endpoints `/api/status`, `/api/map/<session>`, `/api/frame.jpg`, `/api/jobs` (POST start/cancel build). No CDN, no external JS; point viewer is a hand-written canvas 2D orthographic/simple perspective projector with rotate/zoom/pan (three.js not used). Video pane shows extracted frames or MP4 via `<video>` with detection overlays from `/api/tracks`.
Layout: left = camera video + boxes + track IDs (40 %); center = 3D map + trajectory (45 %); right = controls column (15 %: Load clip, Build map, Load stored map, Cancel, toggle SIMULATED view, Record/Stop mock); bottom status bar = FPS, p50/p95 ms, SLAM state, `map_source` / `geometry` / `scale` / `NOT_NAVIGATION_SAFE`, mock PID command, `SIMULATED / NO REAL DRONE`, `ARMED=false`.
States (state machine in `apps/desktop/static/state.js`, all visible as a badge + message): NO_INPUT (drop zone + capture guide link) → LOADING → PROCESSING (progress: step name, frames done/total, elapsed, Cancel enabled) → LOADED_CURRENT_SESSION | LOADED_STORED_MAP | FAILED (recoverable message with the first error line and the next command to run; Retry/Open log) | CANCELLED (partial outputs kept, labelled); plus overlays STALE_DATA (last frame/pose older than 2 s, banner, PID forced STOP) and DISCONNECTED (server unreachable, auto-retry 1 s, inputs disabled). SIMULATED map view is a separate tab, never merged with real data.
Keyboard/focus: Tab order Controls → Viewer → Video; `Space` play/pause, `L` load stored map, `B` build, `Esc` cancel, `R` reset view, `1/2/3` select target track; visible focus ring; aria-live region for status text; no key sends drone commands.
Design tokens (`tokens.css`): bg `#0f1115`, panel `#171a21`, text `#e6e6e6`, muted `#9aa0a6`, ok `#3ddc84`, warn `#ffb020`, fail `#ff5c5c`, sim `#8a7cff` (purple reserved for SIMULATED), real `#4cc3ff`; font system-ui 13/15/20 px; spacing 4/8/16; radius 6.
Exit tests: M2-T1 `scripts/run_demo.sh` starts server, `curl 127.0.0.1:<port>/api/status` returns JSON with hardware_armed=false. M2-T2 loading the M1 session renders point count equal to report (DOM text check via headless fetch of `/api/map` and a JS unit test on the projector with a 3-point fixture). M2-T3 each of the 8 states reachable via `/api/jobs` mock transitions and asserted in a Python test against `/api/status`. M2-T4 stale-data test: freeze frame feed → status shows STALE_DATA within 2.5 s and PID output is STOP.

## M3 — tracking → PID → MockTello (depends on M0; integrates with M2)
Order: (1) `tracking/kalman.py` linear CV with Δt, Mahalanobis gating, Hungarian (scipy or own O(n³)), FSM SEARCHING/LOCKED/PREDICTING/LOST with COASTING ≤ 2 s; synthetic detector first (labelled), YOLO ONNX CPU later as REAL. (2) `control/pid.py` yaw from bbox center error, rate clamp, anti-windup, STOP on stale frame/pose; never distance-from-bbox approach. (3) `adapters/tello/mock_udp.py` MockUdpServer: ack, state, drops, battery; `TelloClient` refuses `takeoff/rc/emergency` when `HARDWARE_ARMED=false`. (4) wire into dashboard overlays and status bar.
Exit tests: M3-T1 synthetic scenarios (ID persistence, 0.5/1.5/2.5 s occlusion → LOST after 2 s, ID switch count, variable Δt) all pass with logged metrics. M3-T2 PID sign/clamp/anti-windup/STOP unit tests. M3-T3 unarmed guard: socket send count == 0 under all commands (mock socket). M3-T4 Mock UDP parse/ack/timeout tests.

## Risks and fallbacks (per RECOVERY_MATRIX)
- F1 pycolmap wheel unavailable/unimportable → try Python 3.12 venv; else ask user approval for `brew install colmap` (CLI path via subprocess); else OpenCV ORB + Essential matrix + recoverPose + triangulation VO labelled `geometry=VO`, no loop closure. Record error log first.
- F2 real-video SfM registers 0 frames → lower fps / fewer frames, raise blur threshold, try `SIMPLE_PINHOLE`, check pure-rotation parallax; else switch to public dataset (b). Never force PASS.
- F3 no real data at all → M1-T2 BLOCKED, name the exact missing input, continue M2/M3.
- F4 no K → COLMAP auto-estimation, mark UNCALIBRATED_ESTIMATED; checkerboard later.
- F5 UI slow → downsample points for canvas (stride), offline build in subprocess, poll progress.
- F6 YOLO/ONNX install fails → OpenCV HOG labelled limited; synthetic detector stays labelled.
- Dense MVS (CUDA) and live SLAM are out of iteration 1; Depth Anything V2 optional later.

## Handoff to Opus (5 lines)
1. Doc created: `docs/IMPLEMENTATION_PLAN.md` (this file); no other docs touched.
2. Do first: M0 (git init, `.venv` with pycolmap, skeleton, `test_all.sh`), then M1 synthetic smoke, then M1-T2 real-video gate with user clip or licensed public set (record URL/license/sha256).
3. Then M2 dashboard (stdlib server, canvas viewer, 8 UI states, stale/disconnected handling), then M3 tracking/PID/MockTello with unarmed guard.
4. Acceptance: M0-T1..T3, M1-T1..T4, M2-T1..T4, M3-T1..T4 as defined above; save raw logs under `outputs/evidence/<milestone>/` and hand paths to fable-documenter for `docs/PROGRESS.md` / `docs/VALIDATION_LOG.md`.
5. Blockers to confirm: pycolmap cp313 arm64 wheel availability (else 3.12 venv or approved brew), presence of a real clip in `assets/input/` (else public dataset license check), codex image generation irrelevant to iteration 1.
