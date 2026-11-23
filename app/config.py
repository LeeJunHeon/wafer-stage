"""config.py - settings.json 읽기 · 쓰기 · 기본값 · 검증.

설정은 ITEMS 표 한 곳에 모은다(이름 · 기본값 · 한국어 이름 · 단위 · 범위 · 설명). 코드 다른 곳에 숫자를 박지 않는다.
settings.json 은 PC 마다 다른 로컬 파일이라 git 에 넣지 않는다. 없으면 기본값으로 시작한다.
**저장은 기본값과 다른 값만 쓴다**(읽을 때는 기본값 위에 덮는다) - 코드 기본값을 바꾸면 바꾼 적 없는 PC 에도 반영된다.
장비가 한 대라 장비에 딸린 값(쿨러 배치 · 릴레이 배선 · 초점 · 장비 방향)은 코드 기본값으로 둔다(GitHub 로 관리). PC 값(PC_KEYS - 포트 · 주소 · 폴더 ·
카메라 번호 · 촬영 위치 · 접촉 높이)만 settings.json 에 남는다고 보면 된다. 기본값을 바꿀 때는 DEFAULT_HISTORY 에 예전 기본값을 더한다
(저장된 값이 예전 기본값과 같으면 "바꾼 적 없음" 으로 보고 지운다 - 새 기본값을 따른다).
기본값 중 "실장비 확인 필요" 표시는 측정 PC 에서 확인하기 전의 값이다.
"""

import copy
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "settings.json"


@dataclass(frozen=True)
class Item:
    key: str
    default: Any
    kind: str                       # str | int | float | bool | grid(3×3 릴레이 번호) | ints(릴레이 번호 목록) | xy(비어 있을 수 있는 [x, y] mm) |
                                    # optfloat(비어 있을 수 있는 숫자) | choice | list(글자 목록)
    label: str                      # 화면에 보일 한국어 이름(뜻이 바로 보이게)
    unit: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    group: str = ""                 # 설정 값 모두 보기의 묶음
    verify: bool = False            # 실장비 확인 필요(측정 PC 에서 확인하기 전의 값)
    choices: tuple = ()             # kind = choice 일 때 고를 수 있는 값 ((값, 이름), …)
    desc: str = ""                  # 설명(뜻 · 언제 바꾸는지) 1~2줄. 화면의 말풍선에 보인다. 단위 · 기본값 · 범위는 hint() 가 덧붙인다
    folder: bool = False            # 폴더 경로(화면에 [찾아보기] 버튼을 붙인다)


# 스테이지의 네 쪽(장비 앞쪽 · 오른쪽 설정): 먼 쪽 = 좌표가 큰 쪽 · 모터 쪽 = 0 쪽
SIDES = (("x_far", "X 먼 쪽"), ("x_motor", "X 모터 쪽"), ("y_far", "Y 먼 쪽"), ("y_motor", "Y 모터 쪽"))
SIDE_NAME = dict(SIDES)


def _g(group, *items):
    return [Item(**dict(i.__dict__, group=group)) for i in items]


