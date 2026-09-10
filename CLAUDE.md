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
- frontend/: index.html · css/style.css · js/{app,core,camera,map,samples,sequence}.js
  화면은 1920×1040 고정 캔버스(fit() 로 축소, 페이지 스크롤 없음). ISA-101 계열 배색
  (무채색 바탕, 색은 상태에만), 상단 상태줄·경보 배너·하단 명령, 스테이지 맵으로
  기계 위치를 보여준다. 단계(1·2·3) 표시는 쓰지 않는다 — 반복 운전 프로그램이다.
  인라인 style 을 쓰지 않는다(클래스로 뺀다). 버튼 크기는 .btn(34px)·.btn.sm(28px)·
  .btn.lg(42px) 셋뿐이고 전체 폭은 .block.
  화면 문구는 명사형·짧게("…하세요/…합니다" 금지, 구분은 " · "). 화면에 가는 오류
  (stage.last_error·camera.last_error·sequence.message)는 한 줄 60자 이내 —
  logger.short(e) 를 거치고 원문은 파일 로그에만. 대화상자 본문만
  "…하십시오" 체. 파일명·모듈명·펌웨어 명령(fz 등)은 화면에 쓰지 않는다.
  카메라 패널은 미리보기/촬영본 토글. 미리보기는 GET /preview.jpg 를 250ms 마다 받는다.
- tools/: 개발·검증용. 프로그램 실행에는 필요 없다.
  `python tools/regress.py`(검출 개수 회귀) · `python tools/e2e_smoke.py`(전 흐름)
  e2e_smoke 는 임시 폴더를 만들어 WAFER_STAGE_DATA 로 넘긴다 — 실제 data/ 에 쓰지 않는다.
- firmware/stage_v6/, assets/
- 미리보기: backend/vision.py 의 CameraHolder 가 카메라 객체 하나를 lock 으로 공유한다.
  미리보기 스레드가 4fps 로 읽고 촬영도 같은 객체를 쓴다 - 촬영 때 닫았다 다시 열지 않는다.
- 시리얼 원문은 core/stage.py 의 on_line 콜백 → connection.push_log_threadsafe 로 화면에
  간다(level tx/rx). 폴링(st/ST)은 표시만 달아 보내고 숨길지는 화면이 정한다.
- 메시지 계약(state/명령)은 README.md 에 표로 있다. 화면은 요청만 보내고 서버 state 가 와야 바뀐다.

## 하드웨어 (실측 확정값)
- 컨트롤러: Arduino Mega 2560 + 자작 펌웨어 V6(firmware/stage_v6). USB 시리얼 115200, 줄끝 \n. COM7.
- 드라이버: 모터뱅크 MSD-224 ×3(2상, 1/8분주 = 1600펄스/회전). ENA LOW = 동작
- 축: X = 모터 2개 동기(갠트리 양측, 논리축 하나) / Y = 빔 위 캐리지 / Z = 미장착
- 핀: X1 PUL2 DIR31 ENA30 / X2 PUL3 DIR49 ENA48 / Y PUL4 DIR53 ENA52
- 스케일: 160 펄스 = 1mm. 스트로크 X 0~39620(247.6mm), Y 0~39640(247.8mm). 속도 v6000/a40000/b300 ≈ 37.5 mm/s
- 좌표계: 각 축 모터 쪽 끝(하드스톱 2mm 이격) = 0. 파킹 위치 (0, 90) — 캐리지를 카메라 시야에서 치우는 자리
- 리밋 스위치 없음 → 소프트 리밋 + EEPROM 위치 기억
- 카메라: USB index 1, 1280×720 YUY2(MJPG 협상 실패), 작업영역 전체(약 52×29cm)를 위에서 본다. 약 0.41 mm/px
- 기준 마커: ArUco DICT_4X4_50 30mm, id0~id3 을 베이스에 부착. 중심 기계좌표(mm, 실측)
  id3 (15,20) / id2 (15,160) / id1 (201,19) / id0 (201,160) — settings.json 의 marker_mm_xy 가 단일 출처
- 캐리지 앞 드라이버 포인터(육안 확인용)

## 아두이노 프로토콜 (V6)
- 원점: `fz x [탐색펄스]` / `fz y` / `z zx zy`(현재 위치=0) / `sp <x> <y>` / `home`
- 절대 이동: `gx <펄스>` `gy` `g <x> <y>` / `mx <mm>` `my <mm>`
- 상태: `p`(사람용) / `st` → `ST X= Y= X2= HX=0|1 HY=0|1 DIRTY=0|1`
- 저장 `save` / 비상정지 `!` / 기타 `v a b w`, `e 0|1`, `forget`
- 이동 완료 보고: `  X  + cmd=16000 out=16000 t=2783.53ms | X=16000 Y=8000` → `" | X="` 가 완료 신호
- 이미 목표면 `  이미 그 위치`(보고 줄 없음) / 거부 `  [거부] ...` / 경고 `[!]`
- 원점 없으면(HX·HY=0) fz 외 모든 이동 거부. 이동은 블로킹. save 없이 껐으면 부팅 시 원점없음.
- 주의: pyserial 의 `timeout` 에 값을 대입할 때마다 포트가 재설정된다 — 매 읽기마다 대입하면 들어오던 응답이 사라진다(core/stage.py `_readline` 주석).

