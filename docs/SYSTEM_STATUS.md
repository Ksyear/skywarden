# Skywarden 시스템 현황 — 2026-11-09 기준

작성: fable-documenter (claude-fable-5-1). 모든 수치는 이 Mac(Apple M6, 16 GiB, macOS 27.2) 한 대에서 Opus 5.5가 실제로 실행하고 `outputs/evidence/`, `outputs/maps/`, `outputs/tracks/`, `outputs/runs/`에 남긴 산출물에서 가져왔습니다. 행별 명령·버전·경로는 `docs/VALIDATION_LOG.md`, 마일스톤 표는 `docs/PROGRESS.md`, 실패 기록은 `docs/FAILURES.md`에 있습니다. 상태 어휘: **동작**(실행·검증됨) / **시험 단계**(실행되지만 고정 기준 미통과 또는 검증 없음) / **미완**(코드 없음 또는 실행 안 함) / **차단**(정책·환경상 불가).

---

## 1. 한눈에 보기

Skywarden은 **카메라 한 대의 영상만으로** (1) 오프라인 희소 3D 지도(COLMAP/pycolmap SfM)와 그 위의 CPU 밀집 점군을 만들고, (2) YOLOX-nano + Kalman 다중 추적기로 사람을 따라가며, (3) 그 결과를 yaw 전용 PID와 Tello SDK 2.0 Mock에 연결해 **비행 없이** 검증하는 로컬 macOS 프로젝트입니다. 희소 지도·검증 밀집 점군·검출·추적·Mock 제어·라이브 실행·콘솔 UI는 모두 동작하며 자동 테스트(이번 세션 134 passed, UI 스모크 71 passed / 0 failed)로 회귀를 막고 있습니다. 한 테이크 영상(강변 산책 스미다 60 s, 캠퍼스 항공 FSC 71 s, 실내 복도 47 s)은 GOOD 지도가 나오고, 편집 영상(컷 18개)과 고정 카메라 영상은 지도가 되지 않습니다(의도된 거부). 2026-11-09 저녁에는 학습형 다중 뷰 깊이 모델(LingBot-Map, Apache-2.0, 별도 환경 외부 도구)의 Mac 실행을 안정화하고, 모델이 추정한 깊이를 **우리 SfM 카메라 경로 위에** 올린 "steady" 표시용 레이어를 추가했습니다(모델이 불확실한 스미다 28–38 s 구간은 근처 삼각측량점으로 픽셀 단위 보정해 채웠고, 항공 지도가 납작해 보이던 것은 측정 결과 기하가 아니라 기본 시점 문제라 뷰어 시작 시점을 바꿨습니다). 모든 지도는 **상대 스케일**이고 미터 거리·충돌 회피·항법 안전을 보장하지 않으며, 모델이 추정한 레이어는 **검증되지 않은 추정**으로 따로 표시됩니다. 실기체 이륙·모터 명령·자동 추격은 정책상 없고(`HARDWARE_ARMED=false`), 유료 API·클라우드 추론도 없습니다. 아직 열려 있는 가장 큰 게이트는 **사용자 본인 영상(`assets/input/`, `REAL_VIDEO`)으로의 실행**과 **미터 스케일 앵커링**입니다.

---

## 2. 구성 요소별 현재 상태

