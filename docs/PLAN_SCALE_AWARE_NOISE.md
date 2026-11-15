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

## 7. 결과 (2026-11-15, Opus 실행·작성)

0–6절은 측정 전 그대로입니다(`prereg.json`의 해시는 이 7절을 붙이기 전 파일). 원시 증거: `outputs/evidence/scale_aware_noise/` — 판정 `verdict.json`(`verdict.py`가 5절을 기계적으로 적용), 보정 `calibration_decision.json`·`calib/`·`calib_e2.json`, 실제 클립 `real_clips.json`, 원인 분석 `diagnose_live_baseline.json`·`diagnose_live_after.json`, 테스트 `pytest_full.log`(204 passed)·`ruff.log`·`ui_smoke.log`(118 / 0 / 4 skipped).

### 7.1 보정
2절 규칙대로 **`meas_noise_frac` 0.025, `accel_noise_frac` 0**을 골랐습니다(보정 3개 IDF1 VAB 26.95 → 29.24, IDSW 273 → 159). 0.05는 보정 IDF1이 더 높았지만(29.95) E2 AMBIGUOUS가 1,174 → 1,296(+10.4 %)으로 5 % 허용치를 넘어 자격이 없었고, 과정 잡음 비례(1.0, 2.0)는 모든 조합에서 E2 AMBIGUOUS가 22 % 이상 늘었습니다(2.0은 K1–K3 FAIL). 그래서 이번 변경은 **측정 잡음만** 비례합니다.

### 7.2 계획과 달라진 점(모두 공개)
- **E2X-L 추가(사후, 판정을 더 엄격하게만 함):** 0.025 × h는 h ≤ 240 px에서 6 px 최소값과 같아서, 박스가 150–180 px인 E2·E2X에서는 VABS가 VAB와 **비트 단위로 같은 결과**를 냅니다(`e2_*`, `e2x_*` 동일 확인). 사전 등록한 합성 안전 관문이 새 동작을 전혀 보지 못하므로, 같은 E2X 장면을 4.5배로 키운 E2X-L(사람 675–810 px, 2880×2160, 위치 잡음·속도도 4.5배, 지표는 원래 좌표)을 추가하고 a2와 같은 규칙(조건별 잘못된 대상 프레임이 VAB보다 늘면 FAIL)을 적용했습니다. `sim.crossing_ext --scale`.
- **오기 정정:** 3절과 5절 a2의 "E2X 17조건"은 **14조건**(코드의 조건 목록 그대로, 조건당 50 seed = 700회)의 오기입니다. 판정은 14조건 전부에 적용했습니다.
- **라이브 반복(판정에 쓰지 않음):** 사전 등록대로 한 번씩 돌린 라이브에서 VABS 지연 p95가 34.08 ms로 나와, 원인을 보려고 VABS·VAB를 두 번씩 더 번갈아 돌렸습니다(`live_repeats_informational.json`). 판정은 첫 실행 값 그대로입니다.

### 7.3 표
| 지표 | VAB (현재 기본) | VABS |
|---|---|---|
| E2 700회 K1–K3 / 잘못된 대상 LOCKED 프레임 | PASS / 0 | PASS / 0 (VAB와 동일) |
| E2X 잘못된 대상 LOCKED / B 쪽 yaw | 0 / 0 | 0 / 0 (VAB와 동일) |
| **E2X-L(큰 박스) 잘못된 대상 LOCKED / B 쪽 yaw** | **46 / 15** | **0 / 0** |
| 교차 후 유지율 E2+E2X / E2X-L | 0.854 / 0.871 | 0.854 / 1.000 |
| 대상 AMBIGUOUS 프레임 E2 700회 / E2X / E2X-L | 1,174 / 1,235 / 2,257 | 1,174 / 1,235 / 506 |
| 대상 비-LOCKED 프레임 E2X / E2X-L | 9,129 / 17,651 | 9,129 / 8,788 |
| IDSW(정답 신원) E2X / E2X-L | 65 / 994 | 65 / 155 |
| MOT17 보류 4개, YOLOX-nano: HOTA / IDF1 / MOTA / IDSW | 25.99 / 29.67 / 22.35 / 364 | 28.25 / 32.46 / 23.15 / 213 |
| MOT17 보류 4개, 공개 FRCNN 검출: HOTA / IDF1 / IDSW (보고만) | 47.19 / 55.57 / 233 | 48.53 / 57.78 / 167 |
| MOT17 7개 합계, YOLOX-nano: HOTA / IDF1 / IDSW (보고만) | 25.13 / 28.92 / 637 | 27.29 / 31.57 / 372 |
| **위트레흐트 라이브 30 s: 대상 LOCKED / AMBIGUOUS** | **19.2 % / 1.9 %** | **69.1 % / 0.4 %** |
| 위트레흐트: 운용자 재선택 횟수 (보고만) | 10 | 5 |
| 위트레흐트: 지연 p95 / 버린 프레임 / 처리 fps | 30.34 ms / 1 / 29.96 | **34.08 ms** / 6 / 29.79 |
| 위트레흐트 반복 2회 지연 p95 (판정 아님) | 30.50 / 30.40 ms | 30.81 / 30.33 ms |
| TUM walking: id 수 / LOCKED 트랙 있는 프레임 비율 / AMBIGUOUS 트랙-초 (보고만) | 31 / 0.662 / 6.1 | 20 / 0.714 / 2.5 |
| TUM 검출 p95 + 추적 p95 / 처리 fps | 16.20 ms / 67.7 | 16.38 ms / 67.1 |