ITEMS = (
    _g("연결",
       Item("stage_port", "COM5", "str", "스테이지 포트",                             # 측정 PC 는 COM5. 자동으로 찾지 않는다
            desc="스테이지 보드의 시리얼 포트\n초기 설정 1 에서 선택 · 연결되면 자동 저장"),
       Item("k2110_addr", "USB0::0x05E6::0x2110::8017862::INSTR", "str", "온도계 주소",
            desc="온도계 K2110 의 VISA 주소\n초기 설정 1 에서 선택 · 연결되면 자동 저장"),
       Item("k2450_addr", "GPIB0::18::INSTR", "str", "저항계 주소",
            desc="저항계 K2450 의 VISA 주소\n초기 설정 1 에서 선택 · 연결되면 자동 저장"),
       Item("data_dir", "../data_v3", "str", "데이터 폴더", folder=True,
            desc="측정 기록 · 로그 · 초기 설정 값을 저장하는 폴더\n상대 경로는 프로그램 폴더 기준 · 변경 후 재시작 필요"),
       Item("auto_connect", True, "bool", "시작 시 자동 연결",                          # 저장된 포트 · 주소 하나씩만 연다(찾아다니지 않는다)
            desc="시작할 때 저장된 포트 · 주소로 연결\n장비 없이 화면만 볼 때 끔"),
       Item("blocked_ports", ["COM4"], "list", "사용 안 할 포트",                       # COM4 = LabVIEW 용 Uno. 열면 그 장비가 재시작한다
            desc="스테이지로 열지 않는 포트 · 다른 장비가 쓰는 포트\n선택 상자에서 고를 수 없고 연결도 거절"))
    + _g("카메라",                                                                        # 실장비 기본값(v2 운용값): 장치 1 · 4080×3060 · MJPG · 노출 −5 · 화이트밸런스 5000 K · 초점 고정
         Item("plate_pick_mag", 4.0, "float", "돋보기 배율", "배", 1.5, 16,
              desc="크게 선택 · 크게 보기 창의 돋보기 배율 · 지금 화면 배율의 몇 배\n꼭짓점을 더 정확히 찍을 때 늘림"),
         Item("cam_settle_wait_s", 1.0, "float", "이동 뒤 사진 대기", "s", 0, 10, verify=True,
              desc="이동 · 초점 변경 뒤 사진을 쓰기 전에 먼저 기다리는 시간\n카메라 프레임이 실제보다 늦게 옴(4080×3060 MJPG)"),
         Item("cam_still_diff", 1.5, "float", "화면 멈춤 기준", "", 0.1, 50,
              desc="연속 두 장(줄인 회색 사진)의 평균 밝기 차이가 이 값 이하면 화면이 멈춘 것으로 봄"),
         Item("cam_still_timeout_s", 8.0, "float", "화면 멈춤 한도", "s", 1, 60,
              desc="이 시간 안에 화면이 멈추지 않으면 거절 \"카메라 화면 안정 안 됨\""),
         Item("camera_index", 1, "int", "카메라 번호", "번", 0, 9,                     # 0 번은 노트북 내장 카메라
              desc="카메라 장치 번호 · 0 은 보통 내장 카메라\n초기 설정 1 에서 선택 · 연결되면 자동 저장"),
         Item("camera_width", 4080, "int", "카메라 가로", "px", 160, 10000,
              desc="카메라에 요청하는 가로 해상도\n카메라 교체 시 변경 · 재연결과 판 영역 재설정 필요"),
         Item("camera_height", 3060, "int", "카메라 세로", "px", 120, 10000,
              desc="카메라에 요청하는 세로 해상도\n카메라 교체 시 변경 · 재연결과 판 영역 재설정 필요"),
         Item("camera_fourcc", "MJPG", "str", "영상 포맷",
              desc="카메라에 요청하는 영상 포맷\n카메라가 MJPG 를 지원하지 않을 때만 변경"),
         Item("camera_exposure_fixed", True, "bool", "노출 고정",
              desc="자동 노출 끔 · 사진 밝기를 일정하게 유지\n끄면 밝기가 변해 샘플 검출이 흔들릴 수 있음"),
         Item("camera_exposure", -5, "int", "노출 값", "", -13, 0,
              desc="고정 노출 값 · 작을수록 어두움\n사진이 너무 밝거나 어두울 때 변경"),
         Item("camera_wb_fixed", True, "bool", "화이트밸런스 고정",
              desc="화이트밸런스 고정 · 사진 색을 일정하게 유지\n끄면 카메라 기본값 사용"),
         Item("camera_wb_k", 5000, "int", "화이트밸런스 값", "K", 2000, 10000,
              desc="고정 화이트밸런스 색온도\n조명을 바꿨을 때 변경"),
         Item("camera_focus_fixed", True, "bool", "초점 고정",
              desc="자동 초점 끔 · 촬영마다 초점이 변하지 않게 함"),
         Item("camera_focus_value", 240, "int", "초점 값", "", -1, 4095,                # 장비 값(측정 PC 에서 초점 찾기로 정한 값)
              desc="고정 초점 값 · −1 은 미설정(켤 때마다 렌즈가 그 순간 자리에 굳음)\n초기 설정 3 [초점 찾기] 후 [적용] 으로 저장"),
         # 초점 찾기(초기 설정 3): 판 영역 안의 선명도(라플라시안 분산)가 가장 큰 초점 값을 찾는다
         Item("focus_scan_min", 0, "int", "초점 찾기 시작 값", "", 0, 4095,
              desc="초점 찾기에서 훑는 가장 작은 초점 값"),
         Item("focus_scan_max", 1023, "int", "초점 찾기 끝 값", "", 1, 4095,
              desc="초점 찾기에서 훑는 가장 큰 초점 값 · 카메라가 받지 않는 값은 건너뜀"),
         Item("focus_coarse_step", 64, "int", "초점 큰 간격", "", 2, 1024,
              desc="처음 넓게 훑을 때의 간격"),
         Item("focus_fine_step", 8, "int", "초점 작은 간격", "", 1, 256,
              desc="가장 선명한 곳 둘레를 다시 훑을 때의 간격 · 큰 간격보다 작게"),
         Item("focus_settle_s", 0.8, "float", "초점 이동 대기", "s", 0, 10, verify=True,
              desc="초점 값을 넣은 뒤 렌즈가 멈출 때까지 대기\n선명도 그래프가 들쭉날쭉하면 늘림"),
         Item("focus_nudge", 4, "int", "초점 미세 조정", "", 1, 256,
              desc="초점 [−] [+] 한 번에 바꾸는 값"),
         Item("focus_roi_inset", 0.1, "float", "초점 영역 줄임", "", 0, 0.4,
              desc="판 영역을 안쪽으로 줄이는 비율 · 판 가장자리 · 바닥 제외"),
         Item("focus_marker_pad_mm", 40.0, "float", "초점 마커 제외 반경", "mm", 0, 200,
              desc="선명도에서 빼는 헤드 마커 둘레 반경 · 판 배율로 픽셀 환산"),
         Item("focus_peak_min", 0.15, "float", "초점 봉우리 기준", "", 0, 1,
              desc="가장 선명한 값이 양 끝 값보다 이 비율 넘게 커야 뚜렷한 봉우리\n작으면 주의 문구"),
         Item("preview_width", 1280, "int", "미리보기 가로", "px", 160, 4096,
              desc="실시간 화면으로 보내는 사진의 가로 크기\n화면이 느릴 때 줄임 · 카메라 재연결 후 적용"),
         Item("photo_head_xy", None, "xy", "촬영 위치", "mm",                           # 초기 설정 3 에서 정한다
              desc="촬영할 때 헤드를 두는 스테이지 X · Y\n초기 설정 3 [현재 위치로 설정] 으로 저장"))
    + _g("이동",                                                                          # 0 = 모터 쪽 끝. 속도는 펌웨어 기본값과 같다(다르면 연결할 때 보낸다)
         Item("x_max_mm", 247.6, "float", "X 가동 범위", "mm", 1, 400,
              desc="X 축이 갈 수 있는 거리 · 0 은 모터 쪽 끝\n원점이 있으면 범위 밖 이동 거절 · 기구 변경 시에만 변경"),
         Item("y_max_mm", 247.8, "float", "Y 가동 범위", "mm", 1, 400,
              desc="Y 축이 갈 수 있는 거리 · 0 은 모터 쪽 끝\n원점이 있으면 범위 밖 이동 거절 · 기구 변경 시에만 변경"),
         Item("z_max_mm", 45.0, "float", "Z 가동 범위", "mm", 1, 100,
              desc="Z 축이 갈 수 있는 거리 · 0 은 맨 위\n원점이 있으면 범위 밖 이동 거절 · 기구 변경 시에만 변경"),
         Item("confirm_auto_move", True, "bool", "자동 이동 전 확인",
              desc="이동이 주목적이 아닌 버튼이 모터를 움직일 때 먼저 확인 창\n끄면 확인 없이 바로 실행 · 조그 · 좌표 이동은 묻지 않음"),
         Item("move_speed_pps", 6000, "int", "이동 속도", "펄스/s", 100, 20000,
              desc="이동 최고 속도 · 160 펄스 = 1 mm\n탈조 · 소음이 있으면 낮춤 · 스테이지 재연결 후 적용"),
         Item("move_accel_pps2", 40000, "int", "가속", "펄스/s²", 1000, 200000,
              desc="이동 가속도\n탈조가 있으면 낮춤 · 스테이지 재연결 후 적용"),
         Item("move_start_pps", 300, "int", "시작 속도", "펄스/s", 50, 5000,
              desc="이동을 시작하는 속도\n보통 변경 안 함 · 스테이지 재연결 후 적용"),
         Item("slow_speed_pps", 1200, "int", "핀 하강 속도", "펄스/s", 100, 6000, verify=True,
              desc="접촉 탐색에서 핀을 내리는 속도\n핀이 샘플을 세게 누르면 낮춤"),
         Item("approach_mm", 2.0, "float", "저속 하강 거리", "mm", 0, 20,
              desc="접촉 탐색 시작 높이보다 이만큼 위부터 핀 하강 속도로 하강"),
         Item("watchdog_ms", 2000, "int", "통신 감시 시간", "ms", 500, 60000,
              desc="프로그램 신호가 이 시간 동안 없으면 보드가 릴레이 차단\n보통 변경 안 함 · 스테이지 재연결 후 적용"),
         # 수동 Z 하강 한도: 실제로 몇 mm 까지 내려도 되는지 아직 모른다 → 기능은 있고 기본은 끔. 측정의 접촉 단계 · 접촉 탐색은 자기 한도를 따로 쓴다
         Item("z_floor_mode", "off", "choice", "Z 하강 한도", choices=(("off", "끔"), ("contact", "접촉 높이 기준"), ("fixed", "고정 값")),
              desc="수동 이동이 한도 아래로 가는 명령을 거절\n실장비에서 값을 확인한 뒤 켬 · 접촉 높이를 정할 때는 끔"),
         Item("z_floor_margin_mm", 0.3, "float", "Z 한도 여유", "mm", 0, 10,
              desc="접촉 높이 기준일 때 한도 = 접촉 높이 − 이 값\n수동 이동으로 핀이 샘플에 닿지 않게 함"),
         Item("z_floor_mm", None, "optfloat", "Z 한도 고정 값", "mm", 0, 100, verify=True,
              desc="고정 값일 때 수동으로 내릴 수 있는 가장 낮은 Z\n실장비에서 확인한 높이 입력"))
    + _g("릴레이",                                                                        # 릴레이 역할. 그 밖의 릴레이는 미배정(켜는 길이 없다)
         Item("heater_fan_relay", 1, "int", "송풍 릴레이", "번", 1, 10,
              desc="히트건 송풍(바람) 릴레이 번호 · 릴레이 모두 끈 뒤 변경 가능"),
         Item("heater_element_relay", 10, "int", "열선 릴레이", "번", 1, 10,
              desc="히트건 열선 릴레이 번호 · 켤 때 열선 먼저 · 릴레이 모두 끈 뒤 변경 가능"),
         Item("cooler_relays", [2, 3, 4], "ints", "쿨러 릴레이",
              desc="쿨러 묶음 릴레이 번호 목록 · 송풍 · 열선과 겹치면 안 됨"),
         # 쿨러 3x3 자리 배치(판 mm 기준 칸 → 쿨러 릴레이 번호 · 0 행 = A–B 변 쪽 · 0 열 = A–D 변 쪽 = 사진 기준). 같은 릴레이를 여러 칸에 써도 된다.
         # 빈 칸(null)도 된다 - 쿨러 "샘플 아래 구역" 이면 빈 칸의 샘플은 측정 시작을 거절한다(run.gate). 초기 설정 7 에서 정한다
         Item("cooler_grid", [[3, 4, 4], [2, 2, 3], [2, 3, 4]], "grid", "쿨러 배치",          # 장비 값(측정 PC 초기 설정 7)
              desc="판 3×3 칸마다 쿨러 릴레이 번호 · 사진 기준(왼쪽 위 = A) · 빈 칸 가능"),
         # 장비 앞(사람이 서는 쪽)에서 본 방향: 초기 설정 7 의 칸을 장비 앞 기준으로 돌려 그릴 때만 쓴다(cooler_grid 의 뜻은 그대로)
         Item("machine_front_side", "x_far", "choice", "장비 앞쪽", choices=SIDES,
              desc="장비 앞(사람이 서는 쪽)이 스테이지의 어느 쪽인지 · 초기 설정 7 의 칸 방향"),
         Item("machine_right_side", "y_far", "choice", "장비 오른쪽", choices=SIDES,
              desc="장비 앞에서 볼 때 오른쪽이 스테이지의 어느 쪽인지 · 초기 설정 7 의 칸 방향"),
         Item("view_rot", "180", "choice", "사진 화면 회전", choices=(("0", "0°"), ("90", "90°"), ("180", "180°"), ("270", "270°")),
              desc="사진을 문 기준 화면(아래 = 문)으로 돌리는 각도 · 시계 방향\n저장 값은 그대로 · 화면에 그릴 때만 돌림"),
         Item("view_flip", False, "bool", "사진 좌우 뒤집기",
              desc="회전 전에 사진을 좌우로 뒤집음 · 카메라를 거울 방향으로 달았을 때만"))
    + _g("히트건 안전",
         Item("heater_max_c", 110.0, "float", "히트건 차단 온도", "°C", 30, 300,
              desc="이 온도 이상이면 열선을 켜지 않고 켜져 있으면 끔\n가열 끝 온도보다 높게 설정"),
         Item("heater_max_on_s", 180.0, "float", "열선 연속 최대 시간", "s", 1, 3600,
              desc="열선을 끊김 없이 켜 두는 최대 시간 · 지나면 열선 끔\n가열 단계 전체 시간은 한 단계 시간 한도가 봄"),
         Item("temp_stale_s", 3.0, "float", "온도 못 읽음 허용 시간", "s", 0.5, 60,
              desc="측정 중 온도를 이 시간 넘게 못 읽으면 열선 끔\n수동으로 켠 히트건에는 적용 안 함"),
         Item("heater_start_gap_s", 1.0, "float", "열선 후 송풍 대기", "s", 0, 10, verify=True,
              desc="히트건을 켤 때 열선을 켜고 송풍을 켜기까지 대기\n히트건이 켜기 순서 오류를 내면 늘림"),
         Item("heater_fan_after_s", 30.0, "float", "열선 끈 뒤 송풍", "s", 0, 600, verify=True,
              desc="히트건을 끌 때 열선을 끄고 송풍을 더 켜 두는 시간 · 열선 식힘\n열선이 뜨거운 채 멈추면 늘림"))
    + _g("가열 제어",                                                                      # 열선 출력 %(펌웨어 V12 hp): 목표선 기준 PI + 앞먹임(heater.PowerControl) · 계수는 열 모델에서 자동
         Item("run_heat_mode", "ramp", "choice", "가열 방식", choices=(("ramp", "가열 속도 제어"), ("full", "계속 가열")),
              desc="가열 속도 제어는 목표선을 따라 열선 출력 % 조절\n계속 가열은 가열 끝 온도까지 최대 출력"),
         Item("run_rate_by", "step", "choice", "속도 정하는 방법", choices=(("step", "점 간격"), ("time", "걸리는 시간")),
              desc="점 간격 = 목표 점 간격 ÷ 읽기 간격으로 속도를 정함\n걸리는 시간 = 가열 시간 · 냉각 시간으로 속도를 정함(읽기 간격과 무관)"),
         Item("run_heat_time_s", 102.0, "float", "가열 시간", "s", 5, 36000,
              desc="시작 온도에서 가열 끝 온도까지 걸리는 시간\n속도 정하는 방법이 걸리는 시간일 때만 사용"),
         Item("run_cool_time_s", 102.0, "float", "냉각 시간", "s", 5, 36000,
              desc="가열 끝 온도에서 냉각 끝 온도까지 걸리는 시간\n속도 정하는 방법이 걸리는 시간일 때만 사용"),
         Item("run_point_step_c", 0.27, "float", "목표 점 간격", "°C", 0.02, 5,
              desc="한 점마다 오를 · 내릴 온도 · 가열 · 냉각 속도 = 점 간격 ÷ 읽기 간격 × 60\n기존 LabVIEW 데이터 약 0.27 °C"),
         Item("cool_rate_separate", False, "bool", "냉각 속도 따로",
              desc="켜면 냉각만 아래 냉각 속도 사용 · 끄면 가열과 같은 점 간격"),
         Item("run_cool_rate_c_min", 3.0, "float", "냉각 속도", "°C/분", 0.1, 60,
              desc="냉각 속도 따로 켰을 때의 냉각 목표선 기울기"),
         Item("run_heat_hold_s", 0.0, "float", "가열 끝 유지 시간", "s", 0, 3600, verify=True,
              desc="가열 끝 온도에 닿은 뒤 그 온도로 유지하는 시간 · 가열 속도 제어만\n0 이면 닿는 즉시 냉각"),
         Item("run_cool_mode", "ramp", "choice", "냉각 방식", choices=(("ramp", "냉각 속도 제어"), ("free", "자연 냉각")),
              desc="냉각 속도 제어는 송풍을 켜 둔 채 열선 출력 % 로 목표선을 따라 내려감\n자연 냉각은 열선을 끄고 그대로 식힘"),
         Item("heater_lead_c", 3.0, "float", "목표선 기다림 폭", "°C", 0.5, 50, verify=True,
              desc="걸러진 온도가 목표선보다 이만큼 넘게 계속 뒤처지면 목표선이 기다림"),
         Item("heater_lead_filter_s", 3.0, "float", "기다림 평균 창", "s", 0, 60,
              desc="목표선 기다림 판단에 쓰는 온도 평균 시간 · 순간 튐 무시"),
         Item("heater_lead_hold_s", 5.0, "float", "기다림 지속 시간", "s", 0, 120,
              desc="이 시간 넘게 계속 뒤처질 때만 목표선이 기다림"),
         Item("heater_max_pct", 100, "int", "열선 최대 출력", "%", 1, 100,
              desc="제어기가 쓰는 열선 출력 상한\n넘침이 크면 낮춤"),
         Item("heater_full_pct", 90, "int", "연속 판정 출력", "%", 1, 100,
              desc="열선 연속 최대 시간은 출력이 이 값 이상으로 이어진 시간\n출력 % 로 오래 조절하는 동안은 세지 않음"),
         Item("heater_out_mode", "spread", "choice", "열선 출력 방식", choices=(("window", "몰아 켜기"), ("spread", "고르게 나눠 켜기")),
              desc="고르게 = 칸마다 나눠 켜서 출렁임이 작음 · 몰아 켜기 = 주기 앞쪽에 몰아 켬\n[히트건 맞추기] 가 정함 · 펌웨어 V18"),
         Item("heater_pwm_period_ms", 5000, "int", "몰아 켜기 주기", "ms", 200, 5000,
              desc="몰아 켜기의 한 주기 · 주기 앞쪽 (주기 × %) 동안만 켬\n5 s 주기는 30 % 에서 ±3 °C 출렁였다"),
         Item("heater_slot_ms", 300, "int", "고르게 켜기 칸", "ms", 10, 1000,
              desc="고르게 나눠 켜기의 칸 길이\n펌웨어가 전원 한 주기(16.7 ms)의 정수배로 맞춤 · 17 · 33 · 50 · 100 · 300 …"),
         Item("heater_timing", "sync", "choice", "열선 박자", choices=(("sync", "전원 맞춤"), ("detune", "어긋남"), ("random", "무작위")),
              desc="열선을 켜고 끄는 틱의 박자 · 펌웨어 V18\n전원 맞춤 = 전원 반주기 · 어긋남 = 1.6 % 길게 · 무작위 = 0.5~1.5 배\n[박자 시험] 으로 비교 · 전원 맞춤 기준이 있으면 쓰지 않음(전원 맞춤)"),
         Item("heater_sync", "none", "choice", "전원 맞춤 기준", choices=(("none", "없음"), ("zc", "영점 모듈"), ("hum", "전선")),
              desc="열선을 켜고 끄는 박자를 전원 파형에 붙잡는 기준\n영점 모듈 = D19 · 전선 = A0 · 펌웨어 V17 · V18"),
         Item("heater_phase_off", 0, "int", "전원 위상", "‰", 0, 999,
              desc="기준 신호의 영점에서 켜는 순간까지의 위치\n[위상 찾기] 가 정함"),
         Item("heater_trim_ppm", 0, "int", "타이머 보정", "ppm", -3000, 3000,
              desc="전원 맞춤 기준이 없음일 때 틱 길이를 늘리는(+) 양\n[박자 보정] 이 정함"),
         Item("heater_rotate_pm", 0, "int", "켜는 순간 돌리기", "‰", 0, 999,
              desc="열선을 켤 때마다 켜는 순간을 전원 반주기의 이만큼씩 뒤로 돌림 · 0 = 끔 · 382 권장\n전원 맞춤 기준 없음 · 열선 박자 전원 맞춤일 때만 · 펌웨어 V18"),
         Item("heater_boot_hold_s", 5.0, "float", "스테이션 켠 뒤 100 % 유지", "s", 0, 60,
              desc="송풍(스테이션)을 새로 켠 뒤 이 시간 동안 열선 100 % · 그동안 끊으면 H-E\n그 사이 출력 % 요청은 끝날 때 적용"),
         Item("heat_model_k", None, "optfloat", "열 모델 이득", "°C/%", 0.01, 20, verify=True,
              desc="열선 출력 1 % 당 올라가는 온도 · [히트건 맞추기] 로 저장\n출력별 온도 표가 있으면 이득은 표에서 읽음"),
         Item("heat_model_tau_s", None, "optfloat", "열 모델 시간 상수", "s", 1, 3600, verify=True,
              desc="출력을 바꾼 뒤 온도가 63 % 따라가는 시간 · [히트건 맞추기] 로 저장"),
         Item("heat_model_theta_s", None, "optfloat", "열 모델 지연", "s", 0, 300, verify=True,
              desc="출력을 바꾼 뒤 온도가 움직이기 시작할 때까지 · [히트건 맞추기] 로 저장"),
         Item("heat_curve", "", "str", "출력별 온도 표",
              desc="\"출력%:온도\" 를 빈칸으로 이은 글 · 예 0:26.1 2.0:35.4 4.0:47.8\n[히트건 맞추기] 가 저장 · 제어의 유지 출력 · 이득"),
         Item("heater_tc_s", 5.0, "float", "제어 응답 시간", "s", 1, 60,
              desc="클수록 느슨함(흔들림에 덜 반응)\n제어 평균 창의 절반 + 열 모델 지연보다 작으면 그 값 사용"),
         Item("heater_pi_filter_s", 8.0, "float", "제어 평균 창", "s", 0, 60,
              desc="온도 흔들림 한 주기(지금 장비 약 8 s)\n이 시간 평균 오차로 출력을 정함 · 흔들림에 출력이 따라 출렁이지 않게"),
         Item("heater_jump_c", 4.0, "float", "온도 급변 기준", "°C", 1, 50,
              desc="온도가 목표선에서 이만큼 벗어나면(출력에 여유가 있었는데도) \"온도 급변\"으로 표시\n그때 배율을 바로 한 단 바꿈"),
         Item("heater_turn_s", 15.0, "float", "방향 전환 시간", "s", 0, 60,
              desc="가열을 시작할 때 · 가열에서 냉각으로 넘어갈 때 목표선 기울기를 이 시간에 걸쳐 바꿈\n출력을 한 번에 바꾸면 온도가 출렁임 · 0 = 바로 바꿈"),
         Item("reversal_c", 0.3, "float", "역전 기준", "°C", 0.01, 10,
              desc="가열 중 내려감 · 냉각 중 올라감이 이 값 넘으면 역전 1회로 셈"),
         Item("run_fan_during_cool", True, "bool", "냉각 중 송풍",
              desc="자연 냉각 동안 송풍을 켜 둠 · 냉각 속도 제어는 항상 켬\n끄면 열선 끈 뒤 송풍 시간이 지나면 끔"))
    + _g("쿨러",
         Item("run_cooler_mode", "zone", "choice", "쿨러", choices=(("zone", "샘플 아래 구역"), ("all", "전체"), ("none", "사용 안 함")),
              desc="측정 때 켜는 쿨러\n샘플 아래 구역은 초기 설정 7 의 배치 사용"),
         Item("run_cooler_during_heat", True, "bool", "가열 중 쿨러 켬",
              desc="가열 중에도 쿨러를 켠 채로 둠\n끄면 가열하는 동안 쿨러 끔"))
    + _g("온도 시험",                                                                      # 수동 제어 탭 [온도 시험]: 샘플 · 저항계 · 모터 없이 히트건 · 쿨러의 온도 거동만 잰다
         Item("temp_test_coolers", [2, 3, 4], "ints", "온도 시험 쿨러", min=0, max=10,      # 기본 = 쿨러 릴레이 전부
              desc="온도 시험 동안 켜 둘 쿨러 릴레이 목록 · 비우면 [] 쿨러 없이\n쿨러 릴레이 안의 번호만 가능"),
         Item("temp_test_interval_s", 0.5, "float", "온도 시험 읽기 간격", "s", 0.05, 10,
              desc="온도 시험에서 온도를 읽고 기록하는 간격\n온도 읽기 시간 이상 · 온도 못 읽음 허용 시간보다 충분히 짧게"),
         Item("temp_test_mark_c", 5.0, "float", "도달 표 온도 간격", "°C", 0.5, 50,
              desc="도달 표에 걸린 시간을 적는 온도 간격 · 5 면 35 · 40 · 45 °C …\n촘촘히 볼 때 줄임"),
         Item("temp_tune_c", 55.0, "float", "맞추기 비교 온도", "°C", 30, 100,
              desc="[히트건 맞추기] 후보 칸마다 이 온도에 맞춘 뒤 흔들림을 잼"),
         Item("temp_tune_start_pct", 5.0, "float", "맞추기 시작 출력", "%", 0.5, 50,
              desc="첫 후보에서 처음 넣는 출력 · 그 뒤는 스스로 조절"),
         Item("temp_tune_slots", "17,33,50,100,300", "str", "맞추기 후보 칸",
              desc="고르게 켜기 칸(ms) 목록 · 쉼표로 구분\n비우면 비교 없이 현재 출력 방식으로 표만 잼"),
         Item("temp_step_s", 90.0, "float", "맞추기 계단 유지", "s", 20, 1800,
              desc="출력별 온도 표의 한 단을 유지하는 시간\n온도가 거의 멈출 만큼 길게"),
         Item("temp_beat_pct", 20.0, "float", "박자 시험 출력", "%", 1, 60,
              desc="[박자 시험] 박자마다 유지하는 열선 출력"),
         Item("temp_beat_s", 60.0, "float", "박자 시험 유지", "s", 20, 300,
              desc="[박자 시험] 박자 하나를 유지하며 기록하는 시간\n뒤쪽 2/3 로 흔들림 · 주기를 잼"),
         Item("temp_settle_s", 60.0, "float", "유지 시험 안정 대기", "s", 0, 300,
              desc="[박자 시험] · [위상 찾기] · [박자 보정] 이 출력을 넣고 첫 기록 전에 기다리는 시간\n스테이션을 켤 때 튄 온도가 가라앉을 만큼 · 0 이면 기다리지 않음"),
         Item("temp_sweep_s", 120.0, "float", "훑기 올림 시간", "s", 30, 600,
              desc="[환경 맞추기] 가 출력을 0 % 에서 최대 출력까지 일정하게 올리는 시간 · 내릴 때도 같은 속도"),
         Item("temp_sweep_hold_s", 20.0, "float", "훑기 꼭대기 유지", "s", 5, 120,
              desc="[환경 맞추기] 가 최대 출력에서 머무는 시간 · 그 뒤쪽 절반 평균이 천장 온도"),
         Item("temp_phase_s", 30.0, "float", "위상 찾기 유지", "s", 10, 120,
              desc="[위상 찾기] 위상 하나를 유지하며 기록하는 시간\n뒤쪽 2/3 로 평균 · 흔들림을 잼"),
         Item("temp_trim_s", 45.0, "float", "박자 보정 유지", "s", 20, 120,
              desc="[박자 보정] 보정 값 하나를 유지하며 기록하는 시간\n앞 10 s 를 뺀 구간으로 박자 주기를 잼"),
         Item("temp_test_precool", True, "bool", "온도 시험 예비 냉각",
              desc="가열 전에 쿨러로 시작 온도까지 냉각\n끄면 쿨러만 켜고 바로 가열"))
    + _g("판 · 핀",
         Item("plate_size_mm", 154.0, "float", "판 사각형 한 변", "mm", 10, 400, verify=True,
              desc="판에 새겨진 사각형의 한 변 길이 · 좌표의 기준\n판을 바꿨을 때 실측 값으로 변경"),
         Item("pin_long_mm", 10.0, "float", "핀 끝 긴 쪽", "mm", 0.1, 50,
              desc="핀 끝의 긴 쪽 길이 · 샘플이 핀보다 작은지 판정에 사용\n핀 교체 시 변경"),
         Item("pin_short_mm", 2.0, "float", "핀 끝 짧은 쪽", "mm", 0.1, 50,
              desc="핀 끝의 짧은 쪽 길이 · 샘플이 핀보다 작은지 판정에 사용\n핀 교체 시 변경"),
         Item("pin_long_axis", "y", "choice", "핀 긴 쪽 방향", verify=True, choices=(("x", "X 방향"), ("y", "Y 방향")),
              desc="핀 끝의 긴 쪽이 향하는 스테이지 방향\n핀 장착 방향을 바꿨을 때 변경"),
         Item("sample_layout", "free", "choice", "샘플 배치", choices=(("free", "자유"), ("grid", "격자")),
              desc="자유는 보이는 샘플만 검출 · 격자는 행 · 열 격자로 빈 자리를 \"미확인\" 으로 예측\n판 사각형 밖 샘플은 어느 쪽이든 내지 않음"),
         Item("sample_number_start", "top_left", "choice", "샘플 번호 시작", choices=(("top_left", "왼쪽 위"), ("bottom_left", "왼쪽 아래(문 쪽)")),
              desc="문 기준 화면에서 번호를 시작하는 구석 · 줄마다 왼쪽 → 오른쪽\n다시 검출하면 새 규칙"),
         Item("sample_grid_rows", 3, "int", "격자 행", "행", 1, 20,
              desc="샘플 배치가 격자일 때 행 수"),
         Item("sample_grid_cols", 5, "int", "격자 열", "열", 1, 20,
              desc="샘플 배치가 격자일 때 열 수"),
         Item("detect_faint", False, "bool", "약한 샘플 다시 찾기",
              desc="빈 판 비교에서 흐릿한(반투명) 샘플을 낮은 기준으로 한 번 더 찾음\n얼룩을 샘플로 잘못 잡을 수 있어 기본 끔 · 찾은 것은 확인 필요로 표시"),
         Item("view_shift_warn_mm", 1.0, "float", "사진 밀림 알림 기준", "mm", 0, 50,
              desc="샘플 사진이 빈 판 사진보다 이만큼 넘게 밀려 있으면 알림\n카메라나 판이 움직인 것 · 0 = 알리지 않음"),
         Item("pin_margin_mm", 0.3, "float", "핀 여유", "mm", 0, 5,
              desc="핀 크기에 더하는 여유\n샘플이 핀 + 여유보다 작으면 \"핀보다 작음\" 으로 제외"))
    + _g("좌표 · 위치 확인",
         Item("coord_warn_mm", 2.0, "float", "좌표 주의 · 변 길이", "mm", 0.1, 50,
              desc="좌표 보정 품질 주의 기준 · 변 길이 오차\n주의가 뜨면 꼭짓점 재등록"),
         Item("coord_warn_deg", 1.0, "float", "좌표 주의 · 직각", "°", 0.1, 45,
              desc="좌표 보정 품질 주의 기준 · 직각 오차\n주의가 뜨면 꼭짓점 재등록"),
         Item("marker_probe_mm", 10.0, "float", "마커 기억 이동 거리", "mm", 1, 50,
              desc="헤드 마커 등록 때 헤드를 움직여 보는 거리\n보통 변경 안 함"),
         # 헤드 마커 등록 검사(prep.marker_learn): 하나라도 어긋나면 저장하지 않는다
         Item("marker_pair_mm", 0.5, "float", "마커 두 장 허용", "mm", 0.01, 10,
              desc="같은 위치 두 장의 마커 위치 차이 허용 · 판 배율로 px 환산\n넘으면 한 번 더 촬영 · 그래도 넘으면 실패"),
         Item("marker_return_mm", 0.5, "float", "마커 복귀 허용", "mm", 0.01, 10,
              desc="마커 등록 끝에 촬영 위치로 돌아와 본 마커와 처음 위치의 차이 한도 · 판 배율로 px 환산"),
         Item("marker_angle_tol_deg", 15.0, "float", "마커 직각 허용", "°", 1, 45,
              desc="X · Y 로 움직였을 때 마커가 움직인 두 방향 사이 각 · 90° ± 이 값"),
         Item("marker_ratio_min", 0.8, "float", "마커 크기 비 최소", "", 0.1, 1,
              desc="X 쪽 ÷ Y 쪽 이동 크기의 최소"),
         Item("marker_ratio_max", 1.25, "float", "마커 크기 비 최대", "", 1, 10,
              desc="X 쪽 ÷ Y 쪽 이동 크기의 최대"),
         Item("marker_scale_min", 0.8, "float", "마커 배율 최소", "배", 0.1, 10,
              desc="마커 1 mm 당 픽셀 ÷ 판 배율(판 꼭짓점 픽셀 ÷ 판 한 변)의 최소\n마커는 판보다 높아 조금 크게 보임"),
         Item("marker_scale_max", 3.0, "float", "마커 배율 최대", "배", 1, 20,
              desc="마커 1 mm 당 픽셀 ÷ 판 배율의 최대"),
         Item("pos_warn_mm", 2.0, "float", "헤드 위치 주의 거리", "mm", 0.1, 50,
              desc="촬영 때 헤드 마커가 등록 위치와 이만큼 다르면 주의"),
         Item("pos_block_mm", 10.0, "float", "헤드 위치 경보 거리", "mm", 0.5, 100,
              desc="헤드 마커가 등록 위치와 이만큼 다르면 경보 · 측정 시작 차단\n원점 재설정 또는 좌표 재보정으로 해제"))
    + _g("접촉",                                                                          # 기본은 고정 높이. 전기 탐색(v2 방식)은 접촉 판정 기준이 정해진 뒤에 쓴다
         Item("contact_mode", "fixed", "choice", "접촉 방식", choices=(("fixed", "고정 높이"), ("search", "전기 탐색")),
              desc="고정 높이는 접촉 높이까지만 하강 · 전기 탐색은 전류를 보며 더 하강\n접촉 판정 기준을 정한 뒤 전기 탐색으로 변경"),
         Item("contact_fixed_read", True, "bool", "접촉 후 저항 기록",
              desc="고정 높이로 내린 뒤 저항을 한 번 재서 기록 · 판정 없음\n끄면 재지 않음"),
         Item("contact_z_mm", None, "optfloat", "접촉 높이", "mm", 0, 100,
              desc="핀이 샘플에 닿는 Z 높이\n초기 설정 6 에서 저장 · 샘플 두께가 바뀌면 재설정"),
         Item("contact_volt", 0.1, "float", "접촉 확인 전압", "V", 0.01, 5,
              desc="접촉을 확인할 때 거는 전압\n실장비에서 확인된 값 · 보통 변경 안 함"),
         Item("contact_ilim_a", 1e-4, "float", "접촉 확인 전류 한도", "A", 1e-8, 1.0,
              desc="접촉을 확인할 때 전류 한도 · 금속 판에 닿으면 이 한도에 걸림\n보통 변경 안 함"),
         Item("contact_nplc", 0.1, "float", "접촉 확인 NPLC", "", 0.01, 10,
              desc="접촉 확인 한 번의 적분 시간 · 작을수록 빠름\n보통 변경 안 함"),
         Item("contact_step_mm", 0.1, "float", "하강 간격", "mm", 0.00625, 0.5,
              desc="전기 탐색에서 한 번에 내리는 최대 거리 · 펄스 단위로 내림\n작을수록 느리고 정밀 · 최소 1 펄스 0.00625 mm"),
         Item("contact_start_above_mm", 0.3, "float", "저장 높이 위 시작", "mm", 0, 10,
              desc="측정 때 접촉 높이보다 이만큼 위에서 확인 시작\n샘플 높이 편차가 크면 늘림"),
         Item("contact_max_below_mm", 0.5, "float", "저장 높이 아래 한도", "mm", 0, 5,
              desc="측정 때 접촉 높이보다 이만큼 아래까지만 하강\n거기까지 안 닿으면 그 샘플 실패"),
         Item("contact_confirm_n", 2, "int", "접촉 확인 횟수", "번", 1, 10,
              desc="전류가 이 횟수만큼 연속으로 흐르면 접촉으로 판정"),
         Item("contact_overdrive_mm", 0.05, "float", "더 누르는 거리", "mm", 0, 1,
              desc="첫 접촉 뒤 더 누르는 거리 · 네 핀이 모두 닿게 함"),
         Item("contact_short_ohm", 1000.0, "float", "금속 판 판정 저항", "Ω", 0.1, 1e9,
              desc="2단자 저항이 이 값보다 작으면 금속 판 접촉으로 판정\n샘플 저항이 매우 낮을 때 낮춤"),
         Item("contact_search_mm", 1.0, "float", "탐색 거리", "mm", 0.1, 20,
              desc="[접촉 탐색] 이 현재 높이부터 내려가는 최대 거리\n핀을 샘플 가까이 내린 뒤 탐색"),
         Item("contact_search_step_mm", 0.1, "float", "탐색 간격", "mm", 0.00625, 0.5,
              desc="[접촉 탐색] 의 첫 하강에서 한 번에 내리는 최대 거리\n하강 간격 이상으로 설정"),
         Item("contact_search_back_mm", 0.3, "float", "탐색 되돌림 거리", "mm", 0, 5,
              desc="[접촉 탐색] 에서 닿은 뒤 이만큼 올렸다가 하강 간격으로 다시 확인\n탐색 간격 × 접촉 확인 횟수 이상으로 설정"),
         Item("contact_four_min_ratio", 0.1, "float", "4단자 최소 비율", "", 0, 1,
              desc="4단자 저항이 2단자의 이 비율보다 작으면 감지 핀 미접촉으로 판정"),
         Item("contact_max_reads", 2000, "int", "탐색 읽기 상한", "번", 10, 100000,
              desc="한 번의 탐색에서 읽는 최대 횟수 · 넘으면 미접촉으로 종료"),
         Item("contact_open_a", 1e-9, "float", "미접촉 판정 전류", "A", 1e-13, 1e-3,
              desc="전류가 이 값보다 작으면 미접촉으로 판정\n측정 중 핀 떨어짐 판정에도 사용"))
    + _g("측정 조건",
         Item("meter_volt", 0.1, "float", "측정 전압", "V", 0.01, 20,                 # 기존 LabVIEW Set voltage(파일 이름 _0.10V_)
              desc="저항을 잴 때 샘플에 거는 전압 · 파일 이름에 들어감\n기존 LabVIEW Set voltage 와 같음 · 기존 데이터와 비교할 때 같은 값 사용"),
         Item("meter_four_wire", True, "bool", "4단자",
              desc="4단자로 측정 · 끄면 2단자"),
         Item("meter_avg_count", 10, "int", "평균 횟수", "회", 1, 100,
              desc="한 번 읽을 때 평균하는 횟수 · 클수록 잡음이 적고 느림"),
         Item("meter_nplc", 1.0, "float", "NPLC", "", 0.01, 10,
              desc="적분 시간(전원 주기 수) · 클수록 잡음이 적고 느림"),
         Item("meter_ilim_a", 0.1, "float", "전류 한도", "A", 1e-8, 1.0,
              desc="저항을 잴 때 전류 한도\n저항이 낮은 샘플을 보호할 때 낮춤"),
         Item("run_start_c", 30.0, "float", "시작 온도", "°C", -20, 200,
              desc="가열 전에 이 온도까지 냉각한 뒤 기록 시작"),
         Item("run_heater_off_c", 85.0, "float", "가열 끝 온도", "°C", 0, 250,
              desc="이 온도에 닿으면 열선 끔 · 가열 속도 제어의 목표 상한\n히트건 차단 온도보다 낮게 설정"),
         Item("run_cool_end_c", 30.0, "float", "냉각 끝 온도", "°C", -20, 200,
              desc="이 온도까지 냉각하면 그 샘플 측정 완료"),
         Item("run_repeat", 1, "int", "반복", "회", 1, 20,
              desc="샘플마다 가열 · 냉각을 반복하는 횟수\n회차마다 파일 하나"),
         Item("run_interval_s", 0.9, "float", "측정 간격", "s", 0.05, 60,               # 기본 = 기본 설정의 읽기 시간 예상(온도 0.3 + 저항 0.6)
              desc="한 점(온도 · 저항)의 주기 · 읽기 시간 예상보다 짧으면 거절\n가열 · 냉각 속도 = 목표 점 간격 ÷ 이 값 × 60"),
         Item("batch_name", "1", "str", "묶음",
              desc="묶음 이름 · 기록 폴더와 샘플 기본 이름에 사용"))
    + _g("측정 한계",                                                                     # v2 backend/rt.py 에서 쓰던 값
         Item("run_no_rise_s", 45.0, "float", "가열 무반응 시간", "s", 1, 600,
              desc="열선이 켜져 있던 시간이 이만큼 쌓여도 안 오르면 그 샘플 실패\n오른 정도는 가열 무반응 기준으로 판정"),
         Item("run_no_rise_c", 0.5, "float", "가열 무반응 기준", "°C", 0.05, 20,
              desc="가열 무반응 시간 동안 이만큼도 안 오르면 실패"),
         Item("run_fast_rise_c_s", 5.0, "float", "빠른 가열 주의", "°C/s", 0.5, 100,
              desc="1초에 이보다 빨리 오르면 주의 한 번 · 열선은 끄지 않음"),
         Item("run_stall_s", 180.0, "float", "냉각 정체 시간", "s", 5, 3600,
              desc="냉각 중 이 시간 동안 온도가 안 내려가면 그 단계를 정상 종료\n내려간 정도는 냉각 정체 기준으로 판정"),
         Item("run_stall_c", 0.3, "float", "냉각 정체 기준", "°C", 0.05, 20,
              desc="냉각 정체 시간 동안 이만큼도 안 내려가면 정체로 판정"),
         Item("run_phase_limit_s", 1800.0, "float", "한 단계 시간 한도", "s", 10, 36000,
              desc="가열 · 냉각 한 단계가 이 시간을 넘으면 그 샘플 실패"),
         Item("run_open_n", 5, "int", "핀 떨어짐 판정 횟수", "번", 1, 100,
              desc="측정 중 전류가 미접촉 판정 전류보다 작은 일이 이 횟수 연속이면 주의"))
    + _g("읽기 시간 검사",                                                                # 측정 간격 + 읽기 시간이 온도 못 읽음 허용 시간보다 충분히 짧은지(problems)
         Item("stale_use_ratio", 0.7, "float", "허용 시간 사용 비율", "", 0.1, 1,
              desc="읽기 한 회차 예상이 온도 못 읽음 허용 시간 × 이 값 미만 필요\n넘으면 히트건이 꺼져 가열 끝 온도 미도달"),
         Item("line_hz", 60, "int", "전원 주파수", "Hz", 50, 60,
              desc="적분 시간 계산의 기준 · 1 PLC = 1 ÷ 전원 주파수\n국내는 60"),
         Item("meter_conv_factor", 3.0, "float", "저항 읽기 변환 배수", "", 1, 5, verify=True,
              desc="저항 한 번 읽을 때의 적분 횟수 · 자동 영점 켬은 3번\n읽기 시간 예상에만 사용"),
         Item("read_overhead_s", 0.1, "float", "저항 읽기 통신 여유", "s", 0, 5, verify=True,
              desc="저항 한 번 읽을 때 통신과 범위 단계에 드는 시간\n읽기 시간 예상에만 사용 · visa.log 로 확인"),
         Item("temp_read_s", 0.3, "float", "온도 읽기 시간", "s", 0.01, 10, verify=True,
              desc="온도 한 번 읽는 데 걸리는 시간\n읽기 시간 예상에만 사용 · visa.log 로 확인"))
    + _g("저장",
         Item("nas_dir", "", "str", "NAS 폴더", folder=True,
              desc="측정 파일(.dat)을 복사할 NAS 폴더\n비우면 PC 에만 저장"),
         Item("nas_date_folder", True, "bool", "NAS 날짜 폴더",
              desc="NAS 폴더 아래에 연도 · 날짜 폴더를 만들어 저장\n끄면 NAS 폴더 바로 아래 묶음 폴더에 저장"))
)
BY_KEY = {i.key: i for i in ITEMS}