| 구성 요소 | 상태 | 지금 되는 것 | 근거 |
|---|---|---|---|
| 희소 3D 지도 (pycolmap 4.2.1, CPU SIFT, 전역 매퍼 기본) | **동작** | 영상/이미지 폴더 → 컷·정지 카메라 판정 → 3 fps 선별(150 프레임 캡) → SfM → 모델별 PLY + 궤적 + `map_report.json`(GOOD/PARTIAL/POOR). 스미다 150/150 등록, 37,430점, 0.512 px, GOOD; FSC 150/150, 96,344점, 0.708 px, GOOD; 복도 139/139, 10,406점, 0.819 px, GOOD. TUM fr1/desk 정답 대비 ATE 1.66 cm(평가용 스케일 차용). 편집 영상(방콕) POOR, 사람을 따라가는 영상(위트레흐트) PARTIAL, 고정 카메라 거부 | `outputs/maps/mqg_{sumida_0025_0125,fsc_campus_aerial,corridor}/map_report.json`, `outputs/evidence/geometry/` |
| 검증된 밀집 점군 (CPU 평면 스윕 MVS, 기본 레이어 `dense/`) | **동작** | GOOD 지도의 포즈로 ZNCC 평면 스윕 → 희소점 일치 ≥ 0.70 + 다중 뷰 일관성 통과 깊이만 융합. 스미다 917,498점, FSC 1,942,876점. 카메라 바로 앞 보도처럼 가파른 반복 질감은 비어 있음 | `outputs/maps/*/dense/dense_report.json` |
| 밀집 실험 레이어 (`dense_experimental`, `dense_static{,_v2,_v3}`, `dense_learned`) | **시험 단계 (미채택)** | 바닥 커버리지·정적 장면·적응형 선별·학습 깊이 검증 융합의 변형들. 각각 사전 고정한 수용 조항 중 1개 이상을 넘지 못해 라벨 붙은 trial로만 설치. 채택 여부는 사용자 결정. 콘솔에 안 보이는 것은 `outputs/archive/`로 이동(`manifest.json`에 from→to 기록) | `docs/PLAN_DENSE_COVERAGE.md`, `PLAN_STATIC_MAP.md`, `PLAN_FRAMES_AND_DEPTH.md`, `PLAN_LEARNED_DEPTH.md` 각 Result |
| PatchMatch 밀집 / Poisson 메시 | **차단 / 시험 단계** | PatchMatch는 pycolmap에서 CUDA/HIP 전용 → 이 Mac에서 BLOCKED. 메시는 시각 검사 FAIL로 콘솔 미제공 | `outputs/evidence/dense/patch_match_probe.txt`, F-09 |
| 학습형 다중 뷰 깊이 모델 실행 (LingBot-Map, `outputs/tools/venv_learned`) | **동작 (외부 도구)** | 8 s 청크(겹침 2 s)로 재개 가능하게 실행, 우리 경로에 Sim3 정렬. 2026-11-09 저녁: `--dtype bfloat16` + autocast + 프레임마다 MPS 캐시 해제로 청크 추론 131–500 s → 6–7 s. 사용자가 제공한 LingBot-Map 체크아웃의 데모도 Mac(mps)으로 포팅(`port_zip_demo_mac.py`, 8개 정확 일치 편집) — 약 1.7–2.6 frames/s, 최대 11.1–11.2 GB, kv 창 16(64는 OOM) | `packages/learned/{run_lingbot_map,chunked_run,merge_chunks,port_zip_demo_mac}.py`, `outputs/evidence/learned_depth/` |
| 모델 출력 표시 레이어 (`dense_zipstyle` 전 픽셀 / `dense_hybrid` / `dense_onpath` steady) | **시험 단계 (표시용, 검증 아님)** | 모델이 추정한 깊이를 그대로 보여 주는 레이어. steady 레이어(`model_on_path.py`)는 각 프레임을 **우리 SfM 카메라**로 놓고 거리별 깊이 곡선을 우리 삼각측량점에 맞춘 뒤 이웃 프레임이 같은 표면을 볼 때만 픽셀을 남기고(`--fuse`: 이웃이 본 중앙 깊이로 이동), 곡선 하나가 안 맞는 프레임은 근처 삼각측량점 ≥ 3개로 픽셀 단위 보정(`--local-fix`). FSC v3 3,226,170점(떠 있는 점 비율 0.150, 고립 0.0038), 스미다 v4 1,367,234점(0.209, 131 프레임 전부 사용, 26 프레임 국소 보정). **다중 뷰로 검증된 지도가 아니라 모델 추정**임을 라벨로 명시 | `outputs/evidence/learned_depth/steady_layer_v2.json`, `outputs/maps/*/dense_onpath/dense_report.json` |
| 사람 검출·추적 (YOLOX-nano ONNX, OpenCV DNN CPU + Kalman `MultiTracker`) | **동작** | 검출 p50 8.94 ms, 검출+추적 109.5 fps(TUM walking). 만료 우선·게이트 그래프 AMBIGUOUS·픽셀 게이트(E2 수정, 700회 시뮬레이션 K1–K3 PASS). Re-ID 없음(설계), 정답 박스 없어 MOTA/IDF1 미정의 | `outputs/tracks/*/tracking_report.json`, `outputs/evidence/e2_two_person_fix_v3/` |
| yaw PID · 안전 게이트 · Tello EDU Mock | **동작 (MOCK 전용)** | `YawController`(클램프·anti-windup·STOP 사유), SDK 2.0 파서, 루프백 UDP `MockTello`, `GeofenceGuard`(E3 PASS). `HARDWARE_ARMED=true` **그리고** `DRY_RUN=false`가 아니면 소켓을 열기 전에 예외. 비루프백 송신 0회를 4가지 env 조합으로 테스트 | `adapters/tello/`, `tests/test_tello.py`, `outputs/evidence/m3/`, `e3_geofence/` |
| 라이브 실행 (한 시간축: 파일 원속도/최대 속도/`webcam:N`) | **동작 (파일) / NOT_RUN (실제 웹캠)** | 위트레흐트 1080p 30 s 원속도: 29.96 fps 처리, 지연 p95 24.2 ms, 버림 1, `SUCCEEDED`. 대상 LOCKED는 프레임의 17 %(모션 전용 추적기 한계). 이 Mac에 카메라가 없어 `webcam:0`은 `NO_DEVICE`로 올바르게 FAILED | `outputs/evidence/live/` |
| 단안 VO (오프라인·라이브 내) | **시험 단계** | 상대 스케일, 루프 클로저 없음, `relocalization=NOT_IMPLEMENTED`. 합성 ATE 1.82 % PASS, 스미다 vs SfM 23.3 %(기준 15 %) **FAIL**(초점거리 추정) | `outputs/evidence/vo/`, F-10 |
| 지도 콘솔 (stdlib HTTP `127.0.0.1:8780` + 캔버스 뷰어) | **동작** | Mapping / Tracking / Live 탭, 품질 배지, 레이어 선택(폴더가 있을 때만 활성), Nearby 뷰, 학습 깊이 상태 레인, **Top 버튼/`T` 키 위에서 보기**, 평평한 장면의 PCA 지면 수평 맞춤, 수평 맞춘 장면은 카메라 진행 방향으로 43° 피치·1.7× 반경에서 시작(이전 20°·2.6×는 평평한 항공 지도를 얇은 원반처럼 보이게 했음). 지도가 로드되면 3D 패널 우하단에 **overview 지도**(240×180: 위에서 본 지도·카메라 경로·현재 카메라 점과 시야 방향 쐐기; 클릭하면 가장 가까운 카메라 시점으로 재생 이동)가 항상 표시되고, **Follow**(기본 ON, `F`로 토글)는 영상 재생 중 3D 뷰가 현재 카메라 바로 뒤에서 카메라가 본 방향을 보며 프레임 사이를 약 180 ms로 부드럽게 이동하고, 일시정지하거나 재생 중 뷰를 잡으면 바로 그 카메라 자세에서 사용자에게 조작이 넘어가며(카메라가 보던 점을 중심으로 회전), Play로 다시 따라감(Top/Reset은 Follow 해제). UI 스모크 이번 세션 75 passed / 0 failed | `apps/desktop/`, `outputs/evidence/ui/ui_smoke.json` |
| 재측위 · 지도 위 현재 위치 | **미완** | `map_position=NOT_AVAILABLE`. 라이브 VO 경로는 세그먼트별 상대 경로일 뿐 지도 위치가 아님 | README "알려진 한계" 6 |
| 미터 스케일 앵커링 (AprilTag/체커보드) | **미완** | 어떤 지도에도 미터 거리 없음. ATE의 cm 값은 정답 궤적에서 스케일을 빌린 평가 전용 | `docs/PROGRESS.md` TODO 7 |
| 본인 영상 게이트 (`assets/input/`, `REAL_VIDEO`) | **NOT_RUN** | 공개 CC BY 클립과 사용자가 제공한 복도 영상(origin 라벨 `UNSPECIFIED`)으로만 실행. `REAL_VIDEO` 라벨이 붙은 세션은 아직 없음 | `docs/PROGRESS.md` "Next single command" |
| 실기체 비행 | **차단** | 기체 없음, 정책상 금지. Mock만 | `CLAUDE.md` |

