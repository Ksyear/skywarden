# Skywarden 시스템 현황 — 2026-11-15 기준

작성: Opus 5.5 (사용자 지시로 문서 작성을 맡음), 2026-11-15 전면 재정리. 이전 판(fable-documenter 2026-11-09 작성, 2026-11-15 갱신)의 내용 중 지금도 맞는 것만 남기고, 날짜별 서술은 `docs/PROGRESS.md`·`docs/VALIDATION_LOG.md`로 넘겼습니다. 모든 수치는 이 Mac(Apple M6, 16 GiB, macOS 27.2)에서 실제로 실행해 `outputs/` 아래(git 제외)에 남긴 산출물에서 가져왔습니다. 상태 어휘: **동작**(실행·검증됨) / **trial**(실행되지만 사전 기준 미통과, 기본값 아님) / **미완**(코드 없음 또는 실행 안 함) / **차단**(정책·환경상 불가).

---

## 1. 한눈에 보기

Skywarden은 **카메라 한 대의 영상만으로** (1) 오프라인 희소 3D 지도(pycolmap SfM)와 그 위의 CPU 밀집 점군을 만들고, (2) YOLOX-nano + Kalman 다중 추적기로 사람을 따라가며, (3) 그 결과를 yaw 전용 PID와 Tello SDK 2.0 Mock에 연결해 **비행 없이** 검증하는 로컬 macOS 프로젝트입니다. 지도·검출·추적·Mock 제어·라이브 실행·콘솔은 모두 동작하고, pytest 204개·UI 스모크 118개·GitHub Actions가 회귀를 막습니다.

- 한 테이크 영상(스미다 60 s, FSC 항공 71 s, 직접 찍은 실내 복도 47 s)은 GOOD 지도가 나오고, 편집 영상과 고정 카메라 영상은 지도가 되지 않습니다(의도된 거부).
- 모든 지도는 **상대 스케일**이고 항법에 쓸 수 없습니다(`NOT_NAVIGATION_SAFE`). 미터 스케일은 별도 `scale.json`으로만 붙습니다.
- 추적 기본값은 2026-11-15 사전 기준으로 채택한 `VAB`(마진 연관 + 카메라 움직임 보정)입니다. 같은 날 박스 크기 비례 잡음(`VABS`)은 라이브 속도 기준 하나를 넘지 못해 trial로 남았습니다(4절).
- 실기체 이륙·모터 명령·자동 추격은 없고(`HARDWARE_ARMED=false`), 유료 API·클라우드 추론도 없습니다.
- 가장 큰 열린 게이트: **사람이 나오는 본인 영상**으로의 추적 실행, 그리고 추적 기본값 결정(F-35 vs 속도 기준).

## 2. 구성 요소별 현재 상태

