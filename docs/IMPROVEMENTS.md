# Skywarden 개선사항 전수 목록 — 2026-11-10

작성: fable-documenter (claude-fable-5-1). **2026-11-15 상태 갱신: Opus (사용자 지시로 문서 작성을 맡음; 겹침 추적·박스 크기 비례 잡음 라운드까지 반영)** — 아래 "처리 상태" 표와 각 항목의 `[2026-11-15 상태: …]` 표시만 추가했고, 원래 서술은 그대로 두었습니다(그래서 본문의 "현재:" 설명은 2026-11-10 기준입니다). 이 문서는 저장소의 코드·문서·증거 파일을 직접 읽어 확인한 개선 항목만 모은 것입니다. 각 항목에는 근거 위치(`파일:줄`, 문서 절, 또는 증거 JSON 경로)를 붙였고, 확인하지 못한 것은 **미확인**으로 표시했습니다. 실행해서 새로 측정한 수치는 없으며, 인용한 수치는 모두 기존 증거 파일에서 가져왔습니다. 원시 조사 자료는 `outputs/evidence/improvements/repo_survey.txt`(파일별 줄 수, 표식, 테스트 목록, 설정 유무, 문서 길이, 출력 용량)입니다.

**요약.** Skywarden은 카메라 한 대의 영상으로 희소·밀집 3D 지도를 만들고 사람을 추적해 Mock 드론에 연결하는 로컬 macOS 프로젝트이며, 핵심 경로(희소 지도 GOOD, 검증 밀집 점군, YOLOX+Kalman 추적, Mock 제어, 라이브 실행, 콘솔)는 동작하고 자동 테스트 134개가 통과합니다. 가장 큰 열린 과제는 (1) 사용자 본인 영상으로 아직 한 번도 돌리지 않은 것, (2) 모든 지도가 상대 스케일(미터 아님)인 것, (3) 자동화된 CI와 안정된 테스트가 없는 것, (4) 밀집 지도 개선 변형이 전부 사전 기준을 넘지 못해 기본 레이어가 그대로인 것, (5) 15,000줄 코드 중 큰 함수·중복 레이어 목록·오래된 설계 문서가 유지보수를 어렵게 하는 것입니다. 아래 표는 그중 상위 10개이고, 본문은 영역별 전체 목록(총 89개 항목)과 "이미 시도했으나 미채택" 목록(26건)입니다.

읽는 법: 우선순위 P0(지금 막힌 게이트) / P1(다음 라운드) / P2(기회가 될 때). 난이도 S(반나절 이내) / M(며칠) / L(주 단위 또는 외부 자원 필요). "사용자 승인 필요"는 기체 구매·실제 비행·시스템 설치가 전제인 항목입니다.

## 처리 상태 (2026-11-15)

미터 스케일 라운드(2026-11-14), 포트폴리오 품질 라운드, 겹침 추적 라운드(`docs/PLAN_OCCLUSION_TRACKING.md`), 박스 크기 비례 잡음 라운드(`docs/PLAN_SCALE_AWARE_NOISE.md`, 모두 2026-11-15)에서 처리한 항목입니다. 상태: **DONE**(제안 범위를 처리하고 증거가 있음) / **PARTIAL**(일부만 처리) / **폐기**. 여기 없는 항목은 그대로 열려 있으며, M-01(본인 영상 게이트)은 2026-11-15 **PARTIAL**: 사용자가 복도 영상을 직접 찍었다고 확인해 `REAL_VIDEO`로 다시 라벨링했고 지도는 GOOD(139/139, 10,406점)이지만, 사람이 나오지 않아 추적은 NOT_RUN(캐비닛 오검출 트랙 1개 확인; `outputs/evidence/real_video/`). 증거 경로는 각 항목 본문의 표시를 보세요.

| ID | 상태 |
|---|---|
| S-01 | PARTIAL |
| S-02 | 폐기(사용자 결정) |
| T-01 | DONE (기본 `VAB` 채택; 남은 원인 F-34·F-35는 trial `VABS`가 고치지만 미채택) |
| T-02 | DONE (MOT17 train 보정·보류 분할, 라벨 없는 클립은 여전히 미정의) |
| T-04 | PARTIAL (마진·크기 항·측정/과정 잡음 격자를 MOT17 보정 분할 + E2로 스윕; 만료·확정 히트·점수 임계값은 스윕 안 함) |
| Q-01 | PARTIAL |
| Q-02 | DONE |
| Q-03 | PARTIAL |
| Q-04 | DONE |
| Q-05 | PARTIAL |
| Q-06 | DONE |
| U-01 | DONE |
| U-02 | PARTIAL |
| V-01 | DONE |
| V-04 | DONE |
| P-02 | DONE |
| P-03 | DONE |
| C-01 | PARTIAL |
| C-02 | DONE |
| C-07 | DONE |
| C-12 | PARTIAL |
| R-01 | PARTIAL |
| R-02 | PARTIAL |
| W-01 | DONE |
| W-02 | DONE |
| W-03 | PARTIAL (2026-11-15 README·SYSTEM_STATUS 재정리: 이력 수치는 VALIDATION_LOG·계획 문서로 넘기고 링크) |
| W-04 | DONE |
| W-06 | PARTIAL |

## 우선순위 상위 10개

| # | 항목 | 왜 중요한가 | 난이도 | 근거 |
|---|---|---|---|---|
| 1 | **본인 영상(REAL_VIDEO) 게이트 실행** (M-01) | 지금까지 공개 CC BY 클립과 origin 미지정 복도 영상으로만 검증됨. 프로젝트가 "내 공간"에서 되는지 아직 모름 | M | `docs/PROGRESS.md` 190-199행, `docs/SYSTEM_STATUS.md` 2절 "본인 영상 게이트" NOT_RUN |
| 2 | **미터 스케일 앵커링 + 보유 2D 라이다로 보정** (S-01, S-02) | 모든 지도가 상대 스케일이라 거리·크기를 말할 수 없음. 라이다가 있으므로 실측 길이를 넣을 길이 열려 있음 | M | `docs/PROGRESS.md` 171행 NOT_RUN, `docs/SYSTEM_STATUS.md` 2절 "미터 스케일 앵커링" 미완 |
| 3 | **CI 도입** (Q-01) | `.github/` 없음. 테스트는 수동 `scripts/test_all.sh`뿐이라 회귀가 사람 손에 달려 있음 | S | Glob `.github/**` 결과 없음, `repo_survey.txt` 221-255행 |
| 4 | **간헐 실패 테스트 안정화** (Q-02) | `test_live_run_lifecycle_control_and_integrity`가 첫 실행에서 실패, 재실행 통과. CI가 생기면 바로 빨간불이 됨 | M | `outputs/evidence/tests/pytest-20261109-follow.log`, `tests/test_server.py:203-242` |
| 5 | **UI 스모크 재현성** (Q-04) | `--attach` 없이는 새 헤드리스 브라우저를 띄우고, 픽스처가 gitignore된 `outputs/`에 의존해 새 클론에서 돌릴 수 없음 | S | `tests/ui/ui_smoke.mjs:16,19-20,45-55` |
| 6 | **기본 밀집 레이어의 바닥 공백 — 기준 재정의 후 재판정** (M-02) | 카메라 앞 보도 바닥이 거의 비어 있고(`cov1px` 0.0244/0.0024), 모든 개선 변형이 미채택. 측정 지표 자체의 한계가 기록돼 있어 새 기준선이 필요 | L | `docs/FAILURES.md` F-14, F-15; `docs/PLAN_DENSE_COVERAGE.md` 10절 |
| 7 | **대형 함수 분리와 오래된 docstring 정리** (C-01, C-02) | `run_live` 약 490줄 단일 함수, `dense_cpu.py` 898줄에 CLI 플래그 14개, 모듈 설명은 대체된 SGBM 방식을 설명 | M | `packages/vision/live_run.py:272-762`, `packages/mapping/dense_cpu.py:1-16,650-651,858-879` |
| 8 | **README 축소·문서 중복 제거·오래된 설계 문서 배너** (W-01~W-04) | README 117 KB, 표 한 셀 약 1,300자, 같은 수치가 4곳에 반복, 초기 설계 문서가 현재 하드웨어·방법과 불일치 | S | `README.md:14`, `repo_survey.txt` 252·290행, `docs/IMPLEMENTATION_PLAN.md:5`, `docs/VERIFICATION_CONTRACT.md:22` |
| 9 | **콘솔 서버의 요청 출처 검사** (V-01) | 127.0.0.1 바인드·인증 없음·Origin/Content-Type 검사 없음 → 브라우저에서 다른 사이트가 로컬 빌드/라이브 실행을 시작시킬 수 있음(기체 제어 경로는 없음) | S | `apps/desktop/server.py:551-554,596-597` |
| 10 | **뷰어 성능(점 솎아내기/WebGL)과 PLY 스트리밍** (P-02, P-03) | 8.2M점 레이어를 서버가 통째로 메모리에 읽고, 브라우저는 매 프레임 모든 점을 JS 루프로 투영(3.2M점 p50 약 80 ms) | M | `apps/desktop/server.py:467`, `apps/desktop/static/viewer.js:254-269`, `outputs/evidence/rpi/rpi_feasibility.json` `app_viewer_in_pi_browser` |