---

## 3. 앱에서 보이는 지도와 레이어

`scripts/run_demo.sh` → Mapping 탭 → `Stored maps`. 레이어 메뉴는 해당 폴더가 있는 것만 활성화합니다. 밀집 레이어를 고르면 경로·카메라는 기본 숨김(`P`/`C`로 복귀), `T`로 위에서 봅니다. 우하단 overview 지도에 현재 카메라 위치와 시야 방향이 항상 표시되고 클릭하면 그 지점으로 재생이 이동하며, Follow는 기본으로 켜져 있어 영상을 재생하면 3D 뷰가 카메라를 따라 움직이고, 일시정지하면 그 자리에서 마우스로 자유롭게 돌려볼 수 있습니다(`F`로 끄고 켬). 스크린샷: `outputs/evidence/ui/fsc_follow_playing.png`(재생 중, 지도 뷰가 영상 프레임과 일치), `outputs/evidence/ui/fsc_paused_dragged.png`(일시정지 후 드래그), 이전 `fsc_follow_f60.png`.

| 세션 (영상) | 레이어 | 의미 | 점 수 / 지표 |
|---|---|---|---|
| `mqg_fsc_campus_aerial` (캠퍼스 항공, 71 s, CC BY) | `Dense · verified` | **검증된 다중 뷰** 점군(기본) | 1,942,876 |
| | `Dense · with learned depth (trial)` | D1 + 삼각측량점·다른 프레임·자유공간 3중 검증을 통과한 학습 깊이. 미채택 trial | 8,234,561 |
| | `Model output · all pixels` (`dense_zipstyle`) | 모델 신뢰도 ≥ 1.5인 **모든** 픽셀, 모델 카메라, 검증 없음 — **모델 추정** | 3,553,380 / 떠 있는 점 0.326 |
| | `Model output · steady` (`dense_onpath`, v3) | 모델 깊이를 **우리 SfM 카메라**에 놓고 거리 곡선·에지·이웃 ≥ 3개 일관성(4 %)으로 거르고 이웃 중앙 깊이로 융합한 것 — **모델 추정**, 흔들림 없음 | 3,226,170 / 0.150, 고립 0.0038 |
| `mqg_sumida_0025_0125` (강변 산책, 60 s, CC BY) | `Dense · verified` | 검증된 다중 뷰(기본) | 917,498 |
| | `Hybrid · model near + multi-view far` (`dense_hybrid`) | 가까운 곳은 모델 깊이(검증 없음), 먼 구조물은 평면 스윕. 표시용 정리(`clean_cloud`) 적용 | 2,540,617 / 0.288 |
| | `Model output · all pixels` | 위와 같음 | 2,016,336 |
| | `Model output · steady` (v4) | 위와 같음(이웃 ≥ 2개, 8 %); 모델이 불확실한 **28–38 s(넓은 잔디밭 위 회전)는 근처 삼각측량점으로 픽셀 단위 보정**해 채움(26 프레임, 건너뜀 0), 표시용 정리 적용 | 1,367,234 / 0.209, 고립 0.0016 |
| `mqg_corridor` (실내 복도, 47 s, 사용자 제공, origin 라벨 미지정) | `Sparse` | 희소 지도 GOOD | 10,406 |
| | `Model output · all pixels` | 모델 추정 전 픽셀 | 1,755,680 |

