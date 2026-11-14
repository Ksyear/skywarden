# PLAN_E2_TRACKER_FIX — minimal fix for the two-person crossing failure (E2)

Status: IMPLEMENTED 2026-11-09 (see section 7, "As implemented"); sections 1-6 are the plan as written before coding. Scope: `packages/tracking/tracker.py`, `packages/control/yaw_controller.py` (reason code only), UI/replay state display, tests, E2/E3 reruns. Explicitly out of scope: appearance Re-ID, any new tracking framework, gate/noise retuning.

## 1. Failure being fixed (from `outputs/evidence/e2_two_person/`, baseline kept in `e2_two_person_baseline_v1/`)
- Person B walking through hidden target A's predicted position is assigned to A's track for ~1 frame (158/350 crossing runs). That hit refreshes `last_hit_t`, so A's track outlives `max_coast_s` (K2: LOST after a 2.5 s occlusion in only 10 %).
- Expiry runs only on UNMATCHED tracks after assignment, so a track whose last trusted hit is > 2 s old can be revived by any detection inside the gate.

## 2. Design (Opus proposal adopted with two adjustments)
Critique: the proposal is sound and minimal. Adjustments: (i) a track in AMBIGUOUS must still age against its own last trusted `last_hit_t`, so it expires at the same 2 s boundary as PREDICTING (otherwise a contested track could coast forever); (ii) expiry must use the trusted time only, which the proposal already guarantees because ambiguous hits never write `last_hit_t`; make this an assertion in tests, not a comment.

Update order inside `MultiTracker.update(t, dets)`:
1. Predict all tracks (unchanged).
2. **Expire before association.** Remove every track with `t - last_hit_t > max_coast_s`; TENTATIVE tracks that missed a frame still expire as today. **Boundary policy:** age exactly equal to `max_coast_s` (2.000 s) is still alive; strictly greater expires. The rule is evaluated on the actual timestamps, so irregular frame intervals change nothing: a frame at 2.0 s keeps the track, a frame at 2.0001 s removes it, and there is no "grace frame". An expired id is deleted and never reused; a later detection creates a new id.
3. **Gate graph.** Build the bipartite graph of (surviving track, detection) pairs with squared Mahalanobis distance <= `gate_chi2` (9.21). Take connected components:
   - component with exactly 1 track and 1 detection: trusted hit, Kalman correct, `hits += 1`, `last_hit_t = t`, LOCKED/TENTATIVE as today;
   - component with > 1 track or > 1 detection: **AMBIGUOUS**. No correction, no `hits`/`last_hit_t` change, every track in it gets `TrackState.AMBIGUOUS` (coasting on prediction with contested evidence), and none of its detections spawns a new track this frame;
   - track with no gated detection: PREDICTING (or LOST if TENTATIVE); detection with no gated track: new TENTATIVE track.
   Hungarian is no longer needed for a 1x1 component but stays for the degenerate case; results for unambiguous frames are identical to today.
4. `TargetSelector.evaluate`: `TrackState.AMBIGUOUS` -> new `TargetState.AMBIGUOUS`; once the selected id is gone the selector returns LOST on every call until `select()` is invoked again (sticky). The selector never picks another id, including a new id appearing at the old position.
5. `YawController.command` already returns STOP for any state other than LOCKED; add the reason string `TARGET_AMBIGUOUS` (and keep `TARGET_LOST`) so the block is visible in logs. The STOP is produced in the real command path used by `virtual_replay.py`, not in the UI.
6. UI/replay: badge and timeline colour for AMBIGUOUS; old recorded runs without the state stay readable (unknown state -> displayed as before, never crash).

Honest limit: motion alone cannot tell two people apart when their centres fall inside one gate. The fix converts a wrong lock into a STOP; it does not guarantee automatic re-lock on A after an occlusion or a close pass. If B stands near A's reappearance point when A returns, the result is AMBIGUOUS then LOST, and the operator must re-select.

## 3. Regression tests (`packages/tracking/tests/test_tracker_fix.py`; each must FAIL on the baseline tracker, PASS after)
T1 B through A's predicted position while A hidden: A's `last_hit_t` unchanged, A never LOCKED on B's detection, A expires at > 2 s; B's own id continues.
T2 Detection returns after 2.5 s with no intermediate frames: old id absent, new id created, selector LOST (sticky) until `select()`.
T3 Boundary: frames at age 1.999 s and 2.000 s keep the id; 2.001 s removes it; same with irregular dt sequences (e.g. 0.033, 0.5, 1.47 s).
T4 Ambiguous component (2 tracks, 1 detection; 1 track, 2 detections): states AMBIGUOUS, no `hits`/`last_hit_t`/state update, no new track spawned from those detections.
T5 Selector never auto-switches: after LOST a new id at the old position stays unselected; `TargetState` remains LOST.
T6 Control block through the real path: `YawController` fed AMBIGUOUS/LOST/PREDICTING returns yaw 0 with reasons `TARGET_AMBIGUOUS`/`TARGET_LOST`; unaffected LOCKED case still yaws.
T7 Unambiguous regression guard: a single-person sequence produces identical track ids/positions before and after the change.