---

## 1. 지도 품질 (희소·밀집·학습형)

**M-01 (P0 · M) 본인 영상 게이트가 열려 있음.** 현재: `assets/input/`이 비어 있고 `data_origin=REAL_VIDEO` 세션이 하나도 없음(복도 영상은 `UNSPECIFIED`). 문제: 공개 클립 3종에서 GOOD이지만 사용자 환경·카메라에서의 결과는 모름. 제안: 실내 정적 장면, 옆으로 천천히 이동, 30–90 s 클립을 `assets/input/`에 두고 `scripts/build_sparse_map.sh … --origin REAL_VIDEO`와 `scripts/run_tracking.sh … --origin REAL_VIDEO` 실행, 결과 경로를 `docs/VALIDATION_LOG.md`에 기록. 근거: `docs/PROGRESS.md` 190-199행, `docs/FAILURES.md` 388-390행, `docs/SYSTEM_STATUS.md` 2절.

**M-02 (P1 · L) 기본 밀집 레이어(M0)의 바닥 공백.** 현재: 스미다 `f000240`/`f000900` 바닥 `cov1px` 0.0244 / 0.0024, 보도가 거의 비어 있음. 문제: 바닥 커버리지 실험의 최종 변형 ESVFN2x25는 전체 클립과 FSC에서 모든 기준을 만족했지만 사전 등록된 선별 구간(seg30)의 `float_fs`에서 FAIL(+11.3 pp, 허용 +2 pp). 그 지표는 "검사하는 뷰 수가 늘면 더 엄격해지는" 편향이 기록돼 있고(`float_fs_same_maps`는 0.3586 → 0.2634로 개선), `cov1px`는 복셀 크기에 상한이 묶여 있음(1× 복셀에서 `f000240` 상한 0.0262). 제안: 새 날짜 절에서 (a) 같은 뷰 집합 기준의 자유공간 지표와 (b) 줄무늬 지표(열별 점 밀도 이상치)를 수용 지표로 정의하고 M0 기준선 표를 다시 만든 뒤 ESVFN2x25를 재판정. 채택은 사용자 결정. 근거: `docs/FAILURES.md` F-12(162행), F-14(184행), F-15(200행); `docs/PLAN_DENSE_COVERAGE.md` 96·156행.

**M-03 (P1 · M) 강물 반사가 수면 아래 가짜 점으로 삼각측량됨.** 현재: 스미다에서 모델 깊이 약 2인 곳에 우리 삼각측량점의 절반이 깊이 14–17에 있음(건물 반사). steady 레이어는 곡선을 바깥으로 키워 피했지만, 희소 지도 자체에는 거르는 규칙이 없음. 제안: 희소 단계에서 (a) 지면 평면 아래 깊이 점, (b) 수면 대칭 위치에 같은 색 점이 있는 경우를 표시·제거하는 필터를 추가하고 FSC 회귀 가드(150/150, 점 ≥ 0.9×, 재투영 ≤ 0.60 px)로 검증. 근거: `outputs/evidence/learned_depth/steady_layer_v2.json` `findings.river_reflections`, `docs/SYSTEM_STATUS.md` 5절, `docs/PLAN_LEARNED_DEPTH.md` 200행.

**M-04 (P1 · M) 항공 영상에서 모델이 먼 지면을 1.5–5배 가깝게 추정.** 현재: 측정만 되어 있고, 우리 점이 지지하지 않는 거리의 픽셀을 버리는 것 외 보정 없음. 제안: 거리대(quintile)별 비율을 `dense_report.json`에 기록하고, 모델 신뢰 거리 상한을 클립별로 자동 산출. 모델 출력 레이어는 계속 "모델 추정"으로 라벨. 근거: `steady_layer_v2.json` `findings.pancake_cause`, `docs/PROGRESS.md` 231행(TODO 15).

**M-05 (P1 · M) 학습 깊이 검증 레이어의 빈 구간.** 현재: 스미다 10–20, 26–40, 46–60 s, FSC 26–30, 38–60, 68–71 s에 채택된 청크 없음(겹침 불일치, 삼각측량 스케일 1.18/2.22, 빠른 회전 정렬 잔차 5–6 %). 제안: R8의 다음 테스트 — 회전 안쪽에 앵커한 4 s 청크(겹침 2 s), 넓은 잔디밭 구간을 양쪽으로 걸치는 청크, 청크별 사용 근접점 수를 인덱스에 기록. 근거: `docs/PLAN_LEARNED_DEPTH.md` R8(180-191행), `docs/FAILURES.md` F-28(336행), F-29(346행).

**M-06 (P2 · M) 항공 영상용 먼 점 정리 규칙.** 현재: FSC에서 held-out 프레임 구조물 −2.0 pp로 FAIL, 적용 안 함(보행 카메라 기준 규칙). 제안: 클립 깊이 분포에 따른 "먼 점" 임계값과 가용 뷰 수를 세는 지원 임계값 — 새 계획 항목. 근거: `docs/FAILURES.md` F-31(366행).

**M-07 (P2 · S) 하늘 경계 분할 라벨.** 현재: 나뭇가지가 하늘로 라벨되어(384 px에서 약 4 px 경계) 정적 지도 trial의 구조물 손실이 남음. 제안: 다른 분할 모델 또는 가지 클래스의 프레임별 정제를 같은 5개 감사 프레임에서 평가. 근거: `docs/FAILURES.md` F-16(210행), `docs/PROGRESS.md` 228행.

**M-08 (P2 · M) 사람을 따라가는 영상(위트레흐트)의 PARTIAL.** 현재: 사람 마스킹으로 점프 43 → 38이지만 스미다/FSC 가드를 돌리지 않아 미채택, 시작 구간(0.4–7.2 s)의 늘어남은 원인 미상. 제안: 마스킹 변형을 가드에 통과시키고, 7.2 s 이후 하위 구간으로 초기화 효과인지 확인. 근거: `docs/FAILURES.md` F-11(152행).

**M-09 (P2 · M) Poisson 메시 시각 검사 FAIL.** 현재: FSC는 화면 중앙으로 모이는 방사형 줄무늬(원인 미확인), 스미다는 나무 몇 그루만. 제안: 줄무늬가 법선 추정인지 트리밍 반경인지 분리 실험; 그 전까지 콘솔에 내지 않음(현행 유지). 근거: `docs/FAILURES.md` F-09(128행).

**M-10 (P2 · S) FULL_OPENCV 보정(k3)으로 초기화 실패 원인 미확정.** 현재: k3를 빼면 통과(가설). 제안: 5계수로 미리 왜곡 보정한 프레임을 PINHOLE로 빌드하는 테스트와 k3=0 명시 테스트. 근거: `docs/FAILURES.md` F-01(5-33행), `docs/PROGRESS.md` 218행.

**M-11 (P2 · S) 전역 매퍼는 MAP 단계에서 취소가 듣지 않음.** 현재: 기본 매퍼가 global인데 취소는 "이 단계가 끝난 뒤" 반영. 문제: UI에서 Cancel을 눌러도 수 분간 반응 없음. 제안: 상태 메시지에 예상 대기 시간 표시, 또는 자식 프로세스 SIGKILL + CANCELLED 보고 경로 추가. 근거: `packages/mapping/sparse_pipeline.py:50,307`.

**M-12 (P2 · S) 루프 클로저 없음.** 현재: `pairing.loop_detection = False`(어휘 트리 파일 미포함), VO도 루프 클로저 없음. 제안: 어휘 트리 파일을 선택적으로 내려받는 스크립트와 `--loop-detection` 옵션; 긴 클립에서 궤적 점프가 줄어드는지 측정. 근거: `packages/mapping/sparse_pipeline.py:287`, `packages/vision/vo.py:16-17`.

**M-13 (P2 · M) 시간 조항이 병렬 부하 속에서 측정됨(UNMEASURED_QUIET).** 현재: D12만 조용히 재측정(400.0 s). 제안: 한가한 머신에서 `config` 블록 플래그로 V2_full 등 재실행. 근거: `docs/FAILURES.md` F-25(308-314행).

**M-14 (P2 · M) 저장된 지도 대비 재측위 미구현.** 현재: 모든 VO·라이브 헤더가 `relocalization=NOT_IMPLEMENTED`, `map_position=NOT_AVAILABLE`. 제안: 저장된 희소 지도의 3D 점·디스크립터에 대한 PnP 재측위 실험(상대 스케일 한정, NOT_NAVIGATION_SAFE 유지). 근거: `docs/VALIDATION_LOG.md` 108행, `docs/PROGRESS.md` 167행.

