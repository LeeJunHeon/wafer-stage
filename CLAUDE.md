# wafer_stage — 카메라 센싱 + 스테이지 제어 (통합 프로그램)

이 폴더는 "샘플 자동 측정 시스템"의 ① 센싱·모터 제어 부분이다. ② 실제 측정(계측기 제어)은 backend/measure.py 인터페이스로 붙인다(장비 미정). 촬영 결과·보정 파일은 ..\data\ 에 두고 git 에 넣지 않는다.

## 목적
4인치 웨이퍼(또는 철판) 위 샘플 조각 10~16개를 카메라로 검출하고, XY 갠트리가 프로브를 각 샘플 위로 옮긴다. 정확도 요구: 프로브가 샘플 위 ±1~2mm 안.

## 구조 (2026-09-10 재구성)
서버가 상태의 주인이고 화면은 HTML 이다. 가스 센서 프로그램(Gas_Sensor_Measurment_System\Program)과 같은 골격.
- run.py: 진입점. 루트에 있는 유일한 .py (`python run.py [--dry] [--image P] [--no-window] [--port N]`)
- core/: 장비·검출 핵심(검증 끝난 모듈, API 유지). 서버·화면을 모른다.
  paths.py imgio.py camera.py detect.py calib.py stage.py
  CLI: `python -m core.calib fit|samples|check` · `python -m core.stage st|mx|my|save`
- backend/: server.py(FastAPI·라우트·/ws·lifespan·CLI) window.py connection.py state.py
  commands.py engine.py stagectl.py vision.py measure.py loops.py logger.py storage.py version.py
  backend 안끼리는 `import engine` 처럼 이름으로, core 는 `from core import calib` 로 부른다.
- frontend/: index.html · css/style.css · js/{app,core,camera,map,samples,sequence,jog}.js
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
  카메라 패널은 미리보기/촬영본 토글. 미리보기는 GET /preview.jpg 를 250ms 마다 받는다.
- tools/: 개발·검증용. 프로그램 실행에는 필요 없다.
  `python tools/regress.py`(검출 개수 회귀) · `python tools/e2e_smoke.py`(전 흐름)
  · `python tools/flat_check.py DIR...`(평탄화 전·후 검출 비교)
  · `python tools/cam_probe.py`(카메라 포맷·해상도 실측. 앱 끄고. 못 열면 exit 2)
  e2e_smoke 는 임시 폴더를 만들어 WAFER_STAGE_DATA 로 넘긴다 — 실제 data/ 에 쓰지 않는다.
- firmware/stage_v6/, assets/
- 미리보기: backend/vision.py 의 CameraHolder 가 카메라 객체 하나를 lock 으로 공유한다.
  미리보기 스레드가 4fps 로 읽고 촬영도 같은 객체를 쓴다 - 촬영 때 닫았다 다시 열지 않는다.
- 촬영 순서(vision._capture_sync): [노출 브라케팅 → Mertens 융합](camera.capture_bracket,
  설정 bracket/bracket_exposure/bracket_settle_s) → 마커로 감지영역 → [종이 기준 조명
  평탄화](core/flat.py, 설정 flat_field) → 마커 재계산·검출은 평탄화본으로.
  raw.png 는 첫 장(가장 밝으면서 포화되지 않은 장) 원본, fused.png·flat.png 를 같은
  폴더에 남긴다. 노출 목록은 자동(bracket_exposure 비움 = 기본): AE 끄고 종이(밝은 상위
  20%) 평균이 200~235 가 되게 한 단계씩 맞춘 첫 장 → 한 단계씩 내려 최대 3장, 장 평균
  <25 면 버리고 멈춤. 고정 -4/-6/-8 은 194/44/1 이 나와 셋째 장이 검었다. --image 는
  브라케팅 없이 그 사진(평탄화는 한다). 카메라가 노출을 무시하면(장별 밝기 차 <10)
  또는 2장 미만이면 단일 촬영으로. 포화 진단은 평탄화 '전' 사진에서 잰다. 평탄화는 포화
  화소를 255 로 남긴다(안 그러면 검출의 포화 제외가 무력화된다). regress.py 도 같은 전처리.
  한쪽에서 강한 빛이 들어와 자동 노출이 요동(웨이퍼 평균 36~162, 한 장은 31% 포화)한
  것에 대한 대응이다. 검출 임계값은 사진이 일정해진 뒤 따로 맞춘다.
