# Plan: learned feed-forward depth as a verified addition to the static map

Status: PLAN (2026-11-09). Planning only; the full runs in section 4 have not been executed. Scope: camera-only static 3D map, relative scale, NOT_NAVIGATION_SAFE. Sky and people stay excluded. The learned model is an external estimator; the map keeps our SfM poses and our verification rules.

**Goal.** Fill the near floor where plane-sweep MVS leaves holes, without letting model-guessed geometry (far structures, sky, people) into the map layer. Anything not verified by our own independent checks is shown separately and labelled as model-estimated.

## 1. Model and environment (measured, 2026-11-09)

- Model: a LingBot-Map checkout supplied by the user (Apache-2.0, streaming feed-forward reconstruction; code at commit 1f480ae, weights `lingbot-map.pt`, sha256 prefix `ee66510`, identical to the current upstream file). Per the user's request the supplied checkout was used only for a check run; the real work runs on the latest upstream code `b9dd1b1` (2026-10-14: documentation fix stating the decoded pose is camera-to-world, sky-mask fixes). Weights are the same file in both cases.
- Environment: separate `outputs/tools/venv_learned` (torch 2.15, MPS). Never the project `.venv`; not committed. Weights and checkout are not committed.
- Runner `packages/learned/run_lingbot_map.py`, Mac changes: rotary frequency tables complex128 -> complex64 (MPS has no float64/complex128); SDPA attention instead of FlashInfer; decoded pose kept as camera-to-world.
- Check run (Sumida t 4–14 s, 26 frames, MPS): supplied checkout 26.7 s/frame, latest code 15.3 s/frame, identical depth output.

## 2. Alignment and what the model is right about (measured on the check run)

- `packages/mapping/learned_align.py`: one Sim3 from the model's camera centres to OUR SfM centres. Our path is never changed. Residual 0.4 % of path length; scale 2.09–2.11; model focal / ours 1.02.
- Learned depth vs our triangulated sparse points (10 % tolerance), by sparse depth: 0–1: 98 %; 1–2: 91 %; 2–4: 72 %; 4–8: 16 % (ratio 0.79); > 8: 7 % (ratio 0.22). Reading: accurate on the near floor, wrong on far structures (depth compressed). This is why the fusion is distance-gated per frame instead of using the whole depth map.

## 3. Fusion rule (`packages/mapping/learned_fuse.py`)

Stated openly: this rule was chosen AFTER the check run, so the f000240 segment result below is a tuning result, not a test. The full runs in section 4 are the test; the rule is frozen now.

1. Per frame, trust distance = far edge of the last contiguous sparse-depth quintile (near -> far) in which ≥ 80 % of our triangulated points match the learned depth within 10 %. Learned pixels beyond it are not used.
2. Sky and person keep-masks applied; model confidence ≥ 1.5 required.
3. A learned point enters the map layer only if (a) ≥ 2 other frames' learned depth, each within its own trust range, agree within 5 %, AND (b) no plane-sweep MVS depth map sees past it (free space > 10 % is a veto).
4. Everything else goes to a separate MODEL_ESTIMATED cloud that is never part of the map layer.

Caveat: learned depths of different frames come from one joint model run, so their mutual agreement (3a) is not independent evidence. The independent checks are the sparse-point trust range (1) and the MVS free-space veto (3b).

**Segment result, f000240** (same frozen regions and judges as `docs/PLAN_FRAMES_AND_DEPTH.md`), D12 vs D12 + learned verified: floor cov1px 0.258 -> 0.431; on-ground floor 0.245 -> 0.421; off-ground floor points 5.4 % -> 2.3 %; floor_far 0.553 -> 0.635; structure 0.682 -> 0.682; sky points 1.6 % -> 1.25 %; free-space (neutral judge) 0.403 -> 0.333; isolated points 0.134 -> 0.098.

## 4. Frozen full runs and acceptance

**Runs.** L1: Sumida full clip, latest code, D12 poses and frame set, fusion rule of section 3 unchanged. L2: FSC, same. Learned inference external (MPS, per frame time logged); alignment, fusion, evaluation are our part.

**Eval frames.** Sumida: the 7 frames of `regions_sumida_v3.json` (tuning f000240 / f000900 / f001560, held-out f000600 / f001260, fast frames t ≈ 36 s / 56 s); FSC: the 3 frames of `regions_fsc_v1.json`. Judge: neutral depth maps for Sumida; FSC own maps (held-out views). Baseline: D12 with the same poses.