읽는 법: **`Dense · verified`만이 여러 뷰로 확인된 점**이고, `Model output`·`Hybrid`의 가까운 부분은 모델이 추측한 깊이를 우리 경로에 맞춰 놓은 것입니다. 상대 스케일, 미터 아님, 항법에 쓸 수 없음. 스크린샷: `outputs/evidence/ui/fsc_onpath3.png`, `outputs/evidence/ui/sumida_onpath4.png`(이전 버전 `fsc_onpath2_top.png`, `sumida_onpath3.png`).

---

## 4. 장점

- **증거 기반.** 모든 PASS는 이 Mac에서 돌린 명령과 원시 산출물(JSON/로그/PLY/스크린샷)을 가리키며, 사전에 고정한 수용 조항을 결과 뒤에 바꾸지 않습니다. 실패(F-01~F-32)와 미채택 변형도 지우지 않고 기록합니다.
- **안전 설계가 테스트로 묶여 있음.** 비루프백 송신 0회, LOCKED 아닐 때 yaw 0, AMBIGUOUS → STOP, 2 s 만료, 지오펜스 정지가 모두 자동 테스트와 시뮬레이션(E2 700회, E3)으로 확인됩니다.
- **라벨이 정직함.** `SIMULATED` / `PUBLIC_DATASET` / `REAL_VIDEO`, `SCALE RELATIVE`, `NOT_NAVIGATION_SAFE`, `TRIAL, NOT ADOPTED`, `MODEL_ESTIMATED`가 보고서와 UI에 같이 붙고, 합성 결과는 실제 게이트로 집계되지 않습니다.
- **전부 로컬·무료.** 외부 API·업로드 없음. 프레임과 사람 얼굴은 기기 밖으로 나가지 않습니다.
- **한 테이크 영상이면 지도가 됨.** 컷 없는 산책·항공·실내 복도 영상 3종에서 GOOD, 그 위에 검증 밀집 점군까지 CPU만으로 생성.
- **학습 모델도 Mac에서 돌아감.** 16 GB Apple Silicon에서 청크 실행·bfloat16·캐시 해제로 OOM 없이 완주하고, 모델 출력은 우리 SfM 경로에 맞춰 흔들림 없이 표시됩니다.
- **재현 가능.** 명령·버전·입력 해시·산출물 경로가 `docs/VALIDATION_LOG.md`에 있고, 외부 가중치는 SHA-256으로 매번 재검증합니다.

