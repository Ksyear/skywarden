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

## F-05 Target id inherited by a crossing person and track survival past 2 s (experiment E2, 2026-11-09)

**Status:** FAIL on the original tracker (preserved); fixed in two steps; the first step FAILED a different criterion and was superseded. Final state PASS (`outputs/evidence/e2_two_person_fix_v3/`). Plan: `docs/PLAN_E2_TRACKER_FIX.md`.

**Observed** (`outputs/evidence/e2_two_person_baseline_v1/e2_two_person.json`, control-path counters in `e2_two_person_baseline_v1_control/`)
- While target A was hidden, person B walking through A's predicted position was assigned to A's track for about one frame in 158 of 350 crossing runs (K3, criterion 0). That hit refreshed `last_hit_t`, so A's track outlived the 2.0 s coast limit: LOST after a 2.5 s occlusion in only 10 % (K2, criterion 100 %); without B, 100 %.
- Cause: expiry ran only on tracks left unmatched after Hungarian assignment, so any detection inside the gate could revive a track whose last trusted observation was older than 2 s; and a 1:1 assignment was trusted even when two tracks or two detections were inside one gate.
- Control consequence measured with the real `YawController` on the same tracker: LOCKED while A hidden 162 / 167 frames, non-zero yaw toward B 100 / 167 frames (centre / off-centre crossings). Non-zero yaw while not LOCKED was already 0.

**Fix step 1, fix_v2 (strict gate)** — expiry before association, confirmed tracks associated by gate-graph components (1 track + 1 detection trusted, otherwise AMBIGUOUS with no correction and no `last_hit_t`), `TargetState.AMBIGUOUS`, `TARGET_AMBIGUOUS` STOP, sticky LOST. Result `outputs/evidence/e2_two_person_fix_v2/`: K2 100 %, K3 0, but **K1 at 1.5 s fell to 44 %** (FAIL), the existing closed-loop contract test (1.5 s occlusion must re-lock) broke, and real clips churned ids (vtest 39 -> 441, Montreal 438 -> 992). Reason: a track coasting 1.5 s has a Mahalanobis gate of roughly 500 px, so it linked to every nearby person and stayed AMBIGUOUS until it expired.

**Fix step 2, fix_v3 (final)** — added after seeing the fix_v2 numbers: a physical cap on the association radius, pixel distance <= 0.5 x the track's box height (`TrackerConfig.max_gate_box_frac`), applied together with the chi-squared gate. K1-K3 criteria were not changed at any point. Result `outputs/evidence/e2_two_person_fix_v3/`: K1 100 / 100 %, K2 100 %, K3 0; same-row sensitivity 0 switches; LOCKED while A hidden 0, yaw toward B 0; E3 unchanged; 94 tests pass. Real clips: ids 33 -> 34 / 39 -> 56 / 438 -> 680, LOCKED per frame 1.01 -> 0.84 / 6.32 -> 4.74 / 7.33 -> 4.41, ambiguous frames 50 / 287, 369 / 600, 987 / 1,390.

**What remains true after the fix:** motion alone cannot tell two people apart inside one gate. The fix converts a wrong LOCK into a STOP; it does not guarantee re-lock on the same id after a crossing, and in crowded scenes the controller would spend more time stopped and tracks are shorter. Appearance Re-ID is not planned (safety constraint: no appearance features stored).

## F-06 Tracking panel kept the previous run's frame index when a shorter run loaded (UI, 2026-11-09)

**Status:** fixed and verified — re-run 51 / 51 PASS (`outputs/evidence/ui/ui_smoke.json`, 02:39 KST, attach mode `--attach http://127.0.0.1:9222`; the failing JSON at the same path was overwritten by it).

**Observed** (first run, 02:23 KST, 50 / 51 PASS, headless Dia): the check "tracking run finishes and auto-loads" FAILED with detail "Run finished. 40 frames, 5 confirmed tracks." after a 287-frame run had been played earlier in the same session. All four new AMBIGUOUS checks and every other check passed; 0 console errors.

**Diagnosis (implementer):** pre-existing bug, not introduced by the tracker fix — the panel did not reset the frame index when a run shorter than the current one was loaded, so the auto-loaded 40-frame run was displayed at an out-of-range frame. Why the earlier 47-check run did not trip on it was not established.

**Fix:** frame index reset on run load in `apps/desktop/static/tracking.js`.

**Verification:** `node tests/ui/ui_smoke.mjs --attach http://127.0.0.1:9222` against the user's already-open Dia window — 51 / 51, 0 console errors, "tracking run finishes and auto-loads" PASS with detail "Run finished. 40 frames, 5 confirmed tracks.". Attach mode needed two test-script fixes (not product bugs): `Page.bringToFront` because background tabs get throttled timers (disconnect/reconnect checks timed out), and a pre-reload marker so the reload check cannot pass on the pre-reload document. Headless `--browser` mode still exists.

## F-07 Frame-gap guard refused valid inputs (mapping quality work, 2026-11-09)

**Status:** fixed (guard narrowed); the three refused runs are kept as FAILED reports.

**Observed** (`outputs/maps/*/map_report.json`, `status=FAILED`, `failure.stage=SELECT_FRAMES`, `recoverable=true`):
- `mq_bangkok_60s_after`: "selected frames are up to 1.33 s apart (> 1.0 s): the cap of 150 frames would break overlap. Use a shorter section or a higher --max-frames." (180 candidates, 4 blur-rejected, 26 dropped by cap, 18 cuts).
- `mq_utrecht_follow_0035_0135`: same message, 1.33 s (27 dropped by cap).
- `mq_utrecht_follow_0035_0135_mf300`: "up to 1.00 s apart (> 1.0 s)" — the measured gap was 1.001 s with 0 frames dropped by the cap, so the cap was not the cause.
- Implementer-reported, no report archived: the same guard refused TUM fr1/desk, which rates GOOD and has ground truth.

**Diagnosis:** the guard treated any kept-pair gap above 1.0 s as fatal, including gaps that come from the clip itself (blur rejection, cuts, 3 fps sampling) rather than from the 150-frame cap.

**Fix:** the gap check is fatal only when the frame cap caused the gap; `frames.max_gap_s` is still written and the quality verdict still checks `max_internal_gap_s <= 2.0 s`. Recovery runs that followed: `mq_bangkok_60s_after_mf300` (`--max-frames 300`, 176 selected, 0 dropped, max gap 1.0 s) built but rated POOR; `mq_utrecht_follow_0035_0135_fps5` (step 6, 298 selected, max gap 0.601 s) rated PARTIAL.

## F-08 Per-segment diagnosis detector does not separate the FSC drift (plan B, 2026-11-09)

**Status:** FAIL against the frozen expectation; reported, not tuned. Drift cause not confirmed by it.

**Expected (frozen in `docs/PLAN_GEOMETRY_AND_DENSE.md` B):** FSC windows covering 59.7-70.4 s carry at least one flag, windows before 55 s none; Sumida zero flags.

**Observed:** `outputs/maps/mq_sumida_0025_0125/diagnosis_segments.json` flags 9 of 23 windows (`new_over_old_out_of_band`, `r_out_of_band`) on a GOOD map with 0 jumps; `outputs/maps/mq_fsc_campus_aerial/diagnosis_segments.json` flags 0 of 28 windows although the same model has 23 time-normalised jumps at 59.7-70.7 s. `outputs/evidence/geometry/diagnose_mq_fsc_campus_aerial.json`: r median 2.074, by thirds 2.221 / 1.515 / 3.094 — the late third is inside the [1/3, 3] band. The disagreement itself is measured in `outputs/evidence/geometry/fsc_late_stretch_speed.json`: incremental camera speed 10.3x the clip median at 59.7-71 s (0.97-1.06x before), global 1.04x, while source image flow at 58-70 s is 0.34-0.41 px/frame against 0.43-1.18 at 10-50 s.

**What was done instead:** the comparison experiment C1 (global mapper) removed the FSC jumps (23 -> 0) and kept the Sumida guard, so the global mapper became the default on the strength of jumps, registration, reprojection and point counts; the "flagged windows decrease" criterion was declared not evaluable. The `low_confidence_windows` field stays in `coverage.json` but is not used as a guard.