| Clause | PASS |
|---|---|
| 1 coverage | floor and structure `cov1px` ≥ D12 on every eval frame; floor ≥ +5 pp on at least the tuning frames |
| 2 errors | free-space (neutral for Sumida, own for FSC) and isolated points not up by > 1 pp |
| 3 sky / people | sky points ≤ D12 + 0.5 pp; person front + surface points ≤ D12 |
| 4 separation | zero MODEL_ESTIMATED points in the map layer (checked by point provenance tags in the output, count must be 0) |
| 5 time | learned-model time reported separately (external, MPS), not judged; the dense time clause of `PLAN_FRAMES_AND_DEPTH.md` applies to our part only (alignment + fusion + dense) |

Evidence per run: `outputs/experiments/learned_depth/<run>/` with `align.json` (Sim3, residual, scale, focal ratio), `trust.json` (per-frame trust distance and quintile table), `fuse_report.json` (points accepted / rejected by rule 1, 2, 3a, 3b), `static_metrics_<run>.json`, `<run>.cmd`, `<run>.log`, learned per-frame timing. Summary row appended to `outputs/evidence/static_map/summary.json` under `learned_depth`. Point clouds: map layer and MODEL_ESTIMATED as separate PLY files with distinct names.

## 5. Stop rule, fallback, app

- A run that fails any clause stays a trial layer with the failing clause in its row; nothing is installed as default. App layers: "Dense · with learned depth (trial)" (map layer with verified learned points) and "Model estimated (not verified)" (the MODEL_ESTIMATED cloud, off by default, labelled as not verified). Adoption of the trial layer as default is a user decision after both L1 and L2 pass.
- Blocked path: if the latest code cannot run on MPS for the full clip (memory, > 60 s/frame sustained, crash), mark BLOCKED with the log, run L1 on the longest contiguous segment that completes, and report it as a segment result only; FSC continues independently.
- Not in scope: using the model's poses or intrinsics for the map, metric scale, navigation or obstacle avoidance claims, any dense output from the model beyond the gated depth.

## Amendment A1 (2026-11-09, before the full-clip judgement and before any far-cleanup run)

**Why.** The unchunked full-clip run (150 frames in one process) was stopped by the session for low memory on the 16 GB Mac. The 0–10 s and 4–14 s results remain short-segment results; the full-clip judgement of section 4 has not happened.

**Resume protocol** (`packages/learned/chunked_run.py`). One chunk per separate process. Verified first chunk 0–10 s (26 frames) is reused only after checking frame list, weight hash, output files and alignment residual ≤ 2 % of path length; later chunks are 8 s with 2 s overlap. A chunk is marked done (`chunk_done.json`) only after those checks; failed or interrupted output goes to `outputs/experiments/learned_depth/incomplete/`. No chunk starts when macOS free memory < 35 %. MPS high-watermark setting stays at its default. Inference first, then fusion, strictly in sequence. Runner changes: the 4 GB checkpoint is released after loading; model and MPS cache are released after inference; memory per stage is logged (peak process RSS, MPS current/driver allocation, macOS free %, swap). Measured first new chunk 8–16 s: 21 frames, 6.2 s/frame, peak RSS 5.0 GB, MPS driver peak 12.56 GB, free memory 62 % -> 29 % at save -> 73 % after exit, swap 9.0 -> 15.4 GB.

**Precision.** fp16/bf16 is tried only if the default precision fails at a safe chunk size, on the 4–14 s comparison segment, and continued only if peak memory actually falls and the coverage/error clauses of section 4 pass.

**Chunk joining** (`packages/learned/merge_chunks.py`). Neighbouring chunks are compared on their overlapping frames after alignment. A chunk is HELD (not fused) when the median relative depth difference > 5 % or the median scale ratio is off by > 5 %. Each frame is used once, from the chunk whose centre is closest. Boundary quality is reported per chunk pair (overlap depth difference, scale ratio).

**Far cleanup** (`packages/mapping/far_cleanup.py`), a separate variant judged on its own. Points farther than 2 x the median point-to-camera distance are removed when fewer than 2 plane-sweep depth maps agree within 10 %, or when more maps see past them (free space) than agree. Near points untouched; removed points kept in a separate file. Variants compared on the same poses and fields of view: D12 / D12 + learned / D12 + learned + far cleanup, Sumida 7 frames and FSC 3 frames, with the section 4 clauses plus: far cleanup must not lower structure `cov1px` by more than 1 pp on any frame (multi-view-supported far structures such as the railway bridge, trees and buildings must stay); removed-point counts reported.

**Comparison layer only.** A nearby-scene layer cut by distance from the camera path (`packages/mapping/local_view.py`, from a user-supplied patch) is kept for comparison; an earlier check showed the distance-only cut lowers structure coverage at t = 30 s from 85.1 % to 70.0 %, so it is never a candidate for the map.

**App.** Area-of-interest view (Nearby, key N: only points near the current camera, with an overview map from above showing the area) and a per-frame learned-depth state lane on the same time axis as the frame slider. Static camera-based maps only; MOCK / DRY_RUN unchanged.

## Result (2026-11-09, after the runs of section 4 under Amendment A1)

