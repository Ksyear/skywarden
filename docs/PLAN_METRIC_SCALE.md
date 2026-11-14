# 계획: 라이다 없이, 추가 비용 거의 0원으로 지도에 실제 미터 크기 입히기

상태: PLAN (2026-11-14). 작성: fable-planner (claude-fable-5-1). 구현·실행·측정은 Opus 5.5가 맡고, 결과 문서는 fable-documenter가 증거 파일을 보고 갱신합니다. 이 문서는 어떤 명령도 실행하지 않았으며, 아래 수치는 모두 **사전에 정한 합격 기준**이거나 저장소 코드·문서에서 읽은 값입니다. 측정 결과는 하나도 없습니다.

결과: 실행 결과와 계획과 달라진 점은 맨 끝 **10절**에 있습니다(2026-11-15 추가, 작성: Opus (Fable 한도로 대체, 사용자 지시)). 1–9절과 부록은 계획 당시 그대로입니다.

범위: 희소 지도(`outputs/maps/<session>/sparse/0`)와 그 포즈 위의 모든 레이어는 지금 **상대 스케일**입니다. 이 계획은 지도 좌표 1단위가 몇 미터인지를 **측정으로** 정하고, 그 값을 지도 파일에 굽지 않고 별도 파일(`scale.json`)에 두어 뷰어와 거리 도구가 읽게 합니다. 2D 라이다안은 폐기합니다. 미터 스케일이 붙어도 지도는 여전히 `NOT_NAVIGATION_SAFE`이며 충돌 회피·실비행 판단에 쓰지 않습니다.

---

## 0. 한눈에 보기

| 순위 | 방법 | 비용 | 역할 | 필요한 것 |
|---|---|---|---|---|
| 1 | **인쇄한 ArUco 마커**(A4 한 장, 변 길이를 자로 측정) | 0원(종이·잉크) | **기본 방법.** 프레임에서 모서리 검출 → 우리 SfM 포즈로 삼각측량 → `실제 변 길이 / 지도상 변 길이` | 사용자가 마커를 놓고 찍은 새 영상 |
| 2 | **줄자로 잰 두 점**(뷰어에서 점 두 개 클릭 + 실제 거리 입력) | 0원 | 마커가 없는 기존 영상(복도 등)의 대체 방법, 그리고 방법 1의 **독립 검증** 입력 | 줄자, 또는 문 폭·A4·보도블록처럼 크기를 아는 물체 |
| 3 | **Tello 텔레메트리**(`tof`·`h`로 카메라 높이) | 0원(기체가 생기면) | 교차 확인용. 거칠어서 주 근거로 쓰지 않음 | MOCK에서만 구현·검증. 실기체는 **사용자 승인 필요** |
| 4 | **미터 깊이를 내는 모델**(MapAnything, Depth Anything 3 metric, π³-X) | 0원(로컬 실행) | 참고용. 1·2와의 **불일치 표시**에만 사용. 스케일의 주 근거로 쓰지 않음 | Mac(MPS) 동작 미확인 → 별도 시험(부록 A) |

폐기·보류: 2D 라이다(폐기), IMU 직접 융합(추가 하드웨어·시각 동기화 필요, 폐기), 휴대폰 ARKit 기반 미터 궤적(아이폰 전제라 **선택 사항**으로만 언급, 이 계획에서 구현하지 않음).

---

## 1. 지금 코드에서 확인한 것 (계획의 전제)

- `packages/mapping/export.py`: `trajectory.json`에 `"scale": "RELATIVE"`, `camera.params_refined`(COLMAP이 정제한 내부 파라미터), 포즈는 `T_world_camera`. PLY 헤더 주석도 `RELATIVE scale`. → 이 파일들은 **바꾸지 않습니다.**
- `packages/mapping/sparse_pipeline.py`: 선별 프레임은 `frames/f%06d.jpg`(긴 변 최대 1600 px, `SelectionConfig.max_long_side`)이고 COLMAP 내부 파라미터는 **이 축소된 프레임 기준**입니다. 마커 모서리 검출도 반드시 이 프레임에서 해야 내부 파라미터와 맞습니다. 보정 파일이 없으면 `SIMPLE_RADIAL`을 COLMAP이 추정(`UNCALIBRATED_ESTIMATED`). `map_report.json`의 `labels.scale`은 `RELATIVE`. 가장 큰 모델은 `sparse/<idx>`(`reconstruction.chosen_model_dir`)와 `models/0/`에, 다른 모델은 **별도 좌표계**.
- `packages/mapping/labels.py`: `Scale.METRIC_ANCHORED`가 enum에 이미 있지만 아무 곳에서도 쓰이지 않음.
- `apps/desktop/static/viewer.js`: `onPick`은 **overview 지도 클릭 → 가장 가까운 카메라 인덱스**를 돌려주는 콜백입니다(`bindOverview`, 317행). 3D 점을 클릭으로 고르는 기능은 **없습니다.** 다만 `render()`가 모든 점을 화면에 투영하는 루프(254–269행)와 z-버퍼를 이미 가지고 있어, "클릭 위치에서 반경 안의 가장 가까운(z가 작은) 점"을 고르는 코드는 같은 투영식으로 추가할 수 있습니다.
- `apps/desktop/static/app.js` `renderLabels()`: 헤더 배지 `SCALE RELATIVE`, 좌하단 칩 `Relative units · scale unknown`이 고정 문구. 스케일 상태 표시는 이 두 곳만 바꾸면 됩니다.
- `apps/desktop/server.py`: 세션 파일은 allow-list `SESSION_FILES`로만 내려줌 → `scale.json`을 목록에 추가해야 함. POST는 `/api/jobs`, `/api/track-jobs`, `/api/runs/<id>/control` 등 패턴 → `/api/sessions/<id>/scale` 추가 가능.
- `adapters/tello/protocol.py`: 상태 문자열에서 `tof`, `h`는 정수(SDK 문서상 cm), `baro`는 실수, `vgx/vgy/vgz` 정수로 파싱됨. `adapters/tello/mock_tello.py`: 이륙 시 `h=80`, `tof=10` 고정, 상태 10 Hz. 실기체 영상 수신 경로는 코드에 없음(`docs/IMPROVEMENTS.md` D-01).
- `packages/sim/synthetic_scene.py`: 평면 5개(바닥·천장·벽)에 랜덤 질감을 `_PX_PER_UNIT = 170` px/단위로 입혀 레이캐스트. 정확한 `PINHOLE` K와 정답 궤적을 씀. 질감 이미지에 마커 그림을 **알려진 위치·크기**로 붙이면 변 길이를 월드 단위로 정확히 아는 마커 장면이 됩니다.
- `requirements.txt`: `opencv-python-headless==5.0.0.93`. 이 빌드에 `cv2.aruco`가 들어 있는지는 **실행해 봐야 알 수 있음**(0단계 확인 항목). 없으면 같은 버전의 contrib 패키지 또는 AprilTag 파이썬 패키지로 대체(둘 다 무료).

---

## 2. 스케일 파일 구조와 라벨

### 2.1 `outputs/maps/<session>/scale.json` (schema `skywarden/scale@1`)

지도 파일(PLY·`trajectory.json`·`map_report.json`)은 그대로 두고, 스케일은 이 파일 하나에만 둡니다. 뷰어·거리 도구·보고서는 이 파일을 읽어 미터로 환산합니다. 지도를 다시 만들면 `map_hash`가 달라져 **자동으로 무효**가 되어야 합니다.

