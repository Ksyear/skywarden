# FAILURES — observed problems, what was tried, what to test next (2026-11-08)

Rule from `docs/RECOVERY_MATRIX.md`: record the real command and log first, then the alternative that was actually executed. Nothing here is hidden behind the PASS rows in `docs/VALIDATION_LOG.md`.

## F-01 pycolmap cannot initialise with the official FULL_OPENCV calibration (TUM fr1/desk)

**Status:** FAIL for this configuration; recovered via two alternatives (both PASS). Root cause unverified.

**Reproduce**
```zsh
scripts/build_sparse_map.sh data/public/tum_fr1_desk/rgbd_dataset_freiburg1_desk \
  --session tum_fr1_desk --origin PUBLIC_DATASET \
  --camera-json data/public/tum_fr1_desk/camera.json
```
`camera.json` = FULL_OPENCV, params `517.3, 516.5, 318.6, 255.3, 0.2624, -0.9531, -0.0054, 0.0026, 1.1633, 0, 0, 0` (Freiburg 1 RGB values from the TUM file-formats page).

**Observed** (`outputs/evidence/m1/build_tum_fr1_desk.log`, `reports/tum_fr1_desk.map_report.json`)
- Frame selection, SIFT extraction (53/53 images, 904-3,572 features each, prior focal 516.90 px) and sequential matching (146 pairs) completed normally; per-image feature counts are identical to the successful runs, so the inputs were the same.
- Incremental mapping: every candidate initial pair was "Discarding reconstruction due to bad initial pair" (pairs #10/#8, #49/#46, #7/#12, …); pycolmap relaxed the initialisation constraints four times, then `=> No good initial image pair found.` and `E … sfm.cc:310] Failed to create any sparse model`.
- Report: `status=FAILED`, `failure.stage=MAP`, `recoverable=true`, wall time 5.28 s.

**Hypothesis (not confirmed):** the large k3 term (1.1633) in the FULL_OPENCV model distorts the undistortion used during two-view initialisation enough that no pair passes the initial-pair checks. Evidence for it is only that dropping k3 (F-01-B) fixed initialisation while every other setting stayed the same.

**Alternatives executed (RECOVERY_MATRIX row "COLMAP 영상 SfM 등록 실패" / plan fallback F2, F4)**

| | Change | Result |
|---|---|---|
| F-01-A | No camera file; COLMAP estimates a SIMPLE_RADIAL camera (`UNCALIBRATED_ESTIMATED`) | PASS: 48/53 registered, 3,987 points, 0.665 px, ATE 1.86 cm RMSE (Sim(3), scale from GT). `outputs/evidence/m1/build_tum_fr1_desk_estK.log` |
| F-01-B | `camera_opencv4.json`: same fx, fy, cx, cy, k1, k2, p1, p2; k3 dropped; model OPENCV | PASS: 49/53 registered, 4,297 points, 0.831 px, ATE 2.05 cm RMSE. `outputs/evidence/m1/build_tum_fr1_desk_K4.log` |

Both outputs are labelled `scale=RELATIVE`, `NOT_NAVIGATION_SAFE`; the ATE figures borrow scale from ground truth for evaluation only.

**Next test to settle the cause** (not yet run): pre-undistort the 53 selected frames with OpenCV using all five official coefficients, then build with a PINHOLE camera (`fx, fy, cx, cy` only). If that initialises, the failure is in the FULL_OPENCV handling of k3 during initialisation rather than in the data. A second check is the same FULL_OPENCV build with k3 set to 0 explicitly, to confirm that k3 alone is the trigger.

## F-02 Default console port 8765 already in use

**Observed:** another local program on this Mac holds port 8765, so the console was served at `http://127.0.0.1:8780` (`outputs/evidence/server.log`: "Map console: http://127.0.0.1:8780/ (HARDWARE_ARMED=False, DRY_RUN=True)").

**Handling:** `apps/desktop/server.py` defaults to `--port 8780` and prints a hint to run `scripts/run_demo.sh --port <free port>` when binding fails. Nothing was killed or reconfigured outside the project. Documentation now uses 8780.

## F-03 `uv` shim has no configured version

**Observed** (`outputs/evidence/m0/env.txt`): "uv: mise shim has no version configured (not changed)". The implementation plan proposed `uv venv`.

**Handling:** project venv created with `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` (Python 3.13.16). The mise/uv configuration was intentionally left untouched because changing it would affect the user's shell outside this project. Nothing else depends on uv.

## F-04 Two UI defects found and fixed during the headless UI run

Reported by the implementer while bringing `tests/ui/ui_smoke.mjs` to 26/26; the final JSON only shows the passing state, so these are recorded from the implementer's account rather than from a failing artifact. Git history (commit `77f54c0`) contains the fixed versions of the files; the earlier failing run output was not archived, so the status stays "implementer-reported".

1. The "Sample rate (fps)" number field had a `step` that did not match the allowed values, so native form validation blocked "Build map" submissions from the UI.
2. A CSS rule overrode the `[hidden]` attribute, leaving the loading overlay drawn over the map after a load completed.

Both are covered indirectly by the passing checks "Build enabled for a valid input", "build finishes and auto-loads" and "real map loads" in `outputs/evidence/ui/ui_smoke.json`.

## Code review fixes (R-01..R-10, same day, after the first evidence pass)

A code review produced ten findings; Opus confirmed each one and fixed them in a single commit ("Fix code-review findings..."). The project is now a git repository; the relevant history is commits `77f54c0`, `ede3bbe`, `26468f7` and a later commit adding `assets/design/PROMPTS.json` (which hash carries the review-fix commit message was not stated; inspect `git log` to map them). Regression evidence: `scripts/test_all.sh` went from 54 to 61 passed (`outputs/evidence/tests/pytest-20261108-213015.log`: control 11->13, pipeline 4->6, server 7->8, tello 9->11), `node tests/ui/ui_smoke.mjs` still 26/26 (`outputs/evidence/ui/ui_smoke.json`, 12:30 UTC), and the closed-loop sim was re-run (`outputs/evidence/m3/tracking_sim.json`). Status for the set: PASS (regression tests). Numbers quoted below come from those files; the finding descriptions come from the review and the implementer.

| # | Finding (confirmed) | Fix | Evidence |
|---|---|---|---|
| R-01 | Yaw PID derivative had the wrong sign: the D term reinforced the error instead of damping it | Sign corrected | `tests/test_control.py` 13 passed; sim centering error 4-6 s improved 26.4 px -> 21.3 px with the same scenario |
| R-02 | SIGTERM cancel could not interrupt pycolmap's C++ stages (EXTRACT_FEATURES, MATCH, MAP) | CLI runs the build in a worker thread; the main thread handles the signal and marks the session CANCELLED | `outputs/evidence/review_fixes/cancel_latency.txt`: EXTRACT_FEATURES stop latency 0.47 s, MAP 0.04 s, both exit 130 / `status=CANCELLED` / "cancelled by user (InterruptedError)"; real TUM input, 613 candidates -> 150 selected (`cancel_probe_*.log`) |
| R-03 | Calibration intrinsics were applied unscaled when the build resolution differed from the calibration resolution | Intrinsics scaled per camera model and calibration resolution; aspect-ratio mismatch rejected | `tests/test_pipeline.py` 6 passed |
| R-04 | Re-running a session id left stale frames/models from the previous run in place | Rerun clears stale frames and models first | `tests/test_pipeline.py` |
| R-05 | Input hashing (SHA-256 of large inputs) was not cancellable | Hashing checks the cancel flag | `tests/test_pipeline.py` |
| R-06 | `TelloClient` could consume a late reply from a previous command as the answer to the next one, and the hardware gate was checked after socket work | Late replies drained; `HardwareDisarmed` gate evaluated before the socket is touched | `tests/test_tello.py` 11 passed (0 sends in 4 env combinations) |
| R-07 | MockTello heading was integrated as an integer, accumulating rounding error | Float heading integration | `tests/test_tello.py` |
| R-08 | Missing battery reading was treated as OK | Missing battery now forces STOP | `tests/test_control.py` / `tests/test_tello.py` |
| R-09 | Server accepted `.` / `..` as session ids | Rejected with 4xx | `tests/test_server.py` 8 passed |
| R-10 | Two concurrent job-start requests could both start a build | Job start serialised | `tests/test_server.py` |

Which test file covers R-04, R-05, R-07, R-08 and R-10 individually is inferred from the per-file count increases; the test names were not provided with the evidence.

## Not failures, but open gates

- REAL_VIDEO route NOT_RUN: `assets/input/` is empty. Command in `docs/PROGRESS.md`.
- STALE_DATA banner in the web UI not yet asserted (NOT_RUN); the stale-frame STOP itself is verified in `outputs/evidence/m3/tracking_sim.json` (`stale_frame_stops: 5`, `nonzero_yaw_during_freeze_after_0.3s: 0`).
