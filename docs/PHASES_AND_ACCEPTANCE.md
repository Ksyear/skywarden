# 구현 로드맵 및 품질 게이트

모든 단계에서 코드+실행방법+검증 로그를 남긴다. `PASS`, `FAIL`, `NOT_RUN`, `BLOCKED` 혼용 금지. 실제 테스트 결과만 기입.

| 단계 | 산출물 | 합격 조건 |
|---|---|---|
| 0 인벤토리 | ENV_AUDIT.md / dependency table | 기존 Mac·Claude 환경, 불필요한 설치 여부 확인 |
| 1 합성 데모 | camera/motion synthetic fixtures; mock adapter | 패키지 없이도 시뮬레이터 데모 시작; 시드 고정 |
| 2 검출 | 사람 detector, 비디오 입력, 영상 overlay | synthetic 검출 E2E; 실제 모델 1개는 라이선스 확인 후 동작 측정 |
| 3 추적 | CV Kalman 구현, gating, FSM | 1.5s/2s/3s 가림, ID switch, LOST, 잘못된 타깃 회복 실험 |
| 4 제어 | PID 및 rate limiter, windup guard, failsafe | 시뮬레이션 10Hz, 0 명령, 지연·드롭·STOP 검증 |
| 5 지도 기초 | 카메라 보정, visual odometry, sparse map UI | 자료 기반 VO/3D 점 시각화; 미터/상대 스케일 분리 |
| 6 지도 심화 | COLMAP SfM 파이프라인, 영상 프레임 추출, PLY | 공개/직접 영상으로 sparse reconstruction 결과 또는 실패 증거 |
| 7 좌표 보정 | AprilTag/ArUco 보정 옵션, MissionPadPose 인터페이스 | 기준점 누락 시 metric_pose_valid=false, 미터 환산 검증 |
| 8 로컬 앱 | 이벤트, 저장, 경보, 대시보드 | 데이터 보존/삭제, 인터넷 없이 저장·추적 기능 재현 |
| 9 Tello 사전 개발 | UDP protocol fixtures, MockTello, adapters | 8889/8890/11111 설계, `command`, `streamon`, `rc`, `mon` 등 미연결 테스트 |
| 10 모바일(선택) | Android 폰 로컬 추론 데모 | 개발 PC 데모 먼저 완료, Android 앱 미검증은 빌드 검증과 분리 |

## 테스트 매트릭스
- 알고리즘: 지터, 가속도, crossing two persons, 0.5~3s occlusion, detection dropout, frame reorder, variable fps, invalid bbox.
- 지도: pure rotation / no translation, motion blur, low texture, frame skip, wrong camera intrinsics, moving objects, loop and relocalization, scale unknown, failed reconstruction.
- 제어: sign/coordinate transforms, PID anti-windup, rate, stale telemetry, wifi loss, low battery, no pad.
- 동시 실행: 사람 ROI masking이 SLAM 특징점 제거에 미치는 영향, YOLO+VO+UI 프레임 지연, 메모리.
- 증거: FPS, latency p50/p95, tracking MOTA/IDF1은 ground truth가 있으면 계산; 없으면 정의되지 않음 표시. drift는 미터 기준 데이터 있을 때만 측정.
- `make test` 또는 `scripts/test_all.sh`와 `scripts/run_demo.sh`의 사용법 기재.
- 진행 중 예산 집행 없음. 기존 코드와 드론에 명령 전송 없음.
