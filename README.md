# 샘플 자동 측정 v2

4인치 웨이퍼(또는 철판) 위 샘플 조각 10~16개를 천장 카메라로 찾고, XY 갠트리가 Z 축의 공구(히트건, 나중에 프로브)를 각 샘플 위로 옮겨
측정 조건(단계 목록)대로 가열 · 냉각 · 측정한다. 이전 프로그램(`main` 브랜치)은 그대로 있다. 데이터는 `..\data_v2`.

## 실행
```
python run.py              # 창(pywebview)
python run.py --no-window  # 서버만 - 브라우저로 http://127.0.0.1:8000/
python run.py --dry        # 시리얼 없이(이동 흉내) + 합성 카메라
python run.py --image P    # 촬영 대신 그 사진
python run.py --cleanup    # 창 없이 남은 이전 실행 정리
```
의존성은 `requirements.txt`. 이전 프로그램이 실행 중이면 "이전 프로그램 실행 중 · 종료 후 실행" 으로 거절한다(COM 포트 · 카메라 공유 불가).

## 화면 (1920×1040 고정 캔버스)
- 머리줄: 이름 · 탭 [측정] [결과] [장비 설정] · 상태 필 · 상태 5칸 · [비상정지](어떤 상태에서도 눌림, 창이 열려 있어도 도크가 막 위에 있음)
- 측정(준비): 왼쪽 사진 보기(실시간 | 촬영 사진 · 판 맞춤 / 전체 · 번호 / 핀 / 쿨러 구역 · 지도 · 로그) · 오른쪽 카드 3개(촬영 · 샘플 · 측정)
- 측정(진행): 온도 그래프(라벨은 오른쪽 바깥 띠 + 지시선) · 진행 미니 판 · 측정 기록 표 / 현재 샘플 · 단계 목록 · 장비 상태 6칸 · 전체 진행 · [일시정지] [중지]
- 결과: 측정 제목 · 요약 · 결과 표(확인 필요 행은 주황 + 사유) · 선택 샘플 상세(온도 곡선)
- 장비 설정: 7단계(카메라 · 판 영역 · 빈 판 사진 · 좌표 보정 · 헤드 위치 보정 · 공구 오프셋 · 측정 장비) + [고급 설정](스키마로 자동 생성) + [이전 프로그램 설정 가져오기]

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
| `tool.items` | [{"name": "기준 포인터", "dx_mm": 0.0, "dy… |  | 공구 |
| `tool.fit_margin_mm` | 0.3 | mm | 끝 모양 맞음 여유 |
| `tool.active` | 기준 포인터 |  | 활성 공구 |
| `relay.items` | [{"enabled": false, "name": "R1", "ma… |  | 릴레이 |
| `relay.on_estop` | off |  | 비상정지 시 |
| `relay.on_stop` | off |  | 정지 · 측정 종료 시 |
| `relay.on_error` | off |  | 오류 시 |
| `relay.on_exit` | off |  | 종료 · 연결 해제 시 |
| `relay.watchdog_ms` | 5000 | ms | 통신 감시 시간 |
| `temp.driver` | none |  | 온도 드라이버 |
| `temp.period_s` | 0.5 | s | 읽기 주기 |
| `contact.driver` | none |  | 접촉 드라이버 |
| `contact.di_pin` | 2 |  | 입력 핀 번호 |
| `contact.di_pullup` | True |  | 내부 풀업 |
| `contact.di_active_high` | False |  | HIGH 가 접촉 |
| `cooler.zones` | {"rows": 3, "cols": 3, "row_edges": n… |  | 쿨러 구역 |
| `cooler.mode` | sample |  | 쿨러 켜는 범위 |
| `cooler.keep_on` | True |  | 샘플이 끝나도 켜 둠 |
| `heater.relay` | 10 |  | 히트건 릴레이 |
| `heater.max_c` | 300.0 | °C | 과열 보호 온도 |
| `heater.sensor_fail_n` | 3 |  | 센서 오류 허용 횟수 |
| `measure.sample_period_s` | 0.0 | s | 측정값 기록 주기 |
| `sim.time_scale` | 1.0 |  | 시간 배율 |
| `sim.ambient_c` | 25.0 | °C | 주변 온도 |
| `sim.heater_c` | 320.0 | °C | 히트건 도달 온도 |
| `sim.cooler_c` | 5.0 | °C | 쿨러 도달 온도 |
| `sim.tau_heat_s` | 15.0 | s | 가열 시간 상수 |
| `sim.tau_cool_s` | 25.0 | s | 냉각 시간 상수 |
| `sim.tau_amb_s` | 120.0 | s | 자연 냉각 시간 상수 |
| `sim.noise_c` | 0.2 | °C | 잡음 |
| `sim.contact_z_mm` | 44.0 | mm | 접촉 Z |
| `condition.items` | [{"name": "기본", "steps": [{"type": "m… |  | 측정 조건 |
| `condition.active` | 기본 |  | 활성 측정 조건 |
| `meter.driver` | none |  | 계측기 드라이버 |
| `meter.channels` | [{"name": "저항", "unit": "Ω", "digits"… |  | 채널 정의 |
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
| `origin_gap_mm` | 2.0 | mm | 원점 이격 |
| `file_log` | True |  | 파일 로그 |

## 메시지 계약
- WebSocket `/ws`: 서버 → 화면 `{"type":"state","state":{...},"log":[...]}`(변할 때마다, 최소 1초) · `{"type":"log","e":{...}}` · `{"type":"ack","id","ok","msg"|"res"}`
- 화면 → 서버 `{"id":n,"cmd":"이름","args":{...}}`. **비상정지는 줄 세우지 않고** `{"cmd":"estop"}`(WS) 와 `POST /estop`(HTTP) 둘 다. 명령은 수신 순간의 비상정지 세대(gen)를 잡아 잠금 뒤 비교한다.
- HTTP: `/preview.jpg` `/shot.jpg` `/ref.jpg` `/snap.jpg` `/refhist/<id>.jpg` `/run/<측정>/<파일>` `/schema` `/health`
- 명령: `apply_position` · `area_set` · `calib_apply` · `calib_click` · `calib_delete` · `calib_record` · `capture` · `dry` · `exclude_set` · `focus_refind` · `goto_named` · `goto_sample` · `head_apply` · `head_calib` · `jog` · `measure_answer` · `measure_pause` · `measure_redo_sample` · `measure_resume` · `measure_start` · `measure_stop` · `move_xy` · `open_folder` · `ref_capture` · `ref_restore` · `register_xy` · `register_z` · `relay` · `results_open` · `sample_add` · `sample_confirm` · `sample_delete` · `sample_exclude` · `sample_here` · `sample_move` · `sample_select` · `settings_import_old` · `settings_save` · `shutdown_check` · `snap` · `tool_m1` · `tool_m2_click` · `tool_m2_heat` · `undo_edit` · `verify_position` · `z_top`
- `state` 주요 키: `phase` · `alarm` · `stage{x,y,z,locked,z_ok,xy_ok,recovery[]}` · `camera` · `calib{status,text,points,area,head}` · `ref` · `shot` · `samples[]` · `sel` · `run{state,steps,results,series,targets,bands}` · `temp` · `relays` · `settings` · `conditions` · `start_block`

## 측정 결과 폴더
`..\data_v2\runs\<YYYYMMDD_HHMMSS>_<측정 조건>\` — `run.json`(측정 조건 · 설정 사본 · 보정 식별자 · 촬영 폴더 · 시작/끝 · 결과) · `samples.csv` · `sample_NN.csv`(시각 s · 온도 °C · 히트건 · 쿨러 · Z · 계측기 채널) · `plate_fit.jpg`. CSV 는 UTF-8 BOM.

## 검증
`python tools/unit_tests.py` · `python tools/fixed_regress.py [--loo]` · `python tools/align_measure.py` · `python tools/e2e.py [이름일부]` · `python tools/shots.py OUT` · `python tools/exit_test.py`.
