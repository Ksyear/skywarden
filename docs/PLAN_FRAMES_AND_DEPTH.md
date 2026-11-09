# Plan: frame selection and depth neighbours for the static map

Status: PLAN (2026-11-09). Planning only; no run in this document has been executed. Scope: camera-only static 3D map, relative scale, NOT_NAVIGATION_SAFE. Sky and people stay excluded.

**Goal.** Floor and far structures show their real shape better; frames and points stop breaking up where the camera moves fast or very slowly; the camera path does not get worse.

**Baseline.** S3r40 = "Dense · static scene v2 (trial)": `outputs/maps/mqg_sumida_0025_0125/dense_static_v2` (Sumida, 221.3 s) and `outputs/maps/mqg_fsc_campus_aerial/dense_static_v2` (FSC, 244.7 s). Numbers come from `outputs/evidence/static_map/summary.json` (`S3r40_full`, `S3r40_fsc`).

## 1. Findings that motivate the changes (measured, not yet acted on)

- Registration is 100 % on Sumida and FSC; the largest time gap between kept frames is 0.67 s. The gap is produced by the uniform cap in `frame_selection.py` (`linspace` drop 180 -> 150 candidates), not by blur or duplicate filtering.
- Utrecht (`mqg_utrecht_fps5`) is PARTIAL with 43 time-normalised path jumps, many in t 0.4–4.4 s.
- Sumida, 3-s bins, S3r40: t 33–39 s rotation 7–10 deg/s, confident-depth share of reference views 0.03–0.11 (about 0.2 elsewhere); t 54–60 s rotation 19–22 deg/s with speed x2, confident share about 0.06; t 15–30 s neighbour baseline / median co-visible depth 0.018–0.037, below the 0.05 floor of `BASE_DEPTH` (open park: far scene makes the median depth large).
- FSC is stable except the slow start t 0–3 s (speed x0.29 of the clip mean).

Reading: fast segments lose frames to fixed-step sampling plus the cap; slow segments and open scenes give neighbours too little parallax for the median-depth rule; far surfaces are judged by a median that near ground dominates.

## 2. Frozen before any run

- **Sumida eval frames:** tuning f000240 / f000900 / f001560, held-out f000600 / f001260 (`packages/mapping/regions/regions_sumida_v2.json`, unchanged) **plus two fast-motion frames** at t ≈ 36 s and t ≈ 56 s in a new file `regions_sumida_fast.json` (floor, structure, sky regions drawn on the S3r40 frame set before any change; sha256 recorded). For F runs the kept frame nearest in time (≤ 0.2 s) is used and its source index logged.
- **FSC eval frames:** two frames with near floor (ground/field) and far structure (terrain/buildings) in `regions_fsc_v1.json`; one of them held-out.
- **Utrecht:** path-only (registration ratio, gaps, time-normalised jumps in t 0–10 s and whole clip); no region file.
- **Judge for D runs:** neutral depth maps `outputs/experiments/dense_cov/M0_sumida_full/diag/depth_maps.npz`. For F runs poses differ, so `float_fs` and `inconsist` are "own" (held-out views of the same map); they are reported, compared only with S3r40's own values.
- Dense config for every run: S3r40 (`work_width 640, planes 64, fronto+ground, verify multi/8, min_valid_frac 0.4, neighbours_all_sparse, masks_refined`).

## 3. Two separate comparisons

**(F) Frame selection only.** New frame set -> new SfM -> dense with S3r40 config. Compared with S3r40 on coverage, path, errors (own), time (SfM time reported separately from dense time).

**(D) Depth only.** Same S3r40 poses and frame set; only the neighbour rule or the depth estimator changes. Compared with S3r40 on coverage, errors (neutral judge), time.

Changes are combined only after each passes alone; the combined run is then judged again against S3r40 with the same clauses.

## 4. Changes, one at a time

