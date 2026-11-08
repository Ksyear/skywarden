# Security Drone A v3 — **카메라만으로 실제 3D 지도 생성** + Kalman 사람 추적 + Tello EDU 준비

> 2026-11-08. **실행용 작업 지시서/설계서**에서 출발한 프로젝트입니다. 2026-11-08 기준, 실제 카메라 데이터(공개 데이터셋 TUM RGB-D fr1/desk, CC BY 4.0)로 만든 sparse 3D 지도가 `outputs/maps/tum_fr1_desk_K4`에 존재합니다(49/53 프레임 등록, 4,297점, 상대 스케일, `NOT_NAVIGATION_SAFE`; 근거 `docs/VALIDATION_LOG.md`). 사용자 본인 영상으로 만든 지도(REAL_VIDEO 게이트)는 아직 NOT_RUN — `assets/input/`이 비어 있습니다.
>
> 2026-11-09 (M4). 실제 사람 검출 모델 **YOLOX-nano ONNX**(Megvii 0.1.1rc0, Apache-2.0, OpenCV DNN 5.0.0 CPU, 추가 pip 패키지 없음)가 연결되었고, 공개 데이터셋 TUM fr3/walking_xyz(287 프레임, 검출 p50/p95 8.94/10.45 ms, 확정 트랙 24개)와 OpenCV `vtest.avi`(600 프레임, 105 fps 처리, 확정 트랙 23개)에서 `REAL_MODEL` 라벨로 기록되었습니다. 웹 콘솔에 Tracking 탭(박스 오버레이, 대상 선택, MOCK yaw 표시)이 추가되었고 헤드리스 UI 테스트 47/47, pytest 75 passed. MOTA/IDF1은 정답 박스가 없어 NOT_RUN(미정의), 본인 영상 추적도 NOT_RUN. 근거: `outputs/evidence/m4/`, `docs/PROGRESS.md`.

## Quick start (implemented, 2026-11-09 기준)

아래 명령은 실제로 구현되어 이 Mac에서 실행·검증된 것만 적었습니다(근거: `docs/VALIDATION_LOG.md`, `outputs/evidence/`). 현재 상태 요약은 `docs/PROGRESS.md`, 실패와 대안은 `docs/FAILURES.md`.

```zsh
cd '/Volumes/HIBACHI/Share/university/dev/security-drone-a'

# 0) 프로젝트 전용 venv (Python 3.13, pycolmap 4.2.1 / OpenCV 5.0.0 포함). 시스템은 바꾸지 않음.
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1) 지도 콘솔 (127.0.0.1:8780, HARDWARE_ARMED=false DRY_RUN=true 강제). 포트 충돌 시 --port <free port>
scripts/run_demo.sh

# 2) 영상/이미지 폴더 -> COLMAP sparse 지도 (PLY + 카메라 궤적 + map_report.json)
#    검증된 예: TUM fr1/desk 공개 데이터셋(CC BY 4.0), 49/53 프레임 등록, 4,297 점, 0.83 px
scripts/build_sparse_map.sh data/public/tum_fr1_desk/rgbd_dataset_freiburg1_desk \
  --session tum_fr1_desk_K4 --origin PUBLIC_DATASET \
  --camera-json data/public/tum_fr1_desk/camera_opencv4.json

# 3) 저장된 지도 재생(콘솔에서도 "Stored maps"로 로드 가능)
scripts/replay_map.sh outputs/maps/tum_fr1_desk_K4

# 4) 자동 테스트 (최근 실행 2026-11-09: 75 passed) / 헤드리스 UI 테스트 (47/47, Dia 브라우저 필요)
scripts/test_all.sh
node tests/ui/ui_smoke.mjs

# 5) SIMULATED 폐루프: 합성 검출기 -> Kalman -> yaw PID -> MockTello(루프백 UDP). 실기체 송신 0회
scripts/run_tracking_sim.sh

# 6) 실제 사람 검출 모델 가중치 (YOLOX-nano ONNX, Apache-2.0, 3.66 MB, SHA-256 검증 후 data/models/yolox/에 저장; git 제외)
scripts/fetch_models.sh yolox_nano

# 7) 녹화 영상/이미지 폴더 -> YOLOX-nano(CPU) -> Kalman 다중 추적 기록 (REAL_MODEL). 드론 명령 생성 없음.
#    검증된 예: TUM fr3/walking_xyz 287 프레임, 검출 8.94/10.45 ms(p50/p95), 확정 트랙 24개
scripts/run_tracking.sh data/public/tum_fr3_walking_xyz/rgbd_dataset_freiburg3_walking_xyz \
  tum_walking_xyz_nano --origin PUBLIC_DATASET --max-frames 900
#    결과 outputs/tracks/tum_walking_xyz_nano/{tracks.json,tracking_report.json,frames/}; 콘솔 Tracking 탭(T 키)에서 재생
```

