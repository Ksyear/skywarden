# LiDAR 없는 카메라 지도 생성: 검증 가능한 설계 (2026-11-08)

## 결론
일반 RGB 카메라 영상으로 지도 생성은 가능하다. 그러나 **지도의 종류와 크기/정확도**에 따라 가능한 것이 다르다.

| 출력 | 방법 | 드론 없이 | Tello EDU | 한계 |
|---|---|---|---|---|
| 카메라 궤적·희소 특징점 3D 지도 | 단안 Visual SLAM / VO | 영상/데이터셋으로 가능 | 전방 카메라 영상 사용 가능 | 단안 절대 스케일 미정·드리프트·실패 가능 |
| 영상에서 사후 3D 복원 | COLMAP SfM → 선택적 MVS | Mac + 겹치는 영상 프레임 | Tello 촬영 영상에 시도 | 특징점·시차·계산시간 필요, 단안 스케일 미정 |
| 실내 2D 바닥 평면도 / 궤적 | 월드 좌표 변환, 평면 가정, 점군 투영 | 가능 | 보정된 위치 추정에서 일부 가능 | 객체 높이/실제 장애물 판별 불확실 |
| 미터 단위 실제 좌표 | AprilTag 등 기준점/실측 길이, Mission Pad | 인쇄 마커 + 측량 | 패드 관측 중 위치 보정 | 유효 관측 조건 안에서만, 바깥은 추정 불확실 |
| 완전한 밀집 3D 장애물 지도 | SfM/MVS/깊이 추정 | 사후 시도 가능 | 실시간 안정성 보장 불가 | 단안 깊이 추정은 안전용 거리계 아님 |

**중요:** 사람 검출 YOLO의 Bounding Box 지도와 Visual SLAM 지도는 별개. 사람이 움직이면 SLAM 특징점이 오염될 수 있으므로 사람/움직이는 물체 마스킹을 선택 적용할 것.

## 기술 비교
- **ORB-SLAM3**: 단안, 스테레오, RGB-D 및 카메라+IMU를 지원하는 공개 연구 구현. 기성 Mac arm64에서 바로 실행된다고 가정하지 말고 빌드 전 의존성/Pangolin/컴파일러 검증. **GPL-3.0 라이선스** 유의. 초기화에 시차가 필요하며 회전만 하는 장면에서는 삼각측량이 어렵다.
  https://github.com/UZ-SLAMLab/ORB_SLAM3
- **COLMAP**: 정지 장면의 여러 시점 영상에서 카메라 위치·희소 3D 구조를 복원하는 SfM/MVS 도구. macOS Homebrew 설치 지원(`brew install colmap`)하지만 설치 전 사용자 승인; 영상 프레임을 일정 간격으로 추출하고 중복/모션블러 제거. https://colmap.github.io/tutorial.html , https://colmap.github.io/installation.html
- **OpenCV VO 프로토타입**: 카메라 내부파라미터 보정 + ORB/KLT 매칭 + 필수행렬(Essential matrix) + RANSAC + recoverPose로 상대 이동 시각화. 이는 **검증용 Visual Odometry(VO)**로서 루프 클로저·전역 맵 최적화·규모 복원까지 되는 완전한 SLAM이라고 표기하지 말 것.
- **AprilTag/ArUco**: 크기와 위치를 측량한 기준 마커로 외부 스케일/자세 보정. Tello EDU Mission Pad는 기체의 전용 SDK 상태 패킷을 사용, 직접 동일 마커라고 가정하지 않는다.

## 권장 파이프라인 (저비용)
1. Mac + 스마트폰 또는 웹캠: 보정용 체커보드 영상 수집 → `camera_calibration.yaml`(fx, fy, cx, cy, distortion, 해상도) 작성. 영상별 동일 카메라/해상도인지 검증.
2. 드론 없는 VO: EuRoC 등 명시적인 공개 데이터셋 또는 자신이 촬영한 이동 영상 사용. 초기화→카메라 궤적 추정→3D 희소점 생성. 각 프레임 상태 `INITIALIZING/TRACKING/LOST/RELOCALIZING` 기록.
3. COLMAP 오프라인 지도: ffmpeg/파이썬으로 영상 프레임 추출 → feature_extractor → sequential_matcher → mapper → 결과 카메라 궤적 및 sparse point cloud 출력(PLY/JSON). MVS는 Mac 메모리·GPU 사양에 따라 선택. 실패 시 로그 보관.
4. 지도 뷰: 좌측 영상(YOLO, 타깃/칼만), 우측 3D 점/가상 궤적, 하단 제어·상태 그래프. 화면에 `SPARSE_MAP`, `RELATIVE_SCALE`, `SIMULATED`, `NOT_NAVIGATION_SAFE` 상태 구분.
5. 바닥 2D 지도/미터 스케일: 기준 물체 실측 길이, 마커 포즈 또는 Mission Pad 관측 + 설치 위치를 통해 변환. 서로 다른 세션의 좌표계 정렬은 변환값/오차와 신뢰도 기록.
6. Tello EDU 도입 후: `streamon` 전방 카메라 → 동일 VO/COLMAP 재사용. 하방 Mission Pad의 `mid, x, y, z` 상태가 **유효하고 현재 관측된 경우에만** 미터 스케일 보조 및 기준점 정렬; 유효 범위 밖이면 covariance/신뢰도 악화 표기.
7. 측정: 특성점 수, inlier ratio, tracking loss, drift(기준점 있는 데이터), 재구성 성공 프레임 수, 처리시간 p50/p95. 실측 없는 정확도 수치를 만들어 내지 않는다.

## 프라이버시/안전
- 다른 사람 얼굴 식별/재식별, 동의 없는 촬영·보안 영상 수집 금지. 사람 추적 시 개인 신원 인식 사용하지 않음.
- 단안 VO/SLAM 또는 생성된 점군만으로 **자동 장애물 회피, 정밀 거리 유지, 완전자율 추격** 수행 금지.
- Mac에서 Tello Wi-Fi로 직접 연결하면 인터넷이 없을 수 있으므로 패키지/모델/클립을 사전 준비하고 런타임은 로컬 실행.
- LiDAR 기반 점유 지도와 이 프로젝트의 카메라 지도는 동등한 출력이 아님. 시각화가 비슷해도 센서/정확도가 다르다.

## 합격 기준
- 최소 하나의 자기 제작 또는 공개 영상에서 카메라 보정 파일을 이용해 VO 궤적을 시각화하고 상태 로그 출력. 실패 시 LOST를 정직하게 처리.
- COLMAP sparse 결과를 생성하거나 불가능한 이유와 명령/실패 로그를 문서화.
- 임의로 3D 포인트를 생성한 데모에는 `SIMULATED`를 명시, 실제 추정 결과와 혼합하지 않음.
- Mission Pad 데이터가 미존재/만료되면 `metric_pose_valid=false`로 처리.