| 구성 요소 | 상태 | 지금 되는 것 | 근거 |
|---|---|---|---|
| 희소 3D 지도 (pycolmap 4.2.1, CPU SIFT, 전역 매퍼) | **동작** | 컷·정지 카메라 판정 → 3 fps 선별(150 프레임 캡) → SfM → PLY + 궤적 + `map_report.json`(GOOD/PARTIAL/POOR). 스미다 150/150·37,430점·0.512 px; FSC 150/150·96,344점·0.708 px; 복도 139/139·10,406점·0.819 px. TUM fr1/desk ATE 1.66 cm(평가용 스케일 차용) | `outputs/maps/mqg_*/map_report.json`, `outputs/evidence/geometry/` |
| 검증된 밀집 점군 (CPU 평면 스윕 MVS) | **동작** | ZNCC 평면 스윕 → 희소점 일치·다중 뷰 일관성 통과 깊이만 융합. 스미다 917,498점, FSC 1,942,876점. 카메라 앞 가파른 반복 질감(보도)은 비어 있음 | `outputs/maps/*/dense/dense_report.json` |
| 밀집 개선 변형 (`dense_experimental`, `dense_static*`, `dense_learned`) | **trial** | 바닥 커버리지·정적 장면·적응형 선별·학습 깊이 융합 변형. 각각 사전 조항 하나 이상 미통과 | `docs/PLAN_DENSE_COVERAGE.md` 외 각 Result |
| PatchMatch 밀집 / Poisson 메시 | **차단 / trial** | PatchMatch는 pycolmap에서 CUDA 전용 → 이 Mac에서 차단. 메시는 시각 검사 FAIL | F-09 |
| 학습형 깊이 모델 실행 (LingBot-Map, 별도 venv) | **동작 (외부 도구)** | 8 s 청크(겹침 2 s), 우리 경로에 Sim3 정렬, bfloat16·MPS 캐시 해제로 청크당 6–7 s. 약 1.7–2.6 frames/s, 최대 11.2 GB | `packages/learned/`, `outputs/evidence/learned_depth/` |
| 모델 출력 표시 레이어 (`dense_zipstyle`, `dense_hybrid`, `dense_onpath`) | **trial (표시용)** | 모델 깊이를 우리 SfM 카메라에 놓고 이웃 일관성으로 거른 레이어. FSC 3,226,170점, 스미다 1,367,234점. **다중 뷰 검증이 아니라 모델 추정**으로 표시 | `outputs/evidence/learned_depth/steady_layer_v2.json` |
| 사람 검출 (YOLOX-nano ONNX, OpenCV DNN CPU) | **동작** | 416 px, 점수 0.35. TUM walking 검출 p95 + 추적 p95 16.2 ms(`VAB`) | `outputs/evidence/scale_aware_noise/real_clips.json` |
| 다중 추적 (`MultiTracker`, 기본 `VAB`) | **동작** | 만료 우선(2 s), 게이트 그래프, 마진 연관(증거가 갈릴 때만 경합 해제, AMBIGUOUS 뒤 0.15 × h 안에서만 재연결), 카메라 움직임 보정. 원래 추적기 대비 MOT17 보류 HOTA 20.30 → 25.99, IDF1 21.26 → 29.67, IDSW 532 → 364; 합성 잘못된 대상 115 → 0 | `outputs/evidence/occlusion_tracking/verdict.json` |
| 외모 임베딩 (`--reid`, OSNet x0.25) | **trial (opt-in)** | 경합 판정·가림 뒤 재연결에만 사용, 메모리 전용, 얼굴 영역 지움. 기준 통과(IDSW 364 → 319)했지만 라이브 지연 p95 45.4 ms로 속도 탈락 | 같은 파일 |
| 박스 크기 비례 측정 잡음 (`--tracker VABS`) | **trial** | σ = max(6 px, 0.025 × h). 위트레흐트 대상 LOCKED 19.2 → 69.1 %, MOT17 보류 IDF1 29.67 → 32.46, IDSW 364 → 213, 큰 박스 합성 잘못된 대상 46 → 0. 판정 실행 라이브 지연 p95 34.08 ms > 33 ms로 미채택 | `outputs/evidence/scale_aware_noise/verdict.json` |
| yaw PID · 안전 게이트 · Tello EDU Mock | **동작 (MOCK 전용)** | `YawController`(클램프·anti-windup·STOP 사유), SDK 2.0 파서, 루프백 `MockTello`, `GeofenceGuard`(E3 PASS). 비루프백 송신 0회를 4가지 env 조합으로 테스트 | `adapters/tello/`, `tests/test_tello.py` |
| 라이브 실행 (파일 원속도 / 최대 속도 / `webcam:N`) | **동작 (파일) / NOT_RUN (웹캠)** | 위트레흐트 1080p 30 s 원속도: `VAB` 29.96 fps, 지연 p95 30.34 ms, 버림 1/900. 이 Mac에 카메라가 없어 `webcam:0`은 `NO_DEVICE`로 올바르게 FAILED | `outputs/evidence/scale_aware_noise/real_clips.json`, `outputs/evidence/live/` |
| 단안 VO | **trial** | 상대 스케일, 루프 클로저·재측위 없음. 합성 ATE 1.82 % PASS, 스미다 vs SfM 23.3 %(기준 15 %) FAIL | F-10 |
| 미터 스케일 (`scale.json`: 마커·두 점·텔레메트리) | **동작 (공개 데이터) / NOT_RUN (본인 마커 영상)** | 합성 마커 같은 벽 +0.39 %; TUM 두 점 1차 −11.8 % FAIL, 2차 −1.24 % `MEASURED_VERIFIED`(사후 기준); 테니스장 4.906 / 0.477 / 4.925 % `MEASURED_UNVERIFIED`; MockTello 높이 0.0025 % | `docs/PLAN_METRIC_SCALE.md` 10절 |
| 미터 깊이 모델 시험 (4종, MPS) | **trial (참고용)** | TUM 깊이 스케일 오차 −2.27 / −4.53 / +48.19 / +1.73 %. 대체 후보 없음, 항공에서 모델 간 최대 10.96배 차이 | `outputs/evidence/metric_scale/metric_models_summary.json` |
| 지도 콘솔 (`127.0.0.1:8780`, WebGL) | **동작** | Mapping / Tracking / Live 탭, 품질 배지, 레이어 선택, overview 지도, Follow, 위에서 보기, Measure 도구, 추적 모드 한 줄 표시. FSC 3,226,170점 p50 7.6 ms, PLY 스트리밍, Host/Origin 403·JSON 415 | `apps/desktop/`, `outputs/evidence/portfolio/` |
| 재측위 · 지도 위 현재 위치 | **미완** | `map_position=NOT_AVAILABLE` | — |
| 본인 영상 게이트 (`REAL_VIDEO`) | **지도 PASS / 추적 NOT_RUN** | 복도 지도 GOOD. 같은 영상 추적은 사람이 없고 캐비닛 오검출 트랙 1개(2.7 s) | `outputs/evidence/real_video/` |
| 실기체 비행 | **차단** | 기체 없음, 정책상 금지 | `CLAUDE.md` |