같은 코드로 다시 돌린 VAB는 직전 라운드와 같았습니다(E2·E2X 요약 동일, MOT17 보류 IDSW 364 = 364).

원인 재확인(`diagnose_live_after.json`): VABS에서 대상이 LOCKED → PREDICTING으로 떨어진 횟수는 15 → 4번, 그 순간 검출까지 거리 p50은 0.085 × h(VAB 0.027 × h). 즉 정상 흔들림으로는 더 이상 떨어지지 않고, 남은 4번은 박스 아래가 화면에 잘린 프레임(4번 중 3번)에서 중심이 크게 움직인 경우였습니다.

### 7.4 판정
- (a) 안전·합성: **PASS**(a1–a5 모두). 사후 E2X-L 안전 검사도 **PASS** — 오히려 현재 기본 VAB가 큰 박스에서 잘못된 대상 LOCKED 46프레임을 냈고(새 실패 F-35), VABS는 0.
- (b) MOT17 보류: **PASS**(IDF1 +2.79, HOTA +2.26, IDSW −41 %).
- (c) 위트레흐트: **PASS**(69.1 % ≥ 40 %, VAB 대비 +49.9 %p, LOCKED 아닐 때 yaw 0).
- (d) 속도: **FAIL** — 판정 실행의 라이브 지연 p95 34.08 ms > 33 ms(버린 프레임 0.67 %, TUM 16.38 ms는 통과). 같은 실행에서 검출기 시간 p95도 13.82 → 14.66 ms로 늘었고(검출기는 추적 설정과 무관), 추가 반복 두 번은 30.81 / 30.33 ms였으므로 일시적인 부하였을 가능성이 크지만, 기준은 바꾸지 않습니다.
- (e) 테스트·린트·UI: **PASS**(pytest 204 passed, ruff 통과, UI 스모크 118 / 0 / 4 skipped).

**결론: 미채택.** 앱·CLI 기본값은 `VAB` 그대로이고, `VABS`는 `--tracker VABS`로 켜는 trial입니다. 채택하려면 같은 기준으로 라이브 속도를 다시 판정하는 새 계획(예: 실행 여러 번의 중앙값 p95를 사전 등록)이 필요하며, 그 결정은 사용자에게 맡깁니다.

### 7.5 남은 문제
- **F-35(새로 찾음):** 현재 기본 VAB는 사람이 화면을 채우는 크기(E2X-L)에서 잘못된 대상 LOCKED 46프레임, B 쪽 yaw 15프레임을 냅니다. VABS를 쓰지 않는 한 남아 있습니다.
- F-33 재연결 규칙(0.15 × h)은 잡음과 무관하게 작동함을 단위 테스트로 고정했지만, 규칙 자체는 0.15 × h 안의 이웃을 막지 못합니다. 손으로 만든 합성 장면(이웃이 0.04 × h 옆에 붙어 걷다가 그 이웃의 트랙이 비현실적인 급가속으로 끊긴 경우)에서는 VABS가 이웃에게 110프레임 중 32프레임 잠겼고 VAB는 0이었습니다(`guard_gap_probe.json`). E2X-L과 현실적인 속도의 같은 장면에서는 재현되지 않았지만, 큰 게이트가 이 규칙의 빈틈을 더 드러낼 수 있다는 뜻입니다.
- VAB에서는 가림 동안 GMC 스케일이 누적되어 트랙 박스가 검출보다 커집니다(위트레흐트 PREDICTING 중 높이 p50 1,098 px vs 검출 834 px). 이번 범위 밖입니다.

