# 샘플 자동 측정 v3 — 1단계 (장비 층 · 안전 감시 · 서버)

화면은 2단계에서 만든다. 지금은 서버(`127.0.0.1:8000`)와 가짜 장비로 확인한다. 구조 · 하드웨어 값 · 안전 규칙은 CLAUDE.md.

## 설치
```
pip install -r requirements.txt
```
측정 장비(K2110 · K2450)는 NI-VISA 가 필요하다. 없으면 그 장비만 "드라이버 없음"으로 표시하고 프로그램은 계속 돈다.

## 실행
```
python run.py            # 실장비: settings.json 의 stage_port(기본 COM5)를 사람이 연결 명령으로 연다
python run.py --fake     # 가짜 장비(스테이지 · K2110 · K2450). 실제 포트 · VISA 를 열지 않는다
```
설정은 프로그램 폴더의 `settings.json`(git 제외, 없으면 기본값). 데이터 · 로그는 `..\data_v3\logs\YYYY-MM-DD\{app,serial,visa}.log`.

## 서버 인터페이스
| 경로 | 설명 |
|---|---|
| `GET /api/state` | 상태 한 덩어리(장비 연결 · 위치 · 원점 · 릴레이 · 온도 · 저항 · 비상정지 잠금 · 경보 · 히트건 켠 시각) |
| `WS /ws` | 상태를 바뀔 때마다 + 1 초마다 보낸다. 명령 `{"id", "cmd", "args"}` → `{"type":"reply","id","ok","msg"\|"result"}` |
| `POST /api/estop` · `POST /api/estop/release` | 비상정지 · 비상정지 풀기 |
| `POST /api/cmd/{name}` | WS 와 같은 명령 |
| `GET · PUT /api/settings` | 설정 표 · 바꾸기(전부 검증 통과해야 저장) |

명령: `devices` · `connect_stage(port)` · `disconnect_stage` · `connect_meter(kind, addr)` · `disconnect_meter(kind)` · `jog(axis, mm)` · `goto(x, y, z)` · `z_top` ·
`set_origin(axis, mode=here|end)` · `relay_on(n)` · `relay_off(n)` · `relays_off` · `read_temp` · `read_resistance` · `raw(target, line)` · `estop` · `estop_release`.
거절되면 `ok: false` 와 쉬운 한국어 한 문장 `msg`.

## 폴더
`app/`(프로그램) · `sim/`(가짜 장비) · `tests/`(pytest) · `firmware/stage_v11/`(펌웨어, 고치지 않음).

## 시험
```
python -m compileall -q app sim run.py
python -m pytest -q       # 가짜 장비, 약 10~20 초
```
