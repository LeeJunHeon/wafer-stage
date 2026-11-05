# wafer_stage — 카메라 센싱 + 스테이지 제어 (통합 프로그램)

이 폴더는 "샘플 자동 측정 시스템"의 ① 센싱·모터 제어 부분이다. ② 실제 측정(계측기 제어)은 backend/measure.py 인터페이스로 붙인다(장비 미정). 촬영 결과·보정 파일은 ..\data\ 에 두고 git 에 넣지 않는다.

## 목적
4인치 웨이퍼(또는 철판) 위 샘플 조각 10~16개를 천장 카메라로 검출하고, XY 갠트리가 Z 축의 공구(지금은 히트건,
나중에 프로브)를 각 샘플 위로 옮겨 레시피(측정 순서)대로 처리한다. 정확도 요구: 공구 지점이 샘플 위 ±1~2mm 안.

## 바뀐 전제 (2026-11 개편)
- 호밍 센서는 물리적으로 없앴다(V10 의 hs/hw 는 V11 에서 제거). 원점은 **사진 호밍**이 기본:
  Z 모터 윗면의 헤드 마커(ArUco id 10 · 40 mm)를 천장 카메라로 찍어 실제 XY 를 재고 sp 로 맞춘다.
  끝단 밀기 · 수동 원점 등록은 보조. Z 원점은 지금처럼 위쪽 하드스톱(끝단 이동 + 등록).
- 카메라는 천장 고정(작업영역을 수직으로 본다). **판 마커 없이 고정 보정(`calib.mode = fixed`)이 기본이다**
  (2026-11-05): 마커는 Z 모터 윗면의 헤드 마커(id 10) 하나뿐, 감지 영역은 판에 새겨진 사각형으로
  고정(촬영본 픽셀 사각형), 좌표는 한 번 저장한 판 보정(픽셀 → 스테이지 XY 호모그래피, 점 4개 이상)
  으로 낸다. 카메라를 옮기거나 해상도를 바꾸면 다시 보정(보정 창 → 감지 영역 · 판 보정). 카메라 번호 ·
  해상도가 저장 때와 다르면 무효 → 촬영은 하되 좌표를 만들지 않고 경보 배너. 예전 판 마커 4개 방식은
  `calib.mode = markers` 로 코드 그대로 남아 있다. USB 케이블이 불안정 — 촬영 재시도 · 미리보기
  재개방으로 견딘다(고해상도는 첫 프레임 대기 · 재시도 간격을 늘린다).
- Z 축에 히트건(노즐이 비스듬해 가열 지점이 노즐 바로 아래가 아님). 공구는 바뀐다는 전제 —
  공구마다 오프셋(dx, dy)과 작업 높이. 스테이지 좌표 P = 기준 포인터가 가리키는 점, 공구 끝 = P + d,
  샘플 S 를 겨냥하려면 P = S − d, 가동범위 검사는 P 로.
- 촬영 위치(capture_xy)를 파킹(park_xy)과 분리 — 히트건이 웨이퍼를 가리지 않는 좌측 끝 쪽.
- 릴레이 10개(R1~R10, D22~D40 짝수)가 히트건 · 쿨러 등 장비 전원. 실장 확인 결과 HIGH 에서 켜짐
  (RELAY_ACTIVE_LOW = false). 전원 투입 시 전부 꺼짐, 명령으로만 켜짐.
- 측정은 샘플마다 [이동 → Z 하강 → 릴레이 켜기 → 대기 → 측정 → 끄기 → Z 상승] 같은 레시피.
  순서·시간은 자주 바뀐다 → 설정의 측정 순서 탭에서 편집. 계측기는 미정(measure.py 더미).
- 측정 중에는 히트건·모터가 샘플을 가린다 → 샘플 위치는 측정 전 한 번 촬영, 측정 중 위치 확인은
  헤드 마커로.

## 구조 (2026-09-10 재구성)
서버가 상태의 주인이고 화면은 HTML 이다. 가스 센서 프로그램(Gas_Sensor_Measurment_System\Program)과 같은 골격.
- run.py: 진입점. 루트에 있는 유일한 .py (`python run.py [--dry] [--image P] [--no-window] [--port N]`)
- core/: 장비·검출 핵심(검증 끝난 모듈, API 유지). 서버·화면을 모른다.
  paths.py imgio.py camera.py detect.py calib.py plate.py stage.py   (plate.py = 고정 보정: 감지 영역 + 판 보정 + 검출 호출)
  CLI: `python -m core.calib fit|samples|check` · `python -m core.stage st|mx|my|save`
- backend/: server.py(FastAPI·라우트·/ws·lifespan·CLI) window.py connection.py state.py
  settings_schema.py(운용 설정 스키마) recipe.py(레시피 단계) relays.py photohome.py(사진 호밍·보정)
  platecal.py(고정 보정 명령: 감지 영역 · 판 보정 · 단발 촬영)
  commands.py engine.py stagectl.py vision.py measure.py loops.py logger.py storage.py version.py
  backend 안끼리는 `import engine` 처럼 이름으로, core 는 `from core import calib` 로 부른다.
- frontend/: index.html · css/style.css · js/{core,camera,map,samples,sequence,jog,relays,editors,
  settings,calib,plate,app}.js (core.js 가 UI.h · 창 체계를 먼저 정의한다)
  화면은 1920×1040 고정 캔버스(fit() 로 축소, 페이지 스크롤 없음). ISA-101 계열 배색
  (무채색 바탕, 색은 상태에만), 상단 상태줄·경보 배너·하단 명령, 스테이지 맵으로
  기계 위치를 보여준다. 단계(1·2·3) 표시는 쓰지 않는다 — 반복 운전 프로그램이다.
  인라인 style 을 쓰지 않는다(클래스로 뺀다).
  마우스 오버 설명은 요소에 data-tip 속성으로 단다(core.js 가 좌표로 찾아 띄운다).
  툴팁 문구만 "…합니다" 체 1~2문장이다. 버튼 크기는 .btn(34px)·.btn.sm(28px)·
  .btn.lg(42px) 셋뿐이고 전체 폭은 .block.
  화면 문구는 명사형·짧게("…하세요/…합니다" 금지, 구분은 " · "). 화면에 가는 오류
  (stage.last_error·camera.last_error·sequence.message)는 한 줄 60자 이내 —
  logger.short(e) 를 거치고 원문은 파일 로그에만. 대화상자 본문만
  "…하십시오" 체. 파일명·모듈명·펌웨어 명령(fz 등)은 화면에 쓰지 않는다.
  카메라 패널은 미리보기/촬영본 토글. 미리보기는 GET /preview.jpg 를 preview_fps(기본 4) 주기로 받는다.
  **창(설정·보정·확인·공구 선택)은 showModal 금지** — show() + 막(#dimmer) + 비상정지 도크(#estopDock,
  막 위). core.js UI.openWin/closeWin 으로만 연다. 비활성 버튼은 UI.why 로 사유를 툴팁에 붙인다.
  화면 넘침은 playwright 로 1920×1040 스크린샷을 찍어 확인한다(칩·버튼 잘림 검사).
