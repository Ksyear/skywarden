> **초기 설계(2026-11-08); 현재 상태는 [docs/SYSTEM_STATUS.md](SYSTEM_STATUS.md)**

# 최종 결과물과 체크리스트

## Must-pass (구매 전)
- [ ] Mac에서 `scripts/run_demo.sh`가 로컬 영상/맵/추적 UI 실행.
- [ ] `scripts/build_sparse_map.sh`가 **실제** 영상/정지 이미지 시퀀스를 입력받아 pose와 sparse point 3D 맵(PLY) 저장. map_stats.json에 등록 프레임 수·점 수·출처·상대 스케일 표시.
- [ ] 별도 `scripts/replay_map.sh` 또는 UI 버튼으로 이전 저장된 지도를 로드해 전체 지도가 시작 시점에 보이는 기능.
- [ ] 지도는 `STORED_RECONSTRUCTION` / `CURRENT_SESSION` / `SIMULATED` 명확히 표시.
- [ ] 실제 검출 모델/가상 검출 모델 선택 가능, 시뮬레이션의 PASS와 실제 검출 PASS 구분.
- [ ] Kalman 2초 가림 예측·상태·이탈/복귀 및 PID STOP 테스트.
- [ ] `HARDWARE_ARMED=false`에서 실기체 비행 제어 송신 0회 보장 테스트.
- [ ] Tello EDU MockUDP state/video/ack/timeout/parser 테스트.
- [ ] `docs/VALIDATION_LOG.md`에 실제 명령/출력/도구 버전/재현 경로 기록.

## 품질 지표
- camera input FPS/p50/p95, map frames/registered frames/3D points, visual trajectory display, tracking state timeline, frame latency, scale, hardware gates. 목표값은 baseline 측정 후 정하기. `points3D > 0`만으로 유효한 지도 품질이 보장되는 것은 아님.
- 실제 영상 결과물 크기/지도 스케일/좌표계 단위 정확성 평가. AprilTag 기준 측정이 가능할 때만 metric ATE/RMSE 계산.
- LiDAR 기반 충돌 안전 지도와 동등하다고 주장하지 않음.

## 최종 상태 보고 예시
- PASS: 로컬 macOS에서 `data/input/room.mp4` → `outputs/maps/session_01/sparse.ply`, registered n/N, 3D points p, map viewer 동작 (실제 로그가 있어야 함)
- PASS: synthetic Kalman+PID 12/12 시나리오, mock Tello 안전 게이트 (실제 로그가 있어야 함)
- BLOCKED: 실제 Tello 비행(기체 미보유), Dense MVS CUDA(별도 Depth Anything 실험으로 대체)
- NOT_RUN: 사용자 승인 필요한 시스템 설치
