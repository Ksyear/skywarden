# PLAN_SCALE_AWARE_NOISE — 박스 크기에 비례하는 측정·과정 잡음 (F-34)

작성: Opus 5.5 (사용자 지시로 계획·문서 작성을 맡음), 2026-11-15, 브랜치 `feat/scale-aware-noise`.
상태: **계획 고정(실행 전)**. 0–6절은 보정·측정 전에 쓰고 해시를 `outputs/evidence/scale_aware_noise/prereg.json`에 남긴 뒤 바꾸지 않습니다. 결과는 7절에 덧붙입니다.

## 0. 문제와 원인 확인

직전 계획(`docs/PLAN_OCCLUSION_TRACKING.md` 7.3, `docs/FAILURES.md` F-34)에서 위트레흐트 1080p 라이브 30 s의 대상 LOCKED 비율은 어떤 연관 방식에서도 17–19 %였습니다. 원인 가설은 "대상 박스 높이(850–1,100 px)에 비해 고정 6 px 측정 잡음과 χ² 게이트가 좁다"였습니다.

이번에 기록된 라이브 타임라인(`outputs/runs/ot_live_utrecht_V0`, `_VAB`)에서 대상이 PREDICTING인 프레임을 다시 계산했습니다(`outputs/evidence/scale_aware_noise/diagnose_live.py` → `diagnose_live_baseline.json`, 수치만).

| | V0 | VAB(현재 기본) |
|---|---|---|
| 대상 PREDICTING 프레임(검출 있음) | 604 | 637 |
| **PREDICTING이 시작되는 프레임**(LOCKED → PREDICTING) 수 | 14 | 15 |
| 그 프레임의 가장 가까운 검출 거리 p50 (px / × 박스 높이) | 24.0 px / 0.029 | 23.2 px / 0.027 |
| 그 프레임의 m² (고정 6 px, 로그된 σ로 근사) p10 / p50 / p90 | 9.5 / 12.4 / 22.2 | 9.9 / 11.3 / 15.5 |
| m² > 9.21인 비율 | 1.00 | 1.00 |
| LOCKED 프레임의 대상 위치 σ(공분산) p50 | — | 2.9 px |
| PREDICTING 전체의 검출 거리 p50 (× 높이) | 0.17 | 0.19 |

해석:
- 가설은 맞지만 더 정확히는 이렇습니다. LOCKED 동안 위치 공분산이 약 2.9 px로 수렴하고 측정 잡음이 6 px로 고정이라 게이트 반지름이 √9.21 × √(2.9² + 6²) ≈ **20 px**입니다. 850 px 높이 사람에게 이는 0.024 × h이고, 정상 검출의 중심 흔들림(23 px ≈ 0.027 × h, 박스 아래가 화면에 잘린 경우 33 %)이 바로 그 밖에 떨어져 PREDICTING이 시작됩니다.
- 한 번 빠지면 예측이 등속으로 흘러 거리가 0.17–0.19 × h까지 벌어지고(직전 보고의 "0.1–0.2 × h"는 이 누적 값), 2 s 뒤 LOST → 운용자 재선택이 반복됩니다.
- 부수 관찰(이번 범위 밖, 7절에 보고): VAB에서는 PREDICTING 중 트랙 박스 높이 p50이 1,098 px로 검출(834 px)보다 커집니다. GMC의 프레임별 스케일이 가림 동안 누적되기 때문으로 보입니다.
- 박스 크기 쪽은 이미 척도 불변입니다(크기 일관성 항이 `log(h_det/h_track)`, 크기는 칼만 상태가 아니라 0.7/0.3 EMA). 그래서 이번 변경은 **위치 측정 잡음과 과정 잡음**만 다룹니다.

## 1. 바꾸는 것

SORT 계열에서 흔히 쓰는 방식(예: DeepSORT·BoT-SORT의 칼만 잡음이 박스 높이에 비례)을 우리 등속 필터에 맞게 줄여 씁니다.