- tools/: 개발·검증용. 프로그램 실행에는 필요 없다.
  `python tools/regress.py`(검출 개수 회귀) · `python tools/e2e_smoke.py`(전 흐름)
  · `python tools/flat_check.py DIR...`(평탄화 전·후 검출 비교)
  · `python tools/cam_probe.py`(카메라 포맷·해상도 실측. 앱 끄고. 못 열면 exit 2)
  e2e_smoke 는 임시 폴더를 만들어 WAFER_STAGE_DATA 로, 케이스마다 임시 설정 파일을
  WAFER_STAGE_SETTINGS 로 넘긴다 — 실제 data/ 와 저장소 settings.json 에 쓰지 않는다.
  `python tools/e2e_smoke.py 릴레이 레시피` 처럼 케이스 이름 일부로 골라 돌린다.
  사진 호밍 검증은 WAFER_STAGE_DRY_CAMERA=synth(tools/synth_cam.py 합성 카메라 + dry 탈조 흉내).
- firmware/stage_v11/(현재, v8~v10 은 기록), assets/
- 미리보기: backend/vision.py 의 CameraHolder 가 카메라 객체 하나를 lock 으로 공유한다.
  미리보기 스레드가 preview_fps 로 읽고 촬영도 같은 객체를 쓴다 - 촬영 때 닫았다 다시 열지 않는다.
  연속 실패 cam_fail_limit 면 닫고 cam_reopen_s(두 배씩, 최대 cam_reopen_max_s) 뒤 다시 연다.
  촬영 실패는 닫고 다시 열어 capture_retries 번까지 다시 찍는다. 단발 촬영(vision.single_shot)은
  사진 호밍·헤드 보정·확인 촬영용(브라케팅·검출 없음).
- 촬영 순서: [노출 브라케팅 → Mertens 융합](camera.capture_bracket,
  설정 bracket/bracket_exposure/bracket_settle_s) → 마커로 감지영역 → [종이 기준 조명
  평탄화](core/flat.py, 설정 flat_field) → 마커 재계산·검출은 평탄화본으로.
  **fixed 에서는** 마커·평탄화를 건너뛴다(종이 기준이 없다 — 로그 "평탄화 건너뜀 · 고정 보정"): 융합본에서
  저장한 감지 영역 사각형을 crop(밖은 안쪽 판 색으로 채움) → 긴 변이 detect_max_px 보다 크면 줄여 검출 →
  좌표를 원본으로 되돌려 판 보정으로 mm. detect 는 철판 모드 기존 인자(surface=plate, plate_mm_per_px =
  국소 mm/px ÷ 축소 배율, marker_mm=0, plate_margin_pct=0)만 쓴다 — 검출 로직은 고치지 않았다.
  **fixed 기본은 고정 노출 1장**(bracket 끔 — 함체 안 조명이 일정하고 감지 영역 포화 0%). exposure 0 이면 첫 촬영에서
  자동 노출 수렴값을 읽어 세션 동안 재사용(설정 [노출 다시 찾기] = 명령 exposure_refind). 브라케팅을 켜도 융합은 감지 영역
  crop 에만, 헤드 마커는 첫 장 원본으로. capture_frames 기본 3, 못 받으면 받은 만큼 진행("요청 n · 받음 m").
  촬영마다 단계별(노출·안정 대기·프레임·융합·검출·헤드 마커·저장) 소요를 로그에 남긴다.
  촬영 진행은 서버가 state.camera.cap_text 로 푸시(화면이 추측하지 않음), 실패는 cap_error{id,text} — 카메라 패널 막 +
  상단 상태 필 "촬영 중" + [촬영 · 검출] 비활성·스피너. calib.est_mm_per_px 기본 0.143(판 사각형 눈금 10 mm ≈ 70 px).
  **검출만 모드**: 감지 영역만 유효하고 판 보정(H)이 없으면 calib.est_mm_per_px 로 검출하고 X/Y 는 None
  (상태 nocoord, state.calib.area_only) — 이동·순회·클릭 추가는 거절("판 보정 필요"). 감지 영역도 없으면
  검출 없이 data/out/<시각>_nocal/ 에 raw(+fused)만 저장. plate.validity(need_h) / area_validity 로 나뉜다.
  맵은 fixed 에서 판 마커를 그리지 않고 area_mm(판 보정이 있을 때) 사각형을 점선으로.
  raw.png 는 첫 장(가장 밝으면서 포화되지 않은 장) 원본, fused.png·flat.png 를 같은
  폴더에 남긴다. 노출 목록은 자동(bracket_exposure 비움 = 기본): AE 끄고 종이(밝은 상위
  20%) 평균이 200~235 가 되게 한 단계씩 맞춘 첫 장 → 한 단계씩 내려 최대 3장, 장 평균
  <25 면 버리고 멈춤. 고정 -4/-6/-8 은 194/44/1 이 나와 셋째 장이 검었다. --image 는
  브라케팅 없이 그 사진(평탄화는 한다). 카메라가 노출을 무시하면(장별 밝기 차 <10)
  또는 2장 미만이면 단일 촬영으로. 융합은 자체 Mertens(camera.fuse_exposures): 가장 밝은
  장에서 245 이상인 화소는 그 장의 가중치 0. 장별 원본을 bracket_N.png 로 남긴다.
  포화 진단은 평탄화 '전' 사진에서 잰다. 평탄화는 포화 화소를 255 로 남긴다(안 그러면
  검출의 포화 제외가 무력화된다). regress.py 도 같은 전처리.
  한쪽에서 강한 빛이 들어와 자동 노출이 요동(웨이퍼 평균 36~162, 한 장은 31% 포화)한
  것에 대한 대응이다. 검출 임계값은 사진이 일정해진 뒤 따로 맞춘다.
