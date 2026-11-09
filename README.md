# 샘플 자동 측정 v2

4인치 웨이퍼(또는 철판) 위 샘플 조각 10~16개를 천장 카메라로 찾고, XY 갠트리가 Z 축의 공구(히트건, 나중에 프로브)를 각 샘플 위로 옮겨
측정 조건(단계 목록)대로 이동 · 접촉 높이로 내리기 · 쿨러 · 히트건 · 대기 · 확인 대기를 한다.
이 프로그램은 "카메라 샘플 인식 + 모터 제어"와 쿨러 · 히트건 릴레이, 그리고 **R–T 측정(저항–온도)** 을 다룬다. 접촉 판정은 하지 않는다. R–T 측정은 K2110(열전대 온도) · K2450(전압 걸고 전류 측정)을 pyvisa(NI-VISA)로 제어하고, 히트건 켜기/끄기로 온도 속도를 맞춘다.
모터는 사람이 조그로 핀을 샘플에 대고(측정 탭 "이 위치 측정") 재거나, 샘플 순회의 단계 `R–T 측정` 이 같은 코드를 부른다.
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
장치를 뺀 채 시작해 보려면(시험): `WAFER2_MISSING=stage,camera,visa python run.py --dry --no-window`.
의존성은 `requirements.txt`. 이전 프로그램이 실행 중이면 "이전 프로그램 실행 중 · 종료 후 실행" 으로 거절한다(COM 포트 · 카메라 공유 불가).

## 화면 (1920×1040 고정 캔버스)
- 머리줄: 이름 · 탭 [측정] [결과] [장비 설정] · 상태 필 · 상태칸(평소: 스테이지 · 카메라 · 좌표 보정 · 릴레이 / 측정 중: 위치 · 쿨러 · 히트건) · [비상정지](어떤 상태에서도 눌림, 창이 열려 있어도 도크가 막 위에 있음)
- 띠: 경보 또는 원점 회복. **띠의 모든 안내는 누를 수 있는 버튼이다**(비상정지 · 재부팅 · 원점 없음 → [Z 원점 잡기] → [위치 확인] → [적용] 또는 [X 원점 잡기] [Y 원점 잡기] · 통신 끊김 → [연결 확인] · 카메라 위치 → [다시 촬영] …)
- 측정 탭 머리 [이 위치 측정 | 샘플 순회]. **이 위치 측정**: 왼쪽 위 저항–온도 그래프(세로 로그 · 가열 주황 / 냉각 파랑 · 회차가 늦을수록 진하게 · 25–90 °C × 1E+2–1E+6 Ω 로 시작해 값에 따라 넓어짐) · 왼쪽 아래 온도–시간 그래프(목표 온도선 · 축 아래 띠로 히트건 켜짐 구간) — 라벨 · 숫자는 그림 바깥 띠에 두고 지시선으로만 잇는다 · 오른쪽 위 위치 카드(X · Y · Z · 조그 · [접촉 높이로 내리기] · [Z 맨 위] · 샘플 고르기 + [샘플 위로]) · 측정 조건 카드(온도 · 온도 제어 · 안전 · K2450 · 열전대 · 쿨러 · 저장, 마지막 값 기억, [기본값으로]) · 상태 카드(온도 · 저항 · 전류 · 단계 · 경과 · 히트건/쿨러 · [장비 연결] · ⋯ [최대 속도 재기] [NAS 로 다시 복사] · [측정 시작] [중지], 시작 못 하는 사유 한 줄).
- 측정(샘플 순회 · 준비): 왼쪽 사진 보기(실시간 | 촬영 사진 · 판 맞춤 / 전체 · 번호 / 핀 / 지도 · 소식) · 오른쪽 카드 3개(촬영 · 샘플 · 측정). 측정 조건은 [편집] 에서 단계를 고친다.
- 측정(진행): 왼쪽 판 맞춤 촬영 사진(샘플 상태 색: 완료 녹색 · 진행 파랑 · 대기 회색 · 오류 빨강) / 오른쪽 현재 샘플 · 단계 목록(진행 중 단계는 남은 시간, 확인 대기면 큰 [다음]) · 릴레이 상태(쿨러 · 히트건) · 전체 진행 · [일시정지] [중지]
- 결과(측정 기록): 위쪽 [샘플 순회 기록 | R–T 측정 기록]. 샘플 순회 = 측정 목록 + 표(번호 · X · Y · 접촉 Z · 시작 · 끝 · 결과 · 사유) · [CSV 내보내기] [결과 폴더 열기]. 결과는 완료 / 오류 / 중지됨 / 측정 제외. R–T = 시각 · File name · 회차 · 결과 · [그래프 보기] [폴더 열기]
- 장비 설정 8단계: 1 스테이지(포트 · 펌웨어 · 연결 시 저장 기록 · 축별 원점 · [Z/X/Y 원점 잡기] · [대기 위치로] · 조그) · 2 카메라 · 3 판 영역 · 4 빈 판 사진 · 5 좌표 보정 · 6 헤드 위치 보정(헤드 마커 상자 표시) · 7 접촉 높이 · 8 측정 장비 · 릴레이(VISA 주소 목록에서 K2110 · K2450 고르기 · [연결 시험](장비 이름 · 온도 1회 · 저항 1회, 저항 1회를 잴 때만 출력을 짧게 켜고 바로 끔) · 히트건 R10 · 쿨러 R1–R9 · 쿨러 구역 3×3 · [2 s 켜 보기]) + [고급 설정] + [이전 프로그램 설정 가져오기]. 카메라를 옮기면 3–6 다시 설정.
- 기록 보기: [기록] [시리얼] 탭(보낸 줄 · 받은 줄 원문 - 실장비 첫 시험 확인용)

