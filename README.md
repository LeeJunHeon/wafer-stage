# wafer_stage

카메라로 웨이퍼(또는 철판) 위 샘플 조각의 위치를 검출하고, 갠트리 스테이지를 그 위로
옮기는 통합 프로그램. 화면은 HTML(로컬 서버 + WebSocket), 창은 pywebview.

## 폴더 구조

```
자동 측정 프로그램/
├── wafer_stage/            # 이 저장소 (코드만)
│   ├── run.py              # 진입점 (루트의 유일한 .py)
│   ├── settings.json       # 설정의 단일 출처
│   ├── core/               # 장비·검출 핵심. 서버·화면을 모른다
│   │   ├── paths.py        #   경로 정의 (코드와 데이터 분리)
│   │   ├── imgio.py        #   한글 경로에서도 되는 이미지 입출력
│   │   ├── camera.py       #   카메라 열기·초점·단발 촬영·노출 브라케팅 융합
│   │   ├── flat.py         #   종이 기준 조명 평탄화 + 포화 진단
│   │   ├── detect.py       #   원판/샘플 검출 엔진
│   │   ├── calib.py        #   픽셀 <-> 기계좌표 보정 + 감지영역 + 검출 호출
│   │   └── stage.py        #   펌웨어 V6 시리얼 드라이버
│   ├── backend/            # 서버
│   │   ├── server.py       #   FastAPI · 라우트 · /ws · lifespan · CLI
│   │   ├── window.py       #   pywebview 창, 포트 탐색, 단일 인스턴스, 종료
│   │   ├── connection.py   #   WebSocket 관리 + state/log/ack push
│   │   ├── state.py        #   상태의 주인 + settings.json 로드/저장
│   │   ├── commands.py     #   화면 명령 처리
│   │   ├── engine.py       #   촬영·검출 + 샘플 순회
│   │   ├── stagectl.py     #   core.stage 를 단일 워커 스레드에서 asyncio 로
│   │   ├── vision.py       #   촬영·검출(executor) + 프레임 JPEG 보관
│   │   ├── measure.py      #   계측기 인터페이스 (지금은 Dummy)
│   │   ├── loops.py        #   주기 태스크(st 폴링)
│   │   └── logger.py storage.py version.py
│   ├── frontend/           # index.html · css/style.css
│   │                       # js/{app,core,camera,map,samples,sequence,jog}.js
│   ├── tools/              # 개발·검증용. 프로그램 실행에는 필요 없다
│   │   ├── regress.py      #   지금까지 찍은 사진 전부로 검출 회귀
│   │   ├── flat_check.py   #   평탄화 전·후 검출 비교 (raw.png 들로)
│   │   └── e2e_smoke.py    #   하드웨어 없이 전 흐름 검증
│   ├── firmware/stage_v6/  # 아두이노 스케치
│   └── assets/             # aruco_markers_30mm.pdf (실제 크기 100% 로 인쇄)
└── data/                   # 저장소 밖. 코드와 무관한 산출물
    ├── out/                #   seq_<시각>/ (raw.png · annotated.jpg · samples.json · results.csv)
    │                       #   sequence_log.jsonl · serial.log
    ├── logs/               #   날짜별 파일 로그
    └── calib_matrix.json   #   마지막 보정
```

`data` 위치는 `settings.json` 의 `data_dir` (기본 `../data`, wafer_stage 기준 상대경로).
환경변수 `WAFER_STAGE_DATA` 가 있으면 그쪽이 우선한다 — `tools/e2e_smoke.py` 가
임시 폴더로 돌려 실제 `data/` 를 건드리지 않는 데 쓴다.
`WAFER_STAGE_DRY_MOVE_S` 는 `--dry` 이동을 그 초만큼 걸리게 한다(기본 0). 비상정지가
'이동 중' 에 도착하는 상황은 이것 없이 재현되지 않아 검증에서 쓴다.
`WAFER_STAGE_DRY_FW`(기본 V7) · `WAFER_STAGE_DRY_NO_HOME` 도 검증용이다 — 각각
흉내 낼 펌웨어 버전과 '원점 없는 상태로 시작'.

## 실행