## 3. 앱에서 보이는 지도와 레이어

`scripts/run_demo.sh` → Mapping 탭 → `Stored maps`. 레이어 메뉴는 폴더가 있는 것만 활성화합니다. `T` 위에서 보기, `F` Follow, 우하단 overview 지도 클릭으로 재생 위치 이동.

| 세션 (영상) | 레이어 | 의미 | 점 수 / 지표 |
|---|---|---|---|
| `mqg_fsc_campus_aerial` (캠퍼스 항공, CC BY) | `Dense · verified` (기본) | 검증된 다중 뷰 점군 | 1,942,876 |
| | `Dense · with learned depth (trial)` | 3중 검증 통과 학습 깊이, 미채택 | 8,234,561 |
| | `Model output · all pixels` / `· steady` | 모델 추정 / 우리 카메라에 놓고 이웃 일관성으로 거른 모델 추정 | 3,553,380 / 3,226,170 |
| `mqg_sumida_0025_0125` (강변 산책, CC BY) | `Dense · verified` (기본) | 검증된 다중 뷰 | 917,498 |
| | `Hybrid` / `Model output · steady` | 가까운 곳 모델 + 먼 곳 평면 스윕 / 모델 추정(28–38 s는 근처 삼각측량점으로 픽셀 보정) | 2,540,617 / 1,367,234 |
| `mqg_corridor` (실내 복도, 직접 촬영 `REAL_VIDEO`) | `Sparse` / `Model output · all pixels` | 희소 지도 GOOD / 모델 추정 | 10,406 / 1,755,680 |

**`Dense · verified`만이 여러 뷰로 확인된 점**입니다. 상대 스케일, 미터 아님, 항법에 쓸 수 없음. CC BY 영상으로 만든 지도와 화면은 저장소에 넣지 않습니다.

## 4. 사람 추적 현황 (2026-11-15)

| 지표 | 원래(`V0`) | 현재 기본 `VAB` | trial `VABS` | 근거 |
|---|---|---|---|---|
| E2 700회 잘못된 대상 LOCKED / K1–K3 | 0 / PASS | 0 / PASS | 0 / PASS | `occlusion_tracking/`, `scale_aware_noise/verdict.json` |
| E2X 잘못된 대상 LOCKED (14조건 × 50) | 115 | 0 | 0 | 같음 |
| E2X-L(4.5배 큰 박스) 잘못된 대상 LOCKED / 유지율 | — | 46 / 0.871 | 0 / 1.000 | `scale_aware_noise/verdict.json` |
| 교차 후 유지율 (E2+E2X) | 0.811 | 0.854 | 0.854 | 같음 |
| MOT17 보류 4개, YOLOX-nano: HOTA / IDF1 / IDSW | 20.30 / 21.26 / 532 | 25.99 / 29.67 / 364 | 28.25 / 32.46 / 213 | `*/mot17/scores_heldout_yolox_nano.json` |
| 위트레흐트 라이브 30 s: 대상 LOCKED / 지연 p95 | 17.8 % / 27.5 ms | 19.2 % / 30.34 ms | 69.1 % / 34.08 ms | `*/real_clips.json` |