지도 라벨은 항상 `map_source`(STORED_RECONSTRUCTION / CURRENT_SESSION / SIMULATED), `scale=RELATIVE`, `NOT_NAVIGATION_SAFE`를 표시합니다. 단안 카메라 지도는 미터 거리와 충돌 안전을 보장하지 않습니다. 합성 데이터 결과(SIMULATED)는 실제 데이터 통과로 집계하지 않습니다.

**내 영상으로 지도 만들기 (아직 NOT_RUN — `assets/input/`이 비어 있음)**
1. 촬영 팁: 정적인 실내 장면(움직이는 사람·물체 없이), 카메라를 제자리 회전이 아니라 **옆으로 천천히 이동**하며 촬영, 질감이 많은 벽·가구가 보이게, 밝은 조명, 30–90초, 가능하면 1080p 이하.
2. 파일을 `assets/input/<이름>.mp4`에 둡니다(이 폴더는 git에 올라가지 않음).
3. 실행:
   ```zsh
   scripts/build_sparse_map.sh assets/input/<이름>.mp4 --session my_room_01 --origin REAL_VIDEO
   ```
   기본값은 3 fps 샘플링, 최대 150 프레임, 보정 파일이 없으면 COLMAP이 SIMPLE_RADIAL 내부 파라미터를 추정(`UNCALIBRATED_ESTIMATED`). 결과는 `outputs/maps/my_room_01/`. 실패하면 `map_report.json`의 `failure.message`에 다음 조치가 적힙니다(대표 원인: 제자리 회전, 질감 부족, 블러, 겹침 부족).

**사람 검출·추적 패널 (M4, 2026-11-09, 녹화 영상 전용)**
- 검출기: `packages/vision/person_detector.py`의 `YoloxDetector`. 로드할 때마다 가중치 SHA-256을 고정값과 비교하고(불일치 시 `ModelUnavailable`), `cv2.dnn.readNetFromONNX`로 실행. 입력 416x416 레터박스, person 클래스만, score 0.35 / NMS IoU 0.45. 모든 검출에 `source=REAL_MODEL`이 붙어 합성 검출기(`SYNTHETIC`)와 섞이지 않습니다.
- 추적 작업: `python -m vision.track_video` (`scripts/run_tracking.sh`). 단계 `LOAD_MODEL -> DETECT_TRACK -> EXPORT`, 진행 상황은 지도 빌드와 같은 `status.json`, 중단 시 `CANCELLED`. 추적기 시계는 영상 시간(벽시계 아님). 보고서에 모델 출처·지연 p50/p95·트랙 통계가 기록되고 `MOTA/IDF1`은 정답 박스가 없으면 `null`로 남습니다.
- 가상 yaw 재생: `packages/control/virtual_replay.py`. 운영자가 한 프레임에서 한 id를 고르면 M3의 `TargetSelector`+`YawController`가 기록된 트랙 위에서 yaw 명령을 계산합니다. **개루프**(녹화 카메라는 돌지 않음), 텔레메트리는 가상(배터리 85 %), 라벨 `MOCK`, `transmitted=false`, 소켓을 열지 않음. 대상이 안 보이면 PREDICTING(최대 2 s) 후 LOST, 자동 재획득 없음. LOCKED가 아닐 때 0이 아닌 yaw는 0회(`tests/test_vision.py`).
- 웹 콘솔: `M`/`T` 키 또는 탭으로 Mapping/Tracking 전환(`#tracking` 해시 유지). 프레임 위 캔버스 박스(대상 강조, PREDICTING 점선, TENTATIVE 점박이, 원시 검출 토글), 트랙 목록(`1`–`9` 선택, `0` 해제, 박스 클릭 선택), 대상 상태 배지와 힌트, yaw 바 + STOP 사유, 대상 상태별 색 타임라인과 yaw 궤적. 상태: loading / empty / running / failed / cancelled / disconnected. 지도 빌드와 추적 실행은 **한 번에 하나**만(409).
- 서버 API: `GET /api/models`, `GET /api/tracks`, `GET /api/tracks/<id>`, `GET /api/tracks/<id>/files/*`(허용 목록), `POST /api/track-jobs`, `POST /api/track-jobs/<id>/cancel`, `POST /api/tracks/<id>/control`. 127.0.0.1 전용, `hardware_armed=false` 불변.
- 데이터 출처: `data/models/yolox/SOURCE.md`, `data/public/tum_fr3_walking_xyz/SOURCE.md`(CC BY 4.0, 등장 인물은 데이터셋 저자), `data/public/opencv_vtest/SOURCE.md`(원 촬영 출처 미기재, 개발용 클립). 가중치·데이터셋은 git에 올라가지 않으며 외부 업로드 없음.
- 한계: 정답 박스가 없어 정확도(MOTA/IDF1)는 측정하지 않았고 성공률을 주장하지 않습니다. TUM 실행에서 id가 짧게 끊긴 주원인은 사람이 화면에 없는 구간(49 프레임)·가림·손떨림 카메라와 Re-ID 미탑재(의도적)입니다(`outputs/evidence/m4/tum_missed_frames_montage.jpg`). 박스 크기로 거리를 추정하지 않으며 lateral/forward/vertical 축은 항상 0입니다.