- 카메라 설정: width/height/fourcc(MJPG|YUY2) · wb_temperature(0 = 기본. AUTO_WB 끄고 값
  안 주면 초록끼) · exposure(0 = 자동, 값 주면 auto_exposure 꺼짐 - 화면은 둘을 따로 못
  고른다). 카메라 키가 바뀌면 holder.reopen.
- 시리얼 원문은 core/stage.py 의 on_line 콜백 → connection.push_log_threadsafe 로 화면에
  간다(level tx/rx). 폴링(st/ST)은 표시만 달아 보내고 숨길지는 화면이 정한다.
- 수동 이동(조그) 팝업은 show() 로 연다 - showModal 이면 뒤 화면이 inert 가 되어
  비상정지를 못 누른다. 조그는 ack{of:"jog"} 를 받고서야 다음 스텝을 보낸다(한 번에
  하나). 목표 좌표와 가동범위 자르기는 서버가 한다.
- 명령은 줄 세우지 않는다: ws_endpoint 가 메시지마다 create_task. 비상정지는
  handle_command 맨 앞에서 검사 없이 처리하고 POST /estop 우회로도 둔다(2026-09-11 사고:
  원점 탐색 69초 동안 비상정지 3건이 소켓에서 기다렸다). 이동 중이면 이동 명령은 거절.
- 이동 후 3초 조용하면 자동 save(디바운스). USB 절전으로 끊기면 원점을 잃는다.
- 원점은 사람이 축별로 잡는다: 끝단 이동(touch_end, 밀기만) → set_origin(2mm 이격 후 z).
  원점은 축마다 따로다(HX/HY). 가동범위를 벗어나면 펌웨어가 그 축 원점을 푼다.
  비상정지 때 forget 을 보내 EEPROM 의 원점 기록도 지운다.
  끝단 이동(touch_end)과 원점 등록(set_origin)은 따로다. 축마다 따로 잡는다.
- 메시지 계약(state/명령)은 README.md 에 표로 있다. 화면은 요청만 보내고 서버 state 가 와야 바뀐다.

## 하드웨어 (실측 확정값)
- 컨트롤러: Arduino Mega 2560 + 자작 펌웨어 V9(firmware/stage_v9). USB 시리얼 115200, 줄끝 \n. COM7.
- 드라이버: 모터뱅크 MSD-224 ×4(2상, 1/8분주 = 1600펄스/회전). ENA LOW = 동작
- 축: X = 모터 2개 동기(갠트리 양측, 논리축 하나) / Y = 빔 위 캐리지 / Z = 모터만 장착(프로브 미장착)
- 핀: X1 PUL2 DIR31 ENA30 / X2 PUL3 DIR49 ENA48 / Y PUL4 DIR53 ENA52 / Z PUL5 DIR45 ENA44
  (D5 = PORTE 비트3 → M_Z 0x08)
- 스케일: 160 펄스 = 1mm. 스트로크 X 0~39620(247.6mm), Y 0~39640(247.8mm). 속도 v6000/a40000/b300 ≈ 37.5 mm/s
- 가동범위는 두 겹: 펌웨어 X_MAX/Y_MAX/Z_MAX 는 기계 한계, 앱은 settings.json "limits"
  (x_max_mm·y_max_mm·z_max_mm, 기본 247.6·247.8·45)로 실사용 범위를 정한다.
  core/stage.py 의 *_MAX_PULSE 는 기본값일 뿐이고 set_limits 가 런타임에 바꾼다 —
  다른 모듈은 값을 복사하지 말고 stage_mod.limits_mm() / stagectl.limits_mm() 로 읽는다.
  calib.AXIS_MAX_X/Y 도 calib.set_limits 로 같이 따라간다(state.apply_to_core).
  "z_measure_mm"(기본 44)은 순회 중 Z 를 내릴 깊이 — 저장·표시·검증만 하고 아직 순회에는 안 쓴다.
- 좌표계: 각 축 모터 쪽 끝(하드스톱 2mm 이격) = 0. 파킹 위치 (0, 90) — 캐리지를 카메라 시야에서 치우는 자리
- Z 규약: Z=0 이 맨 위(들어 올린 자리), 값이 커질수록 아래(펄스 부호도 +가 아래).
  원점은 위쪽 하드스톱으로 밀어 잡는다 - Z 의 끝단 이동은 '위로' 다.
  Z_MAX 7200(45mm)·Z_PPMM 160 둘 다 실측 확인(2026-09-14 · 스트로크 2026-09-16 재실측).
  고치면 펌웨어·core/stage.py(기본값)·state.DEFAULT_APP["limits"] 셋 다.