Status: **FAIL as recorded on both clips** (one clause each); learned layers installed as labelled trials; **default dense layer unchanged**. Numbers below come from `outputs/evidence/learned_depth/summary.json`, `static_metrics_<run>.json`, `outputs/experiments/learned_depth/chunks_{sumida,fsc}.json` and `lingbot_v2_{sumida,fsc}_merged/run_report.json`. Scale is RELATIVE everywhere; no distance carries a unit; nothing here is navigation-safe.

### R1. Model, code and environment as actually run

- The checkout supplied by the user (code 1f480ae, weights `lingbot-map.pt`) was used **only for the check run** of section 1 (Sumida t 4–14 s, 26 frames: 26.7 s/frame vs 15.3 s/frame on the newer code, identical depth output). Every chunk run records `code_commit b9dd1b1de535e6d36e777fb42b8433c781bfd783` and `weights_sha256 ee665103348e07e6…cd72` (same weight file), `device mps`, decoded pose used as camera-to-world (`pose_convention_used: c2w`; the c2w fit beats the inverse on both clips: Sumida 0.0097 vs 0.0195 of path length, FSC 0.0078 vs 0.0091).
- Environment `outputs/tools/venv_learned` (torch, MPS), separate from the project `.venv`; the model is run as an external tool in its own process by `packages/learned/run_lingbot_map.py`. Checkout, weights and environment are not committed. Mac changes of section 1 (complex64 rotary tables, SDPA attention, `PYTORCH_ENABLE_MPS_FALLBACK=1`) stayed as described; A1 runner changes (checkpoint released after loading, model and MPS cache released after inference, per-stage memory log) are visible in the `memory` arrays of `chunks_*.json`.
- The 4–14 s check (floor `cov1px` 0.258 → 0.431, free-space 0.403 → 0.333) remains a **segment result** and is not the verdict.

### R2. Memory and the resume protocol (A1)

- The unchunked 150-frame run was stopped by the session for low memory (`outputs/experiments/learned_depth/lingbot_v2_full.log`); nothing from it is used (`docs/FAILURES.md` F-27).
- Chunked runs: **peak process RSS 9.02 GB** (FSC chunk 36–44), **MPS driver peak 12.56 GB** (every Sumida chunk up to 50–58) and 11.49 GB (Sumida 56–60 and all FSC chunks). Chunks started only at ≥ 35 % free memory (observed 62–76 % at start); macOS free memory fell to 4–24 % at `inference_done` and recovered to 73–77 % after the MPS release; swap 9–15 GB throughout, attributed to other processes. The first Sumida chunk (0–10 s, 26 frames, 19.3 s/frame) predates the memory logging and has no peak values.
- fp16/bf16 (A1 "Precision") was **not tried**: the default precision completed every chunk.

### R3. Chunk runs and joining (per-chunk status = `chunked_run` verification; join status = `merge_chunks` against the preceding chunk on their overlapping frames; thresholds 5 % median relative depth difference on the nearest 40 % of depths, 5 % scale ratio, 5 % near-point scale check)

Sumida (`outputs/maps/mqg_sumida_0025_0125`, plan `--t-end 60 --first 0 10`, chunk 8 s / overlap 2 s):

| Chunk (s) | Frames | s/frame | Peak RSS GB | MPS driver GB | Alignment residual (of path) | Chunk | Join | Overlap diff / scale ratio | Near-point scale |
|---|---|---|---|---|---|---|---|---|---|
| 0–10 | 26 | 19.298 | — | — | 0.0097 | DONE | **used** | — | — |
| 8–16 | 21 | 6.242 | 5.01 | 12.56 | 0.0047 | DONE | HELD_OVERLAP_DISAGREES | 0.0743 / 0.9259 | 0.9947 |
| 14–22 | 21 | 4.345 | 5.52 | 12.56 | 0.0046 | DONE | HELD_OVERLAP_DISAGREES | 0.0892 / 0.9108 | 1.0172 |
| 20–28 | 21 | 0.968 | 8.44 | 12.56 | 0.0075 | DONE | **used** | 0.0471 / 0.9529 | 1.0042 |
| 26–34 | 21 | 3.021 | 4.06 | 12.56 | 0.0118 | DONE | HELD_SCALE_VS_TRIANGULATION | 0.0143 / 0.9997 | **1.1794** |
| 32–40 | 21 | 4.499 | 4.33 | 12.56 | 0.0137 | DONE | HELD_SCALE_VS_TRIANGULATION | 0.0272 / 1.0267 | **2.2161** |
| 38–46 | 21 | 2.300 | 8.33 | 12.56 | 0.0077 | DONE | **used** | 0.0476 / 1.0415 | 1.0163 |
| 44–52 | 21 | 2.282 | 8.84 | 12.56 | 0.0091 | DONE | HELD_OVERLAP_DISAGREES | 0.0869 / 1.0868 | 0.9895 |
| 50–58 | 21 | 4.781 | 7.57 | 12.56 | **0.0612** | **REJECTED** (> 0.02) | — | — | — |
| 56–60 | 10 | 0.839 | 7.12 | 11.49 | **0.0503** | **REJECTED** (> 0.02) | — | — | — |