## 4. E2 rerun protocol
- Same script `packages/sim/two_person_crossing.py`, same 700 runs (same seeds, same scenario grid), K1/K2/K3 thresholds unchanged, plus the same-row sensitivity sweep. Output to `outputs/evidence/e2_two_person_fix_v2/`; baseline directory untouched.
- Report table: baseline vs fixed for K1, K2 (LOST after 2.5 s), K3 (any LOCKED frame on B), re-lock rate after occlusion (expected to drop; report the drop as is), ids created, mean frames in AMBIGUOUS.
- Control-block metrics (socket-free, real `YawController` via `virtual_replay`-style wiring, reported separately from id accuracy): nonzero-yaw frames while not LOCKED, nonzero-yaw frames while the target track is on B, nonzero-yaw frames while on A. Target: first two are 0 in every run; third is the useful-yaw count and must not be 0 for the no-occlusion rows.
- E3 regression (`packages/sim/tracking_scenario.py`) rerun with identical config; any metric change beyond noise is reported, not tuned away.

## 5. Risks
- Over-triggering: a target walking beside another person stops control until they separate (accepted trade-off). Measure the AMBIGUOUS fraction in E2/E3 so it is visible.
- Larger components under crowding block all involved tracks; acceptable for a one-target system, but note in results.
- Replay files from older runs lack the new state; UI must degrade gracefully (T-UI check below).

## 6. Acceptance
- T1–T7 fail on the baseline tracker (recorded) and pass on the fix; existing tracker tests still pass.
- E2 fixed run: K3 = 0 LOCKED frames on B across 700 runs; K2 LOST after 2.5 s occlusion = 100 %; nonzero-yaw frames while not LOCKED or while on B = 0; re-lock loss reported numerically.
- E3 rerun completed and compared; UI loads one old and one new recorded run and shows the AMBIGUOUS badge; `docs/PROGRESS.md` and `docs/VALIDATION_LOG.md` updated from the raw JSON only.

## 7. As implemented (2026-11-09, branch `fix/e2-target-ambiguity`)

Evidence: `outputs/evidence/e2_two_person_baseline_v1/` (original FAIL, preserved with `MANIFEST.sha256`), `e2_two_person_baseline_v1_control/` (same tracker, control-path counters added), `e2_two_person_fix_v2/` (first fix), `e2_two_person_fix_v3/` (final), `e3_geofence_after_tracker_fix/`, `outputs/evidence/tests/pytest-20261109-022116.log` and `-023130.log` (94 passed), `outputs/evidence/ui/ui_smoke.json` (50 / 51). Narrative rows: `docs/VALIDATION_LOG.md`, `docs/PROGRESS.md`, `docs/FAILURES.md` F-05 / F-06.

Differences from sections 2-3:
- **Pixel gate added (not in the plan).** The first implementation (fix_v2) followed section 2 exactly: Mahalanobis gate only. It gave K2 100 % and K3 0 but K1 at 1.5 s dropped to 44 % (FAIL), broke the existing closed-loop test (1.5 s occlusion no longer re-locked because the coasting gate had grown to about 500 px and swallowed an unrelated person) and churned ids on real clips (vtest 39 -> 441). After seeing those results the gate became "chi2 <= 9.21 AND pixel distance <= 0.5 x box height" (`TrackerConfig.max_gate_box_frac`). Criteria K1-K3 were not changed. This is the fix_v3 that passes.
- **Gate-graph association applies to confirmed tracks only** (LOCKED / PREDICTING / AMBIGUOUS). TENTATIVE tracks keep gated Hungarian on the detections no confirmed track claimed; the plan's wording treated all tracks alike. Reason recorded in the tracker docstring: tentative gates are wide, so letting them make confirmed tracks ambiguous would only churn ids.
- **Tests live in `tests/test_tracker_ambiguity.py` (11 tests)**, not `packages/tracking/tests/test_tracker_fix.py`. Mapping to T1-T7: T1 `test_crossing_person_never_inherits_the_hidden_target`; T2 `test_detection_returning_after_more_than_2s_without_frames_gets_a_new_id`; T3 `test_coast_limit_boundary` (1.999 / 2.000 alive, 2.001 expired) + `test_irregular_intervals_do_not_extend_the_limit`; T4 `test_contested_detection_is_reported_as_ambiguous_and_stops_control`; T5 `test_lost_is_sticky_until_the_operator_selects_again`; T6 `test_no_yaw_toward_wrong_person_and_none_while_not_locked`, `test_virtual_control_is_zero_while_lost`, `test_recorded_replay_stops_on_ambiguous_target` (console replay path); T7 is covered by the unchanged `tests/test_tracking.py` (8) and `tests/test_closed_loop.py` (1) passing. Implementer-recorded: 8 of the 11 failed on the pre-fix tracker; that run was not archived.
- **Control-path metrics are inside the E2 harness** (`packages/sim/two_person_crossing.py` runs the real `YawController` with virtual telemetry) rather than a separate `virtual_replay` wiring, plus an off-centre crossing at x = 440 so a wrong lock produces a non-zero yaw error.
- **UI:** `AMBIGUOUS` badge tone, dashed box, timeline colour and a hint text in `apps/desktop/static/tracking.js`; unknown states fall back to the previous rendering.

Acceptance (section 6) against the evidence: T1-T7 PASS; E2 fix_v3 K3 = 0, K2 = 100 %, non-zero yaw while not LOCKED or toward B = 0; re-lock loss reported (K1 still 100 % at 0.5 / 1.5 s in E2, but ambiguous frames 3,548 / 58,044 and shorter confirmed spans on real clips, e.g. vtest p50 13.0 -> 3.65 s); E3 rerun PASS with identical distances; UI shows the AMBIGUOUS badge and `STOP · TARGET_AMBIGUOUS` (4 new checks PASS); the first smoke run was 50 / 51 because of a pre-existing frame-index bug (F-06), fixed, and the re-run is 51 / 51 (`outputs/evidence/ui/ui_smoke.json`). Suite after the rename: 94 passed (`outputs/evidence/tests/pytest-20261109-023923.log`). Docs updated from the raw JSON.