**M-15 (P2 · S) steady 레이어의 국소 보정 구간(28–38 s) 검증 미측정.** 현재: 근처 희소점 일치만 조건이고 다른 뷰와의 일치는 따로 재지 않음. 제안: 그 구간 점을 이웃 프레임 깊이맵에 투영해 `float_fs`/`inconsist`를 별도 산출. 근거: `docs/SYSTEM_STATUS.md` 11절 3(a), `docs/PROGRESS.md` 231행.

## 2. 스케일·센서 (라이다 포함)

**S-01 (P0 · M) 미터 스케일 앵커링 NOT_RUN.** **[2026-11-15 상태: PARTIAL]** `scale.json` 측정·검증 경로 구현, 공개 실제 녹화 TUM fr1/desk에서 MEASURED_VERIFIED(점 기준은 1차 FAIL 뒤 결정), 테니스장 항공은 MEASURED_UNVERIFIED, 사용자 촬영 마커 영상(S4)은 NOT_RUN — `outputs/evidence/metric_scale/`, `docs/PLAN_METRIC_SCALE.md` 10절. 현재: 어떤 지도에도 미터가 없고, ATE의 cm 값은 정답 궤적에서 스케일을 빌린 평가 전용. 라벨 체계에는 `METRIC_ANCHORED`가 이미 정의됨. 제안: 체커보드로 카메라 보정 → 실측 길이의 AprilTag/자를 찍은 클립 → 희소 지도의 두 점 거리와 실측으로 스케일 계수 → `scale=METRIC_ANCHORED`는 측정된 구간에만. 근거: `docs/PROGRESS.md` 171행, `docs/CAMERA_ONLY_EXECUTION_PLAN.md` 16·21행, `docs/SYSTEM_STATUS.md` 2절.

**S-02 (P1 · M) 사용자 보유 2D 라이다로 스케일 보정 (계획만).** **[2026-11-15 상태: 폐기(사용자 결정)]** 2D 라이다안은 사용자 결정으로 폐기. 대신 라이다 없는 방법(마커·두 점·텔레메트리) — `docs/PLAN_METRIC_SCALE.md`. 현재: 세션에서 2D 라이다 보유가 확인되었고 코드·계획 문서는 없음(**미확인**: 기종). 문제: 2D 라이다는 수평 한 줄만 재므로 드론 탑재보다 "지상에서 같은 공간의 단면을 한 번 재는" 용도가 현실적. 제안: (1) 라이다 스캔 한 바퀴(미터)를 저장, (2) 카메라 지도에서 같은 높이의 벽 점을 수평 단면으로 추출, (3) 2D ICP로 정합해 스케일 계수와 잔차 기록, (4) 잔차가 기준 이하인 세션만 `METRIC_ANCHORED`. 근거: `docs/DRONE_PURCHASE_AND_KOREA_RULES.md` 74행(RPLIDAR C1 비고 "수평 한 줄만"), `docs/SYSTEM_STATUS.md` "라이다 vs 카메라" 표.

**S-03 (P1 · S) VO 초점거리 추정값(fx = 0.9 × 너비)이 틀려 스미다 23.3 % FAIL.** 현재: 보정 파일이 없으면 `ESTIMATED`로 돌아감. 제안: 체커보드 보정 파일을 라이브 실행 기본 `--calib`로 연결, 없으면 UI에 "UNCALIBRATED — 경로 신뢰 불가" 배지. 근거: `packages/vision/vo.py:466-469`, `docs/FAILURES.md` F-10(142행).

**S-04 (P2 · S) 카메라 보정 도구가 저장소에 없음.** 현재: 설계 문서는 `camera_calibration.yaml`을 1단계로 권하지만 체커보드 보정 스크립트가 없음(`scripts/` 목록과 `repo_survey.txt` 1-71행 소스 목록에 보정 모듈 없음). 제안: `python -m vision.calibrate <video>`로 OpenCV 체커보드 보정 → `camera.json` 생성. 근거: `docs/VISUAL_MAPPING.md` 24행.

**S-05 (P2 · S) 단일 프레임 상대 깊이 프리뷰(plan D5) NOT_RUN.** 현재: 계획만. 제안: 학습 깊이 경로가 이미 있으므로 우선순위 낮음; 하려면 `ALIGNED_PREVIEW` 라벨로만. 근거: `docs/PROGRESS.md` 169행.

## 3. 사람 탐지·추적

**T-01 (P1 · M) 모션 전용 추적의 한계.** — **2026-11-15 처리:** `docs/PLAN_OCCLUSION_TRACKING.md` 7절(마진 연관 + GMC 채택, 외모 ReID는 opt-in; F-33은 margin 모드에서 수정). F-34(큰 박스에서 고정 6 px 잡음)는 `docs/PLAN_SCALE_AWARE_NOISE.md`에서 원인 확인 후 `VABS`로 고쳤지만(위트레흐트 LOCKED 19.2 → 69.1 %) 라이브 속도 기준 하나를 넘지 못해 trial이고, 그 과정에서 기본 `VAB`의 큰 박스 잘못된 대상 잠김(F-35)을 찾았습니다. 아래는 당시 기록. 현재: 위트레흐트 라이브 30 s에서 LOCKED 17 %(155/899), 교차 시 AMBIGUOUS → STOP, 재획득 보장 없음(설계), 옷 색 재식별은 NO_GO. 제안(정책 결정 필요): (a) 학습 재식별 임베딩은 라이선스·개인정보 정책을 먼저 결정, (b) 비생체 대안 — 박스 높이/종횡비 일관성, IMM(다중 모델) Kalman, 짧은 가림 전용 완화 — 를 E2 하네스(700회)와 실제 3클립으로 판정. 근거: `docs/PROGRESS.md` 162행(LV-T7), `docs/FAILURES.md` F-05(75-88행), `docs/VALIDATION_LOG.md` 134행 "Possible next step".

**T-02 (P1 · M) MOTA/IDF1 미정의.** — **2026-11-15 처리:** MOT17 train 7개 시퀀스(CC BY-NC-SA 3.0, 로컬 평가 전용)를 TrackEval로 채점(`outputs/evidence/occlusion_tracking/mot17/`). 라벨 없는 클립은 여전히 미정의. 아래는 당시 기록. 현재: 정답 박스 있는 시퀀스가 없음. 제안: 라이선스 확인된 공개 MOT 시퀀스 1개 또는 본인 클립 30 s 수동 라벨로 산출; 라벨 없는 클립은 계속 "미정의". 근거: `docs/PROGRESS.md` 221행.

**T-03 (P2 · S) 검출기 티어·모델 비교 NOT_RUN.** 현재: yolox_tiny 비교, onnxruntime/HOG 티어 미실행. 제안: 같은 3클립에서 p50/p95·검출률 표. 근거: `docs/PROGRESS.md` 188·221행.

**T-04 (P2 · S) 추적기 임계값의 민감도 분석 없음.** — **2026-11-15 PARTIAL:** 마진·크기 항(`outputs/evidence/occlusion_tracking/calib*/`)과 측정·과정 잡음 비례 격자 9점(`outputs/evidence/scale_aware_noise/calibration_decision.json`)을 MOT17 보정 분할 + E2 700회로 스윕. 아래는 당시 기록. 현재: 가속 잡음 150, 측정 잡음 6 px, coast 2.0 s, 픽셀 게이트 0.5×박스 높이, 확정 3히트, 점수 0.3이 코드 기본값이며 E2 시뮬레이션으로만 정당화. 제안: 파라미터 스윕 하네스(E2 + 실제 클립 id 수·AMBIGUOUS 비율). 근거: `packages/tracking/tracker.py:66-76`.

**T-05 (P2 · S) 실제 웹캠 라이브 NOT_RUN.** 현재: 이 Mac에 카메라 없음(`NO_DEVICE` 경로만 검증). 제안: USB 웹캠 연결 후 `webcam:0` 30 s 실행, 지연 p95 기록. 근거: `docs/PROGRESS.md` 161행(LV-T6).

## 4. 실시간 처리·성능

**P-01 (P1 · M) 지도는 오프라인(영상 1분당 6–12분).** 현재: SfM 74–213 s, 평면 스윕 269–508 s(일부 병렬 측정). 제안: 단계별 프로파일(SIFT CPU 스레드, 150 프레임 캡, 긴 변 1600 px)과 해상도·프레임 수 트레이드오프 표를 만들어 "빠른 미리보기" 프리셋 정의. 근거: `docs/SYSTEM_STATUS.md` 6절, `packages/mapping/frame_selection.py:30-32`.

**P-02 (P1 · S) 서버가 PLY 전체를 메모리로 읽어 응답.** **[2026-11-15 상태: DONE]** PLY를 조각 단위로 스트리밍, `Range` 1구간 지원, `?max_points=N` 결정적 균등 솎아내기. FSC 156 MB PLY 전송 시 서버 최대 RSS 증가 +149 MB → +3 MB — `outputs/evidence/portfolio/p02_bench.jsonl`. 현재: `path.read_bytes()`로 한 번에 전송; FSC `dense_learned`는 8,234,561점. 제안: 청크 스트리밍/`Range` 지원, 레이어별 데시메이션 PLY를 미리 만들어 기본 로드. 근거: `apps/desktop/server.py:462-469`, `docs/SYSTEM_STATUS.md` 3절 표.