## 장치 상태 · 자동 다시 연결 · 빈 상태
- **상태는 실제 장치에서만 만든다**(설정값이 있다고 연결 · 완료로 보이지 않는다). 장치 넷 - 스테이지 · 카메라 · K2110 · K2450 - 마다 `state.dev.<장치>.state` = `ok`(연결됨) · `down`(연결 안 됨) · `retry`(다시 연결 중) · `unknown`(확인 안 됨).
  카메라는 **실제 프레임이 들어올 때만** 연결이다(열기 성공 + 첫 프레임, 미리보기 프레임이 연속 8번 실패하면 끊김). 측정 장비는 [장비 연결]을 누르기 전에는 확인 안 됨, 실패 · 끊김은 연결 안 됨.
- 연결 안 된 장치에 딸린 값은 숨긴다: 스테이지 없음 → 위치 · 릴레이 켜짐/꺼짐 · 펌웨어 · 포트 없음(머리줄 릴레이 칸 "알 수 없음", 측정 화면 히트건 · 쿨러 "—"), 카메라 없음 → 해상도 · 노출 "—".
- 머리줄 상태 필은 **동작 상태만**(대기 · 촬영 중 · 이동 중 · 측정 중 · 일시정지 · 비상정지 · 오류). 연결 문제는 상태 칸과 띠가 맡는다.
- **띠는 목록**(`state.bands[]`): 맨 위 경보 · 원점 회복(비상정지 · 통신 끊김 · 재부팅 · 원점 없음), 그 아래 장치마다 한 줄 - "스테이지 연결 안 됨 · USB · 포트 번호 확인 [다시 연결] [장비 설정 1]" · "카메라 연결 안 됨 · USB · 카메라 번호 확인 [다시 연결] [장비 설정 2]" · "K2110 연결 안 됨 · 주소 · 케이블 확인 [장비 연결] [장비 설정 8]"(K2450 도 같은 줄).
- **장비 설정 단계 상태 셋**: 완료(설정됨 + 장치 정상) · 연결 안 됨(설정은 있으나 장치 없음 · 빨강) · 필요(설정 없음). 카메라 단계는 프레임이 들어올 때만 완료, 단계 3–7 은 필요한 장치(카메라 · 스테이지)가 없으면 연결 안 됨, 8단계는 "릴레이"(스테이지 필요)와 "측정 장비"(K2110 · K2450)를 두 줄로 따로 표시한다.
- **자동 다시 연결**(`App._auto_link`, 5 s 간격 백그라운드 스레드): 스테이지가 없으면 포트를 다시 열어 보고(연결되면 지금의 EEPROM · 재부팅 · 원점 확인 규칙을 그대로 탄다), 카메라가 없으면 다시 연다, 통신 끊김 경보 중 응답이 돌아오면 [연결 확인]처럼 경보를 닫는다. **측정 · 이동 · 촬영 중에는 하지 않는다.** 카메라 열기 실패가 오래 걸려도 이 스레드만 기다린다(화면 · 비상정지는 붙잡히지 않는다 - e2e 49 가 자동 확인 중 비상정지 요청 지연 100 ms 이하를 잰다). 연결되면 띠가 사라지고 소식 한 줄("스테이지 다시 연결됨" · "카메라 다시 연결됨"). 측정 장비는 [장비 연결]을 누를 때만 연결한다.
  시작 연결: 스테이지는 서버가 뜨기 전에 한 번, 카메라는 서버가 뜬 뒤 백그라운드로(`server._startup_link`). 실패해도 경보를 만들지 않고 장치 띠로 보인다.
