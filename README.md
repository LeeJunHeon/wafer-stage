# wafer_stage

카메라로 웨이퍼(또는 철판) 위 샘플 조각의 위치를 검출하고, 갠트리 스테이지를 그 위로
옮기는 통합 프로그램. 화면은 HTML(로컬 서버 + WebSocket), 창은 pywebview.

## 폴더 구조

```
자동 측정 프로그램/
├── wafer_stage/            # 이 저장소 (코드만)
│   ├── run.py              # 진입점 (루트의 유일한 .py)
│   ├── settings.example.json # 예시(실제 settings.json 은 장비별 로컬 파일 — git 제외, 없으면 스키마 기본값)
│   ├── core/               # 장비·검출 핵심. 서버·화면을 모른다
│   │   ├── paths.py        #   경로 정의 (코드와 데이터 분리)
│   │   ├── imgio.py        #   한글 경로에서도 되는 이미지 입출력
│   │   ├── camera.py       #   카메라 열기·초점·단발 촬영·노출 브라케팅 융합
│   │   ├── flat.py         #   종이 기준 조명 평탄화 + 포화 진단
│   │   ├── detect.py       #   원판/샘플 검출 엔진
│   │   ├── calib.py        #   픽셀 <-> 기계좌표 보정 + 감지영역 + 검출 호출 (판 마커 방식)
│   │   ├── plate.py        #   고정 보정: 감지 영역 사각형 + 판 보정 호모그래피 + 검출 호출
│   │   └── stage.py        #   펌웨어 V11 시리얼 드라이버 (+ --dry 펌웨어 흉내)
│   ├── backend/            # 서버
│   │   ├── server.py       #   FastAPI · 라우트 · /ws · lifespan · CLI
│   │   ├── window.py       #   pywebview 창, 포트 탐색, 단일 인스턴스, 종료
│   │   ├── connection.py   #   WebSocket 관리 + state/log/ack push
│   │   ├── state.py        #   상태의 주인 + settings.json 로드/검증/저장/백업
│   │   ├── settings_schema.py #  운용 설정 스키마(기본값·타입·범위·탭·라벨·검증·이행)
│   │   ├── recipe.py       #   측정 순서(레시피) 단계 정의·검증·예상 시간
│   │   ├── relays.py       #   릴레이 10개 · 정책 · 하트비트 · 통신 감시 경보
│   │   ├── photohome.py    #   사진 호밍(헤드 마커) · 헤드/판 마커/공구 오프셋 보정
│   │   ├── platecal.py     #   고정 보정 명령: 감지 영역 · 판 보정 · 단발 촬영
│   │   ├── commands.py     #   화면 명령 처리
│   │   ├── engine.py       #   촬영 흐름 · 레시피 순회 엔진(안전 불변식)
│   │   ├── stagectl.py     #   core.stage 를 단일 워커 스레드에서 asyncio 로
│   │   ├── vision.py       #   촬영·검출(executor) + 프레임 JPEG 보관
│   │   ├── measure.py      #   계측기 인터페이스 (지금은 Dummy)
│   │   ├── loops.py        #   주기 태스크(st 폴링 · 릴레이 하트비트 · 자동 저장)
│   │   └── logger.py storage.py version.py
│   ├── frontend/           # index.html · css/style.css
│   │                       # js/{core,camera,map,samples,sequence,jog,relays,
│   │                       #     editors,settings,calib,plate,app}.js
│   ├── tools/              # 개발·검증용. 프로그램 실행에는 필요 없다
│   │   ├── regress.py      #   지금까지 찍은 사진 전부로 검출 회귀
│   │   ├── flat_check.py   #   평탄화 전·후 검출 비교 (raw.png 들로)
│   │   ├── fixed_regress.py #  고정 환경 검출기(core/fixed_detect.py) 회귀 · 튜닝 (data/truth 정답)
│   │   ├── make_truth.py   #   앱에서 고친 촬영(samples_edit.json)을 정답 파일로 내보내기
│   │   ├── cam_probe.py    #   카메라 포맷·해상도 실측 (카메라 필요)
│   │   ├── e2e_smoke.py    #   하드웨어 없이 전 흐름 검증
│   │   └── synth_cam.py    #   사진 호밍 검증용 합성 카메라(--dry)
│   ├── firmware/stage_v11/ # 아두이노 스케치(현재). v8~v10 은 기록
│   └── assets/             # aruco_markers_30mm.pdf (실제 크기 100% 로 인쇄)
└── data/                   # 저장소 밖. 코드와 무관한 산출물
    ├── out/                #   seq_<시각>/ (raw.png · annotated.jpg · samples.json · results.csv
    │                       #     · samples_edit.json - 샘플 편집이 있었을 때)
    │                       #   sequence_log.jsonl · serial.log
    │                       #   seq_*/steps.jsonl(레시피 단계 기록) · after.png(순회 후 확인 촬영)
    ├── logs/               #   날짜별 파일 로그
    ├── settings_backup/    #   설정 저장 직전본(최근 20개)
    ├── settings_export/    #   설정 내보내기 사본
    ├── head_calib.json     #   헤드 마커 보정(사진 호밍)
    ├── plate_calib.json    #   고정 보정: 감지 영역 + 판 보정(카메라 번호 · 해상도 포함)
    └── calib_matrix.json   #   마지막 판 마커 보정
```

**`settings.json` 은 장비별 로컬 파일**이다(git 추적 제외 · `.gitignore`). 없으면 스키마 기본값
(`settings_schema.py` · 검출 파라미터는 `detect.DEFAULTS`)으로 시작하고, 설정 창에서 처음 저장할 때
만들어진다. 예시는 `settings.example.json`. `tools/regress.py` · `tools/e2e_smoke.py` 는 이 파일에
의존하지 않는다(regress 는 `--settings` 를 줄 때만 얹는다).