```json
{
 "schema": "skywarden/scale@1",
 "session_id": "my_room_01",
 "model_rank": 0,
 "colmap_dir": "sparse/0",
 "map_hash": "<trajectory.json의 SHA-256>",
 "status": "MEASURED_VERIFIED | MEASURED_UNVERIFIED | MODEL_ESTIMATED | NONE",
 "metres_per_unit": 0.0,
 "uncertainty": {"rel_std": null, "n_observations": 0, "spread_between_methods_rel": null},
 "primary_method": "ARUCO_MARKER | TWO_POINT_TAPE | TELLO_TELEMETRY | METRIC_DEPTH_MODEL | null",
 "observations": [
   {"method": "ARUCO_MARKER", "marker_id": 0, "dictionary": "DICT_4X4_50", "side_m": 0.160,
    "frames_used": ["f000012.jpg", "..."], "corners_triangulated": 4,
    "corner_reproj_rms_px": null, "min_triangulation_angle_deg": null,
    "side_lengths_units": [0, 0, 0, 0], "squareness": {"side_ratio_max_dev": null, "diag_over_side_dev": null},
    "metres_per_unit": null, "rel_std_sides": null, "accepted": true, "reject_reason": null},
   {"method": "TWO_POINT_TAPE", "p1": [0, 0, 0], "p2": [0, 0, 0], "true_m": 0.0, "map_units": 0.0,
    "pick_tolerance_units": 0.0, "metres_per_unit": null, "note": "문 폭", "accepted": true}
 ],
 "verification": {
   "criterion": "fit에 쓰지 않은 독립 길이 ≥ 2개가 각각 오차 ≤ 5 %",
   "checks": [{"name": "책상 길이", "true_m": 0.0, "map_units": 0.0, "predicted_m": 0.0, "error_pct": null, "pass": null}],
   "passed": false
 },
 "cross_checks": [
   {"method": "TELLO_TELEMETRY", "metres_per_unit": null, "diff_vs_primary_pct": null, "note": "MOCK"},
   {"method": "METRIC_DEPTH_MODEL", "model": "...", "metres_per_unit": null, "diff_vs_primary_pct": null, "note": "모델 추정, 검증 아님"}
 ],
 "ground_plane": null,
 "tools": {"opencv": "...", "pycolmap": "..."},
 "commands": ["..."],
 "created_at": "...", "updated_at": "..."
}
```

규칙:
- `status` 결정은 `packages/mapping/metric_scale.py` 한 곳에서만 합니다(UI나 서버가 따로 판정하지 않음).
  - `NONE`: 관측 0개, 또는 `map_hash` 불일치(지도가 다시 만들어짐), 또는 모델 추정만 있고 그것도 없음.
  - `MODEL_ESTIMATED`: 측정 관측은 없고 모델(방법 4) 추정만 있음. UI에는 **"모델 추정, 검증 아님"** 으로만 표시.
  - `MEASURED_UNVERIFIED`: 방법 1 또는 2의 측정 관측이 ≥ 1개 있으나 3절의 검증 기준을 통과하지 못함(검증 길이 부족 포함).
  - `MEASURED_VERIFIED`: 3절 기준 통과. 이때만 UI 라벨 **"측정으로 확인됨"**.
- `metres_per_unit`은 **채택된 측정 관측들의 중앙값**. 모델 추정과 Tello 값은 `cross_checks`에만 들어가고 평균에 섞지 않습니다.
- 모델이 여러 개(`num_models > 1`)이면 스케일은 `model_rank`가 가리키는 모델에만 유효합니다. 다른 모델은 각각 별도 `scale.json`이 필요하지만 이번 범위는 rank 0만.
- `map_report.json`의 `labels.scale`은 계속 `RELATIVE`로 둡니다(그 파일은 빌드의 증거). `/api/sessions/<id>`가 `scale.json` 요약을 `metric_scale` 필드로 덧붙여 내려주고 UI는 그걸로 표시합니다. `Scale.METRIC_ANCHORED` enum 값은 API 응답의 파생 라벨에만 사용(파일에는 쓰지 않음).

### 2.2 UI 라벨 (최소)

- 좌하단 칩 한 줄(기존 `Relative units · scale unknown` 자리):
  - `NONE` → "미터 스케일 없음 · 상대 단위"
  - `MODEL_ESTIMATED` → "미터 스케일: 모델 추정 (검증 아님)"
  - `MEASURED_UNVERIFIED` → "미터 스케일: 측정됨 · 미검증 (검증 길이 n/2 통과)"
  - `MEASURED_VERIFIED` → "미터 스케일: 측정으로 확인됨 (방법, 관측 n개, 최대 오차 x %)"
- 헤더 배지 `SCALE RELATIVE`는 유지하고, 확인된 경우에만 `SCALE RELATIVE → m (verified)`처럼 덧붙임. `NOT_NAVIGATION_SAFE` 배지는 어떤 경우에도 그대로.
- 거리 재기 도구 1개(아래 5절). 그 외 UI 추가 없음.

---

## 3. 불확실성 계산과 사전 등록 합격 기준

아래 임계값은 **지금 정해 두는 것**이며 결과를 보고 바꾸지 않습니다. 바꾸려면 새 날짜 절을 추가합니다.

### 3.1 마커 관측(방법 1)의 품질 지표

| 지표 | 정의 | 채택 조건 |
|---|---|---|
| `frames_used` | 마커 4모서리가 모두 검출되고 포즈가 있는 프레임 수 | ≥ 3 |
| `min_triangulation_angle_deg` | 각 모서리를 본 카메라 광선 사이의 최대 각(모서리별 최소값) | ≥ 2° |
| `corner_reproj_rms_px` | 삼각측량한 모서리를 사용 프레임에 재투영한 RMS(1600 px 긴 변 기준) | ≤ 1.5 px |
| `side_ratio_max_dev` | 지도상 네 변 길이의 최대/최소 − 1 | ≤ 0.03 |
| `diag_over_side_dev` | 두 대각선 평균 / 네 변 평균 − √2, 절대값/√2 | ≤ 0.03 |
| `rel_std_sides` | 네 변으로 각각 구한 스케일의 표준편차 / 평균 | 기록(채택 조건 아님) |

조건을 하나라도 어기면 그 마커 관측은 `accepted=false`, `reject_reason`을 적고 평균에서 뺍니다. 정사각형 검사(`squareness`)는 오검출·잘못된 삼각측량·내부 파라미터 오차를 걸러 주는 자체 검사입니다.

### 3.2 불확실성

- 마커 1개: `rel_std_sides`(네 변 간 분산).
- 마커 ≥ 2개: 마커별 스케일의 (최대 − 최소) / 평균 = `spread_between_markers_rel`. **≤ 5 %** 면 "한 스케일로 지도 전체를 설명할 수 있음", 넘으면 **"구간별 스케일 다름(단안 드리프트 의심)"** 플래그를 세우고 `status`는 `MEASURED_UNVERIFIED`로 둡니다(어느 한 마커도 지도 전체를 대표하지 못하므로).
- 두 점 관측(방법 2): 클릭이 실제 모서리가 아닌 **근처 희소점**에 붙기 때문에, 선택 반경(`pick_tolerance_units`, 화면 반경을 그 깊이에서 월드 단위로 환산)을 기록하고 상대 불확실성 ≈ 2 × `pick_tolerance_units` / `map_units`로 계산합니다. 긴 거리(실제 ≥ 2 m)를 재면 불확실성이 작아진다는 안내를 UI 힌트에 넣습니다.
- 최종 `uncertainty.rel_std`: 채택 관측들의 스케일 표준편차/평균(관측 1개면 그 관측의 자체 불확실성).

### 3.3 검증 기준 (`MEASURED_VERIFIED`가 되는 조건)