- **사진 빈 상태**: 프레임이 없으면 깨진 이미지 대신 회색 판에 안내 - 카메라 없음 "카메라 연결 안 됨 · [다시 연결]", 촬영 사진 없음 "촬영 사진 없음 · [촬영]". `<img>` 로드 실패(`onerror`)도 같은 빈 상태로 바뀐다(`Viewer.setEmpty`). 사진이 필요한 작업(촬영 · 빈 판 사진 · 판 영역 적용 · 위치 기록 · 헤드 위치 보정 · 위치 확인)은 카메라가 없으면 비활성 + 사유, 서버도 이동하기 전에 거절한다(`App._need_camera`). 판 영역 꼭짓점 편집기는 사진이 있을 때만 보인다.
- 장비 설정 2 **[카메라 찾기]**: 번호 0–5 를 백그라운드로 열어 보고(열림 · 해상도) 목록에서 [이 번호로] 로 저장한다(`cam_scan`, 시뮬레이션은 가짜 목록).
- **시작을 막는 사유는 한 곳에 한 줄**(`gateOf`): 가장 먼저 해결할 것 + 바로 가기 버튼(예 "스테이지 연결 안 됨 [다시 연결]" · "접촉 높이 필요 [장비 설정 7]" · "측정 장비 연결 안 됨 [장비 연결]"). 카드마다 흩어진 빨간 문구 · 칩은 없다.
- 순회 쪽 단계 목록(접촉 시험 · 수동 측정 · 가열)의 화면 이름은 **순회 단계**(R–T 측정 조건과 구별), 카드 "3 순회".
- 시험용 환경변수: `WAFER2_MISSING=stage,camera,visa`(장치를 뺀 채 시작, dry · fake 에서만 - 실제 포트 · 카메라 · VISA 를 열지 않는다 · `dry` 명령 `missing` 으로 켜고 끈다), `WAFER2_AUTO_LINK_S`(자동 확인 간격, 기본 5).

## 순회 단계 (단계 일곱 가지 + R–T 측정)
샘플 위로 이동(Z 맨 위 확인 뒤 X · Y, 첫 단계 고정) · 접촉 높이로 내리기 · 쿨러 켜기/끄기(샘플 구역 / 전부) · 히트건 n s(시간이 되면 자동으로 끔, `heater.max_on_s` 상한 120 s) · 대기 n s · 확인 대기(사람이 [다음]) · Z 맨 위.
기본 측정 조건(값은 예시, [편집] 에서 바꾼다): **접촉 시험** 이동 → 접촉 높이 → 대기 2 s → Z 맨 위 / **수동 측정** 이동 → 쿨러 켜기(구역) → 접촉 높이 → 확인 대기 → 쿨러 끄기 → Z 맨 위 / **가열** 이동 → 쿨러 켜기(구역) → 접촉 높이 → 대기 30 s → 히트건 20 s → 대기 60 s → 쿨러 끄기 → Z 맨 위.

단계 `R–T 측정` 은 여덟 번째 단계 종류다(기본 측정 조건에는 넣지 않았다 - [편집] 에서 "단계 추가"). 순회에서는 이동 → 접촉 높이 → R–T 측정 → Z 맨 위 순서이고 파일 이름은 `File name + 샘플 번호 + _`.