## 5. 단점·한계 (솔직하게)

- **지도는 상대 스케일이고 항법용이 아닙니다.** 미터 거리·장애물 회피·충돌 안전을 보장하지 않습니다. 박스 크기로 거리 추정도 측정 결과(E1 p95 61–90 %) 때문에 금지입니다.
- **밀집 지도의 "채택된" 개선이 없습니다.** 바닥 커버리지·정적 장면·적응형 선별·학습 깊이 융합의 모든 변형이 사전 조항 중 하나에서 FAIL이라 trial로만 남아 있고, 기본 레이어(M0)는 카메라 앞 보도가 대부분 비어 있습니다.
- **모델 출력 레이어는 검증이 아닙니다.** `Model output`·`Hybrid` 가까운 부분은 단일 모델의 추정이며 다른 독립 증거로 확인된 점이 아닙니다. 모델은 항공 영상에서 **먼 지면을 카메라 쪽으로 눌러**(우리 점 대비 1.5–5배 차이) 멀리 갈수록 깊이가 어긋나므로 우리 점이 지지하지 않는 거리의 픽셀은 버립니다. 항공 지도가 "호떡"처럼 납작해 보이던 것은 측정 결과 기하 문제가 아니었습니다(지면 휨은 범위의 3 %로 희소·검증 밀집 지도와 같고 지면 아래 점 0 %) — 20° 피치 기본 시점이 평평한 지도를 옆에서 보여 준 탓이라 시작 시점을 43°로 바꿨습니다. 그래도 평평한 항공 지도는 높이 정보가 적어 위에서 볼 때 가장 잘 읽힙니다.
- **우리 검증 지도에도 약점이 있습니다.** 스미다 강물에 비친 건물 반사가 수면 아래 가짜 점으로 삼각측량됩니다(모델 깊이 ~2에서 우리 점 절반이 깊이 14–17). 반사면은 SfM 희소 지도에서도 가짜 점이 됩니다.
- **모델이 불확실한 구간은 보정에 기대고 있습니다.** 스미다 28–38 s(넓은 잔디밭 위 회전)는 모델 픽셀의 20–35 %만 신뢰도 1.5에 도달하고 깊이 곡선 하나가 맞지 않아(프레임 오차 0.13–0.67), 근처 삼각측량점으로 픽셀 단위 보정(오차 0.015–0.03)하고 신뢰도 1.0까지 받아들여 채웠습니다. 보정은 근처에 검증된 점이 있는 픽셀에만 적용되지만, 그 구간 점의 밀도와 세부는 다른 구간보다 낮습니다(30 s 바닥 0.28, 36 s 바닥 0.16). 검증 융합 레이어(`dense_learned`)에는 이 구간의 채택 청크가 여전히 없습니다.
- **추적은 모션만 봅니다.** Re-ID가 없어 가림·교차 후 같은 id 재획득을 보장하지 않고, 혼잡하면 STOP이 늘고 트랙이 짧아집니다(옷 색 재식별 E4는 NO_GO). MOTA/IDF1은 정답 박스가 없어 미정의입니다.
- **VO는 보정 카메라가 필요**하고(F-10), 재측위·지도 위 위치는 미구현입니다.
- **편집 영상·고정 카메라·사람이 화면을 채우는 영상**은 지도가 안 되거나 PARTIAL입니다(방콕 POOR, 몬트리올 거부, 위트레흐트 점프 19–43회).
- **한 대의 Mac에서만 측정**했고, 실제 웹캠·실기체·다른 OS는 NOT_RUN/차단입니다. 본인 영상(`REAL_VIDEO`) 게이트도 아직 열려 있습니다.
- **일부 실행 인자 미보관.** `learned_fuse`/`far_cleanup`/`static_eval`의 정확한 셸 한 줄과 "우리 쪽" 융합 시간은 보관되지 않았습니다(모듈 docstring의 사용법이 기준).
- 코드 라이선스 파일이 아직 없습니다(`LICENSE` 미정).