(`V0` 열은 겹침 라운드, `VAB`·`VABS` 열은 이번 라운드 측정. `VAB`의 위트레흐트 값은 겹침 라운드에서 19.3 % / 30.2 ms였습니다.)

읽는 법: 정지(AMBIGUOUS, PREDICTING)는 안전한 실패이고 잘못된 대상 LOCKED는 위험한 실패입니다. `VAB`는 사람이 작게 보이는 장면에서 위험한 실패를 없앴지만, 사람이 화면을 채우면 고정 6 px 잡음 때문에 대상을 자주 놓치고 확대 합성 장면에서 잘못된 대상 잠김이 생깁니다(F-34, F-35). `VABS`가 둘 다 고치지만 사전 등록한 라이브 속도 기준(p95 ≤ 33 ms)을 판정 실행에서 1.08 ms 넘었습니다. 추가 반복 두 번은 30.81 / 30.33 ms였고(판정에 쓰지 않음), 기본값 전환은 새 기준으로 다시 판정할 일입니다.

## 5. 장점

- **증거 기반.** 모든 PASS는 이 Mac에서 돌린 명령과 원시 산출물을 가리키며, 사전 고정한 기준을 결과 뒤에 바꾸지 않습니다. 실패(F-01–F-35)와 미채택 변형도 지우지 않습니다.
- **안전 설계가 테스트로 묶여 있음.** 비루프백 송신 0회, LOCKED 아닐 때 yaw 0, AMBIGUOUS → STOP, 2 s 만료, 잘못된 대상 0프레임(E2·E2X), 지오펜스 정지가 자동 테스트와 시뮬레이션으로 확인됩니다.
- **정답 있는 추적 평가.** MOT17 보정·보류 분할로 파라미터를 고르고 보류 시퀀스로만 판정합니다.
- **라벨이 정직함.** `SIMULATED` / `PUBLIC_DATASET` / `REAL_VIDEO`, `SCALE RELATIVE`, `NOT_NAVIGATION_SAFE`, `TRIAL`, `MODEL_ESTIMATED`가 보고서와 UI에 같이 붙습니다.
- **전부 로컬·무료.** 프레임·얼굴은 기기 밖으로 나가지 않고, 외모 임베딩은 켜도 디스크에 남지 않습니다.
- **재현 가능.** 명령·버전·입력 해시·산출물 경로가 `docs/VALIDATION_LOG.md`에 있고 외부 가중치는 SHA-256으로 재검증합니다.

## 6. 한계

- **지도는 상대 스케일이고 항법용이 아닙니다.** 실제 녹화에서 미터 스케일 검증까지 간 것은 TUM fr1/desk 한 건이며 그 점 기준은 사후에 정했습니다. 테니스장 폭 방향 4–5 % 체계 오차 원인은 미확인. 박스 크기로 거리 추정은 E1(p95 61–90 %) 때문에 금지.
- **밀집 지도의 채택된 개선이 없습니다.** 기본 레이어는 카메라 앞 보도가 대부분 비어 있습니다.
- **모델 출력 레이어는 검증이 아닙니다.** 항공 영상에서 모델이 먼 지면을 카메라 쪽으로 눌러(우리 점 대비 1.5–5배) 지지 없는 픽셀은 버립니다. 스미다 강물 반사는 우리 희소 지도에서도 가짜 점이 됩니다.
- **추적은 겹침에서 멈춥니다.** 두 사람이 붙어 있으면 대상이 AMBIGUOUS로 정지하고, 재획득은 위치·크기 증거가 분명할 때만 됩니다. 기본 `VAB`는 사람이 화면을 채우면 자주 PREDICTING으로 빠지고(위트레흐트 LOCKED 19.2 %) 확대 합성 장면에서 잘못된 대상 잠김 46프레임(F-35). AMBIGUOUS 뒤 재연결 규칙(0.15 × h)은 그보다 가까운 이웃을 막지 못합니다.
- **MOT17 절대값은 낮습니다.** YOLOX-nano(416)가 1080p 군중의 작은 사람을 많이 놓쳐 변형 간 비교용입니다.
- **VO는 보정 카메라가 필요**하고(F-10), 재측위·지도 위 위치는 미구현입니다.
- **편집 영상·고정 카메라·사람이 화면을 채우는 영상**은 지도가 안 되거나 PARTIAL입니다(방콕 POOR, 몬트리올 거부, 위트레흐트 PARTIAL).
- **한 대의 Mac에서만 측정**했고 실제 웹캠·실기체·다른 OS는 NOT_RUN/차단입니다. 본인 영상은 사람이 없는 복도 한 편뿐입니다.
- **미터 깊이 모델은 장면에 따라 크게 틀립니다**(항공에서 최대 10.96배, DA3NESTED는 TUM +48 %). 교차 확인으로만 씁니다.
- **일부 실행 인자 미보관.** `learned_fuse`/`far_cleanup`/`static_eval`의 정확한 셸 한 줄은 남아 있지 않습니다(모듈 docstring의 사용법이 기준).

