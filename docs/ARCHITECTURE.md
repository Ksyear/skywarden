> **초기 설계(2026-11-08); 현재 상태는 [docs/SYSTEM_STATUS.md](SYSTEM_STATUS.md)**

# 시스템 전체 설계 — PC 우선, 나중에 Tello EDU

## 요구사항 우선순위
P0: Mac으로 영상→사람검출→칼만→PID→Mock 드론→영상/로그/UI까지 한 번에 재현.
P1: 카메라만으로 VO/SLAM 희소 3D 맵 + COLMAP 오프라인 복원 + 지도/카메라 동시 UI.
P2: Tello EDU SDK UDP Mock 검증, 패드 기준계, 실기체 연결 준비.
P3: Android/스마트폰 클라이언트 포팅(선택; P0~P2를 희생하지 말 것).

## 모듈 구분
```
VideoSource(Mock | MP4 | Camera | TelloUDP)
       ├─> PersonDetector(Fake | OpenVINO/ONNX/ML backend) -> DataAssociation -> KalmanFilter -> TrackerFSM
       │                                                          │
       │                                                          v
       │                                                  Policy/Safety -> PID -> DroneAdapter(Mock | TelloDisabled)
       └─> Calibration -> VisualOdometry/SLAM -> MapStore(Sparse3D, 2D, confidence)
                                                     │
TelloState(UDP8890) -> MissionPadAnchoring -> CoordinateTransform -> LocalPoseEstimate
                                                     │
Alarm(PIR Mock or ESP32 MQTT) -> EventStore -> Local recorder
                                                     │
                        Dashboard(Camera + Sparse3D/2D Map + Timeline + E-STOP controls)
```

## 데이터 계약 — JSON/Python dataclass
- `Frame`: frame_id, source, timestamp_ns_monotonic, bgr_frame, width/height, camera_id, calibration_id.
- `Detection`: track_hint? class_id, score, bbox_xyxy_px, timestamp_ns.
- `Track`: ID, center_px, velocity_px_s, covariance, state[TENTATIVE,LOCKED,PREDICTING,AMBIGUOUS,LOST], age_ms. (설계 초안의 SEARCHING/COASTING은 구현에서 TENTATIVE/PREDICTING으로 명명.)

### 데이터 연관 규칙 (구현 기준, `packages/tracking/tracker.py`, 2026-11-09 E2 수정 반영)
1. 예측 후 **만료 먼저**: 마지막 *신뢰* 검출로부터 `t − last_hit_t > max_coast_s`(2.0 s)인 트랙은 어떤 검출도 보기 전에 제거. 2.000 s는 생존, 그 초과는 만료, 프레임 간격이 불규칙해도 동일. 만료된 id는 재사용·부활하지 않음.
2. 게이트 = Mahalanobis χ² ≤ 9.21 **그리고** 픽셀 거리 ≤ 0.5 × 박스 높이. 픽셀 상한이 없으면 1.5 s coasting 트랙의 게이트가 약 500 px로 커져 무관한 사람을 끌어들임(E2 fix_v2에서 관측).
3. 확정 트랙(LOCKED/PREDICTING/AMBIGUOUS)은 게이트 그래프의 연결 성분 단위로 연관. 트랙 1 + 검출 1인 성분만 신뢰(Kalman 보정, hits, `last_hit_t` 갱신). 트랙 또는 검출이 둘 이상인 성분은 **AMBIGUOUS**: 보정·갱신 없음, 그 검출들은 이번 프레임에 보류(새 트랙도 만들지 않음). 순수 Hungarian 1:1 배정은 더 이상 확정 트랙에 쓰지 않음.
4. 잠정(TENTATIVE) 트랙은 확정 트랙이 가져가지 않은 검출에 대해 게이트된 Hungarian으로 배정, 남은 검출은 새 TENTATIVE 트랙.
5. `TargetSelector`는 선택된 id가 사라지면 운영자가 다시 `select()`할 때까지 LOST를 반환하고, 같은 자리의 새 id를 자동으로 고르지 않음. `YawController`는 LOCKED 외 모든 상태에서 STOP이며 사유는 `TARGET_<state>`(`TARGET_AMBIGUOUS`, `TARGET_LOST`, `TARGET_PREDICTING`).
한계: 모션만으로는 게이트가 겹친 두 사람을 구분할 수 없다. 이 규칙은 잘못된 LOCK을 STOP으로 바꾸는 것이지, 교차 후 같은 id 재획득을 보장하지 않는다.
- `Pose`: xyz, R/quaternion, timestamp_ns, frame_id, scale_status[UNKNOWN,RELATIVE,METRIC_ANCHORED], quality, covariance_or_null, origin.
- `SparseMap`: xyz_points, feature_ids, source_frame_ids, trajectory, point units, calibration provenance.
- `MissionPadPose`: pad_id, pad_center_and_axes, xyz_cm, detected_age_ms, validity, map transform if known.
- `DroneCommand`: yaw_rc, lateral_rc, forward_rc, vertical_rc, timestamp, source, armed bool, safety flags.
- `AlarmEvent`: local timestamp, type, source, confidence optional, retention.
모듈간 단위 명시: px/s, m, cm, deg, rad, ns. 좌표축 convention과 transformation graph 문서화, PID 와 Tello RC yaw sign 실제 검증 전 표시.