- 카메라 설정: width/height/fourcc(MJPG|YUY2) · wb_temperature(0 = 기본. AUTO_WB 끄고 값
  안 주면 초록끼) · exposure(0 = 자동, 값 주면 auto_exposure 꺼짐 - 화면은 둘을 따로 못
  고른다) · focus_mode(auto|fixed)·focus·focus_sweep(고정 모드에서 열 때 스윕). 카메라 키가
  바뀌면 holder.reopen.
- 시리얼 원문은 core/stage.py 의 on_line 콜백 → connection.push_log_threadsafe 로 화면에
  간다(level tx/rx). 폴링(st/ST · rs/RS)은 표시만 달아 보내고 숨길지는 화면이 정한다.
- 수동 이동(조그) 팝업은 show() 로 연다 - showModal 이면 뒤 화면이 inert 가 되어
  비상정지를 못 누른다. 조그는 ack{of:"jog"} 를 받고서야 다음 스텝을 보낸다(한 번에
  하나). 목표 좌표와 가동범위 자르기는 서버가 한다.
- 명령은 줄 세우지 않는다: ws_endpoint 가 메시지마다 create_task. 비상정지는
  handle_command 맨 앞에서 검사 없이 처리하고 POST /estop 우회로도 둔다(2026-09-11 사고:
  원점 탐색 69초 동안 비상정지 3건이 소켓에서 기다렸다). 이동 중이면 이동 명령은 거절.
- 이동 후 autosave_s(기본 3초) 조용하면 자동 save(디바운스). USB 절전으로 끊기면 원점을 잃는다.
- 사진 호밍(photohome.py): 지금 자리에서 한 장 → 헤드 마커 → 헤드 보정(data/head_calib.json, 판과
  다른 평면이라 전용 호모그래피) → 실제 XY. d < 허용(0.3) 그대로 / ~경고(5) 자동 sp / 넘거나 원점
  없음이면 확인 후 sp(서버가 잰 값, 120초 · 이동 없을 때만). sp 뒤 save · reset_estop(원점 복구 수단).
  카메라 번호·해상도·마커 번호가 보정 때와 다르면 무효 → 막는다. 촬영 전 자동 확인은 경고만.
  판 마커와 무관(헤드 마커 하나로 동작). 마커가 화면 가장자리(photohome.edge_margin_px, 0 = 짧은 변의 3%)에
  걸치면 결과를 쓰지 않고 경고, 헤드 보정 격자 점 중 그런 점은 건너뛴다. photohome.on_connect(기본 켬):
  연결 직후 헤드 보정이 유효하면 이동 없이 한 장 찍어 같은 판정 규칙으로 위치를 판단(마커 안 보이면 알리기만).
- 수동 원점(보조)은 사람이 축별로 잡는다: 끝단 이동(touch_end, 밀기만) → set_origin(origin_gap_mm 이격 후 z).
  원점은 축마다 따로다(HX/HY). 가동범위를 벗어나면 펌웨어가 그 축 원점을 푼다.
  비상정지 때 forget 을 보내 EEPROM 의 원점 기록도 지운다.
  끝단 이동(touch_end)과 원점 등록(set_origin)은 따로다. 축마다 따로 잡는다.
- 메시지 계약(state/명령)은 README.md 에 표로 있다. 화면은 요청만 보내고 서버 state 가 와야 바뀐다.
- **settings.json 은 장비별 로컬 파일(git 제외, .gitignore)**. 기본값은 스키마(settings_schema.py,
  검출 파라미터는 detect.DEFAULTS)이고 파일이 없으면 기본값으로 시작, 설정 창 첫 저장 때 만들어진다.
  예시는 settings.example.json. e2e_smoke(케이스마다 빈 설정 + 패치) · regress(기본값만, --settings 로 로컬 파일)는
  로컬 파일에 의존하지 않는다.
- **설정 스키마 규칙: 운용 값은 backend/settings_schema.py 에만 기본값을 둔다**(코드에 숫자를 박지
  않는다). 항목 = key(점 표기)·default·type·min/max·step·unit·tab·group·label·tip·apply·advanced·
  validate. state.DEFAULT_APP/APP_KEYS 는 스키마에서 만든다. 저장은 전부 검증 통과해야(부분 저장
  없음) 원자적으로 쓰고 직전본을 data/settings_backup(20개)에. 예전 settings.json 은 migrate 로 읽고
  모르는 키는 보존. 설정 화면은 스키마로 자동 생성 — 새 설정은 스키마에 항목만 더하면 화면이 생긴다
  (목록·표 같은 것은 frontend/js/editors.js 에 전용 편집기). 검출 파라미터만 settings.json 직접 편집.
  안전상 사용자가 바꾸면 안 되는 펌웨어 타이밍(펄스 폭 등)은 스키마에 넣지 않는다.
- 레시피 엔진(engine._run_loop + RUNNERS, 정의는 recipe.py): 샘플마다 단계 실행, steps.jsonl 기록.
  **안전 불변식**: X·Y 이동 전 항상 Z 맨 위(Z 원점 있을 때, goto_xy·단계·순회 후 이동 전부) ·
  Z 원점 없으면 z 단계 거절(시작부터) · 비상정지는 아무것도 안 움직이고 save 안 함(릴레이 정책만) ·
  정지는 지금 단계 즉시 끊고 릴레이 정책 → Z 맨 위 → after_run → save · 일시정지는 단계 사이(대기는
  남은 시간 멈춤). 흐름(CAPTURE_FLOW · POST_RUN_FLOW)은 설정으로 조합 — 하드코딩하지 않는다.
  조그만은 Z 를 올리지 않는다(공구 끝 맞추기). 순회 밖의 긴 작업(헤드 보정·공구 가열)은
  engine.run_job 으로 띄워 busy() 에 잡힌다.
