# Plan: Static 3D Map Verification (Skywarden, camera-only)

Status: PLANNING, 2026-11-09. Follows `docs/PLAN_DENSE_COVERAGE.md` (sections 1–10; its harness, regions and run naming stay in force). Input: the Sumida clip (YouTube, CC BY), frozen SfM model of `outputs/maps/mqg_sumida_0025_0125`. Every number below is frozen before the first run; changes need a new dated section. Camera poses are FIXED for every dense comparison; a pose fix would be a separate segment experiment with its own evidence. Scale stays RELATIVE, NOT_NAVIGATION_SAFE; robot policy stays MOCK-only.

**Goal.** A static map in which paths, ground, buildings and fixed structures are recognisable, with sky and people excluded: more static surface recovered, less structure loss, less wrong depth. Point count is reported, never a criterion. Mesh only after streaks and false faces have their own check (section 7).

## 1. Baselines (full clip, same frozen poses, judged by section 3)

| Id | Layer | Config | Points | Known facts (measured unless marked reported) |
|---|---|---|---|---|
| B0 | default `dense/` (M0) | fronto sweep, next-view verify, 1x voxel, median neighbours | 917,498 | floor `cov1px` 0.0244 / 0.0024 / 0.2764, structure 0.858 / 0.8468 / 0.5455 (f000240 / f000900 / f001560) |
| B1 | `dense_experimental/` (ESVFN2x25_full) | fronto+ground sweep, multi-view verify ±8, 0.25x near voxel, `median_then_near` | 2,087,132 | floor 0.2789 / 0.119 / 0.6714, structure 0.8766 / 0.8773 / 0.6644; `inconsist` 0.1165, `float_knn` 0.0956, `float_fs` 0.3689; sparse-agree min 0.749; 147 refs / 141 verified; 135.1 s; 4,843,796 raw kept |
| B2 | `dense_static/` "Dense · static scene (trial)" | B1 + sky/person keep-masks (`data/models/static/skywater_fp32.onnx`, Realcat/skywater_seg rev ec20751; sky∪person dilated 3x3 at 384 px; the 7x7 window is ERODED by the mask in the reference and in every warped neighbour, so one masked pixel in the window drops the pixel) | 1,324,919 | 147 refs / 141 verified; 3,681,722 raw kept; 148.0 s. **Reported, to be reproduced in S0:** f000900 floor 0.119 -> 0.140, structure 0.877 -> 0.742; `float_fs` on the same depth maps 0.287 -> 0.342 |

**Findings already measured (Opus), stated as the causes this plan acts on.**
1. *Streak, f000240 right floor.* Points in columns 576–590 (640 px) lie 71 % behind the fitted floor (control columns 23 %). They come from reference views f000720–f000800 at source columns u = 636–639, the last 4 columns of the 640 px image, one constant depth per column: a border-window artefact of the depth-candidate stage (box filter with `BORDER_REFLECT`, warped neighbour border) that then passes multi-view checks. 55,331 of 4,843,796 B1 raw points (1.1 %) lie within 3 px of the image border.
2. *Mask quality.* Bare branches against the sky are partly labelled sky (f000900 left tree, f001560, f000600); a small walking person on the path in f001260 is NOT labelled person; mask edge loss is about 4 px (dilation) + 3 px window radius at 640 px, about 20 px at 1600 px. This is the mechanism behind B2's structure loss.

## 2. Frozen evaluation set (before any new run)

- **Frames.** Tuning: f000240 (t≈8 s), f000900 (t≈30 s), f001560 (t≈52 s), regions unchanged from `regions_sumida.json`. Held-out: f000600 (t≈20 s) and f001260 (t≈42 s), drawn before any change and never used to choose one. File: `packages/mapping/regions/regions_sumida_v2.json`, sha256 prefix `7f72b5463189c7bc`; byte-identical copy under `outputs/evidence/static_map/`. Classes drawn: `floor`, `floor_far`, `structure`, `wall` (held-out only).
- **Derived per frame (computed, not drawn).** `sky` = sky pixels of that frame's keep-mask; `person` = mask person pixels ∪ YOLOX-nano person boxes (conf ≥ 0.5) on that frame; `boundary band` = structure/wall region pixels within 20 px (1600-px units, 8 px at 640) of the sky mask. The derived masks are written once (`derived_<frame>.npz`) and hashed; every run uses the same files.
- **Segments.** t 4–14 s (f000240; B1 segment 25.7 s) and t 26–34 s (f000900; B1 segment 20.1 s). Every change runs on both segments first; full clip + FSC only for changes that pass both.

