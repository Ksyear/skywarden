# Skywarden

**카메라 한 대로 만드는 희소 3D 지도와 사람 추적 콘솔 — 지상 검증 전용, 실기체 명령 0회.**

Skywarden은 스마트폰·웹캠·Tello EDU급 소형 드론의 단안 영상만으로 (1) 오프라인 희소 3D 지도(COLMAP sparse SfM)를 만들고, (2) 실제 사람 검출 모델과 Kalman 다중 추적기로 사람을 따라가며, (3) 그 결과를 yaw 전용 PID와 Tello SDK 2.0 Mock에 연결해 **비행 없이** 검증하는 로컬 macOS 프로젝트입니다. 모든 처리는 로컬 CPU에서 실행되며 외부 API나 업로드가 없습니다.

---

## 핵심 기능

| 영역 | 구현 내용 | 결과물 |
|---|---|---|
| 희소 3D 지도 | 영상 파일 또는 이미지 폴더 → 샷(컷) 검출·정지 카메라 판정 → 프레임 선별(블러·저질감·중복 제거, 컷을 넘는 매칭 없음) → pycolmap SIFT/매칭 → **전역(global) 매퍼 기본**, `--mapper incremental` 선택 → 모델별 PLY + 카메라 궤적 + 보고서(`skywarden/map_report@2`). 보정 파일이 없으면 COLMAP이 SIMPLE_RADIAL 내부 파라미터를 추정. 작업 상태(`status`)와 별개로 **품질 판정** `quality.verdict` GOOD / PARTIAL / POOR을 고정 임계값(등록률 ≥ 0.6, 시간 커버리지 ≥ 0.7, 내부 공백 ≤ 2 s, 재투영 ≤ 1.5 px, 10 % 이상 모델 정확히 1개, 시간 정규화 궤적 점프 0, 점 ≥ 2,000)과 함께 기록. 여러 모델은 각자 좌표계로 따로 내보내며 절대 합치지 않음. 표시용 점 필터(`sparse_filtered.ply`: 삼각측량각 < 1.5°, 트랙 < 3, 재투영 > 2 × 중앙값 제거)는 원본 PLY를 바꾸지 않음 | `outputs/maps/<session>/{sparse.ply,sparse_filtered.ply,trajectory.json,map_report.json,coverage.json,shots.json,models/<i>/,log.txt}` |
| 검증된 밀집 점군 (CPU) | GOOD 지도의 포즈로 CPU 평면 스윕 MVS(ZNCC, 이웃 4개 중 상위 2개, 640 px) → 뷰마다 희소점 일치율 ≥ 0.70 **그리고** 다중 뷰 일관성 ≥ 0.30을 통과한 깊이만 융합 → `DENSE_VERIFIED (relative scale)`. PatchMatch(CUDA/HIP 전용)는 이 Mac에서 BLOCKED. Poisson 메시는 시각 검사 FAIL로 **실험 단계**, 콘솔 레이어 아님 | `outputs/maps/<session>/dense/{dense_report.json,dense_verified.ply}` |
| 단안 시각 주행거리계 (VO, 오프라인) | 저장된 영상에서 프레임 간 상대 포즈 추적. 모든 출력에 `kind=VO, scale=RELATIVE, loop_closure=false, relocalization=NOT_IMPLEMENTED, NOT_NAVIGATION_SAFE` 라벨. 컷·추적 실패 시 새 세그먼트(독립 좌표계, 연결하지 않음) | `packages/vision/vo.py`, `outputs/evidence/vo/` |
| 라이브 실행 (한 시간축) | 영상 파일을 **원래 시간 기준**(`--rate realtime`) 또는 최대 속도(`--rate max`)로, 혹은 `webcam:N`을 입력으로 받아 검출 → Kalman 추적 → 대상 선택 → MOCK yaw → VO를 한 워커에서 처리. 처리가 밀리면 오래된 프레임을 버리고 **세어서 기록**(1칸 버퍼). 두 시계를 분리 기록: `media_t`(영상 시간)와 `run_t`(벽시계). `control.json`(순번 `seq`)로 pause / resume / seek / stop / select / clear; seek마다 `epoch`이 늘고 추적기·선택기·제어기·VO 세그먼트가 초기화. 완료 판정은 파일 기반: `timeline.jsonl` 줄 수·SHA-256·프레임 매니페스트가 맞을 때만 `SUCCEEDED`, 워커가 죽으면 `INCOMPLETE`, SIGTERM은 `CANCELLED`. 끝난 실행은 Tracking 탭에서 `OPEN_LOOP_REPLAY`로 재생. `map_position=NOT_AVAILABLE`, `relocalization=NOT_IMPLEMENTED`, 명령 `MOCK`·미송신 | `outputs/runs/<session>/{timeline.jsonl,run_report.json,tracks.json,status.json,frames/}` |
| 지도 콘솔 | 표준 라이브러리 HTTP 서버(`127.0.0.1:8780`) + 캔버스 3D 뷰어. 저장된 지도 로드, 브라우저에서 빌드 시작/취소, 진행 단계, 라벨 배지(`STORED_RECONSTRUCTION`, `SCALE RELATIVE`, `NOT_NAVIGATION_SAFE` 등), **품질 배지 GOOD / PARTIAL / POOR / UNRATED(READY 제거)**, 모델 선택기("partial: model i of N"), 샷 변화·궤적 점프를 표시하는 커버리지 띠(점프를 가로질러 경로를 그리지 않음), 기하 레이어 선택(희소 원본 / 필터 / 검증 밀집), 주 관측 영역 vs 전체 범위 프레이밍, `raw / filtered / drawn` 점 수 | `apps/desktop/` |
| 사람 검출·추적 | YOLOX-nano ONNX(Apache-2.0)를 OpenCV DNN CPU로 실행(`source=REAL_MODEL`), 등속 Kalman 기반 `MultiTracker`: 마지막 **신뢰** 검출 후 2.0 s가 지난 트랙을 연관 **이전에** 만료, 게이트 = Mahalanobis χ² 9.21 **그리고** 픽셀 거리 ≤ 0.5 × 박스 높이, 확정 트랙은 게이트 그래프의 연결 성분으로 연관(트랙 1 : 검출 1만 신뢰, 그 외는 `AMBIGUOUS` — 보정·갱신 없음, 해당 검출은 보류), 잠정(TENTATIVE) 트랙만 남은 검출에 게이트된 Hungarian 적용. 한 명을 고르는 `TargetSelector`는 LOST 후 다시 선택하기 전까지 LOST 유지(자동 재선택 없음) | `outputs/tracks/<session>/{tracks.json,tracking_report.json,status.json,frames/}` |
| Tracking 탭 | 프레임 위 박스 오버레이, 트랙 목록에서 대상 선택, 대상 상태(LOCKED / PREDICTING / AMBIGUOUS / LOST) 타임라인, 기록된 트랙 위에서 계산한 **가상** yaw 명령(`MOCK`, 송신 안 함). AMBIGUOUS 프레임은 배지와 `STOP · TARGET_AMBIGUOUS`로 표시되며 yaw는 0 | 콘솔 `Tracking` 탭 |
| 제어 · Mock 기체 | yaw 전용 `YawController`(PID, 클램프, anti-windup, STOP 사유), Tello SDK 2.0 텍스트 프로토콜 파서, 루프백 UDP `MockTello`, 가상 경계 `GeofenceGuard` | `packages/control/`, `adapters/tello/` |
| 안전 게이트 | `TelloClient`는 `HARDWARE_ARMED=true` **그리고** `DRY_RUN=false`가 아니면 소켓을 열기 전에 `HardwareDisarmed`를 발생시킴. 모든 실행 스크립트가 `HARDWARE_ARMED=false DRY_RUN=true`를 강제 | `adapters/tello/client.py`, `tests/test_tello.py` |