---

## 6. 소요 시간 (이 Mac, 오프라인)

| 단계 | 복도 47 s | FSC 71 s | 스미다 60 s |
|---|---|---|---|
| 희소 SfM(150 프레임 캡, 전역 매퍼) | 74 s | 201 s | 213 s |
| 평면 스윕 밀집 | 269 s (D12) | 481 s (D1) | 508 s (D12) |
| 학습 깊이 청크 실행(모델) | — | 462 s | — |
| steady 레이어 생성(청크가 있으면) | — | 3–6 s | 3–6 s |
| 체크아웃 데모 한 번에 실행(모델 로드 ~30 s 별도) | 41 s | 63 s | ~60 s |

대략 **영상 1분당 6–12분**의 오프라인 처리입니다. 밀집 시간 일부는 다른 작업과 병렬로 측정되어 참고값입니다(`UNMEASURED_QUIET`, `docs/PLAN_FRAMES_AND_DEPTH.md`).

## 7. 비용

유료 API·클라우드 AI 없음. 모든 추론은 로컬(CPU, 학습 모델만 Apple GPU/MPS). 네트워크 사용은 무료 가중치 1회 다운로드(YOLOX ONNX, GitHub 릴리스; LingBot-Map 체크포인트는 로컬 파일)와 CC BY 공개 클립 다운로드뿐이며, 프레임·얼굴은 업로드하지 않습니다.