Merged Sumida input: **68 frames** from chunks 0–10, 20–28, 38–46 s (`lingbot_v2_sumida_merged/run_report.json`; merged Sim3 scale 1.709, model focal / ours 0.976, sparse agreement median 0.376). Learned depth therefore exists for **0–10, 20–28 and 38–46 s only**; 10–20 s and 46–52 s are held by overlap disagreement (7.4–8.9 %), **26–40 s** by the triangulation scale check (1.18 / 2.22), **50–60 s** rejected by the alignment residual (6.1 % / 5.0 % of path length on the fast turn).

FSC (`outputs/maps/mqg_fsc_campus_aerial`, `--t-end 71`, chunk 8 s / overlap 2 s):

| Chunk (s) | Frames | s/frame | Peak RSS GB | MPS driver GB | Alignment residual | Chunk | Join | Overlap diff / scale ratio | Near-point scale |
|---|---|---|---|---|---|---|---|---|---|
| 0–8 | 16 | 1.020 | 4.97 | 11.49 | 0.0078 | DONE | **used** | — | — |
| 6–14 | 17 | 0.880 | 6.53 | 11.49 | 0.0052 | DONE | **used** | 0.0374 / 0.9632 | 0.9640 |
| 12–20 | 17 | 0.822 | 8.06 | 11.49 | 0.0084 | DONE | **used** | 0.0283 / 0.9741 | 0.9665 |
| 18–26 | 17 | 0.909 | 7.85 | 11.49 | 0.0059 | DONE | **used** | 0.0373 / 0.9628 | 0.9774 |
| 24–32 | 17 | 0.967 | 4.13 | 11.49 | 0.0050 | DONE | HELD_OVERLAP_DISAGREES | 0.0523 / 0.9477 | 1.0208 |
| 30–38 | 17 | 0.846 | 7.83 | 11.49 | 0.0052 | DONE | **used** | 0.0328 / 1.0322 | 0.9532 |
| 36–44 | 17 | 0.862 | **9.02** | 11.49 | 0.0046 | DONE | HELD_OVERLAP_DISAGREES | 0.1041 / 0.8960 | 1.0449 |
| 42–50 | 17 | 0.817 | 8.36 | 11.49 | 0.0097 | DONE | HELD_OVERLAP_DISAGREES | 0.0548 / 1.0541 | 0.9869 |
| 48–56 | 17 | 0.857 | 5.66 | 11.49 | 0.0095 | DONE | HELD_SCALE_VS_TRIANGULATION | 0.0281 / 0.9864 | **0.9447** |
| 54–62 | 17 | 0.835 | 8.95 | 11.49 | 0.0067 | DONE | HELD_OVERLAP_DISAGREES | 0.1199 / 0.8808 | 1.0065 |
| 60–68 | 16 | 0.838 | 7.90 | 11.49 | 0.0099 | DONE | **used** | 0.0201 / 0.9819 | 0.9813 |
| 66–71 | 11 | 0.838 | 4.22 | 11.49 | 0.0083 | DONE | HELD_OVERLAP_DISAGREES | 0.0535 / 0.9476 | 1.0469 |

All **12 of 12** FSC chunks completed; **6 accepted**. Merged FSC input: **88 frames** covering 0–26, 30–38 and 60–68 s (merged Sim3 scale 4.577, focal ratio 0.991, sparse agreement median 0.854). Empty of learned depth: 26–30, 38–60 and 68–71 s (held chunks 24–32, 36–44, 42–50, 48–56, 54–62, 66–71).

**Scale refinement, tried and rejected.** Rescaling a chunk's depth by the near-triangulated-point factor (the quantity reported as `near_point_scale_check`) was tried and made the fast-turn chunks worse, so it was dropped (implementer-reported; no metrics of the rejected variant were archived). The factor is kept only as a diagnostic and as the A1 hold rule (`HELD_SCALE_VS_TRIANGULATION`).

### R4. Measured results (frozen regions, 1 px points at 640 px; Sumida judged on the neutral depth maps, FSC on its own maps)

Sumida, 7 frames (`regions_sumida_v3.json`), D12 = `dense_static_v3` baseline on the same poses; D12L = D12 + verified learned points; D12LFC = D12L + far cleanup; NEARBY = distance cut of D12LFC (comparison only):