# PC 값: PC 마다 다르다(settings.json 에 남는다 · 설정 가져오기의 "PC 값 제외" 가 건너뛴다). 그 밖은 장비 · 측정 값(코드 기본값 · 바꾼 값만 저장)
PC_KEYS = frozenset({"stage_port", "k2110_addr", "k2450_addr", "data_dir", "auto_connect", "blocked_ports", "camera_index",
                     "nas_dir", "nas_date_folder", "photo_head_xy", "contact_z_mm"})

# 장비 값: 장비(한 대)에 딸린 값 - 코드 기본값이 그 장비의 값이다(배선 · 설치가 바뀌면 코드 기본값도 바꾼다). 전체 설정에 "장비 값" 표시
DEVICE_KEYS = frozenset({"cooler_grid", "machine_front_side", "machine_right_side", "view_rot", "view_flip", "camera_focus_value",
                         "heater_fan_relay", "heater_element_relay", "cooler_relays", "heat_model_k", "heat_model_tau_s", "heat_model_theta_s",
                         "heater_pwm_period_ms", "heater_boot_hold_s", "heater_out_mode", "heater_slot_ms", "heater_timing", "heat_curve"})

# 기본값 이력(키 → 예전 기본값들): 저장된 값이 예전 기본값과 같으면 "바꾼 적 없음" 으로 보고 지운다(새 기본값을 따른다 - Config.load 한 번 · 로그 한 줄씩).
# 기본값을 바꿀 때 예전 값을 여기에 더한다. 없어진 키는 REMOVED_KEYS 에(읽을 때 버리고 로그).
DEFAULT_HISTORY = {
    "meter_volt": [1.0],
    "camera_focus_value": [-1],
    "cooler_grid": [[[2, 2, 2], [3, 3, 3], [4, 4, 4]]],
    "run_interval_s": [1.0],
    "heater_pwm_period_ms": [1000],
    "temp_step_s": [240.0],
    "heater_slot_ms": [10],
    "temp_tune_slots": ["20,30,70,100,150,200,300"],
}
REMOVED_KEYS = {
    "marker_pair_px": "마커 두 장 허용은 mm(marker_pair_mm) · 지연 대기 뒤 2.9 · 5.7 px(약 0.2~0.3 mm)가 2 px 로 거절됐다",
    "marker_return_px": "마커 복귀 허용은 mm(marker_return_mm)",
    "cooler_view": "화면 방향은 문 기준 하나(view_rot · view_flip)",
    "heater_min_on_s": "열선 출력 % 제어(SSR)라 최소 켜짐 없음",
    "heater_min_off_s": "열선 출력 % 제어(SSR)라 최소 꺼짐 없음",
    "heater_band_c": "켜기 · 끄기 판정 대신 출력 % 제어",
    "run_heat_rate_c_min": "가열 속도 = 목표 점 간격 ÷ 읽기 간격 × 60",
    "temp_step_pct": "맞추기는 비교 온도에 맞는 출력을 스스로 찾는다 · temp_tune_c · temp_tune_start_pct",
}