### 라이다 vs 카메라

| 항목 | 라이다 | 카메라(현재 시스템) |
|---|---|---|
| 거리 정확도 | 실측 m, cm급 | 상대 스케일(미터는 별도 측정으로만) |
| 실시간 지도 | 됨 | 안 됨(영상 1분당 6–12분 오프라인) |
| 어두운 곳 · 무늬 없는 벽 · 물 반사 | 대체로 강함 | 약함(스미다 반사가 가짜 점) |
| 항법·충돌 회피 | 가능 | 불가(`NOT_NAVIGATION_SAFE`) |
| 사람 인식·추적 · 색 지도 | 어려움 · 없음 | 잘함 · 있음 |
| 무게·전력·가격 | 무겁고 비쌈(대략치, 구매 전 확인 필요) | 가볍고 쌈 |

결론: **지도·항법은 라이다, 사람 추적·비용·무게는 카메라 우위.** 중간안은 스테레오 깊이 카메라이고, Tello EDU는 탑재 여유가 수십 g이라 라이다 탑재가 불가능합니다. 기체 선택과 한국 규정은 `docs/DRONE_PURCHASE_AND_KOREA_RULES.md`.

## 7. 소요 시간 · 비용 · 실시간

| 단계 (이 Mac, 오프라인) | 복도 47 s | FSC 71 s | 스미다 60 s |
|---|---|---|---|
| 희소 SfM(150 프레임 캡) | 74 s | 201 s | 213 s |
| 평면 스윕 밀집 | 269 s | 481 s | 508 s |
| 학습 깊이 청크 실행 | — | 462 s | — |

- 대략 **영상 1분당 6–12분**. 밀집 시간 일부는 다른 작업과 병렬로 측정한 참고값입니다.
- **비용:** 유료 API·클라우드 AI 없음. 네트워크는 무료 가중치·공개 데이터 다운로드뿐입니다.
- **실시간:** 검출+추적+MOCK yaw+VO는 1080p 원속도 실시간(위 2절). 지도는 실시간이 아닙니다.

## 8. 라즈베리파이 (추정, 실측 아님)

**NOT_RUN** — Pi가 없어 실행하지 않았습니다. `outputs/evidence/rpi/rpi_feasibility.json`은 PyPI aarch64 휠 확인과, 이 Mac 측정값을 Pi 5(CPU 5–8배 느림, GPU 없음)로 환산한 추정입니다. 검출+추적은 ~10–20 fps로 가능 추정, 희소 SfM은 pycolmap aarch64 휠이 없어(0개) 소스 빌드 필요, 학습 깊이는 불가. 권장: Pi는 촬영·검출·추적(MOCK), 지도는 Mac/PC. (뷰어 수치는 WebGL 이전 값이며 다시 계산하지 않았습니다.)

## 9. 테스트·CI

- **로컬(2026-11-15, 이번 라운드):** pytest **204 passed**, ruff 통과, UI 스모크 **118 passed / 0 failed / 4 skipped**(이미 열린 브라우저에 `--attach`; 4 skip은 아카이브된 레이어를 이유와 함께 기록). 증거 `outputs/evidence/scale_aware_noise/{pytest_full.log,ruff.log,ui_smoke.log}`.
- **GitHub Actions(macos-15):** main의 최신 실행 43673521558(겹침 추적 병합) — fast 잡 180 passed / 12 skipped / 5 deselected, full 잡(가중치·샘플 영상 받은 뒤) 196 passed / 1 skipped, 둘 다 success. 이번 브랜치는 아직 푸시하지 않아 CI 미실행.
- 간헐 실패 3건(빌드·라이브 자식 프로세스 종료 경쟁, 테스트 보조 클래스 음수 sleep, 느린 러너의 텔레메트리 기록기 경쟁)은 2026-11-15에 고쳤습니다. 이력과 이전 실행 수치는 `docs/VALIDATION_LOG.md`.