1. **독립 검증 길이 ≥ 2개**: 스케일 fit에 쓰지 않은, 줄자로 잰 길이(각각 실제 ≥ 1 m 권장). 뷰어 두 점 도구로 지도상 거리를 재고 현재 스케일로 미터 예측 → `|predicted − true| / true ≤ 5 %`가 **모두** 성립.
2. 마커가 2개 이상이면 `spread_between_markers_rel ≤ 5 %`.
3. 채택된 마커 관측 ≥ 1개(방법 2만 있는 세션은 1·2점 관측 ≥ 1개 + 독립 검증 2개로 `MEASURED_VERIFIED` 가능. 이때 fit 길이와 검증 길이는 서로 다른 물체여야 함).

통과 못 하면 `MEASURED_UNVERIFIED`로만 표시하고, 어떤 항목이 왜 실패했는지 `verification.checks`에 남깁니다. "미검증"이어도 거리 도구는 미터를 보여 주되 라벨에 "미검증"을 붙입니다.

### 3.4 합성 장면 테스트 기준 (6.2절)

렌더러의 월드 단위를 미터로 선언하고(마커 변 0.5 단위 = 0.5 m), SfM → 마커 검출 → 스케일 추정 후 `|metres_per_unit − 1| ≤ 2 %`. (기존 `tests/test_pipeline.py`는 같은 장면에서 ATE < 1 cm를 요구하고 통과하므로 2 %는 넉넉한 사전 기준입니다. 결과가 이를 넘으면 FAIL로 기록하고 원인을 `docs/FAILURES.md`에 적습니다.)

---

## 4. 방법별 설계

### 4.1 방법 1 — 인쇄 마커 (기본)

**모듈** `packages/mapping/markers.py`(생성·검출), `packages/mapping/metric_scale.py`(삼각측량·스케일·상태). 둘 다 새 파일. 기존 파이프라인(`sparse_pipeline.py`)은 **수정하지 않고** 빌드가 끝난 세션에 사후 적용합니다.

처리 순서:
1. 세션의 `sparse/<rank>`를 pycolmap으로 읽어 카메라 모델·`params_refined`·등록 이미지 포즈를 가져옵니다(`trajectory.json`을 읽어도 같지만, 왜곡 보정에 카메라 모델이 필요하므로 COLMAP 모델 쪽을 기준으로 함).
2. `frames/*.jpg` 중 등록된 프레임마다 `cv2.aruco`(또는 대체 검출기)로 마커를 검출합니다. 모서리 서브픽셀 정제를 켭니다. 결과: `(image_name, marker_id, 4×2 픽셀)`. 중간 산출물 `scale_work/marker_detections.json`.
3. 각 모서리 픽셀을 COLMAP 카메라 모델로 **왜곡 보정한 정규화 좌표**로 바꾼 뒤, 그 모서리를 본 모든 프레임의 포즈로 DLT 선형 삼각측량 → 재투영 오차 최소화로 정제(소규모 비선형 최소제곱, scipy). 모서리마다 `reproj_rms_px`, 최대 삼각측량 각을 기록.
4. 마커별로 네 변·두 대각선의 지도상 길이를 계산 → 3.1절 품질 검사 → 변 길이로 스케일 4개 → 중앙값이 그 마커의 `metres_per_unit`.
5. 사용자가 적어 준 `measurements.json`(마커 id별 실측 변 길이, 검증 길이 목록)을 읽어 `scale.json`을 쓰고 상태를 판정합니다.

보조 진단(채택 조건 아님): 마커 크기를 아는 상태에서 프레임별 `solvePnP`로 카메라–마커 거리를 미터로 구해, 지도상 카메라 중심–마커 중심 거리와 비교한 프레임별 스케일을 `scale_work/pnp_scales.json`에 남깁니다. 같은 데이터에서 나온 값이라 독립 검증은 아니지만, 삼각측량 결과와 크게 다르면 모서리 대응이 틀렸다는 신호입니다.

**마커 생성**: `python -m mapping.markers make --dict 4X4_50 --ids 0 1 --side-mm 160 --out assets/markers/` → A4 비율 PNG(300 dpi)와 안내문. 변 160 mm는 A4(210 × 297 mm)에 여백을 두고 들어가는 크기로 정한 **설계값**입니다. 인쇄 후 실제 변 길이는 사용자가 자로 재서 쓰는 값만 믿습니다(프린터 배율 때문).

**명령**(Opus가 구현):
```
PYTHONPATH=packages .venv/bin/python -m mapping.cli scale markers outputs/maps/<session> \
  --measurements assets/input/<clip>.measurements.json --model 0
PYTHONPATH=packages .venv/bin/python -m mapping.cli scale status outputs/maps/<session>
```

### 4.2 방법 2 — 줄자로 잰 두 점 (대체·검증 입력)

**뷰어**: `viewer.js`에 `pickMode`와 `pickPoint(clientX, clientY)` 추가. 현재 뷰 행렬로 투영했을 때 클릭 반경(예: 8 px × dpr) 안에 들어오는 점 중 깊이가 가장 작은 점의 인덱스를 돌려줍니다(`render()`의 투영식 재사용; 성능을 위해 밀집 레이어에서는 샘플링 간격을 두거나 희소 레이어로 자동 전환). 선택한 점은 작은 원과 번호(1, 2)로 표시, 두 점 사이 선분과 거리 텍스트를 그립니다. 기존 `onPick`(overview 카메라 선택)은 이름을 그대로 두고 건드리지 않습니다.

**앱**(`app.js`, `index.html`): 지도 패널 헤더에 버튼 **"Measure"** 1개(`aria-pressed`). 켜면 클릭 두 번으로 거리 표시 → 지도 단위는 항상, 미터는 `scale.json` 상태가 `NONE`이 아닐 때 라벨과 함께. 그 아래 작은 입력 1개 "실제 거리 (m)"와 두 버튼: **"스케일에 사용"**(관측으로 저장) / **"검증으로 사용"**(검증 길이로 저장). 이것이 UI의 전부입니다.

**서버**: `POST /api/sessions/<id>/scale` 본문 `{"method": "TWO_POINT_TAPE", "p1": [...], "p2": [...], "true_m": 0.9, "role": "fit" | "verify", "note": "문 폭", "pick_tolerance_units": 0.01}` → `metric_scale.add_two_point()`가 `scale.json`을 원자적으로 갱신하고 상태 재판정, 갱신된 요약을 응답. `GET /api/sessions/<id>/files/scale.json` 허용. 기존 V-01 지적(요청 출처 검사 없음)이 열려 있으므로 이 POST에도 같은 한계가 있음을 기록(이 계획에서 고치지 않음; 파일 1개를 쓰는 경로라 위험은 작음).

**기존 영상에의 적용**: 복도 영상(`mqg_corridor`, 사용자 제공)은 사용자가 그 복도에서 문 폭·바닥 타일 등을 줄자로 재면 방법 2로 스케일을 붙일 수 있습니다. 스미다·FSC는 공개 클립이라 실측이 불가능하므로 크기를 "아는 것 같은" 물체로 추정하지 않습니다(붙이면 `MEASURED_UNVERIFIED`에 "출처: 추정 치수"를 명시, 기본은 붙이지 않음).

### 4.3 방법 3 — Tello 텔레메트리 (MOCK만, 교차 확인용)