**P-03 (P1 · M) 브라우저 뷰어가 매 프레임 모든 점을 JS로 투영.** **[2026-11-15 상태: DONE]** WebGL 점 렌더 + 2D 대체 경로의 LOD. FSC 3,226,170점 프레임 p50 61.4 → 7.6 ms(dpr 1), 121.2 → 12.6 ms(dpr 2); 스미다 1,367,234점 42.0 → 3.5 ms(dpr 1) — `outputs/evidence/portfolio/render_bench.json`, `node_2d_viewer_check.txt`, `lod/`. 현재: 2D 캔버스 z-버퍼 스플랫, 3.2M점 p50 약 80 ms; Follow 글라이드(180 ms) 중 연속 재그리기. 제안: 줌 거리별 점 솎아내기(LOD), WebGL 포인트 렌더 또는 OffscreenCanvas 워커. 라즈베리파이 브라우저에서는 0.4–0.6 s/프레임 추정. 근거: `apps/desktop/static/viewer.js:150,254-269`, `outputs/evidence/rpi/rpi_feasibility.json` `app_viewer_in_pi_browser`.

**P-04 (P2 · S) VO 경로 API가 매 호출마다 타임라인 전체를 파싱.** 현재: `tl.read_text().splitlines()` 후 전부 JSON 파싱. 제안: 마지막 오프셋부터 증분 읽기 + 세그먼트 캐시. 근거: `apps/desktop/server.py:378-399`.

**P-05 (P2 · S) 세션 목록이 매번 모든 status/report JSON을 읽고 15 s마다 폴링.** 현재: `sessions()`가 디렉터리 전체 순회; 라이브 탭은 250 ms 렌더. 제안: mtime 기반 캐시. 근거: `apps/desktop/server.py:188-219`, `apps/desktop/static/app.js:563`, `apps/desktop/static/live.js:181`.

**P-06 (P2 · M) 학습 모델 메모리 한계.** 현재: KV 창 16에서 약 75프레임, 피크 11 GB, 전체 클립 한 번에 실행은 BLOCKED(저메모리). fp16 미실행. 제안: 청크 자동화는 있으므로 A1 정밀도 조항(fp16 4–14 s 구간)을 실제로 돌려 메모리·오차 기록. 근거: `docs/FAILURES.md` F-27(326-334행), `docs/SYSTEM_STATUS.md` 8절.

## 5. 앱·UI

**U-01 (P1 · S) UI 스모크 체크 수 88 → 71 감소 원인 미확인.** **[2026-11-15 상태: DONE]** 원인: 다섯 블록이 `outputs/maps`만 보고 건너뛰었는데 해당 세션이 `outputs/archive`로 옮겨져 18개 체크가 기록 없이 빠짐(88 → 71). 이제 SKIP으로 기록 — `outputs/evidence/portfolio/u01_check_count.json`. 현재: 아카이브 이동 후 줄었고 아무 문서도 원인을 적지 않음. 제안: 두 `ui_smoke.json`의 체크 이름 diff를 기록. 근거: `docs/SYSTEM_STATUS.md` 10절, `docs/PROGRESS.md` 203·231행.

**U-02 (P1 · S) Live 탭 상태 배지 5종과 지도 콘솔 STALE_DATA 배너 브라우저 미검증.** **[2026-11-15 상태: PARTIAL]** Live 탭 `STALE_DATA`/`STALLED`/`FAILED`/`INCOMPLETE`/`CANCELLED` 브라우저 체크 PASS(`ui_smoke_final_opus.log`). 지도 콘솔 쪽 STALE_DATA 배너 체크는 이 로그에서 확인하지 못함. 현재: `STALE_DATA/STALLED/FAILED/INCOMPLETE/CANCELLED` 라벨은 `live.js`에 있으나 스모크가 구동하지 않음. 제안: ScriptedSource로 각 상태를 강제하는 체크 추가. 근거: `docs/PROGRESS.md` 164행(LV-T8), `docs/FAILURES.md` 391행.

**U-03 (P2 · S) 레이어 목록이 세 곳 이상에 하드코딩.** 현재: 서버 allow-list(13개 폴더), `app.js`의 `densePaths`와 라벨·제목·기하 사전 4개, `index.html`의 `<option>`. 새 레이어마다 4곳 이상 수정. 제안: 서버가 세션 폴더에 존재하는 `*/dense_report.json`의 `label`을 읽어 `/api/sessions/<id>`에 레이어 목록으로 내려주고 UI는 그걸로 메뉴 생성. 근거: `apps/desktop/server.py:38-51`, `apps/desktop/static/app.js:214,283-308`, `apps/desktop/static/index.html:58-66`.

**U-04 (P2 · S) UI 언어가 영어, 사용자 문서는 한국어.** 현재: `lang="en"`, 모든 라벨·힌트 영어. 제안: 라벨 사전 분리 후 한/영 토글(기본 한국어). 근거: `apps/desktop/static/index.html:2`, `apps/desktop/static/app.js:9-21`.

**U-05 (P2 · S) 접근성 보완.** 현재: aria-label·aria-pressed·live region은 잘 붙어 있음. 빈틈: 캔버스(지도·커버리지 띠·타임라인)는 색으로만 상태 구분, `prefers-reduced-motion`은 진행바에만 적용되고 Follow 글라이드 애니메이션에는 미적용. 제안: reduced-motion 시 글라이드 끄기, 커버리지 띠 상태를 텍스트 요약으로도 제공. 근거: `apps/desktop/static/app.css:242`, `apps/desktop/static/viewer.js:150`, `apps/desktop/static/index.html:42-43`.

**U-06 (P2 · S) 터치·좁은 화면.** 현재: viewport 메타는 있으나 뷰어 입력은 포인터 드래그·휠·키보드만(핀치 줌 없음); 스모크는 1100×800, 700×900만 검사. **미확인**: 실제 태블릿 동작. 제안: 터치 핀치/두 손가락 팬 추가, 모바일 뷰포트 스모크 1종. 근거: `apps/desktop/static/viewer.js:321-343`, `docs/VALIDATION_LOG.md` 27행.

**U-07 (P2 · S) 오류 메시지가 내부 예외 문자열 그대로.** 현재: 서버 500이 `internal error: <ExceptionType>: <msg>`를 UI에 전달, 단계별 힌트는 빌드 5단계에만 있음. 제안: 사용자 언어 메시지 + "자세히" 펼침. 근거: `apps/desktop/server.py:477-482`, `apps/desktop/static/app.js:15-21`.

**U-08 (P2 · S) 레이어 메뉴에 trial/비교 레이어가 기본 노출.** 현재: 옵션 11개 중 다수가 미채택 trial·비교용(폴더가 있을 때만 활성). 사용자 메모는 "UI는 최소로, 테스트는 아카이브, 항공 시점이 중요". 제안: 기본은 `Sparse / Dense · verified / Model output · steady` 3개, 나머지는 "고급" 토글 뒤로. 근거: `apps/desktop/static/index.html:58-66`, MEMORY.md "Show results in app" 메모.

## 6. 서버·보안

**V-01 (P1 · S) 요청 출처 검사 없음(CSRF/DNS 리바인딩).** **[2026-11-15 상태: DONE]** Host가 이 콘솔(127.0.0.1/localhost/[::1]:포트)이 아니면 403, POST의 Origin이 로컬이 아니면 403, Content-Type이 `application/json`이 아니면 415 — `apps/desktop/server.py` `_local_request`, `tests/test_server.py::test_post_needs_local_host_local_origin_and_json`. 현재: 127.0.0.1 외 바인드는 거부하고 인증은 없음. POST는 Content-Type·Origin·Host를 보지 않고 본문을 JSON으로 파싱. 문제: 사용자가 콘솔을 켠 채 악의적 웹페이지를 열면 그 페이지의 `fetch('http://127.0.0.1:8780/api/jobs', {method:'POST', body: …})`(단순 요청, 프리플라이트 없음)가 전달되어 로컬 빌드·추적·라이브 실행을 시작하거나 취소할 수 있음(응답은 CORS로 못 읽음; 기체 제어 경로는 서버에 없음). 제안: `Origin`/`Host`가 `127.0.0.1:<port>`/`localhost`가 아니면 403, `Content-Type: application/json` 필수, 선택적으로 시작 시 생성한 토큰을 UI가 헤더로 전달. 근거: `apps/desktop/server.py:548-554,594-597`, `README.md:31`.

**V-02 (P2 · S) 서버 자체 로그 파일 없음.** 현재: 실패 요청만 stderr, 자식 작업은 `process.log`. 제안: `outputs/evidence/server/` 회전 로그. 근거: `apps/desktop/server.py:439-445`.