## 안전 원칙

- **실기체 이륙·모터 명령·자동 추격 없음.** 모든 명령 트래픽은 루프백 UDP의 `MockTello`로만 갑니다. 테스트는 4가지 환경 변수 조합에서 비루프백 주소로의 송신이 0회임을 확인합니다.
- **단안 지도는 상대 스케일.** 모든 지도는 `scale=RELATIVE`, `NOT_NAVIGATION_SAFE`로 라벨됩니다. 미터 거리, 장애물 회피, 충돌 안전을 보장하지 않습니다. TUM 정답 궤적 대비 ATE는 평가용으로만 스케일을 빌려 계산했고 저장된 지도는 여전히 상대 스케일입니다.
- **박스 크기로 거리를 추정하지 않습니다.** 실험 E1에서 측정한 결과(아래 표) 때문에 이 규칙은 설계가 아니라 측정으로 뒷받침됩니다. lateral / forward / vertical 축 명령은 항상 0입니다.
- **신원 재식별(Re-ID) 없음.** 대상이 LOST되면 자동 재획득하지 않고 새 id가 부여됩니다. `TargetSelector`는 운영자가 다시 선택할 때까지 LOST를 유지하고, 같은 자리에 새 id가 나타나도 고르지 않습니다. 외모 특징을 저장하지 않습니다.
- **경합하면 정지.** 다른 사람의 검출이 대상의 게이트 안에 들어오면(또는 한 검출을 두 트랙이 다투면) 대상은 `AMBIGUOUS`가 되어 Kalman 보정도 생존 시간 연장도 받지 않고, `YawController`는 `TARGET_AMBIGUOUS`로 STOP합니다. 모션만으로는 두 사람을 구분할 수 없기 때문에 잘못된 LOCK 대신 정지를 택한 것이며, 그 대가로 교차 후 같은 id 재획득은 보장되지 않고 혼잡한 장면에서는 STOP 시간이 늘고 트랙이 짧아집니다(실험 E2, 아래 표).
- **합성 데이터와 실제 데이터 분리.** `SIMULATED` 결과는 실제 데이터 게이트 통과로 집계하지 않으며, 파이프라인은 합성 입력에 실제 라벨을 붙이는 것을 거부합니다.
- 콘솔은 `127.0.0.1`에만 바인드되고 `/api/health`의 `hardware_armed`는 항상 `false`입니다.

## 요구 사항

검증된 환경(`docs/ENV_AUDIT.md`): macOS 27.2 arm64, Apple M6, 16 GiB RAM. 다른 환경은 NOT_RUN입니다.

| 항목 | 버전 | 용도 |
|---|---|---|
| Python | 3.13.16 (`python3 -m venv .venv`) | 전부 |
| pycolmap | 4.2.1 (CPU SIFT, CUDA·dense MVS 없음) | 지도 |
| opencv-python-headless | 5.0.0.93 | 영상 읽기, ONNX 추론(`cv2.dnn`) |
| numpy / scipy / pytest | 2.5.4 / 1.18.1 / 9.2.0 | 추적기, 평가, 테스트 |
| Node.js | v24.21.0 (선택) | `tests/ui/ui_smoke.mjs`만 사용. `--browser`는 `/Applications/Dia.app`을 헤드리스로 띄우고, `--attach http://127.0.0.1:9222`는 사용자가 `--remote-debugging-port=9222`로 이미 연 Dia 창에 탭 하나를 열어 테스트 후 그 탭만 닫음 |
| ffmpeg | 9.0.2 (선택) | 영상 사전 변환용. 파이프라인 자체는 `cv2.VideoCapture`로 읽음 |

`colmap` CLI는 필요 없습니다. 모델 가중치(`data/models/yolox/*.onnx`)와 데이터셋(`data/public/*/`)은 git에 포함되지 않으며 아래 스크립트로 받습니다.

## 빠른 시작

```zsh
git clone <this repo> skywarden && cd skywarden

# 0) 프로젝트 전용 venv. 시스템 Python은 건드리지 않음
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1) 사람 검출 가중치 (YOLOX-nano ONNX 3.66 MB, SHA-256 검증 후 data/models/yolox/ 에 저장)
scripts/fetch_models.sh yolox_nano

# 2) 로컬 콘솔: http://127.0.0.1:8780 (포트 충돌 시 --port <free port>)
scripts/run_demo.sh --check   # 경로와 HARDWARE_ARMED=false DRY_RUN=true 만 출력하고 종료
scripts/run_demo.sh

# 3) 영상 또는 이미지 폴더 -> 희소 3D 지도
scripts/build_sparse_map.sh assets/input/<clip>.mp4 --session my_room_01 --origin REAL_VIDEO
#    옵션: --camera-json FILE  --fps 3  --max-frames 150  --origin REAL_VIDEO|PUBLIC_DATASET|SYNTHETIC

# 4) 저장된 지도 요약 출력 (콘솔의 "Stored maps" 에서도 로드 가능)
scripts/replay_map.sh outputs/maps/my_room_01

# 5) 녹화 영상 -> YOLOX-nano(CPU) -> Kalman 다중 추적 (드론 명령 생성 없음)
scripts/run_tracking.sh assets/input/<clip>.mp4 my_people_01 --origin REAL_VIDEO
#    옵션: --model yolox_nano  --fps 10  --max-frames 600  --score-thr 0.35

# 6) SIMULATED 폐루프 약 20 s: 합성 검출기 -> Kalman -> yaw PID -> MockTello (루프백 UDP)
scripts/run_tracking_sim.sh

# 7) 라이브 실행: 영상을 원래 시간 기준으로(또는 webcam:0) 검출+추적+MOCK yaw+VO 한 시간축에 기록 (콘솔 Live 탭과 동일)
PYTHONPATH=packages .venv/bin/python -m vision.live_run assets/input/<clip>.mp4 --session live_01 --rate realtime --max-seconds 30
#    옵션: --rate realtime|max  --max-seconds S  --no-vo  --fx F | --calib camera.json  --vo-width 640  --runs-root outputs/runs
#    실행 중 제어: outputs/runs/live_01/control.json 에 {"seq": n, "op": "pause|resume|seek|stop|select|clear", "args": {...}} 를 원자적으로 기록
PYTHONPATH=packages .venv/bin/python -m vision.live_run --verify outputs/runs/live_01   # 줄 수·SHA-256·프레임 검증만 수행

# 8) 자동 테스트 (원시 로그를 outputs/evidence/tests/ 에 남김) / 헤드리스 UI 테스트
scripts/test_all.sh
node tests/ui/ui_smoke.mjs
```