- 입력: 상태 패킷의 `h`(이륙점 기준 상대 높이, cm)와 `tof`(아래쪽 거리 센서, cm). 두 값 모두 정수 cm이고 사용자 조사에 따르면 실제 기체에서는 분해능이 거칠고 기압계 기반 대체값이 섞인다고 알려져 있습니다(이 저장소에서 측정한 사실이 아님). `tof`는 바로 아래 표면까지의 거리라 탁자 위를 지나면 바닥 높이가 아닙니다.
- 지도 쪽: 바닥 평면을 희소점에서 RANSAC으로 맞추고(`ground_plane`에 저장; 뷰어의 PCA 수평 맞춤과 별개로 숫자로 기록), 포즈별 카메라 중심–바닥 평면 거리(지도 단위)를 구합니다.
- 스케일: 동일 시각의 `(h_cm/100 + 착륙 시 카메라 높이 보정값)` ÷ 지도상 높이. 보정값은 기체가 생기면 자로 재는 값(지금은 미정). 프레임–패킷 시각 동기는 라이브 실행의 `timeline.jsonl` 시간축을 씁니다.
- 결과는 `cross_checks`에만 들어가며, 주 스케일과의 차이 `diff_vs_primary_pct`를 기록합니다. 사전 플래그 기준: **> 15 %** 면 "텔레메트리 불일치" 표시(1 m 고도에서 10 cm 분해능이 10 %에 해당하므로 느슨하게 둔 값). 검증 상태에는 영향 없음.
- 구현 범위: `MockTello`에 스크립트 고도 프로필(예: 0.8 → 1.2 → 1.0 m)을 넣는 옵션, 상태 패킷 기록기(`outputs/runs/<id>/telemetry.jsonl`), 합성 포즈·바닥 평면으로 스케일을 복원하는 단위 테스트. **실기체 연결·이륙은 이 계획에 없음.** 기체가 생기더라도 영상·상태 수신만 하는 첫 단계(D-01)와 사용자 승인이 먼저입니다.

### 4.4 방법 4 — 미터 깊이 모델 (참고용)

부록 A의 시험이 Mac에서 돌아간다고 확인된 뒤에만 연결합니다. 연결 방식: 모델이 낸 프레임별 미터 깊이와 우리 삼각측량점의 깊이(지도 단위)를 같은 픽셀에서 비교해 프레임별 비율 → 중앙값을 `cross_checks[METRIC_DEPTH_MODEL].metres_per_unit`에 기록. UI에는 측정 스케일과 **다른 정도(%)** 만 표시합니다. 측정 관측이 하나도 없으면 상태는 `MODEL_ESTIMATED`이고 거리 도구의 미터 표시에 "모델 추정, 검증 아님"이 붙습니다.

---

## 5. 앱 UI 변경 요약 (최소)

| 위치 | 변경 | 비고 |
|---|---|---|
| 지도 패널 헤더 | 버튼 `Measure` 1개 추가 | 토글. 켜면 포인터가 점 선택 모드 |
| 지도 위 오버레이 | 두 점·선분·거리 텍스트, 입력 "실제 거리 (m)" + "스케일에 사용"/"검증으로 사용" | Measure가 켜진 동안만 보임 |
| 좌하단 칩 | 스케일 상태 한 줄(2.2절 문구) | 기존 고정 문구 대체 |
| 헤더 배지 | `SCALE RELATIVE`에 확인 여부 덧붙임 | `NOT_NAVIGATION_SAFE` 불변 |

레이어 메뉴·측정 이력 목록·스케일 바 등은 추가하지 않습니다(사용자 메모: UI는 최소로).

---

## 6. 단계 분할 (산출물·증거·테스트)

증거 루트: `outputs/evidence/metric_scale/`. 모든 단계는 `scripts/test_all.sh` 통과를 전제로 하며, 각 단계 종료 시 Opus가 원시 JSON/로그를 남기고 fable-documenter가 `docs/PROGRESS.md`·`docs/VALIDATION_LOG.md`를 갱신합니다.

### S0 — 환경 확인 (Opus, 반나절 이내)

- 할 일: `.venv`에서 `cv2.aruco` 존재 여부, `ArucoDetector` API 유무, pycolmap에서 카메라 모델의 픽셀→정규화 좌표 함수 이름 확인. 없으면 대체(같은 버전 contrib 패키지 또는 AprilTag 파이썬 패키지)를 `.venv`에만 설치하고 `requirements.txt` 갱신.
- 산출물: `outputs/evidence/metric_scale/env_check.json`(opencv 버전, `has_aruco`, 사용 검출기, pycolmap 버전, 확인 명령).
- 합격: 검출기 1종이 import되고, 생성한 마커 이미지를 그대로 다시 검출해 id가 일치.
- 막힘: 두 대체 모두 설치 불가 → `BLOCKED`로 기록하고 S3(두 점 방법)부터 진행.

### S1 — 코어 모듈 `packages/mapping/metric_scale.py` (Opus)

- 내용: `scale.json` 읽기/쓰기(원자적), `map_hash`, 상태 판정, DLT 삼각측량 + 재투영 정제, 정사각형 검사, 두 점 관측 추가, 검증 평가, 바닥 평면 RANSAC.
- 테스트 `tests/test_metric_scale.py`(빠른 단위 테스트, SfM 없이 합성 기하): 알려진 포즈·K로 사각형 네 모서리를 투영하고 ±0.3 px 잡음을 섞어 삼각측량 → 변 길이 오차 ≤ 1 %; 재투영 RMS ≤ 1 px; 상태 전이(NONE → UNVERIFIED → VERIFIED → `map_hash` 불일치 시 NONE); 검증 길이 1개만 있을 때 VERIFIED가 되지 않음; 모델 추정만 있을 때 `MODEL_ESTIMATED`.
- 합격: 위 테스트 전부 PASS, 로그 `outputs/evidence/tests/pytest-<날짜>-metric-scale.log`.

### S2 — 합성 마커 장면과 end-to-end 테스트 (Opus)

- `packages/sim/synthetic_scene.py`에 `markers=[{"id": 0, "plane": 0, "center_units": (x, y), "side_units": 0.5}]` 옵션 추가(기본값 없음 → 기존 테스트 불변). 마커 그림을 해당 평면 질감에 `_PX_PER_UNIT` 기준 크기로 붙이고, 모서리의 월드 좌표를 `markers_gt.json`에 기록.
- `tests/test_metric_scale_synthetic.py`(`@pytest.mark.slow`): 마커 2개(서로 다른 벽) 장면 생성 → `build_sparse_map` → `scale markers` → 3.4절 기준(`|metres_per_unit − 1| ≤ 2 %`), 두 마커 스케일 차 ≤ 2 %, 각 모서리 재투영 RMS ≤ 1.5 px. 추가로 정답 모서리 좌표 대비 삼각측량 모서리 위치 오차(Sim3 정렬 후) 기록.
- 산출물: `outputs/evidence/metric_scale/synthetic_marker.json`(생성 설정, 등록 수, 스케일, 지표, PASS/FAIL).
- 막힘: 합성 장면에서 마커가 검출되지 않으면(질감 대비·해상도) 마커 크기·프레임 해상도를 바꾼 **새 날짜 절**을 쓰고 재시도. 기준값은 바꾸지 않음.

### S3 — 두 점 도구와 스케일 상태 표시 (Opus)

- 서버: `SESSION_FILES`에 `scale.json`, `POST /api/sessions/<id>/scale`, `/api/sessions/<id>` 응답에 `metric_scale` 요약.
- 뷰어·앱: 5절.
- 테스트: `tests/test_server.py`에 POST → `scale.json` 생성 → 상태 `MEASURED_UNVERIFIED` → 검증 2개 추가 시 `MEASURED_VERIFIED`(합성 세션 픽스처) 케이스; `tests/ui/ui_smoke.mjs`에 Measure 토글 → 점 2개 클릭 → 거리 텍스트 표시 → 실제 거리 입력 → 칩 문구 변경 체크(스모크 체크 수 증가분을 로그에 기록).
- 산출물: `outputs/evidence/ui/measure_tool.png`, `outputs/evidence/ui/ui_smoke.json`, 서버 테스트 로그.
- 합격: 위 테스트 PASS, 콘솔 오류 0.