## 8. 실시간 가능 여부

- **검출+추적+MOCK yaw+VO는 실시간 가능**: 1080p 파일을 원속도로 29.96 fps, 지연 p95 24.2 ms.
- **지도는 실시간이 아닙니다.** SfM+MVS는 오프라인(위 표). 학습 모델은 로드 ~30 s 후 약 2 fps로 스트리밍되지만 kv 창 16에서 약 75 프레임이 메모리 한계이고, 스케일이 없습니다. 따라서 **이 Mac에서 LiDAR 같은 실시간 미터 지도 장치가 아니며** 항법에 쓸 수 없습니다.

## 9. 라즈베리파이에서 돌아가나 (추정, 실측 아님)

**NOT_RUN** — 연결된 Raspberry Pi가 없어 실제 실행은 하지 않았습니다. `outputs/evidence/rpi/rpi_feasibility.json`은 PyPI의 linux aarch64 휠 유무 확인과, 이 Mac 측정값을 **가정한 Pi 5(Cortex-A76 4코어, CPU 5–8배 느림, PyTorch에 쓸 GPU 없음)** 로 환산한 추정치입니다. Pi에서 실측하기 전까지는 어느 숫자도 보장이 아닙니다.

| 구성 요소 | Mac 측정 | Pi 5 추정 | 판정 |
|---|---|---|---|
| 검출+추적 (YOLOX-nano, OpenCV DNN) | 검출 p50 8.94 ms, 109.5 fps | 프레임당 ~45–70 ms, ~10–20 fps | 가능 |
| MOCK 제어 · VO · 라이브 실행 | 가벼움 | 가벼움 | 가능 |
| steady 레이어 합성 (numpy) | 3–6 s/지도 | ~30–60 s | 깊이 맵을 다른 곳에서 가져오면 가능 |
| 희소 SfM (pycolmap) | 74–213 s/클립 | 클립당 약 10–30 min; **pycolmap은 aarch64 휠 없음**(0개) → 소스 빌드 필요 | 오프라인이면 가능, 느림 |
| CPU 평면 스윕 밀집 | 269–508 s | 클립당 약 0.5–1.5 h | 가능하나 비실용적 |
| 학습형 깊이 (LingBot-Map) | Apple GPU 메모리 ~11 GB, 체크포인트 4.6 GB, ~2 fps | GPU 없음; CPU만으로는 프레임당 수십 초 이상, 메모리 부족 가능성 큼 | **불가** |
| 앱 뷰어 (Pi 브라우저) | 3.2 M점 렌더 p50 ~80 ms | 3 M점에서 다시 그리기 ~0.4–0.6 s; Follow를 부드럽게 하려면 점 솎아내기 필요 | 동작하나 끊김 |

휠 확인(aarch64): numpy / scipy 12개, torch 7개, onnxruntime 6개, opencv-python-headless 2개, **pycolmap 0개**. **권장 구성:** Pi는 기체 쪽에서 촬영·스트리밍·사람 검출/추적(MOCK 제어만), 지도(SfM·밀집·학습 깊이)는 녹화 영상으로 Mac/PC에서. 실제 Pi에서의 fps·메모리 실측이 다음 테스트입니다.

## 10. 테스트 상태

- 최신(overview 지도·Follow 기본 ON 후, 22:23 KST): UI 스모크 **75 passed, 0 failed**(신규: 로드된 지도에 overview 표시, Follow 기본 ON, 재생 중 지도가 카메라를 따라감, 일시정지하면 사용자에게 조작이 넘어감, Reset이 Follow 해제) — `outputs/evidence/tests/ui-20261109-follow.log`, `outputs/evidence/ui/ui_smoke.json`. pytest **134 passed in 88.39 s**(재실행; 첫 실행은 `test_live_run_lifecycle_control_and_integrity` 등 2개가 일회성으로 실패 — 이 테스트는 전에도 간헐적으로 실패한 적이 있음) — `outputs/evidence/tests/pytest-20261109-follow.log`.
- 그 직전(steady 레이어 v3/v4·아카이브·뷰어 시점 변경 후, 22:11 KST): pytest **134 passed in 87.33 s**, UI **71 passed, 0 failed** — `outputs/evidence/tests/pytest_ui-20261109-steady-v4.log`. 모두 사용자가 `--remote-debugging-port=9222`로 열어 둔 브라우저 창에 붙여 실행했고 픽스처 탐색은 `outputs/archive/`도 봅니다.
- 더 이전 보관 로그: 134 passed(`pytest-20261109-183645.log`), UI 88/88(18:36 KST). UI 체크 수가 88 → 71 → 74로 바뀐 이유 중 88 → 71 구간은 이 문서에서 확인하지 않았습니다.