- 릴레이(relays.py): 펌웨어 RS 가 진실. 사용 안 함 켜기 거절 · 순회 중 수동 거절(전체 끄기만) ·
  정책 off|keep(비상정지·정지·오류·종료) · 비상정지는 '!' 와 같은 쓰기에 "r all 0" · 감시 시간 1/3
  주기 rs 하트비트(이동 중 제외) · max_on_s 초과 자동 끄기 + 경보 · "[!] 통신 끊김" 은 경보 배너.
- **판 보정 두 방식**(core/plate.py solve_record · backend/platecal.py): 점 클릭(기존) + 샘플로 맞추기. 근사 변환(감지 영역 = area_size_mm 154 정사각형 + 축 방향(헤드 보정 | 자동 측정)) →
  1점 오프셋 / 2점+ 닮음 / 4점+ 퍼짐 호모그래피. 같은 points 목록(src sample|click), 근사 상태는 순회 금지 · 샘플 이동은 확인. [이 자리로 보정]은 핀으로만(활성 공구가 핀이
  아니면 거절), 순회 중엔 일시정지에서만. 이상점(중앙값 n배) · 카메라 이동 의심(연속 2) 확인, 이력 plate_calib_history 20개.
- **측정 과정**(backend/devices.py · engine 단계 실행기): 온도(TempSource) · 접촉(ContactSource) 드라이버 인터페이스 + 더미(열 모델 시뮬레이터, sim.time_scale 로 빠르게).
  쿨러 구역(cooler.zones 3×3 → 샘플 cooler) · 레시피 단계 cooler/contact/wait_temp/heat/cool/repeat/log · 기록 CSV(sample_NN_log.csv). **안전 불변식**: X·Y 이동 전
  히트건 꺼짐(engine._goto 강제) · 정지/오류/비상정지는 릴레이 정책과 무관하게 히트건 끔 · 과열 보호(heater.max_c · 센서 오류 연속)는 레시피와 무관. 실제 장비에서
  온도 드라이버가 더미면 가열/온도 단계 레시피는 시작 안 함. 활성 레시피 기본은 "기본", "가열 측정"은 임시값 예시(장비에서 측정 후 조정).
- **촬영본 표시 규칙**: 라벨 한 벌(번호만, 좌표 숫자 없음) · 칩/윤곽/다른 라벨과 안 겹치게 바깥 빈 자리 + 지시선(core/labels.py = js placeLabels) · 윤곽 얇게 색은 상태에만 ·
  번호는 새 촬영의 첫 매김에만 위치 순서(행 우선; 격자면 격자 인덱스) · 감지 영역 밖/제외 영역에 중심이 있는 검출은 결과에서 뺀다 · 빈 판 기준은 새로 찍을 때
  data/plate_ref_history 에 보관(10개)하고 보정 창에서 되돌린다(메타 불일치는 무효 표시 + 확인).
- **공구 끝 모양**(core/tip.py): 공구마다 tip_shape/tip_w_mm/tip_h_mm/tip_angle_deg(**스테이지 기준** 0 = 긴 변이 스테이지 X 축; 그릴 때 판 보정으로 국소 방향을 사진 각도로 변환,
  판 보정 전에는 tip_angle_img_deg 예비값; 판 보정이 생기면 사진 방향 → 스테이지 각도를 한 번 채움). 촬영본 [공구 끝]
  토글이 순회 대상 샘플마다 실제 크기로 그리고(배율 = 판 보정 국소 배율 · 없으면 감지 영역 크기 배율 calib.area_size_mm), engine.refresh_fit
  이 샘플별 fit(윤곽 안 = tool.fit_margin_mm 까지 허용)을 계산 — 벗어나면 "핀 벗어남" 배지 · needs_confirm. 예측 · 윤곽 없는 샘플은 검사 안 함.
  수동 추가는 sample_add.snap 이면 fixed_detect.snap(빈 칸 재탐색 로직)으로 클릭 주변 칩 중심에 붙는다. 샘플 끌기 = sample_move
  (편집 스택 op move · source manual · 되돌리기). **검출만 모드에서도 추가 · 이동은 픽셀 좌표로 된다**(mm · 이동 · 순회는 판 보정 뒤,
  판 보정 적용 때 engine.recalc_from_plate 가 mm 계산).
- 샘플 편집(engine add_sample_px·add_sample_here·delete_sample·undo_edit, 스택 state.edits):
  번호는 다시 매기지 않는다(지운 번호는 빈 번호, 추가는 state.next_no - 되돌려도 줄지 않음).
  변환은 이번 촬영 보정(_CAL_CACHE)으로만 - _last_cal() 은 옛 보정으로 떨어지므로 쓰지 않는다.
  samples.json 은 검출 원본(regress --update 가 고친다), 편집본은 samples_edit.json(source 열).
  순회 중(파킹 포함)·촬영 중·촬영 결과 없음이면 거절. 새 촬영이 편집을 비운다(남아 있으면
  capture 가 needs_confirm).

## 하드웨어 (실측 확정값)
- 컨트롤러: Arduino Mega 2560 + 자작 펌웨어 V11(firmware/stage_v11). USB 시리얼 115200, 줄끝 \n. COM7.
- 드라이버: 모터뱅크 MSD-224 ×4(2상, 1/8분주 = 1600펄스/회전). ENA LOW = 동작
- 축: X = 모터 2개 동기(갠트리 양측, 논리축 하나) / Y = 빔 위 캐리지 / Z = 히트건 장착(나중에 프로브 추가·교체)
- 핀(V10~, 드라이버 번호 기준): 드라이버1=X1 ENA39 DIR41 PUL5(PE3) / 2=X2 ENA43 DIR45 PUL4(PG5) / 3=Y ENA47 DIR49 PUL3(PE5) / 4=Z ENA51 DIR53 PUL2(PE4).
  축↔드라이버는 .ino 맨 위 배정 블록 AXIS_*_DRV 4줄(확정 2026-10-26: X1=1 X2=2 Y=3 Z=4).
  PUL 포트·비트는 setup 에서 digitalPinToPort 로 계산·캐시, 같은 포트 축은 한 번의 쓰기로 동시 출력.
  X1·X2 는 다른 포트(PE·PG)라 한 cli 구간 안 연속 쓰기 두 번으로 나간다(시차 1µs 미만).