### S4 — 실제 영상 게이트 (사용자 촬영 필요 → Opus 실행)

- 입력: 사용자가 7절대로 찍은 영상 `assets/input/<clip>.mp4` + `assets/input/<clip>.measurements.json`(마커 변 길이, 검증 길이 2개 이상).
- 명령: `scripts/build_sparse_map.sh assets/input/<clip>.mp4 --session metric_01 --origin REAL_VIDEO` → `mapping.cli scale markers outputs/maps/metric_01 --measurements ...` → 앱에서 검증 길이 2개를 Measure 도구로 입력 → `scale status`.
- 산출물: `outputs/maps/metric_01/{map_report.json,scale.json,scale_work/}`, `outputs/evidence/metric_scale/real_metric_01.json`(지표 복사본, 명령, 시간), 스크린샷 `outputs/evidence/ui/metric_01_measure.png`.
- 합격: 지도 `GOOD` 또는 `PARTIAL`에서 rank 0 모델에 마커 ≥ 1개 채택 + 3.3절 검증 통과 → `MEASURED_VERIFIED`. 이 단계가 **`REAL_VIDEO` 게이트(M-01)도 함께 닫습니다.**
- 실패/막힘: 마커 미검출(흐림·크기) → 7절 재촬영 안내; 두 마커 스케일 차 > 5 % → `MEASURED_UNVERIFIED` + 드리프트 플래그로 기록하고, 다음 시험으로 "마커 사이를 더 천천히 옆으로 이동"한 재촬영 또는 `--selection adaptive` 재빌드(기존 옵션) 비교.

### S5 — Tello 텔레메트리 MOCK 경로 (Opus)

- `MockTello`에 고도 프로필 옵션, 상태 기록기, `metric_scale.scale_from_height()`.
- 테스트 `tests/test_tello.py` 추가 케이스: 합성 포즈(바닥 평면 위 알려진 높이)와 Mock 상태 패킷으로 스케일 복원 오차 ≤ 1 %(MOCK은 잡음이 없으므로), `HARDWARE_ARMED=false`에서 비루프백 송신 0회 불변.
- 산출물: `outputs/evidence/metric_scale/tello_mock_height.json`.
- 실기체: **사용자 승인 필요**, 이 계획에서 NOT_RUN.

### S6 — 미터 깊이 모델 시험 (부록 A, 독립 진행 가능)

- 별도 venv(`outputs/tools/venv_metric`), 가중치 로컬 저장, SHA-256 기록. 결과는 `cross_checks`로만 연결.

### S7 — 문서 갱신 (fable-documenter)

- `docs/PROGRESS.md`(미터 스케일 행 NOT_RUN → 결과), `docs/VALIDATION_LOG.md`(명령·버전·경로), `docs/SYSTEM_STATUS.md` 2절·5절, `README.md` 설계 절(거리 도구 사용법·라벨 의미). `docs/IMPROVEMENTS.md` S-01 상태 갱신, S-02(2D 라이다) **폐기**로 표기.

의존 관계: S0 → S1 → S2, S1 → S3, (S2, S3) → S4(사용자 촬영 필요), S1 → S5, S6은 S1 이후 독립, S7은 각 단계 뒤.

---

## 7. 사용자에게 요청할 촬영 방법

이 절은 S4의 입력을 만드는 절차입니다. 비용은 A4 인쇄 2장과 줄자(또는 크기를 아는 물체)입니다.

1. **마커 인쇄**: Opus가 만든 `assets/markers/aruco_4x4_50_id0.png`, `id1.png`를 A4에 **100 % 배율(페이지 맞춤 끄기)** 로 인쇄. 마커 둘레에 흰 여백이 1 cm 이상 남아야 합니다. 무광 용지가 좋고, 코팅·유리 뒤는 반사 때문에 피합니다.
2. **변 길이 측정**: 인쇄된 검은 정사각형의 한 변을 자로 **mm 단위**까지 재서 네 변 평균을 종이 여백에 적습니다(예: 159 mm). 이 값만 믿습니다.
3. **붙이기**: 종이가 휘지 않게 딱딱한 판·벽·바닥에 테이프로 평평하게 붙입니다. **마커 2개를 서로 3 m 이상 떨어진 곳**(방 양쪽 끝, 복도 양 끝)에 둡니다. 하나는 바닥, 하나는 벽(카메라 높이)도 좋습니다.
4. **검증 길이 2개 재기**: 마커와 **무관한** 길이 2개를 줄자로 잽니다(문 폭, 책상 긴 변, 바닥에 테이프로 표시한 두 X 사이 거리 등, 각 1 m 이상 권장). 잰 물체의 양 끝이 영상에 또렷히 나와야 뷰어에서 점을 찍을 수 있습니다. 값과 물체 이름을 적습니다.
5. **촬영**: 지도 촬영 규칙과 같습니다 — 같은 카메라, 한 테이크 30–90초, 줌·노출 고정(가능하면), 천천히 **옆으로 이동하며** 촬영(제자리 회전만 하면 삼각측량이 안 됨). 마커 1에서 시작해 1–2 m 거리에서 3–4초 이상 비추며 좌우로 반 걸음 이동, 검증 물체들을 지나 마커 2까지 가서 같은 방식으로 3–4초. 마커가 화면 너비의 1/15 이상(1600 px 기준 약 100 px) 크기로 보이게 합니다. 흔들림·모션블러를 줄이려면 걷는 속도를 평소의 절반으로.
6. **파일 두기**: 영상을 `assets/input/<이름>.mp4`로, 측정값을 `assets/input/<이름>.measurements.json`으로 둡니다. 형식:
   ```json
   {"markers": {"0": {"side_m": 0.159}, "1": {"side_m": 0.160}},
    "checks": [{"name": "문 폭", "true_m": 0.905}, {"name": "책상 긴 변", "true_m": 1.402}],
    "camera": "iPhone 후면 기본 카메라, 1080p30", "notes": "실내, 낮"}
   ```
7. 선택: 같은 카메라로 체커보드 보정(`docs/IMPROVEMENTS.md` S-04, 저장소에 아직 도구 없음). 없어도 S4는 진행되며 `CALIB UNCALIBRATED_ESTIMATED`로 기록됩니다.

---

## 8. 위험과 한계

- **단안 드리프트**: 한 곳에서 구한 스케일이 지도 전체에 맞지 않을 수 있습니다. 그래서 떨어진 두 마커를 요구하고 차이가 5 %를 넘으면 "확인됨"으로 올리지 않습니다. 구간별 스케일(마커별 영역 분할)은 이 계획의 범위 밖이며, 필요해지면 새 계획으로 다룹니다.
- **충돌 회피 안전을 보장하지 않음**: 미터가 붙어도 희소/모델 레이어의 빈 곳·가짜 점(반사 등, `docs/SYSTEM_STATUS.md` 5절)은 그대로입니다. `NOT_NAVIGATION_SAFE` 유지.
- **내부 파라미터 추정 오차**: 보정 없이 COLMAP이 추정한 `SIMPLE_RADIAL`은 왜곡을 완전히 설명하지 못할 수 있습니다. 정사각형 검사가 일부를 걸러 주지만, 체커보드 보정이 있으면 더 좋습니다.
- **인쇄 배율·종이 휨**: 변 길이는 항상 실측값, 종이는 평평하게.
- **마커 미검출**: 흐림·너무 작음·강한 반사·역광. 재촬영 안내(7절).
- **지도 재빌드**: `map_hash`가 바뀌면 `scale.json`은 `NONE`으로 떨어지고 UI에 "지도가 다시 만들어져 스케일 무효" 사유를 보입니다. 재적용은 `scale markers` 한 번 더.
- **여러 모델**: `num_models > 1`이면 rank 0만 스케일. 다른 모델은 좌표계가 달라 같은 스케일을 쓰면 안 됩니다.
- **Tello 값의 거칠기**: 정수 cm, 센서 특성상 교차 확인용. 실기체 단계는 사용자 승인 필요.
- **모델 추정의 한계**: 항공 영상에서 모델이 먼 지면을 1.5–5배 가깝게 눌렀던 기록(`docs/SYSTEM_STATUS.md` 5절)이 있어, 모델이 내는 미터도 장면에 따라 크게 틀릴 수 있습니다. 주 근거로 쓰지 않는 이유입니다.
- **`cv2.aruco` 부재 가능성**: S0에서 확정. 대체 경로 명시.
- **공개 클립(스미다·FSC)** 에는 실측이 없어 스케일을 붙일 수 없습니다. 붙이지 않습니다.