**V-03 (P2 · S) 캐시 사전의 무한 성장.** 현재: `_verify_cache`, `_control_seq`가 세션마다 쌓이고 비워지지 않음(규모 작음). 제안: LRU 또는 세션 종료 시 제거. 근거: `apps/desktop/server.py:102-103`.

**V-04 (P2 · S) `Content-Length` 음수 처리.** **[2026-11-15 상태: DONE]** `0 <= Content-Length <= 65536`이 아니면 400, 테스트에 `-1`·`70000`·`abc` 포함 — `tests/test_server.py`. 현재: `n <= 65536`만 검사하므로 음수가 통과해 `rfile.read(음수)`가 호출됨. **미확인**(실행 안 함): 해당 핸들러 스레드가 연결 종료까지 대기할 가능성. 제안: `0 <= n <= 65536`. 근거: `apps/desktop/server.py:551-552`.

**V-05 (P2 · S) 유지할 것(개선 아님, 기록용).** 세션 ID 정규식(선행 점 금지), 파일 allow-list, 정적 경로의 `parents` 검사는 정상 동작하며 테스트(R-09)로 묶여 있음. 근거: `apps/desktop/server.py:37,52-56,490-495`, `docs/FAILURES.md` 70행.

## 7. 드론 제어·안전

**D-01 (P1 · M, 사용자 승인 필요) 실기체 영상 수신 경로가 코드에 없음.** 현재: 어댑터는 명령(8889)·상태(8890)만 구현, 영상(11111 H.264)은 문서 표에만 있음. 제안: 기체 확보 후 첫 단계는 `command` + `streamon` 영상 수신만(모터 명령 0회, `HARDWARE_ARMED=false` 유지). `VideoSource(TelloUDP)` 어댑터를 `live_source`에 추가하고 디코더(ffmpeg/OpenCV)로 프레임화. 근거: `adapters/tello/client.py:1-6`, `docs/TELLO_SDK.md` 6-10행, `docs/ARCHITECTURE.md` 11행 모듈도.

**D-02 (P2 · S) PID 이득과 Tello rc yaw 부호 미검증.** 현재: kp 60 / ki 8 / kd 6 / 출력 40은 시뮬레이션 튜닝; 설계 문서가 "실제 검증 전 표시"를 요구. 제안: 하드웨어 없이 PX4 SITL(Gazebo)에 MOCK 제어를 연결해 부호·응답을 먼저 확인(무료 단계). 근거: `packages/control/yaw_controller.py:58-69`, `docs/ARCHITECTURE.md` 42행, `docs/DRONE_PURCHASE_AND_KOREA_RULES.md` 31행.

**D-03 (P2 · S) 루프백에서는 비행 명령도 그대로 수락.** 현재: 전송 게이트는 "비루프백 주소"만 막고, MockTello는 `takeoff`/`land`를 처리(테스트 목적). 제안: `FLIGHT_COMMANDS` 송신 횟수를 리포트에 항상 기록하고, 루프백이라도 포트 포워딩 등으로 실기체에 닿지 않도록 Mock 주소가 `MockTello`가 바인드한 소켓인지 확인하는 검사 추가. 근거: `adapters/tello/client.py:65-69`, `adapters/tello/mock_tello.py:81-89`, `adapters/tello/protocol.py:11-12`.

**D-04 (P2 · S) 지오펜스는 SIMULATED 프레임 전용, Mission Pad 앵커 미구현.** 현재: 실기체에는 위치원이 없다고 docstring이 명시; 설계의 `MissionPadPose` 인터페이스는 **미확인**(패키지에서 해당 코드를 찾지 못함). 제안: 파드 상태(`mid ≥ 1`)가 유효할 때만 `metric_pose_valid=true`로 넘기는 인터페이스를 Mock 상태 패킷으로 먼저 테스트. 근거: `packages/control/geofence.py:1-5`, `docs/ARCHITECTURE.md` 39행, `docs/TELLO_SDK.md` 12행.

**D-05 (P2 · M) 라이브 실행은 OPEN_LOOP(카메라가 돌지 않음).** 현재: 폐루프는 별도 MockTello 시나리오. 제안: 라이브 워커의 yaw를 MockTello heading에 반영해 "가상 카메라 회전"을 시뮬레이션하는 통합 시나리오. 근거: `docs/PROGRESS.md` 168행, `packages/vision/live_run.py:59`.

**D-06 (P1 · S, 사용자 승인 필요) 촬영 고지·동의 절차의 앱 내 체크리스트.** 현재: 개인정보보호법 이동형 영상기기 규칙(촬영 표시, 동의한 참가자만, 로컬 보관)이 문서에만 있음. 제안: 실기체 영상 수신 시작 전 UI에서 체크리스트 확인을 요구하고 리포트에 기록. 근거: `docs/DRONE_PURCHASE_AND_KOREA_RULES.md` 105행·112-121행.

## 8. 코드 구조·유지보수

**C-01 (P1 · M) 대형 단일 함수.** **[2026-11-15 상태: PARTIAL]** `run_live`를 `_LiveRun` 클래스(열기·루프·처리·마무리·요약 메서드)로 분리, 같은 입력 60 프레임에서 리팩터 전후 출력 동일(`outputs/evidence/portfolio/c01_equivalence.json`). `build_sparse_map`, `dense_cpu.py` CLI 정리는 하지 않음. 현재: `run_live` 약 490줄(272-762행)에 중첩 클로저와 `nonlocal` 10여 개; `build_sparse_map` 약 290줄(152-442행); `dense_cpu.py` 898줄에 CLI 플래그 14개. 제안: 단계(열기·루프·마무리·리포트)별 함수/클래스 분리, 설정은 dataclass로. 근거: `packages/vision/live_run.py:272-762,396-397`, `packages/mapping/sparse_pipeline.py:152-442`, `packages/mapping/dense_cpu.py:858-879`.

**C-02 (P1 · S) `dense_cpu.py` 모듈 설명이 대체된 방식을 설명.** **[2026-11-15 상태: DONE]** `dense_cpu.py` 모듈 설명을 실제 방식(평면 스윕)으로 고치고 쓰지 않는 SGBM 코드 삭제. 현재: docstring(1-16행)은 정류 스테레오 SGBM(D2)을 기본처럼 쓰지만 실제 기본은 평면 스윕이고 리포트에 "deviation_from_plan"으로 기록됨; `_sgbm`/`stereo_pair`는 남아 있음. 제안: docstring 갱신, 미사용 코드는 삭제하거나 `legacy_` 접두사와 테스트. 근거: `packages/mapping/dense_cpu.py:1-16,101-135,650-651`, `docs/FAILURES.md` F-09.

**C-03 (P2 · S) 한 줄 여러 문장과 함수 내부 import.** 현재: `model_on_path.py`에 세미콜론으로 이어 쓴 줄이 다수(42-43, 61, 72-73, 116, 128, 132, 146-148, 170, 175, 181-182, 198, 222, 231-233, 239-244, 255-256, 262, 269-275행), `cKDTree`를 함수 안에서 세 번 import(84, 254, 266행), `warnings.simplefilter("ignore", RuntimeWarning)`를 함수 안에서 전역 설정(100-101행). 제안: 포매터(ruff format)와 린트 규칙을 저장소에 추가. 근거: 위 줄 번호, `repo_survey.txt` 146-171행(`# noqa: E731` 람다 10곳).

**C-04 (P2 · S) 매직 넘버가 리포트에 남지 않음.** 현재: `model_on_path` CONF 1.5, 밴드당 ×1.6, 근접 40 %, 60 px, 12 %, k=8 등이 코드에 박혀 있고 리포트 `config`는 `{"work_width": 640}` 고정. 반면 `VOParams`, `SelectionConfig`, `DenseConfig`는 dataclass. 제안: `OnPathConfig` dataclass와 `asdict`를 리포트에 기록. 근거: `packages/learned/model_on_path.py:35,79,117-130,275`.

**C-05 (P2 · S) 모델 rank 고정.** 현재: `model_on_path`는 `sparse/0`만 읽음; `dense_cpu`는 `--model` 옵션이 있음. 제안: 일관되게 `--model`. 근거: `packages/learned/model_on_path.py:160`, `packages/mapping/dense_cpu.py:861`.

**C-06 (P2 · S) 죽은 분기.** 현재: 조건이 `sel.dropped_by_cap`일 때만 들어가는 블록 안에 `if sel.dropped_by_cap else …`가 있어 else는 도달 불가. 근거: `packages/mapping/sparse_pipeline.py:243-249`.

**C-07 (P2 · S) 공유 기본 인스턴스.** **[2026-11-15 상태: DONE]** `MultiTracker(cfg: TrackerConfig | None = None)`. 현재: `MultiTracker.__init__(cfg=TrackerConfig())` — 변경하지 않아 지금은 무해하나 함정. 제안: `cfg: TrackerConfig | None = None`. 근거: `packages/tracking/tracker.py:118`.