N_RELAY = 10                                       # 보드의 릴레이 수(펌웨어 V11 · R1~R10)
ROLE_KEYS = ("heater_fan_relay", "heater_element_relay", "cooler_relays")      # 릴레이 역할(릴레이가 하나라도 켜져 있으면 바꾸지 못한다)


def _relay_no(n) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= N_RELAY


def default_grid(coolers):
    """쿨러 배치를 되돌릴 값: 쿨러 릴레이가 기본값이면 ITEMS 의 기본값(장비 값). 아니면 판의 줄(위 → 아래)마다 쿨러 릴레이를 차례로."""
    cs = [int(n) for n in coolers]
    if cs == list(BY_KEY["cooler_relays"].default):
        return copy.deepcopy(BY_KEY["cooler_grid"].default)
    return [[cs[r % len(cs)]] * 3 for r in range(3)]


class ConfigError(ValueError):
    """잘못된 설정 값. 문구는 사람이 읽는 한국어 한 문장이다."""


def check(item: Item, value: Any) -> Any:
    """한 항목 검증. 통과한 값(형 변환 후)을 돌려주고 못 쓰면 ConfigError."""
    name = item.label
    if item.kind == "str":
        if not isinstance(value, str):
            raise ConfigError("%s: 문자 필요" % name)
        return value.strip()
    if item.kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError("%s: 켬/끔 값 필요" % name)
        return value
    if item.kind == "grid":                       # 칸마다 릴레이 번호 또는 빈 칸(None). 같은 번호를 여러 칸에 써도 된다(쿨러 하나가 여러 칸을 맡는다)
        ok = isinstance(value, list) and len(value) == 3 and all(isinstance(r, list) and len(r) == 3 for r in value)   # 쿨러 릴레이인지는 problems() 가 본다
        nums = [n for r in value for n in r] if ok else []
        if not ok or not all(n is None or _relay_no(n) for n in nums):
            raise ConfigError("%s: 3×3 칸에 릴레이 번호 1~%d 또는 빈 칸 필요" % (name, N_RELAY))
        return [[None if n is None else int(n) for n in r] for r in value]
    if item.kind == "ints":                       # min = 0 이면 빈 목록도 된다(온도 시험 쿨러 - 쿨러 없이 시험)
        lo = 0 if item.min == 0 else 1
        if not isinstance(value, list) or not lo <= len(value) <= N_RELAY or not all(_relay_no(n) for n in value) or len(set(value)) != len(value):
            raise ConfigError("%s: 릴레이 번호 1~%d 목록 필요 · 중복 없이 · 예 [2, 3, 4]" % (name, N_RELAY))
        return [int(n) for n in value]
    if item.kind == "choice":
        if value not in [c[0] for c in item.choices]:
            raise ConfigError("%s: %s 중 하나 필요" % (name, " · ".join(c[1] for c in item.choices)))
        return value
    if item.kind == "list":
        if not isinstance(value, list) or not all(isinstance(s, str) and s.strip() for s in value):
            raise ConfigError("%s: 글자 목록 필요 · 예 [\"COM4\"]" % name)
        return [s.strip() for s in value]
    if item.kind == "optfloat" and value is None:
        return None
    if item.kind == "xy":
        if value is None:
            return None
        if not (isinstance(value, list) and len(value) == 2 and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in value)):
            raise ConfigError("%s: [X, Y] 숫자 2개 또는 빈 값 필요" % name)
        return [float(value[0]), float(value[1])]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("%s: 숫자 필요" % name)
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")):
        raise ConfigError("%s: 숫자 필요" % name)
    if item.min is not None and v < item.min:
        raise ConfigError("%s: %g%s 이상 필요" % (name, item.min, item.unit))
    if item.max is not None and v > item.max:
        raise ConfigError("%s: %g%s 이하 필요" % (name, item.max, item.unit))
    if item.kind == "int" and v != int(v):
        raise ConfigError("%s: 정수 필요" % name)
    return int(v) if item.kind == "int" else v