---

## 9. 비용

새로 드는 돈: A4 인쇄 2장, (없으면) 줄자. 소프트웨어는 전부 로컬·무료(OpenCV, pycolmap, scipy, 선택적으로 contrib/AprilTag 파이썬 패키지와 부록 A 모델 가중치). 유료 API·클라우드 추론 없음. 기체 구매·비행은 이 계획과 무관하며 사용자 승인 전 금지(`docs/COST_LEDGER.md` 0원 원칙 유지).

---

## 부록 A. "현재 방식보다 나을 수 있는 방법" 시험 설계 (모두 Mac 동작 미확인)

목적 두 가지: (1) 모델이 내는 **미터 깊이**가 측정 스케일과 얼마나 맞는지 교차 확인값으로 쓸 수 있는가, (2) 현재 "모델 깊이를 우리 경로에 올리는" steady 레이어(`model_on_path.py`)보다 **포즈·내부 파라미터를 입력으로 받는 모델**이 더 일관된 레이어를 만드는가.

| 후보 | 왜 후보인가 | 확인 필요 |
|---|---|---|
| **MapAnything** | 여러 뷰와 함께 **카메라 포즈·내부 파라미터를 선택 입력**으로 받을 수 있어, 지금 우리가 바깥에서 하는 "모델 깊이를 우리 경로에 정렬"을 모델 안에서 수행. 미터 단위 출력을 표방 | 코드·가중치 라이선스(상업/비상업 구분), MPS 동작, 메모리, 프레임 수 한계 |
| **Depth Anything 3 (metric)** | 단안/다중 뷰 깊이의 metric 변형 | 가중치 라이선스, MPS 동작, 포즈 입력 가능 여부 |
| **π³-X** | 순서 없는 다중 뷰에서 포즈·점군을 내는 계열의 metric 변형으로 알려짐 | 실제 metric 출력 여부와 라이선스, MPS 동작 |
| VGGT-SLAM 계열 | 보고된 환경이 NVIDIA 24 GB급 메모리 | 이 Mac(16 GB)에서는 **후순위**, 시험 보류 |

시험 설계(사전 등록):
- **입력**: 스미다·FSC·복도 세션의 선별 프레임(`frames/`), 우리 포즈(`trajectory.json`)와 `params_refined`. 가능한 모델에는 포즈·내부 파라미터를 함께 넣고, 넣지 못하는 모델은 출력 포즈를 우리 경로에 Sim3 정렬(기존 `learned_align.py`).
- **지표 1 (스케일 교차 확인)**: 프레임별 모델 깊이 / 우리 삼각측량 깊이 비율의 중앙값과 사분위 범위. 측정 스케일(S4)이 있는 세션에서만 `diff_vs_primary_pct`를 계산. 사전 플래그: 측정값과 **> 15 %** 차이면 "모델 불일치".
- **지표 2 (레이어 품질)**: 현재 steady 레이어와 **같은 평가 프레임·같은 지표**(`static_eval.py`의 `cov1px`, 떠 있는 점 비율 `float_fs`, 고립점 비율, 하늘 오탐)로 비교. 기존 steady 값이 기준선(`outputs/evidence/learned_depth/static_metrics_onpath3_fsc.json`, `static_metrics_onpath4_sumida.json`).
- **지표 3 (자원)**: 프레임당 시간, 피크 메모리, 완주 여부. 16 GB에서 OOM이면 청크 실행(기존 `chunked_run.py` 방식) 가능 여부.
- **산출물**: `outputs/experiments/metric_models/<model>/<session>/{run.cmd, run.log, depth_npz 또는 ply, align.json, scale_check.json, static_metrics_<...>.json}`, 요약 `outputs/evidence/metric_scale/metric_models_summary.json`.
- **PASS/FAIL**: 지표 2에서 steady 대비 `float_fs` 악화 없이(> +1 pp 금지) 평가 프레임 바닥·구조물 `cov1px`가 모두 같거나 좋으면 "steady 대체 후보", 아니면 "참고 레이어"로만. 지표 1은 PASS/FAIL이 아니라 교차 확인 수치.
- **BLOCKED 경로**: MPS에서 로드 실패·OOM·프레임당 60 s 초과가 지속되면 해당 모델은 BLOCKED로 기록(로그 첨부)하고 다음 후보로. CPU 강제 실행은 시간만 기록하고 판정에 쓰지 않음.
- 제약: 로컬 실행, 유료 API 없음, 가중치 SHA-256 기록, 모델 포즈·내부 파라미터를 지도 파일에 쓰지 않음(기존 원칙 유지), 모델 출력 레이어는 계속 "모델 추정" 라벨.

---

## 부록 B. 선택 사항으로만 두는 것

- 휴대폰 ARKit/ARCore의 미터 궤적을 영상과 함께 기록해 스케일로 쓰는 방법: 아이폰 등 특정 기기와 별도 앱이 전제라 이 계획에서는 구현하지 않음. 기록만.
- 미터 스케일을 적용한 파생 PLY(`sparse_metric.ply`)를 내보내는 기능: 원본은 그대로 두고 파생 파일 헤더에 `metres_per_unit`·상태를 적는 조건이면 가능하지만, 뷰어가 `scale.json`으로 환산하므로 기본 범위에서 제외.
- 구간별(마커별) 스케일 보정: 8절 참조, 새 계획으로.

---

## 10. 결과

작성: Opus (Fable 한도로 대체, 사용자 지시), 2026-11-15. 실행·측정은 2026-11-14에 Opus 5.5가 했고, 아래 수치는 모두 `outputs/evidence/metric_scale/`의 원시 파일에서 옮겼습니다. 1–9절의 사전 기준은 바꾸지 않았고, 기준을 바꾸거나 계획과 다르게 한 것은 10.3절에 모았습니다.

### 10.1 단계별 판정

