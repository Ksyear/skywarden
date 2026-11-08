# PLAN_E4_APPEARANCE_CUE — can a non-biometric clothing cue resolve AMBIGUOUS components?

Status: E4 run on 2026-11-09, decision NO_GO; sections 2-4 not started (see "Result" at the end). Scope: feasibility experiment E4, then (only on GO) a minimal cue in `packages/tracking/tracker.py` + `vision.track_video`, tests, E2/E3 reruns, UI. Question answered: "motion alone cannot separate two close people; does adding clothing or face help?"

## 0. Fixed constraints (decided, not open)
- **No face recognition, no biometric identification, no identity-based Re-ID.** Faces are never cropped, embedded or compared.
- Allowed cue: **clothing colour only** — HSV histograms of the torso (upper 20–55 % of the person box) and legs (55–90 %) regions, hue/sat bins only (e.g. 16x4 per region, L1-normalised, concatenated → 128 floats). No texture/CNN embedding.
- Transient: held in memory for the live session, never written to disk, logs, `tracks.json` or evidence (only scalar distances/decisions are recorded). Not an identity: it is a per-track running colour summary that dies with the track.
- Used **only** to split components the motion tracker marks AMBIGUOUS. Never revives a LOST id, never auto-selects a target, never extends the 2 s expiry. If the cue is not decisive the component stays AMBIGUOUS and the yaw command stays STOP (`TARGET_AMBIGUOUS`).

## 1. E4 feasibility experiment (no ground-truth identities needed)
Input: recorded runs `outputs/tracks/{vtest_nano_fixv3, yt_montreal_street_nano_fixv3, tum_walking_xyz_nano_fixv3}/` (frames/ JPEGs + `tracks.json`, commit 98813e6 tracker). Descriptor computed offline from each frame's detection boxes (REAL_MODEL only; boxes < 40 px high or score < 0.4 skipped and counted).
- **Positives (pseudo-labels):** pairs of descriptors from the same confirmed track at two trusted, unambiguous hits, time gap 0.5–2.0 s (the span over which the cue must actually work). May contain tracker errors; report count.
- **Negatives:** pairs from two different tracks visible in the same frame (certainly different people). Includes similar clothing by construction.
- Distance: Bhattacharyya on each region, averaged (also report L1 for comparison). Metrics per clip: ROC AUC, TPR at FPR ≤ 1 % and ≤ 0.1 %, pair counts, box-size histogram. Report also TPR at FPR 1 % restricted to boxes < 80 px (the crowded Montreal case).
- Command (Opus): `python -m packages.tracking.experiments.e4_appearance --sessions <3 sessions> --out outputs/evidence/e4_appearance_cue/` → `metrics.json`, `roc_<clip>.png`, `pairs_summary.json` (no descriptors saved).
- **Pre-declared GO/NO-GO (fixed before running):** GO only if TPR ≥ 0.60 at FPR ≤ 1 % on at least two of the three clips. NO-GO → document in `docs/VALIDATION_LOG.md`, keep the current STOP policy, close the request as "not feasible without biometrics". BLOCKED if frames/ are missing or any session lacks ≥ 200 negative pairs.

## 2. Minimal integration (only on GO)
- `Detection.appearance: np.ndarray | None = None` (optional; frozen dataclass stays; `source` unchanged). `vision.track_video` fills it from the frame; sims set `source="SYNTHETIC"` and a synthetic descriptor (colour label → fixed histogram + noise), reported as SYNTHETIC.
- Per-track EMA descriptor (alpha 0.1, init on confirmation) updated **only on trusted 1x1 hits**; never in AMBIGUOUS, never after expiry; dropped with the track.
- Decision inside an AMBIGUOUS component: for each motion-gated track–detection pair compute d. Accept pair (i,j) only if d(i,j) ≤ τ₁% (the E4 FPR-1 % threshold of that camera profile, config, default conservative), d(i,j) + margin m ≤ second-best d for track i AND for detection j (mutual margin, m from E4 negatives, e.g. 0.10), and both have descriptors. Accepted pairs are corrected as trusted hits; everything else in the component stays AMBIGUOUS with current behaviour (no correction, no `last_hit_t`, detections withheld). Expiry, LOST, selector stickiness unchanged.
- Fallback: `appearance_enabled=False` config restores commit 98813e6 behaviour bit for bit.