**F1 motion-adaptive selection** (extends `frame_selection.py`, same two-pass structure). Scan candidates at a fixed rate (10 fps); run sparse feature tracking (static features only: sky and person masks applied) from the last kept frame. Keep a candidate when any of: tracked share from the last kept frame < overlap floor (0.6); rotation-compensated parallax (median residual flow after a homography fit, translation-only flow) > parallax threshold (frames of the work width, e.g. 8 px at 640); total median flow > flow threshold (40 px at 640). Fast segments: keep the real intermediate frame that still has tracked share ≥ floor to both kept neighbours. Slow segments: skip until parallax is enough (no frame kept while residual flow < 3 px, unless the overlap floor triggers). Blur filter unchanged; duplicate filter replaced by the parallax rule. Cap 150: drop the frame with the smallest parallax score whose removal keeps tracked share ≥ floor between its neighbours; repeat. Eval frames forced in. Output `selection.json`: per kept frame source index, time, reason (overlap / parallax / flow / forced), per dropped span start, end, cause (blur, no connection, cap). Spans with no usable source frame are listed as time + cause, never filled with synthetic frames.

**D1 far-structure neighbours** (`dense_cpu.py`, new `neighbour_rule = "median_then_far"`): after the existing median and p10 passes, also admit neighbours within ±12 frames whose baseline / **p90** co-visible depth lies in [0.05, 0.30], up to 6 neighbours per reference view; the best-2-of-N ZNCC cost is unchanged. Step 0 (diagnostic, before the run): for each eval frame list the ±12 candidates with their median/p10/p90 ratios and how many of the far-structure sparse points each candidate sees; D1 is run only if at least one eval frame has an admitted p90 neighbour that the median rule rejects. Otherwise D1 is marked NOT_NEEDED with the table as evidence.

**D2 near floor** (only if D1 leaves floor below the clause in section 5): either coarse-to-fine (sweep at 320 px, refine around the coarse depth at 640 with 16 planes) or multi-view-supported local planes (RANSAC on sparse floor points per view, accepted only where ≥ 2 neighbours agree photometrically). Boundary check: floor points must not fall on wall/rail pixels of any eval frame (`floor_on_ground` and a new `floor_on_wall` share ≤ S3r40 + 0.5 pp). Unsupported areas stay empty.

## 5. Metrics and acceptance (vs S3r40, every eval frame, held-out included)

Metrics: `cov1px` floor and structure on static pixels; sky-edge band; registration ratio and max gap; path jumps (time-normalised, factor 5); `float_fs` (own for F, neutral for D), `float_knn`, `inconsist`; `sky_false` share; person front / surface points; wall time. Point count is reported, not judged. No distance or speed units are stated (relative scale).

| Clause | PASS |
|---|---|
| 1 coverage | floor and structure `cov1px` ≥ S3r40 on every eval frame, and ≥ +3 pp on the failing-segment frames (F: fast frames t≈36 s / 56 s; D: far-structure regions) |
| 2 held-out | no held-out frame regresses by > 1 pp on any class |
| 3 path | registration ratio ≥ S3r40; path jumps ≤ S3r40; max gap ≤ 0.67 s (Sumida, FSC); Utrecht jumps in t 0–10 s and whole clip ≤ current |
| 4 errors | `float_fs` and `inconsist` not up by > 1 pp; `sky_false` ≤ S3r40 + 0.5 pp; person front + surface ≤ S3r40 (3 on f001260) |
| 5 time | dense time ≤ 1.5 x S3r40 (Sumida ≤ 332 s, FSC ≤ 367 s); F runs report SfM time separately, not judged |

Reproducible command per run: the selection / dense / eval invocation is archived as `<run>.cmd` next to `dense_report.json`; evidence folder `outputs/experiments/frames_depth/<run>/` with `selection.json` (F), `neighbours.json` (D), `static_metrics_<run>.json`, `path_metrics_<run>.json`, `<run>.log`. Summary table appended to `outputs/evidence/static_map/summary.json` under `frames_depth`.

**Order:** D1 step 0 (table) -> D1 -> F1 Sumida -> F1 FSC -> F1 Utrecht (path only) -> D2 if needed -> combination of passing changes.

## 6. Stop rule and fallback

A change that fails any clause stays an experimental layer with its cause written in the run row; it is not combined and not installed. Only passing changes become default candidates (adoption remains a user decision). If F1 cannot be run (tracking too slow, >10 min on Sumida) the fallback is F0: cap by dropping the smallest-parallax candidates instead of `linspace`, judged by the same clauses. If no eval frame qualifies in D1 step 0, D1 is NOT_NEEDED and D2 is examined directly. Blocked path: a run that cannot complete (crash, missing masks) is marked BLOCKED with the log, and the remaining runs continue.

## Result