촬영 팁(지도용): 움직이는 사람·물체가 없는 정적인 실내, 제자리 회전이 아니라 **옆으로 천천히 이동**, 질감 많은 벽·가구, 밝은 조명, 30–90초, 1080p 이하. 고정 카메라 영상은 베이스라인이 없어 지도가 만들어지지 않으며, 이 경우 `map_report.json`의 `failure.message`에 다음 조치가 적힙니다.

## 콘솔 사용법

![설계 단계 레이아웃 목업 — 실제 실행 화면이나 결과가 아니며 표시된 수치는 모두 가상입니다](assets/design/mockup_map_console_v1.png)

위 이미지는 UI 작성 전에 만든 **레이아웃 목업**입니다(`docs/DESIGN_ASSETS.md`). 실제 콘솔은 같은 분할(카메라 프레임 | 3D 지도 | 우측 제어 열)을 따르되, 배지와 수치는 전부 `map_report.json` / `tracking_report.json`에서 읽습니다.

**Mapping 탭** — 입력 선택 후 `Build map`으로 빌드(단계: Frames → Features → Matching → Mapping → Export, 취소 가능), `Stored maps`에서 저장된 세션 로드. 프레임 스크러버를 움직이면 궤적과 점군이 그 시점까지 그려집니다. 하단 상태 줄에 등록 프레임 수, 3D 점 수, 재투영 오차, 렌더 시간이 표시됩니다.

**Tracking 탭** — 입력과 검출기(로컬에서 해시가 확인된 모델만 목록에 뜸)를 고르고 `Run tracking`. 완료된 실행은 `Recorded runs`에서 재생합니다. 프레임 위 박스(대상 강조, PREDICTING 점선, TENTATIVE 점박이, 원시 검출 토글), 트랙 목록, 대상 상태 배지, 가상 yaw 바와 STOP 사유, 상태별 색 타임라인이 표시됩니다. 대상 선택은 **개루프 재생**입니다: 녹화 카메라는 돌지 않았으므로 yaw 값은 `MOCK · NOT SENT`로만 표시됩니다. 끝난 라이브 실행도 이 목록에 `LIVE RUN · OPEN_LOOP_REPLAY`로 올라옵니다. 지도 빌드·추적 실행·라이브 실행은 한 번에 하나만 가능합니다(409).

**Live 탭** — 입력(영상 파일 또는 `webcam:N`), 속도(원래 시간 기준 / 최대 속도), 최대 초(기본 30)를 고르고 `Start`. 워커는 매 프레임을 검출 → 추적 → 대상 선택 → MOCK yaw → VO로 처리해 `timeline.jsonl`에 기록하고, 콘솔은 250 ms마다 `status.json`과 `frames/latest.jpg`를 읽어 최신 프레임과 박스를 그립니다. 상단 배지는 `LIVE / PAUSED / STALLED / STALE_DATA / DISCONNECTED / SUCCEEDED / FAILED / INCOMPLETE / CANCELLED`, 하단 상태 줄은 처리 fps · 지연 p95 · 버린 프레임 수 · epoch · 데이터 나이입니다. **확정(LOCKED) 박스를 클릭**하면 대상이 되고(`SELECT` 이벤트), LOST 뒤에는 다시 골라야 합니다(자동 재선택 없음). `Pause`/`Resume`/시크 슬라이더(파일만)/`Stop`은 `control.json`을 거쳐 워커에 전달되며, seek는 추적기·선택기·제어기·VO 세그먼트를 초기화합니다. "Camera pose (VO)" 패널은 **현재 세그먼트의 상대 스케일 경로**일 뿐 지도가 아니며 `Map position: NOT_AVAILABLE`로 표시됩니다. yaw는 라이브 파이프라인이 계산한 `MOCK` 값이고 송신되지 않습니다. `Stop`으로 끝낸 실행은 파일 검증 후 `SUCCEEDED (STOP_REQUESTED)`가 되고 Tracking 탭의 `Recorded runs`에서 재생할 수 있습니다.

| 키 | 동작 |
|---|---|
| `M` / `T` / `L` | Mapping / Tracking / Live 탭 전환(URL 해시 `#tracking` 유지) |
| `Space`, `←` / `→` | 재생·일시정지, 한 프레임 이동 (Live 탭에서는 `Space`가 워커에 pause/resume 전달) |
| `R`, `+` / `-`, `G` | (Mapping) 뷰 리셋, 줌, 지도 성장 모드 토글 |
| `1`–`9`, `0`, 박스 클릭 | (Tracking) n번째 트랙을 대상으로 선택, 대상 해제, 클릭 선택. (Live) LOCKED 박스 클릭으로만 선택, `Clear target` 버튼으로 해제 |
| `Esc` | 취소 확인 창 닫기 |

## 프로젝트 구조

```
apps/desktop/            # server.py (stdlib HTTP, 127.0.0.1:8780) + static/{index.html,app.js,tracking.js,live.js,viewer.js,app.css}
packages/
  mapping/               # frame_source, frame_selection, shots(컷·정지 카메라 검출), sparse_pipeline(pycolmap, 전역/증분 매퍼), quality(판정·임계값), export(모델별 PLY/trajectory, 점 필터), diagnose(구간 진단), dense(CPU 평면 스윕 MVS + 검증), evaluate(ATE), replay, labels, cli(build / rate)
  vision/                # person_detector(YoloxDetector), track_video(offline job), bbox_distance_eval(E1), vo(단안 VO), live_source(FileSource realtime|max, WebcamSource, 1칸 LatestFrameBuffer), live_run(한 시간축 워커: timeline.jsonl / status.json / run_report.json / tracks.json, control.json, --verify)
  tracking/              # tracker: Detection, MultiTracker(Kalman, 만료 우선, 게이트 그래프 성분 연관 + AMBIGUOUS, 잠정 트랙만 Hungarian), TargetSelector(sticky LOST)
  control/               # yaw_controller(PID, SafetyLimits, DroneCommand), virtual_replay, geofence(Geofence, GeofenceGuard)
  sim/                   # synthetic_scene, tracking_scenario(SIMULATED 폐루프), two_person_crossing(E2), geofence_scenario(E3)
adapters/tello/          # protocol(SDK 2.0 텍스트), client(TelloClient + HardwareDisarmed 게이트), mock_tello(MockTello, 루프백 UDP)
scripts/                 # run_demo.sh, build_sparse_map.sh, replay_map.sh, run_tracking.sh, run_tracking_sim.sh, fetch_models.sh, test_all.sh
tests/                   # pytest 17개 모듈(124 tests; test_live_run 11, test_quality 8, test_export_geometry 3, test_vo 3 포함) + ui/ui_smoke.mjs(73 checks)
data/public/<set>/SOURCE.md   # 데이터셋 출처·라이선스·SHA-256 (데이터 자체는 git 제외)
data/models/yolox/SOURCE.md   # 가중치 출처·SHA-256 (가중치는 git 제외)
assets/input/            # 사용자 본인 영상을 두는 곳 (git 제외)
assets/design/           # 레이아웃 목업 이미지 (결과 아님)
outputs/                 # maps/, tracks/, runs/(라이브 실행), evidence/ — 실행 산출물 (git 제외)
docs/                    # 설계·계획·검증 기록
```