## 8. 재판정: 라이브 속도 프로토콜 (1차 FAIL 뒤 측정 전에 추가, 2026-11-15)

상태: **측정 전 고정.** 이 절은 7절의 1차 판정(미채택) 뒤, 새 측정을 하기 전에 썼고 해시를 `outputs/evidence/scale_aware_noise/prereg2.json`에 남기고 커밋합니다. 사용자 지시("고칠 것")에 따른 재판정이며, 1차 결과와 순서(1차 FAIL → 프로토콜 사전 등록 → 재측정)를 모두 공개합니다.

### 8.1 왜 바꾸는가
- 5절 (d)는 **한 번의 실행 p95**로 판정했습니다. 1차 판정 실행에서 VABS 지연 p95가 34.08 ms였는데, 같은 실행에서 **추적 설정과 무관한 검출기 시간 p95도 13.82 → 14.66 ms로 늘었고** 최대 지연이 117 ms였습니다. 한 번의 p95는 그 순간의 기기 부하에 크게 흔들립니다.
- **공개:** 판정에 쓰지 않은 반복 2회(VABS 30.81 / 30.33 ms, VAB 30.50 / 30.40 ms, `live_repeats_informational.json`)를 이미 보았습니다. 그 결과를 알고서 이 프로토콜을 쓰므로, 아래 기준은 1차보다 **엄격하게**(차이·추적 시간 조건 추가) 정했습니다. 이 반복 4회와 1차 실행은 새 판정에 쓰지 않습니다.
- **계측 추가(측정 전):** 지금까지 라이브 리포트에는 추적 단계 시간이 없었습니다. `live_run`에 `track_ms`(추적기 갱신 — GMC 포함 — 와 대상 선택 시간, 프레임별 `timeline.jsonl` 행과 리포트의 p50/p95)를 추가했습니다. 다른 동작은 바뀌지 않습니다.

### 8.2 측정
- 위트레흐트 라이브 30 s 원속도(같은 클립·같은 운용자 드라이버 `select_driver.py`), **새로 10회**: VAB와 VABS 각 5회, 순서 ABBA 교차 — VAB, VABS, VABS, VAB, VAB, VABS, VABS, VAB, VAB, VABS. 세션 이름 `sn2_live_utrecht_<변형>_<1–5>`. 실행 중 다른 무거운 작업(테스트·합성·MOT17·빌드)을 돌리지 않습니다.
- 각 실행에서: 지연 p95, `track_ms` p95, 검출 p95, 버린 프레임 비율, 대상 LOCKED 비율, LOCKED 아닐 때 yaw 프레임.

### 8.3 합격 기준 (d′, 5절 (d)의 라이브 부분을 대체)
모두 만족해야 PASS:
1. VABS 5회 지연 p95의 **중앙값 ≤ 33 ms**.
2. **중앙값(VABS) − 중앙값(VAB) ≤ 2 ms** (지연 p95).
3. **중앙값(VABS `track_ms` p95) − 중앙값(VAB `track_ms` p95) ≤ 1 ms**.
4. VABS **모든 실행**에서 버린 프레임 ≤ 5 %, LOCKED 아닐 때 yaw = 0.
5. TUM 조건(검출 p95 + 추적 p95 ≤ 33 ms)은 1차 측정값(16.38 ms) 그대로.

나머지 (a)(b)(e)와 사후 E2X-L 안전 검사는 1차 판정 그대로입니다. (c)도 1차 판정을 유지하되, 새 10회의 VABS LOCKED 비율 중앙값이 (c)의 문턱(≥ 0.40, VAB 중앙값 대비 +15 %p)을 못 넘으면 채택하지 않습니다(더 엄격한 쪽).

### 8.4 판정 후
- PASS: 앱·CLI 기본값을 `VABS`로 바꿉니다(`tracker_setup.DEFAULT_PRESET`). 라이브러리 기본 `TrackerConfig()`는 원래 strict(V0) 그대로, 기존 안전 테스트 그대로. `--fixed-noise`로 끌 수 있습니다. `--reid`는 계속 opt-in(외모 경로의 속도는 이번에 판정하지 않음).
- FAIL: 기본값 `VAB` 유지, 그대로 보고.