```
python run.py                 # 창(pywebview)으로 실행
python run.py --no-window     # 브라우저로 접속 (http://127.0.0.1:8000/)
python run.py --dry           # 시리얼 없이(이동은 즉시 완료로 흉내)
python run.py --image PATH    # 촬영 대신 저장된 사진
python run.py --port 8010     # 포트 지정 (기본 8000, 쓰이면 8001~ 자동 탐색)
```

이중 실행 방지(뮤텍스)는 창 모드에서만 건다 — 두 번째 창은 안내 후 종료한다.
`--no-window` 는 개발·검증용이라 막지 않는다(`tools/e2e_smoke.py` 가 임의 포트로
여러 개를 띄운다). 그래서 앱 창을 띄워 둔 채로도 스모크를 돌릴 수 있다.

보조 도구(하드웨어 점검·회귀):

```
python -m core.stage --list-ports   # 시리얼 포트 목록
python -m core.stage st             # 스테이지 상태 (mx / my / save 도 가능)
python -m core.calib fit            # 마커로 보정해 data/calib_matrix.json 저장
python -m core.calib samples        # 촬영 → 검출 → 기계좌표 표
python -m core.calib check --save   # 저장된 보정과 지금 사진 비교
python tools/regress.py             # data/out 의 사진 전부로 검출 회귀 (개수 줄면 exit 1)
python tools/e2e_smoke.py           # 서버를 띄워 capture→run→done 전 흐름 검증
python tools/flat_check.py DIR ...  # raw.png 에 평탄화만 적용해 전·후 검출 개수·경고 비교
```

## 통신 계약 (WebSocket `/ws`, JSON)

### 서버 → 화면

`{"type":"state", ...}` — 서버가 상태의 주인이다. 화면은 이 메시지가 와야 바뀐다.

| 키 | 내용 |
|---|---|
| `version` | `{name, version, build}` |
| `stage` | `connected, port, homed_x, homed_y, homed_z, x_mm, y_mm, z_mm, u, v, moving, dirty, needs_home, last_error` |
| `camera` | `index, ok, last_error, capturing, preview` |
| `log_file` | 오늘 로그 파일 이름(상태줄에 표시) |
| `stage.jog_mode_x` `jog_mode_y` `jog_mode_z` | 축별 `abs`(원점 있음) / `rel`(원점 없음·비상정지 뒤) |
| `stage.fw` | 펌웨어 버전(`V8` / `V9`). 앱은 V9 을 쓴다 |
| `frame` | `{id, ts, w, h, url:"/frame/<id>.jpg"}` 또는 `null` |
| `calib` | `{refit, used_ids, missing_ids, transform, corner_rms_mm, corner_max_mm, rotation_deg, corners}` 또는 `null` |
| `sensing` | `{rect:[u0,v0,u1,v1]}` 또는 `null` |
| `wafer` | `{found, cx, cy, r_px, center_mm:[X,Y]}` 또는 `null` |
| `markers` | `{id: [[u,v] x4]}` |
| `samples` | `[{no, shape, u, v, X, Y, verts, on, status, value, unit, edge_completed, area_mm2}]` |
| `sequence` | `{phase, mode, dwell_s, cur_no, done, total, elapsed_s, out_dir, message}` |
| `warnings` | 검출 경고 문자열 목록 |
| `settings` | `{serial_port, camera_index, park_xy, dwell_s, marker_mm_xy, measure, limits, z_measure_mm}` |
| `limits` | `{x_max_mm, y_max_mm, z_max_mm}` — 지금 드라이버가 쓰는 가동범위(settings.limits 반영값). 스테이지 맵의 축척·조그 상한 |
| `marker_mm` | `{"0":[X,Y],…}` — 서버가 지금 쓰는 마커 기준 좌표. settings 에 `marker_mm_xy` 가 없어도 설정 창이 이것으로 표를 채운다 |
| `data_dir` | 데이터 폴더 절대경로(설정 창에 표시) |

`status`: `wait | moving | measuring | done | skip | error`
`phase`: `idle | capturing | ready | running | paused | waiting_confirm | parking | done | stopped | error`
`sequence.estopped`: 비상정지 상태(원점을 다시 잡을 때까지 유지)