## 3. Tests (`packages/tracking/tests/test_appearance_cue.py`)
A1 identical synthetic clothing, crossing → component stays AMBIGUOUS, STOP, no id switch. A2 different clothing crossing → resolves, no switch, ids persist, selector unchanged. A3 LOST/expired id never revived by a matching descriptor (new id created). A4 descriptor never on disk: `tracks.json`/evidence JSON contain no `appearance` key, recorded run bytes equal with cue on/off apart from state fields. A5 descriptors absent → behaviour identical to 98813e6. A6 EMA not updated during AMBIGUOUS.

## 4. Re-runs and reports
E2: K1–K3 unchanged, plus `same_clothes` (must stay AMBIGUOUS/LOST, 0 switches) and `different_clothes` (re-lock rate reported, 0 switches) variants. Real-clip before/after table extended with cue-on column (ambiguous frames, locked tracks/frame, accepted/rejected appearance decisions) from `outputs/evidence/e2_two_person_fix_v3/real_clip_before_after.json`. E3 regression unchanged. UI: AMBIGUOUS badge gains "resolved by clothing" count, no descriptor display.

## 5. Risks and privacy notes
Lighting/white-balance drift (hue shift → false rejects, safe direction), similar clothing (stays AMBIGUOUS — expected), small boxes and motion blur (histograms degenerate → treat as no descriptor), partial occlusion mixing two people's colours into one box. Privacy: colour summaries are not identifiers and are not persisted; no face region is used; the cue cannot be used to look up a person across sessions. Any change to persistence or regions requires a new plan.

## Result (2026-11-09) — NO_GO, cue not integrated

Section 1 was run; sections 2-4 were not started because the pre-declared gate was missed. Evidence: `outputs/evidence/e4_appearance/e4_appearance.json` (SHA-256 in `MANIFEST.sha256`); code `packages/vision/appearance.py`, `packages/vision/appearance_eval.py`; tests `tests/test_appearance.py` (3 passed, implementer-reported, no archived log yet). Command: `PYTHONPATH=packages .venv/bin/python -m vision.appearance_eval --sessions outputs/tracks/vtest_nano_fixv3 outputs/tracks/yt_montreal_street_nano_fixv3 outputs/tracks/tum_walking_xyz_nano_fixv3 --out outputs/evidence/e4_appearance`. Full row in `docs/VALIDATION_LOG.md`.

- Constraint 0 held: no face region, no biometric identification, descriptor in memory only; the evaluation wrote scalar metrics only.
- As implemented (differs slightly from section 1): torso y 22-50 % / legs y 58-90 % of the box (head band never sampled), HSV histograms with 12x4 hue/saturation bins plus 6 value bins for grey pixels, mean Hellinger distance; positives = same LOCKED track >= 1.0 s apart (max 40 per track), negatives = two LOCKED tracks in one frame (max 6 per frame), boxes < 48 px skipped; inputs are the fix_v3 recorded runs. No ROC images, L1 comparison or small-box subset were produced.
- Gate: TPR >= 0.60 at FPR <= 1 % on >= 2 clips; < 200 negative pairs = BLOCKED.
- vtest: AUC 0.870, TPR at FPR 1 % 0.154 (0.1 %: 0.059), 871 pos / 2,484 neg -> FAIL. City street (Montreal, CC BY): AUC 0.874, TPR at 1 % 0.493 (0.1 %: 0.222), 5,083 / 5,756 -> FAIL. TUM walking: AUC 0.963, 255 / 55 pairs -> BLOCKED (too few people visible together).
- Decision NO_GO: the cue separates people on average, but at a 1 % false-match rate it recovers only 15-49 % of true matches, so using it would risk handing the target to someone dressed alike. The tracker is unchanged and AMBIGUOUS -> STOP (`TARGET_AMBIGUOUS`) stays. The request is closed as "not feasible with a non-biometric colour cue".
- Limits: pseudo-labels from the tracker (positives may contain switches), similar clothing kept in the negatives on purpose, small far boxes in stored <= 960 px frames, one camera per clip.
- Possible next step, not done and not planned: a learned person re-identification embedding. Most public training sets are licensed for non-commercial research only and the method is closer to identity matching, so it needs a licence and policy decision before any plan.