def _show(item: Item, v) -> str:
    """값을 사람이 읽는 글로(설명 말풍선의 기본값 · 범위)."""
    if v is None:
        return "없음"
    if item.kind == "bool":
        return "켬" if v else "끔"
    if item.kind == "choice":
        return dict(item.choices).get(v, str(v))
    if item.kind in ("grid", "ints", "xy", "list"):
        return json.dumps(v, ensure_ascii=False)
    if item.kind in ("int", "float", "optfloat"):
        return "%g" % v
    return str(v) if str(v) else "없음"


def hint(item: Item) -> str:
    """설명 말풍선의 마지막 줄: 단위 · 기본값 · 범위(항목 정의에서 만든다 - 설명 글에 숫자를 따로 적지 않는다)."""
    parts = []
    if item.unit:
        parts.append("단위 %s" % item.unit)
    parts.append("기본값 %s" % _show(item, item.default))
    if item.min is not None and item.max is not None and item.kind != "ints":
        parts.append("범위 %g~%g" % (item.min, item.max))
    return " · ".join(parts)


def problems(v: dict) -> list:
    """설정 조합 검사: 값 하나하나는 맞아도 같이 쓰면 안 되는 조합을 찾는다. 문제마다 사람이 읽는 한 줄. 없으면 [].
    설정 저장(PUT)과 측정 시작 · 재개가 이 함수 하나를 쓴다(app/control.py update_settings · app/jobs/run.py start · resume)."""
    out = heat_problems(v)
    if not float(v["run_no_rise_s"]) < float(v["heater_max_on_s"]):
        out.append("가열 무반응 시간 %g초 · 열선 연속 최대 시간 %g초 미만 필요" % (v["run_no_rise_s"], v["heater_max_on_s"]))
    # ---- 읽기 시간: 측정 중 온도는 측정 간격마다 읽는다. 한 회차가 온도 못 읽음 허용 시간에 가까우면 안전 감시가 히트건을 꺼서
    #      모든 샘플이 "가열 끝 온도 미도달" 이 된다 → 측정 간격 + 온도 읽기 + 저항 읽기 예상 < 허용 시간 × 사용 비율 ----
    cycle = cycle_time_s(v)
    limit = float(v["temp_stale_s"]) * float(v["stale_use_ratio"])
    if not cycle < limit:
        out.append("읽기 한 회차 예상 %.2f초 · %.2f초 미만 필요 · 측정 간격과 평균 횟수 확인" % (cycle, limit))
    rt = read_time_s(v)
    if float(v["run_interval_s"]) < rt - 1e-9:                  # 측정 간격 = 한 점의 주기 - 읽기가 더 걸리면 점 간격이 설정보다 넓어진다
        out.append("측정 간격 %g초 · 읽기 시간 %.2f초 이상 필요" % (v["run_interval_s"], rt))
    out += side_problems(v)
    out += focus_problems(v)
    # ---- 접촉 값들의 크기 관계 ----
    if float(v["contact_step_mm"]) > float(v["contact_search_step_mm"]):
        out.append("하강 간격 %g mm · 탐색 간격 %g mm 이하 필요" % (v["contact_step_mm"], v["contact_search_step_mm"]))
    if float(v["contact_search_step_mm"]) > float(v["contact_search_mm"]):
        out.append("탐색 간격 %g mm · 탐색 거리 %g mm 이하 필요" % (v["contact_search_step_mm"], v["contact_search_mm"]))
    if float(v["contact_overdrive_mm"]) > float(v["contact_max_below_mm"]):
        out.append("더 누르는 거리 %g mm · 저장 높이 아래 한도 %g mm 이하 필요" % (v["contact_overdrive_mm"], v["contact_max_below_mm"]))
    need_back = int(v["contact_confirm_n"]) * float(v["contact_search_step_mm"])
    if float(v["contact_search_back_mm"]) < need_back - 1e-9:
        out.append("탐색 되돌림 거리 %g mm · 탐색 간격 × 확인 횟수 %g mm 이상 필요" % (v["contact_search_back_mm"], need_back))
    if not float(v["contact_open_a"]) < float(v["contact_ilim_a"]):
        out.append("미접촉 판정 전류 %g A · 접촉 확인 전류 한도 %g A 미만 필요" % (v["contact_open_a"], v["contact_ilim_a"]))
    # ---- Z 하강 한도 · 포트 ----
    if v["z_floor_mode"] == "fixed":
        if v["z_floor_mm"] is None:
            out.append("Z 하강 한도 고정 값 필요")
        elif float(v["z_floor_mm"]) > float(v["z_max_mm"]):
            out.append("Z 한도 고정 값 %g mm · Z 가동 범위 %g mm 이하 필요" % (v["z_floor_mm"], v["z_max_mm"]))
    if port_blocked(v["stage_port"], v["blocked_ports"]):
        out.append("스테이지 포트 %s · 사용 안 할 포트에 포함됨" % v["stage_port"])
    return out