| Frame (t) | Role | Floor `cov1px` D12 → D12L → D12LFC (NEARBY) | Structure D12 → D12L → D12LFC (NEARBY) | `sky_false` D12 → D12L → D12LFC | Person front+surface |
|---|---|---|---|---|---|
| f000240 (8 s) | tuning | 0.2625 → **0.4305** → 0.4304 (0.4294) | 0.9158 → 0.9182 → 0.9134 (0.4807) | 0.01399 → 0.0118 → 0.01192 | 0 |
| f000900 (30 s) | tuning | 0.1531 → 0.1531 → 0.1531 (0.1526) | 0.8916 → 0.8916 → 0.8818 (0.0511) | 0.04432 → 0.04421 → 0.04331 | 0 |
| f001560 (52 s) | tuning | 0.7037 → 0.7038 → 0.7038 (0.7038) | 0.6761 → 0.6761 → 0.6744 (0.5754) | 0.04363 → 0.04363 → 0.04368 | 0 |
| f000600 (20 s) | held-out | 0.1569 → **0.2752** → 0.2752 (0.2739) | 0.8528 → 0.853 → 0.8444 (0.179); wall 0.8265 → 0.8265 → 0.8202 | 0.01587 → 0.01479 → 0.01467 | 0 |
| f001260 (42 s) | held-out | 0.4925 → **0.7153** → 0.7153 (0.7151) | 0.5788 → 0.6678 → 0.6653 (0.6051); wall 0.1429 → 0.1429 → 0.1419 | 0.06782 → 0.05876 → 0.05926 | 0 |
| f001080 (36 s) | fast | 0.0968 → 0.1031 → 0.1031 (0.1001) | 0.7209 → 0.724 → 0.7144 (0.1776) | 0.01806 → 0.01814 → 0.017 | 0 |
| f001680 (56 s) | fast | 0.2481 → 0.2481 → 0.2481 (0.2474) | 0.5744 → 0.5744 → 0.5713 (0.5353) | 0.1339 → 0.1339 → 0.13489 | 888 → 888 → 888 |

Whole-cloud, Sumida: points 1,653,054 → 1,936,318 → 1,909,684 (NEARBY 1,363,015); free-space violations (neutral judge) **0.3563 → 0.3295 → 0.3207** (NEARBY 0.2838); isolated points `float_knn` **0.1033 → 0.0882 → 0.085** (NEARBY 0.0342). `floor_far`: 0.7617 → 0.7629, 0.5317 → 0.5317 → 0.5287, 0.6174, 0.4703 → 0.5711, 0.5905 → 0.6393 → 0.6386, 0.4532 → 0.4763 → 0.4762.

FSC, 3 frames (`regions_fsc_v1.json`), D1 = baseline on the same poses; D1L = D1 + verified learned points; D1LFC = D1L + far cleanup (judged, **not installed**):

| Frame | Role | Floor D1 → D1L → D1LFC | Structure D1 → D1L → D1LFC | Far D1 → D1L → D1LFC | `sky_false` (all three) |
|---|---|---|---|---|---|
| f000320 | tuning | 0.9426 → **0.9841** (+4.1 pp) → 0.9807 | 0.8289 → **0.979** → 0.9782 | 0.66 → 0.6615 → 0.6574 | 1e-05 |
| f001000 | tuning | 0.7639 → **0.9569** (+19.3 pp) → 0.9569 | 0.9536 → 0.9744 → 0.9731 | 0.7729 → 0.7761 → 0.7726 | 0.00023 / 0.00022 / 0.00022 |
| f001720 | held-out | 0.7467 → 0.7901 (+4.3 pp) → **0.7622** | 0.6888 → 0.7502 → **0.7298** | 0.7964 → 0.7972 → 0.7949 | 6e-05 / 5e-05 / 5e-05 |

Whole-cloud, FSC: points 6,493,418 → 8,234,561 → 8,192,708; free-space (field `float_fs_neutral` in `summary.json`) **0.1982 → 0.1691** → 0.1668; `float_knn` 0.0543 → 0.0428 → 0.0415; person points 0 on all frames.

### R5. Verdicts, clause by clause (section 4 and A1; one clause missed = FAIL)