- 원점 복귀(return_origin)·파킹은 Z 를 먼저 0(맨 위)으로 올린 뒤 X·Y 를 움직인다.
  Z 원점이 없으면 경고만 하고 X·Y 만 간다 - 프로브 장착 뒤 거절(인터록)로 올린다.
  Z 는 아직 X·Y 와 인터록이 없는 독립 축이다(서로 잠그지 않는다).
- 리밋 스위치 없음 → 소프트 리밋 + EEPROM 위치 기억
- 카메라: USB index 1, 1280×720 YUY2(MJPG 협상 실패), 작업영역 전체(약 52×29cm)를 위에서 본다. 약 0.41 mm/px
  해상도·포맷(width/height/fourcc)은 설정 창에서 고른다. 카메라는 못 하는 요청을 조용히
  가까운 값으로 바꾸므로 열 때마다 실제 포맷을 로그에 남기고 다르면 경고한다
  (camera.format_text/format_mismatch). 표기 화소 수(5000만)는 보간이라 믿지 말고
  tools/cam_probe.py 의 detail 지표(1/2 축소→복원 RMS)로 실측해서 정한다.
- 기준 마커: ArUco DICT_4X4_50 30mm, id0~id3 을 베이스에 부착. 중심 기계좌표(mm, 실측)
  id3 (15,20) / id2 (15,160) / id1 (201,19) / id0 (201,160) — settings.json 의 marker_mm_xy 가 단일 출처
- 캐리지 앞 드라이버 포인터(육안 확인용)

## 아두이노 프로토콜 (V9)
- 원점: `fz x <탐색펄스>` / `fz y` / `z zx zy`(현재 위치=0) / `sp <x> <y>` / `home`
  · 수동: `jx <±펄스>` `jy` `jz` (원점 없어도 움직인다 - 사람이 끝단까지 몰 때)
  · Z: `fz z` / `zz`(현재 위치=0) / `mz <mm>` / `gz <펄스>`
- 절대 이동: `gx <펄스>` `gy` `gz` `g <x> <y>` / `mx <mm>` `my` `mz`
- 상태: `p`(사람용) / `st` → `ST X= Y= X2= Z= HX=0|1 HY=0|1 HZ=0|1 DIRTY=0|1`
- 저장 `save` / 비상정지 `!` / 기타 `v a b w`, `e 0|1`, `forget`
- 이동 완료 보고: `  X  + cmd=16000 out=16000 t=2783.53ms | X=16000 Y=8000 Z=0` → `" | X="` 가 완료 신호
- 이미 목표면 `  이미 그 위치`(보고 줄 없음) / 거부 `  [거부] ...` / 경고 `[!]`
- 원점 없으면(HX·HY·HZ=0) fz·j* 외 모든 이동 거부. 이동은 블로킹. save 없이 껐으면 부팅 시 원점없음.
- EEPROM 매직 POS9. V8 이하로 저장한 기록은 '기록 없음' 으로 읽힌다(형식이 다르다).
- 주의: pyserial 의 `timeout` 에 값을 대입할 때마다 포트가 재설정된다 — 매 읽기마다 대입하면 들어오던 응답이 사라진다(core/stage.py `_readline` 주석).