## 11. 다음 할 일 (우선순위)

1. `assets/input/`에 본인 영상을 두고 `scripts/build_sparse_map.sh … --origin REAL_VIDEO`, `scripts/run_tracking.sh … --origin REAL_VIDEO` 실행 → `REAL_VIDEO` 게이트 닫기.
2. (완료 2026-11-09 22:11) 이번 세션 pytest·UI 스모크 로그 기록 — `docs/VALIDATION_LOG.md` SL 블록. 남은 것: UI 체크 수 88 → 71 차이의 원인 확인.
3. 모델 출력 steady 레이어의 다음 검사: (a) 국소 보정된 28–38 s 구간의 점이 다른 뷰와 얼마나 일치하는지 별도 측정(지금은 근처 희소점 일치만 조건), (b) 검증 융합 레이어 쪽에서 그 구간을 채우려면 더 짧은 청크를 회전 안쪽에 앵커링(기존 R8 항목), (c) 반사면 점을 희소 지도 단계에서 거를 규칙(현재 없음), (d) 항공 영상에서 모델이 먼 지면을 누르는 정도를 거리대별로 기록.
4. 미채택 trial 레이어(학습 깊이 포함)의 채택 여부를 사용자가 결정; 조항을 바꾸려면 새 날짜 절과 새 기준선.
5. AprilTag/체커보드 미터 앵커링 → 그때까지 미터 거리 주장 없음.
6. 실제 웹캠 라이브 실행, Live 탭의 STALE_DATA/STALLED/FAILED/INCOMPLETE/CANCELLED 브라우저 검증, 보정 카메라로 VO 재측정(F-10).
7. `LICENSE` 결정.
8. Raspberry Pi 실측(9절): 검출+추적 fps·메모리, pycolmap 소스 빌드 가능 여부, 브라우저 뷰어 프레임 시간 — 추정치를 측정값으로 교체.

## 12. 근거 파일 (이 문서가 참조한 것)

`docs/PROGRESS.md`, `docs/VALIDATION_LOG.md`, `docs/FAILURES.md`, `docs/PLAN_LEARNED_DEPTH.md`, `README.md`, `outputs/evidence/learned_depth/{steady_layer_v2.json(runs fsc_v3_fused·sumida_v4_local_fix, findings gap_28_38s·pancake_view 포함),static_metrics_onpath*_*.json(최신 onpath3_fsc·onpath4_sumida),static_metrics_zipdemo_*.json,static_metrics_zipstyle_fsc.json,static_metrics_dense_hybrid_sumida.json,zip_demo_mac_compare.json(포즈 규약 수정 전, 수치 대체됨)}`, `outputs/maps/{mqg_fsc_campus_aerial,mqg_sumida_0025_0125,mqg_corridor}/*/dense_report.json`, `outputs/archive/manifest.json`, `outputs/evidence/ui/{fsc_onpath3.png,sumida_onpath4.png,fsc_follow_playing.png,fsc_paused_dragged.png,ui_smoke.json}`, `outputs/evidence/tests/{ui-20261109-follow.log,pytest-20261109-follow.log,pytest_ui-20261109-steady-v4.log,pytest-20261109-183645.log}`, `outputs/evidence/rpi/rpi_feasibility.json`.