| Run | Clause 1 coverage | Clause 2 errors | Clause 3 sky / people | Clause 4 separation | Clause 5 time | Verdict as recorded in `summary.json` |
|---|---|---|---|---|---|---|
| **Sumida D12 + learned** | floor and structure ≥ D12 on all 7 frames (PASS); floor +5 pp on the tuning frames: f000240 +16.8 pp PASS, **f000900 (30 s) +0.0 pp FAIL, f001560 (52 s) +0.0 pp FAIL** — both lie in spans without accepted chunks (26–40 s held, 50–60 s rejected); held-out +11.8 / +22.3 pp | free-space neutral 0.3563 → 0.3295, isolated 0.1033 → 0.0882, both down (PASS) | `sky_false` ≤ D12 + 0.5 pp on every frame (largest rise +0.008 pp on f001080); person 888 = 888 (PASS) | MODEL_ESTIMATED written as a separate cloud (`model_estimated/`), never in the map layer by construction; the provenance-tag **count of 0 in the map PLY was not archived** (see R7) | learned time reported per chunk (R3); our part (align + fuse + dense) **not archived** | **FAIL** — "FAIL: floor +5 pp clause at 30 s and 52 s (chunks 26-40 held by the triangulation scale check, 50-60 rejected by alignment residual)"; coverage and all error measures improved |
| **Sumida far cleanup** (D12LFC vs D12L) | structure change −0.2 … −1.0 pp on the 7 frames (f000900 −0.98, f001080 −0.96, f000600 −0.86; limit −1 pp) PASS; floor unchanged | free-space 0.3295 → 0.3207, isolated 0.0882 → 0.085 (PASS) | sky within +0.5 pp; person unchanged | removed points kept in a separate file | — | **PASS its clause** — 26,634 of 768,214 far points removed, 741,580 kept; **applied in the Sumida layer** |
| **FSC D1 + learned** | floor +4.1 / +19.3 / +4.3 pp, structure +15.0 / +2.0 / +6.1 pp, far ≥ D1 on all 3; **floor +5 pp on tuning f000320 missed (+4.1 pp, floor already 0.94)** | free-space 0.1982 → 0.1691, isolated 0.0543 → 0.0428 (PASS) | sky ≤ D1; person 0 (PASS) | separate `model_estimated/` cloud; count not archived | learned ≈ 0.8–1.0 s/frame per chunk; our part not archived | **FAIL** — "floor +5 pp on f000320 (+4.1, already 0.94)" |
| **FSC far cleanup** (D1LFC vs D1L) | held-out f001720 structure 0.7502 → 0.7298 (**−2.0 pp** > −1 pp limit) and floor 0.7901 → 0.7622 (**−2.8 pp**) | free-space 0.1691 → 0.1668 | — | — | — | **FAIL** → **not applied on FSC**; the FSC layer is D1 + learned only |
| Nearby distance cut | structure at 30 s 0.8818 → 0.0511, at 8 s 0.9134 → 0.4807 (verdict text: "0.882 -> 0.051 at 30 s, 0.913 -> 0.481 at 8 s"); floor about unchanged | free-space 0.2838, isolated 0.0342 (lower because far structure is removed, not because it is better) | — | — | — | **comparison only**, never a map candidate (A1) |
| Default | — | — | — | — | — | **unchanged**; learned layers are trials |

### R6. What is installed and shown

