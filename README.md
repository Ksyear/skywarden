# Skywarden

카메라 영상 하나를 입력으로 받아 **3D 지도 생성 → 사람 검출·추적 → 제어 명령 생성**까지 이어지는 공간 인식 시스템을 처음부터 구축한 프로젝트입니다. 특정 기체를 만드는 것이 아니라, 각 단계를 교체 가능한 모듈로 나누고 단계마다 결과를 측정·검증하는 파이프라인과 그것을 운영하는 로컬 콘솔을 만드는 데 초점을 둡니다. 드론은 이 시스템의 한 가지 출력 대상이며, 지금은 가상 기체(MOCK)로만 연결됩니다. 모든 처리는 이 컴퓨터 안에서 끝나며 외부 API·업로드·실기체 명령이 없습니다.

## 시스템 설계 원칙

- **단계 분리와 계약.** 입력(영상·웹캠) → 프레임 선별 → 지도(SfM·MVS) → 스케일 → 검출·추적 → 제어 → 출력(Mock 기체)을 독립 모듈로 나누고, 단계 사이는 파일 형식과 스키마(`map_report.json`, `scale.json`, `timeline.jsonl` 등)로만 주고받습니다.
- **검증 우선.** 모든 산출물에 출처 라벨(`PUBLIC_DATASET` / `SIMULATED` / `MOCK`)과 신뢰 라벨(검증됨 / 모델 추정 / 미검증)을 붙이고, 합격 기준은 실행 전에 정해 결과를 보고 바꾸지 않습니다.
- **안전 게이트.** 출력 단계는 `HARDWARE_ARMED=false`·루프백 전용이 기본이며, 판단이 애매하면(두 사람이 겹침 등) 추격 대신 정지합니다.
- **관측 가능성.** 처리 시간·지연·누락 프레임·품질 판정을 기록하고 콘솔에서 바로 보여 줍니다.
- **재현성.** 고정 시드, 원시 증거 보관, 테스트 175개와 CI, 공개 데이터셋만 사용.

| 항공 영상 지도 (모델 추정 레이어) | 강변 산책 영상 지도 (다중 뷰 검증 레이어) |
|---|---|
| ![FSC 캠퍼스 항공 영상의 모델 출력 레이어](docs/images/01_fsc_aerial_model_output.png) | ![스미다 강변 산책 영상의 검증 밀집 레이어](docs/images/04_sumida_dense_verified.png) |
| **라이다 없는 미터 스케일 측정 (미검증 표시)** | **사람 추적과 MOCK yaw 명령 (송신 안 함)** |
| ![테니스장 항공 영상에서 두 점 거리 재기](docs/images/06_tennis_measure.png) | ![TUM 녹화에서 대상 LOCKED와 가상 yaw](docs/images/07_tracking_target.png) |

<sub>왼쪽 위: "Drone Footage: Florida Southern College FSC Drone Flyover 5 4K" by Audric Kraus, CC BY — 학습 모델이 추정한 깊이를 우리 SfM 카메라 경로에 놓은 표시 레이어이며 검증된 지도가 아닙니다. 오른쪽 위: "Sumida Park Tokyo Japan 4K Walking Tour …" by 4K Explore On Foot, CC BY — 여러 뷰로 확인된 점만 남긴 기본 지도 레이어. 왼쪽 아래: "Sue Lorimer and Bob Bruns Tennis Complex @ Folsom Lake College" by Zack Dowell, CC BY, https://www.youtube.com/watch?v=a5mun9m1CKY — 표시된 미터 값은 미검증. 오른쪽 아래: TUM RGB-D `fr3/walking_xyz`, CC BY 4.0, Sturm et al., IROS 2012 — 공개 데이터셋의 인물이며 식별·재식별은 하지 않습니다. 각 영상의 출처는 `data/public/*/SOURCE.md`. 화면은 2026-11-15 콘솔 캡처(`outputs/evidence/portfolio/screens.json`).</sub>

## 무엇을 하는가