**C-08 (P2 · S) 넓은 예외 처리의 조용한 실패.** 현재: 13곳 대부분은 이유가 주석에 있음(좋음). 그러나 macOS 전용 `memory_pressure`/`sysctl` 실패를 `None`으로 삼켜 다른 OS에서 메모리 가드가 꺼진 채 돌 수 있음. 제안: 가드 비활성화를 로그·리포트에 명시. 근거: `packages/learned/chunked_run.py:28-41`, `packages/learned/run_lingbot_map.py:38-44`, `repo_survey.txt` 146-171행.

**C-09 (P2 · S) 이미지 폴더 정렬 키 타입 혼합.** 현재: 파일 이름이 숫자/비숫자 섞이면 `float`와 `str`를 비교해 TypeError 가능. **미확인**(실행 안 함). 근거: `packages/vision/vo.py:430-431`.

**C-10 (P2 · S) 실험 전용 모듈이 제품 패키지에 섞여 있음.** 현재: 테스트가 언급하지 않는 소스 23개 중 다수가 비교·진단·일회성 실험(`compare_mapper`, `diagnose*`, `mask_people_experiment`, `openmvs_compare`, `*_eval`, `port_zip_demo_mac`, `compare_zip_demo`, `zip_style_cloud` 등). 제안: `packages/experiments/` 또는 `tools/`로 이동하고 README에서 구분. 근거: `repo_survey.txt` 196-219행.

**C-11 (P2 · S) 로깅이 `print` 기반.** 현재: 워커·CLI가 `print(..., flush=True)`로 상태 출력; 표준 `logging` 미사용(**미확인**: 전수). 제안: 모듈별 logger + 파일 핸들러. 근거: `packages/vision/live_run.py:761`, `packages/learned/model_on_path.py:277`.

**C-12 (P2 · S) 린트·타입 검사 설정 없음.** **[2026-11-15 상태: PARTIAL]** `ruff.toml`(E4·E7·E9·F·W) 추가, 전체 통과(`outputs/evidence/portfolio/ruff.log`). mypy는 추가하지 않음. 현재: 설정 파일은 `pytest.ini`, `requirements.txt`뿐(`pyproject.toml`, ruff/mypy 설정 없음). 제안: `pyproject.toml`에 ruff + mypy(점진) 설정, CI에 연결. 근거: `repo_survey.txt` 221-255행, `pytest.ini:1-4`.

## 9. 테스트·CI

**Q-01 (P0 · S) CI 없음.** **[2026-11-15 상태: PARTIAL]** `.github/workflows/ci.yml`(fast: ruff + `-m "not slow"`, full: 가중치·클립 SHA-256 확인 후 전체) 작성, 로컬 동등 실행은 통과(`outputs/evidence/portfolio/ruff.log`, `pytest-fast.log` 170 passed / 5 deselected). **GitHub에서는 아직 실행 안 함(NOT_RUN)**. 현재: `.github/` 디렉터리 없음, 테스트는 `scripts/test_all.sh` 수동 실행. 제안: GitHub Actions — macOS 러너 pytest(`-m "not slow"` 빠른 잡 + 전체 잡), ruff; 가중치·클립 필요 테스트는 다운로드 단계 포함 또는 skip 유지. 근거: Glob `.github/**` 결과 없음, `scripts/test_all.sh:1-12`.

**Q-02 (P0 · M) 간헐 실패 테스트.** **[2026-11-15 상태: DONE]** 원인 3건 수정: 빌드·라이브 자식 프로세스가 최종 상태를 쓴 뒤 5–23 ms 더 살아 있어 서버가 409/202를 잘못 판단(`q02_race_probe_*.jsonl`), 테스트 보조 클래스의 음수 sleep. 수정 전 기준선 1 failed / 158 passed(`pytest_baseline_before.log`), 수정 후 5회 연속 175 passed / 0 failed(`outputs/evidence/portfolio/pytest-5x-summary.txt`). 현재: `test_live_run_lifecycle_control_and_integrity`는 실제 워커 프로세스를 원속도로 돌리며 60 s 폴링 — 부하에 민감하고, 첫 실행 실패 로그는 보관되지 않음(요약 한 줄만). 제안: `--rate max` + 짧은 클립, 결정적 `ScriptedSource` 사용, 폴링 타임아웃·대기 간격을 상수화, 실패 시 전체 `-v` 로그 보관. 근거: `tests/test_server.py:203-242`, `tests/test_live_run.py:85-120`, `outputs/evidence/tests/pytest-20261109-follow.log`.

**Q-03 (P1 · S) 조용한 skip.** **[2026-11-15 상태: PARTIAL]** `scripts/ci_summary.py`가 JUnit XML에서 skip 수를 출력, CI full 잡이 가중치·클립을 내려받음; 로컬 5회 실행은 0 skipped. CI 자체는 NOT_RUN. 현재: 가중치·`vtest.avi`가 없으면 `skipif`로 건너뛰어 새 클론에서는 134개보다 적게 돌아도 "passed"로 보임. 제안: CI에서 `scripts/fetch_models.sh`와 클립 다운로드를 수행하고, 로그에 skip 수를 항상 기록. 근거: `tests/test_live_run.py:25,356,394`, `tests/test_vision.py:19-20`, `tests/test_server.py:170`.

**Q-04 (P1 · S) UI 스모크 재현성.** **[2026-11-15 상태: DONE]** 합성 방 픽스처 `tests/fixtures/maps/fixture_synthetic_room`(git 포함), `--attach` / `--browser` / `CHROME_PATH` 순서, 빠진 픽스처는 이유와 함께 SKIP으로 기록 — `outputs/evidence/portfolio/ui_smoke_final_opus.log`(117 passed / 0 failed / 4 skipped). 현재: `--attach` 없이는 `/Applications/Dia.app` 경로의 브라우저를 헤드리스로 새로 띄움(사용자 환경과 불일치); 픽스처 세션 이름(`mq_bangkok_60s_after_mf300`, `mqg_sumida_0025_0125`, `tum_fr1_desk_K4`, …)이 gitignore된 `outputs/`에 의존. 제안: 기본 동작을 "9222 attach 시도 → 실패 시 `CHROME_PATH` 환경변수의 브라우저" 순으로, 소형 픽스처(합성 방 세션)를 `tests/fixtures/`에 포함. 근거: `tests/ui/ui_smoke.mjs:16,19-20,29-35,45-55`.

**Q-05 (P1 · M) 테스트 공백.** **[2026-11-15 상태: PARTIAL]** `tests/test_display_filters.py`(points_filter, clean_cloud, far_cleanup 6개), `tests/test_eval_harness.py`(dense_eval, static_eval 5개) 추가. model_on_path·learned_fuse·local_view 테스트는 이번에 추가하지 않음. 현재: 테스트가 언급하지 않는 소스 23개. 그중 앱 레이어를 실제로 만드는 `model_on_path.py`, 학습 깊이 융합 `learned_fuse.py`, 표시 필터 `points_filter.py`/`clean_cloud.py`/`far_cleanup.py`/`local_view.py`, 판정 하네스 `dense_eval.py`/`static_eval.py`는 무테스트. 제안: 합성 장면(`sim.synthetic_scene`)으로 단위 테스트. 근거: `repo_survey.txt` 196-219행, `tests/conftest.py:12-15`.

**Q-06 (P2 · S) `slow` 마커가 기본 실행에서 제외되지 않음.** **[2026-11-15 상태: DONE]** CI fast 잡과 로컬 빠른 실행이 `-m "not slow"` 사용 — `outputs/evidence/portfolio/pytest-fast.log`(170 passed, 5 deselected). 현재: 마커는 정의됐으나 `addopts` 없음 → 전체 87 s. 제안: 개발용 `-m "not slow"` 안내 또는 `scripts/test_fast.sh`. 근거: `pytest.ini:1-4`, `tests/test_pipeline.py:18,66,77`.

**Q-07 (P2 · S) 지도 수치 스냅샷 회귀 없음.** 현재: 품질 판정 함수는 테스트되지만 고정 세션의 `map_report.json`/`dense_report.json` 수치가 바뀌었는지 확인하는 테스트는 없음(**미확인**: `tests/test_quality.py` 전체 범위). 제안: 합성 방 세션의 등록 수·점 수·재투영을 허용 오차와 함께 고정. 근거: `tests/test_quality.py:11`, `repo_survey.txt` 173-194행.

**Q-08 (P2 · S) 테스트 로그 보관 형식 불일치.** 현재: `test_all.sh`는 전체 `-v` 로그를 저장하지만 일부 로그는 수동 작성한 요약 한 줄. 제안: 항상 `scripts/test_all.sh` 경유, 로그 파일명에 커밋 해시 포함. 근거: `outputs/evidence/tests/pytest-20261109-follow.log`(1줄), `scripts/test_all.sh:7-8`.

## 10. 재현성·배포 (라즈베리파이 포함)

