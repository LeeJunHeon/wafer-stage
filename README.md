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
│   │   ├── camera.py       #   카메라 열기·초점·단발 촬영
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
```

## 통신 계약 (WebSocket `/ws`, JSON)

### 서버 → 화면

`{"type":"state", ...}` — 서버가 상태의 주인이다. 화면은 이 메시지가 와야 바뀐다.

| 키 | 내용 |
|---|---|
| `version` | `{name, version, build}` |
| `stage` | `connected, port, homed_x, homed_y, x_mm, y_mm, u, v, moving, dirty, needs_home, last_error` |
| `camera` | `index, ok, last_error, capturing, preview` |
| `log_file` | 오늘 로그 파일 이름(상태줄에 표시) |
| `stage.jog_mode_x` `jog_mode_y` | 축별 `abs`(원점 있음) / `rel`(원점 없음·비상정지 뒤) |
| `stage.fw` | 펌웨어 버전(`V6` / `V7`) |
| `frame` | `{id, ts, w, h, url:"/frame/<id>.jpg"}` 또는 `null` |
| `calib` | `{refit, used_ids, missing_ids, transform, corner_rms_mm, corner_max_mm, rotation_deg, corners}` 또는 `null` |
| `sensing` | `{rect:[u0,v0,u1,v1]}` 또는 `null` |
| `wafer` | `{found, cx, cy, r_px, center_mm:[X,Y]}` 또는 `null` |
| `markers` | `{id: [[u,v] x4]}` |
| `samples` | `[{no, shape, u, v, X, Y, verts, on, status, value, unit, edge_completed, area_mm2}]` |
| `sequence` | `{phase, mode, dwell_s, cur_no, done, total, elapsed_s, out_dir, message}` |
| `warnings` | 검출 경고 문자열 목록 |
| `settings` | `{serial_port, camera_index, park_xy, dwell_s, marker_mm_xy, measure}` |
| `limits` | `{x_max_mm, y_max_mm}` — 스테이지 맵의 축척 |
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

`firmware/stage_v8/` 이 현재 버전이다(V7 은 이전 버전으로 남겨 둔다). 업로드는 Arduino IDE 로 수동.

| 명령 | 내용 |
|---|---|
| `jx <±펄스>` `jy <±펄스>` | **원점 없이도 되는 상대 이동**. 8000 펄스(50mm)까지. 가동범위 밖으로 나가면 그 축 원점 해제 |
| `fz x [펄스]` `fz y [펄스]` | 밀고 물러나 0 으로 등록. **앱은 쓰지 않는다**(시리얼 모니터용) |
| `zx` `zy` `z` | 지금 자리를 그 축(또는 두 축)의 0 으로 등록 |
| `forget` | 원점 기록 삭제(앱이 비상정지 때 보낸다) |
| `zx` `zy` `z` | 지금 자리를 0 으로 등록 |

V7 에서 달라진 것은 `jx/jy` 의 범위 처리뿐이다. V7 은 원점이 있는 축이 범위 밖으로
나가는 명령을 거부해서, 사고 뒤 EEPROM 에 원점OK 가 남아 있으면 0 에서 더 가지 못해
하드스톱에 맞출 수가 없었다(2026-09-11).

## 원점 잡기

사람이 보면서 잡는다. **끝단 이동**(미는 것)과 **원점 등록**(0 으로 정하는 것)은
따로다 — 끝에 닿았다고 판단하는 것은 사람이기 때문이다. 축마다 따로 한다.

1. [수동 이동] 팝업을 연다. 원점이 없는 축은 "X 없음" 처럼 주황 배지로 보인다.
2. 패드로 대강 끝 근처까지 옮긴다(원점이 없는 축은 1500 pps 로 느리게 간다).
3. [끝단 이동] 거리를 5 mm 로 두고 [X] 를 눌러 조금씩 민다. 끝에 닿으면 드르륵
   소리가 난다(정상). 가동범위를 벗어나는 순간 그 축의 원점은 자동으로 풀린다 —
   끝에 닿아 탈조하면 좌표를 믿을 수 없기 때문이다.
4. 그 자리에서 [원점 등록] [X] — 2 mm 물러난 자리를 X 의 0 으로 등록·저장한다.
5. Y 도 같은 방법으로. 두 축이 모두 등록되어야 절대 이동·순회·파킹이 열린다.

비상정지 뒤에는 앱이 펌웨어의 원점 기록도 지운다(`forget`). 지우지 않으면 다음
부팅에서 믿을 수 없는 원점이 EEPROM 에서 복원된다(2026-09-11 실장).

## 실장 첫 실행 절차

1. [연결] — 시리얼 포트가 잡히는지, 칩이 `COM7 · 원점 필요` 로 바뀌는지 확인
2. [수동 이동] → 축별로 [끝단 이동] 반복 → [원점 등록]. 칩이 `원점 등록됨` 으로 바뀐다
3. [미리보기] — 웨이퍼 위치·조명·초점을 눈으로 확인(마커 4개가 가리지 않게)
4. [촬영 · 검출] — 검출 개수와 배너(반사광·마커)를 확인
5. 번호 선택 모드로 샘플 1개만 [선택 위치 이동] — 프로브가 실제로 그 위 ±1~2mm 인지 확인
6. 확인 후 진행 모드로 2~3개를 돌려 보고, 문제 없으면 자동 모드로 전체 순회

## 화면 → 서버

`{"cmd": ...}` 한 종류다.

| 명령 | 인자 |
|---|---|
| `stage_connect` | `port?` |
| `stage_disconnect` | |
| `touch_end` | `axis:"x"\|"y"`, `mm`(기본 5, 1~10) — 그 축을 끝단 쪽으로 민다. 등록하지 않는다 |
| `set_origin` | `axis:"x"\|"y"\|"xy"`, `gap_mm`(기본 2) — 지금 자리에서 물러나 0 으로 등록 |
| `park` | |
| `goto` | `no` 또는 `x, y` |
| `jog` | `axis:"x"\|"y"`, `delta_mm` — 원점이 있으면 절대(가동범위로 자름), 없으면 상대 |
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
| `settings_save` | `serial_port, camera_index, park_xy, dwell_s, marker_mm_xy, measure` |
| `exit` | |

`run` 은 검출 경고에 "wafer is cut off" 또는 반사광 50% 이상이 있거나 마커가 3개뿐이면
`ack{of:"run", ok:false, reason:"needs_confirm", needs_confirm:[사유]}` 를 돌려준다.
화면이 확인 모달을 띄우고 `confirm:true` 로 다시 보내야 시작한다.

## 화면

한 화면에 다 들어간다(페이지 스크롤 없음). 1920×1040 고정 캔버스를 창 크기에 맞춰
축소하고, 내부에서 스크롤하는 곳은 샘플 표와 로그뿐이다. 무채색 바탕에 색은 상태에만
쓰고(ISA-101), 상태는 색과 글자를 함께 바꾼다(IDLE·READY·RUNNING·E-STOP …).

  헤더(상태 필 · X/Y 큰 숫자 · 연결 칩 4개 · 설정/종료/비상정지)
  배너(있을 때만: 오류/경고 · 출처별 한 줄 — 스테이지·카메라·검출·보정·순회)
  카메라(사진 + 오버레이) │ 스테이지 맵 + 샘플 목록 │ 조작
  로그 + 상태줄

스테이지 맵은 작업영역(limits)을 축척대로 그린다 — 위 X+, 왼쪽 Y+, 50mm 격자,
X 레일과 현재 위치의 빔·캐리지, 마커 4개, 감지영역, 웨이퍼, 샘플 점(상태별 색), 파킹 P.

## 운용

- 웨이퍼는 마커 4개 안쪽에 두고, 마커를 가리지 않는다(가리면 12점 보정 → 오차 약 1mm).
- 전원을 끄기 전에 종료 버튼으로 닫는다(파킹 → `save`). 꺼진 동안 손으로 밀지 않는다.
- 아두이노 IDE 시리얼 모니터와 이 프로그램을 동시에 쓸 수 없다(포트 점유).
- 마커 시트(`assets/aruco_markers_30mm.pdf`)는 반드시 실제 크기(100%)로 인쇄해 평평하게 붙인다.