- `TrackerConfig.meas_noise_frac`(기본 0 = 끔): 위치 측정 잡음 σ_z = max(`meas_noise_px`, `meas_noise_frac` × h).
- `TrackerConfig.accel_noise_frac`(기본 0 = 끔, 단위 h/s²): 과정 잡음 σ_a = max(`accel_noise_px_s2`, `accel_noise_frac` × h).
- h는 트랙의 현재 박스 높이(`Track.size[1]`). 새 트랙의 초기 위치 공분산은 검출 박스 높이로 같은 식을 씁니다.
- 둘 다 0이면 지금 코드와 **비트 단위로 같은 결과**(라이브러리 기본 `TrackerConfig()`의 strict 의미와 안전 테스트 그대로).
- 작은 박스에서는 최소값(6 px, 150 px/s²)이 그대로 쓰이므로 h ≤ 6 / `meas_noise_frac` 이고 h ≤ 150 / `accel_noise_frac` 인 사람은 동작이 바뀌지 않습니다(예: 0.05와 1.0이면 h ≤ 120 px).
- 새 프리셋 `VABS` = `VAB` + 위 두 값(보정으로 고름). 기존 프리셋 `V0/VA/VAB/VABC`의 의미는 바꾸지 않습니다(이전 증거와 비교 가능하게). CLI에 `--fixed-noise`(비례 잡음 끔)를 추가합니다.
- 바꾸지 않는 것: χ² 게이트 9.21, 박스 높이 상한 `max_gate_box_frac` 0.5, F-33 재연결 규칙 `contested_reconnect_frac` 0.15 × h, 만료 2.0 s, LOST 고정, 선택기 자동 전환 없음, `HARDWARE_ARMED=false`·MOCK.

F-33 규칙과의 상호작용: 재연결 규칙은 잡음과 무관하게 픽셀/높이 기준이라 그대로 작동합니다. 다만 게이트가 커지면 경합 성분이 늘 수 있으므로 E2X `side_by_side`(F-33 조건)를 조건별로 확인하고, 단위 테스트로 "AMBIGUOUS 뒤 0.15 × h 밖 검출 거부"가 비례 잡음에서도 유지되는지 고정합니다.

## 2. 보정(파라미터 고르기) — 측정 전 고정

데이터: **MOT17 보정용 {02, 05, 09}**(YOLOX-nano 캐시 검출, 직전 계획과 같은 0.35 임계값) + **E2 700회**. 보류 시퀀스·E2X·TUM·위트레흐트는 보정에 쓰지 않습니다.

격자(기준 = VAB): `meas_noise_frac` ∈ {0.0125, 0.025, 0.05} × `accel_noise_frac` ∈ {0, 1.0, 2.0} — 9점. 모든 점을 기록합니다.

선택 규칙:
1. 자격: E2 700회 K1–K3 PASS, 잘못된 대상 LOCKED 0(중앙·오프센터 `yaw_nonzero_on_B`, `locked_while_A_hidden`, `yaw_nonzero_not_locked` = 0), E2 AMBIGUOUS 프레임 ≤ VAB × 1.05; 보정 3개 합계 IDF1 ≥ VAB − 0.5, HOTA ≥ VAB − 0.5, IDSW ≤ VAB × 1.05.
2. 자격이 있는 점 중 보정 IDF1이 가장 높은 점. 더 작은 값(먼저 `meas_noise_frac`, 다음 `accel_noise_frac`)의 점이 0.3 IDF1 이내면 작은 쪽(지금 동작에 가까운 쪽).
3. 자격 있는 점이 없으면 채택하지 않고(7절에 FAIL 보고) 기본값은 VAB 유지.

## 3. 판정 데이터
| 이름 | 정답 | 용도 |
|---|---|---|
| E2 700회(`sim.two_person_crossing`) | 합성 신원 | 안전·유지율·AMBIGUOUS |
| E2X(`sim.crossing_ext`, 17조건 × 50 seed) | 합성 신원 | 안전(조건별)·유지율·AMBIGUOUS·IDSW |
| MOT17 보류 {04, 10, 11, 13}, YOLOX-nano | 사람 박스·id | IDF1/HOTA/IDSW (판정), FRCNN 공개 검출은 보고만 |
| TUM fr3/walking_xyz 900프레임 10 fps | 없음 | 속도 판정, id 수·LOCKED 비율은 보고만 |
| 위트레흐트 라이브 30 s 원속도 | 없음 | 대상 LOCKED 비율·지연 판정 |