**R-01 (P1 · S) 의존성 기록 불완전.** **[2026-11-15 상태: PARTIAL]** `requirements-dev.txt`(ruff 0.17.5, yt-dlp 2026.10.21). 학습 모델 환경 재현 절차는 여전히 없음. 현재: `requirements.txt`는 5개만 고정; `yt-dlp`는 `.venv`에 설치됐다고 로그에만 있음; 학습 환경(`outputs/tools/venv_learned`, torch)은 저장소에 재현 절차가 없음. 제안: `requirements-dev.txt`(yt-dlp), `requirements-learned.txt`와 설치 스크립트(별도 venv, 모델 SHA-256 검증), 가능하면 해시 고정. 근거: `requirements.txt:1-6`, `docs/VALIDATION_LOG.md` 9행, `README.md:472`, `repo_survey.txt` 293행(deps 절 비어 있음).

**R-02 (P1 · S) 저장소 루트의 대용량 비추적 파일.** **[2026-11-15 상태: PARTIAL]** `.gitignore`에 루트 `/*.mp4`, `/*.zip` 추가. 루트의 대용량 파일 자체는 옮기지 않음. 현재: 루트에 9.3 MB 영상 1개와 8.0 GB zip 1개가 있고 `.gitignore`에 루트 `*.mp4`/`*.zip` 패턴이 없음 → 실수 커밋 위험(GitHub 100 MB 제한). 제안: 영상은 `assets/input/`, zip은 `outputs/tools/`로 이동, 루트 `*.zip`/`*.mp4` ignore. 근거: `repo_survey.txt` 246-247행, `.gitignore:1-27`.

**R-03 (P1 · M) 라즈베리파이 실측 없음.** 현재: `rpi_feasibility.json`은 PyPI 휠 확인(pycolmap aarch64 0개)과 Mac 측정값 환산 추정(5–8배 느림 가정)뿐이며 NOT_RUN; 학습 깊이는 "불가". 제안: Pi 5에서 (1) YOLOX-nano `cv2.dnn` 검출+추적 fps·메모리, (2) pycolmap 소스 빌드 가능 여부, (3) 브라우저 뷰어 3M점 프레임 시간을 실측해 추정치를 교체. 역할 분담(Pi 촬영·추적·MOCK 제어, Mac 지도)은 유지. 근거: `outputs/evidence/rpi/rpi_feasibility.json`, `docs/SYSTEM_STATUS.md` 9절, `docs/PROGRESS.md` 232행.

**R-04 (P1 · S) 단일 환경 검증과 macOS 전용 호출.** 현재: macOS 27.2 / Apple M6 / 16 GiB에서만 검증, 다른 OS는 NOT_RUN. `memory_pressure`, `sysctl vm.swapusage`, `system_profiler`, MPS는 macOS 전용. 제안: OS 분기와 Linux CI 잡(지도·추적 핵심만). 근거: `README.md:35`, `packages/learned/chunked_run.py:30,38`, `docs/PROGRESS.md` 160행(LV-T6 `system_profiler`).

**R-05 (P2 · S) `LICENSE` 파일 없음.** 현재: README가 "미정"이라고 명시. 제안: 라이선스 결정(OpenMVS AGPL은 별도 프로세스 유지, LingBot-Map·YOLOX Apache-2.0, 분할 모델 MIT와 호환되는 선택). 근거: `README.md:497`, `docs/SYSTEM_STATUS.md` 78행.

**R-06 (P2 · S) 개인 환경 설정 스크립트가 제품 스크립트와 같은 폴더.** 현재: `scripts/`에 에이전트 계정·설치 관련 zsh 5개가 빌드·데모 스크립트와 섞여 있음. **미확인**: 커밋 여부. 제안: `tools/dev/`로 분리하고 README 문서 목록에서 구분. 근거: Glob `scripts/*` 결과.

**R-07 (P2 · S) `outputs/` 35 GB, 정리 정책 없음.** 현재: archive 8.1 GB, experiments 11 GB, tools 14 GB; 증거는 216 MB. 제안: `outputs/archive/manifest.json` 기반 정리 스크립트, 증거 폴더만 별도 백업 대상으로 지정. 근거: `repo_survey.txt` 295-305행.

**R-08 (P2 · S) 실행 인자 미보관.** 현재: `learned_fuse`/`far_cleanup`/`static_eval`의 정확한 셸 한 줄과 "우리 쪽" 융합 시간이 보관되지 않음; `sparse_pipeline`은 `commands`를 리포트에 기록함. 제안: 모든 CLI가 `sys.argv`와 벽시계를 리포트 JSON에 기록. 근거: `docs/SYSTEM_STATUS.md` 77행, `docs/VALIDATION_LOG.md` 137-138행, `packages/mapping/sparse_pipeline.py:192`.

**R-09 (P2 · S) 포트 고정.** 현재: 8765 충돌로 8780 기본; 충돌 시 수동 `--port`. 제안: `--port 0`(자동) 옵션과 콘솔 URL 출력. 근거: `docs/FAILURES.md` F-02(35-39행), `apps/desktop/server.py:610,617-619`.

**R-10 (P2 · S) 네트워크 볼륨의 AppleDouble 파일.** 현재: `._*` 파일이 루트·`scripts/`에 다수(`.gitignore`에 `._*`는 있음). 제안: `dot_clean` 주기 실행 또는 로컬 디스크 작업 트리. 근거: `repo_survey.txt` 224-231행, Glob `scripts/*` 결과.

## 11. 문서

**W-01 (P1 · S) README 표 한 셀이 약 1,300자.** **[2026-11-15 상태: DONE]** README 재작성(긴 셀 제거) — `README.md`. 현재: "검증된 밀집 점군" 행 한 셀에 실험 5건의 이력이 들어 있음. 제안: 셀은 2–3문장 + 절 링크, 이력은 각 계획의 Result 절로. 근거: `README.md:14`, `repo_survey.txt` 74행.

**W-02 (P1 · S) README 117 KB / 497행.** **[2026-11-15 상태: DONE]** README 140행, 이전 README 전체는 `docs/EXPERIMENTS.md`로 보존. 현재: 빠른 시작과 실험 기록이 섞여 처음 보는 사람이 길을 잃음. 제안: README는 개요·안전 원칙·빠른 시작·문서 지도(≤ 150행)로 줄이고, 실험 절은 `docs/EXPERIMENTS_*.md`로 이동. 근거: `repo_survey.txt` 252·290행.

**W-03 (P1 · S) 문서 간 중복.** 현재: 같은 결과(예: E4 수치)가 `README.md:178`, `docs/PROGRESS.md:68`, `docs/VALIDATION_LOG.md:73`, `docs/PLAN_E4_APPEARANCE_CUE.md:41`에 4회; `docs/PROGRESS.md` 3행 머리말이 한 단락 약 2,900자. 제안: 단일 출처 원칙 — 수치는 VALIDATION_LOG, 서사는 PROGRESS, 나머지는 링크. 근거: 위 줄 번호.

**W-04 (P1 · S) 초기 설계 문서가 현재와 불일치.** **[2026-11-15 상태: DONE]** 초기 설계 문서 6개 맨 위에 "초기 설계(2026-11-08); 현재 상태는 docs/SYSTEM_STATUS.md" 배너, 본문 불변. 현재: `IMPLEMENTATION_PLAN.md:5` "M4 Pro"(실측 M6 16 GiB), `VERIFICATION_CONTRACT.md:22` "Depth Anything 실험으로 대체"(실제는 다른 학습 깊이 모델 사용), `CAMERA_ONLY_EXECUTION_PLAN.md:15` "M4 Pro"와 stella_vslam/ORB-SLAM3/Depth Anything V2 경로(미시도), `RECOVERY_MATRIX.md` ORB-SLAM3 행, `VISUAL_MAPPING.md:17,25` ORB-SLAM3/EuRoC 권장, `ARCHITECTURE.md:53-55` "Python 3.11+", Streamlit/FastAPI/PySide6(실제 stdlib HTTP), 11-22행 모듈도의 PIR/ESP32/MQTT/Alarm(미구현). 제안: 각 문서 머리에 "2026-11-08 초기 설계; 현재 상태는 `docs/SYSTEM_STATUS.md`" 배너를 달거나 `docs/archive/`로 이동. 근거: `docs/ENV_AUDIT.md:15`, 위 줄 번호.

**W-05 (P2 · S) 언어 혼용.** 현재: README·SYSTEM_STATUS·CLAUDE는 한국어, PROGRESS·VALIDATION_LOG·FAILURES·계획 문서는 영어. 제안: 독자를 정한 뒤 최소한 각 문서 첫 단락 요약을 양쪽 언어로. 근거: `repo_survey.txt` 257-291행 문서 목록.

**W-06 (P2 · S) 수치 변동의 설명 공백.** **[2026-11-15 상태: PARTIAL]** UI 체크 수 변동은 U-01에서 설명. 표 수치 자동 생성 스크립트는 없음. 현재: UI 체크 수 88 → 71 → 75 변동 미설명; 설치된 D12 레이어 사유 "+0.5 pp"는 "+0.42 pp"로 정정됐지만 아카이브된 UI 캡션은 옛 문구. 제안: 표 수치를 증거 JSON에서 생성하는 스크립트로 자동화. 근거: `docs/SYSTEM_STATUS.md` 10절, `docs/FAILURES.md` F-24(300-304행).

