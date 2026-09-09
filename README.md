# wafer_stage

카메라로 웨이퍼(또는 철판) 위 샘플 조각의 위치를 검출하고, 갠트리 스테이지를 그 위로
옮기는 통합 프로그램. 화면은 HTML(로컬 서버 + WebSocket), 창은 pywebview.

## 폴더 구조

```
자동 측정 프로그램/
├── wafer_stage/            # 이 저장소 (코드만)
│   ├── backend/            # 서버
│   │   ├── server.py       #   진입점: FastAPI · 라우트 · /ws · lifespan · CLI
│   │   ├── window.py       #   pywebview 창, 포트 탐색, 단일 인스턴스, 종료
│   │   ├── connection.py   #   WebSocket 관리 + state/log/ack push
│   │   ├── state.py        #   상태의 주인 + settings.json 로드/저장
│   │   ├── commands.py     #   화면 명령 처리
│   │   ├── engine.py       #   촬영·검출 + 샘플 순회
│   │   ├── stagectl.py     #   stage.Stage 를 단일 워커 스레드에서 asyncio 로
│   │   ├── vision.py       #   촬영·검출(executor) + 프레임 JPEG 보관
│   │   ├── measure.py      #   계측기 인터페이스 (지금은 Dummy)
│   │   ├── loops.py        #   주기 태스크(st 폴링)
│   │   └── logger.py storage.py version.py
│   ├── frontend/           # index.html · css/style.css · js/{app,core,camera,samples,sequence}.js
│   ├── test/e2e_smoke.py   # 하드웨어 없이 전 흐름 검증
│   ├── camera.py           # 카메라 열기·초점·단발 촬영
│   ├── detect.py           # 원판/샘플 검출 엔진 (GUI 의존성 없음)
│   ├── calib.py            # 픽셀 <-> 기계좌표 보정 + 감지영역 + 검출 호출
│   ├── stage.py            # 펌웨어 V6 시리얼 드라이버 (CLI 도 있음)
│   ├── imgio.py            # 한글 경로에서도 되는 이미지 입출력
│   ├── paths.py            # 경로 정의 (코드와 데이터 분리)
│   ├── regress.py          # 지금까지 찍은 사진 전부로 검출 회귀
│   ├── settings.json       # 설정의 단일 출처
│   ├── firmware/stage_v6/  # 아두이노 스케치
│   └── assets/             # aruco_markers_30mm.pdf (실제 크기 100% 로 인쇄)
└── data/                   # 저장소 밖. 코드와 무관한 산출물
    ├── out/                #   seq_<시각>/ (raw.png · annotated.jpg · samples.json · results.csv)
    │                       #   sequence_log.jsonl · serial.log
    ├── logs/               #   날짜별 파일 로그
    └── calib_matrix.json   #   마지막 보정
```

`data` 위치는 `settings.json` 의 `data_dir` (기본 `../data`, wafer_stage 기준 상대경로).

## 실행

```
python backend/server.py                 # 창(pywebview)으로 실행
python backend/server.py --no-window     # 브라우저로 접속 (http://127.0.0.1:8000/)
python backend/server.py --dry           # 시리얼 없이(이동은 즉시 완료로 흉내)
python backend/server.py --image PATH    # 촬영 대신 저장된 사진
python backend/server.py --port 8010     # 포트 지정 (기본 8000, 쓰이면 8001~ 자동 탐색)
```

보조 도구(하드웨어 점검·회귀):

```
python stage.py --list-ports      # 시리얼 포트 목록
python stage.py st                # 스테이지 상태 (mx / my / save 도 가능)
python calib.py fit               # 마커로 보정해 data/calib_matrix.json 저장
python calib.py samples           # 촬영 → 검출 → 기계좌표 표
python calib.py check --save      # 저장된 보정과 지금 사진 비교
python regress.py                 # data/out 의 사진 전부로 검출 회귀 (개수 줄면 exit 1)
python test/e2e_smoke.py          # 서버를 띄워 capture→run→done 전 흐름 검증
```

## 통신 계약 (WebSocket `/ws`, JSON)

### 서버 → 화면

`{"type":"state", ...}` — 서버가 상태의 주인이다. 화면은 이 메시지가 와야 바뀐다.

| 키 | 내용 |
|---|---|
| `version` | `{name, version, build}` |
| `stage` | `connected, port, homed_x, homed_y, x_mm, y_mm, u, v, moving, dirty, needs_home, last_error` |
| `camera` | `index, ok, last_error, capturing` |
| `frame` | `{id, ts, w, h, url:"/frame/<id>.jpg"}` 또는 `null` |
| `calib` | `{refit, used_ids, missing_ids, transform, corner_rms_mm, corner_max_mm, rotation_deg, corners}` 또는 `null` |
| `sensing` | `{rect:[u0,v0,u1,v1]}` 또는 `null` |
| `wafer` | `{found, cx, cy, r_px, center_mm:[X,Y]}` 또는 `null` |
| `markers` | `{id: [[u,v] x4]}` |
| `samples` | `[{no, shape, u, v, X, Y, verts, on, status, value, unit, edge_completed, area_mm2}]` |
| `sequence` | `{phase, mode, dwell_s, cur_no, done, total, elapsed_s, out_dir, message}` |
| `warnings` | 검출 경고 문자열 목록 |
| `settings` | `{serial_port, camera_index, park_xy, dwell_s, marker_mm_xy, measure}` |

`status`: `wait | moving | measuring | done | skip | error`
`phase`: `idle | capturing | ready | running | paused | waiting_confirm | parking | done | stopped | error`

그 밖에 `{"type":"log", msg, level:"info|ok|warn|err"}`,
`{"type":"ack", of, ok, reason, needs_confirm:[사유...]}`.

### 화면 → 서버

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
| `settings_save` | `serial_port, camera_index, park_xy, dwell_s, marker_mm_xy, measure` |
| `exit` | |

`run` 은 검출 경고에 "wafer is cut off" 또는 글레어 50% 이상이 있거나 마커가 3개뿐이면
`ack{of:"run", ok:false, reason:"needs_confirm", needs_confirm:[사유]}` 를 돌려준다.
화면이 확인 모달을 띄우고 `confirm:true` 로 다시 보내야 시작한다.

## 운용

- 웨이퍼는 마커 4개 안쪽에 두고, 마커를 가리지 않는다(가리면 12점 보정 → 오차 약 1mm).
- 전원을 끄기 전에 종료 버튼으로 닫는다(파킹 → `save`). 꺼진 동안 손으로 밀지 않는다.
- 아두이노 IDE 시리얼 모니터와 이 프로그램을 동시에 쓸 수 없다(포트 점유).
- 마커 시트(`assets/aruco_markers_30mm.pdf`)는 반드시 실제 크기(100%)로 인쇄해 평평하게 붙인다.