def heat_problems(v: dict) -> list:
    """가열 · 냉각 온도와 가열 제어 · 릴레이 역할의 조합 검사. 자동 측정(problems)과 온도 시험(temptest_problems 와 같이)이 같이 쓴다."""
    out = []
    # ---- 온도 ----
    if not float(v["run_heater_off_c"]) < float(v["heater_max_c"]):
        out.append("가열 끝 온도 %g °C · 차단 온도 %g °C 미만 필요" % (v["run_heater_off_c"], v["heater_max_c"]))
    if not float(v["run_cool_end_c"]) < float(v["run_heater_off_c"]):
        out.append("냉각 끝 온도 %g °C · 가열 끝 온도 %g °C 미만 필요" % (v["run_cool_end_c"], v["run_heater_off_c"]))
    if not float(v["run_start_c"]) < float(v["run_heater_off_c"]):
        out.append("시작 온도 %g °C · 가열 끝 온도 %g °C 미만 필요" % (v["run_start_c"], v["run_heater_off_c"]))
    # ---- 가열 제어: 예상 가열 시간이 한 단계 시간 한도 안 · 최소 켜짐 < 연속 최대 · 허용 폭 > 0 ----
    if v.get("run_rate_by") == "time":                              # 걸리는 시간으로 정한 속도가 쓸 수 있는 범위인가(시작 < 가열 끝 · 냉각 끝 < 가열 끝 은 위에서 본다)
        if v["run_heat_mode"] == "ramp" and float(v["run_start_c"]) < float(v["run_heater_off_c"]):
            hr = heat_rate_c_min(v)
            if not RATE_MIN <= hr <= RATE_MAX:
                out.append("가열 시간 %g초 · 속도 %.1f °C/분 · %g ~ %g °C/분 범위 필요" % (v["run_heat_time_s"], hr, RATE_MIN, RATE_MAX))
        if v["run_cool_mode"] == "ramp" and float(v["run_cool_end_c"]) < float(v["run_heater_off_c"]):
            cr = cool_rate_c_min(v)
            if not RATE_MIN <= cr <= RATE_MAX:
                out.append("냉각 시간 %g초 · 속도 %.1f °C/분 · %g ~ %g °C/분 범위 필요" % (v["run_cool_time_s"], cr, RATE_MIN, RATE_MAX))
    if v["run_heat_mode"] == "ramp":
        heat_s = heat_time_s(v)
        if not heat_s < float(v["run_phase_limit_s"]):
            out.append("예상 가열 시간 %.0f초 · 한 단계 시간 한도 %g초 미만 필요 · 가열 속도 확인" % (heat_s, v["run_phase_limit_s"]))
    if v["run_cool_mode"] == "ramp":
        cool_s = cool_time_s(v)
        if not cool_s < float(v["run_phase_limit_s"]):
            out.append("예상 냉각 시간 %.0f초 · 한 단계 시간 한도 %g초 미만 필요 · 냉각 속도 확인" % (cool_s, v["run_phase_limit_s"]))
    if not int(v["heater_full_pct"]) <= int(v["heater_max_pct"]):
        out.append("연속 판정 출력 %d %% · 열선 최대 출력 %d %% 이하 필요" % (v["heater_full_pct"], v["heater_max_pct"]))
    out += curve_problems(v)
    # ---- 릴레이 역할: 송풍 · 열선 · 쿨러가 서로 겹치지 않는다 · 쿨러 배치는 쿨러 릴레이만 ----
    out += role_problems(v)
    return out