**Next test:** redefine or recalibrate the window statistics only in a new plan section with a new baseline table (the plan forbids silent threshold edits); until then, FSC drift under the incremental mapper is "reconstruction and image motion disagree in 59.7-70.4 s; cause not confirmed".

## F-09 Dense route: rectified SGBM degenerate for forward motion; mesh visual check FAIL (2026-11-09)

**Status:** depth method replaced (criteria unchanged), dense PASS on two sessions; mesh remains EXPERIMENTAL with a FAIL visual check.

**Observed 1 (method):** the planned rectified two-view SGBM has no usable disparity when the baseline lies along the optical axis (walking or flying forward puts the epipole inside the image). Recorded in every `dense_report.json` under `deviation_from_plan`. Replacement: CPU plane-sweep MVS (ZNCC, best 2 of 4 neighbours, 640 px). D3 verification thresholds (sparse agreement >= 0.70, consistency kept >= 0.30) unchanged.

**Observed 2 (PatchMatch):** `outputs/evidence/dense/patch_match_probe.txt` — pycolmap 4.2.1 raises "Dense stereo reconstruction requires CUDA or HIP, neither of which is available on your system." BLOCKED on this Mac, not retried.

**Observed 3 (synthetic acceptance):** `outputs/evidence/dense/synthetic_room_surface_check.json` — dense points lie on the true room surfaces (p50 0.0095 room units, 97.8 % within 0.05) yet acceptance is FAIL because only 3 of 8 views pass the consistency share, whose frozen definition counts non-overlapping pixels. Definition kept; the FAIL stands beside the surface measurement.

**Observed 4 (mesh):** Poisson depth 10 on verified dense points. The multi-threaded run aborts, so it runs single-threaded in a child process. `mesh.visual_check.result = FAIL` in `outputs/maps/mqg_{sumida_0025_0125,fsc_campus_aerial}/dense/dense_report.json`: FSC shows roofs and trees but radial streak artefacts converging near the image centre (cause not confirmed); Sumida covers only a few trees (no ground points). Decision: not offered as a console layer; verified dense points remain the shape result. Evidence `outputs/evidence/dense/*_mesh_sameview*.jpg`.

**Remaining gaps:** ground directly in front of the walking camera mostly empty (repetitive paving at a steep angle); some cloud points at the top of Sumida views; 9 of 117 Sumida views REJECTED.

## F-10 Monocular VO against SfM fails with an estimated focal length (2026-11-09)

**Status:** FAIL for the independent configuration; PASS only with a non-independent focal. Kept as measured.

**Observed** (`outputs/evidence/vo/README.txt`, `sumida_fxest_vs_colmap.json`, `sumida_fxcolmap_vs_colmap.json`): Sumida 0-60 s, 900 processed frames. Estimated K (fx = 0.9 x width): Sim(3) RMSE vs the SfM trajectory 3.644 over an SfM path of 15.663 = 23.3 % (criterion <= 15 %) -> FAIL. Focal borrowed from the SfM-refined camera (about 0.52 x width, distortion ignored): 0.401 / 15.666 = 2.56 % -> PASS, but the reference and the intrinsics come from the same reconstruction, so it is not independent evidence.

**Reading:** the wide-angle clip makes the 0.9 x width focal guess wrong; a calibrated camera matters more than the tracker. Also recorded: one development fix before these numbers (homography preferred on a planar-dominant synthetic scene discarded real translation; fixed, criteria unchanged, first failing run kept under `outputs/evidence/vo/dev_log/`).