- 릴레이 R1~R10 D22~D40 짝수(RELAY_ACTIVE_LOW = false — 2026-10-26 실장 확인, HIGH 에서 켜짐,
  KOINO KH-103-4CL DC24V 소켓형. 부팅 시 꺼짐 레벨 쓰기 → OUTPUT, 펌웨어는 !·forget 때 안 건드림).
  호밍 센서는 없앴다(D23~D37 홀수는 V11 에서 설정하지 않는다).
- 스케일: 160 펄스 = 1mm. 스트로크 X 0~39620(247.6mm), Y 0~39640(247.8mm). 속도 v6000/a40000/b300 ≈ 37.5 mm/s
- 가동범위는 두 겹: 펌웨어 X_MAX/Y_MAX/Z_MAX 는 기계 한계, 앱은 settings.json "limits"
  (x_max_mm·y_max_mm·z_max_mm, 기본 247.6·247.8·45)로 실사용 범위를 정한다.
  core/stage.py 의 *_MAX_PULSE 는 기본값일 뿐이고 set_limits 가 런타임에 바꾼다 —
  다른 모듈은 값을 복사하지 말고 stage_mod.limits_mm() / stagectl.limits_mm() 로 읽는다.
  calib.AXIS_MAX_X/Y 도 calib.set_limits 로 같이 따라간다(state.apply_to_core).
  Z 를 내리는 깊이는 공구마다의 작업 높이 tool.items[].z_work_mm(≤ Z 가동범위, 예전 z_measure_mm 은
  히트건의 작업 높이로 이행). 레시피의 z 단계가 쓴다.
- 좌표계: 각 축 모터 쪽 끝(하드스톱 origin_gap_mm 2mm 이격) = 0. 스테이지 좌표 = 기준 포인터가 가리키는 점.
  파킹(park_xy, 기본 (0, 90)) = 끝나고 쉬는 자리, 촬영 위치(capture_xy) = 찍는 자리(히트건이 웨이퍼를
  가리지 않는 좌측 끝 쪽 — 사용자가 설정에서 입력).
- Z 규약: Z=0 이 맨 위(들어 올린 자리), 값이 커질수록 아래(펄스 부호도 +가 아래).
  원점은 위쪽 하드스톱으로 밀어 잡는다 - Z 의 끝단 이동은 '위로' 다.
  Z_MAX 7200(45mm)·Z_PPMM 160 둘 다 실측 확인(2026-09-14 · 스트로크 2026-09-16 재실측).
  고치면 펌웨어·core/stage.py(기본값)·state.DEFAULT_APP["limits"] 셋 다.
- X·Y 이동은 언제나 Z 를 먼저 0(맨 위)으로 올린 뒤(엔진 불변식 — 좌표 이동·샘플 이동·원점 복귀·
  파킹·촬영 위치·순회). Z 원점이 없으면 경고만 하고 X·Y 만 간다(z 단계 레시피는 시작 거절).
  수동 조그는 Z 를 올리지 않는다.
- 리밋 스위치 없음 → 소프트 리밋 + EEPROM 위치 기억
- 카메라: 천장 고정(이전보다 90° 돌린 방향, 2026-11-05), USB index 1, **기본 4080×3060 MJPG**(4:3 = 센서 전체
  화각, 윈도우 카메라 앱과 같다. 실측 detail: 1280×720 0.7~0.9 / 2560×1440 2.98 / 3840×2160 2.8 /
  4080×3060 1.8~2.4 / 8160×6120 0.67 = 보간이라 쓰지 않는다. 16:9 모드는 위아래가 잘려 화면 위 끝의 헤드
  마커가 잘린다). 초점은 고정 + 스윕(자동초점 헌팅 방지). USB 케이블 불안정 — 촬영 재시도 · 미리보기
  재개방. 위치가 바뀌었으니 판 보정(감지 영역 포함)·헤드 보정을 다시 잡는다.
  해상도·포맷(width/height/fourcc)은 설정 창에서 고른다([최대 해상도로] 버튼). 미리보기는 원본을 줄여
  (preview_max_w) 보내지만 촬영·마커 검출·판 보정 클릭 좌표는 원본 해상도 기준이다. 카메라는 못 하는 요청을 조용히
  가까운 값으로 바꾸므로 열 때마다 실제 포맷을 로그에 남기고 다르면 경고한다
  (camera.format_text/format_mismatch). 표기 화소 수(5000만)는 보간이라 믿지 말고
  tools/cam_probe.py 의 detail 지표(1/2 축소→복원 RMS)로 실측해서 정한다.
- 판 마커(**markers 모드 전용** — fixed 에서는 붙이지 않고 화면에 있어도 무시): ArUco DICT_4X4_50 30mm, id0~id3 을 베이스에 부착. 중심 기계좌표는 settings.json 의
  marker_mm_xy(옛 실측 id3 (15,20) / id2 (15,160) / id1 (201,19) / id0 (201,160) — 카메라 이전 뒤
  보정 창 → 판 마커로 기준 포인터를 맞춰 다시 기록)
- 헤드 마커: ArUco id 10 · 40mm(설정 photohome.*), Z 모터 윗면. 판 마커 번호와 겹치면 저장 거절
- 캐리지 앞 기준 포인터(스테이지 좌표가 가리키는 점)

## 아두이노 프로토콜 (V11 — V10 명령·출력 그대로 + 통신 감시, 호밍 센서 제거)
- 원점: `fz x <탐색펄스>` / `fz y` / `z zx zy`(현재 위치=0) / `sp <x> <y>` / `home`
  · 수동: `jx <±펄스>` `jy` `jz` (원점 없어도 움직인다 - 사람이 끝단까지 몰 때)
  · Z: `fz z` / `zz`(현재 위치=0) / `mz <mm>` / `gz <펄스>`
