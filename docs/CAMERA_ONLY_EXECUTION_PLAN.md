> **초기 설계(2026-11-08); 현재 상태는 [docs/SYSTEM_STATUS.md](SYSTEM_STATUS.md)**

# 카메라만으로 지도 만드는 실행 설계 — v2

## 영상만으로 가능한 지도 4종
1. **단안 VO**: 카메라 상대 위치 경로. 미터 스케일이 자동 결정되지 않는다. 회전만 하면 평행이동/삼각측량이 관측되지 않는다.
2. **실시간 단안 V-SLAM**: 카메라 궤적+3D 희소점+루프 클로저/재위치추정. stella_vslam / ORB-SLAM3, Mac arm64 빌드 자체는 실행 검증 필요.
3. **오프라인 전체 지도**: 충분히 겹치는 비디오 프레임을 COLMAP SfM으로 등록. 저장된 전체 희소점 클라우드를 다시 로드하면 첫 화면에서 전체 지도가 이미 보일 수 있다. 미관측 영역은 없음.
4. **깊이 기반 표시**: Depth Anything V2 (상대 깊이)로 고밀도 깊이 영상/개별 프레임 3D 프리뷰. 같은 공간의 일관된 미터 지도는 별도의 카메라 포즈 정합/scale anchor 검증이 필요하다.

## 권장 구현 순서
- Mac에서 **COLMAP sparse** 먼저; `brew install colmap`은 공식 지원 경로지만 시스템 변경 전에 승인 여부 확인. 공식 CLI/설치 버전을 직접 검증.
- 30~90초 짧은 실내 스마트폰 이동 영상 확보: 정적인 배경, 높은 중첩, 충분한 시차/특징점, 모션블러 최소화, 너무 큰 영상부터 시작하지 않음.
- 프레임 추출, blur/overlap 필터, 3D sparse SfM, 카메라 궤적/PLY/JSON 내보내기, 지도 뷰어 구현.
- stella_vslam / ORB-SLAM3 또는 OpenCV VO 실시간 처리 별도로 진행.
- Depth Anything V2 Small macOS Core ML/ MPS 선택 레이어로 연결, 단일 프레임의 RGB+Depth 및 백프로젝션 우선.
- M4 Pro에는 NVIDIA CUDA가 없으므로 **COLMAP PatchMatch dense 기능은 필수 요구사항에서 제외**. 자세한 설명: https://github.com/colmap/colmap/issues/2390 .
- 보정된 AprilTag/ArUco 또는 Tello Mission Pad를 기준으로 실측 좌표/스케일을 정합. 안 되면 RELATIVE 표시.

## 지도 품질과 UI 표시 계약
- `map_source`: `STORED_RECONSTRUCTION` / `CURRENT_SESSION` / `SIMULATED`.
- `geometry`: `SPARSE_SFM` / `VO` / `SLAM` / `SINGLE_FRAME_DEPTH` / `FUSED_UNVERIFIED`.
- `scale`: `RELATIVE` / `METRIC_ANCHORED`.
- `tracking`: `INITIALIZING` / `TRACKING` / `RELOCALIZING` / `LOST`.
- 사용자에게 '실측 안전 지도'라는 인상을 주지 않도록 `NOT_NAVIGATION_SAFE` 상태를 명시.
- 정적 환경 기반 map에서는 YOLO가 검출한 사람 ROI와 움직이는 물체 mask 사용. 마스킹이 특징점 수를 과하게 줄이면 가중치 조절하고 결과 비교.

## 근거
- Ryze Tello EDU SDK 2.0 AI 추적과 3D 재구성 가능: https://www.ryzerobotics.com/kr/mobile/tello-edu
- COLMAP tutorial (video SfM 및 dense 단계): https://colmap.github.io/tutorial.html
- COLMAP macOS 설치: https://colmap.github.io/install.html
- COLMAP dense CUDA 한계: https://github.com/colmap/colmap/issues/2390
- stella_vslam: https://github.com/stella-cv/stella_vslam
- stella_vslam macOS docs (이전 macOS 테스트, 최신 M4 검증 아님): https://stella-cv.readthedocs.io/en/latest/installation.html
- ORB-SLAM3: https://github.com/UZ-SLAMLab/ORB_SLAM3
- Depth Anything V2 Small: https://github.com/DepthAnything/Depth-Anything-V2 , https://developer.apple.com/machine-learning/models/