## 검출 파이프라인 (검증 완료·고정. 순서 바꾸지 말 것)
무채색 마스크(채도 상한을 훑어 후보 경쟁) → OPEN → 최대 연결요소 → 구멍 메우기 → CLOSE → 광선 스캔 → fitEllipse.
색 후보와 엣지 후보를 '지지율 × 링 대비' 로 경쟁시켜 원판을 고른다. ROI = 축소 타원 ∩ 침식 볼록껍질 ∩ 직선 컷.
σ=stat_blur(1.0) 블러 → Lab 세 채널 각각 중앙값 배경(nearest-fill, _local_bg)을 뺀 잔차 → 채널별 잡음 σ(ROI 안 잔차 MAD×1.4826, 하한 1.0 = 8비트 Lab 양자화 한 단계) → d²=Σ(r/σ)² → 히스테리시스(stat_seed 25 / stat_grow 12, χ²(3); 포화 픽셀 제외) → 부호 분리(L 잔차 부호로 붙은 덩어리 가르기, 통과 덩어리는 둘 이상 나올 때만) → rescue/split(홈 짝 선택·ㄱ자 단일 홈 컷·재귀) → 병합 → 모양 판정 → 엣지 보완(3.7, 새 샘플 없음) → 엣지 제안(3.8, 새 샘플. edge_propose).
'배경으로 나눈 편차 %'(seed_pct·grow_pct)·채도(sat_delta)·색거리(de_floor·de_k)는 통계 거리 하나로 일반화됐다 - 설정 키는 남아 있지만 무시된다(2026-09-18). 평탄화 뒤 웨이퍼 밝기가 45 인 사진에서 편차 % 가 잡음을 증폭해 본 경로가 0개를 냈던 것이 계기다.
샘플마다 strength(덩어리 안 d² 중앙값 · 엣지 제안은 지지율)를 남기고 경로별 하위 weak_frac(20%)은 weak 로 표시한다(annotated 자홍 '?', 목록 배지) - confirm 에서 사람이 본다.
엣지 제안(3.8): 같은 재질의 짙은 회색 칩은 면 대비가 0(Lab 잔차 < 잡음)이라 가장자리 선만 보인다. σ=1 블러한 L·a·b Canny OR(edge_canny 사다리, 엄한 문턱에서 제안이 나오면 멈춤) → 3×3·5×5 닫기 → 컨투어 → 볼록껍질 → 면적 edge_min_area_mm2(25)~max → 꼭짓점 3~edge_vertices_max → 직선성(approxPolyDP 5% 둘레/컨투어 둘레 ≥ edge_straight_min 0.85) → 엣지 지지율(껍질 둘레 2px 안 엣지 비율 ≥ edge_support_min 0.6) → 볼록도(edge_solidity_min. 지지율 ≥0.85 면 면제 - ㄷ자 열린 엣지) → 안쪽 비어 있음(엣지 비율 ≤ edge_inner_max, d² 중앙값 < stat_grow) → 포화 화소 3px 안 없음 → 기존 샘플과 겹침 ≤ edge_overlap_max(양방향) → 테두리 띠 규칙. 들어온 샘플은 edge_only 'G' + "found by edge only; verify".
같은 재질 칩은 면이 아니라 가장자리로 찾는다 - 조명은 낮은 각도(측광)가 유리하다. 반사광 경계는 가장 강한 엣지라 그 언저리 제안은 버린다.
면적 임계는 mm². 실패해서 반복 금지: CLOSE 먼저 / 배경 블러 커널=샘플 크기 / Otsu / top-hat / 꼭짓점 수로 모양 거르기 / 화면 밖 광선 끝점 / 이미지 위 한글 / 오버레이 패널 크기 상수 / 채널 잡음 하한 0.5(a·b 가 양자화돼 MAD 0 → 옅은 색조 전부 검출) / 연결요소를 처음부터 L 부호로 분리(배경과 밝기가 같은 칩이 화소 부호 잡음으로 조각남 - 142900 D 소실) / 엣지 제안의 직선성 문턱만으로 얼룩 거르기(얼룩 0.87 vs 칩 0.86~0.95 겹침 - 면적 하한·사다리 폴백으로 거른다) / 엣지 제안에서 Canny 사다리 전부 돌리기(느슨한 문턱이 얼룩 둘레를 닫는다).
surface 모드 wafer/plate/auto. 검출 로직을 고치면 반드시 `python tools/regress.py` 로 개수 회귀를 본다(브라케팅 촬영은 fused.png 로 돈다). 경로별 기여는 `tools/flat_check.py --mode all`.

## 보정 (core/calib.py)
- 촬영마다 그 프레임의 마커로 다시 보정한다. 카메라 고정이 흔들려 촬영 사이에 1~2° 돌아간다(13분 사이 2.3°, 작업영역에서 최대 11mm 차이).
- 마커 중심 최소제곱 어파인으로 모서리를 mm 에 배정한 뒤, 모서리 전부(4마커 16점 / 3마커 12점)로 호모그래피. 잔차 RMS 0.14~0.24mm.
- 감지영역 = 마커 사각형(3개면 네 번째 꼭짓점을 A+C−B 로 보완). detect 는 이 영역만 본다 — 전체 프레임을 넣으면 레일·모터를 원판으로 오검출한다.
- 마커가 3개뿐이면 화면이 확인을 요구한다(오차 약 1mm).