## 검출 파이프라인 (검증 완료·고정. 순서 바꾸지 말 것)
무채색 마스크(채도 상한을 훑어 후보 경쟁) → OPEN → 최대 연결요소 → 구멍 메우기 → CLOSE → 광선 스캔 → fitEllipse.
색 후보와 엣지 후보를 '지지율 × 링 대비' 로 경쟁시켜 원판을 고른다. ROI = 축소 타원 ∩ 침식 볼록껍질 ∩ 직선 컷.
중앙값 배경 나눗셈(nearest-fill) → 히스테리시스(seed 15%/grow 12%) + 채도 양방향 OR(포화 픽셀 제외) → rescue/split → 모양 판정 → 색거리(dE) 보조 경로 → 엣지 보완.
면적 임계는 mm². 실패해서 반복 금지: CLOSE 먼저 / 배경 블러 커널=샘플 크기 / Otsu / top-hat / 꼭짓점 수로 모양 거르기 / 화면 밖 광선 끝점 / 이미지 위 한글 / 오버레이 패널 크기 상수.
surface 모드 wafer/plate/auto. 검출 로직을 고치면 반드시 `python tools/regress.py` 로 개수 회귀를 본다.

## 보정 (core/calib.py)
- 촬영마다 그 프레임의 마커로 다시 보정한다. 카메라 고정이 흔들려 촬영 사이에 1~2° 돌아간다(13분 사이 2.3°, 작업영역에서 최대 11mm 차이).
- 마커 중심 최소제곱 어파인으로 모서리를 mm 에 배정한 뒤, 모서리 전부(4마커 16점 / 3마커 12점)로 호모그래피. 잔차 RMS 0.14~0.24mm.
- 감지영역 = 마커 사각형(3개면 네 번째 꼭짓점을 A+C−B 로 보완). detect 는 이 영역만 본다 — 전체 프레임을 넣으면 레일·모터를 원판으로 오검출한다.
- 마커가 3개뿐이면 화면이 확인을 요구한다(오차 약 1mm).

## 운용 규칙
- 웨이퍼는 마커 4개 안쪽에, 마커를 가리지 않게 둔다.
- 전원 끄기 전 종료 버튼(파킹 → save). 꺼진 동안 손으로 밀지 않는다.
- 아두이노 IDE 시리얼 모니터와 앱을 동시에 쓸 수 없다(포트 점유).
- 이중 실행 방지 뮤텍스는 창 모드에서만. --no-window(스모크·개발)는 여러 개 띄워도 된다.
- 마커 시트는 실제 크기(100%)로 인쇄해 평평하게 부착.
- 비상정지는 확인 없이 즉시 `!` 를 보낸다. 그 뒤 위치는 신뢰할 수 없으니 원점잡기를 다시 한다.

## 안전
- 프로브 장착 후 충돌 = 파손. 소프트 리밋이 유일한 방어선 → 원점 정확도가 전부.
- fz 탐색 거리는 넉넉히. 하드스톱 충돌은 시리얼로 감지되지 않는다(보고는 정상으로 나온다).

## 미결·다음 목표
1. 조명 — 밝은 회색/반투명 칩은 웨이퍼와 밝기 편차 4~9%, 채도 차 0~7 이라 영상만으로 구분되지 않는다. 확산광으로 바꾸면 반사광(현재 일부 사진 70%)과 함께 해결된다.
2. 렌즈 왜곡 보정 — 지금은 호모그래피(평면 가정)뿐이다. 화면 가장자리 정확도를 더 올리려면 체스보드로 왜곡 계수를 잡는다.
3. Z축·프로브 — 모터 미도착. engine 에 [Z 상승 → 이동 → Z 하강 → 측정] 자리를 비워 두었다.
4. 계측기 — backend/measure.py 의 Measurer 인터페이스로 붙인다(지금은 DummyMeasurer 가 값 없이 통과).

## 작업 규칙
- 하드웨어 실행은 사용자가 한 단계씩. 코드는 요청 범위만.
- 화면은 HTML(서버가 상태의 주인). cv2.imshow 금지. 오프라인 납품 전제 — 웹폰트·CDN 금지(맑은 고딕·Consolas), 자산 URL 에 ?v=버전.
- 오프라인 재현 스위치(--image) 필수. 특정 사진에 오버피팅 금지 — tools/regress.py 로 전체 회귀.
- 검출 로직 수정 금지(요청된 소규모 수정만). 땜질 대신 근본적 해결.
- md 문서는 README.md·CLAUDE.md 둘뿐. 프롬프트 사본·작업 기록 만들지 않는다.
- 상세 기록은 Obsidian 01_Projects/Auto_Measurement_Program 에 사용자가 관리.