서버 API: `GET /api/health`, `/api/inputs`, `/api/sessions[/<id>[/files/*]]`, `/api/models`, `/api/tracks[/<id>[/files/*]]`, `/api/runs`, `/api/runs/<id>`(상태 + 완료 검증, 워커가 죽었으면 `INCOMPLETE`), `/api/runs/<id>/latest`, `/api/runs/<id>/vo`, `/api/runs/<id>/files/{timeline.jsonl,run_report.json,tracks.json,frames/latest.jpg,frames/fNNNNNN.jpg}`; `POST /api/jobs`, `/api/jobs/<id>/cancel`, `/api/track-jobs`, `/api/track-jobs/<id>/cancel`, `/api/tracks/<id>/control`, `/api/run-jobs`(`{input_id | "webcam:N", rate, session}`, 202), `/api/run-jobs/<id>/cancel`(SIGTERM → `CANCELLED`), `/api/runs/<id>/control`(`{op, args}` → `control.json`에 순번 부여, 202; 실행 중이 아니면 409, 모르는 op는 400), `/api/runs/<id>/replay`(끝난 실행의 `tracks.json`을 `OPEN_LOOP_REPLAY`로). 지도 빌드·추적·라이브 실행은 통틀어 한 번에 하나(409). 세션 id로 `.`/`..`는 거부되고 파일 경로는 허용 목록으로 제한되며, 라이브 입력은 입력 목록의 id 또는 `webcam:N` 문자열만 받습니다.

## 검증 결과

전 행이 이 Mac에서 실제 실행된 명령과 원시 산출물(`outputs/evidence/`, `outputs/maps/`, `outputs/tracks/`)에 근거합니다. 전체 명령·버전·경로는 `docs/VALIDATION_LOG.md`, 실패와 후속 조치는 `docs/FAILURES.md`에 있습니다. 상태 어휘는 PASS / FAIL / BLOCKED / NOT_RUN 네 가지만 씁니다.