- 절대 이동: `gx <펄스>` `gy` `gz` `g <x> <y>` / `mx <mm>` `my` `mz`
- 상태: `p`(사람용) / `st` → `ST X= Y= X2= Z= HX=0|1 HY=0|1 HZ=0|1 DIRTY=0|1`
- 저장 `save` / 비상정지 `!` / 기타 `v a b w`, `e 0|1`, `forget`
- 릴레이 `r <1~10> <1|0>` · `r all 0` · `rs` → `RS R1=0 … R10=0` / 통신 감시 `wd <ms>`(0 = 끔) — 마지막 줄 수신
  뒤 ms 가 지나면 켜진 릴레이 전부 끄고 `[!] 통신 끊김 · 릴레이 전부 꺼짐`. 블로킹 이동 중에는 판정 안 함,
  명령이 끝나는 순간 타이머 재시작 / 위치 세팅 `sp <x펄스> <y펄스>`(사진 호밍) / 속도 `v a b`(앱이 motion.* 로)
- 이동 완료 보고: `  X  + cmd=16000 out=16000 t=2783.53ms | X=16000 Y=8000 Z=0` → `" | X="` 가 완료 신호
- 이미 목표면 `  이미 그 위치`(보고 줄 없음) / 거부 `  [거부] ...` / 경고 `[!]`
- 원점 없으면(HX·HY·HZ=0) fz·j* 외 모든 이동 거부. 이동은 블로킹. save 없이 껐으면 부팅 시 원점없음.
- EEPROM 매직 POSA(V10 부터, V11 도 같음 · 형식은 V9 와 같음). V9 이하 기록은 '기록 없음' 으로 읽힌다.
- 주의: pyserial 의 `timeout` 에 값을 대입할 때마다 포트가 재설정된다 — 매 읽기마다 대입하면 들어오던 응답이 사라진다(core/stage.py `_readline` 주석).

## 고정 환경 검출기 (core/fixed_detect.py — fixed 모드 기본, 2026-11-05)
- fixed 모드 검출은 `core/fixed_detect.py`(설정 fixed.detector = fixed | legacy). **core/detect.py 는 markers 모드 전용**으로
  유지하고 고치지 않는다(아래 "검출 파이프라인"은 detect.py 의 것).
- 파이프라인: Lab 채널별 (가우시안 σ1.5 → 채널 − medianBlur(칩 긴 변 ×3)) 잔차 → 채널 강건 z(MAD×1.4826, MAD 하한 1.0) →
  d=√Σz² > seed 4.5 → open/close → 연결요소 → 모양 필터(면적 · 볼록도 · 종횡비 · 최소외접사각형 채움률) → 칩 크기 자동 추정 후
  재검출 → 3x5 격자 RANSAC + 최소제곱으로 15칸 예측 → 빈 칸만 칸 창에서 낮은 기준 재탐색 → Lab Canny → 예측 위치(weak).
  샘플 `source` = detected | local | predicted(weak → 확인 모드). 제외 영역(plate_calib.json `exclude`) · 빈 판 기준(선택 · 미검증).
- **튜닝 규칙이 바뀌었다**: 이 모듈은 특정 사진 오버피팅 금지 대신 **실제 운용 환경 사진(data/truth)에 맞춰 튜닝**한다. 단 여러 장으로
  검증하고 `python tools/fixed_regress.py`(기준보다 나쁘면 exit 1) · `--loo`(한 장 빼고 맞춘 값으로 뺀 장 채점)로 한 장 과적합을
  확인한다. 정답은 앱의 샘플 편집 → `tools/make_truth.py`. 파라미터는 전부 스키마(fixed.*, 검출 탭 고급).
- 운용 카메라 기본(스키마): 4080×3060 MJPG · 노출 -5 고정 · 화이트밸런스 5000 K 고정 · 브라케팅 끔 · 초점 고정. 근거: 밝은 노출일수록
  회색 칩이 판에 묻혀 나빠지고(종이 249 의 140936 최악, 어두운 140835 최선) 화이트밸런스 미고정이면 색이 초록 · 노랑으로 흔들린다.

