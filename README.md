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
│   │                       # js/{app,core,camera,map,samples,sequence}.js
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

그 밖에 `{"type":"log", msg, level, serial?, poll?}`,
`{"type":"ack", of, ok, reason, needs_confirm:[사유...], ...}`.

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

미리보기는 서버 기동과 함께 4 fps 로 돈다. 카메라 장치는 한 번에 한 곳만 열 수
있으므로 미리보기와 촬영이 같은 객체를 lock 으로 나눠 쓴다 — 촬영 때 닫았다 다시
열지 않는다. `--image` 로 띄우면 그 사진이 미리보기로 나온다(카메라를 열지 않는다).

## 실장 첫 실행 절차

1. [연결] — 시리얼 포트가 잡히는지, 칩이 `COM7 · 원점 필요` 로 바뀌는지 확인
2. [원점 설정] — 경로에 프로브·웨이퍼가 없는지 보고 실행. 칩이 `원점 설정` 으로 바뀐다
3. [미리보기] — 웨이퍼 위치·조명·초점을 눈으로 확인(마커 4개가 가리지 않게)
4. [촬영 · 검출] — 검출 개수와 배너(글레어·마커)를 확인
5. 번호 선택 모드로 샘플 1개만 [선택 위치 이동] — 프로브가 실제로 그 위 ±1~2mm 인지 확인
6. 확인 후 진행 모드로 2~3개를 돌려 보고, 문제 없으면 자동 모드로 전체 순회

## 화면 → 서버

`{"cmd": ...}` 한 종류다.

| 명령 | 인자 |
|---|---|
| `stage_connect` | `port?` |
| `stage_disconnect` | |
| `stage_home` | `axis:"x"\|"y"\|"xy"`, `search_pulses?` |
| `park` | |
| `goto` | `no` 또는 `x, y` |
| `capture` | |
| `run` | `mode:"auto"\|"confirm"\|"pick"`, `dwell_s`, `only?:[no]`, `confirm?` |
| `pause` `resume` `next` `stop` `estop` | |
| `set_on` | `no, on` |
| `set_all` | `on` |
| `measure_here` | |
| `open_out_dir` | 결과 폴더를 탐색기로 연다 |
| `open_results` | results.csv 를 연다(없으면 폴더) |
| `preview_start` `preview_stop` | 카메라 미리보기 |
| `list_ports` | 시리얼 포트 목록 → `ack{of:"list_ports", ports:[{device, description}]}` |
| `settings_save` | `serial_port, camera_index, park_xy, dwell_s, marker_mm_xy, measure` |
| `exit` | |

`run` 은 검출 경고에 "wafer is cut off" 또는 글레어 50% 이상이 있거나 마커가 3개뿐이면
`ack{of:"run", ok:false, reason:"needs_confirm", needs_confirm:[사유]}` 를 돌려준다.
화면이 확인 모달을 띄우고 `confirm:true` 로 다시 보내야 시작한다.

## 화면

한 화면에 다 들어간다(페이지 스크롤 없음). 1920×1040 고정 캔버스를 창 크기에 맞춰
축소하고, 내부에서 스크롤하는 곳은 샘플 표와 로그뿐이다. 무채색 바탕에 색은 상태에만
쓰고(ISA-101), 상태는 색과 글자를 함께 바꾼다(IDLE·READY·RUNNING·E-STOP …).

  헤더(상태 필 · X/Y 큰 숫자 · 연결 칩 4개 · 설정/종료/비상정지)
  경보 배너(있을 때만: 잘림·글레어·마커 가려짐·원점 필요·비상정지)
  카메라(사진 + 오버레이) │ 스테이지 맵 + 샘플 목록 │ 조작
  로그 + 상태줄

스테이지 맵은 작업영역(limits)을 축척대로 그린다 — 위 X+, 왼쪽 Y+, 50mm 격자,
X 레일과 현재 위치의 빔·캐리지, 마커 4개, 감지영역, 웨이퍼, 샘플 점(상태별 색), 파킹 P.

## 운용

- 웨이퍼는 마커 4개 안쪽에 두고, 마커를 가리지 않는다(가리면 12점 보정 → 오차 약 1mm).
- 전원을 끄기 전에 종료 버튼으로 닫는다(파킹 → `save`). 꺼진 동안 손으로 밀지 않는다.
- 아두이노 IDE 시리얼 모니터와 이 프로그램을 동시에 쓸 수 없다(포트 점유).
- 마커 시트(`assets/aruco_markers_30mm.pdf`)는 반드시 실제 크기(100%)로 인쇄해 평평하게 붙인다.