# ---- [히트건 맞추기] 후보 칸 · 출력별 온도 표(글자 설정 → 값) ----
def _parse_slots(s):
    """"20,30,70" → [20, 30, 70]. 빈 값 → []. 숫자가 아니면 ValueError."""
    return [int(x.strip()) for x in str(s or "").split(",") if x.strip()]


def _parse_curve(s):
    """"0:26.1 2.0:35.4" → [(0.0, 26.1), (2.0, 35.4)]. 빈 값 → []. 모양이 틀리면 ValueError."""
    out = []
    for tok in str(s or "").split():
        a, b = tok.split(":")
        out.append((float(a), float(b)))
    return out


def tune_slots(v) -> list:
    """맞추기 후보 칸(ms) 목록. 못 읽으면 [](설정 저장 · 시작 검사가 tune_slot_problems 로 거절한다)."""
    try:
        return _parse_slots(v)
    except ValueError:
        return []


def heat_curve(v) -> list:
    """출력별 온도 표 [(출력 %, 온도 °C)]. 빈 값이나 못 읽으면 [](설정 저장 · 시작 검사가 curve_problems 로 거절한다)."""
    try:
        return _parse_curve(v)
    except ValueError:
        return []


def curve_text(pts) -> str:
    """[(출력 %, 온도)] → "0:26.1 2.0:35.4"(heat_curve 저장 형식)."""
    return " ".join("%g:%.1f" % (round(float(p), 1), float(T)) for p, T in pts)


def curve_problems(v: dict) -> list:
    """출력별 온도 표: 2~12점 · 첫 점 출력 0 · 출력과 온도 모두 앞 점보다 큼. 빈 값은 문제 아님(표 없음)."""
    s = str(v.get("heat_curve") or "").strip()
    if not s:
        return []
    try:
        pts = _parse_curve(s)
    except ValueError:
        return ["출력별 온도 표 · \"출력:온도\" 를 빈칸으로 이은 글 필요"]
    if not 2 <= len(pts) <= 12:
        return ["출력별 온도 표 %d점 · 2~12점 필요" % len(pts)]
    if pts[0][0] != 0:
        return ["출력별 온도 표 · 첫 점 출력 0 필요"]
    for (pa, ta), (pb, tb) in zip(pts, pts[1:]):
        if not (pb > pa and tb > ta):
            return ["출력별 온도 표 · %g %% 뒤 출력 · 온도 모두 커야 함" % pa]
    return []


def tune_slot_problems(v: dict) -> list:
    """맞추기 후보 칸: 10~1000 · 중복 없이 · 10개 이하(펌웨어 V15 ~ V18 이 전원 주기의 정수배로 맞춘다)."""
    try:
        ms = _parse_slots(v.get("temp_tune_slots"))
    except ValueError:
        return ["맞추기 후보 칸 · 쉼표로 이은 숫자 필요"]
    if len(ms) > 10:
        return ["맞추기 후보 칸 %d개 · 10개 이하 필요" % len(ms)]
    if len(set(ms)) != len(ms):
        return ["맞추기 후보 칸 · 중복 없이 필요"]
    bad = [m for m in ms if not 10 <= m <= 1000]
    if bad:
        return ["맞추기 후보 칸 %d ms · 10~1000 필요" % bad[0]]
    return []


def focus_problems(v: dict) -> list:
    """초점 찾기 값의 크기 관계: 시작 값 < 끝 값 · 작은 간격 < 큰 간격."""
    out = []
    if not int(v["focus_scan_min"]) < int(v["focus_scan_max"]):
        out.append("초점 찾기 시작 값 %d · 끝 값 %d 미만 필요" % (v["focus_scan_min"], v["focus_scan_max"]))
    if not int(v["focus_fine_step"]) < int(v["focus_coarse_step"]):
        out.append("초점 작은 간격 %d · 큰 간격 %d 미만 필요" % (v["focus_fine_step"], v["focus_coarse_step"]))
    return out


def side_problems(v: dict) -> list:
    """장비 앞쪽 · 오른쪽 방향은 서로 다른 축이어야 한다(X 하나 · Y 하나)."""
    f, r = v["machine_front_side"], v["machine_right_side"]
    if f[0] == r[0]:
        return ["장비 앞쪽 %s · 오른쪽 %s 같은 축 · X 하나 · Y 하나 필요" % (SIDE_NAME[f], SIDE_NAME[r])]
    return []


def grid_relays(grid) -> list:
    """쿨러 배치에 든 릴레이 번호(빈 칸 제외 · 칸 순서대로)."""
    return [int(n) for row in grid for n in row if n is not None]


def grid_empty(grid) -> int:
    """쿨러 배치의 빈 칸 수."""
    return sum(1 for row in grid for n in row if n is None)


def role_problems(v: dict) -> list:
    """릴레이 역할이 겹치는지 · 쿨러 배치가 쿨러 릴레이만 쓰는지. 문제마다 한 줄."""
    out = []
    fan, el, cool = int(v["heater_fan_relay"]), int(v["heater_element_relay"]), [int(n) for n in v["cooler_relays"]]
    if fan == el:
        out.append("송풍 · 열선 릴레이 R%d 겹침" % fan)
    for n, what in ((fan, "송풍"), (el, "열선")):
        if n in cool:
            out.append("R%d %s · 쿨러 릴레이와 겹침" % (n, what))
    bad = sorted(set(grid_relays(v["cooler_grid"])) - set(cool))
    if bad:
        out.append("쿨러 배치 %s · 쿨러 릴레이 아님" % " · ".join("R%d" % n for n in bad))
    return out


def temptest_problems(v: dict) -> list:
    """온도 시험 설정의 조합 검사: 켤 쿨러는 쿨러 릴레이 안 · 읽기 간격 ≥ 온도 읽기 시간 · 읽기 간격 < 온도 못 읽음 허용 시간 × 사용 비율
    (온도 시험은 측정 주체 규칙 - 그 시간 넘게 못 읽으면 안전 감시가 열선을 끈다). 설정 저장과 온도 시험 시작이 쓴다."""
    out = []
    bad = sorted({int(n) for n in v["temp_test_coolers"]} - {int(n) for n in v["cooler_relays"]})
    if bad:
        out.append("온도 시험 쿨러 %s · 쿨러 릴레이 아님" % " · ".join("R%d" % n for n in bad))
    out += tune_slot_problems(v)
    iv = float(v["temp_test_interval_s"])
    if iv < float(v["temp_read_s"]):
        out.append("온도 시험 읽기 간격 %g초 · 온도 읽기 시간 %g초 이상 필요" % (iv, v["temp_read_s"]))
    limit = float(v["temp_stale_s"]) * float(v["stale_use_ratio"])
    if not iv < limit:
        out.append("온도 시험 읽기 간격 %g초 · %.2f초 미만 필요" % (iv, limit))
    full_s = sweep_full_s(v)
    if full_s >= float(v["heater_max_on_s"]):
        out.append("훑기 중 연속 판정 출력 이상 %.0f초 · 열선 연속 최대 %g초 미만 필요" % (full_s, v["heater_max_on_s"]))
    return out


def sweep_full_s(v: dict) -> float:
    """[환경 맞추기] 훑기에서 출력이 연속 판정 출력(heater_full_pct) 이상인 시간 = 올림 · 내림 두 번 × 올림 시간 × (1 − 연속 판정 ÷ 최대 출력)(음수면 0) + 꼭대기 유지
    (안전 감시의 열선 연속 최대 시간은 이 출력 이상만 센다 - Safety.full_on_t)."""
    frac = max(0.0, 1.0 - float(v["heater_full_pct"]) / float(v["heater_max_pct"]))
    return 2.0 * float(v["temp_sweep_s"]) * frac + float(v["temp_sweep_hold_s"])


RATE_MIN, RATE_MAX = 0.1, 600.0                     # 속도 정하는 방법 "걸리는 시간" 으로 나온 속도(°C/분)가 들어야 할 범위


def heat_rate_c_min(v: dict, interval_key="run_interval_s") -> float:
    """가열 속도(°C/분). 속도 정하는 방법(run_rate_by) "step" = 목표 점 간격 ÷ 읽기 간격 × 60(측정 run_interval_s · 온도 시험 temp_test_interval_s) ·
    "time" = (가열 끝 온도 − 시작 온도) ÷ 가열 시간 × 60(읽기 간격과 무관). 속도는 모두 이 함수와 cool_rate_c_min 에서만 정한다."""
    if v.get("run_rate_by") == "time":
        return (float(v["run_heater_off_c"]) - float(v["run_start_c"])) / max(1e-6, float(v["run_heat_time_s"])) * 60.0
    return float(v["run_point_step_c"]) / max(1e-6, float(v[interval_key])) * 60.0