화면에 나가는 오류 문구(`stage.last_error`·`camera.last_error`·`sequence.message`)는
항상 한 줄 60자 이내다. 예외 원문(여러 줄·트레이스백)은 파일 로그에만 남는다.

그 밖에 `{"type":"log", msg, level, serial?, poll?}`,
`{"type":"ack", of, ok, reason, needs_confirm:[사유...], ...}`.

`ack{of:"jog", ok, reason, x_mm, y_mm}` — 수동 이동은 한 번에 하나만 보낸다.
화면은 이 ack 를 받고서야 다음 스텝을 보낸다(타이머로 밀어 넣으면 손을 뗀 뒤에도
큐에 남은 명령이 실행된다). `reason`: `locked`·`busy`·`moving`·`at_limit`·`error`.

`log.level`: `info | ok | warn | err` 에 시리얼 원문용 `tx`(-> 보냄) · `rx`(<- 받음)
가 더 있다. `serial:true` 는 시리얼 원문, `poll:true` 는 2초 상태 폴링(`st` / `ST …`)
이라는 표시다. 화면은 이 두 값으로 걸러 보여 주고(기본: 원문 켬 · 폴링 끔), 파일
`data/out/serial.log` 에는 걸러진 것까지 전부 남는다.

#### HTTP

| 경로 | 내용 |
|---|---|
| `GET /` | 콘솔 화면 |
| `GET /health` | `{ok, version}` |
| `GET /frame/<id>.jpg` | 마지막 촬영본 (검출 결과를 그릴 바탕) |
| `GET /preview.jpg` | 미리보기 최신 한 장(640×360). 없으면 204 |
| `POST /estop` | 비상정지 우회로 — WebSocket 이 막혀도 도착한다 |

미리보기는 서버 기동과 함께 4 fps 로 돈다. 카메라 장치는 한 번에 한 곳만 열 수
있으므로 미리보기와 촬영이 같은 객체를 lock 으로 나눠 쓴다 — 촬영 때 닫았다 다시
열지 않는다. `--image` 로 띄우면 그 사진이 미리보기로 나온다(카메라를 열지 않는다).

### 촬영 순서 (한쪽에서 강한 빛이 들어오는 환경 대응)

자동 노출은 반사광에 반응해 장마다 웨이퍼 안 평균 밝기가 36~162 로 요동했고, 한 장은
웨이퍼의 31.6% 가 255 로 포화돼 후처리로 복구할 수 없었다(마커 모서리도 날아가 보정
오차 RMS 0.73 · 최대 2.21 mm). 그래서 촬영은 아래 순서로 간다(`backend/vision.py
_capture_sync`). 검출 임계값(seed_pct·sat_delta·val_min 등)은 건드리지 않았다.

1. **노출 브라케팅 + 융합** (`core/camera.py capture_bracket`, 설정 `bracket` 기본 켬)
   — 자동 노출을 끄고 `bracket_exposure`(기본 `[-4, -6, -8]`)의 값마다
   `CAP_PROP_EXPOSURE` 를 넣고 `bracket_settle_s`(0.4 s) 기다린 뒤 앞 3장을 버리고
   `capture_frames` 장 중 가장 선명한 한 장을 고른다. 모인 장을
   `cv2.createMergeMertens()` 로 융합한다(노출시간을 몰라도 되는 방식). 장별 평균 밝기
   차이가 10 미만이면 카메라가 노출 지시를 무시한 것 — "노출이 바뀌지 않음 · 브라케팅을
   건너뜁니다" 경고 뒤 단일 촬영으로 돌아간다. `--image` 는 브라케팅 없이 그 사진이다.
2. **마커 → 감지영역** (`calib.preprocess`).
3. **종이 기준 조명 평탄화** (`core/flat.py flatten`, 설정 `flat_field` 기본 켬) — 감지영역
   안에서 포화되지 않고(<250) 충분히 밝은 화소를 종이 후보로 두고, 채널별 2차 다항식
   조명면을 반복 최소제곱(잔차 큰 화소 버리고 다시 맞추기 ×3)으로 맞춘다. 웨이퍼·칩·
   그림자는 이 과정에서 빠진다. 그 면으로 채널별로 나눠 종이가 밝기 180 의 무채색이
   되게 한다 — 조명 기울기와 화이트밸런스가 한 번에 잡힌다. 종이 화소가 감지영역의
   20% 미만이면 건너뛰고 경고를 남긴다.