같은 조건 비교를 위해 VAB도 이번 코드로 다시 돌립니다(합성·MOT17은 결정적이라 직전 값과 같아야 함 — 다르면 보고). 라이브는 실시간이라 실행마다 조금 다르므로 VAB와 VABS를 같은 세션에서 연달아 한 번씩 돌립니다.

## 4. 비교 변형
- **VAB**: 현재 앱·CLI 기본(margin + GMC, 고정 잡음).
- **VABS**: VAB + 2절에서 고른 비례 잡음.

## 5. 합격 기준(측정 전 고정, 결과 보고 바꾸지 않음)

비열등 허용치는 직전 계획(`PLAN_OCCLUSION_TRACKING.md` 5.2)과 같은 방식입니다.

- **(a) 안전·합성**
  - a1 E2 700회 K1–K3 PASS, 중앙·오프센터 `yaw_nonzero_on_B`, `locked_while_A_hidden`, `yaw_nonzero_not_locked` = 0.
  - a2 **잘못된 대상 LOCKED 프레임**과 B 쪽 yaw 프레임: E2 교차 조건별, E2X 17조건별로 VAB보다 하나라도 늘면 **FAIL**(VAB는 모두 0이므로 사실상 0 유지). E2X `yaw_not_locked` = 0.
  - a3 교차 후 올바른 대상 유지율(E2 교차 + E2X 전 조건 평균): VAB − 1 %p 이상.
  - a4 대상 AMBIGUOUS 프레임(E2 700회 + E2X 합): VAB × 1.05 이하. 대상 비-LOCKED 프레임(E2X)도 같은 허용치(× 1.05).
  - a5 E2X IDSW(정답 신원) 합: VAB × 1.05 이하.
- **(b) MOT17 보류 4개 합계(YOLOX-nano)**: IDF1 ≥ VAB − 0.5, HOTA ≥ VAB − 0.5, IDSW ≤ VAB × 1.05.
- **(c) 위트레흐트 라이브 30 s**: 대상 LOCKED 비율 **≥ 0.40**(직전 VAB 0.193) **그리고** 같은 세션 VAB 재측정보다 +15 %p 이상, `yaw_nonzero_while_not_locked_or_stop` = 0.
- **(d) 속도**: 위트레흐트 라이브 지연 p95 ≤ 33 ms, 버린 프레임 ≤ 5 %; TUM 프레임당 검출 p95 + 추적 p95 ≤ 33 ms.
- **(e)** 전체 `pytest`, `ruff check .`, UI 스모크(`node tests/ui/ui_smoke.mjs --attach http://127.0.0.1:9222`) 통과.

판정: (a)–(e)를 모두 통과하면 **채택**: `DEFAULT_PRESET = "VABS"`(앱·CLI 기본). 하나라도 실패하면 **미채택**: 기본 VAB 유지, `VABS`는 `--tracker VABS`로만 쓰는 trial. TUM의 id 수·LOCKED 비율, FRCNN 결과, 사람마다 다를 운용자 재선택 횟수는 판정에 쓰지 않고 보고만 합니다.

## 6. 구현·산출물
- `packages/tracking/tracker.py`: `meas_noise_frac`, `accel_noise_frac`, 높이별 σ 계산(예측·혁신·새 트랙). `describe()`에 `noise scaled`일 때만 표시.
- `packages/sim/crossing_ext.py`: `VABS` 프리셋. `packages/vision/tracker_setup.py`: 프리셋 목록, `--fixed-noise`.
- `packages/vision/mot_calibrate.py`: 이번 격자를 돌릴 수 있게 옵션 추가(기존 동작 유지).
- 테스트: 큰 박스(850 px)에서 20–40 px 떨어진 정상 검출이 게이트 안(비례 잡음) / 밖(고정 잡음), 작은 박스(≤ 120 px)에서 두 설정 결과가 같음, 0이면 V0·VAB와 비트 단위 동일, AMBIGUOUS 뒤 0.15 × h 재연결 거부 유지, CLI 기본값.
- 증거: `outputs/evidence/scale_aware_noise/`(prereg.json, diagnose_*, calib/, calibration_decision.json, e2_*, e2x_*, mot17/, tum_*, live_*, verdict.json, pytest·ruff·ui 로그).