def cool_rate_c_min(v: dict, interval_key="run_interval_s") -> float:
    """냉각 속도(°C/분): "time" = (가열 끝 온도 − 냉각 끝 온도) ÷ 냉각 시간 × 60(냉각 속도 따로 · 냉각 속도는 쓰지 않는다) ·
    "step" 이면 "냉각 속도 따로" 는 run_cool_rate_c_min · 아니면 가열과 같은 점 간격."""
    if v.get("run_rate_by") == "time":
        return (float(v["run_heater_off_c"]) - float(v["run_cool_end_c"])) / max(1e-6, float(v["run_cool_time_s"])) * 60.0
    return float(v["run_cool_rate_c_min"]) if v["cool_rate_separate"] else heat_rate_c_min(v, interval_key)


def point_step_c(v: dict, interval_key="run_interval_s") -> float:
    """보여 줄 목표 점 간격(°C): "step" 이면 run_point_step_c 그대로 · "time" 이면 가열 속도 × 읽기 간격 ÷ 60."""
    if v.get("run_rate_by") == "time":
        return heat_rate_c_min(v, interval_key) * float(v[interval_key]) / 60.0
    return float(v["run_point_step_c"])


def read_time_s(v: dict) -> float:
    """측정 한 점의 읽기 시간 예상(초) = 온도 읽기 + 저항 읽기(meters.read_time_s). 측정 간격은 이보다 짧을 수 없다."""
    from app.devices.meters import read_time_s as r_s
    return float(v["temp_read_s"]) + r_s(v["meter_nplc"], v["meter_avg_count"], v["line_hz"], v["meter_conv_factor"], v["read_overhead_s"])


def expect(v: dict, interval_key="run_interval_s") -> dict:
    """예상 점 수 · 시간(화면 · 시작 전): 가열 (끝 − 시작) ÷ 점 간격 · 냉각 (가열 끝 − 냉각 끝) ÷ 냉각 점 간격."""
    iv = float(v[interval_key])
    hr, cr = heat_rate_c_min(v, interval_key), cool_rate_c_min(v, interval_key)
    hs = max(0.0, float(v["run_heater_off_c"]) - float(v["run_start_c"]))
    cs = max(0.0, float(v["run_heater_off_c"]) - float(v["run_cool_end_c"]))
    return {"heat_rate": round(hr, 2), "cool_rate": round(cr, 2), "interval": iv,
            "heat_n": int(round(hs / max(1e-6, hr * iv / 60.0))), "cool_n": int(round(cs / max(1e-6, cr * iv / 60.0))),
            "heat_s": round(heat_time_s(v, interval_key), 1), "cool_s": round(cool_time_s(v, interval_key), 1)}


def heat_time_s(v: dict, interval_key="run_interval_s") -> float:
    """가열 속도 제어의 예상 가열 시간(초) = (가열 끝 온도 − 시작 온도) ÷ 가열 속도 + 유지 시간("time" 이면 가열 시간 + 유지 시간 - 같은 값)."""
    if v.get("run_rate_by") == "time":
        return float(v["run_heat_time_s"]) + float(v["run_heat_hold_s"])
    span = max(0.0, float(v["run_heater_off_c"]) - float(v["run_start_c"]))
    return span / heat_rate_c_min(v, interval_key) * 60.0 + float(v["run_heat_hold_s"])


def cool_time_s(v: dict, interval_key="run_interval_s") -> float:
    """냉각 속도 제어의 예상 냉각 시간(초) = (가열 끝 온도 − 냉각 끝 온도) ÷ 냉각 속도("time" 이면 냉각 시간 - 같은 값)."""
    if v.get("run_rate_by") == "time":
        return float(v["run_cool_time_s"])
    span = max(0.0, float(v["run_heater_off_c"]) - float(v["run_cool_end_c"]))
    return span / cool_rate_c_min(v, interval_key) * 60.0


def cycle_time_s(v: dict) -> float:
    """측정 한 회차(온도 한 번 + 저항 한 번 + 측정 간격)의 예상 시간(초). 저항 읽기 시간의 근거는 meters.read_time_s."""
    from app.devices.meters import read_time_s
    r = read_time_s(v["meter_nplc"], v["meter_avg_count"], v["line_hz"], v["meter_conv_factor"], v["read_overhead_s"])
    return float(v["run_interval_s"]) + float(v["temp_read_s"]) + r


def port_blocked(port, blocked) -> bool:
    """사용 안 할 포트인가(대소문자 구분 없이)."""
    p = str(port or "").strip().upper()
    return bool(p) and p in [str(b).strip().upper() for b in (blocked or [])]


def defaults() -> dict:
    return {i.key: copy.deepcopy(i.default) for i in ITEMS}


class Config:
    def __init__(self, path: Optional[os.PathLike] = None):
        self.path = Path(path) if path else DEFAULT_PATH
        self.values = defaults()
        self.loaded_notes = []                       # 읽을 때 무시한 값(화면 · 로그용)
        self.migrated = []                           # 예전 설정을 지금 규칙으로 옮긴 것(로그 한 줄씩 - 경보는 띄우지 않는다)
        self.cooler_reset = False                    # 쿨러 배치를 기본값으로 되돌렸다 → 초기 설정 7 미완료(Controller 가 calib 을 지운다)

    # ---- 바꾼 값 ----
    def changed(self) -> dict:
        """기본값과 다른 값만(저장 · 내보내기에 쓴다)."""
        return {i.key: copy.deepcopy(self.values[i.key]) for i in ITEMS if self.values[i.key] != i.default}

    # ---- 읽기 · 쓰기 ----
    def load(self):
        self.values = defaults()
        self.loaded_notes = []
        self.migrated = []
        self.cooler_reset = False
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            self.loaded_notes.append("설정 파일 읽기 실패 · 기본값 사용 · %s" % str(e)[:40])
            return self
        if not isinstance(raw, dict):
            self.loaded_notes.append("설정 파일 형식 오류 · 기본값 사용")
            return self
        for k in list(raw):
            if k in DEFAULT_HISTORY and raw[k] in DEFAULT_HISTORY[k] and raw[k] != BY_KEY[k].default:
                self.migrated.append("%s · 예전 기본값 %s 저장됨 · 새 기본값 %s 사용" % (
                    BY_KEY[k].label, json.dumps(raw[k], ensure_ascii=False), json.dumps(BY_KEY[k].default, ensure_ascii=False)))
                del raw[k]                            # 바꾼 적 없는 값: 새 기본값을 따른다(다음 저장 때 파일에서도 빠진다)
            elif k in REMOVED_KEYS:
                self.migrated.append("%s · 없어진 설정 · %s" % (k, REMOVED_KEYS[k]))
                del raw[k]
        for k, v in raw.items():
            item = BY_KEY.get(k)
            if item is None:
                continue                              # 모르는 키(예전 설정)는 무시
            try:
                self.values[k] = check(item, v)
            except ConfigError as e:
                if k == "cooler_grid":                # 못 쓰는 쿨러 배치는 _migrate 가 기본값으로 되돌리고 로그에 적는다
                    self.cooler_reset = True
                    continue
                self.loaded_notes.append("%s · 기본값 사용" % e)
        self._migrate(raw)
        return self

    def _migrate(self, raw):
        """예전 settings.json 을 지금 규칙으로 옮긴다(로그 한 줄씩 · migrated). 릴레이 배선 변경(송풍 · 열선 · 쿨러 3묶음) 때문이다.
          · heater_relay(히트건 릴레이 하나)는 버린다 - 지금은 송풍 · 열선 릴레이 둘이다.
          · 송풍 · 열선 · 쿨러 릴레이가 서로 겹치면 셋 다 기본값으로 - 겹친 채로는 쿨러 버튼이 열선을 켤 수 있다.
          · 쿨러 배치가 쿨러 릴레이 밖의 번호를 쓰면 기본 배치로 되돌리고 초기 설정 7 을 미완료로 한다(cooler_reset)."""
        v = self.values
        if "heater_relay" in raw:
            self.migrated.append("예전 설정 heater_relay R%s 버림 · 송풍 R%d · 열선 R%d 사용" % (raw["heater_relay"], v["heater_fan_relay"], v["heater_element_relay"]))
        fan, el, cool = v["heater_fan_relay"], v["heater_element_relay"], v["cooler_relays"]
        if fan == el or fan in cool or el in cool:
            for k in ROLE_KEYS:
                v[k] = copy.deepcopy(BY_KEY[k].default)
            self.migrated.append("릴레이 역할 겹침 · 송풍 R%d · 열선 R%d · 쿨러 %s 기본값 사용" % (
                v["heater_fan_relay"], v["heater_element_relay"], json.dumps(v["cooler_relays"])))
        cool = v["cooler_relays"]
        if self.cooler_reset or any(n not in cool for n in grid_relays(v["cooler_grid"])):      # 빈 칸은 그대로 둔다
            v["cooler_grid"] = default_grid(cool)
            self.cooler_reset = True
            self.migrated.append("쿨러 배치 %s · 쿨러 릴레이 %s 밖 번호 · 기본 배치 사용 · 초기 설정 7 확인 필요" % (
                json.dumps(raw.get("cooler_grid"), ensure_ascii=False), json.dumps(cool)))

    def save(self):
        """기본값과 다른 값만 원자적으로 쓴다(임시 파일 → 바꿔치기). 읽을 때 기본값 위에 덮으므로 바꾸지 않은 값은 코드 기본값을 따른다."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".settings.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.changed(), f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ---- 값 ----
    def get(self, key: str):
        return self.values[key]

    def __getitem__(self, key: str):
        return self.values[key]

    def checked(self, patch: dict) -> dict:
        """patch 의 값을 하나하나 검증해 돌려준다(형 변환 후). 아무것도 바꾸지 않는다. 못 쓰는 값이면 ConfigError."""
        new = {}
        for k, v in patch.items():
            item = BY_KEY.get(k)
            if item is None:
                raise ConfigError("알 수 없는 설정: %s" % k)
            new[k] = check(item, v)
        return new

    def update(self, patch: dict):
        """전부 검증을 통과해야 바뀐다(일부만 바뀌지 않는다). 조합 검사(problems)는 Controller.update_settings 가 한다."""
        self.values.update(self.checked(patch))

    def data_path(self) -> Path:
        p = Path(self.values["data_dir"])
        return p if p.is_absolute() else (ROOT / p).resolve()

    def describe(self):
        """화면용 설정 표."""
        return [{"key": i.key, "label": i.label, "unit": i.unit, "value": self.values[i.key], "default": i.default, "kind": i.kind, "group": i.group,
                 "verify": i.verify, "min": i.min, "max": i.max, "choices": [list(c) for c in i.choices],
                 "desc": i.desc, "hint": hint(i), "folder": i.folder, "pc": i.key in PC_KEYS, "device": i.key in DEVICE_KEYS} for i in ITEMS]