4. **마커 재계산 + 검출** 은 평탄화한 사진으로 한다. `tools/regress.py` 도 같은 전처리를
   거친다(앱과 같은 경로여야 회귀 비교가 뜻이 있다).

저장: `raw.png`(가운데 노출의 원본) · `fused.png`(융합본, 브라케팅했을 때) ·
`flat.png`(평탄화본, 적용됐을 때) · `annotated.jpg`.

촬영마다 로그에 남기는 진단: 브라케팅 여부와 장별 평균 밝기 · 종이 RGB 평균(보정
전→후) · 감지영역 안 254 이상 화소 비율 · 웨이퍼 안 포화 비율(평탄화 **전** 사진에서
잰다 — 나눗셈은 잃은 정보를 되살리지 못한다). 웨이퍼 포화가 10% 를 넘으면
"반사광으로 정보가 사라진 영역 N% · 노출을 더 낮추거나 편광 필터가 필요합니다" 경고.

카메라 설정(설정 창): `wb_temperature`(화이트밸런스 색온도, 0 = 카메라 기본. AUTO_WB
는 끄는데 값을 안 주면 종이가 초록끼를 띤다) · `exposure`(0 = 자동, 값을 주면
`auto_exposure` 가 꺼지고 그 노출로 고정). `bracket*`·`flat_field` 는 settings.json
에서 직접 고친다.

2026-09-16 네 장으로 `tools/flat_check.py` 를 돌린 결과(평탄화 전→후 검출 개수):
153305 14→14 · 153512 18→14 · 153617 13→15 · 153716 14→14. 153512 는 포화 31% 인
사진이라 전의 18 에 반사광 오검출이 섞여 있고, 후에는 어두운 쪽 회색 칩 두 개를
놓친다 — 사진이 일정해진 뒤 임계값을 맞출 때 볼 것. 종이 RGB 는 네 장 모두
(180,180,180) 으로 맞았다(전: 169~198 / 183~210 / 180~205).

## 명령 처리

WebSocket 으로 온 명령은 **줄 세우지 않는다**. 메시지마다 태스크를 띄워 받는 쪽이
막히지 않게 한다. 예전에는 `receive -> await handle_command` 를 한 줄로 돌려,
원점 탐색(69초) 중에 누른 비상정지가 탐색이 끝난 뒤에야 펌웨어로 나갔다(실장 사고).
비상정지는 `handle_command` 맨 앞에서 다른 검사 없이 처리하고, 화면은 WebSocket 과
`POST /estop` 로 동시에 보낸다.

이동 계열 명령은 스테이지가 이미 움직이는 중이면 거절한다(`이동 중 · 명령 무시`).
동시에 실행될 수 있게 된 뒤로는 이 검사가 없으면 두 이동이 겹친다.

이동이 끝나고 3초 동안 새 이동이 없으면 위치를 EEPROM 에 저장한다(자동, 파일 로그에만).
USB 가 빠져 보드가 리셋되면 마지막 저장 이후의 위치는 사라진다.

## 펌웨어

`firmware/stage_v9/` 이 현재 버전이다(V8 은 이전 버전으로 남겨 둔다). 업로드는 Arduino IDE 로 수동.

| 축 | PUL | DIR | ENA |
|---|---|---|---|
| X1 | D2 | D31 | D30 |
| X2 | D3 | D49 | D48 |
| Y | D4 | D53 | D52 |
| Z | D5 | D45 | D44 |

| 명령 | 내용 |
|---|---|
| `jx <±펄스>` `jy` `jz` | **원점 없이도 되는 상대 이동**. 한 번에 8000 펄스(50mm)까지 — 더 먼 거리는 드라이버가 조각으로 나눠 보낸다. 가동범위 밖으로 나가면 그 축 원점 해제 |
| `mx <mm>` `my` `mz` | 절대 이동(mm) |
| `gx <펄스>` `gy` `gz` | 절대 이동(펄스) |
| `fz x [펄스]` `fz y` `fz z` | 밀고 물러나 0 으로 등록. **앱은 쓰지 않는다**(시리얼 모니터용) |
| `zx` `zy` `zz` `z` | 지금 자리를 그 축의 0 으로 등록(`z` 는 X·Y 둘 다) |
| `forget` | 원점 기록 삭제(세 축 모두. 앱이 비상정지 때 보낸다) |