| 항목 | 입력 (라벨) | 상태 | 핵심 수치 |
|---|---|---|---|
| 희소 지도, OPENCV 4계수 보정 | TUM fr1/desk, 613 → 53 프레임 (PUBLIC_DATASET) | PASS | 49/53 등록(92.5 %), 4,297점, 재투영 0.831 px, 약 5 s. ATE RMSE 2.05 cm(Sim(3), 정답 스케일 차용, 평가 전용) |
| 희소 지도, 보정 없이 추정 | 동일 | PASS | 48/53 등록, 3,987점, 0.665 px, ATE 1.86 cm |
| 희소 지도, 공식 FULL_OPENCV 5계수 보정 | 동일 | FAIL | 초기 이미지 쌍 생성 실패. 원인 미확인(F-01), 위 두 경로로 복구 |
| 희소 지도, 편집된 공원 산책 영상 60 s (방콕), **기준선** | CC BY YouTube 클립 (PUBLIC_DATASET) | PASS (빌드); 오프라인 품질 판정 **PARTIAL** | 49/150 등록(0.327), 10,814점, 0.848 px, 약 5.9 min. 4개 모델(49/16/12/8), 시간 커버리지 0.302, 궤적 점프 13회(25.3–37.3 s). 60 s 구간에 컷 18개 — 최대 모델이 여러 컷을 가로질러 이어진 것으로 보임(추론) — `outputs/evidence/map_quality/baseline_bangkok_60s/` |
| 희소 지도, 같은 방콕 영상, 컷 존중 매칭 후 | 동일, `--max-frames 300` | PASS (빌드); 판정 **POOR** (개선 없음) | 증분 매퍼: 176 프레임 중 최대 모델 12(0.068), 모델 4개가 각각 한 샷 안, 점프 0. 전역 매퍼: 모델 14개, 최대 15/176(0.085). 컷을 넘는 연결이 사라지며 점프는 0이 됐지만 커버리지는 0.327 → 0.068. 컷 18개짜리 편집 영상은 지도 한 장이 되지 않음(기록만, 수정 없음) |
| 희소 지도, 강변 산책 한 테이크 60 s (스미다) | CC BY YouTube 클립, 컷 0 (PUBLIC_DATASET) | PASS, 판정 **GOOD** (증분·전역 모두) | 증분: 150/150 등록, 33,904점(필터 27,524), 0.454 px, 점프 0, MAP 38.5 s. 전역: 150/150, 37,430점(필터 28,559), 0.512 px, 점프 0, MAP 93.2 s. 두 매퍼 경로 일치(Sim(3), 150 포즈): RMSE 경로 길이의 1.75 %, 마지막 프레임(59.7 s)에서 최대 10.28 % — 정답이 없어 어느 쪽이 맞는지는 미결 (`outputs/evidence/geometry/sumida_global_vs_incremental.json`) |
| 희소 지도, 캠퍼스 항공 촬영 71 s (FSC) | CC BY YouTube 클립, 컷 0 (PUBLIC_DATASET) | 증분 **PARTIAL** → 전역 **GOOD** | 증분: 150/150, 99,293점, 0.837 px, 59.7–70.7 s에 시간 정규화 점프 **23회**, MAP 271.8 s. 전역: 150/150, 96,344점, 0.708 px, 점프 **0**, MAP 116.7 s. 증분 지도의 59.7–71 s 구간 카메라 속도는 클립 중앙값의 10.3×(그 전 0.97–1.06×)인데 같은 구간의 영상 흐름은 0.34–0.41 px/frame로 앞부분(0.43–1.18)보다 작음; 전역 매퍼는 1.04× (`outputs/evidence/geometry/fsc_late_stretch_speed.json`). 증분 드리프트의 원인은 미확인. 전역 지도에서 야구장이 형태를 유지(`views/fsc_global_topview.jpg`, 이미지 관찰) |
| 희소 지도, 사람을 따라가는 드론 영상 60 s (위트레흐트), 5 fps | CC BY YouTube 클립 (PUBLIC_DATASET) | PASS (빌드); **PARTIAL** (두 매퍼 모두, 미해결) | 증분: 모델 2개 216 + 37 / 298. 전역: 한 모델 298/298, 81,007점, 0.517 px이나 점프 43회(0.4–7.2 s 구간 6–12배 늘어남, 44.6–45.8 s·50.3–51.1 s 급등). 피사체가 화면을 크게 차지하는 영상. 사람 박스 마스킹 실험(`outputs/experiments/utrecht_global_masked/`, 마스크 픽셀 중앙값 20.6 %): 점프 43 → 38, 50.3–51 s 급등 소멸, 44.6–45.6 s 급등 215× → 77×, 시작 구간은 그대로, 여전히 PARTIAL — 스미다·FSC 회귀 가드를 마스킹과 함께 돌리지 않아 **미채택**(F-11) |
| 희소 지도, 고정 카메라 거리 영상 (몬트리올) | CC BY YouTube 클립 (PUBLIC_DATASET) | FAIL (예상된 실패; 측정 근거 있는 거부) | "camera is static (median flow 0.006 px/frame < 0.1) for 100% of the input" — COLMAP 실행 전에 정지 카메라로 판정해 거부, 추적용으로 안내 |
| 프레임 간격 가드 (첫 버전) | 방콕·위트레흐트 3개 세션 | **FAIL** → 가드 축소 | 1.0 s 초과 간격을 모두 치명 처리해 1.33 / 1.335 / 1.001 s 간격의 입력을 거부(정답 있는 TUM fr1/desk도 거부, 구현자 기록). 이제 150 프레임 캡이 간격의 원인일 때만 치명(F-07) |
| TUM fr1/desk, 증분 vs 전역 매퍼, 정답 궤적 대비 | PUBLIC_DATASET, OPENCV 4계수 | PASS | 증분 49 포즈 ATE RMSE 2.05 cm / 최대 6.81 cm; **전역 53/53 포즈 1.66 cm / 5.01 cm**, 4,351점, 0.650 px. Sim(3) 스케일은 평가용으로만 차용 — `outputs/evidence/geometry/global_vs_incremental_tum_ate.json` |
| 내보내기·표시 vs 복원 오류 분리 | 스미다·FSC·TUM 세션 3개 | PASS (결함은 복원 단계) | 내보낸 포즈·점을 COLMAP 투영과 재비교: 최대 차이 0.0078 / 0.0136 / 0.00008 px, 카메라 뒤 점 0, 포즈 순서 = 시간 순서 — `outputs/evidence/geometry/verify_export_*.json` |
| 구간별 LOW_CONFIDENCE 검출기 (5 s 창) | 스미다·FSC 증분 지도 | **FAIL** (구분하지 못함; 보고만, 조정 안 함) | 사전 기대: 스미다 0개, FSC 59.7–70.4 s 창에 플래그. 관측: 스미다 23개 중 9개 플래그, FSC 28개 중 0개. 드리프트 원인은 이 검출기로 확인되지 않음(F-08) |
| 검증된 밀집 점군, 스미다 (전역 포즈) | GOOD 지도 | PASS (수용 기준 3개 모두 충족) | 참조 뷰 117개 중 108개 VERIFIED, 융합 917,498점(덮인 뷰 희소점 36,470의 25배), 58.6 s. v1(경계 깊이 미제거) 104 뷰 / 1,120,031점 보존. 카메라 바로 앞 보도는 대부분 비어 있음 — `outputs/maps/mqg_sumida_0025_0125/dense/` |
| 검증된 밀집 점군, FSC (전역 포즈) | GOOD 지도 | PASS | 145/145 뷰 VERIFIED, 1,942,876점(희소의 20배), 76.8 s — `outputs/maps/mqg_fsc_campus_aerial/dense/` |
| 밀집 수용 기준, 합성 방 (정답 표면 있음) | 생성기 8 뷰 (SIMULATED) | 실행 PASS; 수용 **FAIL** 3/8 (정의상) | 밀집점의 실제 표면까지 거리 p50 0.0095, 97.8 %가 0.05 룸 단위 이내(희소 p50 0.0035). 고정된 일관성 비율이 겹치지 않는 픽셀을 분모에 넣어 FAIL; 정의는 바꾸지 않음 |
| PatchMatch 밀집 스테레오 | pycolmap 4.2.1 | BLOCKED | "requires CUDA or HIP" — 이 Mac에서 재시도 없음 (`outputs/evidence/dense/patch_match_probe.txt`) |
| Poisson 메시 (검증 밀집점 기반) | 스미다·FSC | 실험 단계; 시각 검사 **FAIL** | FSC: 지붕·나무는 보이나 화면 중심으로 모이는 방사형 줄무늬(원인 미확인); 스미다: 나무 몇 그루만. 콘솔 레이어로 제공하지 않음(F-09) |
| 단안 VO (오프라인) | 합성 방 / 스미다·방콕·몬트리올 0–60 s | 기준 6개 PASS, **1개 FAIL** | 합성 ATE 1.82 %(≤ 5 %), 순수 회전 yaw 19.995° vs 20°, 컷 → 새 세그먼트, 방콕 컷 재현율 16/18 = 0.889, 몬트리올 삼각대 TRACKING 0 %, p50 2.6–4.8 ms/frame PASS. **스미다 vs SfM 궤적 23.3 %(≤ 15 %) FAIL**(초점거리 추정 0.9 × 폭); SfM 초점거리를 빌리면 2.56 %이나 독립 증거 아님(F-10) — `outputs/evidence/vo/` |
| 희소 지도, 합성 방 | 생성기 8프레임 (SIMULATED) | PASS (파이프라인 점검용) | 8/8 등록, 3,845점, 0.084 px, ATE 0.5 mm. 실제 데이터 게이트에 집계하지 않음 |
| 라이브 실행, 사람을 따라가는 드론 영상 1080p, 원래 시간 기준 30 s (위트레흐트) | CC BY YouTube 클립 (REAL_MODEL / PUBLIC_DATASET), 스크립트가 운영자 역할로 select | PASS (사전 기준 7/7); 대상 LOCKED는 프레임의 17 %뿐 | 처리 29.96 fps(기준 ≥ 8), 지연 p50 19.5 / p95 24.2 ms(기준 ≤ 250), 캡처 900 · 처리 899 · 버림 1(= 캡처 − 처리), 프레임 파일 899 = 프레임 행, 908행·SHA-256 검증 `SUCCEEDED`. 검출 9.8/11.4 ms, VO 5.0/8.7 ms. 대상 상태 LOCKED 155 / PREDICTING 576 / AMBIGUOUS 74 / LOST 82 / SEARCHING 12 (899 프레임 행), LOST 뒤 재선택 6회(select 7회 적용, 적용 지연 p50 60.5 / 최대 86.4 ms). MOCK yaw −40(한계)…+12, 149행, LOCKED 아닐 때 yaw ≠ 0 **0회**, 미송신. VO 세그먼트 2개(5.0 s에 기하 검증 실패로 분할), 포즈 없는 구간 2개 기록, 초점거리 추정(UNCALIBRATED)이라 경로는 상대·미검증. 재생 `OPEN_LOOP_REPLAY`, `nonzero_yaw_while_not_locked 0` — `outputs/evidence/live/t7_utrecht_realtime_30s.json` |
| 라이브 실행, 같은 구간 최대 속도 | 동일, 선택 없음 | PASS (실행) | 47.78 fps, 902 캡처 / 901 처리 / 1 버림, 30.03 s 영상을 18.93 s에 처리, 전 구간 SEARCHING(yaw 0). 지연 p50 61.5 / p95 68.7 ms는 프레임을 즉시 넘기는 모드라 실시간 수치와 비교 불가 — `utrecht_max_30s.json` |
| 라이브 실행, 실시간 페이싱·일시정지·시크·정지 (vtest.avi 10 fps) | 개발용 클립 | PASS | 10 s 영상 → 10.044 s 실행(±0.5 s 이내), `media_t` 단조 증가. 일시정지 2.07 s 동안 프레임 행 0 · PAUSED 하트비트 4행 · `media_now` 고정(2.0794), 재개 후 2.0 → 2.1 s 이어짐. 제어 적용 지연 pause 53.8 / resume 58.1 / 모르는 op 81.8(`CONTROL_IGNORED`) / seek 115.8 / stop 69.1 ms. seek 3.27 → 40 s: epoch 0 → 1, 트랙 비움, SEARCHING, VO 세그먼트 1 항등 포즈. stop → `SUCCEEDED (STOP_REQUESTED)` — `t2_vtest_realtime_pacing_10s.json`, `t2_t3_vtest_transport.json` |
| 라이브 실행, 안전·지속성 | 스텁 파이프라인 / SIGKILL / SIGTERM / 파일 변조 (`tests/test_live_run.py` 11개, `tests/test_server.py` 1개) | PASS | AMBIGUOUS·PREDICTING·LOST·입력 정지 0.5 s·일시정지 구간 모두 yaw 0 + 사유, LOST는 select 전까지 유지. SIGKILL → `INCOMPLETE`(마지막 fsync까지 읽힘), SIGTERM → `CANCELLED`(부분 파일 보존), 완료 후 `timeline.jsonl`에 한 줄 추가 → API가 `INCOMPLETE`로 전환. 1칸 버퍼 `dropped == captured − processed` 정확 |
| 라이브 실행, `webcam:0` (카메라 없는 Mac) | `system_profiler` 카메라 목록 비어 있음 | PASS (무장치 분기); 실제 웹캠 **NOT_RUN** | 0.285 s 만에 `FAILED` / `OPEN_SOURCE` / `NO_DEVICE`(기준 5 s 이내), 부분 SUCCEEDED 없음 — `t6_webcam0_no_device.json`. 물리 카메라가 없어 실제 웹캠 측정은 수행하지 못함 |
| **본인 영상 지도·추적 (REAL_VIDEO)** | `assets/input/` | **NOT_RUN** | `.gitkeep`만 존재. 위 공개 클립은 타인 영상이라 이 게이트를 닫지 못함 |
| 사람 추적, 주 증거 | TUM fr3/walking_xyz 859 → 287 프레임 (REAL_MODEL / PUBLIC_DATASET) | PASS | 검출 p50/p95 8.94/10.45 ms, 109.5 fps(검출+추적), 349 검출, 트랙 33개 중 24개 확정, MOTA/IDF1 미정의 |
| 사람 추적, 스트레스 클립 | OpenCV `vtest.avi` 600 프레임 | PASS | 105.0 fps, 3,856 검출, 39개 중 23개 확정, 확정 지속 p50 13.0 s / 최대 52.1 s |
| 사람 추적, 혼잡한 거리 | CC BY YouTube 클립 1,390 프레임 | PASS | 90.6 fps, 10,916 검출, 438개 중 222개 확정, Re-ID가 없어 189 new ids/min |
| SIMULATED 폐루프 | 합성 검출기 → Kalman → yaw PID → MockTello | PASS | 300 프레임/20 s, 15.04 Hz, 처리 0.108/0.18 ms p50/p95, 중심 오차 106.4 → 21.3 px, 0.5/1.5 s 가림 재획득, 2.5 s 가림 → LOST, LOCKED 아닐 때 yaw 0회, rc 패킷 301건 모두 Mock |
| E1 박스 크기로 거리 추정이 쓸 만한가 | 위 TUM 검출 329건 vs 등록된 Kinect 깊이 0.94–9.33 m | 실행 PASS, **판정 FAIL** | 키 1.70 m 가정: 상대 오차 p50 43 % / p95 90 %. 첫 20 %로 보정(1.387 m): p50 26 % / p95 61 %, 절대 오차 p50 0.43 m / p95 1.16 m. 사전 기준 p95 ≤ 25 % 미달 |
| E2 가림 중 다른 사람이 지나가면 대상 id가 유지되는가 — **수정 전 추적기** (보존된 기준선) | 합성 검출 700회(50 seed × 7 가림 × B 유무) (SIMULATED) | 실행 PASS, **판정 FAIL** (K1 PASS, K2 FAIL, K3 FAIL) | K1 0.5/1.5 s 가림 후 같은 id 재획득 100 %. K2 2.5 s 가림 후 LOST 10 %(기준 100 %): B의 검출이 coasting 트랙을 연장. K3 대상 id가 B 위에 LOCKED된 프레임이 있는 실행 158/350(기준 0). 같은 추적기로 측정한 제어 경로: A가 숨은 동안 LOCKED 162/167 프레임, B를 향한 yaw ≠ 0 100/167 프레임(중앙/오프센터 교차) — `outputs/evidence/e2_two_person_baseline_v1{,_control}/` |
| E2 재실행 — **수정 후 추적기** (만료 우선 + 게이트 그래프 AMBIGUOUS + 픽셀 게이트, fix_v3) | 동일 700회, 동일 seed·기준 K1–K3 (SIMULATED) | **PASS** (K1 PASS, K2 PASS, K3 PASS) | K1 100/100 %, K2 LOST 100 %, K3 0/350. 같은 행 민감도 0.5/1.0/1.5 s: 같은 id 100 %, 전환 0. 제어 경로(실제 `YawController`, 가상 텔레메트리, 소켓 없음): B를 향한 yaw ≠ 0 0/0, A가 숨은 동안 yaw ≠ 0 0/0·LOCKED 0/0, LOCKED 아닐 때 yaw ≠ 0 0(수정 전후 동일); AMBIGUOUS 프레임 3,548/58,044(중앙) · 3,550/29,022(오프센터). 중간 버전 fix_v2(픽셀 게이트 없음)는 K1 1.5 s 44 %로 FAIL — `docs/FAILURES.md` F-05 |
| 수정 후 추적기, 실제 클립 재추적 (같은 검출 재사용) | TUM walking / `vtest.avi` / 몬트리올 거리 (REAL_MODEL / PUBLIC_DATASET) | PASS (실행; 정확도 지표 아님) | id 33→34 / 39→56 / 438→680, 프레임당 LOCKED 1.01→0.84 / 6.32→4.74 / 7.33→4.41, AMBIGUOUS 트랙이 있는 프레임 50/287 · 369/600 · 987/1,390, vtest 확정 지속 p50 13.0→3.65 s. 혼잡할수록 STOP이 늘고 트랙이 짧아지는 의도된 대가 |
| E4 옷 색으로 두 사람을 구분할 수 있는가 (얼굴·생체 식별은 처음부터 제외; 머리 영역 미사용, 메모리 전용 HSV 히스토그램, Hellinger 거리) | fix_v3 재추적 3개 클립의 LOCKED 트랙 의사 라벨 (REAL_MODEL / PUBLIC_DATASET, 정답 신원 없음) | 실행 PASS, **판정 NO_GO** (vtest FAIL, 거리 FAIL, TUM BLOCKED) | 사전 기준 TPR ≥ 0.60 @ FPR ≤ 1 % 를 2개 클립 이상. vtest AUC 0.870, TPR@1 % 0.154(871/2,484쌍); 몬트리올 거리 AUC 0.874, TPR@1 % 0.493(5,083/5,756쌍); TUM AUC 0.963이나 음성 쌍 55개(< 200)로 BLOCKED. 1 % 오매칭에서 진짜 매칭의 15–49 %만 회수 → 추적기에 통합하지 않음, AMBIGUOUS → STOP 유지 — `outputs/evidence/e4_appearance/` |
| E3 가상 경계에서 이동을 멈추는가 | MockTello 운동학 토이, 10 × 10 m, 여유 1 m (SIMULATED_FRAME_ONLY) | PASS (추적기 수정 후 재실행도 PASS) | 사전 6개 체크 모두 참. 보호 시 최소 거리 경계 안쪽 1.175 m(여유 구간 미진입), 비보호 대조군은 0.485 m 침범, 포즈 끊김 3–4 s 동안 `NO_POSE_FOR_GEOFENCE` 정지 10회·이동 0, 비루프백 패킷 0. 재실행 `outputs/evidence/e3_geofence_after_tracker_fix/`: 6개 체크·거리 수치 동일, 10 Hz 루프 지터로 차단 프레임 수만 1프레임 차이(42→43) |
| 자동 테스트 | `scripts/test_all.sh` | PASS | **124 passed in 84.81 s** (`outputs/evidence/tests/pytest-20261109-043008.log`). 신규: `test_live_run.py` 11(버퍼 2, 안전 1, 시계 2, seek 1, 완료 검증 1, SIGKILL/SIGTERM 1, 무장치 1, 실제 모델 end-to-end + 재생 1, 느린 파이프라인 드롭 1), `test_server.py` +1(라이브 실행 수명주기·제어·무결성). 이전 단계: 112 passed(040916), 97 passed(024540), 94 passed(023923). `tests/test_tracker_ambiguity.py` 11개는 수정 전 추적기에서 8개 FAIL 후 수정으로 PASS(구현자 기록, 실패 로그 미보관) |
| UI 테스트 (브라우저 연결 모드) | `node tests/ui/ui_smoke.mjs --attach http://127.0.0.1:9222` | PASS | **73/73 checks**, 콘솔 오류 0 (`outputs/evidence/ui/ui_smoke.json`, 2026-11-09 04:29 KST, 사용자가 `--remote-debugging-port=9222`로 띄운 Dia 창에 탭 하나를 열어 실행하고 그 탭만 닫음). 신규 11개(Live 탭): 라이브 작업공간 표시, 처리 프레임 `LIVE`, 상태 줄 `3.0 fps · 16 ms · 0 of 6`, 지도 위치 NOT_AVAILABLE 표시, pause → `PAUSED`, resume → `LIVE`, LOCKED 박스 클릭 → `SELECT #1`, stop → "19 frames processed, 0 dropped (STOP_REQUESTED). Files verified", 끝난 실행이 재생 목록에 등재, `OPEN_LOOP_REPLAY`로 재생, 연결 끊김 시 Live start 비활성. 이전 62개도 유지. 브라우저에서 아직 확인하지 않은 Live 상태: `STALE_DATA`, `STALLED`, `FAILED`, `INCOMPLETE`, `CANCELLED`(NOT_RUN; 라벨은 `live.js`에 존재) |
| 실기체 비행 | — | BLOCKED | 정책상 금지, 기체 없음 |

