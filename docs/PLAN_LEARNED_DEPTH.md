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