V9 에서 Z 축이 붙었다. EEPROM 기록 형식이 바뀌어(매직 `POS9`) V8 이하로 저장한
위치는 '기록 없음' 으로 읽힌다 — 올린 뒤 원점을 다시 잡는다.

**Z 규약**: `Z=0` 이 **맨 위**(들어 올린 자리)이고 값이 커질수록 아래로 내려간다.
펄스 부호도 같다(`+` = 아래). 원점은 **위쪽** 하드스톱으로 밀어서 잡고, 거기서
2 mm 물러난 자리가 0 이다(X·Y 와 같은 규칙). 끝단 이동은 Z 에서 '위로' 다.

`Z_MAX`(7200 = 45 mm)와 `Z_PPMM`(160) 둘 다 실측 확인했다(2026-09-14 · 스트로크
2026-09-16 재실측). 스트로크는 위 끝단에서 2 mm 이격한 0 에서 아래 끝단까지 45 mm 이고,
펄스/mm 는 1 mm 명령이 실제로 1 mm 를 간다.

**가동범위는 두 겹이다.** 펌웨어의 `X_MAX/Y_MAX/Z_MAX` 는 기계 한계이고(넘으면 펌웨어가
거부), 앱은 그 안에서 실사용 범위를 `settings.json` 의 `limits`
(`x_max_mm`·`y_max_mm`·`z_max_mm`, 기본 247.6·247.8·45)로 정한다. 설정 창에서 고쳐
저장하면 `core/stage.py set_limits` 와 `core/calib.py set_limits` 에 곧바로 반영되어
다음 이동부터 조그 자르기·범위 검사·끝단 이동 상한이 그 값을 쓴다(1~1000 mm 밖의
값은 무시). 연결할 때 앱 설정이 배너의 펌웨어 한계보다 크면 경고 로그를 남긴다.
`z_measure_mm`(기본 44)은 순회 중 측정할 때 Z 를 내릴 깊이다 — `z_max_mm` 보다 크면
저장을 거절한다. 아직 순회에서는 쓰지 않는다(아래 [미결] 2).

### Z 실측 (프로브를 달기 전에)

이 절차는 **끝났다**(2026-09-14). 값이 틀어졌다고 의심되면 같은 방법으로 다시 잰다.

1. ~~펄스/mm~~ — 완료(160). 스텝 10 으로 [Z↓] 를 다섯 번 눌러 실제로 내려온 거리를
   자로 잰다. `Z_PPMM = 160 x (지시 50) / (실제 mm)`. 고칠 때는 펌웨어와
   `core/stage.py` 의 `Z_PPMM` 을 같은 값으로, `Z_MAX`(펄스)도 다시 계산한다.
2. ~~스트로크~~ — 완료(45 mm = 7200 펄스).
3. [Z↓] 인데 위로 올라가면 펌웨어의 `Z_DIR_INVERT` 를 `true` 로 바꾸고 다시 올린다.

**원점 복귀와 파킹은 Z 를 먼저 맨 위(0)로 올린 뒤 X·Y 를 움직인다.** Z 원점이 없으면
"Z 원점 없음 · Z 는 두고 이동" 경고만 남기고 X·Y 만 간다 — 프로브를 단 뒤에는 이
경우를 거절(인터록)로 바꾼다. 그 밖에는 Z 와 X·Y 가 서로를 막지 않는다(X·Y 원점이
없어도 Z 는 움직인다). 순회 중 Z 하강·상승도 프로브를 단 뒤에 넣는다(맨 아래 [미결]).

## 원점 잡기

사람이 보면서 잡는다. **끝단 이동**(미는 것)과 **원점 등록**(0 으로 정하는 것)은
따로다 — 끝에 닿았다고 판단하는 것은 사람이기 때문이다. 축마다 따로 한다.

