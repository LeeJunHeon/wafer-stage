# 샘플 자동 측정 v3 — 2단계 (장비 층 · 안전 감시 · 서버 · 화면 틀 · 수동 화면 · 카메라)

준비 탭은 1단계(장비 연결)만, 수동 탭은 완성, 측정 · 결과 탭은 다음 단계다. 구조 · 하드웨어 값 · 안전 규칙은 CLAUDE.md.

## 설치
```
pip install -r requirements.txt
```
측정 장비(K2110 · K2450)는 NI-VISA 가 필요하다. 없으면 그 장비만 "드라이버 없음"으로 표시하고 프로그램은 계속 돈다.

## 실행
```
python run.py                # 실장비 + 창(최대화, "VO₂ 온도–저항 측정"). 시작할 때 저장된 장비에 자동 연결(settings auto_connect)
python run.py --fake         # 가짜 장비(스테이지 · K2110 · K2450 · 카메라). 실제 포트 · VISA · 카메라를 열지 않는다
python run.py --no-window    # 서버만(브라우저로 http://127.0.0.1:8000)
```
설정은 프로그램 폴더의 `settings.json`(git 제외, 없으면 기본값). 데이터 · 로그는 `..\data_v3\logs\YYYY-MM-DD\{app,serial,visa}.log`.

## 서버 인터페이스
| 경로 | 설명 |
|---|---|
| `GET /` · `/ui/…` | 화면(ui/) |
| `GET /api/camera/preview.jpg` | 카메라 미리보기(가로 1280 JPEG, 1 초에 한 장 갱신, 없으면 204) |
| `GET /api/state` | 상태 한 덩어리(장비 연결 · 위치 · 원점 · 릴레이 · 온도 · 저항 · 비상정지 잠금 · 경보 · 히트건 켠 시각) |
| `WS /ws` | 상태를 바뀔 때마다 + 1 초마다 보낸다. 명령 `{"id", "cmd", "args"}` → `{"type":"reply","id","ok","msg"\|"result"}` |
| `POST /api/estop` · `POST /api/estop/release` | 비상정지 · 비상정지 풀기 |
| `POST /api/cmd/{name}` | WS 와 같은 명령 |
| `GET · PUT /api/settings` | 설정 표 · 바꾸기(전부 검증 통과해야 저장) |

명령: `devices`(시리얼 포트 이름 · VISA 주소 · 카메라 번호) · `connect_camera(index)` · `disconnect_camera` · `monitor(on)` · `connect_stage(port)` · `disconnect_stage` · `connect_meter(kind, addr)` · `disconnect_meter(kind)` · `jog(axis, mm)` · `goto(x, y, z)` · `z_top` ·
`set_origin(axis, mode=here|end)` · `relay_on(n)` · `relay_off(n)` · `relays_off` · `read_temp` · `read_resistance` · `raw(target, line)` · `estop` · `estop_release`.
거절되면 `ok: false` 와 쉬운 한국어 한 문장 `msg`. 조그는 이동 중이면 대기 칸 하나에 받아 두고 `{"queued": true}` 를 돌려준다(같은 축 · 같은 방향이면 거리를 더함). `state.move` = 이동 중 정보(무엇 · 목표 X Y Z · 대기 조그).

## 폴더
`app/`(프로그램) · `ui/`(화면) · `sim/`(가짜 장비) · `tests/`(pytest) · `firmware/stage_v11/`(펌웨어, 고치지 않음).

## 시험
```
python -m compileall -q app sim run.py
python -m pytest -q       # 가짜 장비, 약 10~20 초
```