## 알려진 한계 / 다음 단계

1. **본인 영상 게이트 NOT_RUN.** `assets/input/`에 영상 한 개를 두고 빠른 시작 3)·5)를 실행하면 `data_origin=REAL_VIDEO`로 기록됩니다. 이것이 가장 먼저 닫아야 할 게이트입니다.
2. **MOTA/IDF1 미정의.** 로컬 데이터셋에는 정답 사람 박스가 없습니다(TUM `groundtruth.txt`는 카메라 포즈만). 검출·추적 성공률을 주장하지 않습니다. 정답 박스가 있는 시퀀스가 생기면 `tracking_report.json`의 `accuracy` 블록이 채워집니다.
3. **E2 수정의 대가.** `TARGET_AMBIGUOUS` STOP과 만료 우선 규칙으로 대상 id가 다른 사람의 검출을 물려받거나 2 s를 넘겨 살아남는 일은 없어졌지만(E2 PASS), 교차·근접 후 **같은 id 재획득은 보장되지 않습니다**(모션 전용). 혼잡한 클립에서는 STOP 시간이 늘고 트랙이 짧아집니다(위 표). 외모 Re-ID는 설계상 도입하지 않습니다. 실험 E4에서 얼굴·생체 식별 없이 옷 색만으로 AMBIGUOUS를 풀 수 있는지 측정했으나 1 % 오매칭률에서 진짜 매칭 회수율이 15–49 %에 그쳐 NO_GO로 닫았고(위 표), 학습된 재식별 임베딩은 라이선스·정책 결정 전까지 NOT_RUN입니다. 수정 후 회귀(94 tests, UI 51/51, E3 재실행)는 모두 PASS입니다.
4. **지오펜스는 포즈 소스가 필요합니다.** Tello EDU는 절대 위치를 주지 않습니다. E3의 가드는 시뮬레이터의 포즈를 읽었고, 0.4 s 지연 모델은 토이입니다. 실제 기체에서는 미션 패드 등 별도 포즈 소스와 측량된 지도 앵커 없이는 쓸 수 없습니다.
5. **F-01 원인 미확인.** FULL_OPENCV 5계수(k3 = 1.1633) 보정으로 초기화가 실패하는 이유는 가설 단계입니다. 다음 테스트는 5계수로 사전 왜곡 보정한 프레임을 PINHOLE로 빌드하는 것입니다.
6. **미구현 (NOT_RUN / NOT_IMPLEMENTED):** 물리 웹캠으로의 라이브 실행(이 Mac에 카메라가 없어 NOT_RUN; `webcam:0`은 `FAILED NO_DEVICE`로 올바르게 끝남), 저장된 지도에 대한 재측위·지도 위치(라이브 실행 헤더와 VO 출력 모두 `relocalization=NOT_IMPLEMENTED`, `map_position=NOT_AVAILABLE`), 라이브 실행 안의 폐루프 제어(라이브는 항상 개루프, 폐루프 시뮬레이션은 MockTello 시나리오로 분리), 상대 깊이 추정 모델(계획 D5), 녹화 UI, AprilTag/체커보드 기반 미터 앵커링, 지도 뷰의 STALE_DATA 배너와 Live 탭의 STALE_DATA/STALLED/FAILED/INCOMPLETE/CANCELLED 브라우저 검증, `yolox_tiny` 비교 실행. 미터 앵커링 전까지 어떤 지도에도 미터 거리를 붙이지 않습니다.
12. **라이브 실행은 측정이지 추격 성능 보장이 아닙니다.** 위트레흐트 30 s 실행에서 대상이 LOCKED인 프레임은 17 %(155/899)였고 LOST 뒤 재선택이 6회 필요했습니다. 이는 사람이 화면을 크게 차지하며 카메라가 따라가는 영상에서 모션 전용 추적기가 보이는 한계이며(지도 쪽 F-11과 같은 클립), LOCKED이 아닌 동안 yaw는 항상 0이었습니다. VO 경로는 세그먼트별 상대 스케일이고 초점거리를 추정했으므로 지도 위치로 쓰지 않습니다. select는 현재 프레임의 확정 트랙 id만 받아들이고(아니면 `SELECT_REJECTED`), `Stop`은 `SUCCEEDED (STOP_REQUESTED)`, SIGTERM/취소는 `CANCELLED`입니다.
7. **단일 환경.** 모든 수치는 Apple M6 / 16 GiB / macOS 27.2 한 대에서 측정했습니다.
8. **품질 판정은 지도가 "맞다"는 뜻이 아닙니다.** GOOD은 등록률·커버리지·재투영·모델 수·점프 수의 고정 임계값을 통과했다는 뜻이며, 정답 궤적과 비교한 지도는 TUM fr1/desk뿐입니다(증분 2.05 cm, 전역 1.66 cm ATE RMSE, 스케일 차용). 스미다·FSC·위트레흐트 지도에는 정답이 없어 절대 정확도를 주장하지 않습니다. FSC 증분 지도의 후반부 드리프트는 전역 매퍼로 사라졌지만 원인은 확인되지 않았고(F-08), 위트레흐트는 두 매퍼 모두 PARTIAL로 남아 있습니다(F-11).
9. **편집된 영상은 지도가 되지 않습니다.** 방콕 60 s 구간(컷 18개)은 컷을 넘는 매칭을 막은 뒤 커버리지 0.068–0.085의 POOR로 남았습니다. 한 테이크 영상(스미다·FSC)만 GOOD입니다.
10. **밀집 점군은 "검증된 상대 스케일 점"이지 표면이 아닙니다.** 평면 스윕 결과 중 희소점 일치와 다중 뷰 일관성을 통과한 점만 남기며, 카메라 바로 앞 보도처럼 반복 질감이 가파른 각도로 보이는 영역은 비어 있습니다. Poisson 메시는 시각 검사 FAIL로 콘솔에 제공하지 않습니다(F-09). 합성 방에서는 밀집점의 97.8 %가 실제 표면 0.05 룸 단위 이내였지만 고정된 수용 정의로는 FAIL(3/8 뷰)입니다.
11. **VO는 보정된 카메라가 필요합니다.** 초점거리를 0.9 × 폭으로 추정한 스미다 VO는 SfM 궤적 대비 23.3 %로 기준(15 %)에 미달했고, SfM의 초점거리를 빌리면 2.56 %지만 독립 증거가 아닙니다(F-10).