## 운용 규칙
- 웨이퍼는 마커 4개 안쪽에, 마커를 가리지 않게 둔다.
- 종료는 이동을 일으키지 않는다: save 만 하고 끝난다(다음 실행에서 EEPROM 복원).
  순회 중·이동 중에는 종료를 거절한다. 꺼진 동안 손으로 밀지 않는다.
- 원점(0)은 언제나 끝단에서 2 mm 물러난 자리다(core/stage.py ORIGIN_GAP_MM =
  펌웨어 HOME_GAP 320 펄스). settings.json 의 마커 좌표가 이 원점 기준으로
  실측되어 있어서 이격은 선택이 아니라 좌표계의 일부다 - 화면에서 고를 수 없게 했다.
- 끝단 이동 거리는 1~그 축의 가동범위(limits). 펌웨어 jx/jy 한도(8000 펄스 = 50 mm)는 그대로 두고
  드라이버(jog_rel)가 조각으로 나눠 보낸다. 조각 사이마다 중단을 확인한다.
- 수동 이동 팝업은 위에서 아래로 [위치] [조그] [좌표 이동] [원점 잡기] [비상정지].
  원점 잡기는 축이 행(X·Y·Z), 동작(밀기 · 등록)이 열인 표다. 조그 패드 오른쪽에
  [Z↑][Z↓](PageUp/PageDown). 패드는 클릭 한 번 = 한 스텝, 0.5초 이상 누르면 연속.
- 아두이노 IDE 시리얼 모니터와 앱을 동시에 쓸 수 없다(포트 점유).
- 이중 실행 방지 뮤텍스는 창 모드에서만. --no-window(스모크·개발)는 여러 개 띄워도 된다.
- 마커 시트는 실제 크기(100%)로 인쇄해 평평하게 부착.
- 비상정지는 확인 없이 즉시 `!` 를 보낸다(WS + POST /estop 둘 다). 그 뒤 위치는 신뢰할 수 없으니 원점잡기를 다시 한다.

## 안전
- 프로브 장착 후 충돌 = 파손. 소프트 리밋이 유일한 방어선 → 원점 정확도가 전부.
- 끝단 이동은 5mm 씩 사람이 보면서, 원점 등록도 사람이 누른다. 하드스톱 충돌은
  시리얼로 감지되지 않는다(보고는 정상으로 나온다).

## 미결·다음 목표
1. 조명 — 밝은 회색/반투명 칩은 웨이퍼와 밝기 편차 4~9%, 채도 차 0~7 이라 영상만으로 구분되지 않는다. 확산광으로 바꾸면 반사광(현재 일부 사진 70%)과 함께 해결된다.
2. 렌즈 왜곡 보정 — 지금은 호모그래피(평면 가정)뿐이다. 화면 가장자리 정확도를 더 올리려면 체스보드로 왜곡 계수를 잡는다.
3. Z축·프로브 — 펌웨어 V9·드라이버·조그·원점 등록·실측(45mm, 160펄스/mm)까지 완료.
   원점 복귀·파킹은 Z 를 먼저 올린다. 남은 것: Z 원점이 없을 때를 거절로 올리고
   좌표 이동·순회 전체에 같은 인터록 적용, 순회에 Z 하강·상승 넣기(프로브 장착 뒤,
   깊이는 settings z_measure_mm).
4. 계측기 — backend/measure.py 의 Measurer 인터페이스로 붙인다(지금은 DummyMeasurer 가 값 없이 통과).

## 작업 규칙
- 하드웨어 실행은 사용자가 한 단계씩. 코드는 요청 범위만.
- 화면은 HTML(서버가 상태의 주인). cv2.imshow 금지. 오프라인 납품 전제 — 웹폰트·CDN 금지(맑은 고딕·Consolas), 자산 URL 에 ?v=버전.
- 오프라인 재현 스위치(--image) 필수. 특정 사진에 오버피팅 금지 — tools/regress.py 로 전체 회귀.
- 검출 로직 수정 금지(요청된 소규모 수정만). 땜질 대신 근본적 해결.
- md 문서는 README.md·CLAUDE.md 둘뿐. 프롬프트 사본·작업 기록 만들지 않는다.
- 상세 기록은 Obsidian 01_Projects/Auto_Measurement_Program 에 사용자가 관리.