Written 2026-11-09 after the runs, from `outputs/evidence/frames_depth/summary.json` (`findings`, `verdicts`, `paths`, `layers`, `runs`), `fs_rates.json`, `static_metrics_<run>.json`, the `map_report.json` / `selection.json` of the three `mqf_*_F1b` sessions and `research_mirage.md`. Sections 1–6 above are unchanged; where the runs departed from them the departure is listed in "Deviations". Status words: PASS / FAIL / BLOCKED / NOT_RUN / NOT_NEEDED / UNMEASURED_QUIET. No distance or speed carries a unit (relative scale). **Default frame selection and default dense layer are unchanged.**

### What was run

| Step | Status | Evidence |
|---|---|---|
| Eval frames frozen | done | `packages/mapping/regions/regions_sumida_v3.json` (tuning f000240 / f000900 / f001560, held-out f000600 / f001260, fast f001080 (t ≈ 36 s) / f001680 (t ≈ 56 s)), `packages/mapping/regions/regions_fsc_v1.json` (tuning f000320 / f001000, held-out f001720; classes floor / structure / far) |
| D1 step 0 (neighbour table for the fast frame) | run; D1 **needed** | `findings.sumida_fast_36s`: reference f001080 had only 2 neighbours, both 6 frames away (camera turning nearly in place: close frames have too little baseline); the matching cost rejected 97 % of the floor; the view was REJECTED (sparse agreement 0). Far-rule neighbours turn it VERIFIED (`D1_seg36`). |
| D1 segments t 4–14 / 26–34 / 33–39 / 48–56 s | run | `runs.D1_seg*` vs `runs.V2_seg*` (table below) |
| D1 full clip, D1 FSC | run; **FAIL** | `runs.D1_full`, `runs.D1_fsc` |
| D2 (half-resolution floor fill, `coarse_ground`) on D1 = **D12**, segments + full clip | run; **FAIL** (same clauses as D1) | `runs.D12_seg*`, `runs.D12_full` |
| F1 (parallax / flow keyframes only, `adaptive_base none`) Sumida | selection only; **rejected before SfM** | `outputs/experiments/frames_depth/F1_selection_sumida/selection.json`: 600 candidates, 92 kept (flow 40, connector 22, slow_parallax 13, parallax 9, forced 7, first 1), too few for dense neighbours — forward motion over a near-planar floor leaves small homography residuals |
| F1b (fixed 3 fps base kept + connectors / flow frames, no uniform cap) Sumida, FSC, Utrecht | run; path **PASS**, dense **FAIL** | `outputs/maps/mqf_sumida_F1b`, `mqf_fsc_F1b`, `mqf_utrecht_F1b` (`selection.json`, `map_report.json`); `runs.F1b_mqf_sumida_F1b`, `runs.F1b_mqf_fsc_F1b` |
| F1b + D12 combination (Sumida) | run although neither part passed alone (deviation); **FAIL** | `runs.F1bD12_full` |
| Time clause (5) | **UNMEASURED_QUIET** | every full-clip time was measured while other jobs ran in parallel (the user asked for parallel work), so no dense time is valid for the clause; values are reported, not judged |
| "Mirage" family of methods | researched; **no-go** | `outputs/evidence/frames_depth/research_mirage.md` |
| Learned multi-view depth model (the route to a filled, dense look like the reference recording of an indoor dense reconstruction supplied by the user) | **BLOCKED** | installing external model code / weights was refused by the session's permission check; needs the user's approval. The user stated the project is personal and non-commercial, so non-commercial weights would be acceptable licence-wise; nothing was installed. |

### Path (clause 3) — F1b vs the fixed 3 fps baseline

`summary.json` `paths`; `sfm_seconds` is the sum of the build stages (SELECT_FRAMES + EXTRACT + MATCH + MAP + EXPORT) from `map_report.json`, measured under parallel load.