## 검출 파이프라인 (검증 완료·고정. 순서 바꾸지 말 것)
무채색 마스크(채도 상한을 훑어 후보 경쟁) → OPEN → 최대 연결요소 → 구멍 메우기 → CLOSE → 광선 스캔 → fitEllipse.
색 후보와 엣지 후보를 '지지율 × 링 대비' 로 경쟁시켜 원판을 고른다. ROI = 축소 타원 ∩ 침식 볼록껍질 ∩ 직선 컷.
통계 씨앗(σ=stat_blur 블러 → Lab 채널별 중앙값 배경(nearest-fill) 잔차 → 채널 잡음 σ(ROI 잔차 MAD×1.4826, 하한 1.0 = 8비트 Lab 양자화 한 단계) → d²=Σ(r/σ)² > stat_seed 25 / stat_grow 12, χ²(3); 포화 픽셀 제외) [+ legacy_seed 일 때만 옛 편차 %·채도 씨앗 OR] → 볼록도 ≥ min_solidity 0.70 → 부호 분리(L 잔차 부호로 붙은 덩어리 가르기. 통과 덩어리는 둘 이상 나올 때만, 띠에 닿은 저볼록도 덩어리는 부호 분리만) → rescue/split(홈 짝 선택·ㄱ자 단일 홈 컷·재귀) → 병합 → 모양 판정 + 얼룩·림 관문 → 색거리(dE) 보조 경로 → 엣지 보완(3.7, 새 샘플 없음) → 엣지 제안(3.8, 새 샘플. edge_propose).
통계 씨앗이 기본이고 옛 씨앗(seed_pct·grow_pct·sat_delta)은 legacy_seed(기본 false, 개발용)로만 OR 한다(2026-09-21). 앱 경로(fused.png)·143334 정답 16개로 채점: 통계만 16·0/15·0/15·0, 기존 OR 통계 16·0/15·1/13·0, 기존만 15·0/14·1/13·0 (143334/143454/142900 적중·오검출) - 옛 잡음 씨앗이 어두운 사진에서 칩을 덩어리에 묻고 143454 에서 유령 조각을 만든다. dE 보조 경로(3.5)는 그대로.
얼룩·림 관문(_emit, 모든 경로): 웨이퍼보다 어두운 후보(L 잔차 중앙값 < 0)는 직선성(approxPolyDP 5% 둘레/컨투어 둘레) ≥ dark_straight_min · 꼭짓점 3~dark_vertices_max(6). 원판 경계에서 반지름의 dark_rim_pct(3%) 안에 중심이 있으면 버린다. 직선성 0.85 는 143334 의 진짜 칩(마스크 윤곽 0.72·0.82·0.85)을 떨어뜨려 0.70 으로 완화했다 - 143454 의 오검출 둘(림 삼각 11mm² 0.82 · 얼룩 9.5mm² 0.75)은 그래서 못 거른다.
샘플마다 strength(덩어리 안 d² 중앙값 · 엣지 제안은 지지율)를 남기고 절대 기준으로 weak 를 붙인다: strength < stat_seed×weak_strength_k(2) · 엣지 제안 지지율 < weak_support(0.75). (annotated 자홍 '?', 목록 배지) - confirm 에서 사람이 본다.
엣지 제안(3.8): 같은 재질의 짙은 회색 칩은 면 대비가 0(Lab 잔차 < 잡음)이라 가장자리 선만 보인다. σ=1 블러한 L·a·b Canny OR(edge_canny 사다리 전부. 둘째 단계부터 직선성 edge_straight_loose 0.90) → 3×3·5×5 닫기 → 컨투어 → 볼록껍질 → 면적 edge_min_area_mm2(25)~max → 꼭짓점 3~edge_vertices_max → 직선성 ≥ edge_straight_min 0.85 → 엣지 지지율(껍질 둘레 2px 안 엣지 비율 ≥ edge_support_min 0.6) → 볼록도(edge_solidity_min. 지지율 ≥0.85 면 면제 - ㄷ자 열린 엣지) → 안쪽 비어 있음(엣지 비율 ≤ edge_inner_max, d² 중앙값 < stat_grow) → 안쪽이 웨이퍼보다 밝지 않음(밝은 얼룩 제외 - 143334 가운데 얼룩) → 포화 화소 3px 안 없음 → 기존 샘플과 겹침 ≤ edge_overlap_max(양방향) → 확정 샘플 경계에서 edge_min_gap_mm(1.5) 밖 → 빌린 엣지 제외(둘레를 받치는 엣지 성분이 다각형 3px 팽창 밖에 edge_borrow_max 30% 넘게 걸치면 지지에서 뺌) 뒤에도 지지율 ≥ edge_support_min → 테두리 띠에 닿지 않음. 들어온 샘플은 edge_only 'G' + "found by edge only; verify". 빌린 엣지 = 황갈색 칩의 변 + 얼룩 경계로 닫힌 빈 자리(143334·143454 X105.6 Y84.3).
같은 재질 칩은 면이 아니라 가장자리로 찾는다 - 조명은 낮은 각도(측광)가 유리하다. 반사광 경계는 가장 강한 엣지라 그 언저리 제안은 버린다.
면적 임계는 mm². 실패해서 반복 금지: CLOSE 먼저 / 배경 블러 커널=샘플 크기 / Otsu / top-hat / 꼭짓점 수로 모양 거르기 / 화면 밖 광선 끝점 / 이미지 위 한글 / 오버레이 패널 크기 상수 / 채널 잡음 하한 0.5(a·b 가 양자화돼 MAD 0 → 옅은 색조 전부 검출) / 연결요소를 처음부터 L 부호로 분리(배경과 밝기가 같은 칩이 화소 부호 잡음으로 조각남 - 142900 D 소실) / 엣지 제안의 직선성 문턱만으로 얼룩 거르기(얼룩 0.87 vs 칩 0.86~0.95 겹침 - 면적 하한·사다리 폴백으로 거른다) / 엣지 제안에서 Canny 사다리 전부 돌리기(느슨한 문턱이 얼룩 둘레를 닫는다).
surface 모드 wafer/plate/auto. 검출 로직을 고치면 반드시 `python tools/regress.py` 로 개수 회귀를 본다(브라케팅 촬영은 fused.png 로 돈다). 회귀 기준은 개수가 아니라 '맞는 자리': `tools/flat_check.py DIR --truth <143334 samples.json>`(입력은 fused.png, 없으면 raw.png. flat.png 는 이중 평탄화라 안 쓴다. 같은 폴더 정답이면 px, 아니면 mm 로 2.5mm) - 143334 는 16/16·오검출 0 을 지켜야 하고, 143454·142900 은 적중 15·오검출 0 목표(2026-09-21: 15·0, 14·0 - 142900 의 A 는 d² 6 으로 grow 아래). 경로별 기여는 `--mode all`.

## 보정 (core/plate.py = 고정 · core/calib.py = 판 마커)
- **고정 보정(기본)**: data/plate_calib.json = 감지 영역 사각형(촬영본 px) + 판 보정 점·H(카메라 번호 · 해상도
  포함). 판 보정 점 4개 미만(또는 한 직선)은 적용 거절, 잔차 한계(calib.residual_limit_mm) 초과 점은 빨간
  표시. 샘플 좌표 · 수동 추가 · 맵 포인터 · 공구 오프셋 방법 2 모두 이 판 보정으로 변환(engine.current_cal —
  fixed 에서는 _CAL_CACHE 대신 저장한 판 보정). 사각형 경계(calib.area_margin_mm)에 닿은 검출은 버린다.