## 지도 정확도 계층
1. `SIMULATED`: 세계 좌표가 정답으로 주어진 가상 지도(실제 성과 아님).
2. `VO_RELATIVE`: 단안 카메라 상대궤적/희소포인트, 스케일 모름.
3. `RECONSTRUCTED_RELATIVE`: COLMAP 시각화 결과, 스케일 모름.
4. `METRIC_ANCHORED`: 실제 크기/패드 좌표계와 정렬, 관측 유효 조건에서만.
5. `NAVIGATION_SAFE`: 본 프로젝트에서 기본적으로 불허; 검증된 장애물 감지·거리·실제 경로 안정성 필요.
**YOLO Bounding Box 크기만으로 사람과의 절대 거리 추정 금지.**

## 플랫폼
- Mac M4 Pro Python 3.11+ (최소 버전 설치 결과에 따라 재조정), `uv` 또는 venv, `numpy`, `opencv-python`, 선택적 `onnxruntime` 등.
- 지도: OpenCV VO 먼저, ORB-SLAM3 별도 선택 브랜치/서브프로세스(빌드 여부 기록), COLMAP 별도 CLI 선택.
- UI: Streamlit/FastAPI+웹 프런트 또는 PySide6 중 환경/재현성에 맞게 결정. 로컬포트만 bind `127.0.0.1` 기본.
- 추후 Tello EDU: Python DJITelloPy 또는 직접 UDP SDK 2.0; **DJI MSDK V5 사용 금지**.
- ESP32-PIR optional; 이전 `/Volumes/HIBACHI/Share/university/dev/drone` 기존 프로젝트 변경 금지.

## 안전 아키텍처
- `MockDroneAdapter`만 기본 활성화. `HardwareDroneAdapter`는 별도 opt-in/실기체 사용자 승인/프로펠러 가드/통제된 환경 전제.
- UI 기본은 기록/분석/수동 카메라 추적. 실기체 사람 방향 자동 전진, 자동 이륙, 가시거리 밖 자율순찰 금지.
- Tello GPS RTH를 기대하지 않음. `return`은 시뮬레이터 이벤트만; 실기체에서 임의 좌표 자동 복귀 금지.
- STOP은 명령 0/hover/안전 착륙 계획; `emergency`는 모터 컷/추락 위험으로 일반 STOP과 분리.
- 신뢰도 부족(영상 누락/SLAM LOST/패드 분실/UDP 지연/저전압) 시 적극적인 이동 명령 차단.
- 실기체 인터페이스 테스트는 우선 전원+영상/상태만(이륙/모터 작동 없음).