## 10. 저장소 이력

- 공개 저장소 `Ksyear/skywarden`은 2026-11-15(KST) **새로 만들어** 기존 커밋 이력을 그대로 올렸습니다. 그래서 GitHub의 PR 번호는 **#1부터 다시 시작**했고(현재 #1 = 겹침 추적, 병합 `dc849a6`), 커밋 메시지에 남은 예전 번호(`Merge pull request #1/#9/#10/#11`)는 이전 저장소의 번호라 지금 GitHub의 PR과 다릅니다.
- 이전 저장소에서 병합된 주요 작업(커밋 해시): E2·E3 실험 `50bd108`, 미터 스케일·WebGL 뷰어·CI `b9060cd`, 학습 깊이·steady 레이어·overview 지도 `74341e4`, CI 안정화·복도 영상 `REAL_VIDEO` 기록 `8a1b824`; 새 저장소에서 라이선스(Apache-2.0)·NOTICE·데모 영상 `431e0dd`, 겹침 추적 `dc849a6`.
- 이번 라운드 브랜치 `feat/scale-aware-noise`(로컬 커밋만, 푸시·PR 전).

## 11. 다음 할 일 (우선순위)

1. **추적 기본값 결정:** `VAB`(F-35: 큰 박스 잘못된 대상 잠김)와 `VABS`(판정 실행 라이브 p95 34.08 ms) 중 하나. 여러 번 실행한 p95 중앙값 같은 속도 기준을 새로 사전 등록해 다시 판정.
2. AMBIGUOUS 뒤 재연결 규칙을 0.15 × h보다 가까운 이웃에도 안전하게(F-35의 합성 사례), GMC 스케일 누적으로 가림 중 박스가 커지는 문제.
3. 사람이 나오는 본인 영상으로 `scripts/run_tracking.sh … --origin REAL_VIDEO` → 본인 영상 추적 게이트. 복도 캐비닛 오검출(점수 0.36–0.59)과 임계값 0.35의 관계도 함께.
4. 미터 스케일: 마커 2장 + 줄자 길이 2개를 넣은 본인 영상으로 실행(`docs/PLAN_METRIC_SCALE.md` 7절); 테니스장 폭 방향 오차 원인 확인.
5. 모델 출력 레이어 점검: 스미다 28–38 s 보정 구간의 다중 뷰 일치, 반사면 점 거르기 규칙.
6. 실제 웹캠 라이브, 보정 카메라로 VO 재측정(F-10).
7. Raspberry Pi 실측(8절)으로 추정치 교체.
8. 기체·한국 규정 확인(`docs/DRONE_PURCHASE_AND_KOREA_RULES.md`). 구매·이륙은 사용자 승인 전 금지, 먼저 할 수 있는 무료 단계는 PX4 SITL에 MOCK 제어 연결.

## 12. 근거 파일

`docs/PLAN_SCALE_AWARE_NOISE.md`·`docs/PLAN_OCCLUSION_TRACKING.md`·`docs/PLAN_METRIC_SCALE.md` 결과 절, `docs/FAILURES.md`, `docs/PROGRESS.md`, `docs/VALIDATION_LOG.md`; `outputs/evidence/scale_aware_noise/{verdict.json,real_clips.json,live_repeats_informational.json,diagnose_live_*.json,pytest_full.log,ui_smoke.log}`, `outputs/evidence/occlusion_tracking/{verdict.json,real_clips.json}`, `outputs/evidence/metric_scale/`, `outputs/evidence/portfolio/`, `outputs/evidence/learned_depth/steady_layer_v2.json`, `outputs/evidence/real_video/`, `outputs/evidence/rpi/rpi_feasibility.json`, `outputs/maps/*/map_report.json`·`dense/dense_report.json`. CI 수치는 `gh run view 43673521558 --log`.