**W-07 (P2 · S) 용어집 없음.** 현재: MQ, G, D, LV, DC, SM, FD, LD, SL, M0, B1, S3r40, ESVFN2x25, D12L, F1b 같은 약어가 README·PROGRESS에 설명 없이 쓰임. 제안: `docs/GLOSSARY.md`(약어 → 한 줄 설명 → 계획 문서 링크). 근거: `README.md:14`, `docs/PROGRESS.md:3`.

**W-08 (P2 · S) 사용자용 촬영 가이드 부재.** 현재: "어떻게 찍어야 GOOD이 나오는지"가 UI 힌트와 PROGRESS 명령 설명에 흩어짐. 제안: 한 페이지 가이드(정적 장면, 옆 이동, 회전만 금지, 30–90 s, 보정 파일). 근거: `apps/desktop/static/app.js:15-21`, `docs/PROGRESS.md` 190-199행.

**W-09 (P2 · S) 한국 규정 문서의 2차 출처 의존.** 현재: 출처 7, 9, 10, 11, 37이 블로그·기사. 제안: 처리 기간·과태료·교육 시간은 법령 원문·드론원스톱·한국교통안전공단에서 재확인한 날짜를 표에 추가. 근거: `docs/DRONE_PURCHASE_AND_KOREA_RULES.md` 165행.

---

## 12. 이미 시도했으나 미채택 (다시 시도하지 않도록)

각 줄은 "무엇을 / 왜 실패했나 / 근거"입니다. 재시도하려면 해당 계획의 고정 조항을 바꾸는 새 날짜 절이 먼저 필요합니다.

| 시도 | 결과·원인 | 근거 |
|---|---|---|
| 정류 두 뷰 SGBM 밀집(계획 D2) | 전진 이동에서 에피폴이 화면 안에 들어와 시차 없음 → 평면 스윕으로 교체 | `docs/FAILURES.md` F-09 |
| PatchMatch 밀집(pycolmap) | CUDA/HIP 전용, 이 Mac에서 BLOCKED, 재시도 없음 | `outputs/evidence/dense/patch_match_probe.txt`, F-09 |
| Poisson 메시 | 방사형 줄무늬·바닥 없음, 시각 검사 FAIL → 콘솔 레이어 아님 | F-09 |
| ESVF(바닥 커버리지) | `f000900` 바닥 +0.7 pp(조항 +8 pp) — 이웃 선택이 원인 | F-12 |
| E-N(`median_or_near`) | 구조물 −12 pp, 자유공간 위반 +14 pp | F-13 |
| ESVFN2x25(`dense_experimental`) | 전체 클립·FSC 전 기준 PASS이나 사전 등록 seg30 선별에서 `float_fs` +11.3 pp FAIL → NOT_ADOPTED, 채택은 사용자 결정 | F-14, `docs/PLAN_DENSE_COVERAGE.md` 156행 |
| B2 정적 지도(`dense_static`, 마스크로 창 침식) | 하늘 경계 구조물 −8.7~−11.9 pp, 벽 −18.9 pp | F-16 |
| S3r40(`dense_static_v2`) | B2보다 좋지만 B1 대비 구조물·벽·띠 손실, `sky_false` 1.4–6.8 %(한도 0.5 %), 221 s > 203 s | F-20 |
| OpenMVS 기본 해상도(800 px) | `DensifyPointCloud` 시그널 −11(segfault); 640 px는 바닥 좋으나 구조물 0.118 | F-21 |
| F1 적응형 선별(시차·흐름만) | 92 프레임만 남아 밀집 이웃 부족 | F-22 |
| F1b 적응형 선별 | 경로 조항 PASS(세 클립), 밀집 오류 조항 FAIL → `--selection adaptive` 옵션으로만 유지 | F-23 |
| D1 먼 이웃 규칙 | 하늘 +0.57 pp, 사람 점 850 → 877 | F-24 |
| D12(D1 + 반해상도 바닥) | 시간 400.0 s > 332 s, 사람 점 850 → 888; 나머지 조항 PASS | F-24, F-25 |
| 학습 깊이 검증 융합(`dense_learned`) | 스미다 30·52 s 바닥 +5 pp 조항(채택 청크 없음), FSC `f000320` +4.1 pp(이미 0.94) | F-32 |
| FSC 먼 점 정리 | held-out 구조물 −2.0 pp → FSC에 미적용 | F-31 |
| 근접점 스케일로 청크 재조정 | 빠른 회전 청크가 더 나빠짐 → 진단값으로만 유지 | F-30 |
| 전체 클립 한 번에 학습 모델 실행 | 저메모리로 세션이 중단 → 청크 재개 프로토콜 | F-27 |
| 스미다 50–58·56–60 s 청크 | 정렬 잔차 6.1/5.0 % > 2 % → 기각 | F-28 |
| 일부 청크 합치기 | 겹침 깊이 차 5.2–12 %, 삼각측량 스케일 1.18/2.22 → 보류(설계) | F-29 |
| "Mirage" 계열 모델 | 라이다+IMU 또는 CUDA/비상업 가중치 → no-go | F-26 |
| `clean_cloud` 병합 옵션 | 스미다 바닥 0.64 → 0.05로 얇아짐 → 기본 off | `docs/PROGRESS.md` 209행 |
| 근처 거리 컷 레이어(`dense_static_local`) | 30 s 구조물 0.88 → 0.05 — 비교 전용, 지도 후보 아님 | `docs/PROGRESS.md` 148행 |
| E2 fix_v2(엄격 게이트만) | 1.5 s 가림 재획득 44 %로 하락, 실제 클립 id 폭증 → fix_v3(픽셀 게이트 추가)로 대체 | F-05 |
| E4 옷 색 재식별 | FPR 1 %에서 TPR 0.154 / 0.493(기준 0.60) → NO_GO, STOP 정책 유지 | `outputs/evidence/e4_appearance/e4_appearance.json`, `docs/VALIDATION_LOG.md` 73행 |
| 사람 마스킹 SfM(위트레흐트) | 점프 43 → 38이지만 스미다/FSC 가드 미실행 → 미채택 | F-11 |
| 구간별 진단 감지기(계획 B) | FSC 드리프트를 분리하지 못함 → 가드로 쓰지 않음 | F-08 |

---

## 미확인으로 남긴 것

- S-02: 사용자 2D 라이다 기종·인터페이스.
- U-06: 실제 태블릿·터치 동작.
- V-04: 음수 `Content-Length` 시 핸들러 동작(실행 안 함).
- D-04: `MissionPadPose` 인터페이스 코드 유무(패키지에서 찾지 못함).
- C-09: 이미지 폴더 정렬 키 혼합 시 실제 예외 발생 여부.
- C-11: `print` 기반 로깅의 전수 범위.
- Q-07: `tests/test_quality.py`의 전체 범위.
- R-06: 개인 환경 스크립트의 커밋 여부.
- 이 문서는 어떤 명령도 새로 실행하지 않았으므로, 성능 수치는 모두 기존 증거 파일의 값이며 새 측정값은 없습니다.

## 근거로 읽은 파일

`outputs/evidence/improvements/repo_survey.txt`; `docs/{SYSTEM_STATUS,FAILURES,PROGRESS,VALIDATION_LOG,PLAN_LEARNED_DEPTH,PLAN_DENSE_COVERAGE,RECOVERY_MATRIX,VERIFICATION_CONTRACT,CAMERA_ONLY_EXECUTION_PLAN,DRONE_PURCHASE_AND_KOREA_RULES,ENV_AUDIT,ARCHITECTURE,TELLO_SDK,VISUAL_MAPPING,IMPLEMENTATION_PLAN,SOURCES_AND_ASSUMPTIONS,PHASES_AND_ACCEPTANCE,MODEL_ROUTING}.md`; `README.md`; `apps/desktop/server.py`; `apps/desktop/static/{app.js,viewer.js,index.html}`(+ `live.js`, `tracking.js`, `app.css` grep); `packages/mapping/{dense_cpu,sparse_pipeline,frame_selection}.py`; `packages/vision/{live_run,vo,person_detector,live_source}.py`; `packages/learned/{model_on_path,chunked_run,run_lingbot_map}.py`; `packages/tracking/tracker.py`; `packages/control/{yaw_controller,geofence}.py`; `adapters/tello/{client,mock_tello,protocol}.py`; `tests/{conftest,test_live_run,test_server}.py`, `tests/ui/ui_smoke.mjs`; `scripts/{test_all,run_demo,build_sparse_map,run_tracking}.sh`; `requirements.txt`, `.gitignore`, `pytest.ini`; `outputs/evidence/rpi/rpi_feasibility.json`, `outputs/evidence/learned_depth/steady_layer_v2.json`, `outputs/evidence/tests/pytest-20261109-follow.log`, `outputs/evidence/ui/ui_smoke.json`(머리 부분).