아직 구현되지 않은 것(NOT_RUN): 웹캠 실시간 VO/SLAM, Depth Anything V2, 녹화 UI, AprilTag 미터 보정, 정답 박스 기반 MOTA/IDF1, 본인 영상 추적(`assets/input/` 비어 있음). 계획서의 onnxruntime/HOG 대체 경로는 필요하지 않아 NOT_RUN.

## 가장 먼저 읽을 것
- `docs/CAMERA_ONLY_EXECUTION_PLAN.md` — COLMAP sparse → Visual SLAM → Depth Anything V2 지도 계획
- `docs/RECOVERY_MATRIX.md` — 빌드/데이터 오류별 다음 실행 대안
- `docs/VERIFICATION_CONTRACT.md` — 실제 결과물 확인 기준
- `docs/IMPLEMENTATION_PLAN.md`, `docs/PLAN_M4_DETECTOR_TRACKING_PANEL.md` — 마일스톤별 구현 계획과 종료 테스트
- 기존 `docs/*` — Tello SDK, 시각 지도 설계, 품질 테스트 상세

## 모델 분업 (프로젝트 내부에서만 적용)
- **Opus 5.5** (`claude-opus-5-5`): 모든 코드 작성·디버깅·테스트·지도 생성 프로그램 제작
- **Fable 5.1** (`claude-fable-5-1`): `fable-planner`가 개발 계획·설계, `fable-documenter`가 README·실험/테스트 문서 작성
- Fable 에이전트는 별도 모델로 지정되어 다른 프로젝트·팀 계정에 영향을 주지 않음.
- 메인 Opus에게 계획을 직접 작성하지 말고 **명명된 Fable 서브에이전트를 명시적으로 호출**하도록 지시. `/tasks`에서 Fable 5.1 사용 여부 확인. 계정 권한이 없으면 자동 대체하지 않음.
- Anthropic 공식: https://code.claude.com/docs/en/sub-agents

## 0원 우선 구현 → Tello EDU
1. Mac + 스마트폰 동영상/웹캠으로 지도와 사람 검출 개발.
2. 실제 촬영 영상으로 `COLMAP sparse` 결과 PLY + 카메라 궤적 만들기.
3. Visual SLAM/VO 실시간 트랙과 Depth Anything V2 3D 보조 시각화.
4. Kalman+PID+MockTello 비행 가상 검증.
5. Tello EDU 정품 구입 후 별도 승인하여 SDK 2.0 영상·상태 입력 테스트. 실제 자동 비행은 이후 별도 안전 시험.

## Claude Code 첫 입력
```
docs/CAMERA_ONLY_EXECUTION_PLAN.md, docs/RECOVERY_MATRIX.md, docs/VERIFICATION_CONTRACT.md를 전체 읽고, Fable 5.1 `fable-planner` 서브에이전트에게 계획 작성을 명시적으로 위임한 뒤 Opus 5.5로 코딩·테스트하고, Fable 5.1 `fable-documenter`로 문서를 작성하세요. 위 문서에 따라 Phase 0부터 바로 코드를 구현하세요. 계획만 쓰고 종료하지 마세요. Mac에서 실제 영상 기반 COLMAP 희소 3D 지도, PLY와 카메라 궤적을 먼저 만드는 것을 최우선으로 하세요. 실패하면 오류/재현 명령을 기록하고 대체 기술을 실제 실행하세요. 이어서 Visual SLAM/VO, Depth Anything V2, YOLO+Kalman+PID, MockTello, 자동 테스트를 구현하세요. 드론 구매·비행·기존 팀 계정 및 다른 프로젝트 수정은 금지입니다.
```

## 근거
- Ryze Tello EDU: https://www.ryzerobotics.com/tello-edu/downloads
- COLMAP tutorial: https://colmap.github.io/tutorial.html
- COLMAP Mac: https://colmap.github.io/install.html
- stella_vslam: https://github.com/stella-cv/stella_vslam
- Depth Anything V2 + Core ML: https://developer.apple.com/machine-learning/models/