| 단계 | 상태 | 핵심 결과 | 근거 |
|---|---|---|---|
| S0 환경 확인 | PASS | OpenCV 5.0.0에 `cv2.aruco`, `ArucoDetector`, ChArUco, AprilTag 사전이 모두 있음(대체 패키지 설치 불필요). 합성 이미지(1280×720, f=900 px)에서 15 cm 마커는 8 m 정면(약 17 px)까지 검출, 12 m(약 11 px)부터 미검출; 25 cm 마커는 16 m(약 14 px)까지 검출 | `opencv_aruco_check.txt` (계획의 `env_check.json` 대신 이 파일) |
| S1 코어 모듈 | PASS | `packages/mapping/metric_scale.py`, 단위 테스트 `tests/test_metric_scale.py` 12개 | 2026-11-15 pytest 5회 연속 175 passed (`outputs/evidence/portfolio/pytest-5x-summary.txt`) |
| S2 합성 마커 E2E | **계획 배치 FAIL / 같은 벽 배치 PASS** | 10.2절 표 | `synthetic_marker.json`, `synthetic_marker_run.log` |
| S3 두 점 도구·상태 표시 | PASS | 서버 `POST /api/sessions/<id>/scale`(fit 1개 + 검증 2개 → `MEASURED_VERIFIED`, 잘못된 입력 거부), 뷰어 Measure 도구, 상태 칩·배지. UI 스모크 1차 78 passed / 1 failed("found drawn points to click"), 2·3차 88 passed / 0 failed | `tests/test_server.py`(`test_scale_post_*`), `ui_smoke_run{1,2,3}.log`, `outputs/evidence/ui/measure_tool.png` |
| S4 실제 영상 게이트 (사용자 촬영 마커 영상) | **NOT_RUN** | 사용자 영상이 아직 없음. 대신 공개 실제 녹화 2건으로 두 점 방법을 시험(10.2절): TUM fr1/desk(정답 있음)와 테니스장 항공 영상(규정 치수). `REAL_VIDEO` 게이트(M-01)는 열린 채 | `real_tum_fr1_desk.json`, `real_yt_tennis_complex.json` |
| S5 Tello 텔레메트리 (MOCK) | PASS | 스크립트 고도 0.8 → 1.2 → 1.0 m, 텔레메트리 76 패킷·38 프레임, 높이로 구한 스케일 0.3700091 vs 참값 0.37 → 오차 **0.0025 %**(기준 ≤ 1 %). UDP 루프백 83회, **비루프백 0회**, 무장 해제 상태의 실주소 송신 시도 4회 모두 거부. 교차 확인 전용(주 스케일 아님). 실기체 NOT_RUN | `tello_mock_height.json`, `tello_mock_height_telemetry.jsonl` |
| S6 미터 깊이 모델 시험 | PASS(실행) / 대체 후보 **없음** | 10.2절 표 | `metric_models_summary.json`, `metric_models_logs/` |
| S7 문서 | 이 절 | README, `docs/PROGRESS.md`, `docs/VALIDATION_LOG.md`, `docs/SYSTEM_STATUS.md`, `docs/IMPROVEMENTS.md` 갱신 | — |

### 10.2 측정 결과

**합성 마커 장면 (SIMULATED, 렌더러 단위를 미터로 선언).** 기준은 3.4절(스케일 오차 ≤ 2 %, 두 마커 스케일 차 ≤ 2 %, 모서리 재투영 RMS ≤ 1.5 px).

| 배치 | 지도 | 마커 채택 | 스케일 오차 | 판정 |
|---|---|---|---|---|
| 계획대로: 마커 2개를 서로 다른 벽(평면 0, 4)에 | 24/24 등록, 6,388점, GOOD | 마커 0만 채택(19 프레임, RMS 0.199 px). 마커 1은 3 프레임에서만, 모두 45 px 미만으로 검출돼 기각 | +0.339 % | **FAIL**(두 마커 조건) |
| 추가: 같은 앞벽에 마커 2개 | 24/24 등록, 6,416점, GOOD | 둘 다 채택(17·16 프레임, RMS 0.203·0.193 px), 마커 간 차 0.043 % | **+0.391 %** | **PASS** |

**TUM fr1/desk — 정답이 있는 실제 녹화 (PUBLIC_DATASET).** 지도는 단안 RGB만으로 만들고(49 포즈), 정답 길이는 Kinect 깊이와 모션캡처 포즈에서 얻었습니다(지도 생성에는 쓰지 않음). 참조 스케일은 정답 궤적 Sim(3) 정렬값 0.23775 m/단위(ATE 2.05 cm). 규칙: 가장 긴 쌍으로 fit, 다음 두 쌍으로 검증, 검증 오차 각각 ≤ 5 %.

| 실행 | 점 집합 | fit 길이 | 참조 대비 스케일 오차 | 검증 오차 | 판정 |
|---|---|---|---|---|---|
| 1차 | 신뢰 점 전체 1,700개(뷰 ≥ 3, 깊이 퍼짐 ≤ 1.5 cm, 트랙 ≥ 4) | 3.43 m | **−11.795 %** | 6.769 % / 2.431 % | **FAIL** |
| 2차 | 책상 점 619개(트랙 ≥ 6, 뷰 ≥ 5, 중앙 깊이 ≤ 1.5 m) | 1.69 m | **−1.235 %** | 0.163 % / 1.224 % | **PASS** → `scale.json` `MEASURED_VERIFIED` |

2차의 책상 점 기준은 **1차 결과를 본 뒤에** 정했습니다(10.3절). 1차 fit 쌍의 한 끝은 중앙 깊이 3.6 m의 먼 점이었습니다.

뷰어에서 점을 찍는 오차를 흉내 낸 반복 시험(클릭 근처 희소점에 스냅, 줄자 읽기 오차 σ 3 mm, 시험마다 무작위 fit 1 + 검증 2, 각 약 2,000회):

| 점 집합 | 스냅 반경 | 최소 길이 | 검증 통과율 | 스케일 오차 중앙값 / p90 |
|---|---|---|---|---|
| 신뢰 점 전체 | 0 | 0.5 m | 72.4 % | 1.98 / 5.63 % |
| | 2 cm | 0.5 m | 61.0 % | 2.35 / 5.94 % |
| | 5 cm | 0.5 m | 30.6 % | 3.50 / 8.83 % |
| | 2 cm | 1.0 m | 70.7 % | 2.37 / 5.95 % |
| | 5 cm | 1.0 m | 47.2 % | 2.91 / 7.42 % |
| | 10 cm | 1.0 m | 21.8 % | 4.70 / 11.05 % |
| 책상 점 | 0 | 0.5 m | 91.5 % | 1.63 / 3.81 % |
| | 2 cm | 0.5 m | 76.7 % | 1.94 / 4.69 % |
| | 5 cm | 0.5 m | 35.5 % | 3.26 / 8.20 % |
| | 2 cm | 1.0 m | 96.5 % | 1.80 / 3.49 % |
| | 5 cm | 1.0 m | 60.5 % | 2.32 / 5.54 % |
| | 10 cm | 1.0 m | 23.6 % | 3.95 / 9.49 % |

읽는 법: 스냅 반경이 커질수록(점을 대충 찍을수록) 통과율이 빠르게 떨어집니다. 길이 1 m 이상, 반경 2 cm 이내에서 찍어야 통과율이 70 % 이상입니다. 이 장면은 작아서(신뢰 점 범위 3.3 × 1.6 × 1.0 m) 1 m 넘는 쌍이 적다는 한계가 있습니다.