| Session | Selection | Frames | Registered | Max gap (s) | Reproj. (px) | Jumps (whole / first 10 s) | points3D | Build time (s) | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| `mqg_sumida_0025_0125` | fixed, cap 150 | 150 | 1.0 | 0.67 | 0.512 | 0 / 0 | 37,430 | 201.4 | GOOD (baseline) |
| `mqf_sumida_F1b` | adaptive F1b | 190 | 1.0 | **0.33** | **0.46** | 0 / 0 | 49,540 | 390.4 (SELECT 102.7) | GOOD — **PASS** clause 3 |
| `mqg_fsc_campus_aerial` | fixed, cap 150 | 150 | 1.0 | 0.67 | 0.708 | 0 / 0 | 96,344 | 213.5 | GOOD (baseline) |
| `mqf_fsc_F1b` | adaptive F1b | 213 | 1.0 | **0.33** | 0.685 | 0 / 0 | 123,871 | 548.4 (SELECT 171.0) | GOOD — **PASS** clause 3 |
| `mqg_utrecht_fps5` | fixed 5 fps | 298 | 1.0 | 0.6 | 0.517 | 43 / 34 | 81,007 | 270.7 | PARTIAL (baseline) |
| `mqf_utrecht_F1b` | adaptive F1b, 5 fps | 343 | 1.0 | 0.6 | 0.527 | **19 / 0** | 89,706 | 531.4 (SELECT 115.9) | PARTIAL — **PASS** clause 3 (jumps ≤ current); remaining 19 jumps all at t 49.0–51.9 s (factors 5.3–7.7, one 337x at 51.65–51.85 s) |

Selection logs (`selection.json`, candidate step 3 = 10 fps scan, `adaptive_base fixed`, thresholds overlap 0.6 / parallax 8 px / flow 40 px / slow 3 px / max gap 2 s): Sumida 720 candidates → 190 kept (base 172, flow 10, forced 7, first 1), 0 dropped by cap, 0 unusable spans; FSC 852 → 213 (base 209, forced 3, first 1); Utrecht 600 → 343 (base 295, parallax 27, flow 16, connector 2, overlap_no_intermediate 2, first 1), 9 blur-rejected, 2 unusable spans listed as time + cause (45.245–45.345 s tracked share 0.56 and 45.846–45.946 s 0.49, "no usable frame between"), never filled with synthetic frames. Features on sky / person pixels were ignored during tracking (`--selection-masks data/models/static/skywater_fp32.onnx`, MIT, `data/models/static/SOURCE.md`).

### Dense (clauses 1, 2, 4) — Sumida full clip

Per eval frame, `cov1px` on static pixels, from `summary.json` `runs.*.frames` (also `static_metrics_<run>.json`). V2 = S3r40 (`dense_static_v2`). D1 / D12 use V2's poses (judge: neutral maps available); F1b and F1b+D12 use the `mqf_sumida_F1b` poses (judge: own maps only, `neutral_fs: null`; for these the forced source frames are the eval frames themselves).

| Frame (role) | Class | V2 | D1 | D12 | F1b | F1b+D12 |
|---|---|---|---|---|---|---|
| f000240 (tuning) | floor / structure | 0.2369 / 0.8964 | 0.2417 / 0.9168 | 0.2625 / 0.9158 | 0.3069 / 0.9047 | **0.338** / **0.9188** |
| f000900 (tuning) | floor / structure | 0.1205 / 0.866 | 0.1272 / 0.8918 | 0.1531 / 0.8916 | 0.2156 / 0.8798 | **0.2479** / **0.8978** |
| f001560 (tuning) | floor / structure | 0.6816 / 0.6514 | 0.6897 / 0.6774 | 0.7037 / 0.6761 | 0.8201 / 0.7423 | **0.8456** / **0.7662** |
| f000600 (held-out) | floor / structure | 0.145 / 0.8235 | 0.1509 / 0.8536 | 0.1569 / 0.8528 | 0.2111 / 0.8332 | **0.2297** / **0.8579** |
| f001260 (held-out) | floor / structure | 0.4522 / 0.5513 | 0.4684 / 0.5805 | 0.4925 / 0.5788 | 0.5223 / 0.5732 | **0.5725** / **0.5911** |
| f001080 (fast, 36 s) | floor / structure | 0.0536 / 0.6687 | 0.069 / 0.7213 | 0.0968 / 0.7209 | 0.0613 / 0.68 | **0.1061** / 0.7135 |
| f001680 (fast, 56 s) | floor / structure | 0.228 / 0.5386 | 0.2368 / 0.5782 | 0.2481 / 0.5744 | 0.3258 / 0.6401 | **0.3604** / **0.6564** |
| f001560 | `sky_false` | 0.03944 | 0.04509 (+0.57 pp) | 0.04363 (+0.42 pp) | 0.04548 (+0.60 pp) | 0.0477 (+0.83 pp) |
| f001260 | person back / surface / front | 1,203 / 0 / 3 | 1,367 / 0 / 0 | 1,417 / 0 / 0 | 3,836 / 0 / 0 | 3,921 / 0 / 0 |
| f001680 | person back / surface / front | 1,431 / 579 / 271 | 1,617 / 612 / 265 | 1,608 / 615 / 273 | 3,676 / 4 / 1 | 3,952 / 4 / 1 |
| whole run | `float_fs` own (neutral) | 0.3627 (0.3551) | 0.3866 (0.3582) | 0.3863 (0.3563) | 0.4538 (—) | 0.4583 (—) |
| whole run | `float_knn` / `inconsist` own | 0.1024 / 0.1074 | 0.1041 / 0.1134 | 0.1033 / 0.1045 | 0.1364 / 0.103 | 0.1348 / 0.0998 |
| whole run | views verified / reference | 143 / 147 | 146 / 147 | 145 / 147 | 162 / 174 | 168 / 179 |
| whole run | points | 1,530,092 | 1,637,597 | 1,653,054 | 2,153,998 | 2,344,311 |
| whole run | seconds (parallel load) | 221.3 | 382.2 | 508.2 | 331.0 | 597.9 |