## 3. Common judge and metrics

**Judge.** Every run, including B0/B1/B2, is evaluated at 640x360, 1-px splat, hidden points not removed, against the SAME depth maps: B1's `outputs/experiments/dense_cov/ESVFN2x25_full/diag/depth_maps.npz` (sha256 prefix `ad55195dfec2eb08`) for `float_fs(common)` and `inconsist(common)`; the held-out view rule of `PLAN_DENSE_COVERAGE.md` section 1 is unchanged (nearest reference to k±5, not k, not in k's verifier set; verifier sets taken from the run under evaluation; a point with no common map at its held-out view is `unchecked`). Each run's own maps give `float_fs(own)` and `inconsist(own)`, reported in separate columns. Historical numbers obtained with other depth maps keep their original verdicts and are marked `judge=historical`.

**Metrics** (per run; per frame and class where applicable; written to `stage_metrics_<run>.json` and one row per value in `coverage_table.csv`):
- `cov1px` per class (floor, floor_far, structure, wall); `cov1px_boundary` = `cov1px` restricted to the boundary band (structure kept next to sky);
- `sky_false_n`, `sky_false_share` = fused points projecting into sky pixels of an eval frame; share = count / points in view of that frame;
- person: points projecting into person pixels, classified by depth against the person depth (YOLOX box bottom-centre ray intersected with that frame's fitted ground plane, `ground_plane()` of `dense_cpu.py`): `person_background` (> 1.15x, kept), `person_surface` (within ±15 %), `person_front` (< 0.85x). No ground plane on the frame -> person metric `BLOCKED` for that frame (and a run cannot PASS there);
- streak: `streak_border_share` = raw points whose source pixel is within 3 px of the image border / raw points; `streak_col_behind` = share of fused points in f000240 columns 576–590 (640 px) more than 10 % behind the fitted floor depth, with `control_col_behind` on columns 540–554 and 600–614;
- `inconsist` (common, own), `inconsist_unchecked`, `float_knn`, `float_fs` (common, own), sparse agreement min / median over verified views, `views_verified`, `points`, `raw_kept`, `seconds`.

## 4. Acceptance for a change on the full clip (vs B1 unless stated; every eval frame, held-out included)

1. `cov1px(structure)` and `cov1px(wall)` ≥ B1 − 2 pp on every frame AND `cov1px_boundary` ≥ B1 − 3 pp.
2. `cov1px(floor)` ≥ B1 − 1 pp on every frame and ≥ B2 (S0-reproduced value) on f000900.
3. `sky_false_share` ≤ 0.5 % of points in view on every frame and ≤ B1.
4. `person_surface + person_front` ≤ B1 (count, summed over frames).
5. `float_fs(common)` ≤ B1; `inconsist(common)` ≤ B1 + 1 pp; `float_knn` ≤ B1 + 1 pp; sparse agreement min ≥ 0.70.
6. `streak_border_share` ≤ 0.1 % and f000240 `streak_col_behind` ≤ `control_col_behind` + 5 pp.
7. Time ≤ 1.5 x B1 (135.1 s -> 203 s); segments ≤ 1.5 x the B1 segment time.
8. FSC regression vs `ESVFN2x25_fsc`: views verified ≥ 143, `float_fs(own)` and `inconsist(own)` not up by > 1 pp, time ≤ 1.5 x 155.1 s.
Point count is reported only. Verdicts: PASS (all clauses), FAIL (a clause missed, named), NOT_ADOPTED (segment PASS, full/FSC FAIL), BLOCKED (a metric cannot be computed), NOT_RUN (trigger not met).

## 5. Experiments, one change per run, in this order

Base for S1–S5 is the B2 configuration (B1 + masks); a passing change becomes the base of the next; a failing one is recorded with its cause and dropped. Run names: `S<k>_seg`, `S<k>_seg30`, `S<k>_full`, `S<k>_fsc`.

| Id | Single change | Mechanism it targets | Expected observable |
|---|---|---|---|
| S0 | none: reproduce B2 with the harness and the common judge | establishes B2 rows; expected to FAIL clause 1 (structure −13.5 pp on f000900, reported) | B2 numbers reproduced within rounding; `cov1px_boundary` low on f000900 / f001560 |
| S1 | border exclusion: a pixel whose 7x7 window leaves the image (reference or any warped neighbour) is invalid at the candidate stage | finding 1 | `streak_border_share` -> ≤ 0.1 %, f000240 streak columns -> control level; structure/floor unchanged within 0.5 pp |
| S2 | masked cost: ZNCC over valid (unmasked, in-image) window pixels only, per neighbour a minimum valid count ≥ 60 % of the window; reference centre pixel must be unmasked | mask erosion of whole windows (finding 2, edge loss) | `cov1px_boundary` up, structure back toward B1 on f000900; `sky_false_share` not above B1 |
| S3 | mask edge: undilated full-resolution mask for matching; dilation kept only as a final `sky_false` filter on fused points if S2 leaves sky points | remaining 4 px edge loss | boundary band up again; `sky_false_share` ≤ 0.5 % |
| S4 | person handling: classify instead of delete; points behind the person (> 1.15x person depth) seen from other views stay; surface and front points dropped | f001260 unlabelled walker; ground behind people | floor `cov1px` on f001260 and f000900 up; `person_surface + person_front` ≤ B1 |
| S5 | per-surface neighbour choice: pick the best 2 of up to 4 neighbours per pixel by parallax/overlap for the chosen plane family (near floor vs far background), compared with the current best-2-by-cost | wrong neighbours on near floor vs far structure | floor and structure both ≥ B1 − 1 pp; `inconsist(common)` not up |
| S6 | independent dense method on the 4–14 s segment with the same poses: OpenMVS CPU if it builds on this machine, else a learned depth (Depth Anything 3) where only multi-view-verified depth enters the map and the rest is a separate `estimated` layer | only if S1–S5 leave floor or structure below target; decided by a research note before any run | comparison rows against S5 base with the same judge |

Stop rule: a change that fails stays as a labelled experimental layer with its cause; the default layer changes only when a change passes every clause of section 4 on the full clip and FSC. Adoption of the default is a user decision.

## 6. Harness prerequisites (code tasks, before S0)

`packages/mapping/dense_eval.py` today has `CLASSES = ("floor", "floor_far", "structure")`, judges with the run's own maps only, and has no sky, person, boundary or streak metric. Needed: `wall` class and held-out frames; `--judge-maps <npz>` with `(common, own)` columns; derived masks; person classification with YOLOX-nano boxes; streak metrics from `raw_kept.npz` (u, v, ref) and the f000240 fitted floor; four-panel same-view image. `dense_cpu.py` needs the S1–S5 switches as `DenseConfig` fields with B2-reproducing defaults. Tests: one per new metric on a synthetic cloud with known counts.

## 7. Mesh and app

- Mesh stays `EXPERIMENTAL`, `visual_check FAIL`, until a mesh built from a passing static cloud is checked for streak faces (faces with any vertex in the f000240 streak set) and false faces (faces whose centroid projects into sky or person pixels of any eval frame); both counts and shares reported; thresholds set in a later section after the first measurement.
- App: the static map view centres on path / ground / structures (framing on the static cloud's bounding box, not on the trajectory), trajectory and cameras toggle off/on, layer name and point count shown on screen; verified in the open browser window and recorded in `ui_smoke.json` with a screenshot.

## 8. Evidence files to produce

Root: `outputs/evidence/static_map/`. Run folders: `outputs/experiments/static_map/<run>/` (`dense_report.json`, `dense_verified.ply`, `diag/`, log).
- `regions_sumida_v2.json` (copy, hash recorded), `derived_<frame>.npz` + `derived_overlay_<frame>.jpg` (sky, person, boundary band drawn).
- `stage_metrics_<run>.json` for B0_judge, B1_judge, S0 (=B2), S1–S5 `_seg`, `_seg30`, `_full`, `_fsc` as run.
- `coverage_table.csv`: one row per (run, frame, class, metric, value) including the new metrics with a `judge` column (`common` / `own` / `historical`).
- `sameview_<frame>_<run>.jpg`: source / B1 / B2 / new, 640x360 rendered at 1 px, same pose and FOV, region outlines and derived masks drawn, caption with layer label and points in view, for all five eval frames.
- `streak_f000240_<run>.json` (column shares, source views and columns of the streak points); `mask_audit_<frame>.jpg` for f000900, f001560, f000600, f001260.
- Final PLY: `outputs/maps/mqg_sumida_0025_0125/dense_static/dense_verified.ply` only for a passing change (its `dense_report.json` records `static_plan: S<k> adopted`); otherwise `outputs/experiments/static_map/<run>/dense_verified.ply` labelled experimental.
- Quality report: `outputs/evidence/static_map/summary.json` (config, counts, every metric, verdict and deciding clause per run) and one row per run in `docs/VALIDATION_LOG.md`; UI evidence in `outputs/evidence/ui/`.

## Amendment A1 (2026-11-09, after S0–S2n segment runs, before any S3r run)

Sections 1–8 unchanged. Evidence: `outputs/evidence/static_map/static_metrics_*.json`; runs under `outputs/experiments/static_map/`. Frames in the order f000240 (t 4–14 s) / f000900 (t 26–34 s) for segment rows.

1. **S0: B2 reproduced byte-identically** (`B2_full/dense_verified.ply` == `dense_static/dense_verified.ply`). Full-clip baselines on the common judge (B1 maps), B0 / B1 / B2: `float_fs` 0.519 / 0.369 / 0.388; `inconsist` 0.155 / 0.117 / 0.108; `sky_false_share` per eval frame 17–27 % / 13–24 % / 0.1–3.3 %; `cov1px_boundary` 0.63–0.93 / 0.74–0.95 / 0.28–0.66; f000240 streak columns behind-floor 0.40 / 0.88 / 0.37 vs control 0.28 / 0.30 / 0.25; f001260 person box: every point is background (602 / 1,225 / 1,199), 0 `person_surface`, 0 `person_front`.
2. **Two coverage columns.** `cov1px` is now measured on static pixels (sky and person pixels removed from each region); `cov1px_region` keeps the whole-region definition of `PLAN_DENSE_COVERAGE.md`. The reported B2 structure drop 0.877 -> 0.742 on f000900 is in the `cov1px_region` definition. Acceptance clauses of section 4 use `cov1px`; `cov1px_region` is reported next to it.
3. **S1 is a no-op on B2's erode mode**: the eroded reference support already excludes border windows (raw border share 0.0). Border exclusion becomes necessary once S2 replaces the erosion, so S1 is always paired with S2 and has no row of its own.
4. **Segment results** (B1 / B2 / S2 / S2u / S2n). S2 = valid-pixel ZNCC (>= 60 % usable window, static centre pixel) + border exclusion; S2u = S2 with undilated masks (the S3 of section 5); S2n = S2u with neighbour selection and verifier sets computed from ALL sparse points (see item 5).
   - f000240: floor 0.252 / 0.206 / 0.210 / 0.211 / 0.230; structure 0.689 / 0.631 / 0.660 / 0.676 / 0.665; band 0.775 / 0.292 / 0.503 / 0.598 / 0.600; `sky_false` 0.184 / 0.0056 / 0.0077 / 0.0092 / 0.0095; streak behind-floor 0.19 / 0.38 / 0.38 / 0.37 / 0.18 vs control 0.14 / 0.25 / 0.24 / 0.23 / 0.13.
   - f000900: floor 0.111 / 0.132 / 0.135 / 0.133 / 0.104; structure 0.646 / 0.537 / 0.579 / 0.573 / 0.631; band 0.792 / 0.349 / 0.554 / 0.634 / 0.692; `sky_false` 0.246 / 0.0027 / 0.0034 / 0.0051 / 0.0042.
   - Time: S2* about 41 s vs B1 25 s on the 4–14 s segment (budget 1.5 x 25.7 = 38.6 s; over budget, recorded).
   None of S2 / S2u / S2n meets clause 1 (band ≥ B1 − 3 pp) on either segment; they are recorded as FAIL on the segment with the band clause as the deciding criterion, and S2n is the base for S3r because it is the only variant that also removes the streak (0.18 vs control 0.13) and keeps structure within 2.4 pp of B1 on f000240 and 1.5 pp on f000900.
5. **Cause found in B2's sparse filtering.** B2 filtered the sparse points by the keep-mask also for the baseline / median-depth ratio, which changed the neighbour lists of 15 of 26 reference views in the 4–14 s segment and dropped ground-plane pixels in some views (f000190: 23,887 -> 8,495 candidates). Sparse points on sky-labelled pixels are triangulated 3D points (mostly branches), so they stay valid geometry for neighbour choice and verifier sets; the keep-mask applies to matching only (S2n).
6. **Judge bias, found before any adoption.** The frozen common judge maps are B1's own depth maps, the maps B1's points were verified against, so `float_fs(common)` is biased in B1's favour by construction. Amendment: add `float_fs(neutral)` against B0's depth maps (`outputs/experiments/dense_cov/M0_sumida_full/diag/depth_maps.npz`, used by no candidate's verification). Clause 5 "`float_fs` ≤ B1" is evaluated on the neutral judge; the B1-judge column stays reported. Nothing else in section 4 changes.
7. **Remaining band loss and new single change S3r.** The segmentation labels bare branches against the sky as sky, and S2 requires a static centre pixel, so those branches never get depth. S3r (stacked on S2n): within the model's sky label, a pixel darker than the local sky brightness (masked mean over a 61x61 window at 1600 px) by more than 30 grey levels is restored as a candidate; person labels untouched; multi-view checks unchanged. Because `sky_false` uses the frozen model labels, restored branch points count as `sky_false` there and clause 3 is judged on that column unchanged; a supplementary `sky_false_refined` (refined labels) is reported and not used for acceptance. Order unchanged: both segments, then full clip + FSC only on a pass.

## Result (2026-11-09, after S3r40 full clip and FSC)

Sections 1–8 and A1 are unchanged. Every number below is read from `outputs/evidence/static_map/summary.json` (all runs, configs, metrics, verdicts), `static_metrics_<run>.json`, `static_table.csv`, `sky_fringe.json` and `stage_metrics_S3r40_fsc.json`; run folders are `outputs/experiments/static_map/<run>/` (`dense_report.json`, `dense_verified.ply`, `<run>.log`, `<run>.static_eval.log`). Judges: B1 maps (`common`, section 3) and B0 maps (`neutral`, A1 item 6). Frames: tuning f000240 / f000900 / f001560, held-out f000600 / f001260; segment rows show f000240 (t 4–14 s) or f000900 (t 26–34 s). Coverage figures are `cov1px` on static pixels (A1 item 2). Naming: the plan's S2n is the run folder `S2un_*`; S2u is the S3 of section 5; S3r is A1 item 7; **S3r40 is S3r with the S2 minimum valid-window share lowered from 0.60 to 0.40**, a change with no dated section of its own (recorded here as a deviation from the one-change-per-section rule). The exact shell line of each run was not archived; each run's flags are in the `config` block of its `dense_report.json` and in `summary.json`.

### Runs

| Run | Scope | Change vs its base | Measured | Deciding clause | Verdict |
|---|---|---|---|---|---|
| S0 (`B2_full`) | full clip | none | `dense_verified.ply` byte-identical to `dense_static/`; B2 vs B1 structure −8.7 / −5.3 / −11.3 / −7.2 / −11.9 pp, wall −18.9 / −14.6 pp, band 0.46 / 0.66 / 0.28 / 0.38 / 0.38 vs 0.95 / 0.91 / 0.79 / 0.91 / 0.74; `sky_false` 0.43 / 1.41 / 1.34 / 0.10 / 3.33 % vs 13.0–24.2 %; `float_fs` neutral 0.3554 vs 0.3384; 150.3 s (the 148.0 s of section 1 was the reported figure) | clause 1 (structure, band) as expected | PASS (reproduction); B2 itself FAIL vs B1 |
| S1 | — | border exclusion alone | no-op on B2's erode mode (raw border share already 0.0); control run `X_border_seg` (B1 config + border exclusion, no masks): floor 0.2456, structure 0.6871, band 0.7754 vs B1_seg 0.252 / 0.6885 / 0.7754, 25.4 s | — | no row (A1 item 3); paired with S2 |
| S2 | seg / seg30 | valid-pixel ZNCC (≥ 60 % usable window, static centre) + border exclusion | band 0.2921 → 0.5029, 0.3489 → 0.5543; structure 0.6307 → 0.6604, 0.5369 → 0.579; floor 0.2098 / 0.1345; `sky_false` 0.77 / 0.34 %; 41.4 s / 33.2 s (B1 25.4 / 20.1 s) | clause 1 band (B1 − 3 pp = 0.745 / 0.762); clause 7 time | **FAIL** |
| S2u (planned S3) | seg / seg30 | S2 + undilated masks | band 0.5983 / 0.6343; structure 0.6759 / 0.5728; `sky_false` 0.92 / 0.51 %; 41.5 / 33.0 s | clause 1 band | **FAIL** |
| S2n (`S2un_*`) | seg / seg30 | S2u + neighbour lists and verifier sets from all sparse points | band 0.6004 / 0.6919; floor 0.2295 / 0.1043; structure 0.665 / 0.6306; streak 0.1774 vs control 0.1298 (B2_seg 0.3776 vs 0.2504); 26 / 14 views; 41.4 / 33.1 s | clause 1 band | **FAIL**; base for S3r (A1 item 4) |
| S3r | seg / seg30 | sky refinement, delta 30 | band 0.6337 / 0.7189; structure 0.6666 / 0.6294; `sky_false` 1.28 / 0.85 % (frozen labels; restored branch points count here, A1 item 7); 41.7 / 33.0 s | clause 1 band; clause 3 | **FAIL** |
| S3r40 | seg / seg30 | S3r with min valid window 0.40 | band 0.669 / 0.7453 (B1 0.7754 / 0.7918: −10.6 / −4.7 pp); structure 0.6714 / 0.6357 (−1.7 / −1.0 pp); floor 0.2308 / 0.1094 (−2.1 / −0.1 pp); `sky_false` 1.79 / 1.45 %; streak 0.1454 vs 0.191; `float_fs` neutral 0.4069 / 0.3847 (B1 0.3744 / 0.4378); 41.6 / 33.0 s | clause 1 band; clause 2 (f000240 −2.1 pp); clause 3; clause 7 | **FAIL** |
| S3r40_full | full clip | same | clause table below; 143 views verified, 1,530,092 pts, 221.3 s | clauses 1, 2, 3, 4, 5 (`float_fs` neutral), 7 | **FAIL** vs B1; improves on B2 (below) |
| S3r40_fsc | FSC | same, masks `masks_refined_fsc` | 149 / 149 views (≥ 143), 6,444,805 pts (B1 6,520,886), `float_fs` own 0.1777 (0.1817), `inconsist` own 0.0435 (0.0444), `float_knn` 0.0542 (0.0546), sparse agreement min 0.923 / median 0.98; **244.7 s > 232.7 s** | clause 8 time | **FAIL** (quality clauses met) |
| S4 | measured on every run | classify, delete nothing | f001260 person box: B0 / B1 / B2 602 / 1,225 / 1,199 points, all `background`, 0 surface, 0 front; S3r40 1,203 background, 0 surface, **3 front**; no other eval frame has a person | clause 4 (sum ≤ B1 = 0) | S3r40 FAIL by 3 points |
| S5 | — | per-pixel neighbour choice | not run: S2 already exceeds the segment time budget (41.4 s vs 38.6 s) | clause 7 | **NOT_RUN** |
| S6 OpenMVS, `OMVS_seg` / `OMVSm_seg` | seg, 640 px, unmasked / masked | independent tool, same fixed poses | floor 0.3848 / 0.3873 (B1_seg 0.252), floor_far 0.8524 / 0.8665, `floor_on_ground` = floor (no floor point off the fitted plane), `float_fs` neutral 0.2347 / 0.2338 (B1 0.3744), `float_knn` 0.0149 / 0.0147, streak columns 0.0 vs 0.0; **structure 0.1184 / 0.1275**, band 0.1425 / 0.1428; `sky_false` 3.17 / 1.19 %; 98,941 / 99,599 pts; 8.0 / 7.0 s. `inconsist`, `views_verified`, border share not produced by the tool | comparison, not a candidate | reported |
| S6 OpenMVS, `OMVSd_seg` | seg, default resolution (800 px) | same | `DensifyPointCloud` exits with signal −11 after loading 26 images (35.31 MPixels) and 8,985 points, at "Geometric-consistent estimated depth-maps 1 (3.85 %)" (`openmvs_default_res_segfault.log`) | — | **BLOCKED** |
| S6 OpenMVS, `OMVSm_full` | full clip, 640 px, masked | same | 141,563 pts, 81.6 s; floor 0.0177 / 0.0042 / 0.4036 / 0.0064 / 0.3119 (B1 0.2789 / 0.119 / 0.6714 / 0.1547 / 0.4583); structure 0.2204 / 0.067 / 0.1292 / 0.0814 / 0.2014; wall 0.0993 / 0.0076; `sky_false` 0.47 / 2.47 / 6.18 / 0.25 / 4.58 %; `float_fs` neutral 0.3053; person f001260 64 background, 4 surface | near floor missing on f000240 / f000900 / f000600 (cause not investigated) | reported |
| S6 union `U_full` | full clip | S3r40_full ∪ OMVSm_full | 1,671,655 pts, 302.9 s; floor 0.244 / 0.1239 / 0.7926 / 0.1496 / 0.5704 (vs S3r40 +0.7 / +0.3 / +11.1 / +0.5 / +11.8 pp); structure 0.9056 / 0.8715 / 0.6781 / 0.8282 / 0.5831; `sky_false` 1.36 / 4.37 / 4.16 / 1.42 / 6.46 %; `float_fs` neutral 0.3509; person 1,267 / 4 / 3 | clause 7 (303 s); AGPL tool kept separate | not integrated |
| S6 Depth Anything 3 | — | learned depth as prior | not run: no torch in `.venv`; `xformers` hard dependency without MPS support; only SMALL / BASE are Apache-2.0 with pose input (`research_notes.md`, UNVERIFIED on MPS) | — | **NOT_RUN** |
| Mesh (section 7) | — | — | no new mesh built; previous mesh stays EXPERIMENTAL, `visual_check FAIL` | — | not provided |

### S3r40_full against section 4 (vs B1 = ESVFN2x25_full; thresholds in brackets)

| Clause | f000240 | f000900 | f001560 | f000600 | f001260 | Result |
|---|---|---|---|---|---|---|
| 1 structure (≥ B1 − 2 pp) | 0.8964 vs 0.927 (−3.1) | 0.866 vs 0.8694 (−0.3) | 0.6514 vs 0.6913 (−4.0) | 0.8235 vs 0.8373 (−1.4) | 0.5513 vs 0.6183 (−6.7) | FAIL on 3 frames |
| 1 wall (≥ B1 − 2 pp) | — | — | — | 0.808 vs 0.9244 (−11.6) | 0.1305 vs 0.2238 (−9.3) | FAIL on both |
| 1 band (≥ B1 − 3 pp) | 0.8394 vs 0.9476 (−10.8) | 0.8811 vs 0.9061 (−2.5) | 0.6282 vs 0.7859 (−15.8) | 0.6702 vs 0.9069 (−23.7) | 0.6486 vs 0.738 (−8.9) | FAIL on 4 frames |
| 2 floor (≥ B1 − 1 pp; f000900 ≥ B2 0.140) | 0.2369 vs 0.2789 (−4.2) | 0.1205 vs 0.119 (+0.2) but < 0.140 | 0.6816 vs 0.6714 (+1.0) | 0.145 vs 0.1547 (−1.0) | 0.4522 vs 0.4583 (−0.6) | FAIL (f000240; f000900 vs B2) |
| 3 `sky_false` (≤ 0.5 % and ≤ B1) | 1.44 % | 4.41 % | 3.94 % | 1.48 % | 6.77 % | FAIL (all ≤ B1, none ≤ 0.5 %) |
| 4 person surface + front (≤ B1 = 0) | 0 | 0 | 0 | 0 | 3 | FAIL |
| 5 `float_fs` neutral ≤ 0.3384; `inconsist` common ≤ 0.1265; `float_knn` ≤ 0.1056; sparse min ≥ 0.70 | 0.3551 **FAIL**; 0.1099 ok; 0.1024 ok; 0.725 ok | | | | | FAIL (`float_fs`) |
| 6 raw border ≤ 0.1 %; streak ≤ control + 5 pp | 0.0 %; 0.1708 vs 0.1911 | | | | | PASS |
| 7 time ≤ 203 s; segments ≤ 38.6 / 30.2 s | 221.3 s; 41.6 / 33.0 s | | | | | FAIL |
| 8 FSC | 149 views ok; `float_fs` own 0.1777 ok; `inconsist` own 0.0435 ok; 244.7 s > 232.7 s | | | | | FAIL (time) |

The verdict string in `summary.json` names structure on 3 frames, wall f000600, `sky_false`, `float_fs` neutral and time; the band, wall f001260, floor and person items above come from the same file and add nothing to the verdict (FAIL), only to the list of what a future change has to recover.

### What S3r40 does deliver (vs B2, the static trial it replaces; A1 item 4 goal)

Band 0.4622 / 0.6599 / 0.2828 / 0.3816 / 0.376 → 0.8394 / 0.8811 / 0.6282 / 0.6702 / 0.6486 (+37.7 / +22.1 / +34.5 / +28.9 / +27.3 pp); structure +5.6 / +4.9 / +7.3 / +5.8 / +5.2 pp (all five frames); wall +7.3 / +5.2 pp; floor +2.7 / −2.0 / +2.3 / +0.1 / +1.0 pp; f000240 streak columns behind the floor 0.373 (control 0.245) → 0.1708 (control 0.1911), i.e. at control level, and raw border share 0.0; `float_fs` neutral 0.3554 → 0.3551, `inconsist` common 0.108 → 0.1099, `float_knn` 0.1066 → 0.1024 (errors equal within rounding); views verified 141 → 143; points 1,324,919 → 1,530,092; time 150.3 → 221.3 s. `sky_false` rises from 0.43 / 1.41 / 1.34 / 0.10 / 3.33 % to 1.44 / 4.41 / 3.94 / 1.48 / 6.77 % because the restored branch pixels are sky under the frozen model labels (A1 item 7). Supplementary sky-fringe colour check (`sky_fringe.json`, CIELAB dE < 12 to the frame's median sky, points on structure/wall pixels): B1 2.92 / 0.16 / 1.07 / 2.74 / 1.92 %, B2 0.07 / 0.06 / 0.06 / 0.21 / 0.03 %, S3r40 0.19 / 0.05 / 0.07 / 0.55 / 0.05 % — the branch points restored by S3r are not sky-coloured, but this metric is not part of section 4.

### Deviation from the run order

Section 2 and A1 item 7 allow a full-clip run only after both segments pass. S3r40 failed both segments on the band clause and was still run on the full clip and FSC, so that the full-clip comparison with B1 and B2 exists on disk. The segment verdicts stand; the full-clip and FSC rows are recorded as FAIL under the frozen clauses, not as a passing change.

### Decision

- Default dense layer: unchanged (M0, `dense/`). `dense_experimental/` (B1) and `dense_static/` (B2) unchanged.
- S3r40 is installed as a **labelled trial, NOT_ADOPTED**: `outputs/maps/mqg_sumida_0025_0125/dense_static_v2/` (label `DENSE_STATIC_TRIAL_V2 (relative scale) - not adopted`, `experimental.status NOT_ADOPTED`, `run S3r40_full`, `static_masks.directory outputs/experiments/static_map/masks_refined`) and `outputs/maps/mqg_fsc_campus_aerial/dense_static_v2/` (masks `masks_refined_fsc`). Console layer "Dense · static scene v2 (trial)"; UI evidence `outputs/evidence/ui/ui_smoke.json` (83 / 83) and `03f_static_map_v2.png`.
- No mesh. Camera poses fixed throughout; Utrecht not touched (its path jumps stay an independent open item, F-11). Scale RELATIVE, NOT_NAVIGATION_SAFE; robot policy MOCK-only.
- Open for a next dated section (not started): recover the band and wall loss next to sky (segmentation labels, not matching, are now the limiting factor: branches labelled sky, ~4 px edge at 384 px), a time budget that admits valid-pixel matching (S2 costs 1.6x B1 on the segment with no masks: `X_valid_seg` 39.7 s vs 25.4 s), the three `person_front` points on f001260, and an OpenMVS run at its default resolution on this machine (BLOCKED by the segfault). Adoption of any static layer as default is a user decision.