**테니스장 항공 영상 (PUBLIC_DATASET, Zack Dowell, CC BY, https://www.youtube.com/watch?v=a5mun9m1CKY).** 29 s 전체에서 지도 85/85 등록, 63,906점, GOOD. 코트 2면(A, L)의 선 교차점을 프레임마다 다시 찾아 삼각측량하고, ITF 규정 치수에서 선 폭 5 cm를 뺀 값을 "참 길이"로 썼습니다.

| 역할 | 길이 | 규정 | 예측 | 오차 |
|---|---|---|---|---|
| fit | 코트 L 복식 사이드라인 | 23.77 m | — | 16.683 m/단위 |
| 검증 | 코트 A 복식 폭 | 10.97 m | 11.456 m | 4.906 % |
| 검증 | 코트 A 센터 서비스 라인 | 12.80 m | 12.811 m | 0.477 % |
| 검증 | 코트 L 단식 폭(먼 쪽) | 8.23 m | 8.583 m | 4.925 % |

수치로는 3개 모두 5 % 이내(PASS)지만, 규정 치수는 현장에서 잰 길이가 아니므로 **검증 횟수에 세지 않아** 상태는 `MEASURED_UNVERIFIED`입니다(10.3절). 22개 길이 전부를 보면 길이 방향(사이드라인)은 fit 대비 −0.24 ~ +0.03 %, **폭 방향은 −4.1 ~ −4.8 %**로 체계적으로 갈립니다. 모서리 각은 89.8–90.2°로 직각이 유지되고, 카메라는 코트 긴 축에 거의 수직(87.8°, 89.8°)으로 천저에서 약 35° 기울어 봅니다. 보정 없이 COLMAP이 추정한 초점거리 오차가 시선 방향으로 지면을 늘린다는 가설과 맞지만 **검증하지 않았습니다**. 어느 길이로 fit해도 다른 코트 11개 길이 중 9–11개가 5 % 이내였습니다(`fit_sensitivity`).

**공개 영상 후보 탐색.** YouTube의 Creative Commons 필터로 10개 검색어 × 60개 결과를 모으고 각 후보의 라이선스를 메타데이터에서 읽었습니다. 채택 1건(업로더 본인 채널, 6면 이상의 규정 코트 위를 옆으로 비행), 기각 사유는 재업로드 채널이라 권리 출처 불명확, 시차 부족, 코트 기하 없음, 비규정 코트, 라이선스 확인 불가 등(`public_video_candidates.json`).

**미터 깊이 모델 시험 (S6, 부록 A).** Mac mini 16 GB, MPS(torch 2.15.0), 모델마다 별도 venv, 가중치는 `outputs/tools/weights`(git 제외), 실행 시 오프라인, 프레임은 기기 밖으로 나가지 않음. TUM은 60 프레임(10번째마다), FSC·스미다는 등록 프레임 150개. 레이어 품질 기준(사전 등록): 설치된 steady 레이어 대비 떠 있는 점 비율 `float_fs` 악화 ≤ +1 pp **그리고** 모든 평가 프레임에서 바닥·구조물 `cov1px` ≥ 기준선이면 대체 후보, 아니면 참고용.

| 모델 | 가중치 라이선스 | MPS | TUM 프레임당 시간 / 최대 MPS 메모리 | TUM 깊이 스케일 오차 | FSC 판정 | 스미다 판정 |
|---|---|---|---|---|---|---|
| MapAnything (`map-anything-apache`) | Apache-2.0 | 동작 | 1.05 s / 9.15 GB | 내부 파라미터 입력 −2.27 %, 이미지만 −8.94 % | 참고용(`float_fs` +5.08 pp, 일부 프레임 커버리지 하락) | 참고용(+6.96 pp) |
| DA3METRIC-LARGE | Apache-2.0 | 동작 | 0.25 s / 7.38 GB | −4.53 % | 참고용(+12.92 pp) | 참고용(+3.36 pp) |
| DA3NESTED-GIANT-LARGE-1.1 | **CC BY-NC 4.0** | 동작 | 8.95 s / 14.80 GB | **+48.19 %**(청크마다 스케일이 약 1.0 또는 1.4–2.6, 불일치 표시) | 참고용(`float_fs` −0.65 pp지만 조정 프레임 바닥·구조물 커버리지 하락) | 참고용(+3.84 pp) |
| Pi3X | **CC BY-NC 4.0**(코드는 BSD-3-Clause) | 동작 | 1.43 s / 11.56 GB | +1.73 % | 참고용(+5.29 pp) | 참고용(+9.63 pp) |

- **대체 후보 없음.** 모든 모델이 보류 프레임 커버리지는 크게 올렸지만(예: FSC `f001720` 바닥 0.355 → 0.89–0.92), DA3NESTED/FSC를 빼면 모두 `float_fs`를 2.95–12.92 pp 높였습니다. 시험 레이어는 `outputs/archive/layers/<session>/metric_trial_*`로 옮겼고 앱 지도 폴더에는 쓰지 않았습니다.
- **항공 영상에서 모델 간 미터 스케일이 크게 다릅니다.** FSC에서 모델별 "지도 1단위당 m"가 0.75–8.23으로 **최대 10.96배**, 스미다에서 2.68–5.67로 2.11배 차이. MapAnything은 우리 포즈를 함께 주면 FSC 값이 34 % 바뀌었습니다. 정답이 없는 클립이라 어느 쪽이 맞는지는 말할 수 없습니다.
- 기준선 재현: 저장된 청크로 steady 레이어를 다시 만들면 스미다 1,367,234점(설치본과 동일), FSC 3,228,943점(설치본 대비 +0.08 %)이고 판정은 같았습니다.
- MPS 관련 수정(실행기 쪽): MapAnything의 광선→K 복원 최소제곱 한 호출만 CPU로(MPS에서 10분 넘게 걸림), DA3는 pycolmap의 OpenMP 충돌로 실행기에서 pycolmap 대체, Pi3X는 고정된 구버전 torch 대신 2.15.0으로 실행.
- **비상업 가중치(DA3NESTED, Pi3X)는 로컬 연구 실행만 했고 저장소에 포함하지 않습니다.**

### 10.3 계획과 다르게 한 것

1. **합성 E2E 배치 추가.** 2절 계획의 "서로 다른 벽 두 마커" 배치는 두 번째 마커가 3 프레임에서만, 45 px 미만으로 보여 기각되어 FAIL입니다. 기준은 바꾸지 않고, 같은 벽에 두 마커를 둔 배치를 추가해 PASS를 얻었습니다. FAIL 결과는 그대로 남깁니다. 서로 다른 벽 배치를 통과시키려면 합성 카메라 경로나 마커 크기를 바꾼 새 시험이 필요합니다(실행 안 함).
2. **규정 치수는 검증 횟수에 세지 않는 규칙.** 3.3절은 "줄자로 잰" 독립 길이를 요구합니다. 공개 영상에는 실측이 없으므로 규정 치수로 계산한 길이는 `length_source=REGULATION`, 플래그 `REGULATION_DIMENSIONS`로 기록하고 평가만 하며 검증 통과로 세지 않습니다. 그래서 테니스장은 수치상 3/3 이내여도 `MEASURED_UNVERIFIED`입니다.
3. **TUM 점 기준은 사후 결정.** 2차 실행의 "책상 점" 기준(트랙 ≥ 6, 뷰 ≥ 5, 중앙 깊이 ≤ 1.5 m)은 1차 FAIL을 본 뒤에 정했습니다. 사전 등록 기준이 아니므로 이 PASS는 "가까운 점으로 재면 맞는다"는 관찰이지, 미리 정한 시험의 통과가 아닙니다. 1차 FAIL도 그대로 남깁니다.
4. **TUM의 참 길이는 줄자가 아니라 Kinect 깊이와 모션캡처 포즈**에서 나왔습니다(수 mm 잡음, 깊이 경계 제외). 사용자 촬영 영상(S4)이 아니므로 `REAL_VIDEO` 게이트를 닫지 않습니다.
5. S0 산출물 이름이 `env_check.json`이 아니라 `opencv_aruco_check.txt`입니다. S6은 계획의 단일 `venv_metric` 대신 모델마다 별도 venv를 썼고, 복도 세션 대신 TUM(스케일 확인)·FSC·스미다(레이어 품질)를 썼습니다.

### 10.4 남은 일

- S4: 사용자가 7절대로 마커 2장과 줄자 길이 2개를 넣어 촬영한 영상으로 실행(이것이 첫 `REAL_VIDEO` 결과가 됨).
- 테니스장 폭 방향 4–5 % 체계 오차의 원인 확인: 같은 영상에 초점거리를 고정·변경한 재빌드 또는 보정된 카메라로 찍은 영상.
- 서로 다른 벽 마커 배치를 통과시키는 합성 시험(카메라 경로 조정).
- 실기체 텔레메트리는 사용자 승인 전까지 NOT_RUN.