## 데이터 · 모델 출처

저장소에는 출처 기록(`SOURCE.md`)만 포함되며 데이터와 가중치는 각자 내려받습니다. 모두 로컬에서만 처리했고 업로드하지 않았으며, 등장 인물에 대한 식별이나 재식별을 하지 않습니다.

| 자원 | 라이선스 | 비고 |
|---|---|---|
| TUM RGB-D `fr1/desk`, `fr3/walking_xyz` | CC BY 4.0 — Sturm et al., "A Benchmark for the Evaluation of RGB-D SLAM Systems", IROS 2012 | `data/public/tum_fr1_desk/SOURCE.md`, `data/public/tum_fr3_walking_xyz/SOURCE.md`. fr3 영상의 인물은 데이터셋 저자 |
| YOLOX-nano / -tiny ONNX (Megvii-BaseDetection/YOLOX 0.1.1rc0) | Apache-2.0 | `data/models/yolox/SOURCE.md`. `scripts/fetch_models.sh`가 SHA-256을 검증하고 `YoloxDetector`가 로드 때마다 재검증 |
| OpenCV 샘플 `vtest.avi` | OpenCV 저장소(Apache-2.0) 배포, 원 촬영 출처 미기재 | `data/public/opencv_vtest/SOURCE.md`. 개발용 보조 클립 |
| "Free City Street Footage - Royalty Free Stock Footage - People Walking Stock Footage No Copyright" by Montreal Walking Tours | CC BY (YouTube 메타데이터, 2026-11-09 확인) | https://www.youtube.com/watch?v=bwJ-TNu0hGM — `data/public/yt_montreal_street/SOURCE.md` |
| "Suan Luang Rama IX Park Bangkok \| 4K Walking Tour, Lake, Gardens & Pavilion" by Streets & Spaces \| Travel Walks | CC BY (YouTube 메타데이터, 2026-11-09 확인) | https://www.youtube.com/watch?v=kjciube52gA, 0:30–1:30 구간 — `data/public/yt_bangkok_park/SOURCE.md` |

