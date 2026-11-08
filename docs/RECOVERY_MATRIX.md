# 구현 복구 매트릭스: 불가능 선언 대신 증거+대안 실행

| 장애 | 확인해야 할 증거 | 대안 A | 대안 B | 합격 수준 |
|---|---|---|---|---|
| ORB-SLAM3/Pangolin Mac arm64 빌드 실패 | CMake 로그, 소스 commit, compiler | stella_vslam 최소 viewer | OpenCV VO/3D points | VO 가능, SLAM은 NOT_VERIFIED 명시 |
| stella_vslam 종속성 실패 | g2o/eigen/clang 에러 | ORB-SLAM3 최소 예제 | OpenCV VO | 실제 영상 3D 맵은 COLMAP으로 계속 |
| COLMAP 설치 불가 | brew/CLI 오류 및 허용 범위 | 이미 설치된 pycolmap 버전 확인 | OpenCV 상대 pose+triangulation / 요청 승인 기록 | sparse PLY 생성/한계 기록 |
| COLMAP 영상 SfM 등록 실패 | 등록 프레임/인라이어/시차/왜곡 | 프레임 수 줄이기, 조명/시차 개선 | 다른 정적 장면의 허용 영상 | 새 샘플에서 sparse map 재도전 |
| COLMAP dense CUDA 없음 | `colmap` 빌드 플래그/공식 docs | Depth Anything V2 small(Core ML) | PyTorch MPS Depth Anything V2 | 깊이 영상·프리뷰, 미터 보장 X |
| 카메라 캘리브레이션 없음 | K 없음/왜곡 추정 실패 | 체커보드 보정 | COLMAP 카메라 자동 추정 | 결과를 UNCALIBRATED 상태로 표시 |
| 영상 데이터 없음 | asset 폴더 내용 | 사용자가 직접 촬영할 방법 문서 | 라이선스가 명확한 작은 실제 SfM 공개 데이터 사용 | 실제 영상 없는 통과 주장 금지 |
| YOLO 설치 실패 | Python/onnxruntime log | CPU ONNX 지원 모델 | OpenCV HOG 사람 검출(성능 한계 표기) | 실제 탐지는 실제 모델로 검증 |
| Mac UI 프레임 속도 부족 | p50/p95, CPU, 메모리 | 프레임 해상도/샘플링 감소 | 지도 생성 offline / tracking online 분리 | UI 응답/측정 로그 기록 |
| Tello 없음 | 물리 장치 부재 | MockUdpServer 및 영상 파일 | 드론 simulator | 실제 비행 PASS 금지 |

**규칙:** 오류를 숨기지 말고, 반드시 첫 번째로 실제 명령/로그를 기록. 문제가 난 부분 외에는 계속 작성. 실제 작동하지 않는 기능을 성공으로 레이블링하지 말 것.
