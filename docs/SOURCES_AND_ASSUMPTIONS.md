# 근거/가정 구분 — 2026-11-08 재검토

| 항목 | 원문 | 확인 사항 |
|---|---|---|
| Claude config 격리 | https://code.claude.com/docs/en/env-vars | `CLAUDE_CONFIG_DIR`는 공식 지원. 프로젝트 settings 파일에서는 해당 변수 설정 불가 |
| Claude login | https://code.claude.com/docs/en/authentication | `claude auth login` 사용, 사용자가 브라우저에서 로그인 |
| Tello EDU 제품 | https://www.ryzerobotics.com/kr/mobile/tello-edu | 카메라 스트림을 이용한 인식/3D 재구성 개발 가능성, SDK 2.0/패드 |
| SDK 2.0 매뉴얼 | https://dl-cdn.ryzerobotics.com/downloads/Tello/Tello%20SDK%202.0%20User%20Guide.pdf | 8889/8890/11111, streamon, rc, pad 명령. PDF p2~p6 |
| Mission Pad | https://dl-cdn.ryzerobotics.com/downloads/Tello/Tello%20Mission%20Pad%20User%20Guide.pdf | 패드 ID/좌표계, 실제 검출 필요. PDF p2~p4 |
| DJITelloPy | https://github.com/damiafuentes/djitellopy | Tello/Tello EDU 제어 Python 패키지 |
| ORB-SLAM3 | https://github.com/UZ-SLAMLab/ORB_SLAM3 | Visual/Visual-Inertial/Multiple Map SLAM, GPL-3.0 |
| COLMAP tutorial | https://colmap.github.io/tutorial.html | 영상/겹치는 이미지로 SfM 3D 복원. camera poses 및 points |
| COLMAP macOS | https://github.com/colmap/colmap/blob/main/doc/install.rst | Mac Homebrew `brew install colmap` 안내 |

### 사실
- 카메라만으로 Visual SLAM·SfM 가능하나 단안 절대 스케일 모호성 존재.
- Tello EDU는 SDK 2.0과 Mission Pad를 지원하며 DJI MSDK V5와 별개.
- Tello EDU SDK에는 GPS 지도·3D LiDAR·완전 안전 장애물 회피 기능이 제공되지 않음.

### 이번 개발의 선택
- Mac에서 YOLO/칼만/PID/VO/COLMAP + Mock Tello를 먼저 개발.
- 추후 Tello EDU 도입, Mission Pad를 로컬 좌표계 측정 앵커로 활용.
- 3D 지도는 카메라 기반 **희소 지도 또는 사후 복원 지도**로 구현하고 실제 LiDAR 지도와 혼동 금지.

### 현재 미확인
- Tello EDU로 사용자의 공간에서 얻을 지도 밀도·실시간 성능·안전성. 추가 실험 필요.
- Mac/Apple Silicon에서 특정 ORB-SLAM3 빌드 환경의 호환성. 실제 로컬 테스트 필요.
