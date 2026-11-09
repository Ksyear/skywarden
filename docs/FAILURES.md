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

## Not failures, but open gates

- REAL_VIDEO route NOT_RUN: `assets/input/` is empty. Command in `docs/PROGRESS.md`.
- STALE_DATA banner in the web UI not yet asserted (NOT_RUN); the stale-frame STOP itself is verified in `outputs/evidence/m3/tracking_sim.json` (`stale_frame_stops: 5`, `nonzero_yaw_during_freeze_after_0.3s: 0`).
- (closed 2026-11-09) UI smoke re-run after F-06: 51 / 51; `scripts/test_all.sh` after the Skywarden rename: 94 passed (`outputs/evidence/tests/pytest-20261109-023923.log`).
- (closed 2026-11-09) E4 test archive: `tests/test_appearance.py` 3 passed inside `outputs/evidence/tests/pytest-20261109-024540.log` (97 passed) and every later log.
- (closed 2026-11-09) Live run / timeline replay: implemented and measured (`docs/PLAN_LIVE_RUN_TIMELINE.md` Result; `outputs/evidence/live/`); no acceptance test failed. Still open from it, not failures: a run with a physical webcam (NOT_RUN, no camera on this Mac; `webcam:0` correctly ends `FAILED NO_DEVICE` in 0.285 s), browser assertions for the live tab's `STALE_DATA` / `STALLED` / `FAILED` / `INCOMPLETE` / `CANCELLED` states (NOT_RUN; labels exist in `live.js`), and the observation that on the Utrecht person-following clip the target was LOCKED in only 155 of 899 frames (17 %) with 7 scripted re-selections after LOST — a tracker limitation on that clip (compare F-11), recorded in `t7_utrecht_realtime_30s.json`, not a live-run defect. Stored-map relocalisation: NOT_IMPLEMENTED (VO and live headers say so). Bangkok per-boundary cause classification (plan D1 field): not written.
