# Tello EDU SDK 2.0: 하드웨어 연동 계획

## 선택 이유와 구매 전 확인
Tello EDU는 공개 Tello SDK를 통해 PC/Mac/모바일에서 Wi-Fi 로컬 제어, 영상 수신, Mission Pad 인식을 제공하는 저가형 개발 후보. 구매 전 기체 상태, 배터리, 모터, 프로펠러 가드, Mission Pads 포함, SDK 2.0 지원 펌웨어 확인. 구형 일반 Tello에는 Mission Pad 인식 기능이 없다.

| 연결 항목 | 주소/포트 | 공식 SDK 근거 |
|---|---|---|
| UDP 명령 및 응답 | Tello `192.168.10.1:8889` | `command`, `streamon`, `stop`, `rc a b c d`, `mon` |
| 상태 수신 | Mac UDP `0.0.0.0:8890` | yaw, vgx/vgy/vgz, tof, bat, mid, x/y/z 등의 상태 |
| H.264 영상 | Mac UDP `0.0.0.0:11111` | `streamon` 이후 |

Tello EDU에서 `rc a b c d`의 a/b/c/d는 각각 좌우/전후/상하/회전이며 값은 -100..100. `mon`, `mdirection 0/1/2` 명령은 Mission Pad 탐지. `mid` 및 x/y/z는 패드가 관측될 때에만 신뢰할 수 있다. 절대 GPS 좌표 아님. **자동 착륙/타임아웃 등 SDK 자체 동작을 정확히 이해하고, 송신 루프와 watch-dog를 Mock으로 검증해야 함.**

## 개발 원칙
- 기본 `MockTelloAdapter`에서 UDP 수신 모의 및 패킷 파서 테스트. 실제 하드웨어 접속은 사용자가 구입한 이후 별도 승인.
- 우선 드론 없는 Mac에서 영상/사람검출/VO/SLAM/COLMAP을 완성.
- 나중에 Tello 영상 스트림은 기존 `VideoSource` 인터페이스에 연결. Mission Pad 상태는 별도 `PoseAnchor`로 제공. Map status `RELATIVE`, pad 유효 시 `METRIC_ANCHORED` 표시.
- `streamon` 등 카메라만 다루는 테스트도 사용자 승인을 받고 실제 Tello에서 수행. 비행 없는 영상 테스트가 먼저.
- `emergency`는 모터 즉시 정지로 추락 위험. 일반 STOP과 구분, normal landing/hover 전략 별도.
- Tello는 GPS기반 Return To Home 지원 기체가 아님. 오차가 큰 영상 지도만 믿고 자동 복귀 금지.

공식: https://www.ryzerobotics.com/tello-edu/downloads
SDK2: https://dl-cdn.ryzerobotics.com/downloads/Tello/Tello%20SDK%202.0%20User%20Guide.pdf
Pad: https://dl-cdn.ryzerobotics.com/downloads/Tello/Tello%20Mission%20Pad%20User%20Guide.pdf
라이브러리: https://github.com/damiafuentes/djitellopy
