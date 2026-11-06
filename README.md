# 샘플 자동 측정 v2

4인치 웨이퍼(또는 철판) 위 샘플 조각 10~16개를 천장 카메라로 찾고, XY 갠트리가 Z 축의 공구(히트건, 나중에 프로브)를 각 샘플 위로 옮겨
측정 조건(단계 목록)대로 이동 · 접촉 높이로 내리기 · 쿨러 · 히트건 · 대기 · 확인 대기를 한다.
**측정(온도 · 계측기 · 접촉 판정)은 별도 프로그램이 맡는다** - 이 프로그램은 "카메라 샘플 인식 + 모터 제어"와 쿨러 · 히트건 릴레이만 다룬다.
이전 프로그램(`main` 브랜치)은 그대로 있다. 데이터는 `..\data_v2`.

## 실행
```
python run.py              # 창(pywebview)
python run.py --no-window  # 서버만 - 브라우저로 http://127.0.0.1:8000/
python run.py --dry        # 시리얼 없이(이동 흉내) + 합성 카메라
python run.py --fake-serial # 펌웨어 흉내(tools/fake_v11.py) + 합성 카메라 - 진짜 시리얼 코드 경로로 시험
python run.py --image P    # 촬영 대신 그 사진
python run.py --cleanup    # 창 없이 남은 이전 실행 정리
```
의존성은 `requirements.txt`. 이전 프로그램이 실행 중이면 "이전 프로그램 실행 중 · 종료 후 실행" 으로 거절한다(COM 포트 · 카메라 공유 불가).

## 화면 (1920×1040 고정 캔버스)
- 머리줄: 이름 · 탭 [측정] [결과] [장비 설정] · 상태 필 · 상태칸(평소: 스테이지 · 카메라 · 좌표 보정 · 릴레이 / 측정 중: 위치 · 쿨러 · 히트건) · [비상정지](어떤 상태에서도 눌림, 창이 열려 있어도 도크가 막 위에 있음)
- 띠: 경보 또는 원점 회복. **띠의 모든 안내는 누를 수 있는 버튼이다**(비상정지 · 재부팅 · 원점 없음 → [Z 원점 잡기] → [위치 확인] → [적용] 또는 [X 원점 잡기] [Y 원점 잡기] · 통신 끊김 → [연결 확인] · 카메라 위치 → [다시 촬영] …)
- 측정(준비): 왼쪽 사진 보기(실시간 | 촬영 사진 · 판 맞춤 / 전체 · 번호 / 핀 / 지도 · 소식) · 오른쪽 카드 3개(촬영 · 샘플 · 측정). 측정 조건은 [편집] 에서 단계를 고친다.
- 측정(진행): 왼쪽 판 맞춤 촬영 사진(샘플 상태 색: 완료 녹색 · 진행 파랑 · 대기 회색 · 오류 빨강) / 오른쪽 현재 샘플 · 단계 목록(진행 중 단계는 남은 시간, 확인 대기면 큰 [다음]) · 릴레이 상태(쿨러 · 히트건) · 전체 진행 · [일시정지] [중지]
- 결과(측정 기록): 측정 목록 + 표(번호 · X · Y · 접촉 Z · 시작 · 끝 · 결과 · 사유) · [CSV 내보내기] [결과 폴더 열기]. 결과는 완료 / 오류 / 중지됨 / 측정 제외
- 장비 설정 8단계: 1 스테이지(포트 · 펌웨어 · 연결 시 저장 기록 · 축별 원점 · [Z/X/Y 원점 잡기] · [대기 위치로] · 조그) · 2 카메라 · 3 판 영역 · 4 빈 판 사진 · 5 좌표 보정 · 6 헤드 위치 보정(헤드 마커 상자 표시) · 7 접촉 높이 · 8 릴레이(히트건 R10 · 쿨러 R1–R9 · 쿨러 구역 3×3 · [2 s 켜 보기]) + [고급 설정] + [이전 프로그램 설정 가져오기]. 카메라를 옮기면 3–6 다시 설정.
- 기록 보기: [기록] [시리얼] 탭(보낸 줄 · 받은 줄 원문 - 실장비 첫 시험 확인용)

