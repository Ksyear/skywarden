# 어떤 드론을 살까, 한국에서는 무엇을 해야 하나

**2026-11-09 웹 확인 기준. 구매·비행 전 공식 사이트(드론원스톱, 한국교통안전공단, 법제처, 제조사)에서 다시 확인하세요.** 가격·재고·법령은 바뀝니다. **프로젝트 정책: 기체 구매·실제 이륙·모터 송신은 사용자 승인 전 금지, `HARDWARE_ARMED=false` 유지.** 이 문서는 선택지와 절차를 정리한 것이지 구매 지시나 법률 자문이 아닙니다. 현재 시스템의 능력·한계는 `docs/SYSTEM_STATUS.md`(라이다 vs 카메라 비교, 라즈베리파이 추정 포함), Tello 프로토콜 근거는 `docs/TELLO_SDK.md`.

---

## 1. 무엇을 살까 — 3단계

### 1단계 · 지금 코드 그대로, 실내 안전 시험: **Tello EDU** (약 80 g)

- **왜:** 코드가 이미 Tello SDK 2.0(UDP 8889 명령 / 8890 상태 / 11111 영상)의 Mock 어댑터 기준으로 짜여 있어(`adapters/tello/`, `docs/TELLO_SDK.md`) 다리 작업이 없습니다. 실기체가 와도 첫 단계는 전원 + 영상 스트림(`streamon`)만 받고 모터 명령은 보내지 않는 것입니다.
- **단점:** EDU는 단종된 것으로 보입니다(DJI 교육 부문 종료 보도) → **중고 구매**가 현실적. 720p 카메라, GPS 없음, 바람에 약함, 탑재 여유가 수십 g이라 **라이다·깊이 카메라 탑재 불가**. 일반 Tello(비 EDU)는 Mission Pad·스웜 등 EDU 기능이 없습니다.
- **규정:** 250 g 이하라 조종자 증명·기체신고 대상이 아닙니다. 단 비행금지구역·비행승인·촬영허가·개인정보 규칙은 그대로 적용됩니다(2절).
- **대안 교육용 기체:** RoboLink CoDrone EDU(약 $215부터), Bitcraze Crazyflie 등. SDK가 달라 우리 어댑터를 새로 써야 합니다.

### 2단계 · 야외 항공 지도 품질: **DJI Mini 4 Pro** (249 g)

- **왜:** 영상 품질이 지도(SfM·밀집)에 훨씬 유리하고 249 g이라 조종자 증명·기체신고(현행) 대상이 아닙니다.
- **SDK:** DJI 공식 SDK 호환표에 Mobile SDK 지원(Android MSDK v5.13.0에서 추가)으로 나옵니다. **Mini 5 Pro는 SDK 미지원으로 표시**(2026-11 기준 GitHub 이슈 다수). 제어는 Android 앱(MSDK)으로 해야 하므로 우리 Python 스택과는 **다리 연결 작업**이 필요합니다. 프로젝트 규칙상 DJI MSDK V5를 제어 경로로 쓰는 것은 `docs/ARCHITECTURE.md` 플랫폼 절에 금지로 적혀 있으니, 쓰려면 그 규칙을 먼저 바꾸는 결정이 필요합니다. 영상만 녹화해 Mac에서 지도를 만드는 용도라면 SDK 없이도 됩니다.

### 3단계 · 라이다·라즈베리파이·자율 연구: **PX4 개발용 기체**

- **구성 예:** Holybro X500 V2 PX4 개발 키트(목록가 $609, 확인 시 품절) + Pixhawk 비행 컨트롤러 + Raspberry Pi 5(또는 Holybro Pixhawk RPi CM4/5 베이스보드, $248.98부터) + 카메라 + 선택적 2D/3D 라이다 또는 깊이 카메라.
- **왜:** 오픈소스(PX4/MAVLink)라 우리 Python에서 직접 제어할 수 있습니다 — 순서는 **MOCK → PX4 SITL(Gazebo) 시뮬레이션 → 실기체**. 라즈베리파이 역할은 `docs/SYSTEM_STATUS.md` 9절대로 촬영·전송·사람 추적(MOCK 제어만), 지도는 PC.
- **단점:** 조립·튜닝 실력이 필요하고 무게가 2 kg 안팎이 되기 쉬워 **3종 조종자 증명·기체신고 대상**이 될 수 있습니다(2절).

### 추천 요약

- 바로 시험 → **중고 Tello EDU**.
- 연구로 키우기 → **X500급 PX4 + RPi5 + (깊이 카메라 또는 라이다)**.
- **하드웨어 없이 먼저 할 수 있는 무료 단계:** PX4 SITL(Gazebo) 시뮬레이션에 우리 MOCK 제어를 연결. 이 단계는 기체 구매 없이 진행 가능합니다.

---

## 2. 한국에서 해야 할 일