- `outputs/maps/mqg_sumida_0025_0125/dense_learned/` — label `DENSE + LEARNED DEPTH (verified) + FAR CLEANUP - trial`, 1,909,684 points (= D12LFC), `experimental.status "TRIAL, NOT ADOPTED"`, per-view `trust_depth`, quintile bands, `verified / contradicted_by_mvs / estimated_only` pixel counts, chunk table and far-cleanup counts in its `dense_report.json`. `model_estimated/` — `MODEL_ESTIMATED - learned depth NOT confirmed by multi-view geometry; hidden by default, never part of the map`. `dense_static_local/` — the distance-cut comparison layer (`packages/mapping/local_view.py`, from a user-supplied patch).
- `outputs/maps/mqg_fsc_campus_aerial/dense_learned/` — `DENSE + LEARNED DEPTH (verified) - trial`, 8,234,561 points (= D1L); `model_estimated/`.
- Console (`outputs/evidence/ui/ui_smoke.json`, 2026-11-09 09:36 UTC, attached to the user's open browser window, **88 / 88**, 0 console errors): layer options `Dense · with learned depth (trial)`, `Model estimated (not verified)` (never selected by default — checked), `Dense · nearby scene (comparison)`; chip `LAYER ▸ DENSE + LEARNED DEPTH · 1,909,684 pts in file` with the TRIAL, NOT ADOPTED reason; **Nearby** button (key N) with the overview map of the whole map and the current camera marked; learned-depth **state lane** under the coverage strip (`learned depth at t=0.0 s: used · trust range ≤ 3.548 · verified 8,134 of 19,565 px · bar: brighter = more verified, red = frame rejected, ticks = chunk starts`). Screenshot `outputs/evidence/ui/03h_learned_nearby_overview.png`. Default layer still `DENSE · VERIFIED · 917,498 pts in file`.
- Tests: `outputs/evidence/tests/pytest-20261109-183645.log` — **134 passed in 87.44 s** (+3 over 131: `tests/test_learned_chunks.py` 2 — chunk plan overlaps and covers the clip, plan without a first chunk starts at zero; `tests/test_dense.py::test_umeyama_recovers_similarity`).
- Same-view renders: `outputs/evidence/learned_depth/sameview_<frame>_D12LFC_full.jpg` (source / D12 / + learned / + far cleanup, 7 frames), `sameview_<frame>_D1LFC_fsc.jpg` (3 frames), plus `_D12L_full`, `_D1L_fsc`, `_D1_fsc`, `_NEARBY_sumida`.

### R7. Deviations from sections 4–5 and A1 (recorded, not corrected)

1. Evidence file names: section 4 promised `align.json`, `trust.json`, `fuse_report.json`, `<run>.cmd` per run and a row in `outputs/evidence/static_map/summary.json`. What exists instead: `outputs/experiments/learned_depth/chunks_<tag>.json` (per-chunk alignment scale and residual, memory per stage, status), `lingbot_v2_<tag>_merged/run_report.json` (Sim3 fit for both pose conventions, boundary table), per-view trust bands and pixel outcomes inside the installed layer's `dense_report.json`, `outputs/evidence/learned_depth/{summary.json,static_metrics_<run>.json,static_table.csv}`, chunk logs `lingbot_v2_<tag>_t<t0>_<t1>.log` (first Sumida chunk: `lingbot_v2_sumida_c0.log`, `_c0_align.log`) and eval logs `<run>.eval.log`. No `.cmd` file; the exact `learned_fuse` / `far_cleanup` / `static_eval` argument lines were not archived.
2. Clause 4 asks for a provenance-tag count of 0 inside the map layer; separation is by construction (two output clouds), but no artifact records the count. Treated as **not measured**, not as PASS.
3. Clause 5 (our part's time) was not archived for any run; learned per-frame time is reported per chunk.
4. The judgement is made on **merged accepted chunks**, not on one full-clip model run (A1). Sumida eval frames at 30 s, 36 s, 52 s and 56 s lie in spans without accepted chunks, so the full-clip floor clause of section 4 was, in effect, tested on 3 of the 7 frames' spans.
5. Far cleanup was applied in the Sumida layer only; FSC keeps D1 + learned (R5).
6. `summary.json` names the FSC free-space field `float_fs_neutral` although section 4 assigns FSC its own maps; the value is reported under the file's field name.

### R8. Open items and next tests

- **Empty spans.** Sumida 10–20 s, 26–40 s and 46–60 s have no accepted learned depth; FSC 26–30, 38–60 and 68–71 s likewise. The next test for the fast-turn spans (Sumida 50–60 s, alignment residual 5–6 %) is a shorter chunk anchored inside the turn, judged by the same ≤ 2 % residual; for the triangulation-held spans (26–40 s, scale 1.18 / 2.22) the next test is a chunk whose frames straddle the open-park stretch, so the near sparse points used for the check are not all far away. Neither is run.
- **FSC far cleanup** removed real structure on the held-out frame (F-31); a variant with a higher support threshold on aerial views is a new plan item, not a tuning.
- **Adoption** of either trial layer as default is a user decision after both clips pass; today neither does (one clause each).
- Resume commands (from `summary.json`; verified chunks are skipped, a chunk starts only above 35 % free memory):

```zsh
PYTHONPATH=packages .venv/bin/python -m learned.chunked_run --session outputs/maps/mqg_sumida_0025_0125 --tag sumida --t-end 60 --first 0 10
PYTHONPATH=packages .venv/bin/python -m learned.chunked_run --session outputs/maps/mqg_fsc_campus_aerial --tag fsc --t-end 71
PYTHONPATH=packages .venv/bin/python -m learned.merge_chunks --tag sumida --out outputs/experiments/learned_depth/lingbot_v2_sumida_merged
```

## Later results (2026-11-09 evening) — display layers, not acceptance runs

This section records work done after the Result above. Nothing here changes the verdicts of R5 or the default layer; the new layers are **model-estimated display layers** (learned depth placed on our cameras, not confirmed by multiple views), labelled as such in the console, never the default, scale RELATIVE, NOT_NAVIGATION_SAFE. Evidence: `outputs/evidence/learned_depth/steady_layer_v2.json`, `static_metrics_onpath_fsc.json`, `static_metrics_onpath2_fsc.json`, `static_metrics_onpathfill_sumida.json`, `static_metrics_onpath2_sumida.json`, `static_metrics_onpath3_sumida.json`, `static_metrics_zipdemo_{fsc,sumida}.json`; rows in `docs/VALIDATION_LOG.md` (SL block); narrative in `docs/PROGRESS.md` (SL section).

- **Runner speed.** `run_lingbot_map.py --dtype bfloat16` (default) with autocast and a per-frame MPS cache release brought chunk inference from 131–500 s to 6–7 s (implementer-reported). The FSC chunk set for the layer below took 462 s in total.
- **Mac port of the demo in the user-supplied checkout** (`packages/learned/port_zip_demo_mac.py` → `outputs/tools/lingbot_map_mac/demo_mac.py`, 8 exact-match edits, mps, bfloat16 trunk, KV window 16, cache release per frame): about 1.7–2.6 frames/s, peak 11.1–11.2 GB, one pass over 47–75 frames in 41–63 s after a 30 s load. Its postprocess stores world-to-camera poses under `extrinsic`; we first read them as camera-to-world (our bug, fixed in `compare_zip_demo.py`), after which the camera centres fit our path to 0.6 / 1.3 / 2.1 % (corridor / FSC / Sumida). `zip_demo_mac_compare.json` predates the fix and its pose numbers are superseded. The corrected one-pass FSC coverage (floor / structure / far 0.77 / 0.77 / 0.10 on `f000320`) is far below the chunked D1L layer (0.98 / 0.98 / 0.66): one pass over the whole clip is not a substitute for chunked runs.
- **Steady layer (`packages/learned/model_on_path.py`, `dense_onpath/`).** Model depth from the accepted chunk runs, each frame placed with our bundle-adjusted SfM camera instead of the model's (the model's cameras wobble with shake); per-frame isotonic depth curve fitted to our triangulated points by distance, grown outward from the near scale with at most ×1.6 per band, falling back to a single scale when that fits better; pixels beyond the farthest supported depth dropped; `--edge 0.05` removes depth-jump streaks; `--consistency 2` keeps a pixel only when ≥ 2 neighbouring frames see the same surface; frames whose near depth disagrees by > 15 % are skipped. FSC: v1 3,597,262 pts, floating share 0.183 → v2 3,081,789 pts, 0.142, sky false points 399 → 2, `f000320` floor 0.96 → 0.94, structure 0.92 → 0.89, far 0.31 → 0.20 → **v3 (in app)** with `--consistency 3 --neighbours 1,2,3,5 --tol 0.04 --fuse` (each kept point moved to the median depth of its agreeing neighbours): 3,226,170 pts, 0.150, knn 0.0038, `f001720` structure 0.24 → 0.34 (`static_metrics_onpath3_fsc.json`). Sumida: v1 (with MVS fill) 2,066,898 pts, 0.302 → v2 1,492,495, 0.255 → v3 (no fill, 28–38 s left empty, 26 frames skipped, display-cleaned) 1,231,109 pts, 0.195, knn 0.0001 → **v4 (in app)** with `--local-fix` (frames no single curve fits are corrected pixel by pixel from the ≥ 3 nearest triangulated points within 60 px agreeing within 12 %; model confidence ≥ 1.0 accepted on those frames; edge filter on model depth) and `--fuse`: 1,367,234 pts, 0.209, knn 0.0016, 131 frames used, 0 skipped, 26 corrected; `f000900` (30 s) floor 0.01 → 0.28, structure 0 → 0.11, `f001080` (36 s) floor 0.02 → 0.16, floor_far 0.15 → 0.30 (`static_metrics_onpath4_sumida.json`). Build 3–6 s per map once chunks exist. Screenshots `outputs/evidence/ui/{fsc_onpath3.png,sumida_onpath4.png}`.
- **Two measured causes.** (1) The aerial "pancake": the farthest fifth of our triangulated points are 1.5–5× farther than the model's depth — the model squeezes far ground toward the camera; the layer therefore drops pixels our points do not support rather than extrapolating. (2) Sumida reflections: at model depth about 2, about half of our triangulated points lie at depth 14–17 — building reflections in the river triangulated below the water. A plain median curve followed them (errors 0.9–2.9); the outward-growing curve does not. This is also a weakness of our verified sparse map (reflections become false points) and no rule filters them yet. (3) The Sumida 28–38 s gap is a turn over a wide lawn (no water): the model is unsure there (20–35 % of pixels reach confidence 1.5) and one depth curve does not fit (frame error 0.13–0.67); per-pixel correction from nearby triangulated points brings the error to 0.015–0.03, so v4 keeps the stretch. (4) The aerial flat look is not a geometric bowl: ground quadratic sag is 3 % of the extent, the same as our sparse and verified dense maps, with no model points below ground; the default 20° view from far away showed the flat map edge-on. The viewer now opens levelled scenes at 43° along the camera direction, closer (`apps/desktop/static/viewer.js`).
- **Open after this round.** The empty spans of R8 are unchanged for the verified `dense_learned/` layer (the steady display layer fills 28–38 s by local correction, whose agreement with other views is not measured separately); no acceptance clause was run on the display layers and none is claimed; the camera-fit percentages, chunk-time gain and `clean_cloud` merge-option floor loss (0.64 → 0.05) are implementer-reported without a dedicated artifact; regression after the final layers: pytest 134 passed in 87.33 s, UI smoke 71 passed / 0 failed (`outputs/evidence/tests/pytest_ui-20261109-steady-v4.log`, `outputs/evidence/ui/ui_smoke.json`, 22:11 KST; the check count differs from the earlier 88 and the cause is not examined here).