## R–T 측정 (저항–온도)
**장비**: pyvisa + NI-VISA(`requirements.txt` 의 `pyvisa`). 드라이버가 없으면 "장비 통신 드라이버 없음 · NI-VISA 설치 필요" 로 안내하고 프로그램은 계속 돈다. 장비는 [장비 연결] 을 누를 때만 연다. K2450 은 연결 때 명령 세트가 SCPI 인지 확인한다(아니면 "K2450 명령 세트를 SCPI 로 · 장비 메뉴"). 주소 기본값: K2110 `USB0::0x05E6::0x2110::INSTR`(목록에서 고른다) · K2450 `GPIB0::18::INSTR`.
**흐름**: 쿨러 켬 · 히트건 끔 → 예비 냉각(24.5 °C 미만 + 유지) → K2450 출력 켬 → #repeat 회 [가열(max temp 초과까지) → 냉각(temp to break 미만까지)] → 끝(히트건 끔 · 출력 끔 · 쿨러 끔 · Z 맨 위 - 뒤 둘은 설정). 시작 조건: 장비 두 대 연결 · 열전대 값 정상 · 핀이 접촉 높이(±0.5 mm).
**온도 제어**: 가열 속도 · 냉각 속도(°C/분, 0 = 최대). 속도를 지정하면 목표 온도선을 따라 히트건을 켜고 끈다(띠 0.5 °C · 최소 켜짐/꺼짐 2 s · 연속 켜짐 상한 `heater.max_on_s`). 최대 속도: 가열은 계속 켜고 냉각은 계속 끈다. [최대 속도 재기] 로 실제 °C/분을 먼저 잰다.
**안전**: 비상 차단 온도(기본 max temp + 10 °C) · 온도 값 없음/3 s 갱신 없음 · 단계 시간 한도(30 분) · 비상정지 · 중지 · 오류 · 종료 · 통신 끊김 → 히트건 끔 + K2450 출력 끔. 히트건은 기존 규칙(전용 릴레이 · 끄기 RS 확인 · Hub 독립 감시)을 그대로 쓴다.
**전류 범위 단계 변경**: 기존 프로그램 방식(전류가 범위의 1.05 배를 넘으면 10배 올리고 0.95 배 아래로 떨어지면 내림). "장비 자동"도 고를 수 있다. 전류 한도(`rt.ilim_a`, 기본 0.1 A - 기존 데이터에서 저항이 11 Ω 까지 내려가 0.1 V 에서 약 9 mA 라 10 mA 한도는 닿는다)는 기존 프로그램에 없던 값이다. 고정 범위에서는 한도 ≥ 측정 범위의 10.6 % 라 한도 0.1 A 면 1 A 범위는 쓰지 않고(`instruments.clamp_range`), 범위 넘음이면 한도를 덮는 범위(0.1 A)로 올린다. **단자 기본은 뒷면(REAR, 3축 FORCE/SENSE)** - `*RST` 뒤 `:ROUT:TERM` 을 먼저 설정하고 `:ROUT:TERM?` 로 확인한 다음 나머지를 설정, 출력 켬은 그 뒤. **R–T 그래프 기본 범위**(기존 LabVIEW 화면과 같게): 저항 1E+1–1E+6 Ω(세로 로그), 온도 20–100 °C - 값이 넘으면 넓힌다(`frontend/js/rtutil.js`). **장비 주소**: 목록(pyvisa `list_resources` 그대로 - GPIB · USB 모두)에서 고르거나 직접 입력하고, 연결 방식(GPIB · USB · LAN)을 연결 시험 · 상태에 표시한다(기존 PC 의 K2450 은 `GPIB0::18::INSTR`). **열린 회로 감지**: 전류 크기가 하한(`rt.open_a`, 기본 1 nA) 아래면 [연결 시험]이 "연결 없음 · 핀 접촉 · 앞/뒤 단자 설정 확인" 으로 안내하고, 측정 중 연속 5회면 경고 + control.csv 마지막 열 "열린 회로" 1(측정은 계속).
**파일**: PC `..\data_v2\measure\<시각>_<File name>\` 에 `[File name][Set voltage 소수 2자리]V_[회차].dat` 와 `..._merged.dat`(예 `5-1-ncd(380)_0.10V_0.dat` - **회차 번호는 0 부터**, 화면의 "n회" 는 1 부터. 회차 파일 첫 줄은 정확히 `Temp   Resistance   `+CRLF(Temp 뒤 · Resistance 뒤 공백 3칸, 탭 없음), `_merged.dat` 는 **머리글 없이** 첫 줄부터 숫자. 숫자 줄은 `<온도><탭><저항>`+CRLF, 값은 LabVIEW `%.7E`(가수 7자리 · 지수는 부호 + 0 채우지 않음: `2.3691786E+1` · `1.6565295E+5` · `5.0000000E-1`, `rt.fmt_e`). 마지막 줄도 CRLF. 이어 쓰기) + `control.csv`(시각 · 경과 · 온도 · 목표 온도 · 저항 · 전류 · 한도 걸림 · 범위 · 히트건 · 쿨러 · 단계 - PC 에만) + `rt.json`. NAS 폴더 = 기본 폴더(`rt.file_path`) \ 연도 \ YYYYMMDD(측정 시작 날짜, `rt.date_folder` 기본 켬) \ 묶음 번호(`rt.batch_folder`, 비우면 생략) - 예 `\\VanaM_NAS\VanaM_Measurement\VO2\VO2 data (NAS)\data\2026\20261109\6`(`rt.nas_target`; 날짜 자동을 끄면 기본 폴더 그대로, 묶음이 있으면 그 아래). 화면에 최종 경로를 한 줄로 미리 보인다. PC 쪽 저장 규칙은 그대로. NAS 에는 같은 `.dat` 만 별도 스레드로 복사한다 - 못 쓰면 경고만 하고 측정은 계속, 끝난 뒤 다시 복사, [NAS 로 다시 복사]. NAS 에 같은 이름이 있으면 덮어쓰지 않고 `_1`.

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
| `rt.k2110_addr` | USB0::0x05E6::0x2110::INSTR |  | K2110 주소 |
| `rt.k2450_addr` | GPIB0::18::INSTR |  | K2450 주소 |
| `rt.precool_c` | 24.5 | °C | 예비 냉각 목표 |
| `rt.hold_s` | 0 | s | 도달 뒤 유지 |
| `rt.max_temp` | 85 | °C | 최고 온도 |
| `rt.break_temp` | 25 | °C | 냉각 끝 온도 |
| `rt.repeat` | 1 |  | 반복 횟수 (#repeat) |
| `rt.heat_rate` · `rt.cool_rate` | 0 · 0 | °C/분 | 가열 · 냉각 속도(0 = 최대) |
| `rt.band_c` | 0.5 | °C | 띠 |
| `rt.min_on_s` · `rt.min_off_s` | 2 · 2 | s | 최소 켜짐 · 꺼짐 |
| `rt.emergency_c` | (비움 = max+10) | °C | 비상 차단 온도 |
| `rt.phase_limit_s` | 1800 | s | 단계 시간 한도 |
| `rt.min_interval_s` | 0 | s | 최소 기록 간격 |
| `rt.stale_s` | 3 | s | 온도 갱신 한도 |
| `rt.volt` | 0.1 | V | 전압 (Set voltage) |
| `rt.ilim_a` | 0.1 | A | 전류 한도 |
| `rt.open_a` | 1e-9 | A | 열린 회로 전류 하한 |
| `rt.four_wire` · `rt.terminal` | True · REAR |  | 4단자 (4P) · 단자 |
| `rt.filter_n` · `rt.nplc` | 10 · 1 |  | 필터 개수 · 적분 |
| `rt.range_mode` · `rt.start_range` | step · 0.001 | A | 범위 방식 · 시작 범위 |
| `rt.tc_type` | K |  | 열전대 종류(기준점 내부) |
| `rt.cooler_target` · `rt.cooler_custom` | sample ·  |  | 쿨러(샘플 구역 · 전부 · 직접) |
| `rt.file_path` | \\\\VanaM_NAS\\VanaM_Measurement\\VO2\\VO2 data (NAS)\\data |  | 기본 폴더 (File path) |
| `rt.date_folder` | True |  | 날짜 폴더 자동 (연도\\YYYYMMDD) |
| `rt.batch_folder` | (빈 값) |  | 묶음 폴더 |
| `rt.file_name` | 1-1_ |  | 파일 이름 (File name) |
| `rt.cooler_off_end` · `rt.z_top_end` | True · True |  | 끝나면 쿨러 끔 · Z 맨 위 |
| `calib.outlier_n` | 4.0 |  | 이상점 배수 |
| `calib.area_size_mm` | [154.0, 154.0] |  | 판 영역 실제 크기 |
| `sample_add.snap` | True |  | 클릭 스냅 |
| `file_log` | True |  | 파일 로그 |

## 메시지 계약
- WebSocket `/ws`: 서버 → 화면 `{"type":"state","state":{...},"log":[...]}`(변할 때마다, 최소 1초) · `{"type":"log","e":{...}}` · `{"type":"ack","id","ok","msg"|"res"}`
- 화면 → 서버 `{"id":n,"cmd":"이름","args":{...}}`. **비상정지는 줄 세우지 않고** `{"cmd":"estop"}`(WS) 와 `POST /estop`(HTTP) 둘 다. 명령은 수신 순간의 비상정지 세대(gen)를 잡아 잠금 뒤 비교한다.
- HTTP: `/preview.jpg` `/shot.jpg` `/ref.jpg` `/snap.jpg` `/refhist/<id>.jpg` `/run/<측정>/<파일>` `/serial.json`(시리얼 탭) `/rt.json?since=n&id=`(R–T 그래프 점) `/rt_data/<측정 폴더>`(결과 탭 그래프 보기) `/schema` `/health`
- R–T 명령: `inst_list` · `inst_connect` · `inst_disconnect` · `inst_test` · `rt_start` · `rt_stop` · `rt_speed` · `rt_defaults` · `rt_nas_retry`(+ `contact_try {free:true}` = 이 위치에서 접촉 높이로 내리기). `state.rt{inst{k2110,k2450,msg,test}, run{state,status,phase,cycle,repeat,T,R,I,target,heater,cooler,warns,nas,speed}, block, block_speed, results[]}`
- 명령: `alarm_clear` · `apply_position` · `area_set` · `calib_apply` · `calib_click` · `calib_delete` · `calib_record` · `cam_reconnect` · `cam_scan` · `capture` · `contact_clear` · `contact_save` · `contact_try` · `dry` · `exclude_set` · `focus_refind` · `goto_named` · `goto_sample` · `head_apply` · `head_calib` · `heater_off_retry` · `jog` · `link_check` · `measure_answer` · `measure_pause` · `measure_redo_sample` · `measure_resume` · `measure_start` · `measure_stop` · `move_xy` · `open_folder` · `origin`(축 x|y|z) · `reconnect` · `ref_capture` · `ref_restore` · `relay` · `relay_test` · `results_open` · `sample_add` · `sample_confirm` · `sample_delete` · `sample_exclude` · `sample_here` · `sample_move` · `sample_select` · `settings_import_old` · `settings_save` · `shutdown_check` · `snap` · `undo_edit` · `verify_position` · `z_top`
- `state` 주요 키: `phase` · `alarm` · `band{text,next,err,actions[]}` · `dev{stage,camera,k2110,k2450}{state,text,why}` · `bands[]{id,text,next,err,actions[]}` · `cam_scan{running,items[]}` · `stage{x,y,z,locked,z_ok,xy_ok,done,fw_homed,recovery[]}`(스테이지 없으면 null) · `eeprom` · `port` · `fw` · `contact{z,approach}` · `camera` · `calib{status,text,points,area,head}` · `ref` · `shot` · `samples[]` · `sel` · `run{state,steps,results,cur_sample,waiting}` · `relays` · `settings` · `conditions` · `start_block`

## R–T 측정 결과 폴더
`..\data_v2\measure\<YYYYMMDD_HHMMSS>_<File name>\` — 회차 `.dat` · `_merged.dat` · `control.csv` · `rt.json`(조건 · 단계 · 경고 · 파일 · NAS 상태). 샘플 순회 중의 R–T 는 `runs\<측정>\rt_<샘플 번호>\` 에 같은 형식으로.

## 측정 결과 폴더
`..\data_v2\runs\<YYYYMMDD_HHMMSS>_<측정 조건>\` — `run.json`(측정 조건 · 설정 사본 · 보정 식별자 · 촬영 폴더 · 시작/끝 · 결과) · `samples.csv`(번호 · X · Y · 쿨러 · 접촉 Z · 시작 · 끝 · 결과 · 사유) · `plate_fit.jpg`. CSV 는 UTF-8 BOM.

## 검증
`python tools/unit_tests.py` · `python tools/unit_rt.py`(R–T 단위 시험, 목표 온도선 추종 오차 보고) · `python tools/e2e.py 31 …`(R–T e2e 31~47) · `python tools/e2e.py 48 49 50 51`(장치 연결 상태 · 자동 다시 연결) · `python tools/shots_conn.py`(연결 상태별 화면 점검 + 스크린샷 → `..\shots_v2\conn\`) · `python tools/shots_rt.py`(R–T 화면 흐름 · 스크린샷) · `E2E_MODE=fake python tools/e2e.py`(fake-serial) · `python tools/stress_fake.py 300`(동시성 부하) · `python tools/clicks.py`(클릭 수) · `python tools/fixed_regress.py [--loo]` · `python tools/align_measure.py` · `python tools/e2e.py [이름일부]` · `python tools/shots.py OUT` · `python tools/exit_test.py` · `python tools/flows.py`(화면 흐름: 첫 실행 · 비상정지 회복 · 막다른 상태 점검, playwright) · `python tools/perf4080.py`(4080×3060 성능).