| 항목 | 기준 | 어디서 | 근거·비고 |
|---|---|---|---|
| 조종자 증명 | 최대이륙중량 **250 g 이하 면제**; 250 g 초과~2 kg **4종**(만 10세 이상, 온라인 교육 이수, 시험 없음); 2~7 kg **3종** | 한국교통안전공단 | 무자격 비행 과태료는 출처마다 300만 / 400만 원 이하로 다름 → 법령 원문 확인 [1][2] |
| 기체 신고 | 현행: 사업용 또는 **2 kg 초과** | 드론원스톱 drone.onestop.go.kr — 소유 증명, 제원표, 측면 사진 | 2026년 **250 g 이상 비사업용까지 확대**하는 시행령 개정안 입법예고(의견수렴 10/26까지) — 시행 여부 확인 필요 [3][4][5] |
| 비행 승인 | 관제권(공항 주변), 비행금지구역(예: 서울 도심, 원전 주변), 고고도 등은 **사전 승인**; 250 g 이하도 면제 아님 | 드론원스톱 | 처리 평균 3일·최대 6일(근무일; 2차 자료). 비행 전 드론원스톱 지도에서 공역 확인 [6][7][8] |
| 항공촬영 허가 | 비행승인과 **별도**; 촬영 **4일 전(근무일)** 까지 신청 | 드론원스톱 → 국방부 | [7] |
| 야간·가시권 밖 | 일몰 후~일출 전, 가시권 밖은 **특별비행승인** | 지방항공청 | 조종 능력·경력 서류, 보험 증명; 검사 30일 이내(최대 90일) [6] |
| 개인정보보호법 제25조의2 (이동형 영상정보처리기기, 2023-09-15 시행) | 업무 목적으로 공개 장소에서 사람을 촬영하는 것은 **원칙적 제한**; 예외는 수집·이용 사유에 해당하거나 촬영 사실을 알리고 거부가 없는 경우; 불빛·소리·안내판 등으로 **촬영 표시**(드론은 개인정보포털 공지 가능) | 개인정보보호위원회 / 개인정보포털 | 사람 추적 드론은 정면으로 해당 → **동의한 참가자만 촬영, 촬영 표시, 영상은 로컬 보관**(현재도 업로드 없음). 조문 번호는 법령 원문에서 재확인 권장 [9][10][12] |
| 해외직구 | 개인 사용 **1대**는 전파법 적합성평가(KC) 면제; **1년 내 재판매** 시 위반 소지(징역 3년 이하·벌금 3천만 원 이하) | 국립전파연구원 | 드론 특유 규정은 별도 확인 [11] |
| 학교 안 비행 | 캠퍼스 시설 관리 부서 허가 | 소속 학교 | **권장**(법적 근거 아닌 관례) |
| 보험 | 사업용은 필수, 개인도 권장 | — | 특별비행승인 시 보험 증명 필요 [6] |

---

## 3. 순서 체크리스트

1. 기체 고르기(1절) — 구매는 사용자 승인 후.
2. 250 g 초과 기체면 **4종(또는 3종) 조종자 증명** 온라인 교육 이수.
3. 해당 시(사업용·2 kg 초과, 또는 개정안 시행 시 250 g 이상) **기체신고**.
4. 비행 장소의 **공역 확인**(드론원스톱 지도).
5. 필요 시 **비행승인 + 항공촬영 허가** 신청(각각 별도, 근무일 기준 여유 두기).
6. **촬영 대상 동의**와 **촬영 표시** 준비; 영상은 로컬 보관.
7. 실내 또는 그물망 안에서 **MOCK → 영상만(`streamon`)** 시험; 모터 명령 송신 없음(`HARDWARE_ARMED=false`).
8. **사용자 승인 후**에만 실제 비행.

---

## 출처 (2026-11-09 확인)

1. https://www.easylaw.go.kr/CSP/CnpClsMain.laf?csmSeq=1814&ccfNo=2&cciNo=1&cnpClsNo=1
2. https://easylaw.go.kr/CSP/CnpClsMain.laf?popMenu=ov&csmSeq=1814&ccfNo=2&cciNo=3&cnpClsNo=1
3. https://www.cstimes.com/news/articleView.html?idxno=619465
4. https://www.cnbnews.com/news/articleView.html?idxno=1016028
5. https://www.kdrm.kr/news/articleView.html?idxno=603876
6. https://easylaw.go.kr/CSP/CnpClsMain.laf?popMenu=ov&csmSeq=1814&ccfNo=3&cciNo=1&cnpClsNo=2
7. https://studiomorak.com/insights/drone-filming-permit-guide
8. https://news.seoul.go.kr/gov/archives/554433
9. https://www.lawtalk.co.kr/posts/77811
10. https://m.boannews.com/html/detail.html?idx=125815
11. https://quasarzone.com/bbs/qc_plan/views/27245
12. https://www.law.go.kr/LSW/expcInfoP.do?expcSeq=328279
13. https://holybro.com/products/px4-development-kit-x500-v2
14. https://holybro.com/products/pixhawk-rpi-cm4-baseboard
15. https://support.dji.com/help/content?customId=01700000763&documentType=&lang=en&paperDocType=ARTICLE&re=US&spaceId=17
16. https://github.com/dji-sdk/Mobile-SDK-Android-V5/issues/692
17. https://www.thedronegirl.com/2023/12/29/tello-drone-discontinued/
18. https://pilotinstitute.com/dji-tello-alternatives-top-3-best-educational-drones/
19. https://drone.onestop.go.kr

출처 중 7, 9, 10, 11은 법령 원문이 아닌 2차 자료(블로그·기사·커뮤니티)이므로 수치(처리 기간, 과태료, 벌금)는 1, 2, 6, 12와 드론원스톱에서 재확인하세요.