**Next test:** a clip with a known calibration (or the user's own camera with a checkerboard) so the VO focal is independent of SfM.

## F-11 Utrecht follow clip: PARTIAL under both mappers, unresolved (2026-11-09)

**Observed:** incremental at 5 fps (`mq_utrecht_follow_0035_0135_fps5`): 2 models 216 + 37 of 298, PARTIAL (two models >= 10 %), 0 jumps. Global (`mqg_utrecht_fps5`): one model 298/298, 81,007 points, 0.517 px, but 43 time-normalised jumps: 35 consecutive 0.2 s steps at 0.40-7.21 s with factors 6.3-12.5 (start of the path stretched), 4 at 44.6-45.8 s (factors 42-215), 4 at 50.3-51.1 s (11-210), 1 at 52.3 s (5.9) -> PARTIAL. View `outputs/evidence/map_quality/views/utrecht_global_topview.jpg`.

**Hypothesis:** the camera follows a walking person who fills much of the frame; features on the person are not static scene and RANSAC alone may not reject them.

**Test run (not adopted):** `outputs/experiments/utrecht_global_masked/map_report.json` (`packages/mapping/mask_people_experiment.py`) — same 298 frames, YOLOX-nano person boxes padded 10 % and masked out of feature extraction (masked pixel share p50 20.6 %, max 62.8 %), global mapper, 200.2 s. Result: 298/298, 74,303 points, 0.552 px, jumps 43 -> 38, still PARTIAL. The 50.3-51.1 s spikes disappeared, the 44.6-45.6 s spike fell from 215x to 77x, the stretched start (0.4-7.2 s, factors 5.8-11.2) is unchanged, and one new jump appears at 0.0-0.2 s (56.5x). Reading: the walking person contributes to the mid-clip spikes (measured by removal); the stretched start has another cause, not identified. Masking is not adopted into the pipeline because the Sumida / FSC regression guard of plan C was not run with it.

**Next test:** run the masked variant on Sumida and FSC against the guard (150/150, points >= 0.9x, reproj <= 0.60 px on Sumida, 0 jumps); and declare a Utrecht sub-section starting after 7.2 s to test whether the stretched start is an initialisation effect.

## F-12 Dense coverage: ESVF passes the segment and FSC but misses the `f000900` floor clause on the full clip (2026-11-09)

**Status:** FAIL as recorded (plan `docs/PLAN_DENSE_COVERAGE.md` section 4: one clause missed = FAIL). Default dense layer stays M0. Cause measured, follow-up change E-N2 run (F-14).

**Expected (frozen, section 2):** floor `cov1px` +8 pp on each of `f000240`, `f000900`, `f001560`.

**Observed** (`outputs/evidence/dense_cov/stage_metrics_ESVF_full.json` vs `stage_metrics_M0_full.json`): floor `cov1px` 0.0244 -> 0.1266 (+10.2 pp), **0.0024 -> 0.0096 (+0.7 pp)**, 0.2764 -> 0.5862 (+31.0 pp); every other criterion met (`inconsist` 0.151 -> 0.1154, `float_knn` 0.138 -> 0.122, `float_fs` 0.485 -> 0.352, structure +1.0 / −0.5 / +10.8 pp, sparse agreement min 0.711, 113/117 views, 100.2 s). On the t 4–14 s segment ESVF had passed every criterion (floor +9.6 pp, `stage_metrics_ESVF_seg.json`); on FSC it passes the regression clause (145/145, `inconsist` 0.0443, `float_fs` 0.2385, 151.6 s, `stage_metrics_ESVF_fsc.json`).

**Cause** (diagnostic run `outputs/experiments/dense_cov/diag30_ESVF/`, `f000880` with the `f000900` polygon as proxy; plan A4 item 3): reference views at t ≈ 23–35 s have only 2 neighbours, both ±6 frames away, with baseline / median co-visible depth 0.051–0.058 — the lower edge of the [0.05, 0.30] window. The open park makes the median depth large, so frames close enough in time to give parallax on the near floor never qualify as neighbours. `f000880` floor `conf` 0.0125 with `cost` 98.5 % of rejections; `boundary_near` 0; texture and margin not dominant. Neighbour selection, not depth range, texture or plane orientation. A second, independent limit: the 0.5 x voxel ceiling on `f000900` is 0.084 (`fusion_ceiling.json`), while +8 pp needs 0.0824, i.e. 98 % of the ceiling (F-15).

**What followed:** E-N (F-13) and E-N2 (F-14), each a single pre-declared neighbour-rule change stacked on ESVF.

## F-13 Dense coverage: E-N (`median_or_near`) regresses structure on t 4–14 s and raises free-space violations on t 26–34 s (2026-11-09)

**Status:** FAIL on both segments; superseded by E-N2 (F-14). Plan A4 item 4 / A5 item 3.

**Observed** (`outputs/evidence/dense_cov/stage_metrics_ESVFN_seg.json`, `stage_metrics_ESVFN_seg30.json`): on t 4–14 s structure `cov1px` 0.621 (M0) -> 0.500 (−12 pp, clause −2 pp), views verified 26 -> 24, points 313,831 (ESVF) -> 188,849, although floor `cov1px` reached 0.119 and `inconsist` fell to 0.0547. On t 26–34 s reference views 6 -> 21, floor 0.0002 -> 0.0664, structure 0.454 -> 0.636, `inconsist` 0.124 -> 0.119, but `float_fs` 0.151 -> 0.292 (+14 pp, tolerance +2 pp). The 0.25 x variant (ESVFN25) shows the same two failures (structure 0.5008; `float_fs` 0.2602).

**Cause** (A5 item 3): under E-N a candidate qualifies when either the median or the p10 co-visible depth gives baseline/depth in [0.05, 0.30], and candidates are taken in scan order (±1 first). The near-qualified close frames therefore displace the median-qualified ones that give far structure its parallax, and the sweep loses the distant trees and buildings.

**Fix tried (E-N2, F-14):** median-qualified neighbours are collected first (max 4); only when fewer than 4 exist do near-qualified candidates fill the remaining slots. A view with 4 median-qualified neighbours is unchanged by construction, which is why ESVFN2_seg equals ESVF_seg on t 4–14 s (floor 0.1182, structure 0.651 vs 0.653).

## F-14 Dense coverage: ESVFN2x25 fails the pre-registered screening on `float_fs` while meeting every full-clip and FSC criterion; recorded NOT_ADOPTED (2026-11-09)

**Status:** NOT_ADOPTED (plan section 4). Default dense layer stays M0; the variant is installed as a labelled experimental layer only (`outputs/maps/mqg_{sumida_0025_0125,fsc_campus_aerial}/dense_experimental/`, label `DENSE_EXPERIMENTAL (relative scale) - not adopted`). Adoption is a user decision.

**Screening run** (A5 item 5 order: segment t 26–34 s first; `outputs/evidence/dense_cov/stage_metrics_ESVFN2x25_seg30.json` vs `stage_metrics_M0_seg30.json`): floor `cov1px` 0.0002 -> 0.1105 (+11.0 pp, passes), structure 0.454 -> 0.663, `inconsist` 0.124 -> 0.1317 (+0.8 pp, passes), `float_knn` 0.200 -> 0.167, but **`float_fs` 0.1508 -> 0.2634 (+11.3 pp, tolerance +2 pp) -> FAIL**. The 0.5 x variant ESVFN2 also fails there, on the floor clause (0.0563, +5.6 pp) and on `float_fs` (0.2916).

**View-set bias in the screening metric** (stated before the run in A5 item 4, not after): `float_fs` counts a violation when any view with a depth map sees past a point. E-N2 raises the reference views on this segment from 6 to 21, so the "before" cloud is checked against 6 depth maps and the "after" cloud against 21 — the after cloud is tested harder by construction. The supplementary `float_fs_same_maps` (both clouds against the after run's 21 maps) reads 0.3586 -> 0.2634, i.e. down. The plan keeps `float_fs` as defined for acceptance and forbids changing a criterion after seeing the result, so the screening FAIL stands.

**Full clip and FSC, run and reported for the record** (`stage_metrics_ESVFN2x25_full.json`, `stage_metrics_ESVFN2x25_fsc.json`): Sumida floor `cov1px` 0.0244 / 0.0024 / 0.2764 -> 0.2789 / 0.119 / 0.6714 (+25.4 / +11.7 / +39.5 pp; mean 0.101 -> 0.356, x 3.5), `fused_n_src` floor 491 -> 7,701 and 794 -> 6,574, `fused_n_proj` on `f000900` 162 -> 12,532; structure 0.8766 / 0.8773 / 0.6644 (all up); `inconsist` 0.151 -> 0.1165 (<= 0.15); `float_knn` 0.138 -> 0.096; `float_fs` 0.485 -> 0.369 (same maps 0.519 -> 0.369); sparse agreement min 0.749 (>= 0.70), median 0.9265 -> 0.932; 147 reference views, 141 verified; 917,498 -> 2,087,132 points; 135.1 s (<= 176). FSC: 149/149 views (>= 143), 6,520,886 points (>= 0.9 x 1,942,876), `inconsist` 0.0638 -> 0.0444, `float_knn` 0.1833 -> 0.0546, `float_fs` 0.397 -> 0.182, 155.1 s (<= 230.4). Every section 2 criterion is met on these inputs.

**Why it is still not the default:** the pre-registered rule (A5 item 5) made the seg30 screening a gate, and a gate that is redefined after it fails is no gate. The result is therefore NOT_ADOPTED with both tables on disk, and the user can decide with the numbers in front of them.

**Observed, not measured:** `outputs/evidence/dense_cov/sameview_f000240_ESVFN2x25_full.jpg` shows a thin vertical streak of points near the right edge of the floor region in the experimental cloud. No metric in the plan covers it.

**Next test (if the user wants to pursue adoption):** a new dated plan section that (a) defines a same-view-set free-space metric as the acceptance metric with a fresh M0 baseline table computed under that definition, and (b) adds a criterion for the streak artefact (e.g. column-wise point density outliers on the floor region). Until then the layer stays experimental.

## F-15 Dense coverage: the frozen `cov1px` criterion depends on the fusion voxel footprint, not only on reconstruction (2026-11-09)

**Status:** recorded; the criterion was not changed (plan A3 item 2, A5 item 2). This is a property of the measurement, not a code defect.

**Observed** (`outputs/evidence/dense_cov/fusion_ceiling.json`; method: floor pixels intersected with the fitted ground plane, one voxel's foreshortened footprint, ceiling = mean min(1, 1/(sx·sy))): with the baseline voxel 0.0302 and the fitted ground plane of `f000240` (camera height 0.254, floor depth p10/p50/p90 0.42 / 0.62 / 1.19) one voxel projects to a median 16.4 x 5.7 px on the floor at 640 px. If every floor voxel were filled, `cov1px(floor)` could not exceed **0.0262** on `f000240`, **0.0211** on `f000900`, 0.5916 on `f001560`; at 0.5 x voxel 0.1047 / 0.0844 / 0.9839; at 0.25 x 0.328 / 0.2924 / 1.0. The +8 pp clause therefore cannot be met by any depth-estimation or verification change while the voxel stays at 1 x on the two near-floor frames, and on `f000900` it needs 98 % of the 0.5 x ceiling. E-S demonstrates the gap directly: floor `verif` 0.0117 -> 0.0897 and raw verified floor points about 9,440, yet `cov1px` only 0.0225 -> 0.0337 because those points merge into 1,561 voxels (`stage_metrics_ES_seg.json`).

**Consequence in the plan:** E-F (finer voxel for near points only) was triggered by E-S (`fused_n` up, `cov1px` flat) and is what lets ESVF / ESVFN2x25 reach the clause; the pre-fusion verified share S3 `verif` is reported beside `cov1px` as the reconstruction measure (A3 item 3). Related caveat (A3 item 4): `float_knn` uses an absolute radius of 2 base voxels, so a finer near voxel raises neighbour counts by construction; `float_fs` and `inconsist` are the density-neutral error measures.

**Reading:** `cov1px` measures what the viewer shows at 1 px, which is what the plan set out to improve, but a future plan that wants to score reconstruction alone should use S3 `verif` or a voxel-normalised coverage as the acceptance measure, with a new baseline.

## F-16 Static map: sky/person masks erode whole matching windows and remove structure at the sky edge (2026-11-09)

**Status:** cause measured; matching rule replaced (S2, `--mask-cost valid_pixels`); residual loss attributed to the segmentation labels, not fixed. Plan `docs/PLAN_STATIC_MAP.md` (finding 2, A1 items 4 and 7).

**Observed** (`outputs/evidence/static_map/summary.json`, run B2 = `dense_static/`, byte-identically reproduced as `B2_full`): the first static trial applied the sky ∪ person keep-mask (3x3 dilation at 384 px) by eroding the 7x7 ZNCC window in the reference and in every warped neighbour, so one masked pixel in the window dropped the pixel. Against B1 (same config without masks) the structure band within 20 px of the sky fell from 0.9476 / 0.9061 / 0.7859 / 0.9069 / 0.738 to 0.4622 / 0.6599 / 0.2828 / 0.3816 / 0.376 on f000240 / f000900 / f001560 / f000600 / f001260; static-pixel structure −8.7 / −5.3 / −11.3 / −7.2 / −11.9 pp; wall −18.9 / −14.6 pp. Mask edge loss about 4 px (dilation) + 3 px (window radius) at 640 px. The reported `f000900` structure drop 0.877 → 0.742 is in the whole-region (`cov1px_region`) definition.

**Fix tried (S2):** ZNCC over valid (unmasked, in-image) window pixels only, per neighbour ≥ 60 % usable window, reference centre pixel must be unmasked. Segment band 0.2921 → 0.5029 and 0.3489 → 0.5543; with undilated masks (S2u) 0.5983 / 0.6343; still below B1 − 3 pp (0.745 / 0.762). Cost: 41.4 s vs B1 25.4 s on the 4–14 s segment (`X_valid_seg`, the same rule without masks, 39.7 s: the time is the rule, not the masks).

**Residual cause:** the segmentation labels bare branches against the sky as sky (f000900 left tree, f001560, f000600), and S2 requires a static centre pixel, so those branches never get depth. S3r restores sky-labelled pixels darker than the local sky mean (61x61 window, 30 grey levels) as candidates: band 0.6004 / 0.6919 → 0.6337 / 0.7189 (S2n → S3r); with the valid-window share lowered to 0.40 (S3r40) 0.669 / 0.7453. Full clip S3r40: band 0.8394 / 0.8811 / 0.6282 / 0.6702 / 0.6486 — most of the loss recovered, B1 not reached (−10.8 / −2.5 / −15.8 / −23.7 / −8.9 pp). Under the frozen labels the restored branch points count as `sky_false` (1.4–6.8 % vs B2 0.1–3.3 %), although the colour check `sky_fringe.json` shows they are not sky-coloured (0.05–0.55 % vs B1 0.16–2.92 %).

**Next test:** a better sky/vegetation label at the edge (a different segmentation model or a per-frame refinement of the branch class with its own evaluation on the mask audit frames) measured on the same five frames; the matching rule does not need to change for that test.

## F-17 Static map: mask-filtered sparse points changed neighbour selection and verifier sets (2026-11-09)

**Status:** cause found and fixed (`--neighbours-all-sparse`, S2n); recorded in A1 item 5.

**Observed:** B2 filtered the sparse SfM points by the keep-mask before computing the baseline / median-depth ratio that selects neighbour views and verifier sets. In the 4–14 s segment this changed the neighbour lists of 15 of 26 reference views and dropped ground-plane candidates in some views (f000190: 23,887 → 8,495 candidates). Sparse points on sky-labelled pixels are triangulated 3D points (mostly branches) and remain valid geometry for choosing views.

**Fix:** neighbour lists and verifier sets from all sparse points; the keep-mask applies to matching only. Segment (S2u → S2n): floor 0.2107 → 0.2295, structure 0.6759 → 0.665, band 0.5983 → 0.6004, views verified 25 → 26, streak columns behind the floor 0.3686 (control 0.2335) → 0.1774 (control 0.1298). On the t 26–34 s segment floor fell 0.1328 → 0.1043 while structure rose 0.5728 → 0.6306 and band 0.6343 → 0.6919; the plan's clause 2 counts the floor drop, the band gain made S2n the base for S3r.

## F-18 Static map: floor streak from the last 4 image columns of reference views f000720–f000800 (2026-11-09)

**Status:** cause measured; fixed by border exclusion + all-sparse neighbour choice (S3r40 full clip streak at control level). Plan finding 1, section 5 S1, A1 item 3.

**Observed** (B1 = `dense_experimental/`): in f000240 the points in columns 576–590 (640 px) lie 88.45 % behind the fitted floor against 29.54 % in the control columns (`streak_cols` 1,680 points). They come from reference views f000720–f000800 at source columns u = 636–639, one constant depth per column: a border-window artefact of the depth-candidate stage (box filter with `BORDER_REFLECT`, warped neighbour border) that passes the multi-view checks. 55,331 of 4,843,796 B1 raw points (1.14 %) lie within 3 px of the image border. The same streak is the "thin vertical streak" observed without a metric in F-14.

**Fix:** a pixel whose 7x7 window leaves the image in the reference or any warped neighbour is invalid at the candidate stage (`--border-exclusion`). On B2's erode mode this is a no-op (raw border share already 0.0), so it has no row of its own and is paired with S2. Full clip S3r40: raw border share 0.0, streak columns 0.1708 behind the floor vs control 0.1911 (B2 0.373 vs 0.245; B1 0.8845 vs 0.2954). Segment runs cannot show the fix because the source views f000720–f000800 lie outside t 4–14 s.

## F-19 Static map: the frozen common judge is biased toward B1 (2026-11-09)

**Status:** found before any adoption; neutral judge added (A1 item 6); clause 5 evaluated on it. The B1-judge column stays reported.

**Observed:** section 3 froze B1's own depth maps (`outputs/experiments/dense_cov/ESVFN2x25_full/diag/depth_maps.npz`) as the common judge for `float_fs` and `inconsist`. B1's points were verified against exactly those maps, so B1's free-space violation is low by construction. Measured `float_fs` B1-judge vs neutral (B0 maps, `M0_sumida_full/diag/depth_maps.npz`, used by no candidate's verification): B1 0.3689 vs 0.3384; B2 0.3879 vs 0.3554; S3r40 0.3904 vs 0.3551; B0 0.519 vs 0.4846 (B0's neutral value is against its own maps and is biased the other way). The ordering B1 < S3r40 ≈ B2 holds on both judges, so the verdict on clause 5 does not change; the gap narrows from 2.2 to 1.7 pp.

**Rule kept:** metrics obtained with other maps keep their original verdict and are marked `judge=historical`; the S2 / S2u segment runs have no neutral value because the neutral judge was added after them (`float_fs_neutral: null` in `summary.json`).

## F-20 Static map: S3r40 improves on the static trial but fails the frozen comparison with B1; installed as a trial, NOT_ADOPTED (2026-11-09)

**Status:** FAIL under `docs/PLAN_STATIC_MAP.md` section 4 (one clause missed = FAIL). Default dense layer unchanged (M0). S3r40 installed as `dense_static_v2/` on Sumida and FSC with label `DENSE_STATIC_TRIAL_V2 (relative scale) - not adopted`, `experimental.status NOT_ADOPTED`. Adoption is a user decision.

**Configuration:** B1 + refined undilated masks (`--dilate 1 --refine-sky 30`), `--mask-cost valid_pixels --min-valid-frac 0.4 --border-exclusion --neighbours-all-sparse`. The 0.4 valid-window share has no dated plan section of its own (deviation recorded in the plan's Result).

**Full clip vs B1 (`summary.json`, `static_metrics_S3r40_full.json`):** structure 0.8964 / 0.866 / 0.6514 / 0.8235 / 0.5513 vs 0.927 / 0.8694 / 0.6913 / 0.8373 / 0.6183 (−3.1 / −0.3 / −4.0 / −1.4 / −6.7 pp; clause 1 allows −2); wall 0.808 / 0.1305 vs 0.9244 / 0.2238 (−11.6 / −9.3); band −10.8 / −2.5 / −15.8 / −23.7 / −8.9 pp (allows −3); floor 0.2369 vs 0.2789 on f000240 (−4.2, allows −1) and 0.1205 on f000900 below B2's 0.140; `sky_false` 1.44 / 4.41 / 3.94 / 1.48 / 6.77 % (limit 0.5 %; all below B1's 13–24 %); person front 3 on f001260 (B1 0); `float_fs` neutral 0.3551 vs 0.3384; `inconsist` 0.1099, `float_knn` 0.1024, sparse min 0.725 within tolerance; border share 0.0 and streak 0.1708 vs control 0.1911 PASS; 221.3 s vs 203 s budget; FSC 149 / 149 views, `float_fs` own 0.1777 vs 0.1817, `inconsist` 0.0435 vs 0.0444, 244.7 s vs 232.7 s budget.

**Full clip vs B2 (what it does deliver):** band +37.7 / +22.1 / +34.5 / +28.9 / +27.3 pp, structure +5.6 / +4.9 / +7.3 / +5.8 / +5.2 pp, wall +7.3 / +5.2 pp, floor +2.7 / −2.0 / +2.3 / +0.1 / +1.0 pp, streak removed, `float_fs` neutral 0.3554 → 0.3551, `inconsist` 0.108 → 0.1099, `float_knn` 0.1066 → 0.1024, 141 → 143 views, 1,324,919 → 1,530,092 points, 150.3 → 221.3 s.

**Order deviation:** S3r40 failed both segments on the band clause and was still run on the full clip and FSC so that the comparison exists on disk; the segment verdicts stand and the full-clip rows are FAIL, not a passing change.

**Next test:** nothing in the matching rule is left to tune against the frozen clauses without a time increase; the open items are the segmentation edge (F-16), the 3 `person_front` points on f001260, and the time budget (a 1.5x B1 budget is not met by any valid-pixel variant). Any change to the clauses needs a new dated section with a new baseline table.

## F-21 OpenMVS comparison: `DensifyPointCloud` segfaults at the default resolution on this Mac (2026-11-09)

**Status:** BLOCKED at the default resolution level (800 px); the 640 px runs completed. OpenMVS (AGPL-3.0) is run only as a separate command-line process from its official v2.4.0 macOS arm64 release build; nothing is linked, redistributed or committed.

**Observed** (`outputs/experiments/static_map/openmvs_default_res_segfault.log`): "OpenMVS x64 v2.4.0, Build date: Jan 19 2026", Apple M6 12 cores, 16 GB; command `DensifyPointCloud -i scene.mvs -o OMVSd_seg.mvs --max-threads 0`; 26 images (26 calibrated, 35.31 MPixels, 1.36 MPixels/image), 8,985 points, ROI estimated, neighbour views selected; the log ends at "Geometric-consistent estimated depth-maps 1 (3.85%, 193ms, ETA 4s)...". `summary.json` records the exit as signal −11. No further attempt at that resolution is recorded.

**What ran instead:** 640 px working resolution on the same 26-view workspace, unmasked (`OMVS_seg`: 98,941 points, 8.0 s) and masked (`OMVSm_seg`: 99,599 points, 7.0 s), and the full clip at 640 px masked (`OMVSm_full`: 141,563 points, 81.6 s). Segment floor 0.3848 / 0.3873 vs B1 0.252 with no floor point behind the fitted plane and `float_fs` neutral 0.2347 vs 0.3744, structure 0.1184 / 0.1275 vs 0.6885; full-clip near floor 0.0177 / 0.0042 / 0.0064 on f000240 / f000900 / f000600 (cause not investigated).

**Next test:** the same `scene.mvs` at the default resolution with `--max-threads 1` or a memory-bounded `--sub-scene-area`, to separate a memory fault from a threading fault; and `--resolution-level 1` with `--max-resolution 800` explicitly, to confirm which setting triggers the crash.

## F-22 Frame selection F1 (parallax / flow keyframes only) keeps too few frames for dense neighbours (2026-11-09)

**Status:** rejected before SfM; replaced by F1b (a departure from `docs/PLAN_FRAMES_AND_DEPTH.md` section 4 and from its F0 fallback, recorded in the plan's Result). No map was built from F1.

**Observed** (`outputs/experiments/frames_depth/F1_selection_sumida/selection.json`): Sumida, 600 candidates at 10 fps, thresholds overlap 0.6 / parallax 8 px / flow 40 px / slow 3 px, `adaptive_base none`: 92 frames kept — flow 40, connector 22, slow_parallax 13, parallax 9, forced 7, first 1; 0 dropped by the cap. Against the 150 of the fixed 3 fps baseline this leaves too few frames for the dense stage's neighbour rule (which needs several views per reference within a small baseline / depth window).

**Cause:** forward motion over a near-planar floor makes the rotation-compensated parallax (median residual flow after a homography fit) small — the homography absorbs most of the floor's motion — so the parallax rule rarely triggers and frames are kept mostly by the flow and slow rules.

**What followed:** F1b (`adaptive_base fixed`): the fixed 3 fps frames are always kept, the uniform `linspace` cap is dropped, and connector / flow frames are added where the tracker asks for them. F1b runs: Sumida 190, FSC 213, Utrecht 343 frames (F-23).

## F-23 F1b improves the camera path on every clip but raises dense error measures; FAIL as a dense default (2026-11-09)

**Status:** path clause PASS on Sumida, FSC and Utrecht; dense clause 4 FAIL on Sumida and FSC. Default selection stays `fixed`; `--selection adaptive` is opt-in. F1b's dense clouds are installed only as `dense_static_v3/` trials (`mqf_sumida_F1b` = F1b + D12, `mqf_fsc_F1b` = F1b).

**Path, observed** (`outputs/maps/mqf_{sumida,fsc,utrecht}_F1b/map_report.json`, `summary.json` `paths`): Sumida max internal gap 0.67 → 0.33 s, reprojection 0.512 → 0.46 px, 0 jumps, 150 → 190 frames all registered; FSC 0.67 → 0.33 s, 0.708 → 0.685 px, 0 jumps, 150 → 213; Utrecht jumps 43 → 19 and first-10-s jumps 34 → 0, 298 → 343 frames, still PARTIAL with the 19 jumps at 49.0–51.9 s (one 337x step at 51.65–51.85 s) and two unusable spans recorded as time + cause at 45.2–45.3 s and 45.8–45.9 s (tracked share 0.56 / 0.49, no usable frame between).

**Dense, observed** (`static_metrics_F1b_mqf_sumida_F1b.json`, `fs_rates.json`): floor `cov1px` up on all seven Sumida eval frames (0.2369 / 0.1205 / 0.6816 / 0.145 / 0.4522 / 0.0536 / 0.228 → 0.3069 / 0.2156 / 0.8201 / 0.2111 / 0.5223 / 0.0613 / 0.3258) and structure up on six, but own-map free-space violations 0.3627 → 0.4538, per-check violation rate 6.67 → 8.8 %, points violated in the majority of their checking views 1.2 → 3.3 %, `float_knn` 0.1024 → 0.1364, `sky_false` on f001560 0.03944 → 0.04548 (+0.60 pp, limit +0.5). The fast frame f001080 gains only +0.8 pp floor (clause 1 asked +3 pp on the fast frames). FSC: far class −1.1 / −1.2 pp on f000320 / f001720, own free-space 0.1777 → 0.1896.

**Reading (not proven):** denser frames give the sweep more references and more verifiers; more points are placed, and more of them are seen past by some other view. Because the F1b poses differ from V2's, only own-map judges exist (`neutral_fs: null`), and own-map free-space grows with the number of checking views (the view-set bias already recorded in F-14). Whether the rise is "more wrong points" or "more views checking" is not separated.

**Next test:** check the F1b cloud against V2's 145 depth maps and the V2 cloud against F1b's 169 maps (same-view-set comparison), and time F1b alone (331.0 s under parallel load; budget 332 s).

## F-24 D1 / D12 raise floor and structure on fixed poses but fail the frozen error clauses as recorded; judge and rounding rulings pending (2026-11-09)

**Status:** FAIL as recorded in `summary.json` `verdicts`; D12 installed as `outputs/maps/mqg_sumida_0025_0125/dense_static_v3/` (trial, NOT ADOPTED). Two points need a ruling before the FAIL is treated as final.

**Observed** (`static_metrics_{D1,D12}_full.json` vs `static_metrics_V2_full.json`): D1 (`neighbour_rule median_then_near_far`: after the median and p10 passes, neighbours within ±12 frames whose baseline / p90 co-visible depth lies in [0.05, 0.30]) raises structure +2.0 … +5.3 pp on all seven frames and floor +0.5 … +1.6 pp; the fast segment t 33–39 s goes from 4 to 9 verified views of 16 and 5,352 to 25,043 points. D12 (D1 + `--coarse-ground`, half-resolution sweep of the ground rows below the horizon) raises floor +1.2 … +4.3 pp and structure +1.9 … +5.2 pp, `inconsist` 0.1074 → 0.1045. Errors: own-map free-space 0.3627 → 0.3866 (D1) / 0.3863 (D12), +2.4 pp; `sky_false` on f001560 0.03944 → 0.04509 (D1, +0.57 pp) / 0.04363 (D12, +0.42 pp); person points on f001680 612 + 265 (D1) / 615 + 273 (D12) vs V2 579 + 271. FSC D1: about no change.

**Ruling 1 — judge.** Plan section 2 assigns the neutral depth maps (`M0_sumida_full/diag/depth_maps.npz`) to `float_fs` / `inconsist` of D runs, because own maps are biased by the run's own verification. The recorded verdict cites own-map free-space (+2.4 pp). Neutral values in `summary.json`: V2 0.3551 → D1 0.3582 (+0.3 pp) → D12 0.3563 (+0.1 pp), within clause 4's +1 pp.

**Ruling 2 — D12 sky.** The installed layer's `reason` says "sky points +0.5 pp on f001560"; `summary.json` and `static_metrics_D12_full.json` give 0.04363 − 0.03944 = +0.42 pp, inside the +0.5 pp limit. D1's +0.57 pp is outside it either way.

**What this does not change:** no default was altered; the person count on f001680 is above V2 for both. Any re-statement of the verdict must be a dated amendment to the plan, not an edit of the clauses.

**Ruling (2026-11-09, recorded in `summary.json` `verdicts` and the plan's Result):** plan section 2 governs, D runs are judged on the neutral maps. **D12**: free-space neutral 0.3551 → 0.3563 (+0.1 pp) PASS, `inconsist` 0.1099 → 0.1047 PASS, `sky_false` max +0.42 pp (f001560) PASS, floor and structure ≥ V2 on all 7 frames PASS, fast-frame structure +5.2 / +3.6 pp PASS; **FAIL** on (a) time — 400.0 s measured with no other dense job running (`outputs/experiments/frames_depth/D12_full_timing.log`, output byte-identical to `D12_full`) against the 332 s limit — and (b) person front + surface on f001680 850 → 888. Not adopted. **D1**: free-space neutral +0.3 pp PASS; **FAIL** sky f001560 +0.57 pp, person f001680 850 → 877; time not measured quietly. The "+0.5 pp sky" wording in the installed D12 layer's reason was wrong (+0.42 pp) and has been corrected in `outputs/maps/mqg_sumida_0025_0125/dense_static_v3/dense_report.json`; the UI smoke caption archived at 14:15 KST still shows the old wording until the next run.

**Next test:** find which reference views place the 38 extra person-box points on f001680 (surface 615 + front 273 vs V2 579 + 271) — far-neighbour rule or coarse ground pass — and time D1 alone.

## F-25 Time clause unmeasurable: all full-clip runs timed under parallel load (2026-11-09)

**Status:** UNMEASURED_QUIET for every full-clip run of the FD plan except D12, which was re-timed alone: **400.0 s** (`outputs/experiments/frames_depth/D12_full_timing.log`, output byte-identical to `D12_full`) → clause 5 **FAIL** (> 332 s). Not a code defect.

**Observed** (`summary.json` `verdicts.time`, `runs.*.seconds`): the user asked for parallel work, so D1_full (382.2 s), D12_full (508.2 s), F1b Sumida dense (331.0 s), F1bD12_full (597.9 s), D1_fsc (481.0 s) and F1b FSC dense (577.8 s) ran while other jobs were running, as did the three F1b builds (390.4 / 548.4 / 531.4 s including 102.7 / 171.0 / 115.9 s of adaptive selection). Budgets were ≤ 332 s (Sumida) and ≤ 367 s (FSC). The numbers are reported in every table but no PASS / FAIL is attached to clause 5.

**Next test:** re-run `V2_full` and the remaining runs alone on an idle machine with the archived flags (`config` block of each `dense_report.json`); D12 is settled (400.0 s vs 1.5 x 221.3 = 332 s, the 221.3 s baseline itself measured under load).

## F-26 Learned multi-view depth: first BLOCKED, then approved, run and judged (2026-11-09)

**Status:** closed as a blocker. Was BLOCKED (installation refused by the session's permission check); the user approved; the model (LingBot-Map, Apache-2.0) was installed as an external tool in a separate environment under `outputs/tools/` (not committed) and run under `docs/PLAN_LEARNED_DEPTH.md` with Amendment A1. The outcomes of that round are F-27 to F-32 below; the per-clause verdicts are in the plan's Result. "Mirage" family researched separately: no-go.

**Observed:** the route most likely to give a filled, dense look is a learned multi-view depth model run on the existing COLMAP poses. Installing external model code and weights was refused by the session's permission check (`summary.json` `verdicts."learned multi-view depth"`); `import torch` fails in the project `.venv` (`research_mirage.md`). The user has stated that the project is personal and non-commercial, so weights under a non-commercial licence would be acceptable licence-wise; this is recorded, not acted on.

**"Mirage" candidates** (`outputs/evidence/frames_depth/research_mirage.md`): the reconstruction method of that name needs LiDAR + IMU input and presumably CUDA; the monocular-depth benchmark of that name is MIT code over non-commercial weights and dataset, needs torch and an A100-class GPU to fine-tune, and addresses single-image depth that this pipeline does not use; the others are generative, CUDA-only, hosted, or unrelated. Recommendation in the note: no-go for integration; one zero-dependency idea kept (gate any future floor depth prior with a planarity check and keep it labelled as model-guessed).

**Next step:** once installed, run it on the existing COLMAP poses and judge with the same seven Sumida eval frames; the result would be an `ALIGNED_PREVIEW`-class layer, never metric, and never merged with the real-video MVS points.

## F-27 Learned depth: the unchunked full-clip run was stopped by the session for low memory (2026-11-09)

**Status:** BLOCKED for that configuration; replaced by the resume protocol of Amendment A1 (`packages/learned/chunked_run.py`), under which every planned chunk completed. Plan `docs/PLAN_LEARNED_DEPTH.md` A1 and Result R2.

**Observed** (`outputs/experiments/learned_depth/lingbot_v2_full.log`): the 150-frame Sumida run in one process was stopped by the session for low memory on the 16 GB Mac. Nothing from it is used.

**What was done instead:** one chunk per separate process (8 s with 2 s overlap; the verified 0–10 s chunk reused after re-checking frame list, weight hash, output files and alignment residual), no chunk start below 35 % free memory, 4 GB checkpoint released after loading, model and MPS cache released after inference, memory logged per stage. Measured over 22 chunks (`chunks_{sumida,fsc}.json`): peak process RSS **9.02 GB** (FSC 36–44), MPS driver peak **12.56 GB** (Sumida) / 11.49 GB (FSC); free memory 62–76 % at start, 4–24 % at `inference_done`, 73–77 % after the MPS release; swap 9–15 GB from other processes throughout. fp16/bf16 (A1 "Precision") never triggered.

**Next test:** none needed for completion; if a longer chunk is ever wanted, the A1 precision clause applies (fp16 on the 4–14 s segment, continued only if peak memory falls and the coverage / error clauses still pass).

## F-28 Learned depth: the two fast-turn chunks of Sumida (50–58 s, 56–60 s) rejected by the alignment residual (2026-11-09)

**Status:** FAIL (REJECTED by `chunked_run.verify`), not joined; the Sumida learned layer has no learned depth in 50–60 s, and the eval frames f001560 (52 s) and f001680 (56 s) are unchanged by it.

**Observed** (`chunks_sumida.json`): chunk 50–58 s (21 frames) aligned to our path with an RMS residual of **0.0612** of path length (limit 0.02), Sim3 scale 4.20; chunk 56–60 s (10 frames) **0.0503**, scale 14.61. Every other Sumida chunk lies at 0.0046–0.0137. Both chunks cover the fast turn at the end of the clip, the same stretch where the plane-sweep baselines struggle (F-24 fast frames).

**Reading (not proven):** the model's own camera path disagrees with ours where the camera turns nearly in place with little translation; a single Sim3 cannot bring the chunk's cameras onto ours, so the depth scale would be unreliable and the chunk is refused by rule rather than fused.

**Next test:** a shorter chunk anchored inside the turn (for example 4 s with 2 s overlap) judged by the same ≤ 0.02 residual; and a chunk that starts before the turn so that the Sim3 is dominated by translating frames. Neither has been run.

## F-29 Learned depth: neighbouring chunks disagree on overlapping frames or on scale against triangulated points; held, not fused (2026-11-09)

**Status:** by design (A1 "Chunk joining"); recorded because it leaves spans without learned depth. Sumida: 5 of 8 DONE chunks held; FSC: 6 of 12 held.

**Observed** (`summary.json` `boundary`, `lingbot_v2_*_merged/run_report.json`): held by **overlap disagreement** (median relative depth difference on the nearest 40 % of depths > 5 %): Sumida 8–16 s (7.4 %), 14–22 s (8.9 %), 44–52 s (8.7 %); FSC 24–32 s (5.2 %), 36–44 s (10.4 %), 42–50 s (5.5 %), 54–62 s (12.0 %), 66–71 s (5.4 %). Held by the **near-point scale check** against our triangulated points (> 5 % off): Sumida 26–34 s (**1.18**) and 32–40 s (**2.22**) although their overlap agreement was fine (1.4 % / 2.7 %); FSC 48–56 s (0.945). Result: learned depth exists only for Sumida 0–10, 20–28, 38–46 s (68 frames) and FSC 0–26, 30–38, 60–68 s (88 frames). The Sumida eval frames at 30 s and 36 s sit in the triangulation-held span; the +5 pp floor clause at 30 s therefore fails (F-32).

**Reading (not proven):** chunks are separate model runs with separate scales; where two runs see the near floor differently by more than 5 % a merged map would carry two shifted floor surfaces, which is the case the hold rule exists for. The large triangulation factors on 26–40 s coincide with the open-park stretch where our own sparse points are mostly far away, so the near-point check has few near points to use.

**Next test:** a chunk whose frames straddle the open-park stretch (so near triangulated points exist on both sides); report the number of near points used by the check per chunk (not currently in the index).

## F-30 Learned depth: rescaling chunks by the near-point factor made the fast-turn chunks worse; rejected (2026-11-09)

**Status:** rejected before any evaluation row; implementer-reported, no artifact of the rejected variant archived. The quantity survives only as the `near_point_scale_check` diagnostic and as the A1 hold rule (`HELD_SCALE_VS_TRIANGULATION`).

**Observed:** applying the near-triangulated-point scale factor (`scale_near_sparse_factor` in each aligned chunk's `run_report.json`; merged values 1.0113 Sumida, 0.9718 FSC) as a correction to the chunk depth was tried and made the fast-turn chunks worse; it was dropped, and the Sim3 scale from the camera centres stayed the only scale applied.

**Why it is recorded:** a correction that helps the good chunks and hurts the bad ones would have hidden the disagreement that F-28 / F-29 expose. Keeping the factor as a check rather than a correction leaves the hold decisions inspectable per chunk pair.

**Next test:** none planned; if revisited, it needs its own dated plan section with the fast-turn segment as the pre-registered screening run.

## F-31 Learned depth: far cleanup removes real structure on the FSC held-out frame; FAIL, not applied on FSC (2026-11-09)

**Status:** FAIL under the A1 far-cleanup clause (structure `cov1px` must not drop by more than 1 pp on any frame) on FSC; PASS on Sumida. The FSC learned layer is D1 + learned without far cleanup; the Sumida layer includes it.

**Observed** (`static_metrics_D1L_fsc.json` vs `static_metrics_D1LFC_fsc.json`): held-out f001720 structure 0.7502 → **0.7298 (−2.0 pp)** and floor 0.7901 → **0.7622 (−2.8 pp)**; tuning frames f000320 floor −0.3 pp / structure −0.1 pp, f001000 structure −0.1 pp; free-space 0.1691 → 0.1668; 8,234,561 → 8,192,708 points. Sumida (`D12L_full` → `D12LFC_full`): 26,634 of 768,214 far points removed, structure −0.17 … −0.98 pp on all 7 frames (within the −1 pp limit), floor unchanged, free-space 0.3295 → 0.3207.

**Reading (not proven):** the rule (points beyond 2 x the median camera distance need ≥ 2 agreeing plane-sweep maps and more agreement than free-space conflict) was set for a walking camera, where "far" is mostly background; on an aerial flyover, large parts of the real ground and buildings are "far" by that definition and are seen by few overlapping maps, so support < 2 removes them.

**Next test:** a variant with the far threshold tied to the per-clip depth distribution and a support threshold that counts available views, judged first on the held-out FSC frame; this is a new plan item, not a tuning of the frozen rule.

## F-32 Learned depth: the +5 pp floor clause missed on Sumida at 30 s and 52 s and on FSC f000320 (+4.1 pp); FAIL as recorded (2026-11-09)

**Status:** FAIL as recorded in `summary.json` `verdicts` on both clips (one clause missed = FAIL, plan section 4); both learned layers stay trials (`experimental.status "TRIAL, NOT ADOPTED"`); default unchanged. Adoption is a user decision after both clips pass.

**Observed, Sumida** (`static_metrics_D12_full.json` → `static_metrics_D12L_full.json`, 7 frames, neutral judge): floor 0.2625 → 0.4305 (+16.8 pp, 8 s), 0.1569 → 0.2752 (+11.8, 20 s, held-out), 0.4925 → 0.7153 (+22.3, 42 s, held-out), 0.0968 → 0.1031 (36 s), but **0.1531 → 0.1531 at 30 s and 0.7037 → 0.7038 at 52 s** — the two tuning frames inside spans with no accepted chunk (26–40 s held by the triangulation check, 50–60 s rejected; F-28, F-29). Every other clause improved or held: structure ≥ D12 on all 7, free-space neutral 0.3563 → 0.3295, isolated 0.1033 → 0.0882, sky ≤ D12 + 0.5 pp, person 888 = 888.

**Observed, FSC** (`static_metrics_D1_fsc.json` → `static_metrics_D1L_fsc.json`, 3 frames): floor 0.9426 → **0.9841 (+4.1 pp)** on tuning f000320 against the +5 pp clause, with the floor already at 0.94 before; f001000 +19.3 pp, held-out f001720 +4.3 pp; structure +15.0 / +2.0 / +6.1 pp; free-space 0.1982 → 0.1691.

**What this does not change:** the clause was frozen before the runs and is not re-stated after seeing the numbers. The Sumida misses are a consequence of empty spans rather than of wrong points (the frames are unchanged), and the FSC miss is a ceiling effect on a frame that was already 94 % covered; both facts are recorded beside the FAIL, not instead of it.

**Next test:** the chunk re-runs of F-28 / F-29 for the Sumida spans; for FSC, no further run changes the clause outcome on f000320 unless the clause itself is re-stated in a dated amendment with a new baseline table.

## Not failures, but open gates

- REAL_VIDEO route NOT_RUN: `assets/input/` is empty. Command in `docs/PROGRESS.md`.
- STALE_DATA banner in the web UI not yet asserted (NOT_RUN); the stale-frame STOP itself is verified in `outputs/evidence/m3/tracking_sim.json` (`stale_frame_stops: 5`, `nonzero_yaw_during_freeze_after_0.3s: 0`).
- (closed 2026-11-09) UI smoke re-run after F-06: 51 / 51; `scripts/test_all.sh` after the Skywarden rename: 94 passed (`outputs/evidence/tests/pytest-20261109-023923.log`).
- (closed 2026-11-09) E4 test archive: `tests/test_appearance.py` 3 passed inside `outputs/evidence/tests/pytest-20261109-024540.log` (97 passed) and every later log.
- (closed 2026-11-09) Live run / timeline replay: implemented and measured (`docs/PLAN_LIVE_RUN_TIMELINE.md` Result; `outputs/evidence/live/`); no acceptance test failed. Still open from it, not failures: a run with a physical webcam (NOT_RUN, no camera on this Mac; `webcam:0` correctly ends `FAILED NO_DEVICE` in 0.285 s), browser assertions for the live tab's `STALE_DATA` / `STALLED` / `FAILED` / `INCOMPLETE` / `CANCELLED` states (NOT_RUN; labels exist in `live.js`), and the observation that on the Utrecht person-following clip the target was LOCKED in only 155 of 899 frames (17 %) with 7 scripted re-selections after LOST — a tracker limitation on that clip (compare F-11), recorded in `t7_utrecht_realtime_30s.json`, not a live-run defect. Stored-map relocalisation: NOT_IMPLEMENTED (VO and live headers say so). Bangkok per-boundary cause classification (plan D1 field): not written.
- (2026-11-09, frame selection and depth) Adaptive selection stays opt-in (`--selection adaptive`), not a failure: path PASS on all three clips (F-23). Ruled: D runs on the neutral judge — D12 fails only quiet time (400.0 s) and person points on f001680, D1 fails sky and person (F-24). Open: clause 5 timing alone for every run but D12 (F-25); same-view-set free-space comparison for F1b (F-23); (closed 2026-11-09) learned multi-view depth approved, installed in a separate environment and run (F-26 → F-27–F-32). Closed: `data/models/static/SOURCE.md` now records the segmentation model (Realcat/skywater_seg rev ec20751, MIT, SHA-256).
- (2026-11-09, learned depth) Not failures: the model runs only as an external tool in `outputs/tools/venv_learned` and nothing of it is committed; `model_estimated/` is a separate, hidden cloud by construction. Open, not failures: the provenance-tag count of 0 inside the learned map layer (plan clause 4) and the time of our own alignment + fusion + dense part (clause 5) were not archived; fp16/bf16 NOT_RUN (never triggered); Sumida 10–20, 26–40, 46–60 s and FSC 26–30, 38–60, 68–71 s have no accepted learned depth (next tests in F-28, F-29); the FSC far-cleanup variant for aerial views (F-31) is a new plan item. Regression after the round: 134 tests (`outputs/evidence/tests/pytest-20261109-183645.log`), UI 88/88.

## F-33 Strict tracker locks on the neighbour after a long side-by-side walk (E2X, 2026-11-15)

**Status:** found in round 1 of `docs/PLAN_OCCLUSION_TRACKING.md`; present in the current default tracker (V0 = strict); fixed only in margin mode (adopted preset `VAB`). Evidence `outputs/evidence/occlusion_tracking/round1/e2x_V0/`, trace `round2_trace_side_by_side.txt`.

**Observed:** E2X `side_by_side` (B walks 40 px beside A for 3 s), 50 seeds: V0 target LOCKED on B in 115 frames (5 seeds), yaw toward B in the same 115 frames. The tracks stay AMBIGUOUS while both people are in each gate; when A's box drops out for one frame (5 % dropout), the coasting target track and B's box form a 1x1 component, which both modes trusted. Round-1 margin variants: 88 frames.

**Fix (margin mode, added after seeing round 1):** after an AMBIGUOUS spell a track re-connects only to a detection within 0.15 × box height of its prediction until its next trusted hit (`TrackerConfig.contested_reconnect_frac`). Round 2: 0 wrong-target frames in every margin variant; retention in this condition 0.10 → 0.94. Cost: without camera motion compensation the guard refuses more re-connections under panning (E2X `camera_pan_occ1.0` retention 0.94 → 0.22 for VA; 1.00 with VAB).

**Not done:** the strict mode (V0) was left unchanged on purpose (it is the reference). Anyone running `--tracker V0` keeps this weakness.

## F-34 Live target is PREDICTING most of the time on a close, large person (Utrecht, 2026-11-15)

**Status:** cause confirmed and fixed by `VABS`, **the app/CLI default since 2026-11-15** (`docs/PLAN_SCALE_AWARE_NOISE.md`): first verdict NOT_ADOPTED on a single-run live latency p95, then adopted under a live-speed protocol pre-registered before re-measuring (section 8). Evidence `outputs/evidence/occlusion_tracking/real_clips.json`, `outputs/runs/ot_live_utrecht_*`, `outputs/evidence/scale_aware_noise/`.

**Observed:** in every variant the target is LOCKED in only 16–19 % of the 899 frames and PREDICTING in 604–637. In those PREDICTING frames a detection was always present, its centre 0.10 / 0.17–0.19 / 0.29–0.33 × box height from the target (p25 / p50 / p75), with a median box height of 850–1,100 px.

**Cause:** the Kalman measurement noise is a fixed 6 px and the χ² gate is in pixels, so for a person filling the frame the normal jitter of the box centre (tens of pixels) falls outside the gate. Overlap handling cannot change this.

**Next test:** a measurement / process noise that scales with box height (as in common SORT-family filters), judged with the same E2 / E2X safety gate and MOT17 held-out split, under its own pre-registered plan.

**Confirmed (2026-11-15, `outputs/evidence/scale_aware_noise/diagnose_live_baseline.json`):** the 15 LOCKED → PREDICTING drops of the VAB run start with a detection only 0.027 × h (23 px, p50) from the prediction, squared Mahalanobis distance 11.3 (p50) against the 9.21 gate: with the position covariance converged to ~2.9 px and 6 px measurement noise the gate radius is ~20 px for an 850 px person. The 0.17–0.19 × h distances quoted above are the drift accumulated afterwards while coasting.

**Fix tried:** measurement noise σ = max(6 px, 0.025 × h) (`TrackerConfig.meas_noise_frac`; process-noise scaling was rejected in calibration because it raised E2 AMBIGUOUS frames by ≥ 22 %). Preset `VABS`. Utrecht live 30 s: target LOCKED 19.2 % → 69.1 %, drops to PREDICTING 15 → 4; MOT17 held-out (YOLOX-nano) IDF1 29.67 → 32.46, IDSW 364 → 213; E2 / E2X unchanged (bit-identical, boxes < 240 px). First verdict NOT_ADOPTED: live latency p95 34.08 ms > 33 ms in the single judged run (two informational repeats: 30.81 / 30.33 ms). Re-judgement (protocol fixed in commit `d73f2f8` before measuring; 5 + 5 alternating new runs): median latency p95 VABS 29.98 vs VAB 30.34 ms, tracker-step p95 2.92 vs 3.10 ms, target LOCKED median 0.682 vs 0.195 → adopted (`verdict_speed.json`). Remaining: ~30 % of Utrecht frames still not LOCKED, mostly with the box bottom clipped by the frame edge.

## F-35 Current default locks on the wrong person when people fill the frame (E2X-L, 2026-11-15)

**Status:** found while checking `docs/PLAN_SCALE_AWARE_NOISE.md` (supplementary safety check added after the plan was fixed); present in `VAB`, absent in `VABS`. **Fixed for the app and CLIs since `VABS` became the default** (2026-11-15 re-judgement); still present with `--tracker VAB` or `--fixed-noise`. The guard gap below stays open. Evidence `outputs/evidence/scale_aware_noise/e2xL_VAB/`, `e2xL_VABS/`, `verdict.json` (`per_condition_E2XL`).

**Observed:** the E2X scenes scaled 4.5× (people 675–810 px, 2880×2160, position noise and speeds scaled too, metrics in the original coordinates), 14 conditions × 50 seeds: VAB target LOCKED on the other person in 46 frames (same_row occlusion 0.5 / 1.0 / 1.5 s, camera pan, the X5 variants), yaw toward B in 15 frames; retention 0.871, IDSW 994. The same scenes at 1× give 0 for VAB. Same mechanism as F-34: the fixed 6 px gate drops normal detections, the target coasts, and the coasting track meets the other person.

**With VABS:** 0 wrong-target frames, 0 yaw toward B, retention 1.000, IDSW 155.

**Also seen (hand-made probe, informational, `guard_gap_probe.json`):** with VABS's larger gate, a target coming out of an AMBIGUOUS spell re-connected to a neighbour 0.12 × h away (inside the F-33 guard's 0.15 × h) in 32 of 110 frames when the neighbour's own track had broken after an unrealistically fast approach; VAB 0. Not reproduced with a realistic approach or in E2X-L. The guard does not protect against neighbours closer than 0.15 × h.

**Next test:** a tighter post-ambiguity rule than 0.15 × h for the guard-gap case above, judged on E2 / E2X / E2X-L (now more relevant, since the default's gate is larger for big boxes).