1. **카메라 영상 → 3D 지도.** 영상을 프레임으로 고르고 pycolmap(COLMAP) SfM으로 희소 지도와 카메라 궤적을 만든 뒤, CPU 평면 스윕 MVS로 여러 뷰에서 확인된 점만 밀집 점군으로 더합니다. 지도마다 품질 판정(GOOD / PARTIAL / POOR)을 고정 임계값으로 기록합니다.
2. **사람 검출·추적.** YOLOX-nano(ONNX, OpenCV DNN, CPU)와 Kalman 다중 추적기. 두 사람이 겹쳐 구분할 수 없으면 대상을 `AMBIGUOUS`로 두고 정지합니다(잘못된 추격보다 정지).
3. **MOCK 드론 제어.** yaw 전용 PID → Tello SDK 2.0 텍스트 프로토콜 → 루프백 UDP `MockTello`. 실기체로는 아무것도 보내지 않습니다.
4. **라이다 없는 미터 스케일.** 지도 파일은 상대 스케일 그대로 두고, 인쇄 마커·두 점 실측·(MOCK) 텔레메트리 높이로 "지도 1단위 = 몇 m"를 별도 `scale.json`에 측정합니다. 독립 검증 길이 2개가 모두 5 % 이내일 때만 "확인됨"으로 표시합니다.
5. **로컬 콘솔.** `127.0.0.1` 전용 HTTP 서버 + WebGL 점군 뷰어(Mapping / Tracking / Live 탭, 거리 재기 도구).

## 핵심 수치 (실제로 실행해 측정한 것만)

이 Mac(Apple M6, 16 GiB, macOS 27.2) 한 대에서 실제로 실행한 결과입니다. 라벨: `PUBLIC_DATASET` = 공개 데이터셋·CC BY 공개 영상, `SIMULATED` = 합성, `MOCK` = 가상 기체. 상태 어휘는 PASS / FAIL / BLOCKED / NOT_RUN이고, 실패한 시도도 지우지 않고 적습니다. 사용자 본인 영상(`REAL_VIDEO`)으로는 아직 실행하지 않았습니다. 원시 증거(`outputs/`)는 용량 때문에 git에 넣지 않았고, 명령·버전·경로는 `docs/VALIDATION_LOG.md`에 기록돼 있습니다.