`data` 위치는 `settings.json` 의 `data_dir` (기본 `../data`, wafer_stage 기준 상대경로).
환경변수 `WAFER_STAGE_DATA` 가 있으면 그쪽이 우선한다 — `tools/e2e_smoke.py` 가
임시 폴더로 돌려 실제 `data/` 를 건드리지 않는 데 쓴다.
`WAFER_STAGE_DRY_MOVE_S` 는 `--dry` 이동을 그 초만큼 걸리게 한다(기본 0). 비상정지가
'이동 중' 에 도착하는 상황은 이것 없이 재현되지 않아 검증에서 쓴다.
`WAFER_STAGE_DRY_FW`(기본 V11) · `WAFER_STAGE_DRY_NO_HOME` 도 검증용이다 — 각각
흉내 낼 펌웨어 버전과 '원점 없는 상태로 시작'. `WAFER_STAGE_SETTINGS` 는 settings.json
대신 쓸 설정 파일(스모크가 케이스마다 임시 파일을 넘긴다 — 저장소 settings.json 을 건드리지
않는다), `WAFER_STAGE_DRY_CAMERA=synth` 는 단발 촬영(사진 호밍·헤드 보정)을 합성 카메라로
바꾼다(`tools/synth_cam.py` — 4080×3060 도 만든다. `WAFER_STAGE_SYNTH_HEAD_OFF=1` 은 헤드 마커가 안
보이는 장면). 고정 보정 검증은 `synth_cam.render_plate` 가 알려진 호모그래피 · 알려진 샘플 위치로
만든 판 장면을 `--image` 로 준다.

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
python tools/fixed_regress.py       # 고정 환경 검출기 회귀(data/truth, config=ceiling) · --all · --update · --loo · --set fixed.seed=4
python tools/make_truth.py DIR      # 앱에서 고친 촬영 폴더 → data/truth/<폴더>.json
python tools/e2e_smoke.py           # 서버를 띄워 capture→run→done 전 흐름 검증
python tools/flat_check.py DIR ...  # raw.png 에 평탄화만 적용해 전·후 검출 개수·경고 비교
python tools/flat_check.py DIR --mode all   # 통계 경로만 / 엣지 제안만 기여를 나란히 (stat|edge)
python tools/cam_probe.py           # 카메라가 실제로 내보내는 포맷·해상도 실측 (앱을 끄고)
```

## 통신 계약 (WebSocket `/ws`, JSON)

### 서버 → 화면

`{"type":"state", ...}` — 서버가 상태의 주인이다. 화면은 이 메시지가 와야 바뀐다.

| 키 | 내용 |
|---|---|
| `version` | `{name, version, build}` |
| `stage` | `connected, port, homed_x, homed_y, homed_z, x_mm, y_mm, z_mm, u, v, moving, dirty, needs_home, last_error` |
| `camera` | `index, ok, last_error, capturing, preview, cap_text, cap_error` — `cap_text` 는 촬영 중 서버가 단계마다 푸시하는 진행 문구("촬영 위치로 이동 중" · "촬영 중 · 노출 맞춤" · "촬영 중 · 프레임 2/3" · "합치는 중" · "검출 중" · "저장 중"), `cap_error` 는 실패 한 줄 `{id, text}`(화면이 id 가 새로울 때 2.8초 보임) |
| `plate` | 고정 보정: `{mode, valid, reason, data{created, camera_index, size, area[4×(u,v)], points[{x,y,u,v,res_mm}], has_h, rms_mm, max_mm, res_limit_mm, n}, shot}` — `valid` 는 카메라 번호 · 해상도가 저장 때와 같고 감지 영역 · 판 보정이 다 있을 때. `shot` 은 보정 창 단발 촬영 `{id, w, h, url}` |
| `head_marker` | 촬영본에서 찾은 헤드 마커 `{center, corners[4]}`(표시용) 또는 null |
| `sensing` | `{rect, quad}` — `quad` 는 고정 보정의 감지 영역 사각형(촬영본 px), `rect` 는 그것을 감싸는 영역 |
| `calib` | `{mode: fixed\|markers, used_ids, missing_ids, corner_rms_mm, corner_max_mm, …}` — fixed 에서는 마커 항목이 비어 있다 |
| `log_file` | 오늘 로그 파일 이름(상태줄에 표시) |
| `stage.jog_mode_x` `jog_mode_y` `jog_mode_z` | 축별 `abs`(원점 있음) / `rel`(원점 없음·비상정지 뒤) |
| `stage.fw` | 펌웨어 버전(`V8` … `V11`). 앱은 V11 을 쓴다 |
| `frame` | `{id, ts, w, h, url:"/frame/<id>.jpg"}` 또는 `null` |
| `calib` | `{refit, used_ids, missing_ids, transform, corner_rms_mm, corner_max_mm, rotation_deg, corners}` 또는 `null` |
| `sensing` | `{rect:[u0,v0,u1,v1]}` 또는 `null` |
| `wafer` | `{found, cx, cy, r_px, center_mm:[X,Y]}` 또는 `null` |
| `markers` | `{id: [[u,v] x4]}` |
| `samples` | `[{no, shape, u, v, X, Y, verts, on, status, value, unit, edge_completed, edge_only, weak, strength, area_mm2, manual}]` — 늘 번호순. `manual:true` 는 수동 추가(`shape:""`, `verts:[]`) |
| `edit` | `{undo, manual, deleted}` — 되돌릴 수 있는 편집 수 · 목록의 수동 샘플 수 · 지운 검출 샘플 수 |
| `sequence` | `{phase, mode, cur_no, done, total, elapsed_s, out_dir, message, recipe, tool, step_index, step_total, step_desc, step_left_s, steps, used}` — `steps` 는 이번 순회의 단계 설명, `step_desc` 는 "#3 · 4/7 · 히트건 켜기", `used` 는 이 촬영으로 이미 순회했는가 |
| `warnings` | 검출 경고 문자열 목록 |
| `settings` | 앱 설정 전체(스키마의 최상위 키 — 아래 설정 표). 화면은 이 값으로 그린다 |
| `relays` | `[{n, on, since}] x10` — 펌웨어 RS 줄이 진실, `since` 는 켜진 것을 본 시각(epoch) |
| `photohome` | `{status: unknown\|ok\|corrected\|failed, d_mm, time, x_mm, y_mm, msg, calib, calib_valid, calib_reason}` |
| `alarms` | `[{src, text}]` — 경보 배너(통신 끊김 · 릴레이 최대 켜짐 초과 · 헤드 보정 무효) |
| `tool` | 활성 공구 `{name, dx_mm, dy_mm, z_work_mm, note}` |
| `job` | 순회 밖의 긴 작업 `{kind: head_calib\|tool_heat, running, i, n, message, result}` |
| `estimate` | 활성 레시피 `{recipe, sample_s, total_s, n, confirm, relays}` |
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

`ack{of:"sample_add", ok, reason, no}` — `sample_add`·`sample_add_here` 의 결과. 성공이면
`no` 가 새 번호(화면이 그 샘플을 선택한다). `reason`: `busy`(순회 중) · `capturing` ·
`no_capture`(촬영 결과 없음) · `bad_args` · `outside`(감지영역 밖) · `out_of_range`(가동범위 밖) ·
`overlap`(기존 샘플 중심 2.5 mm 안 또는 검출 윤곽 안) · `locked`·`moving`(현재 위치 추가의 원점·이동 조건).

`ack{of:"capture", ok:false, reason:"needs_confirm", needs_confirm:[사유], edits}` — 편집이 남은
채 촬영하면 찍지 않고 이것만 돌려준다. 화면이 묻고 `confirm:true` 로 다시 보낸다.

`log.level`: `info | ok | warn | err` 에 시리얼 원문용 `tx`(-> 보냄) · `rx`(<- 받음)
가 더 있다. `serial:true` 는 시리얼 원문, `poll:true` 는 주기 폴링(`st` / `ST …` · 릴레이 하트비트 `rs` / `RS …`)
이라는 표시다. 화면은 이 두 값으로 걸러 보여 주고(기본: 원문 켬 · 폴링 끔), 파일
`data/out/serial.log` 에는 걸러진 것까지 전부 남는다.

#### HTTP

| 경로 | 내용 |
|---|---|
| `GET /` | 콘솔 화면 |
| `GET /health` | `{ok, version}` |
| `GET /frame/<id>.jpg` | 마지막 촬영본 (검출 결과를 그릴 바탕) |
| `GET /preview.jpg` | 미리보기 최신 한 장(가로 `preview_max_w` 이하로 줄인 비율 유지 JPEG). 없으면 204 |
| `POST /estop` | 비상정지 우회로 — WebSocket 이 막혀도 도착한다 |
| `GET /settings/schema` | 설정 스키마 + 현재값 + 기본값(설정 화면이 이것으로 그려진다) |

미리보기는 서버 기동과 함께 설정 주기(`preview_fps`, 기본 4 fps)로 돈다. 카메라 장치는 한 번에 한 곳만 열 수
있으므로 미리보기와 촬영이 같은 객체를 lock 으로 나눠 쓴다 — 촬영 때 닫았다 다시
열지 않는다. `--image` 로 띄우면 그 사진이 미리보기로 나온다(카메라를 열지 않는다).

### 고정 보정 (`calib.mode = fixed`, 기본)

판 마커(id0~3)는 붙이지 않는다. 카메라는 천장에 고정이고 샘플은 판에 새겨진 사각형 안에만 놓이므로
**감지 영역은 고정**이고 좌표는 **한 번 저장한 판 보정**으로 낸다(카메라를 옮기거나 해상도를 바꾸면
다시). 판 마커 검출 · "마커 부족" 판정 · 마커 3개 확인은 하지 않고, 판 마커가 화면에 있어도
무시한다. 마커는 Z 모터 윗면의 헤드 마커(id 10) 하나뿐이다. 예전 방식은 `calib.mode = markers`
로 그대로 남아 있다.

- **감지 영역** — 촬영본 픽셀 좌표의 볼록 사각형(네 꼭짓점). `data/plate_calib.json`(카메라 번호 ·
  해상도와 함께). 검출은 사각형을 감싸는 영역만 잘라(crop) 사각형 밖을 안쪽 판 색으로 채워 평평하게
  만든 뒤 `detect` 에 넣고, 결과 픽셀 좌표는 원본 프레임 좌표로 되돌려 저장 · 표시한다. 사각형
  경계 `calib.area_margin_mm` 안에 닿은 검출(새겨진 선 · 경계 이음매)은 버리고 번호를 1부터 다시 매긴다.
- **판 보정** — (촬영본 픽셀 → 스테이지 XY, 기준 포인터 기준) 호모그래피, 점 4개 이상(권장 5~9)
  최소제곱. 점별 잔차 · RMS · 최대를 보이고 한계(`calib.residual_limit_mm`) 초과 점은 빨갛게 표시.
  4점 미만(또는 한 직선 위)은 적용을 거절한다. 샘플 좌표 · 수동 추가(촬영본 클릭) · 맵 포인터 ·
  공구 오프셋 방법 2(자국 클릭)가 모두 이 판 보정으로 변환한다.
- **검출 스케일** — `detect` 는 고치지 않았다. 철판 모드(`surface=plate`)의 기존 인자만 쓴다:
  `plate_mm_per_px` = 감지 영역 중심에서 호모그래피의 국소 mm/px ÷ 축소 배율, `marker_mm=0`(마커 끔),
  `plate_margin_pct=0`. `detect_max_px` 보다 crop 의 긴 변이 크면 줄여서 검출하고 좌표 · 배율을
  원본으로 되돌린다.
- **검출만 모드** — 감지 영역만 유효(판 보정 없음)하면 검출은 돌리고 mm 스케일은 `calib.est_mm_per_px` 를
  쓴다. 샘플은 픽셀 좌표 · 윤곽 · 모양 · 약함 표시까지 나오고 X/Y 는 null(상태 `nocoord`, 목록 "—"),
  `state.calib.area_only = true`. 상단 칩 "검출만 · 판 보정 필요"와 경고 한 줄만 띄우고 경보는 없다. 좌표 없는
  샘플의 이동 · 순회 · 촬영본 클릭 추가는 거절("판 보정 필요"). 판 보정을 적용하면 다음 촬영부터 좌표가 붙는다.
  감지 영역도 없으면 검출은 건너뛰고 촬영본(raw.png · fused.png)을 `data/out/<시각>_nocal/` 에 저장하며
  "감지 영역 없음 · 보정 → 감지 영역 지정"을 알린다(보정 창은 이 촬영본을 바로 쓴다).
  스테이지 맵은 fixed 에서 판 마커를 그리지 않고, 판 보정이 있으면 감지 영역 사각형을 점선으로 그린다.
- **무효 처리** — 카메라 번호 · 해상도가 저장 때와 다르면 감지 영역 · 판 보정이 무효다. 촬영은 하되
  좌표를 만들지 않고 경보 배너 "판 보정 무효 · 다시 보정"을 띄운다.
- **평탄화** — 종이 기준이라 고정 보정에서는 기준이 없다. 건너뛰고 "평탄화 건너뜀 · 고정 보정"을
  로그에 남긴다. 브라케팅 · 융합은 그대로다.
- **소요 시간** — 촬영마다 "소요 W×H · 촬영 · 융합 · 검출 · 헤드 마커"(초)를 로그에 남긴다.
- **보정 창 → 감지 영역 · 판 보정 탭** — ① 촬영본(최근 촬영본 또는 단발 촬영) ② 네 모서리 클릭 →
  사각형 미리보기 → 꼭짓점 끌기 → [적용] ③ 점마다 기준 포인터를 판 위 표시점에 맞춰 [위치 기록] +
  촬영본에서 같은 점 클릭(1~8배 확대, 선택한 점은 0.5 px 씩 미세 이동). [감지 영역 꼭짓점을
  보정점으로 쓰기]는 꼭짓점 픽셀을 점 목록에 넣어 두고 [위치 기록]만 하게 한다 ④ 표(번호 · X · Y ·
  u · v · 잔차 · 삭제)에서 RMS · 최대 확인 후 [적용]. 클릭 좌표는 원본 해상도 기준이다.

### 고정 환경 검출기 (`core/fixed_detect.py`, fixed 모드 기본)

`core/detect.py`(웨이퍼 · 철판 일반 검출, 검증 완료)는 **markers 모드 전용**으로 그대로 두고, fixed 모드는
설정 `fixed.detector`(`fixed` 기본 | `legacy` = 기존 `detect.detect`)로 고른다. 운용 환경(천장 고정 카메라 ·
알루미늄 판 · 3x5 배치)에 맞춰 **실제 촬영 사진(data/truth 정답) 회귀로 튜닝**한다 — 한 장에 맞추지 않도록
여러 장 · `--loo` 로 확인한다. 회색 · 반투명 칩은 판과 색 · 밝기가 거의 같고 판 긁힘이 많아 기준을 낮추면
긁힘이 잡힌다. 밝은 노출일수록 나쁘다(종이 249 의 140936) — 그래서 운용 카메라 기본이 **노출 -5 고정 ·
화이트밸런스 5000 K 고정**이다(미고정이면 색이 초록 · 노랑으로 흔들린다).

1. Lab 채널별 잔차 = 가우시안(`fixed.blur_sigma` 1.5) 블러 − medianBlur(칩 긴 변 × `fixed.bg_k_chips` 3) →
   채널 강건 z(중앙값 · MAD×1.4826, MAD 하한 1.0 = 8비트 Lab 양자화 한 단계 — 하한이 없으면 밝은 사진에서
   MAD 0 이 되어 전부 잡음) → `d = √Σz²`. 검출용으로 칩 긴 변이 `fixed.work_long_px`(60) 가 되게 줄여서 한다.
2. `d > fixed.seed`(4.5) → open 3×3 → close 5×5 → 연결요소 → 모양 필터(면적 `fixed.area_ratio` 0.25~3배 ·
   볼록도 ≥ `fixed.min_solidity` 0.75 · 종횡비 ≤ `fixed.max_aspect` 4.5 · 최소외접사각형 채움률 ≥
   `fixed.min_fill` 0.45 — 긁힘 제거).
3. 칩 크기: 1차 검출의 강한 후보(볼록도 > 0.85) 긴 변 중앙값으로 자동 추정해 그 크기로 다시 맞춘다. 추정이
   실패하면 `fixed.chip_mm`(11 mm)을 배율로 환산한다.
4. 배치 사전정보(`fixed.layout` `grid` 기본 | `free`, `fixed.grid_rows` 3 · `fixed.grid_cols` 5): 후보 중심으로
   격자(아핀 `o + i·u + j·v`, 정수 인덱스)를 RANSAC 으로 맞추고 최소제곱으로 정련해 15칸 위치를 예측한다.
   방향(가로 · 세로)과 약간의 회전 · 원근은 허용. 인라이어가 부족하면 free 로 떨어지고 경고 한 줄. 칸에 들지
   않은 후보는 버린다. 빈 칸은 칸 창(칩 긴 변 ±0.9배) 안에서 낮은 기준(`fixed.local_seeds` 3.5 → 2.8 → 2.2,
   이어 임계를 올려 후광으로 부푼 덩어리의 핵만 잡는 단계)으로 다시 찾고, 그래도 없으면 같은 창에서 Lab
   Canny(L 10/30 · a·b 4/12) 닫기 · 채우기, 그래도 없으면 예측 위치로 샘플을 낸다(`fixed.emit_predicted`).
5. 출력 샘플은 기존 dict 와 호환(`x_px` `y_px` `vertices_px` `shape` `area_mm2` `weak` `strength`) +
   **`source`** = `detected`(검출 · 실선) | `local`(보완 · 실선 + `+`, weak) | `predicted`(예측 · 점선 원 + `?`,
   weak). 보완 · 예측은 확인 모드에서 사람이 본다(기존 weak 처리와 같다). 샘플 번호는 행 우선.
6. **제외 영역**: 감지 영역 안에 걸리는 헤드 · 기판 등을 가리는 다각형(원본 px, `plate_calib.json` 의 `exclude`).
   보정 창 감지 영역 · 판 보정 탭의 [제외 영역 클릭] → [영역 닫기] → [적용]. 검출 전에 판 색으로 칠하고 d 를
   0 으로 둔다. **빈 판 기준(선택 · 미검증)**: [빈 판 기준 촬영]이 `data/plate_ref.png` + `plate_ref.json`(카메라
   번호 · 해상도 · 노출 · 화이트밸런스 · 감지 영역)을 저장하고, 설정 `fixed.use_ref` 가 켜져 있고 메타가
   현재와 같으면 배경을 medianBlur 대신 그 사진으로 쓴다(작은 이동은 위상 상관으로 맞춤). 다르면 무효로
   표시하고 medianBlur 로 되돌아간다.
7. 로그: 칩 크기 추정값 · 격자 인라이어 수 · source 별 개수 · 단계별 시간(잔차 · 격자 · 재탐색).

**정답 파일(`data/truth/*.json`)**: `{ "<폴더명>": { "image": "raw.png|fused.png", "size": [w,h], "samples":
[{"u","v","how"}], "area": [[u,v]×4], "mm_per_px": 근사, "chip_long_px": 근사, "config": "ceiling"|"motor" } }`
(`how` = detected / local / grid / edited). 폴더는 `data/out/<폴더명>/` 에 있어야 한다. `tools/make_truth.py DIR` 가
앱의 샘플 편집(추가 · 삭제)으로 고친 `samples_edit.json` 을 이 형식으로 내보낸다(감지 영역은 그 촬영 때의
`plate_calib.json`, 배율은 판 보정 또는 `calib.est_mm_per_px`). **회귀 `tools/fixed_regress.py`**: 항목마다
감지 영역 crop → `fixed_detect` → 정답과 min(2.5 mm, 칩 긴 변 × 0.35) 안 가까운 순 1:1 매칭 → 사진별 · 전체
적중 / 놓침 / 오검출 · source 별 표. `--update` 로 기준(`tools/fixed_regress_base.json`)을 고정하고 나빠지면
exit 1. `--loo` 는 한 장씩 빼고 나머지로 `fixed.seed` · `fixed.min_fill` · `fixed.min_solidity` 격자를 맞춘 값으로
뺀 장을 채점해 기본값과 비교한다(과적합 확인). `--set fixed.seed=4` 로 파라미터를 덮어 시험한다.

### 촬영 순서 (한쪽에서 강한 빛이 들어오는 환경 대응)

자동 노출은 반사광에 반응해 장마다 웨이퍼 안 평균 밝기가 36~162 로 요동했고, 한 장은
웨이퍼의 31.6% 가 255 로 포화돼 후처리로 복구할 수 없었다(마커 모서리도 날아가 보정
오차 RMS 0.73 · 최대 2.21 mm). 그래서 촬영은 아래 순서로 간다(`backend/vision.py
_capture_sync`).

0. **고정 노출 한 장이 기본이다**(설정 `bracket` 기본 끔 — 함체 안 조명이 일정하고 감지 영역 포화가
   0.0% 라 브라케팅이 필요 없다. 2026-11-05 실장 4080×3060: 브라케팅 촬영 17.7 s + 융합 4.8 s).
   `exposure` 가 0 이면 첫 촬영에서 자동 노출이 수렴하기를 기다려 그 값을 읽어 고정하고(카메라 번호 ·
   해상도가 같은 동안) 이후 촬영은 노출 탐색 없이 그 값을 재사용한다. 설정 카메라 탭 `exposure` 옆
   **[노출 다시 찾기]**(명령 `exposure_refind`)가 저장값을 버려 다음 촬영이 다시 맞춘다. 값을 읽지 못하면
   자동 노출을 유지한다. 프레임은 `capture_frames`(기본 3)장 중 가장 선명한 장, 못 받으면 받은 만큼으로 진행하고
   로그에 "프레임 요청 n · 받음 m". 소요는 노출 · 안정 대기 · 프레임 · 융합 · 검출 · 헤드 마커 · 저장을 초 단위로
   로그에 남긴다. 예상(장비 확인 전): 노출 재사용 시 안정 대기 0.4 + 프레임 3장 ≈ 1 s 안팎, 검출은 `detect_max_px`
   로 줄인 크기 기준 약 1.8 s(실측) → 촬영→검출 수 초 이내.
1. **노출 브라케팅 + 융합** (`core/camera.py capture_bracket`, 설정 `bracket` 을 켠 경우. fixed 에서는 융합을 감지 영역
   사각형(+여백)에만 하고 헤드 마커는 첫 장 원본으로 검출한다)
   — 자동 노출을 끄고 노출을 바꿔 가며 받아 Mertens 방식으로 융합한다
   (노출시간을 몰라도 되는 방식). 노출 목록은 밝기를 보며 정한다(`bracket_exposure`
   가 비어 있을 때 = 기본): 첫 장은 종이(밝은 상위 20% 화소) 평균이 200~235 가 되도록
   노출을 한 단계씩 올리거나 내려 맞추고(최대 5회), 거기서 한 단계씩 내리며 더 받는다.
   장 전체 평균이 25 미만이면 검은 사진이라 버리고 멈춘다(최대 3장). 고정 목록
   -4/-6/-8 은 이 카메라에서 194/44/1 이 나와 셋째 장이 검었고 raw.png 도 어두웠다.
   `bracket_exposure` 에 값을 적으면 그 목록을 그대로 쓴다(수동 지정). 장마다
   `bracket_settle_s`(0.4 s) 기다린 뒤 앞 3장을 버리고 `capture_frames` 장 중 가장
   선명한 한 장을 고른다. 장별 평균 밝기 차이가 10 미만이면 카메라가 노출 지시를 무시한
   것 — "노출이 바뀌지 않음 · 브라케팅을 건너뜁니다" 경고 뒤 단일 촬영으로 돌아간다(2장
   미만이어도 같다). 실제로 쓴 노출값과 장별 평균은 로그에 남는다. `--image` 는 브라케팅
   없이 그 사진이다.
2. **마커 → 감지영역** (`calib.preprocess`). 고정 보정은 이 단계 없이 저장한 감지 영역을 쓴다.
3. **종이 기준 조명 평탄화** (`core/flat.py flatten`, 설정 `flat_field` 기본 켬) — 감지영역
   안에서 포화되지 않고(<250) 충분히 밝은 화소를 종이 후보로 두고, 채널별 2차 다항식
   조명면을 반복 최소제곱(잔차 큰 화소 버리고 다시 맞추기 ×3)으로 맞춘다. 웨이퍼·칩·
   그림자는 이 과정에서 빠진다. 그 면으로 채널별로 나눠 종이가 밝기 180 의 무채색이
   되게 한다 — 조명 기울기와 화이트밸런스가 한 번에 잡힌다. 종이 화소가 감지영역의
   20% 미만이면 건너뛰고 경고를 남긴다.
4. **마커 재계산 + 검출** 은 평탄화한 사진으로 한다. `tools/regress.py` 도 같은 전처리를
   거친다(앱과 같은 경로여야 회귀 비교가 뜻이 있다).

저장: `raw.png`(첫 장 = 가장 밝으면서 포화되지 않은 장의 원본) · `fused.png`(융합본,
브라케팅했을 때) · `bracket_N.png`(장별 원본) · `flat.png`(평탄화본, 적용됐을 때) ·
`annotated.jpg`. 평탄화는 포화 화소를 255 로 남긴다 — 나눗셈으로 값이 내려가면 검출의
포화 제외(V<250)가 무력화된다.

융합은 자체 Mertens(`camera.fuse_exposures`: 대비·채도·적정 노출 가중치 + 라플라시안
피라미드)다. **가장 밝은 장에서 245 이상인 화소는 그 장의 가중치를 0** 으로 두고 어두운
장을 쓴다 — 포화 화소는 정보가 없는데 '적정 노출' 가중치가 그 자리를 밝은 장에서 가져와
결과도 날아갔다. 143454 의 장별 원본은 남아 있지 않아(이 커밋부터 남긴다) 그 사진에서의
효과는 확인하지 못했다. 합성 검사(raw×3.0/1.0/0.4)에서 포화 지대의 밝기가 cv2 161 → 140 으로
내려가고 칩 L 이 143 → 176 으로 살아난다.

### 검출 판정 (통계 기반 · 2026-09-18)

검출 임계가 고정 숫자(밝기 편차 % · 채도 차 · 색거리)일 때는 사진의 밝기·잡음 수준이
바뀌면 그대로 어긋났다. 142900 촬영에서 평탄화 뒤 웨이퍼 밝기가 45 라 '배경으로 나눈
편차 %' 가 잡음을 증폭해(순수 배경 dev 중앙값 10%, 임계 15%) 본 경로가 0개를 냈고, HSV
채도도 어두운 화소에서 불안정해 sat_mask 가 ROI 의 34% 를 덮었다. 그래서 판정을
**잡음으로 정규화한 Lab 잔차** 로 일반화했다(배경 추정 방식은 그대로):

- σ=`stat_blur`(1.0) 블러 → Lab 세 채널 각각 국소 배경(`_local_bg`, 중앙값·nearest-fill)을
  뺀 잔차 → 채널별 잡음 σ = ROI 안 잔차의 MAD×1.4826(하한 1.0 — 8비트 Lab 은 a·b 가 1 단위로
  양자화돼 MAD 가 0 이 된다) → d² = Σ(r/σ)² → `stat_seed`(25, χ²(3) p≈1e-5) /
  `stat_grow`(12, p≈7e-3). 설정 창에는 이 두 키만 보인다.
- **통계 씨앗이 기본이고 기존 씨앗은 `legacy_seed`(기본 false, 개발용 — 설정 창에 없다)로만
  OR 한다.** 2026-09-21 실제 앱 경로(fused.png, 143334 의 16개 정답)로 씨앗 구성을 채점한
  결과(143334 / 143454 / 142900 적중·오검출):

  | 씨앗 | 143334 | 143454 | 142900 |
  |---|---|---|---|
  | 통계 씨앗만 (지금) | 16·0 | 15·0 | 15·0 |
  | 기존 OR 통계 | 16·0 | 15·1 | 13·0 |
  | 기존 씨앗만 | 15·0 | 14·1 | 13·0 |

  기존 경로(배경으로 나눈 편차 %·채도 차)의 잡음 씨앗이 어두운 사진에서 칩 둘을 덩어리에
  묻고(142900 15→13), 143454 에서는 밝은 띠로 이어진 어두운 칩 셋을 가르다 유령 조각을
  만들었다. `seed_pct`·`grow_pct`·`sat_delta` 는 `legacy_seed` 일 때만 뜻이 있고, 색거리(dE)
  보조 경로(3.5)와 그 뒤 단계는 그대로다.
- 본 경로 후보의 볼록도 하한 `min_solidity` 0.70(0.55 였다). 통계 씨앗이 옛 사진에서 볼록도
  0.51~0.54 · 13 mm² 파편을 잡았다. 143334 16개의 최저 볼록도는 0.82.
- 얼룩·림 관문(어느 경로에서 왔든): 웨이퍼보다 어두운 후보는 직선성(approxPolyDP 5% 둘레 /
  컨투어 둘레) ≥ `dark_straight_min` 과 꼭짓점 3~`dark_vertices_max`(6)를 요구한다. 원판
  경계에서 반지름의 `dark_rim_pct`(3%) 안에 중심이 있으면 버린다. 직선성 0.85 는 143334 의
  진짜 칩 셋(마스크 윤곽 0.72·0.82·0.85)을 떨어뜨려 **0.70 으로 완화**했다 — 그래서 143454 의
  오검출 둘(웨이퍼 위쪽 림의 11 mm² 삼각형 0.82, 가운데 얼룩 9.5 mm² 0.75)은 이 관문으로는
  못 거른다(둘 다 weak 표시).
- 붙은 덩어리: L 잔차의 부호(배경보다 밝다/어둡다)로 가른다 — 중앙값 배경이 가까운 두
  어두운 칩 사이에서 내려앉아 그 틈이 밝은 편차로 잡혀 다리가 됐다(170421 칩 3개 226mm²,
  143454 칩 둘 145mm²). 통과한 덩어리는 조각이 둘 이상 나올 때만 대신한다. 홈 자르기는
  '실제로 갈라지는 홈 짝' 을 고르고, 홈이 하나뿐인 ㄱ자는 맞은편까지 가장 짧은 선으로,
  셋이 붙은 것은 재귀로 가른다.
- 샘플마다 `strength`(덩어리 안 d² 중앙값, 엣지 제안은 지지율)를 남기고, **절대 기준**으로
  `weak` 를 붙인다: 본 경로 strength < `stat_seed`×`weak_strength_k`(2 → 50), 엣지 제안
  지지율 < `weak_support`(0.75). annotated 자홍 '?', 목록 '?' 배지 — confirm 모드에서 사람이
  본다. (하위 20% 상대 기준은 16개가 다 확실해도 셋에 '?' 가 붙어 없앴다.)
- 웨이퍼 판정 `roi_fit` 은 ROI 안 (S, V) 중앙값 주변 MAD×3 안에 드는 비율이다(상대 기준).
  화이트밸런스가 맞으면 웨이퍼는 무채색이 아니라(S 중앙값 103) `sat_max` 절대값으로는
  "mostly not the wafer" 가 헛되이 떴다. `sat_max` 는 원판을 찾는 단계에서만 쓴다.

**엣지 제안 경로** (`edge_propose`, 기본 켬. 안쪽이 웨이퍼보다 밝은 후보는 제안하지
않는다 — 같은 재질 칩은 밝지 않다. 143334·143454 웨이퍼 가운데의 밝은 얼룩 32 mm² 가
직선성 0.95·지지율 1.0 으로 다른 관문을 다 통과했다. 테두리 띠에 닿는 제안도 버린다.
Canny 사다리는 전부 돌되 둘째 단계부터 직선성을 `edge_straight_loose` 0.90 으로 올린다): 웨이퍼와 같은 재질의 짙은 회색 칩은 Lab
잔차가 L 1~4, a/b 1~6 으로 잡음(σ_L≈8)보다 작아 면으로는 어떤 밝기·색 방법으로도 못
찾고 가장자리 선만 보인다. 3.7단계 엣지 보완은 '새 샘플을 만들지 않는다' 가 규칙인데,
이 경로는 그 제한을 명시적으로 푼 별도 단계다(3.7은 그대로). 조건: σ=1 블러한 L·a·b
Canny OR(`edge_canny` 사다리 — 엄한 문턱에서 제안이 나오면 거기서 멈추고, 없을 때만
느슨한 문턱으로. 느슨한 문턱은 얼룩 둘레도 닫는다) → 3×3·5×5 닫기 → 컨투어 → 볼록껍질 →
면적 `edge_min_area_mm2`(25)~`max_area_mm2` → 꼭짓점 3~`edge_vertices_max` → 직선성
approxPolyDP(5%) 둘레/컨투어 둘레 ≥ `edge_straight_min`(0.85) → 엣지 지지율(껍질 둘레 중
2px 안에 엣지가 있는 비율) ≥ `edge_support_min`(0.6) → 볼록도 ≥ `edge_solidity_min`(지지율
0.85 이상이면 면제 — 한 변이 끊긴 ㄷ자 엣지는 컨투어 면적이 0) → 껍질 안쪽이 비어 있음
(엣지 비율 ≤ `edge_inner_max` 0.05, d² 중앙값 < `stat_grow`) → 껍질 3px 안에 포화 화소 없음
(반사광 경계) → 기존 샘플과 겹침 ≤ `edge_overlap_max`(0.3, 양방향) → 테두리 띠 규칙.
들어온 샘플은 `edge_only`('G' 배지) + "sample #N found by edge only; verify", `strength`
는 지지율. **같은 재질 칩은 면이 아니라 가장자리로 찾는다 — 조명은 낮은 각도(측광)가
유리하다.**

'빌린 엣지' 관문 둘(2026-09-21): 143334·143454 둘 다 X105.6 Y84.3(황갈색 칩의 오른쪽, 얼룩의
왼쪽 아래 사이)에 edge_only 오검출이 있었다 — 황갈색 칩의 오른쪽 변과 얼룩의 경계가 닫힌
다각형을 만들었고 안은 빈 웨이퍼다(지지율 1.0·직선성 0.96·볼록도 0.98, 모양으로는 못 거른다.
이대로면 프로브가 빈 자리를 찍는다). ① 제안 다각형이 확정된 샘플 경계에서
`edge_min_gap_mm`(1.5) 안에 들어오면 버린다 — 칩은 자기 변을 가진다. ② 제안 둘레를 받치는
엣지 화소 중, 그 엣지의 연결 성분이 다각형을 3 px 팽창한 영역 밖에 `edge_borrow_max`(30%)
넘게 걸쳐 있으면 지지에서 뺀다(다른 물체의 가장자리다). 뺀 뒤 지지율이 `edge_support_min`
아래면 버린다. 적용 뒤 data/out 26장에서 남은 edge_only: 09-09 세션 4장의 (483,401)
삼각 칩(옛 결과에도 있던 진짜 칩) 각 1 · 155302 (616,341) 1 · 170421 (680,266) 같은 재질
칩 1 · 153512 (569,324)·(659,340) 2 · 153716 (660,340) 1 · 나머지 0(143334·143454·142900 0).

**회귀 기준은 '맞는 자리'다**: `python tools/flat_check.py DIR --truth <samples.json|self>` 가
정답 좌표와 대조해 적중·오검출·놓침을 찍는다. 입력은 앱이 실제로 검출한 원천 —
**fused.png(없으면 raw.png)** — 이고 flat.png 는 쓰지 않는다(다시 평탄화되어 결과가 달라진다:
143454 flat.png 14/16 vs fused.png 15/16). 같은 폴더의 정답이면 px 로(2.5 mm 환산), 다른
촬영의 정답이면 기계좌표(mm)로 비교한다(촬영 사이에 픽셀 위치는 바뀌어도 기계좌표는 같다).
2026-09-21 결과(정답 = 143334 samples.json 16개): 143334 **16/16 · 오검출 0**(이걸 못 지키는
변경은 넣지 않는다) · 143454 15 · 오검출 0(놓친 하나는 (713,408) — 3마커 보정이라 기계좌표가
13 mm 어긋나 대조에서 빠졌을 뿐 검출은 된다) · 142900 14 · 오검출 0(놓친 둘: A(611,270)는
d² 중앙값 6 < grow 12, B(702,277)는 엣지 지지율 0.65·직선성 0.76·볼록도 0.35). 142900 의
얼룩(633,290)은 안 잡힌다. `tools/regress.py`(26장): 개수가 준 사진은 153512(옛 18 에 한 칩의
중복 검출 둘·반사광 자락)와 143454(옛 오검출 둘이 빠지고 밝은 칩 하나가 들어옴)뿐. 늘어난
것은 대부분 09-09·09-10 세션의 같은 자리 회색 칩(옛 결과가 놓친 진짜 칩, weak) 이고,
파편은 150901 (532,79) 14.6 mm² · 155608 (667,427) 14.1 · 153716 (552,401) 12.4 볼록도 0.51 ·
(507,441) 10.0 · (543,477) 10.1 — 반사광 사진의 병합 결과(merge_solidity 0.5)라 min_solidity
로는 안 걸리고 전부 weak 다.

촬영마다 로그에 남기는 진단: 브라케팅 여부와 장별 평균 밝기 · 종이 RGB 평균(보정
전→후) · 감지영역 안 254 이상 화소 비율 · 웨이퍼 안 포화 비율(평탄화 **전** 사진에서
잰다 — 나눗셈은 잃은 정보를 되살리지 못한다). 웨이퍼 포화가 10% 를 넘으면
"반사광으로 정보가 사라진 영역 N% · 노출을 더 낮추거나 편광 필터가 필요합니다" 경고.

카메라 설정(설정 창): `width`·`height`·`fourcc`(MJPG/YUY2 — 요청값. 기본 4080×3060 MJPG = 4:3 전체 센서
화각이고 윈도우 카메라 앱과 같다. 8160×6120 은 보간이고 16:9 모드는 위아래가 잘려 화면 위 끝의
헤드 마커가 잘린다. 설정 창 [최대 해상도로] 버튼이 이 값으로 채운다. 올리면 촬영과 검출이 느려진다) · `wb_temperature`(화이트밸런스 색온도, 0 = 카메라 기본.
AUTO_WB 는 끄는데 값을 안 주면 종이가 초록끼를 띤다) · `exposure`(0 = 자동, 값을 주면
`auto_exposure` 가 꺼지고 그 노출로 고정). 저장하면 카메라를 다시 연다.
`bracket*`·`flat_field`·초점 모드/스윕·촬영 프레임 수·재시도도 설정 창 카메라 탭에 있다.

### 카메라 포맷·해상도는 실측한다 (`tools/cam_probe.py`)

카메라는 못 하는 요청을 조용히 가까운 값으로 바꾼다(MJPG 를 달라는데 YUY2 로 열리는
식). 그래서 카메라를 열 때마다 실제 포맷을 로그에 남기고(`카메라 열림 · MJPG 1280x720
30fps`), 요청과 다르면 경고한다(`카메라 포맷 · MJPG 1280x720 요청 · YUY2 1280x720 으로
열림`). YUY2 는 무압축이라 USB 대역폭에 바로 걸려 고해상도가 되지 않는다.

**표기 화소 수(5000만 등)는 믿지 않는다.** USB 카메라의 그런 표기는 대개 보간이라
실제 정보가 늘지 않고 촬영·검출만 느려진다. 앱을 끄고(장치 점유) 다음을 돌린다:

```
python tools/cam_probe.py                              # MJPG·YUY2 x 1280x720 ~ 8000x6000
python tools/cam_probe.py --fourcc MJPG --size 1280x720 --size 1920x1080 --frames 5
```

조합마다 실제로 협상된 폭·높이·fourcc·fps(요청과 다르면 표시), 한 장당 시간(초),
focus_score, 그리고 실질 해상도 지표 `detail`(프레임을 1/2 로 줄였다 다시 키운 뒤
원본과의 RMS 차 — 보간 사진은 0 근처, 진짜 해상도면 뚜렷하게 크다)을 표로 찍는다.
같은 장면을 연속으로 찍으므로 나란히 비교할 수 있다. 해상도를 올렸는데 `detail` 이
오르지 않으면 그 해상도는 보간이다. 카메라를 못 열면 이유를 적고 종료코드 2.

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

이동이 끝나고 `autosave_s`(기본 3초) 동안 새 이동이 없으면 위치를 EEPROM 에 저장한다(자동, 파일 로그에만).
USB 가 빠져 보드가 리셋되면 마지막 저장 이후의 위치는 사라진다.

## 펌웨어

`firmware/stage_v11/` 이 현재 버전이다(V8~V10 은 이전 버전으로 남겨 둔다). 업로드는 Arduino IDE 로 수동.

V10 에서 바뀐 것: 릴레이 극성 `RELAY_ACTIVE_LOW = false`(2026-10-26 실장 확인 — 구동 회로가
HIGH 에서 켜진다, KOINO KH-103-4CL DC24V 소켓형) · 호밍 센서(hs/hw · H1~H8) 제거(물리적으로
없앴다, 그 핀은 설정하지 않는다) · 통신 감시 `wd <ms>`. EEPROM 형식·매직(POSA)은 그대로라
V10 에서 저장한 위치가 복원된다. 그 밖의 명령·출력 형식은 V10 과 같다.

핀은 드라이버 번호로 정의한다. 축 ↔ 드라이버 배정은 `.ino` 맨 위 배정 블록
(`AXIS_X1_DRV` `AXIS_X2_DRV` `AXIS_Y_DRV` `AXIS_Z_DRV`) 4줄로 정한다 — 확정 배정
(2026-10-26): 드라이버1~4 = X1 · X2 · Y · Z. 같은 드라이버 두 번·1~4 밖이면 부팅 때 오류를 찍고
모든 이동을 거부한다. PUL 포트·비트는 부팅 때 계산하므로 배정을 바꿔도 코드는 그대로다.

| 드라이버 | 축 | ENA | DIR | PUL |
|---|---|---|---|---|
| 1 | X1 | D39 | D41 | D5 (PE3) |
| 2 | X2 | D43 | D45 | D4 (PG5) |
| 3 | Y | D47 | D49 | D3 (PE5) |
| 4 | Z | D51 | D53 | D2 (PE4) |

X1·X2 는 서로 다른 포트(PE·PG)라 X 이동의 펄스는 두 번의 연속 쓰기로 나간다(인터럽트를 막은
한 구간 안이라 시차 1µs 미만, 펄스 수·주기는 같다).

릴레이 R1~R10 = D22·24·…·40(짝수, 출력, HIGH = 켜짐). 전원·리셋 때 전부 꺼짐(꺼짐 레벨을 먼저
쓴 뒤 OUTPUT), 명령으로만 켜진다. 펌웨어는 비상정지·`forget` 때 릴레이를 건드리지 않는다 —
끄는 것은 앱의 정책이다(아래 릴레이).

**통신 감시(`wd <ms>`, 0 = 끔)**: 마지막으로 줄을 받은 뒤 그 시간이 지나면 켜진 릴레이를 전부
끄고 `[!] 통신 끊김 · 릴레이 전부 꺼짐` 을 한 번 출력한다. 판정은 `loop()` 에서만 한다 —
블로킹 이동 중에는 판정하지 않고 명령이 끝나는 순간 타이머를 다시 시작하므로 긴 이동이 거짓
경보를 내지 않는다. 앱은 연결 직후 `relay.watchdog_ms`(기본 5000)를 보내고, 감시 시간의 1/3
주기로 `rs` 를 보내 하트비트로 쓴다.

| 명령 | 내용 |
|---|---|
| `jx <±펄스>` `jy` `jz` | **원점 없이도 되는 상대 이동**. 한 번에 8000 펄스(50mm)까지 — 더 먼 거리는 드라이버가 조각으로 나눠 보낸다. 가동범위 밖으로 나가면 그 축 원점 해제 |
| `mx <mm>` `my` `mz` | 절대 이동(mm) |
| `gx <펄스>` `gy` `gz` | 절대 이동(펄스) |
| `fz x [펄스]` `fz y` `fz z` | 밀고 물러나 0 으로 등록. **앱은 쓰지 않는다**(시리얼 모니터용) |
| `zx` `zy` `zz` `z` | 지금 자리를 그 축의 0 으로 등록(`z` 는 X·Y 둘 다) |
| `forget` | 원점 기록 삭제(세 축 모두. 앱이 비상정지 때 보낸다) |
| `r <1~10> <1\|0>` `r all 0` `rs` | 릴레이 켜기·끄기 / 전부 끄기 / 상태 `RS R1=0 … R10=0`(1 = 켜짐) |
| `wd <ms>` | 통신 감시 시간(V11, 0 = 끔) |
| `sp <x펄스> <y펄스>` | 지금 위치를 그 좌표로 세팅 + X·Y 원점 있음 + 저장(사진 호밍이 쓴다) |
| `a <가속도>` `b <시작 속도>` `v <최고 속도>` | 속도 프로파일(앱이 설정 `motion.*` 으로 보낸다) |

V9 에서 Z 축이 붙었다. V10 은 배선이 전부 바뀌어 EEPROM 매직을 `POSA` 로 바꿨다(형식은
V9 와 같다) — V9 이하로 저장한 위치는 '기록 없음' 으로 읽힌다. V11 은 같은 매직이다.

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
Z 를 내리는 깊이는 공구마다의 작업 높이(`tool.items[].z_work_mm`)다 — Z 가동범위보다 크면
저장을 거절한다(예전 `z_measure_mm` 은 히트건 공구의 작업 높이로 이행된다).

### Z 실측 (프로브를 달기 전에)

이 절차는 **끝났다**(2026-09-14). 값이 틀어졌다고 의심되면 같은 방법으로 다시 잰다.

1. ~~펄스/mm~~ — 완료(160). 스텝 10 으로 [Z↓] 를 다섯 번 눌러 실제로 내려온 거리를
   자로 잰다. `Z_PPMM = 160 x (지시 50) / (실제 mm)`. 고칠 때는 펌웨어와
   `core/stage.py` 의 `Z_PPMM` 을 같은 값으로, `Z_MAX`(펄스)도 다시 계산한다.
2. ~~스트로크~~ — 완료(45 mm = 7200 펄스).
3. [Z↓] 인데 위로 올라가면 펌웨어의 `Z_DIR_INVERT` 를 `true` 로 바꾸고 다시 올린다.

**X·Y 를 움직이기 전에는 늘 Z 를 맨 위(0)로 올린다**(좌표 이동 · 샘플 이동 · 원점 복귀 ·
파킹 · 촬영 위치 · 순회 — 엔진이 레시피와 무관하게 강제하는 불변식). Z 원점이 없으면
"Z 원점 없음 · Z 는 두고 이동" 경고만 남기고 X·Y 만 간다. 대신 Z 단계가 있는 레시피는 Z 원점이
없으면 순회 시작을 거절한다. 수동 조그만은 Z 를 올리지 않는다(공구 끝을 표시점에 맞출 때
Z 를 내린 채 조금씩 민다).

## 원점 잡기

**기본은 사진 호밍이다**(아래 [사진 호밍]). 천장 카메라가 Z 축 고정부의 헤드 마커를 찍어
스테이지의 실제 X·Y 를 재고 펌웨어 좌표를 맞춘다 — 원점을 잃었을 때(비상정지 뒤 등)도 이것으로
복구한다. 헤드 보정이 아직 없을 때나 Z 원점은 아래 수동 절차(끝단 밀기 + 등록)로 잡는다.

사람이 보면서 잡는다. **끝단 이동**(미는 것)과 **원점 등록**(0 으로 정하는 것)은
따로다 — 끝에 닿았다고 판단하는 것은 사람이기 때문이다. 축마다 따로 한다.

원점(0)은 **언제나 끝단에서 `origin_gap_mm`(고급, 기본 2 mm) 물러난 자리**다. 판 마커 좌표와
헤드 보정이 이 원점을 기준으로 잡혀 있어서, 물러나는 거리를 바꾸면 둘 다 다시 해야 한다(설정
창이 저장 전에 경고한다) — 이격 없이 등록했더니 모든 샘플이 X·Y 로 2 mm 씩 모자라게 갔다(2026-09-11).

1. [수동 이동] 팝업을 연다. [원점 잡기] 는 축이 행, 동작이 열인 표다.
2. 패드로 대강 끝 근처까지 옮긴다(원점이 없는 축은 `motion.jog_slow_pps`(1500 pps)로 느리게 간다).
3. [밀 거리] 에 남은 거리를 어림해 넣고(1~그 축의 가동범위) X 행의 [밀기] 를 누른다. 펌웨어
   한도는 한 번에 50 mm 지만 드라이버가 나눠 보내므로 한 번에 멀리 갈 수 있다.
   끝에 닿으면 드르륵 소리가 난다(정상) — 그만큼 덜 갔다는 뜻이니 마지막은 짧게.
   가동범위를 벗어나는 순간 그 축의 원점은 자동으로 풀린다(탈조하면 좌표를 못 믿는다).
4. 끝에 닿은 자리에서 X 행의 [등록] — 이격 거리만큼 물러난 뒤 그 자리를 X 의 0 으로 저장한다.
5. Y 행도 같은 방법으로. 두 축이 모두 등록되어야 좌표 이동·순회·파킹이 열린다.
6. Z 행도 같지만 미는 방향이 **위**다(Z 는 위 끝단이 0).

비상정지 뒤에는 앱이 펌웨어의 원점 기록도 지운다(`forget`). 지우지 않으면 다음
부팅에서 믿을 수 없는 원점이 EEPROM 에서 복원된다(2026-09-11 실장).

## 실장 첫 실행 절차 (2026-11 개편 뒤)

1. **펌웨어 V11 업로드**(Arduino IDE · `firmware/stage_v11`). 배정 블록 · `RELAY_ACTIVE_LOW=false` 확인
2. **연결** — 칩이 `COM7 · XY …` 로 바뀌는지, 배너가 `3-AXIS V11` 인지 확인
3. **릴레이** — [설정] → 릴레이: 쓸 릴레이를 사용으로, 이름(히트건 · 쿨러 …), 최대 켜짐 시간.
   정책 기본은 비상정지·정지·오류·종료 때 전부 끄기. 조작 열 릴레이 패널에서 켜고 꺼 본다
4. **원점(임시)** — 헤드 보정 전이므로 수동 이동 → [원점 잡기]로 X·Y·Z 등록
5. **촬영 위치** — 히트건이 웨이퍼를 가리지 않는 좌측 끝 쪽으로 옮겨 [현재 위치를 촬영 위치로]
6. **카메라 · 감지 영역 · 판 보정**(고정 보정) — [설정] → 카메라: [최대 해상도로](4080×3060) · 초점 고정 → 저장 →
   [촬영] → [보정] → 감지 영역 · 판 보정: 새겨진 사각형 네 모서리 클릭 → [적용], 점마다 포인터를 표시점에
   맞춰 [위치 기록] + 사진에서 클릭(4점 이상) → RMS 확인 → [적용]. (예전 판 마커 방식은 보정 방식을
   `판 마커` 로 바꾸고 [보정] → 판 마커: id0~3 마다 기준 포인터를 마커 중심에 맞춰 [기록] → [적용])
7. **헤드 마커 부착 · 보정** — Z 모터 윗면에 헤드 마커(id 10 · 40 mm)를 붙이고 [보정] → 헤드 마커
   → [보정 시작] → 잔차 RMS 가 한계(0.5 mm) 안이면 [적용]
8. **사진 호밍** — 조작 열 [사진 호밍]. 칩이 `위치 확인 정상` 이 되는지 확인
9. **공구 오프셋** — [보정] → 공구 오프셋: 히트건은 방법 2(감열지 자국), 끝이 보이는 공구는 방법 1
10. **레시피** — [순서 편집]: 샘플 이동 → Z 작업 높이 → 히트건 켜기 → 대기 → 측정 → 끄기 → Z 맨 위 …
11. **순회** — 촬영 · 검출 → 확인 후 진행 모드로 2~3개 → 자동 모드로 전체
12. **종료** — 이동 없이 위치만 저장하고 닫는다(릴레이는 정책대로 꺼진다)

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
| `park_here` `capture_here` | 현재 위치를 `park_xy` / `capture_xy` 로 저장 |
| `capture` | `confirm?` — 샘플 편집이 남아 있으면 `confirm:true` 가 있어야 찍는다(편집은 사라진다) |
| `sample_add` | `u, v`(프레임 px) — 촬영본의 그 점을 샘플로 추가 → `ack{of:"sample_add"}` |
| `sample_add_here` | 지금 스테이지 위치를 샘플로 추가(원점·연결 필요, 이동 중 거절) → `ack{of:"sample_add"}` |
| `sample_delete` | `no` — 목록에서 뺀다 |
| `sample_undo` | 마지막 추가·삭제를 취소 |
| `run` | `mode:"auto"\|"confirm"\|"pick"`, `only?:[no]`, `confirm?` — 활성 레시피로 돈다. 레시피 오류·Z 원점 없음(Z 단계)·새 촬영 요구면 `ack{ok:false, needs_confirm:[사유]}` |
| `pause` `resume` `next` `stop` `estop` | |
| `set_on` | `no, on` |
| `set_all` | `on` |
| `measure_here` | |
| `open_out_dir` | 결과 폴더를 탐색기로 연다 |
| `open_results` | results.csv 를 연다(없으면 폴더) |
| `open_log_dir` | 날짜별 로그 폴더를 연다 |
| `preview_start` `preview_stop` | 카메라 미리보기 |
| `list_ports` | 시리얼 포트 목록 → `ack{of:"list_ports", ports:[{device, description}]}` |
| `settings_schema` | → `ack{schema, values, defaults, data_dir, settings_path}` |
| `settings_validate` | `values`(중첩) · `seq` → `ack{ok, errors:{키: 사유}, warnings, seq}` (저장 안 함) |
| `settings_save` | `values`(중첩) 또는 `patch:{점 키: 값}` — 전부 통과해야 저장(부분 저장 없음), 직전본 백업. 실패 `ack{ok:false, errors}`. 예전 최상위 키(`limits`·`z_measure_mm`·`dwell_s` …)도 이행해서 받는다 |
| `settings_export` | → `ack{data, path}` (data/settings_export 에도 쓴다) |
| `settings_import` | `data`(내보내기 JSON 또는 settings.json) — 같은 검증 뒤 저장. 설정 키가 없으면 `bad_format` |
| `relay_set` | `n`(1~10), `on` → `ack{ok, reason}` (`disabled`·`busy`·`not_connected`·`unsupported`·`error`) |
| `relay_all_off` | 전부 끄기(순회 중에도 된다) |
| `tool_select` | `name` — 활성 공구(순회 중 거절) |
| `recipe_select` | `name` — 활성 레시피(순회 중 거절) |
| `photo_home` | 지금 자리에서 헤드 마커로 위치 확인 → `ack{ok, reason: ok\|corrected\|failed\|needs_confirm, d_mm, measured?, recorded?}` |
| `photo_home_apply` | 확인 요구된 결과를 적용(서버가 잰 값으로 sp + save, 120초 안 · 그 사이 이동 없음) |
| `head_calib_start` `head_calib_apply` | 헤드 마커 보정 실행(격자) → `ack{of:"head_calib", result}` / 결과 저장(data/head_calib.json) |
| `plate_shot` | 보정 창 단발 촬영(브라케팅 · 검출 없음) → `ack{of:"plate_shot", frame{id,w,h,url}}`, `state.plate.shot` |
| `plate_pos` | `[위치 기록]` — 지금 스테이지 XY → `ack{of:"plate_pos", x, y}` (원점 · 연결 필요) |
| `plate_calib_fit` | `points[{x,y,u,v}], size[w,h]` — 저장 없이 잔차 계산 → `ack{res[], rms_mm, max_mm, over[], res_limit_mm}` (4점 미만 `reason:"min_points"`) |
| `plate_calib_apply` | 같은 인자 — 4점 이상이면 판 보정 저장(`data/plate_calib.json`) → `ack{rms_mm, max_mm, over[]}`, 4점 미만 · 한 직선은 `ack{ok:false, reason:"min_points"}` |
| `plate_area_apply` | `quad[4×(u,v)], size[w,h]` — 감지 영역 저장. 볼록 · 프레임 안 · 최소 넓이가 아니면 `ack{ok:false, reason:"invalid"}` (카메라 · 해상도가 다르면 기존 판 보정 점은 버린다) |
| `calib_marker_record` `calib_marker_apply` | `id` — 지금 위치를 판 마커 좌표 후보로 기록 → `ack{new, old, diff_mm}` / 저장 |
| `calib_tool_record` | `which:"A"\|"B"` — 방법 1 기록 → `ack{result:{A, B, d1}}` |
| `calib_tool_heat` | `n, s, z_mm` — 방법 2 단독 가열(지금 위치 = P0) → `ack{result:{P0}}` |
| `calib_tool_mark` | `u, v`(촬영본 px) — 자국 중심 → `ack{result:{M, d2}}` |
| `calib_tool_apply` | `method:"1"\|"2"`, `tool` — 결과 d 를 그 공구에 저장 |
| `dry_slip` `dry_stall` | 검증용(--dry 에서만): 실제 위치 어긋남(dx, dy) · 펌웨어가 줄을 못 받은 척(s 초) |
| `exit` | |

`run` 은 검출 경고에 "wafer is cut off" 또는 반사광 50% 이상이 있거나 마커가 3개뿐이면
`ack{of:"run", ok:false, reason:"needs_confirm", needs_confirm:[사유]}` 를 돌려준다.
화면이 확인 모달을 띄우고 `confirm:true` 로 다시 보내야 시작한다.

`sample_*` 넷은 순회 중(일시정지·확인 대기·파킹 포함)·촬영 중·촬영 결과 없음이면 거절한다.

`results.csv` 열: `no, shape, X_mm, Y_mm, value, unit, status, time, source`
(`source` = `detect` | `manual`).

### 샘플 편집

검출이 놓친 칩을 넣고 오검출을 뺀다. 편집은 **그 촬영에만** 속한다.

- **[샘플 추가]**(카메라 머리줄, 켜고 끄는 버튼): 켜면 촬영본으로 바뀌고, 빈 곳 클릭 = 그
  점을 샘플(프로브 목표점)로 추가. 칩 가운데를 클릭한다. 연속 추가 가능. 켜진 동안은
  샘플 그림(윤곽 · 마름모 · 번호) 위 클릭은 선택, 그 밖은 추가다. 미리보기로 바꾸거나
  버튼이 잠기면 저절로 꺼진다.
- **[현재 위치를 샘플로]**(수동 이동 팝업): 조그로 포인터를 칩 가운데에 맞춘 뒤 누른다.
  사진에 거의 안 보이는 같은 재질·반투명 칩용 — 보정 오차가 끼지 않는다.
- **[선택 샘플 삭제]**: 사진·목록·맵에서 선택한 샘플을 뺀다(확인 없음).
- **[되돌리기]**: 마지막 추가·삭제를 한 단계씩 취소. 지운 샘플은 번호·윤곽·상태 그대로 돌아온다.

번호는 다시 매기지 않는다(annotated.jpg · results.csv · 로그의 번호가 어긋나지 않게).
지운 번호는 빈 번호로 남고, 추가는 이번 촬영에서 한 번도 안 쓴 다음 번호를 받는다
(검출 N개면 N+1 부터, 되돌린 추가의 번호도 다시 쓰지 않는다). 목록·순회는 번호순이라
수동 샘플이 마지막이다. 좌표 변환은 이번 촬영의 보정만 쓴다. 편집이 남은 채
[촬영 · 검출]을 누르면 확인 후 찍고 편집은 사라진다.

편집할 때마다 결과 폴더에 `samples_edit.json`(지금 목록 전체, `no · u · v · X · Y · source`)을
다시 쓴다. `samples.json` 은 검출 원본이라 건드리지 않는다. 사람이 고친 목록은 그대로
정답으로 쓸 수 있다:

```bat
python tools/flat_check.py ..\data\out\seq_XXXX --truth ..\data\out\seq_XXXX\samples_edit.json
```

편집마다 `sequence_log.jsonl` 에 `{time, edit:add|delete|undo, via?:click|here, undo_of?:add|delete, no, u, v, X, Y}` 한 줄.

## 설정 체계 (backend/settings_schema.py)

**운용 값은 스키마에만 기본값을 둔다.** 기본값 · 타입 · 범위 · 단위 · 탭 · 그룹 · 라벨 · 설명 ·
적용 시점(즉시 / 다음 촬영 / 재연결 / 다음 순회) · 교차 검증을 항목마다 한 곳에 적고,
`state.DEFAULT_APP` · `APP_KEYS` 는 거기서 만든다. settings.json 이 값을 덮어쓰고, 설정 화면은
`GET /settings/schema`(또는 `settings_schema` 명령)로 자동 생성된다. 키는 점 표기다
(`relay.items` = `settings["relay"]["items"]`). 검출 파라미터(`detect.DEFAULTS`)는 예외로
settings.json 을 직접 편집한다(화면에 있던 `stat_seed` · `stat_grow` 만 스키마로 옮겼다).

- **검증**: 저장 전에 타입·범위·교차 검증(좌표가 가동범위 안 · 공구 작업 높이 ≤ Z 가동범위 ·
  헤드 마커 번호 ≠ 판 마커 번호 · 허용 < 경고 오차 · 확장 < 씨앗 임계 · 레시피 단계 유효성 ·
  사용 안 함 릴레이 참조 금지 …)을 모두 한다. 하나라도 틀리면 아무것도 저장하지 않고 항목별
  사유를 돌려준다(화면은 그 칸 아래에 빨간 한 줄, 저장 버튼 잠금). 화면은 입력이 멈출 때마다
  `settings_validate` 로 미리 검사한다.
- **저장**: settings.json 을 원자적으로 쓰고, 직전본을 `data/settings_backup/settings_YYYYmmdd_HHMMSS.json`
  으로 남긴다(최근 20개). 앱 키만 바꾸고 파일의 나머지(검출 파라미터 · 모르는 키)는 그대로 둔다.
- **이행**: 예전 settings.json 을 그대로 읽는다 — `park_xy` · `marker_mm_xy` 유지, `z_measure_mm` →
  히트건 공구의 `z_work_mm`, `dwell_s` → 기본 레시피의 대기, `autofocus`/`focus:"auto"` →
  `focus_mode` · `focus_sweep`, `capture_xy` 가 없으면 `park_xy`. 모르는 키는 지우지 않는다.
  파일의 값이 검증에 걸리면 그 키만 기본값으로 쓰고 경고를 남긴다(프로그램은 뜬다).
- **내보내기/가져오기**: JSON 파일. 가져오기도 같은 검증을 거치고, 파일에 없는 항목은 지금 값을
  유지한다.
- 원점 이격(`origin_gap_mm`)은 고급 탭에 있고, 바꾸면 판 마커 · 헤드 보정을 다시 해야 한다는
  경고 대화상자가 뜬다.

설정 표(스키마에서 뽑은 것 — 고급 표시는 화면에서 고급 탭에 모인다):

| 탭 | 키 | 기본값 | 단위 | 적용 | 뜻 |
|---|---|---|---|---|---|
| 카메라 | `camera_index` | 1 |  | 재연결 | 카메라 번호 |
| 카메라 | `width` | 4080 | px | 재연결 | 가로 해상도(기본 4:3 전체 센서) |
| 카메라 | `height` | 3060 | px | 재연결 | 세로 해상도 · [최대 해상도로] 버튼(4080×3060 · MJPG) |
| 카메라 | `fourcc` | MJPG |  | 재연결 | 영상 포맷 |
| 카메라 | `wb_temperature` | 5000 | K | 재연결 | 화이트밸런스(운용 고정 · [운용 기본값으로] 버튼이 아래 값들을 한 번에 채움) |
| 카메라 | `exposure` | -5 |  | 재연결 | 노출(운용 고정, 0 = 첫 촬영 자동 노출 값을 세션 동안 재사용 · [노출 다시 찾기]) |
| 카메라 | `focus_mode` | fixed |  | 재연결 | 초점 모드(고정 — 자동초점 헌팅 방지) |
| 카메라 | `focus_sweep` | 켬 |  | 재연결 | 초점 스윕(고정 모드에서 열 때 가장 선명한 값으로) |
| 카메라 | `focus` | -1 |  | 재연결 | 고정 초점값 |
| 카메라 | `focus_sweep` | 끔 |  | 재연결 | 초점 스윕 |
| 카메라 (고급) | `af_settle_s` | 5.0 | s | 다음 촬영 | 자동 초점 대기 최대 |
| 카메라 (고급) | `af_min_wait_s` | 0.5 | s | 다음 촬영 | 자동 초점 최소 대기 |
| 카메라 (고급) | `focus_step` | 15 |  | 재연결 | 스윕 간격 |
| 카메라 (고급) | `focus_settle_s` | 0.35 | s | 재연결 | 스윕 안정 대기 |
| 카메라 | `capture_frames` | 3 |  | 다음 촬영 | 촬영 프레임 수 |
| 카메라 | `bracket` | 끔 |  | 다음 촬영 | 노출 브라케팅 — 끄면 고정 노출 한 장(처음 찾은 노출 재사용 · [노출 다시 찾기]) |
| 카메라 | `bracket_exposure` | [] |  | 다음 촬영 | 브라케팅 노출 목록 |
| 카메라 | `bracket_settle_s` | 0.4 | s | 다음 촬영 | 브라케팅 안정 대기 |
| 카메라 | `flat_field` | 켬 |  | 다음 촬영 | 조명 평탄화 |
| 카메라 | `capture_retries` | 2 |  | 다음 촬영 | 촬영 재시도 |
| 카메라 | `preview_fps` | 4.0 | fps | 즉시 | 미리보기 주기 |
| 카메라 | `preview_max_w` | 1280 | px | 즉시 | 미리보기 최대 가로(촬영 · 검출 · 클릭 좌표는 원본 해상도) |
| 카메라 (고급) | `cam_first_frame_s` | 5.0 | s | 즉시 | 첫 프레임 대기(4백만 화소 초과는 두 배) |
| 카메라 | `cam_fail_limit` | 3 |  | 즉시 | 연속 실패 허용 |
| 카메라 | `cam_reopen_s` | 5.0 | s | 즉시 | 재개방 대기 |
| 카메라 (고급) | `cam_reopen_max_s` | 30.0 | s | 즉시 | 재개방 대기 최대 |
| 스테이지 | `serial_port` | 없음 |  | 재연결 | 시리얼 포트 |
| 스테이지 | `motion.speed_pps` | 6000 | pps | 즉시 | 이동 속도 |
| 스테이지 | `motion.accel` | 40000 | pps² | 즉시 | 가속도 |
| 스테이지 | `motion.start_pps` | 300 | pps | 즉시 | 시작 속도 |
| 스테이지 | `motion.jog_pps` | 6000 | pps | 즉시 | 조그 속도 |
| 스테이지 | `motion.jog_slow_pps` | 1500 | pps | 즉시 | 원점 없을 때 조그 속도 |
| 스테이지 | `jog.steps` | [0.1, 1, 10, 50] |  | 즉시 | 조그 스텝 |
| 스테이지 | `jog.max_mm` | 50.0 | mm | 즉시 | 조그 한 번 한도 |
| 스테이지 | `touch.default_mm` | 5.0 | mm | 즉시 | 끝단 이동 기본 거리 |
| 스테이지 | `touch.max_mm` | 0.0 | mm | 즉시 | 끝단 이동 최대 거리 |
| 스테이지 | `limits.x_max_mm` | 247.6 | mm | 즉시 | X 가동범위 |
| 스테이지 | `limits.y_max_mm` | 247.8 | mm | 즉시 | Y 가동범위 |
| 스테이지 | `limits.z_max_mm` | 45.0 | mm | 즉시 | Z 가동범위 |
| 스테이지 | `autosave_s` | 3.0 | s | 즉시 | 자동 위치 저장 대기 |
| 스테이지 (고급) | `status_poll_s` | 2.0 | s | 즉시 | 상태 조회 주기 |
| 스테이지 (고급) | `serial.line_timeout_s` | 5.0 | s | 재연결 | 응답 대기 |
| 스테이지 (고급) | `serial.move_timeout_s` | 30.0 | s | 재연결 | 이동 응답 대기 |
| 스테이지 (고급) | `serial.boot_wait_s` | 5.0 | s | 재연결 | 부팅 배너 대기 |
| 위치 | `park_xy` | [0.0, 90.0] | mm | 즉시 | 파킹 위치 |
| 위치 | `capture_xy` | [0.0, 90.0] | mm | 다음 촬영 | 촬영 위치 |
| 위치 | `after_run` | park |  | 다음 순회 | 순회 후 갈 곳 |
| 위치 | `flow.capture_lift_z` | 켬 |  | 다음 촬영 | 촬영 전 Z 올리기 |
| 위치 | `flow.capture_goto` | 켬 |  | 다음 촬영 | 촬영 위치로 이동 |
| 위치 | `flow.require_fresh_capture` | 끔 |  | 다음 순회 | 순회 전 새 촬영 요구 |
| 위치 | `flow.after_photo` | 끔 |  | 다음 순회 | 순회 후 확인 촬영 |
| 사진 호밍 | `photohome.marker_id` | 10 |  | 즉시 | 헤드 마커 번호 |
| 사진 호밍 | `photohome.marker_mm` | 40.0 | mm | 즉시 | 헤드 마커 크기 |
| 사진 호밍 | `photohome.dict` | 4X4_50 |  | 즉시 | 마커 사전 |
| 사진 호밍 | `photohome.tol_mm` | 0.3 | mm | 즉시 | 허용 오차 |
| 사진 호밍 | `photohome.warn_mm` | 5.0 | mm | 즉시 | 경고 오차 |
| 사진 호밍 | `photohome.on_connect` | 켬 |  | 재연결 | 연결 직후 위치 판단(이동 없이 한 장. 헤드 보정이 유효할 때만) |
| 사진 호밍 | `photohome.edge_margin_px` | 0 | px | 즉시 | 헤드 마커 화면 끝 여백(0 = 프레임 짧은 변의 3%) |
| 사진 호밍 | `photohome.before_capture` | 켬 |  | 다음 촬영 | 촬영 전 자동 확인 |
| 사진 호밍 | `photohome.after_move` | 끔 |  | 다음 순회 | 샘플 이동 후 재확인 |
| 사진 호밍 | `photohome.after_move_fail` | stop |  | 다음 순회 | 재확인 실패 시 |
| 사진 호밍 | `photohome.grid_n` | 3 |  | 즉시 | 보정 격자 |
| 사진 호밍 | `photohome.grid_margin_mm` | 20.0 | mm | 즉시 | 격자 여백 |
| 사진 호밍 | `photohome.rms_limit_mm` | 0.5 | mm | 즉시 | 보정 잔차 한계 |
| 사진 호밍 (고급) | `photohome.settle_s` | 0.5 | s | 즉시 | 안정 대기 |
| 사진 호밍 (고급) | `photohome.shot_retries` | 3 |  | 즉시 | 마커 촬영 재시도 |
| 공구 | `tool.items` | 목록 2개 |  | 다음 순회 | 공구 |
| 공구 | `tool.active` | 기준 포인터 |  | 다음 순회 | 활성 공구 |
| 릴레이 | `relay.items` | 목록 10개 |  | 즉시 | 릴레이 |
| 릴레이 | `relay.on_estop` | off |  | 즉시 | 비상정지 시 |
| 릴레이 | `relay.on_stop` | off |  | 즉시 | 정지 · 순회 종료 시 |
| 릴레이 | `relay.on_error` | off |  | 즉시 | 오류 시 |
| 릴레이 | `relay.on_exit` | off |  | 즉시 | 종료 · 연결 해제 시 |
| 릴레이 | `relay.watchdog_ms` | 5000 | ms | 즉시 | 통신 감시 시간 |
| 측정 순서 | `recipe.items` | 목록 1개 |  | 다음 순회 | 레시피 |
| 측정 순서 | `recipe.active` | 기본 |  | 다음 순회 | 활성 레시피 |
| 측정 순서 | `measure.driver` | dummy |  | 다음 순회 | 계측기 드라이버 |
| 검출 | `stat_seed` | 25.0 | d² | 다음 촬영 | 검출 씨앗 임계 |
| 검출 | `stat_grow` | 12.0 | d² | 다음 촬영 | 검출 확장 임계 |
| 검출 | `calib.mode` | fixed |  | 다음 촬영 | 보정 방식(고정 / 판 마커). 없는 예전 설정은 fixed 로 이행하고 로그에 한 줄 |
| 검출 | `calib.residual_limit_mm` | 1.0 | mm | 다음 촬영 | 판 보정 잔차 한계(초과 점 빨간 표시) |
| 검출 | `calib.area_margin_mm` | 1.0 | mm | 다음 촬영 | 감지 영역 경계 여유(닿은 검출 버림) |
| 검출 | `calib.est_mm_per_px` | 0.143 | mm/px | 다음 촬영 | 대략 배율 — 판 보정 전 대략 배율(판 사각형 눈금 10 mm 간격의 픽셀 수로 잴 수 있음, 면적 필터에만 씀). 판 보정을 하면 보정값을 씀. 검출 때 쓴 배율과 출처(대략값/판 보정값)를 로그 한 줄로 남김 |
| 검출 | `detect_max_px` | 2000 | px | 다음 촬영 | 검출 최대 변(crop 이 크면 줄여 검출 후 원본 좌표로 복원. 0 = 줄이지 않음) |
| 검출 | `marker_mm` | 30.0 | mm | 다음 촬영 | 판 마커 크기 (markers 모드에서만 보임) |
| 검출 | `marker_mm_xy` | 표 |  | 다음 촬영 | 판 마커 좌표 (markers 모드에서만 보임) |
| 검출 | `calib.min_markers` | 3 |  | 다음 촬영 | 판 마커 최소 개수 (markers 모드에서만 보임) |
| 검출 | `calib.confirm_3markers` | 켬 |  | 다음 순회 | 마커 3개면 확인 (markers 모드에서만 보임) |
| 검출 | `fixed.detector` | fixed |  | 다음 촬영 | 고정 환경 검출기 `fixed` 또는 기존 `legacy` (fixed 모드에서만 보임) |
| 검출 | `fixed.layout` | grid |  | 다음 촬영 | 배치 `grid`(격자 사전정보) 또는 `free` |
| 검출 | `fixed.grid_rows` · `fixed.grid_cols` | 3 · 5 |  | 다음 촬영 | 격자 행 · 열 수(화면 가로 · 세로 어느 쪽이든) |
| 검출 | `fixed.emit_predicted` | 켬 |  | 다음 촬영 | 끝까지 못 찾은 칸도 예측 위치로 샘플(weak, `source=predicted`) |
| 검출 | `fixed.chip_mm` | 11.0 | mm | 다음 촬영 | 예상 칩 긴 변(자동 추정 실패 시 배율로 환산) |
| 검출 | `fixed.use_ref` | 끔 |  | 다음 촬영 | 빈 판 기준 사진을 배경으로 사용(미검증) |
| 검출(고급) | `fixed.seed` | 4.5 |  | 다음 촬영 | 검출 임계 d |
| 검출(고급) | `fixed.local_seeds` | [3.5, 2.8, 2.2] |  | 다음 촬영 | 빈 칸 재탐색 임계 |
| 검출(고급) | `fixed.bg_k_chips` · `fixed.blur_sigma` | 3.0 · 1.5 |  | 다음 촬영 | 배경 커널(칩 길이 배) · 블러 σ |
| 검출(고급) | `fixed.min_solidity` · `fixed.max_aspect` · `fixed.min_fill` | 0.75 · 4.5 · 0.45 |  | 다음 촬영 | 모양 필터 |
| 검출(고급) | `fixed.area_ratio` · `fixed.work_long_px` | [0.25, 3.0] · 60 |  | 다음 촬영 | 면적 범위(예상 칩 배) · 검출용 칩 긴 변 |
| 고급 | `origin_gap_mm` | 2.0 | mm | 즉시 | 원점 이격 |
| 고급 | `file_log` | 켬 |  | 즉시 | 파일 로그 |

설정으로 옮기지 않은 상수: 펌웨어 타이밍(펄스 폭 `PULSE_HIGH_US` · `DIR_SETUP_US` · 펄스/mm ·
기계 한계 `X_MAX/Y_MAX/Z_MAX` · `JOG_MAX_PULSE` 8000) — 펌웨어에 박힌 값이라 앱에서 바꾸면
펌웨어와 어긋나고, 잘못 바꾸면 탈조·충돌로 이어진다. 비상정지 중복 무시 시간(2 s) · 미리보기
그림 크기/화질 · 브라케팅의 자동 노출 탐색 상수 · 검출 파라미터도 운용 값이 아니라 그대로 둔다.

## 좌표 정의 — 기준 포인터와 공구 오프셋

- 스테이지 좌표 **P** 는 "**기준 포인터가 가리키는 점**" 이다(펌웨어가 보고하는 X·Y, 판 마커 좌표와
  같은 좌표계).
- 공구 끝(히트건은 가열 중심 — 노즐이 비스듬해 노즐 바로 아래가 아니다)은 **P + (dx, dy)** 를
  가리킨다. 공구마다 `{name, dx_mm, dy_mm, z_work_mm, note}`, 활성 공구 하나.
- 샘플 **S** 를 공구로 겨냥하려면 스테이지를 **P = S − (dx, dy)** 로 보낸다. **가동범위 검사는 P 로
  한다**(공구를 바꾸면 가동범위 밖 판정(skip)도 다시 한다).
- 순회 · [선택 샘플 이동] 은 활성 공구 오프셋을 적용한다. 맵은 포인터(✚)와 공구 지점(◎)을 따로 그린다.
- 오프셋 보정: 방법 1(맞추기) d = A − B(기준점에 포인터를 맞춘 A, 같은 점에 공구 끝을 맞춘 B),
  방법 2(자국) d = M − P0(P0 에서 단독 가열, 촬영본에서 클릭한 자국 중심 M — 판 보정으로 mm).

## 사진 호밍 (헤드 마커)

Z 축 고정부(Z 모터 윗면 — 높이가 변하지 않는 곳)에 ArUco 마커(헤드 마커, 기본 id 10 · 40 mm ·
4X4_50)를 붙이고, 천장 카메라가 그 마커를 찍어 스테이지의 실제 X·Y 를 계산한다.

- **보정**: 판 마커와 높이가 달라 같은 변환을 쓸 수 없어 헤드 마커 평면 전용 호모그래피를 만든다.
  원점이 잡힌 상태에서 격자 점(n×n, 가동범위 안 · 여백)을 차례로 이동 → Z 맨 위 → 안정 대기 →
  한 장(브라케팅 없이, 실패 시 재시도) → 헤드 마커 중심(모서리 4점 평균 · 서브픽셀) →
  (픽셀 ↔ 스테이지 XY) 쌍 → 최소제곱 호모그래피 → 점별 잔차와 RMS. [적용]을 눌러야
  `data/head_calib.json`(시각 · 카메라 번호 · 해상도 · 점별 잔차 · RMS)에 저장한다. RMS 가 한계를
  넘으면 경고하고 기본 버튼이 [다시]가 된다. 카메라 번호 · 해상도 · 마커 번호가 보정 때와 다르면
  보정 무효(경보 배너)이고 사진 호밍을 막는다.
- **동작**: 이동 없이 지금 자리에서 한 장 → 실제 위치 → 기록된 위치와의 차이 d:
  허용 오차(0.3 mm) 안이면 그대로, 허용~경고(5 mm)면 `sp` 로 자동 보정 + 로그, 경고 초과 또는
  원점 없음이면 대화상자로 차이를 보여 주고 확인 뒤 `sp`(서버가 잰 값으로 · 그 사이 이동이 없을 때만).
  `sp` 뒤 save. 결과는 상단 '위치 확인' 칩(미확인 / 정상 / 보정됨 / 실패).
- 마커가 안 보이면 "헤드 마커 미검출 · 수동 이동으로 시야 안에". 판 마커와 무관하다 — 고정 보정에서 판 마커 없이
  헤드 마커 하나로 촬영 전 확인 · 사진 호밍 · 헤드 보정이 된다.
- **화면 끝**: 헤드 마커가 화면 가장자리(`photohome.edge_margin_px`, 0 이면 짧은 변의 3%) 안쪽에 걸치면
  결과를 쓰지 않고 "헤드 마커가 화면 끝 · 안쪽으로 이동"을 알린다. 헤드 보정 격자 점 중 마커가
  가장자리에 걸리는 점은 건너뛰고(보정 결과 `skipped`) 4점 미만이 되면 실패한다.
- **연결 직후**(`photohome.on_connect`, 기본 켬): 스테이지를 연결하면 헤드 보정이 유효할 때 이동 없이 한
  장 찍어 위치를 판단한다(판정 규칙은 위와 같다 — 허용 안 그대로 / 경고 안 자동 sp / 넘거나 원점 없음은
  확인 후 sp). 마커가 안 보이면 "헤드 마커 미검출"만 알리고 아무것도 움직이지 않는다.
- 촬영 전 자동 확인(기본 켬): 촬영 위치에 도착하면 확인한다. 확인이 필요해도 묻지 않고 경고만
  남기고 촬영은 한다(검출 좌표는 판 보정 · 판 마커 기준이라 촬영 자체는 안전하다).
- 샘플 이동 후 재확인(기본 끔): 도착 후 한 장 → 허용 오차를 넘으면 한 번 바로잡고 다시 이동,
  그래도 넘으면 설정대로 정지(기본) 또는 경고 후 계속.

## 측정 순서(레시피)와 순회 엔진

레시피 = `{name, steps}`. 단계: `move_sample`(첫 단계 고정 — 공구 오프셋 적용) · `z`(작업 높이 /
맨 위 / 숫자 mm) · `relay`(번호 · 켜기/끄기) · `wait`(초) · `measure`(계측기, 지금은 더미) ·
`confirm`(사람이 [다음]). 새 종류는 `backend/recipe.py` 의 정의와 `engine.RUNNERS` 의 실행 함수만
더하면 된다. 기본 레시피 "기본": 샘플 이동 → Z 작업 높이 → 대기 5 s → 측정 → Z 맨 위(릴레이 단계는
사용할 릴레이가 정해지지 않아 넣지 않았다).

- 엔진은 샘플마다 단계를 차례로 실행하고 `sequence` 에 현재 샘플 · 단계 번호 · 설명 · 대기 남은
  시간을 싣는다. 단계마다 시작·끝 시각·결과를 순회 폴더 `steps.jsonl` 에(첫 줄은 레시피 · 공구 이름
  · 오프셋), results.csv · 샘플 상태는 그대로.
- **안전 불변식**(레시피와 무관): X·Y 이동 전에는 항상 Z 를 맨 위로(Z 원점이 있을 때). Z 원점이 없으면
  z 단계를 실행하지 않는다(그런 레시피는 시작부터 거절). 레시피가 Z 맨 위로 끝나지 않으면 경고하고
  (저장은 허용) 엔진이 다음 이동 전에 올린다.
- **정지**: 지금 단계를 즉시 끊고(대기 · 확인 포함, 진행 중인 이동 한 번은 끝까지) → 릴레이 정책 →
  Z 맨 위 → 순회 후 갈 곳(`after_run`) → save. **비상정지**: 아무것도 움직이지 않고 save 도 하지
  않는다. 릴레이 정책만 적용한다.
- **일시정지**: 단계 사이에서 멈춘다. 대기 단계 중이면 남은 시간을 멈춘다. 확인 모드 = 샘플마다
  레시피 끝에 확인 단계를 붙인 것.
- **흐름 조합**: 촬영 = [Z 올리기 → 촬영 위치로 → 사진 호밍 확인 → 촬영 · 검출], 순회 뒤 =
  [릴레이 정책 → Z 올리기 → 확인 촬영(after.png) → 순회 후 갈 곳 → save]. 각 요소는 설정으로
  켜고 끈다(`flow.*` · `photohome.before_capture` · `after_run`) — 흐름을 코드에 박지 않는다.
  `flow.require_fresh_capture` 면 한 번 순회한 촬영으로는 다시 순회하지 않는다.

## 릴레이

R1~R10(히트건 · 쿨러 등 장비 전원). 설정에서 릴레이마다 사용 여부(기본 꺼짐) · 이름 · 최대 켜짐
시간(0 = 제한 없음)을 정한다. 사용 안 함 릴레이는 켜기를 거절한다.

- 정책(전부 끄기 / 그대로, 기본 전부 끄기): 비상정지 · 정지와 순회 종료 · 오류 · 종료와 연결 해제.
  비상정지는 '!' 를 맨 먼저 보내고 정책이 끄기면 **같은 쓰기에** `r all 0` 을 붙인다 — 비상정지
  처리 순서·지연이 늘지 않는다.
- 연결 직후와 릴레이 명령 뒤 `rs` 로 동기화하고, 연결 중에는 감시 시간의 1/3 주기로 `rs` 를 보낸다
  (펌웨어 통신 감시의 하트비트 겸 상태 동기화 · 같은 시리얼 워커 · 이동 중에는 보내지 않는다).
- 최대 켜짐 시간을 넘긴 릴레이는 앱이 끄고 경보를 띄운다. 펌웨어가 `[!] 통신 끊김` 을 보내면
  경보 배너에 띄우고 상태를 다시 읽는다.
- 순회 중 수동 켜기/끄기는 거절한다(전체 끄기만 허용). 화면은 state 가 와야 바뀐다(낙관적 갱신 없음).

## 화면

한 화면에 다 들어간다(페이지 스크롤 없음). 1920×1040 고정 캔버스를 창 크기에 맞춰
축소하고, 내부에서 스크롤하는 곳은 샘플 표와 로그뿐이다. 무채색 바탕에 색은 상태에만
쓰고(ISA-101), 상태는 색과 글자를 함께 바꾼다(IDLE·READY·RUNNING·E-STOP …).

  헤더(상태 필 · X/Y/Z 큰 숫자 · 칩 7개(서버·카메라·스테이지·위치 확인·공구·릴레이·계측기, 두 줄)
       · 설정/보정/종료/비상정지)
  배너(있을 때만: 경보/경고 · 출처별 한 줄 — 스테이지·카메라·검출·보정·순회·통신 감시·릴레이·위치 확인)
  카메라(사진 + 오버레이) │ 스테이지 맵 + 샘플 목록 │ 조작(스테이지 / 촬영·사진 호밍 / 순회 / 릴레이)
  로그 + 상태줄

창(설정 · 보정 · 확인 · 공구 선택)은 `showModal` 을 쓰지 않는다 — 뒤 화면이 inert 가 되어
비상정지를 누를 수 없기 때문이다. `show()` + 막(#dimmer)으로 뒤를 막고, 비상정지 도크
(#estopDock)를 막 위에 띄운다. 수동 이동 팝업은 막 없이 보정 창 위에도 뜬다.

스테이지 맵은 작업영역(limits)을 축척대로 그린다 — 위 X+, 왼쪽 Y+, 50mm 격자,
X 레일과 현재 위치의 빔·캐리지, 마커 4개, 감지영역, 웨이퍼, 샘플 점(상태별 색), 파킹 P ·
촬영 C, 활성 공구의 지점(포인터 + 오프셋, 보라 ◎).

## 운용

- 웨이퍼는 마커 4개 안쪽에 두고, 마커를 가리지 않는다(가리면 12점 보정 → 오차 약 1mm).
- 전원을 끄기 전에 종료 버튼으로 닫는다. 종료는 **위치 저장(`save`)만 하고 스테이지를
  움직이지 않는다** — 다음 실행에서 저장된 위치가 EEPROM 에서 복원된다.
  순회 중·이동 중에는 종료가 거절된다(정지·완료 후 종료). 꺼진 동안 손으로 밀지 않는다.
- 아두이노 IDE 시리얼 모니터와 이 프로그램을 동시에 쓸 수 없다(포트 점유).
- 마커 시트(`assets/aruco_markers_30mm.pdf`)는 반드시 실제 크기(100%)로 인쇄해 평평하게 붙인다.

## 미결

1. **계측기** — `backend/measure.py` 의 `Measurer` 로 붙인다(지금은 더미). 레시피의 측정 단계가 부른다.
2. **사진 단계** — 레시피에 `photo` 단계(예: 가열 뒤 촬영)를 넣을 자리만 구조로 남겼다.
3. **Z 인터록 강화** — X·Y 전 Z 상승은 엔진 불변식이지만 Z 원점이 없으면 경고 후 X·Y 만 간다
   (z 단계 레시피는 거절). 프로브를 달면 Z 원점 없음을 X·Y 이동 거절로 올릴지 정한다.
4. **카메라 케이블** — USB 끊김은 촬영 재시도 · 미리보기 재개방으로 복구하지만 납땜 수리 뒤 다시 본다.
5. **조명** — 밝은 회색/반투명 칩은 웨이퍼와 밝기 차가 4~9% 라 영상만으로 구분되지 않는다.
   확산광으로 바꾸면 반사광과 함께 해결된다.
6. **렌즈 왜곡 보정** — 지금은 호모그래피(평면 가정)뿐이다(판 · 헤드 모두).
