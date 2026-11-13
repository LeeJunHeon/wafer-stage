"""config.py - settings.json 읽기 · 쓰기 · 기본값 · 검증.

설정은 ITEMS 표 한 곳에 모은다(이름 · 기본값 · 한국어 이름 · 단위 · 범위 · 설명). 코드 다른 곳에 숫자를 박지 않는다.
settings.json 은 장비마다 다른 로컬 파일이라 git 에 넣지 않는다. 없으면 기본값으로 시작한다.
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
    kind: str                       # str | int | float | bool | grid | xy(비어 있을 수 있는 [x, y] mm) | optfloat(비어 있을 수 있는 숫자) | choice
    label: str                      # 화면에 보일 한국어 이름(뜻이 바로 보이게)
    unit: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    group: str = ""                 # 설정 값 모두 보기의 묶음
    verify: bool = False            # 실장비 확인 필요(측정 PC 에서 확인하기 전의 값)
    choices: tuple = ()             # kind = choice 일 때 고를 수 있는 값 ((값, 이름), …)
    desc: str = ""                  # 설명(뜻 · 언제 바꾸는지) 1~2줄. 화면의 말풍선에 보인다. 단위 · 기본값 · 범위는 hint() 가 덧붙인다
    folder: bool = False            # 폴더 경로(화면에 [찾아보기] 버튼을 붙인다)


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
            desc="시작할 때 저장된 포트 · 주소로 연결\n장비 없이 화면만 볼 때 끔"))
    + _g("카메라",                                                                        # 실장비 기본값(v2 운용값): 장치 1 · 4080×3060 · MJPG · 노출 −5 · 화이트밸런스 5000 K · 초점 고정
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
         Item("camera_focus_value", -1, "int", "초점 값", "", -1, 1000, verify=True,
              desc="고정 초점 값 · −1 은 카메라 현재 값 유지\n사진이 흐릴 때 변경"),
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
              desc="프로그램 신호가 이 시간 동안 없으면 보드가 릴레이 차단\n보통 변경 안 함 · 스테이지 재연결 후 적용"))
    + _g("히트건 안전",
         Item("heater_relay", 1, "int", "히트건 릴레이", "번", 1, 10,
              desc="히트건이 연결된 릴레이 번호\n배선을 바꿨을 때만 변경"),
         Item("heater_max_c", 110.0, "float", "히트건 차단 온도", "°C", 30, 300,
              desc="이 온도 이상이면 히트건을 켜지 않고 켜져 있으면 끔\n가열 끝 온도보다 높게 설정"),
         Item("heater_max_on_s", 180.0, "float", "히트건 최대 켜짐 시간", "s", 1, 3600,
              desc="히트건을 한 번에 켜 두는 최대 시간 · 지나면 끔\n가열이 이 시간 안에 끝나지 않을 때 늘림"),
         Item("temp_stale_s", 3.0, "float", "온도 못 읽음 허용 시간", "s", 0.5, 60,
              desc="측정 중 온도를 이 시간 넘게 못 읽으면 히트건 끔\n수동으로 켠 히트건에는 적용 안 함"))
    + _g("쿨러",
         # 쿨러 3x3 자리 배치(판을 위에서 본 칸 → 릴레이 번호). 초기 설정 7 에서 정한다
         Item("cooler_grid", [[2, 3, 4], [5, 6, 7], [8, 9, 10]], "grid", "쿨러 배치", verify=True,
              desc="판을 위에서 본 3×3 칸마다 쿨러 릴레이 번호\n초기 설정 7 에서 설정"))
    + _g("판 · 핀",
         Item("plate_size_mm", 154.0, "float", "판 사각형 한 변", "mm", 10, 400, verify=True,
              desc="판에 새겨진 사각형의 한 변 길이 · 좌표의 기준\n판을 바꿨을 때 실측 값으로 변경"),
         Item("pin_long_mm", 10.0, "float", "핀 끝 긴 쪽", "mm", 0.1, 50,
              desc="핀 끝의 긴 쪽 길이 · 샘플이 핀보다 작은지 판정에 사용\n핀 교체 시 변경"),
         Item("pin_short_mm", 2.0, "float", "핀 끝 짧은 쪽", "mm", 0.1, 50,
              desc="핀 끝의 짧은 쪽 길이 · 샘플이 핀보다 작은지 판정에 사용\n핀 교체 시 변경"),
         Item("pin_long_axis", "y", "choice", "핀 긴 쪽 방향", verify=True, choices=(("x", "X 방향"), ("y", "Y 방향")),
              desc="핀 끝의 긴 쪽이 향하는 스테이지 방향\n핀 장착 방향을 바꿨을 때 변경"),
         Item("pin_margin_mm", 0.3, "float", "핀 여유", "mm", 0, 5,
              desc="핀 크기에 더하는 여유\n샘플이 핀 + 여유보다 작으면 \"핀보다 작음\" 으로 제외"))
    + _g("좌표 · 위치 확인",
         Item("coord_warn_mm", 2.0, "float", "좌표 주의 · 변 길이", "mm", 0.1, 50,
              desc="좌표 보정 품질 주의 기준 · 변 길이 오차\n주의가 뜨면 꼭짓점 재등록"),
         Item("coord_warn_deg", 1.0, "float", "좌표 주의 · 직각", "°", 0.1, 45,
              desc="좌표 보정 품질 주의 기준 · 직각 오차\n주의가 뜨면 꼭짓점 재등록"),
         Item("marker_probe_mm", 10.0, "float", "마커 기억 이동 거리", "mm", 1, 50,
              desc="헤드 마커 등록 때 헤드를 움직여 보는 거리\n보통 변경 안 함"),
         Item("pos_warn_mm", 2.0, "float", "헤드 위치 주의 거리", "mm", 0.1, 50,
              desc="촬영 때 헤드 마커가 등록 위치와 이만큼 다르면 주의"),
         Item("pos_block_mm", 10.0, "float", "헤드 위치 경보 거리", "mm", 0.5, 100,
              desc="헤드 마커가 등록 위치와 이만큼 다르면 경보 · 측정 시작 차단\n원점 재설정 또는 좌표 재보정으로 해제"))
    + _g("접촉 탐색",                                                                     # v2 에서 실장비로 쓰던 값 그대로
         Item("contact_z_mm", None, "optfloat", "접촉 높이", "mm", 0, 100,
              desc="핀이 샘플에 닿는 Z 높이\n초기 설정 6 [높이 저장] 으로 저장 · 샘플 두께가 바뀌면 재설정"),
         Item("contact_volt", 0.1, "float", "접촉 확인 전압", "V", 0.01, 5,
              desc="접촉을 확인할 때 거는 전압\n실장비에서 확인된 값 · 보통 변경 안 함"),
         Item("contact_ilim_a", 1e-4, "float", "접촉 확인 전류 한도", "A", 1e-8, 1.0,
              desc="접촉을 확인할 때 전류 한도 · 금속 판에 닿으면 이 한도에 걸림\n보통 변경 안 함"),
         Item("contact_nplc", 0.1, "float", "접촉 확인 NPLC", "", 0.01, 10,
              desc="접촉 확인 한 번의 적분 시간 · 작을수록 빠름\n보통 변경 안 함"),
         Item("contact_step_mm", 0.025, "float", "하강 간격", "mm", 0.00625, 1,
              desc="접촉을 확인하며 핀을 내리는 간격\n1 펄스는 0.00625 mm"),
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
         Item("contact_search_mm", 5.0, "float", "탐색 거리", "mm", 0.1, 20,
              desc="[접촉 탐색] 이 현재 높이부터 내려가는 최대 거리"),
         Item("contact_search_step_mm", 0.05, "float", "탐색 간격", "mm", 0.00625, 1,
              desc="[접촉 탐색] 에서 핀을 내리는 간격"),
         Item("contact_open_a", 1e-9, "float", "미접촉 판정 전류", "A", 1e-13, 1e-3,
              desc="전류가 이 값보다 작으면 미접촉으로 판정\n측정 중 핀 떨어짐 판정에도 사용"))
    + _g("측정 조건",
         Item("meter_volt", 1.0, "float", "측정 전압", "V", 0.01, 20,
              desc="저항을 잴 때 샘플에 거는 전압 · 파일 이름에 들어감\n자동 측정 [조건] 에서도 변경 가능"),
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
              desc="이 온도에 닿으면 히트건 끔\n히트건 차단 온도보다 낮게 설정"),
         Item("run_cool_end_c", 30.0, "float", "냉각 끝 온도", "°C", -20, 200,
              desc="이 온도까지 냉각하면 그 샘플 측정 완료"),
         Item("run_repeat", 1, "int", "반복", "회", 1, 20,
              desc="샘플마다 가열 · 냉각을 반복하는 횟수\n회차마다 파일 하나"),
         Item("run_interval_s", 1.0, "float", "측정 간격", "s", 0.05, 60,
              desc="온도와 저항을 읽는 간격"),
         Item("run_cooler_mode", "zone", "choice", "쿨러", choices=(("zone", "샘플 아래 구역"), ("all", "전체"), ("none", "사용 안 함")),
              desc="측정 때 켜는 쿨러\n샘플 아래 구역은 초기 설정 7 의 배치 사용"),
         Item("run_cooler_during_heat", True, "bool", "가열 중 쿨러 켬",
              desc="가열 중에도 쿨러를 켠 채로 둠\n끄면 가열하는 동안 쿨러 끔"),
         Item("batch_name", "1", "str", "묶음",
              desc="묶음 이름 · 기록 폴더와 샘플 기본 이름에 사용"))
    + _g("측정 한계",                                                                     # v2 backend/rt.py 에서 쓰던 값
         Item("run_no_rise_s", 45.0, "float", "가열 무반응 시간", "s", 1, 600,
              desc="히트건을 켜고 이 시간 동안 온도가 안 오르면 그 샘플 실패\n오른 정도는 가열 무반응 기준으로 판정"),
         Item("run_no_rise_c", 0.5, "float", "가열 무반응 기준", "°C", 0.05, 20,
              desc="가열 무반응 시간 동안 이만큼도 안 오르면 실패"),
         Item("run_fast_rise_c_s", 5.0, "float", "빠른 가열 주의", "°C/s", 0.5, 100,
              desc="1초에 이보다 빨리 오르면 주의 한 번 · 히트건은 끄지 않음"),
         Item("run_stall_s", 180.0, "float", "냉각 정체 시간", "s", 5, 3600,
              desc="냉각 중 이 시간 동안 온도가 안 내려가면 그 단계를 정상 종료\n내려간 정도는 냉각 정체 기준으로 판정"),
         Item("run_stall_c", 0.3, "float", "냉각 정체 기준", "°C", 0.05, 20,
              desc="냉각 정체 시간 동안 이만큼도 안 내려가면 정체로 판정"),
         Item("run_phase_limit_s", 1800.0, "float", "한 단계 시간 한도", "s", 10, 36000,
              desc="가열 · 냉각 한 단계가 이 시간을 넘으면 그 샘플 실패"),
         Item("run_open_n", 5, "int", "핀 떨어짐 판정 횟수", "번", 1, 100,
              desc="측정 중 전류가 미접촉 판정 전류보다 작은 일이 이 횟수 연속이면 주의"))
    + _g("저장",
         Item("nas_dir", "", "str", "NAS 폴더", folder=True,
              desc="측정 파일(.dat)을 복사할 NAS 폴더\n비우면 PC 에만 저장"),
         Item("nas_date_folder", True, "bool", "NAS 날짜 폴더",
              desc="NAS 폴더 아래에 연도 · 날짜 폴더를 만들어 저장\n끄면 NAS 폴더 바로 아래 묶음 폴더에 저장"))
)
BY_KEY = {i.key: i for i in ITEMS}


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
    if item.kind == "grid":
        ok = isinstance(value, list) and len(value) == 3 and all(isinstance(r, list) and len(r) == 3 for r in value)
        nums = [n for r in value for n in r] if ok else []
        if not ok or not all(isinstance(n, int) and not isinstance(n, bool) and 2 <= n <= 10 for n in nums) or len(set(nums)) != 9:
            raise ConfigError("%s: 3×3 칸에 릴레이 번호 2~10 중복 없이 입력 필요" % name)
        return [list(r) for r in value]
    if item.kind == "choice":
        if value not in [c[0] for c in item.choices]:
            raise ConfigError("%s: %s 중 하나 필요" % (name, " · ".join(c[1] for c in item.choices)))
        return value
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
    if item.kind in ("grid", "xy"):
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
    if item.min is not None and item.max is not None:
        parts.append("범위 %g~%g" % (item.min, item.max))
    return " · ".join(parts)


def defaults() -> dict:
    return {i.key: copy.deepcopy(i.default) for i in ITEMS}


class Config:
    def __init__(self, path: Optional[os.PathLike] = None):
        self.path = Path(path) if path else DEFAULT_PATH
        self.values = defaults()
        self.loaded_notes = []                       # 읽을 때 무시한 값(화면 · 로그용)

    # ---- 읽기 · 쓰기 ----
    def load(self):
        self.values = defaults()
        self.loaded_notes = []
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
        for k, v in raw.items():
            item = BY_KEY.get(k)
            if item is None:
                continue                              # 모르는 키(예전 설정)는 무시
            try:
                self.values[k] = check(item, v)
            except ConfigError as e:
                self.loaded_notes.append("%s · 기본값 사용" % e)
        return self

    def save(self):
        """원자적으로 쓴다(임시 파일 → 바꿔치기)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".settings.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.values, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ---- 값 ----
    def get(self, key: str):
        return self.values[key]

    def __getitem__(self, key: str):
        return self.values[key]

    def update(self, patch: dict):
        """전부 검증을 통과해야 바뀐다(일부만 바뀌지 않는다)."""
        new = {}
        for k, v in patch.items():
            item = BY_KEY.get(k)
            if item is None:
                raise ConfigError("알 수 없는 설정: %s" % k)
            new[k] = check(item, v)
        self.values.update(new)

    def data_path(self) -> Path:
        p = Path(self.values["data_dir"])
        return p if p.is_absolute() else (ROOT / p).resolve()

    def describe(self):
        """화면용 설정 표."""
        return [{"key": i.key, "label": i.label, "unit": i.unit, "value": self.values[i.key], "default": i.default, "kind": i.kind, "group": i.group,
                 "verify": i.verify, "min": i.min, "max": i.max, "choices": [list(c) for c in i.choices],
                 "desc": i.desc, "hint": hint(i), "folder": i.folder} for i in ITEMS]