Supplementary free-space rates (`fs_rates.json`, 300k random points, each run against its own depth maps): V2 0.0667 violations per (point, view) check and 0.012 of points violated in the majority of their checking views (145 views, 20.6 checks per point); D1 0.0682 / 0.0129 (146 views, 21.4); F1b 0.088 / 0.0332 (169 views, 23.7).

Segments for the record (`runs.*_seg*`; floor / structure of the segment's frame, seconds): t 4–14 s f000240 V2 0.2309 / 0.6714 (47.1 s), D1 0.2342 / 0.6853 (99.3 s), D12 0.2584 / 0.6825 (85.8 s); t 26–34 s f000900 V2 0.1094 / 0.6357 (33.2 s), D1 0.113 / 0.6653 (48.4 s), D12 0.1395 / 0.6609 (69.3 s); t 33–39 s f001080 V2 0.009 / 0.0441 with 4 of 16 views verified and 5,352 points (21.3 s), D1 0.0217 / 0.0965 with 9 of 16 views and 25,043 points (37.1 s); t 48–56 s f001560 V2 0.5945 / 0.5389 (27.8 s), D1 0.6093 / 0.5734 (39.2 s), D12 0.6246 / 0.5723 (41.8 s).

### Dense — FSC (regions `regions_fsc_v1.json`, poses of `mqg_fsc_campus_aerial` for V2 / D1, `mqf_fsc_F1b` for F1b)

| Frame (role) | V2: floor / structure / far | D1 | F1b |
|---|---|---|---|
| f000320 (tuning) | 0.9402 / 0.8232 / 0.6429 | 0.9426 / 0.8289 / 0.66 | 0.9436 / 0.8307 / 0.6319 |
| f001000 (tuning) | 0.7639 / 0.9545 / 0.7723 | 0.7639 / 0.9536 / 0.7729 | 0.7833 / 0.9626 / 0.7754 |
| f001720 (held-out) | 0.7459 / 0.6874 / 0.7951 | 0.7467 / 0.6888 / 0.7964 | 0.7648 / 0.7032 / 0.7834 |
| `float_fs` own (neutral) / `inconsist` / `float_knn` | 0.1777 (0.1932) / 0.0435 / 0.0542 | 0.1806 (0.1982) / 0.0432 / 0.0543 | 0.1896 (—) / 0.0248 / 0.0616 |
| views / points / seconds | 149 of 149 / 6,444,805 / 244.7 | 150 of 150 / 6,493,418 / 481.0 | 200 of 200 / 8,577,524 / 577.8 |

### Verdicts as recorded (`summary.json` `verdicts`)

| Change | Clause 1 coverage | Clause 2 held-out | Clause 3 path | Clause 4 errors | Clause 5 time | Verdict |
|---|---|---|---|---|---|---|
| F1 | — | — | — | — | — | rejected before SfM (92 frames) |
| F1b (Sumida) | floor up strongly on every frame; fast f001080 floor +0.8 pp (< +3 pp) | no regression | **PASS** (max gap 0.67 → 0.33 s, reproj 0.51 → 0.46 px, 0 jumps) | **FAIL**: own `float_fs` 0.363 → 0.454, per-check rate 6.7 → 8.8 %, `float_knn` +3.4 pp, `sky_false` +0.60 pp on f001560 | UNMEASURED_QUIET | **FAIL as dense default, PASS on path** |
| F1b (FSC) | floor / structure +0.4 … +1.9 pp; far −1.1 / +0.3 / −1.2 pp (clause 1 needs ≥ V2 on every frame) | far −1.2 pp on f001720 | **PASS** (max gap 0.67 → 0.33 s) | own `float_fs` +1.2 pp | UNMEASURED_QUIET | **FAIL** |
| F1b (Utrecht) | path only | — | **PASS** (jumps 43 → 19, first 10 s 34 → 0) | — | — | path only |
| D1 | structure +2.0 … +5.3 pp on all 7 frames; floor +0.5 … +1.6 pp | no regression | same poses | **FAIL** as recorded: own `float_fs` +2.4 pp; `sky_false` +0.57 pp on f001560; person on f001680 877 vs 850 | UNMEASURED_QUIET | **FAIL** |
| D1 (FSC) | about no change (≤ +1.7 pp) | — | same poses | own +0.3 pp, neutral +0.5 pp | UNMEASURED_QUIET | no change |
| D12 = D1 + coarse ground | floor +1.2 … +4.3 pp, structure +1.9 … +5.2 pp on all 7 frames; `inconsist` 0.107 → 0.105 | no regression | same poses | **FAIL** as recorded ("same failing clauses as D1"): own `float_fs` +2.4 pp; `sky_false` on f001560 reads +0.42 pp in `summary.json` while the layer reason says +0.5 pp (see Deviations) | UNMEASURED_QUIET | **FAIL** |
| F1b + D12 | best coverage of all runs: floor 0.24 / 0.12 / 0.68 → 0.34 / 0.25 / 0.85 (tuning), 0.15 / 0.45 → 0.23 / 0.57 (held-out), 0.05 / 0.23 → 0.11 / 0.36 (fast) | no regression | as F1b | **FAIL**: own `float_fs` 0.363 → 0.458, `float_knn` +3.2 pp, `sky_false` +0.83 pp on f001560 | UNMEASURED_QUIET | **FAIL**; installed as trial v3 in `mqf_sumida_F1b` |

Reading of the causes (section 1 confirmed or corrected): the 36 s loss is a neighbour-selection effect of turning nearly in place (fixed by far neighbours, D1), not a frame-sampling effect; the 15–30 s loss is the open-park baseline / median-depth ratio (0.018–0.037, below the 0.05 floor), again neighbour selection, not frame selection; the 0.67 s gaps were the uniform cap, removed by F1b. Denser frames (F1b) give more floor and structure on every frame but also more points that other views see past (free-space), and the far-neighbour rule does the same on fixed poses; under the frozen clauses no change passes as a default.

### Installed trial layers (`summary.json` `layers`; label `DENSE_STATIC_TRIAL_V3 (relative scale) - not adopted`, `experimental.status` "NOT ADOPTED", `run`, `plan`, `reason`)

| Session | `dense_static_v3/` content | Points | Console option |
|---|---|---|---|
| `mqg_sumida_0025_0125` | D12 (`neighbour_rule median_then_near_far`, `coarse_ground true`, V2 poses) | 1,653,054 | `Dense · static scene v3 (trial)` |
| `mqf_sumida_F1b` | F1b + D12 (same dense config on the F1b poses) | 2,344,311 | same |
| `mqf_fsc_F1b` | F1b (S3r40 dense config, `median_then_near`, no coarse ground) | 8,577,524 | same |

`mqf_utrecht_F1b` has a sparse map only (path comparison); no dense layer. Same-view renders for the seven Sumida frames: `outputs/evidence/frames_depth/sameview_<frame>_v2_d12_f1bd12.jpg` — panels source / V2 / D12 / F1b+D12, each cloud seen from its own model's camera for that source frame.

### Deviations from sections 2–6

1. **F1b is not in section 4.** F1 as specified kept 92 frames; instead of the fallback F0 (parallax-ranked cap) the implementer added `adaptive_base fixed`: the fixed 3 fps frames are always kept, the uniform `linspace` cap is dropped, and connector / flow frames are added where tracking asks for them. Forced frames and connectors are never dropped by the cap (`max_frames` 260 / 400 was not reached).
2. **D2 variant.** Section 4 offered coarse-to-fine or local planes; what ran is `coarse_ground` = half-resolution sweep of the ground rows below the horizon (also a speed measure), with `tests/test_dense.py::test_ground_rows_crop_gives_identical_result` asserting equality with the uncropped result. The `floor_on_wall` boundary check of section 4 was not computed.
3. **Combination order.** Section 3 combines only after each part passes alone; F1b + D12 was run with both parts FAIL so that the best-looking cloud exists on disk with its numbers. Its verdict is FAIL and it is a labelled trial only.
4. **Judge for D runs.** Section 2 assigns the neutral depth maps to `float_fs` / `inconsist` of D runs. The recorded D1 / D12 verdicts cite the own-map free-space (+2.4 pp); the neutral values in `summary.json` are V2 0.3551 → D1 0.3582 (+0.3 pp) → D12 0.3563 (+0.1 pp), which would be within clause 4's +1 pp. This needs a ruling before the D1 / D12 FAIL is treated as final; nothing has been adopted either way.
5. **D12 sky on f001560.** `summary.json` gives 0.03944 → 0.04363 (+0.42 pp, inside the +0.5 pp limit); the installed layer's `reason` says "+0.5 pp". Same ruling as item 4.
6. **Eval-frame files.** One file `regions_sumida_v3.json` (all seven frames) replaced "`regions_sumida_v2.json` unchanged plus `regions_sumida_fast.json`"; its sha256 is not recorded in `summary.json`. FSC uses `regions_fsc_v1.json` with classes floor / structure / far.
7. **Evidence layout.** Summary at `outputs/evidence/frames_depth/summary.json` (not appended to `outputs/evidence/static_map/summary.json`); `<run>.cmd` files exist for the six V2 / D1 segment runs only, the other runs have `<run>.log` / `<run>.eval.log` and the `config` block of `dense_report.json`; `neighbours.json` and `path_metrics_<run>.json` were not written (path metrics are in `summary.json` `paths` and the `map_report.json` of each session).
8. **Time.** Clause 5 could not be judged (parallel load); all times are reported as measured.

### Ruling (2026-11-09, after the Result above; `summary.json` `verdicts` updated, D12 layer `reason` corrected)

Section 2 governs: D runs are judged on the **neutral** depth maps. Deviations 4 and 5 are resolved as follows; the earlier text is left as written.

- **D12** — clause 1 PASS (floor and structure ≥ V2 on all 7 frames; fast frames structure +5.2 / +3.6 pp ≥ +3 pp); clause 2 PASS; clause 4: `float_fs` neutral 0.3551 → 0.3563 (+0.1 pp) PASS, `inconsist` 0.1099 → 0.1047 PASS, `sky_false` max +0.42 pp (f001560) PASS, **person front + surface on f001680 850 → 888 FAIL**; clause 5: **400.0 s FAIL** (> 332 s), measured with no other dense job running (`outputs/experiments/frames_depth/D12_full_timing.log`, output byte-identical to `D12_full`). **Not adopted** (two clauses). The old layer reason's "+0.5 pp sky" was wrong (+0.42 pp); the installed `dense_report.json` reason now reads correctly.
- **D1** — `float_fs` neutral +0.3 pp PASS; **FAIL**: `sky_false` f001560 +0.57 pp, person f001680 850 → 877; time not measured quietly (382.2 s under load).
- **Time clause** — the only quiet measurement is D12 (400.0 s); every other full-clip time stays UNMEASURED_QUIET.
- **Learned multi-view depth** — no longer BLOCKED: installation of a model is **approved by the user and in progress** in a separate environment (`outputs/tools/`); results will be documented in a later round. (`summary.json` still carries the earlier BLOCKED wording at the time of writing.)

### Next tests

- Time V2_full and the remaining runs alone on an idle machine (budget 332 s Sumida / 367 s FSC); D12 is settled at 400.0 s.
- D12's remaining clause-4 miss is 38 person-box points on f001680 (surface 615 + front 273 vs V2 579 + 271); a per-view check of which reference views place them would say whether the far-neighbour rule or the coarse ground pass adds them.
- For F1b, the free-space rise is the open question: a same-view-set comparison (F1b cloud checked against V2's 145 maps and vice versa) would separate "more points seen past" from "more views checking".
- Learned multi-view depth: approved by the user, installation in progress (see Ruling); judged later with the same seven Sumida eval frames, never labelled metric.