## 측정 조건 (단계는 이 일곱 가지뿐)
샘플 위로 이동(Z 맨 위 확인 뒤 X · Y, 첫 단계 고정) · 접촉 높이로 내리기 · 쿨러 켜기/끄기(샘플 구역 / 전부) · 히트건 n s(시간이 되면 자동으로 끔, `heater.max_on_s` 상한 120 s) · 대기 n s · 확인 대기(사람이 [다음]) · Z 맨 위.
기본 측정 조건(값은 예시, [편집] 에서 바꾼다): **접촉 시험** 이동 → 접촉 높이 → 대기 2 s → Z 맨 위 / **수동 측정** 이동 → 쿨러 켜기(구역) → 접촉 높이 → 확인 대기 → 쿨러 끄기 → Z 맨 위 / **가열** 이동 → 쿨러 켜기(구역) → 접촉 높이 → 대기 30 s → 히트건 20 s → 대기 60 s → 쿨러 끄기 → Z 맨 위.

## 접촉 높이 (고정 절대값)
Z 원점에서 핀이 샘플에 닿는 높이를 한 번 정해 두고(`contact_z_mm`, 기본은 비어 있음) 측정 때는 그 절대 높이로 내린다. **접촉 판정은 하지 않는다.** `approach_mm`(기본 2) 앞까지는 보통 속도, 남은 거리는 느린 속도(`motion.jog_slow_pps`).
비어 있으면 접촉 높이 단계가 있는 측정 조건은 시작을 거절한다("접촉 높이 필요 · 장비 설정 7"). 내리기는 X · Y 가 샘플 위일 때만(허용 0.5 mm).

## 원점 (펌웨어 `fz`)
축마다 [Z 원점 잡기] [X 원점 잡기] [Y 원점 잡기]: 끝단 쪽으로 최대 10 mm 를 느리게 밀고 → 2 mm 물러나 0 → EEPROM 저장(응답 "--- 원점 설정 완료 (저장됨) ---" / "--- 중단됨 ---"). X · Y 는 Z 원점이 있고 Z 가 맨 위(ST 로 확인)일 때만.
펌웨어 위치가 남아 있으면(비상정지 뒤 등) 끝단 3 mm 앞까지 자동으로 다가간 뒤 `fz`, 없으면 조그로 끝단 10 mm 안까지 옮긴 뒤 누른다. 대기 위치 도착 · 측정 끝 · 종료 때 `save` 를 보내 다음 부팅에 원점이 복원된다.
연결 직후 원점이 복원됐고 헤드 위치 보정이 있으면 움직이지 않고 사진으로 위치를 한 번 확인한다(차이가 한도를 넘으면 경고만).