- 아래는 markers 모드:
- 촬영마다 그 프레임의 마커로 다시 보정한다. 카메라 고정이 흔들려 촬영 사이에 1~2° 돌아간다(13분 사이 2.3°, 작업영역에서 최대 11mm 차이).
- 마커 중심 최소제곱 어파인으로 모서리를 mm 에 배정한 뒤, 모서리 전부(4마커 16점 / 3마커 12점)로 호모그래피. 잔차 RMS 0.14~0.24mm.
- 감지영역 = 마커 사각형(3개면 네 번째 꼭짓점을 A+C−B 로 보완). detect 는 이 영역만 본다 — 전체 프레임을 넣으면 레일·모터를 원판으로 오검출한다.
- 마커가 3개뿐이면 화면이 확인을 요구한다(오차 약 1mm, calib.confirm_3markers). 최소 개수 calib.min_markers(3~4).
- 헤드 마커 보정(photohome.py)은 판과 다른 평면 — 격자 점(n×n · 여백)에서 (픽셀 중심 ↔ 스테이지 XY)
  호모그래피, 점별 잔차·RMS, [적용] 해야 data/head_calib.json. 카메라 번호·해상도가 다르면 무효.
- 공구 오프셋 보정: 방법 1 d = A − B(포인터 / 공구 끝을 같은 점에), 방법 2 d = M − P0(단독 가열 자국을
  촬영본에서 클릭, 판 보정으로 mm). [적용] 해야 공구에 저장.

## 운용 규칙
- 웨이퍼는 마커 4개 안쪽에, 마커를 가리지 않게 둔다.
- 종료는 이동을 일으키지 않는다: save 만 하고 끝난다(다음 실행에서 EEPROM 복원).
  순회 중·이동 중에는 종료를 거절한다. 꺼진 동안 손으로 밀지 않는다.
- 원점(0)은 언제나 끝단에서 origin_gap_mm(기본 2 mm = 펌웨어 HOME_GAP 320 펄스) 물러난 자리다.
  판 마커 좌표·헤드 보정이 이 원점 기준이라 이격은 좌표계의 일부다 - 고급 탭에만 두고, 바꾸면
  다시 보정해야 한다는 경고 대화상자가 뜬다.
- 끝단 이동 거리는 1~그 축의 가동범위(limits). 펌웨어 jx/jy 한도(8000 펄스 = 50 mm)는 그대로 두고
  드라이버(jog_rel)가 조각으로 나눠 보낸다. 조각 사이마다 중단을 확인한다.
- 수동 이동 팝업은 위에서 아래로 [위치] [조그] [좌표 이동 · 현재 위치를 파킹/촬영 위치로] [원점 잡기(보조)] [비상정지].
  조그 스텝 선택지는 설정 jog.steps, 한 번 한도 jog.max_mm.
  원점 잡기는 축이 행(X·Y·Z), 동작(밀기 · 등록)이 열인 표다. 조그 패드 오른쪽에
  [Z↑][Z↓](PageUp/PageDown). 패드는 클릭 한 번 = 한 스텝, 0.5초 이상 누르면 연속.
- 아두이노 IDE 시리얼 모니터와 앱을 동시에 쓸 수 없다(포트 점유).
- 이중 실행 방지 뮤텍스는 창 모드에서만. --no-window(스모크·개발)는 여러 개 띄워도 된다.
- 마커 시트는 실제 크기(100%)로 인쇄해 평평하게 부착.
- 비상정지는 확인 없이 즉시 `!` 를 보낸다(WS + POST /estop 둘 다, 정책이 끄기면 같은 쓰기에 `r all 0`).
  그 뒤 위치는 신뢰할 수 없으니 사진 호밍(또는 수동 원점)을 다시 한다.

## 안전
- 공구(히트건·프로브) 충돌 = 파손. 소프트 리밋이 유일한 방어선 → 원점 정확도가 전부(사진 호밍으로 확인).
- 히트건 전원은 릴레이 — 통신이 끊기면 펌웨어 통신 감시가 끈다. 정책 기본은 비상정지·정지·오류·종료 때 끄기.
- 애매하면 안전한 쪽(움직이지 않음 · 릴레이 끔 · 확인 요구)을 기본으로.
- 끝단 이동은 5mm 씩 사람이 보면서, 원점 등록도 사람이 누른다. 하드스톱 충돌은
  시리얼로 감지되지 않는다(보고는 정상으로 나온다).

## 미결·다음 목표
1. 계측기 — backend/measure.py 의 Measurer 인터페이스로 붙인다(지금은 DummyMeasurer 가 값 없이 통과). 레시피 measure 단계가 부른다.
2. 레시피 photo 단계 — 자리만 구조로 남겼다(recipe.STEP_TYPES + engine.RUNNERS 에 더하면 된다).
3. Z 인터록 강화 — 프로브 장착 뒤 Z 원점 없음을 X·Y 이동 거절로 올릴지 결정.
4. 조명 — 밝은 회색/반투명 칩은 웨이퍼와 밝기 편차 4~9%, 채도 차 0~7 이라 영상만으로 구분되지 않는다. 확산광으로 바꾸면 반사광과 함께 해결된다.
5. 렌즈 왜곡 보정 — 지금은 호모그래피(평면 가정)뿐이다(판·헤드 모두).

## 작업 규칙
- 하드웨어 실행은 사용자가 한 단계씩. 코드는 요청 범위만.
- 화면은 HTML(서버가 상태의 주인). cv2.imshow 금지. 오프라인 납품 전제 — 웹폰트·CDN 금지(맑은 고딕·Consolas), 자산 URL 에 ?v=버전.
- 오프라인 재현 스위치(--image) 필수. detect.py 는 특정 사진 오버피팅 금지 — tools/regress.py 로 전체 회귀(fixed 검출기는 위 "고정 환경 검출기" 규칙).
- core/detect.py 수정 금지(요청된 소규모 수정만). fixed 모드 검출기(core/fixed_detect.py)는 data/truth 회귀로 튜닝. 땜질 대신 근본적 해결.
- 운용 값은 settings_schema 에만(위 설정 스키마 규칙). 새 설정은 스키마 항목으로 더하고 README 설정 표를 갱신한다.
- md 문서는 README.md·CLAUDE.md 둘뿐. 프롬프트 사본·작업 기록 만들지 않는다.
- 상세 기록은 Obsidian 01_Projects/Auto_Measurement_Program 에 사용자가 관리.