원점(0)은 **언제나 끝단에서 2 mm 물러난 자리**다. settings.json 의 마커 좌표가
이 원점을 기준으로 실측되어 있어서, 물러나는 거리는 고르는 값이 아니라 좌표계의
일부다 — 이격 없이 등록했더니 모든 샘플이 X·Y 로 2 mm 씩 모자라게 갔다(2026-09-11).

1. [수동 이동] 팝업을 연다. [원점 잡기] 는 축이 행, 동작이 열인 표다.
2. 패드로 대강 끝 근처까지 옮긴다(원점이 없는 축은 1500 pps 로 느리게 간다).
3. [밀 거리] 에 남은 거리를 어림해 넣고(1~그 축의 가동범위) X 행의 [밀기] 를 누른다. 펌웨어
   한도는 한 번에 50 mm 지만 드라이버가 나눠 보내므로 한 번에 멀리 갈 수 있다.
   끝에 닿으면 드르륵 소리가 난다(정상) — 그만큼 덜 갔다는 뜻이니 마지막은 짧게.
   가동범위를 벗어나는 순간 그 축의 원점은 자동으로 풀린다(탈조하면 좌표를 못 믿는다).
4. 끝에 닿은 자리에서 X 행의 [등록] — 2 mm 물러난 뒤 그 자리를 X 의 0 으로 저장한다.
5. Y 행도 같은 방법으로. 두 축이 모두 등록되어야 좌표 이동·순회·파킹이 열린다.
6. Z 행도 같지만 미는 방향이 **위**다(Z 는 위 끝단이 0). Z 원점은 X·Y 잠금과
   무관하다 — Z 가 없어도 X·Y 순회는 돌고, 반대도 같다.

비상정지 뒤에는 앱이 펌웨어의 원점 기록도 지운다(`forget`). 지우지 않으면 다음
부팅에서 믿을 수 없는 원점이 EEPROM 에서 복원된다(2026-09-11 실장).

## 실장 첫 실행 절차

1. [연결] — 시리얼 포트가 잡히는지, 칩이 `COM7 · 원점 없음` 으로 바뀌는지 확인
2. [수동 이동] → [원점 잡기] 표에서 축별로 [밀기] 반복 → [등록]. 칩이 `원점 등록됨` 으로 바뀐다
3. [미리보기] — 웨이퍼 위치·조명·초점을 눈으로 확인(마커 4개가 가리지 않게)
4. [촬영 · 검출] — 검출 개수와 배너(반사광·마커)를 확인
5. 번호 선택 모드로 샘플 1개만 [선택 위치 이동] — 프로브가 실제로 그 위 ±1~2mm 인지 확인
6. 확인 후 진행 모드로 2~3개를 돌려 보고, 문제 없으면 자동 모드로 전체 순회
7. [종료] — 이동 없이 위치만 저장하고 닫는다

## 화면 → 서버

`{"cmd": ...}` 한 종류다.

| 명령 | 인자 |
|---|---|
| `stage_connect` | `port?` |
| `stage_disconnect` | |
| `touch_end` | `axis:"x"\|"y"\|"z"`, `mm`(기본 5, 1~그 축의 `limits`) — 그 축을 0 쪽(z 는 위)으로 민다. 50 mm 조각으로 나눠 보낸다. 등록하지 않는다 |
| `set_origin` | `axis:"x"\|"y"\|"z"\|"xy"` — 끝단에서 2 mm 물러난 자리를 0 으로 등록(거리는 고정) |
| `park` | |
| `goto` | `no` 또는 `x, y` |
| `jog` | `axis:"x"\|"y"\|"z"`, `delta_mm` — 원점이 있으면 절대(가동범위로 자름), 없으면 상대 |
| `return_origin` | Z 를 맨 위(0)로 올린 뒤 X·Y 를 (0, 0) 으로 |
| `park_here` | 현재 위치를 `settings.park_xy` 로 저장 |
| `capture` | |
| `run` | `mode:"auto"\|"confirm"\|"pick"`, `dwell_s`, `only?:[no]`, `confirm?` |
| `pause` `resume` `next` `stop` `estop` | |
| `set_on` | `no, on` |
| `set_all` | `on` |
| `measure_here` | |
| `open_out_dir` | 결과 폴더를 탐색기로 연다 |
| `open_results` | results.csv 를 연다(없으면 폴더) |
| `open_log_dir` | 날짜별 로그 폴더를 연다 |
| `preview_start` `preview_stop` | 카메라 미리보기 |
| `list_ports` | 시리얼 포트 목록 → `ack{of:"list_ports", ports:[{device, description}]}` |
| `settings_save` | `serial_port, camera_index, park_xy, dwell_s, marker_mm_xy, measure, limits, z_measure_mm` — `z_measure_mm > limits.z_max_mm` 면 거절(ack `z_measure_over`) |
| `exit` | |