## 설정 표 (운용 기본값은 `backend/settings_schema.py` 에만 있다)
| 키 | 기본값 | 단위 | 이름 |
|---|---|---|---|
| `camera_index` | 1 |  | 카메라 번호 |
| `width` | 4080 | px | 가로 해상도 |
| `height` | 3060 | px | 세로 해상도 |
| `fourcc` | MJPG |  | 영상 포맷 |
| `wb_temperature` | 5000 | K | 화이트밸런스 |
| `exposure` | -5 |  | 노출 |
| `focus_mode` | fixed |  | 초점 모드 |
| `focus` | -1 |  | 고정 초점값 |
| `focus_sweep` | True |  | 초점 스윕 |
| `af_settle_s` | 5.0 | s | 자동 초점 대기 최대 |
| `af_min_wait_s` | 0.5 | s | 자동 초점 최소 대기 |
| `focus_step` | 15 |  | 스윕 간격 |
| `focus_settle_s` | 0.35 | s | 스윕 안정 대기 |
| `capture_frames` | 3 |  | 촬영 프레임 수 |
| `capture_retries` | 2 |  | 촬영 재시도 |
| `preview_fps` | 4.0 | fps | 실시간 주기 |
| `preview_max_w` | 1280 | px | 실시간 최대 가로 |
| `cam_first_frame_s` | 5.0 | s | 첫 프레임 대기 |
| `cam_fail_limit` | 3 |  | 연속 실패 허용 |
| `cam_reopen_s` | 5.0 | s | 재개방 대기 |
| `cam_reopen_max_s` | 30.0 | s | 재개방 대기 최대 |
| `serial_port` | None |  | 시리얼 포트 |
| `motion.speed_pps` | 6000 | pps | 이동 속도 |
| `motion.accel` | 40000 | pps² | 가속도 |
| `motion.start_pps` | 300 | pps | 시작 속도 |
| `motion.jog_pps` | 6000 | pps | 조그 속도 |
| `motion.jog_slow_pps` | 1500 | pps | 원점 없을 때 조그 속도 |
| `jog.steps` | [0.1, 1, 10, 50] |  | 조그 스텝 |
| `jog.max_mm` | 50.0 | mm | 조그 한 번 한도 |
| `touch.default_mm` | 5.0 | mm | 끝단 이동 기본 거리 |
| `touch.max_mm` | 0.0 | mm | 끝단 이동 최대 거리 |
| `limits.x_max_mm` | 247.6 | mm | X 가동범위 |
| `limits.y_max_mm` | 247.8 | mm | Y 가동범위 |
| `limits.z_max_mm` | 45.0 | mm | Z 가동범위 |
| `autosave_s` | 3.0 | s | 자동 위치 저장 대기 |
| `status_poll_s` | 2.0 | s | 상태 조회 주기 |
| `serial.line_timeout_s` | 5.0 | s | 응답 대기 |
| `serial.move_timeout_s` | 30.0 | s | 이동 응답 대기 |
| `serial.boot_wait_s` | 5.0 | s | 부팅 배너 대기 |
| `park_xy` | [0.0, 90.0] | mm | 대기 위치 위치 |
| `capture_xy` | [0.0, 90.0] | mm | 촬영 위치 |
| `after_run` | park |  | 측정 후 갈 곳 |
| `flow.capture_lift_z` | True |  | 촬영 전 Z 올리기 |
| `flow.capture_goto` | True |  | 촬영 위치로 이동 |
| `flow.require_fresh_capture` | False |  | 측정 전 새 촬영 요구 |
| `flow.after_photo` | False |  | 측정 후 확인 촬영 |
| `photohome.marker_id` | 10 |  | 헤드 마커 번호 |
| `photohome.marker_mm` | 40.0 | mm | 헤드 마커 크기 |
| `photohome.dict` | 4X4_50 |  | 마커 사전 |
| `photohome.tol_mm` | 0.3 | mm | 허용 오차 |
| `photohome.warn_mm` | 5.0 | mm | 경고 오차 |
| `photohome.on_connect` | True |  | 연결 직후 위치 판단 |
| `photohome.edge_margin_px` | 0 | px | 화면 끝 여백 |
| `photohome.after_move` | False |  | 샘플 이동 후 재확인 |
| `photohome.after_move_fail` | stop |  | 재확인 실패 시 |
| `photohome.grid_n` | 3 |  | 보정 격자 |
| `photohome.grid_margin_mm` | 20.0 | mm | 격자 여백 |
| `photohome.rms_limit_mm` | 0.5 | mm | 보정 잔차 한계 |
| `photohome.settle_s` | 0.5 | s | 안정 대기 |
| `photohome.shot_retries` | 3 |  | 마커 촬영 재시도 |
| `tool.items` | [{"name": "기준 포인터", "dx_mm": 0.0, "dy_mm… |  | 공구 |
| `tool.fit_margin_mm` | 0.3 | mm | 끝 모양 맞음 여유 |
| `tool.active` | 기준 포인터 |  | 활성 공구 |
| `relay.items` | [{"enabled": true, "name": "쿨러 1", "max_… |  | 릴레이 |
| `relay.on_estop` | off |  | 비상정지 시 |
| `relay.on_stop` | off |  | 정지 · 측정 종료 시 |
| `relay.on_error` | off |  | 오류 시 |
| `relay.on_exit` | off |  | 종료 · 연결 해제 시 |
| `relay.watchdog_ms` | 5000 | ms | 통신 감시 시간 |
| `cooler.zones` | {"rows": 3, "cols": 3, "row_edges": null… |  | 쿨러 구역 |
| `heater.relay` | 10 |  | 히트건 릴레이 |
| `heater.max_on_s` | 120.0 | s | 히트건 최대 시간 |
| `contact_z_mm` | None | mm | 접촉 높이 |
| `approach_mm` | 2.0 | mm | 느린 구간 |
| `sim.time_scale` | 1.0 |  | 시간 배율 |
| `condition.items` | [{"name": "접촉 시험", "steps": [{"type": "m… |  | 측정 조건 |
| `condition.active` | 접촉 시험 |  | 활성 측정 조건 |
| `data_dir` | ../data_v2 |  | 데이터 폴더 |
| `capture.settle_s` | 0.8 | s | 이동 뒤 안정 시간 |
| `capture.fresh_frames` | 5 |  | 버릴 프레임 수 |
| `calib.residual_limit_mm` | 1.0 | mm | 좌표 보정 잔차 한계 |
| `calib.area_margin_mm` | 1.0 | mm | 판 영역 경계 여유 |
| `calib.est_mm_per_px` | 0.143 | mm/px | 대략 배율 |
| `fixed.layout` | auto |  | 배치 |
| `fixed.grid_rows` | 3 |  | 격자 행 수 |
| `fixed.grid_cols` | 5 |  | 격자 열 수 |
| `fixed.emit_predicted` | True |  | 예측 위치 샘플 |
| `fixed.chip_mm` | 11.0 | mm | 예상 칩 긴 변 |
| `fixed.grid_min_ratio` | 0.8 |  | 격자 인라이어 비율 |
| `fixed.use_ref` | True |  | 빈 판 사진 사용 |
| `fixed.ref_seed` | 5.0 |  | 차이 임계(d) |
| `fixed.ref_faint` | 2.5 |  | 약한 칩 임계 |
| `fixed.ref_area_ratio` | [0.35, 3.5] |  | 면적 범위(배) |
| `fixed.ref_min_solidity` | 0.7 |  | 최소 볼록도 |
| `fixed.ref_max_aspect` | 5.0 |  | 최대 종횡비 |
| `fixed.ref_open` | 5 |  | 열기 커널 |
| `fixed.ref_close` | 9 |  | 닫기 커널 |
| `fixed.ref_thin_k` | 0.1 |  | 긁힘 떼어 내기(칩 길이 배) |
| `fixed.ref_edge_k` | 0.4 |  | 경계 근처 제외(칩 길이 배) |
| `fixed.ref_check_blobs` | 6 |  | 기준 점검 · 큰 덩어리 수 |
| `fixed.ref_check_resid` | 0.6 |  | 기준 점검 · 정렬 잔차 |
| `fixed.cam_shift_warn_mm` | 2.0 | mm | 이동 경고 |
| `fixed.cam_shift_max_mm` | 10.0 | mm | 이동 한도 |
| `fixed.seed` | 4.5 |  | 검출 임계(d) |
| `fixed.local_seeds` | [3.5, 2.8, 2.2] |  | 빈 칸 재탐색 임계 |
| `fixed.bg_k_chips` | 3.0 |  | 배경 커널(칩 길이 배) |
| `fixed.blur_sigma` | 1.5 | px | 블러 σ |
| `fixed.min_solidity` | 0.75 |  | 최소 볼록도 |
| `fixed.max_aspect` | 4.5 |  | 최대 종횡비 |
| `fixed.min_fill` | 0.45 |  | 최소 채움률 |
| `fixed.area_ratio` | [0.25, 3.0] |  | 면적 범위(배) |
| `fixed.work_long_px` | 60 | px | 작업 칩 길이 |
| `calib.outlier_n` | 4.0 |  | 이상점 배수 |
| `calib.area_size_mm` | [154.0, 154.0] |  | 판 영역 실제 크기 |
| `sample_add.snap` | True |  | 클릭 스냅 |
| `file_log` | True |  | 파일 로그 |

## 메시지 계약
- WebSocket `/ws`: 서버 → 화면 `{"type":"state","state":{...},"log":[...]}`(변할 때마다, 최소 1초) · `{"type":"log","e":{...}}` · `{"type":"ack","id","ok","msg"|"res"}`
- 화면 → 서버 `{"id":n,"cmd":"이름","args":{...}}`. **비상정지는 줄 세우지 않고** `{"cmd":"estop"}`(WS) 와 `POST /estop`(HTTP) 둘 다. 명령은 수신 순간의 비상정지 세대(gen)를 잡아 잠금 뒤 비교한다.
- HTTP: `/preview.jpg` `/shot.jpg` `/ref.jpg` `/snap.jpg` `/refhist/<id>.jpg` `/run/<측정>/<파일>` `/serial.json`(시리얼 탭) `/schema` `/health`
- 명령: `alarm_clear` · `apply_position` · `area_set` · `calib_apply` · `calib_click` · `calib_delete` · `calib_record` · `capture` · `contact_clear` · `contact_save` · `contact_try` · `dry` · `exclude_set` · `focus_refind` · `goto_named` · `goto_sample` · `head_apply` · `head_calib` · `heater_off_retry` · `jog` · `link_check` · `measure_answer` · `measure_pause` · `measure_redo_sample` · `measure_resume` · `measure_start` · `measure_stop` · `move_xy` · `open_folder` · `origin`(축 x|y|z) · `reconnect` · `ref_capture` · `ref_restore` · `relay` · `relay_test` · `results_open` · `sample_add` · `sample_confirm` · `sample_delete` · `sample_exclude` · `sample_here` · `sample_move` · `sample_select` · `settings_import_old` · `settings_save` · `shutdown_check` · `snap` · `undo_edit` · `verify_position` · `z_top`
- `state` 주요 키: `phase` · `alarm` · `band{text,next,err,actions[]}` · `stage{x,y,z,locked,z_ok,xy_ok,done,fw_homed,recovery[]}` · `eeprom` · `port` · `fw` · `contact{z,approach}` · `camera` · `calib{status,text,points,area,head}` · `ref` · `shot` · `samples[]` · `sel` · `run{state,steps,results,cur_sample,waiting}` · `relays` · `settings` · `conditions` · `start_block`

## 측정 결과 폴더
`..\data_v2\runs\<YYYYMMDD_HHMMSS>_<측정 조건>\` — `run.json`(측정 조건 · 설정 사본 · 보정 식별자 · 촬영 폴더 · 시작/끝 · 결과) · `samples.csv`(번호 · X · Y · 쿨러 · 접촉 Z · 시작 · 끝 · 결과 · 사유) · `plate_fit.jpg`. CSV 는 UTF-8 BOM.

## 검증
`python tools/unit_tests.py` · `E2E_MODE=fake python tools/e2e.py`(fake-serial) · `python tools/stress_fake.py 300`(동시성 부하) · `python tools/clicks.py`(클릭 수) · `python tools/fixed_regress.py [--loo]` · `python tools/align_measure.py` · `python tools/e2e.py [이름일부]` · `python tools/shots.py OUT` · `python tools/exit_test.py` · `python tools/flows.py`(화면 흐름: 첫 실행 · 비상정지 회복 · 막다른 상태 점검, playwright) · `python tools/perf4080.py`(4080×3060 성능).