Tello EDU 연동 근거: Ryze Tello SDK 2.0 사용자 가이드(UDP 8889 명령, 8890 상태, 11111 영상). 실기체 접속은 이 저장소 범위 밖이며 Mock으로만 검증했습니다(`docs/TELLO_SDK.md`).

## 문서

- `docs/PROGRESS.md` — 마일스톤별 상태표(PASS/FAIL/BLOCKED/NOT_RUN)와 다음 명령
- `docs/VALIDATION_LOG.md` — 실행 날짜, 환경, 명령, 입력 해시, 산출물 경로, 한계
- `docs/FAILURES.md` — 관찰된 실패, 시도한 대안, 다음 테스트
- `docs/ARCHITECTURE.md`, `docs/VISUAL_MAPPING.md`, `docs/CAMERA_ONLY_EXECUTION_PLAN.md` — 설계와 지도 정확도 계층
- `docs/IMPLEMENTATION_PLAN.md`, `docs/PLAN_M4_DETECTOR_TRACKING_PANEL.md`, `docs/PLAN_E2_TRACKER_FIX.md` — 마일스톤 계획, 종료 테스트, E2 추적기 수정 계획과 구현 결과
- `docs/PLAN_MAPPING_QUALITY.md`, `docs/PLAN_GEOMETRY_AND_DENSE.md` — 지도 품질 판정·샷 처리 계획, 기하 진단·매퍼 비교·CPU 밀집 경로 계획, 각각 끝에 "Result" 절(기준별 PASS/FAIL/BLOCKED/NOT_RUN)
- `docs/PLAN_LIVE_RUN_TIMELINE.md` — 한 시간축 라이브 실행(파일 원속도/웹캠, 검출+추적+VO, control.json, 타임라인 재생) 계획과 끝의 "Result" 절(T1–T8 판정, 구현 편차)
- `docs/VERIFICATION_CONTRACT.md`, `docs/RECOVERY_MATRIX.md`, `docs/PHASES_AND_ACCEPTANCE.md` — 검증 기준, 복구 매트릭스, 품질 게이트
- `docs/TELLO_SDK.md`, `docs/ENV_AUDIT.md`, `docs/DESIGN_ASSETS.md`, `docs/SOURCES_AND_ASSUMPTIONS.md`

## 라이선스

이 저장소의 코드 라이선스는 아직 정해지지 않았습니다(`LICENSE` 파일 없음). 외부 데이터셋·가중치·영상은 위 표의 각 라이선스를 따릅니다.