`run` 은 검출 경고에 "wafer is cut off" 또는 반사광 50% 이상이 있거나 마커가 3개뿐이면
`ack{of:"run", ok:false, reason:"needs_confirm", needs_confirm:[사유]}` 를 돌려준다.
화면이 확인 모달을 띄우고 `confirm:true` 로 다시 보내야 시작한다.

## 화면

한 화면에 다 들어간다(페이지 스크롤 없음). 1920×1040 고정 캔버스를 창 크기에 맞춰
축소하고, 내부에서 스크롤하는 곳은 샘플 표와 로그뿐이다. 무채색 바탕에 색은 상태에만
쓰고(ISA-101), 상태는 색과 글자를 함께 바꾼다(IDLE·READY·RUNNING·E-STOP …).

  헤더(상태 필 · X/Y/Z 큰 숫자 · 연결 칩 4개 · 설정/종료/비상정지)
  배너(있을 때만: 오류/경고 · 출처별 한 줄 — 스테이지·카메라·검출·보정·순회)
  카메라(사진 + 오버레이) │ 스테이지 맵 + 샘플 목록 │ 조작
  로그 + 상태줄

스테이지 맵은 작업영역(limits)을 축척대로 그린다 — 위 X+, 왼쪽 Y+, 50mm 격자,
X 레일과 현재 위치의 빔·캐리지, 마커 4개, 감지영역, 웨이퍼, 샘플 점(상태별 색), 파킹 P.

## 운용

- 웨이퍼는 마커 4개 안쪽에 두고, 마커를 가리지 않는다(가리면 12점 보정 → 오차 약 1mm).
- 전원을 끄기 전에 종료 버튼으로 닫는다. 종료는 **위치 저장(`save`)만 하고 스테이지를
  움직이지 않는다** — 다음 실행에서 저장된 위치가 EEPROM 에서 복원된다.
  순회 중·이동 중에는 종료가 거절된다(정지·완료 후 종료). 꺼진 동안 손으로 밀지 않는다.
- 아두이노 IDE 시리얼 모니터와 이 프로그램을 동시에 쓸 수 없다(포트 점유).
- 마커 시트(`assets/aruco_markers_30mm.pdf`)는 반드시 실제 크기(100%)로 인쇄해 평평하게 붙인다.

## 미결

1. **Z 인터록** — 원점 복귀·파킹은 이미 Z 를 먼저 올리지만, Z 원점이 없으면 경고만
   하고 X·Y 가 간다. 프로브를 달면 이것을 **거절**로 올리고, 좌표 이동·순회 전체에
   같은 규칙을 적용해야 한다(Z 가 내려가 있는 동안 X·Y 금지).
2. **순회에 Z 넣기** — [Z 상승 → X·Y 이동 → Z 하강 → 측정 → Z 상승] 자리를 engine 에
   비워 두었다. 하강 깊이는 설정 `z_measure_mm`(이미 저장·검증됨)을 쓴다.
3. ~~**Z 실측**~~ — 완료(2026-09-14). `Z_PPMM` 160 · `Z_MAX` 7200(45 mm, 2026-09-16 재실측).
4. **조명** — 밝은 회색/반투명 칩은 웨이퍼와 밝기 차가 4~9% 라 영상만으로 구분되지
   않는다. 확산광으로 바꾸면 반사광과 함께 해결된다.
5. **렌즈 왜곡 보정** — 지금은 호모그래피(평면 가정)뿐이다.
6. **계측기** — `backend/measure.py` 의 `Measurer` 로 붙인다(지금은 더미).