| 항목 | 데이터 (라벨) | 상태 | 수치 | 근거 |
|---|---|---|---|---|
| 희소 지도, 정답 궤적 비교 | TUM fr1/desk (PUBLIC_DATASET) | PASS | 전역 매퍼 53/53 포즈, ATE RMSE 1.66 cm (Sim(3) 스케일은 평가용으로만 빌림) | [VALIDATION_LOG](docs/VALIDATION_LOG.md) |
| 희소 지도, 한 테이크 영상 | 스미다 60 s / FSC 항공 71 s (PUBLIC_DATASET) | PASS, GOOD | 150/150 등록, 37,430점·0.512 px / 96,344점·0.708 px | [EXPERIMENTS](docs/EXPERIMENTS.md) |
| 검증 밀집 점군 (CPU) | 같은 두 지도 | PASS | 917,498점 / 1,942,876점 | [EXPERIMENTS](docs/EXPERIMENTS.md) |
| 미터 스케일, 정답 있는 실제 녹화 | TUM fr1/desk (PUBLIC_DATASET, Kinect 깊이·모션캡처 정답) | 1차 FAIL → 2차 PASS | 1차(전체 점) 스케일 오차 −11.8 %, 검증 1/2 통과. 2차(책상 점) −1.24 %, 검증 0.16 % · 1.22 %. 2차 점 기준은 1차 결과를 본 뒤 정함 | [PLAN_METRIC_SCALE 결과](docs/PLAN_METRIC_SCALE.md#10-결과) |
| 미터 스케일, 항공 영상 | 테니스장 29 s (PUBLIC_DATASET) | 수치 PASS 3/3, 라벨 `MEASURED_UNVERIFIED` | 검증 오차 4.906 / 0.477 / 4.925 % (5 % 이내지만 실측이 아닌 규정 치수라 검증으로 세지 않음) | 같은 절 |
| 미터 스케일, 합성 마커 | 합성 방 (SIMULATED) | 같은 벽 PASS / 다른 벽 FAIL | 같은 벽 두 마커 +0.39 %; 다른 벽 배치는 두 번째 마커가 3 프레임·45 px 미만이라 기각 | 같은 절 |
| 텔레메트리 높이 스케일 | MockTello (MOCK) | PASS | 오차 0.0025 %, 비루프백 송신 0회 | 같은 절 |
| 사람 검출+추적 | TUM fr3/walking_xyz (PUBLIC_DATASET) | PASS | 검출 p50 8.94 ms, 검출+추적 109.5 fps (MOTA/IDF1은 정답 박스가 없어 미정의) | [EXPERIMENTS](docs/EXPERIMENTS.md) |
| 라이브 실행, 원래 시간 기준 | 위트레흐트 1080p 30 s (PUBLIC_DATASET) | PASS | 29.96 fps, 지연 p95 24.2 ms. 대상 LOCKED는 프레임의 17 % | [EXPERIMENTS](docs/EXPERIMENTS.md) |
| 뷰어 렌더 (WebGL) | FSC 3,226,170점, dpr 1 / 2 | PASS | 프레임 p50 61.4 → 7.6 ms / 121.2 → 12.6 ms (이전 2D 캔버스 대비) | [VALIDATION_LOG](docs/VALIDATION_LOG.md) |
| 자동 테스트 | pytest / ruff / UI 스모크 | PASS | 175 passed × 5회 연속, ruff 통과, UI 117 passed · 0 failed · 4 skipped | [VALIDATION_LOG](docs/VALIDATION_LOG.md) |

## 구조

```mermaid
flowchart LR
  V[영상 파일 / 웹캠] --> F[프레임 선별]
  F --> S[pycolmap SfM<br/>희소 지도 + 궤적]
  S --> D[CPU 평면 스윕 MVS<br/>검증 밀집 점군]
  S --> M[scale.json<br/>마커 · 두 점 · 텔레메트리]
  V --> Y[YOLOX-nano<br/>사람 검출]
  Y --> K[Kalman 다중 추적<br/>대상 선택]
  K --> P[yaw PID]
  P --> T[MockTello<br/>루프백 UDP]
  S & D & M & K --> C[로컬 콘솔<br/>127.0.0.1:8780]
```

코드: `packages/mapping`(지도·스케일), `packages/vision`(검출·VO·라이브 실행), `packages/tracking`, `packages/control`, `packages/learned`(학습 깊이 모델 실행기, 별도 환경), `adapters/tello`(프로토콜·Mock·안전 게이트), `apps/desktop`(서버·뷰어), `tests/`.

## 빠른 시작

요구 사항: macOS arm64, Python 3.13, (선택) Node.js — 검증 환경 상세는 `docs/ENV_AUDIT.md`. `colmap` CLI는 필요 없습니다. 가중치와 데이터셋은 git에 없고 아래 스크립트와 `data/public/*/SOURCE.md`의 링크로 받습니다.

```zsh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # requirements.txt + ruff, yt-dlp
scripts/fetch_models.sh yolox_nano            # YOLOX-nano ONNX, SHA-256 확인 후 data/models/yolox/
scripts/run_demo.sh --check                   # 경로와 HARDWARE_ARMED=false DRY_RUN=true 확인
scripts/run_demo.sh                           # 콘솔 http://127.0.0.1:8780

# 영상 → 희소 지도 (assets/input/ 에 본인 영상)
scripts/build_sparse_map.sh assets/input/<clip>.mp4 --session my_room_01 --origin REAL_VIDEO
scripts/replay_map.sh outputs/maps/my_room_01

# 미터 스케일: 마커 인쇄 → 마커가 보이게 촬영 → 측정 (지도 파일은 RELATIVE 유지)
PYTHONPATH=packages .venv/bin/python -m mapping.markers make --ids 0 1 --side-mm 160
PYTHONPATH=packages .venv/bin/python -m mapping.cli scale markers outputs/maps/my_room_01 \
  --measurements assets/input/<clip>.measurements.json
PYTHONPATH=packages .venv/bin/python -m mapping.cli scale status outputs/maps/my_room_01

# 사람 추적, SIMULATED 폐루프(MockTello), 라이브 실행
scripts/run_tracking.sh assets/input/<clip>.mp4 my_people_01 --origin REAL_VIDEO
scripts/run_tracking_sim.sh
PYTHONPATH=packages .venv/bin/python -m vision.live_run assets/input/<clip>.mp4 --session live_01 --rate realtime --max-seconds 30

# 테스트
scripts/test_all.sh                           # pytest 전체, 로그는 outputs/evidence/tests/
.venv/bin/ruff check .
node tests/ui/ui_smoke.mjs --attach http://127.0.0.1:9222   # 또는 --browser <path> / CHROME_PATH
```

촬영 팁: 움직이는 사람이 적은 정적인 장면, 제자리 회전 대신 **옆으로 천천히 이동**, 질감 많은 벽·바닥, 30–90초, 컷 없는 한 테이크. 미터 스케일용 촬영 절차는 [PLAN_METRIC_SCALE 7절](docs/PLAN_METRIC_SCALE.md#7-사용자에게-요청할-촬영-방법).

## 안전 원칙

- **실기체 이륙·모터 명령·자동 추격 없음.** `HARDWARE_ARMED=false`가 기본이며, `TelloClient`는 `HARDWARE_ARMED=true` **그리고** `DRY_RUN=false`가 아니면 소켓을 열기 전에 멈춥니다. 모든 명령은 루프백의 `MockTello`로만 가고, 테스트가 비루프백 송신 0회를 확인합니다. 기체 구매·실제 비행은 하지 않습니다.
- **모든 지도는 `NOT_NAVIGATION_SAFE`.** 미터 스케일이 "확인됨"이어도 빈 곳·반사로 생긴 가짜 점은 그대로이므로 충돌 회피·항법에 쓰지 않습니다.
- **라벨을 섞지 않음.** 실제 영상으로 검증된 지도(`Dense · verified`)와 모델이 추정한 레이어(`Model output`, `MODEL_ESTIMATED`)를 UI와 보고서에서 구분하고, 합성 결과는 실제 데이터 게이트로 세지 않습니다.
- **박스 크기로 거리를 추정하지 않음**(실험 E1: p95 오차 61–90 %). 외모로 사람을 재식별하지 않음.
- 콘솔은 `127.0.0.1`에만 바인드되고, Host·Origin이 이 콘솔이 아니면 403, JSON이 아닌 POST는 415로 거부합니다.

## 한계

- **지도는 상대 스케일입니다.** 미터 스케일은 별도 측정으로만 붙고, 실제 녹화에서 "확인됨"까지 간 것은 TUM fr1/desk 한 건이며 그 점 기준은 첫 시도가 실패한 뒤 정했습니다. 항공 테니스장 결과는 규정 치수 기반이라 미검증이고, 폭 방향 길이가 4–5 % 길게 나오는 체계 오차의 원인(보정 없이 추정한 초점거리 가설)은 확인하지 않았습니다.
- **본인 영상(`REAL_VIDEO`)·실제 웹캠·실기체는 NOT_RUN.** 모든 결과는 공개 데이터셋, CC BY 공개 영상, 합성 장면, MOCK에서 나왔습니다.
- **한 대의 Mac에서만 검증했습니다.** 다른 OS·하드웨어는 NOT_RUN. GitHub Actions(run 43618986459, macos-15): fast 잡 159 passed / 11 skipped, full 잡(가중치·샘플 영상 받은 뒤) 175 passed로 PASS. 처음 4회는 FAIL — 가중치 없는 환경의 라이브 테스트, 느린 러너에서의 텔레메트리 기록기 경쟁(실제 버그, 수정)과 고정 대기 테스트 2개, 실행 권한 없는 `scripts/*.sh`.
- **지도는 오프라인입니다.** 영상 1분당 대략 6–12분. 편집 영상(컷 많음)과 고정 카메라 영상은 지도가 되지 않습니다.
- **밀집 지도 개선 시도는 모두 미채택.** 바닥 커버리지·정적 장면·학습 깊이 변형이 사전 기준 중 하나를 넘지 못해 trial로만 남았습니다([EXPERIMENTS](docs/EXPERIMENTS.md)).
- **추적은 모션만 봅니다.** 가림·교차 뒤 같은 id 재획득을 보장하지 않습니다.
- **미터 깊이 모델은 참고용입니다.** MapAnything·DA3METRIC·DA3NESTED·Pi3X를 로컬(MPS)에서 시험했지만 기본 레이어를 대체한 모델은 없고, 항공 영상에서 모델 간 미터 스케일이 최대 10.96배 달랐습니다. DA3NESTED와 Pi3X 가중치는 비상업(CC BY-NC 4.0)이라 로컬 연구 실행만 했고 저장소에 없습니다.
- 코드 라이선스(`LICENSE`)는 아직 정하지 않았습니다.

## 문서 지도

| 문서 | 내용 |
|---|---|
| [docs/SYSTEM_STATUS.md](docs/SYSTEM_STATUS.md) | 구성 요소별 현재 상태, 장단점, 소요 시간, 다음 할 일 |
| [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md) | 이전 README 전체: 기능 표, 콘솔 사용법, API, 실험별 검증 표와 절 |
| [docs/PLAN_METRIC_SCALE.md](docs/PLAN_METRIC_SCALE.md) | 미터 스케일 계획(사전 등록 기준)과 결과 |
| [docs/PROGRESS.md](docs/PROGRESS.md) · [docs/VALIDATION_LOG.md](docs/VALIDATION_LOG.md) | 마일스톤 상태표 · 실행 명령·버전·증거 경로 |
| [docs/FAILURES.md](docs/FAILURES.md) | 관찰된 실패와 다음 테스트 |
| [docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md) | 개선 항목 목록과 처리 상태 |
| [docs/DRONE_PURCHASE_AND_KOREA_RULES.md](docs/DRONE_PURCHASE_AND_KOREA_RULES.md) | 기체 선택안과 한국 비행 규정 (구매·비행은 하지 않음) |
| `docs/PLAN_*.md` | 실험별 계획과 끝의 Result 절 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 외 초기 설계 문서 | 2026-11-08 초기 설계 (현재와 다른 부분 있음) |

## 데이터 · 모델 출처

데이터와 가중치는 저장소에 포함하지 않고 출처 기록(`SOURCE.md`)만 둡니다. 모두 로컬에서만 처리했습니다.

| 자원 | 저자 / 라이선스 |
|---|---|
| TUM RGB-D `fr1/desk`, `fr3/walking_xyz` | Sturm et al., IROS 2012, CC BY 4.0 |
| 테니스장 항공 영상 (a5mun9m1CKY) | Zack Dowell, CC BY — https://www.youtube.com/watch?v=a5mun9m1CKY |
| FSC 캠퍼스 항공 (l5IB1ucby_w) | Audric Kraus, CC BY |
| 스미다 강변 산책 (Td9TYnibTl0) | 4K Explore On Foot, CC BY |
| 위트레흐트 공원 드론 (5xXMItmYKp4) | Terrell Neuage, CC BY |
| 방콕 공원 산책 (kjciube52gA), 몬트리올 거리 (bwJ-TNu0hGM) | Streets & Spaces \| Travel Walks / Montreal Walking Tours, CC BY |
| OpenCV `vtest.avi` | OpenCV 저장소 배포(Apache-2.0), 원 촬영 출처 미기재 |
| YOLOX-nano ONNX | Megvii YOLOX, Apache-2.0 (`data/models/yolox/SOURCE.md`) |
| 하늘·사람 분할 `skywater_fp32.onnx` | MIT (`data/models/static/SOURCE.md`) |
| LingBot-Map (학습 깊이, 외부 도구) | Apache-2.0. 별도 환경에서 실행; a LingBot-Map checkout supplied by the user는 점검 실행에만 사용 |
| 미터 깊이 모델 시험: MapAnything(`map-anything-apache`), Depth Anything 3 DA3METRIC-LARGE, DA3NESTED-GIANT-LARGE-1.1, Pi3X | 가중치 기준 Apache-2.0 / Apache-2.0 / **CC BY-NC 4.0** / **CC BY-NC 4.0** — 모두 별도 환경의 로컬 시험만, 코드·가중치는 저장소에 없음 |

영상별 제목·URL·다운로드 명령·SHA-256은 `data/public/*/SOURCE.md`에 있습니다. Tello 연동 근거는 Ryze Tello SDK 2.0 사용자 가이드(`docs/TELLO_SDK.md`)이며 실기체 연결은 이 저장소 범위 밖입니다.
