"""settings_schema.py - 운용 설정의 단일 출처(기본값 · 타입 · 범위 · 화면 배치 · 검증).

원칙: 운용 값은 여기에만 기본값을 둔다. settings.json 이 덮어쓰고, 설정 화면은
이 스키마로 자동 생성된다. 코드 다른 곳에 운용 숫자를 박지 않는다.
(검출 파라미터는 예외 - detect.DEFAULTS 가 기본값이고 settings.json 을 직접
편집한다. 화면에 있던 stat_seed · stat_grow 만 여기로 옮겼다.)

키는 점 표기다. "relay.items" = settings["relay"]["items"].

항목 필드
  key, default, type(int|float|bool|str|enum|xy|list|object), min, max, step,
  unit, tab, group, label(명사형 짧게), tip(…합니다 체 1~2문장),
  apply("즉시"|"다음 촬영"|"재연결"|"다음 측정"), advanced(bool),
  validate(교차 검증 함수 이름), options(enum), editor(전용 편집기),
  hidden(화면에 안 보임 - 다른 값에서 유도), pick(xy: 현재 위치 넣기),
  nullable(None 허용), item(list 원소 타입), warn(바꾸면 경고 대화상자)

순수 모듈이다(state·장비를 모른다). state 가 이것으로 기본값을 만들고, 저장 전
검증(validate)과 예전 파일 이행(migrate)을 한다.
"""

import copy
import math

import conditions
from core import fixed_detect as fd
from core import zones as zones_mod

TABS = [
    ("camera", "카메라"),
    ("stage", "스테이지"),
    ("position", "위치"),
    ("photohome", "위치 확인"),
    ("tool", "공구"),
    ("relay", "릴레이"),
    ("measure", "릴레이 · 접촉 높이"),
    ("rt", "R–T 측정"),
    ("condition", "측정 순서"),
    ("detect", "검출"),
    ("advanced", "고급"),
]
APPLY = ("즉시", "다음 촬영", "재연결", "다음 측정")

PLATE_MARKER_DEFAULT = {"0": [201, 160], "1": [201, 19], "2": [15, 160], "3": [15, 20]}
TOOL_POINTER = "기준 포인터"
TOOL_HEATGUN = "히트건"
N_RELAY = 10
POLICY_OPTS = [{"value": "off", "label": "전부 끄기"}, {"value": "keep", "label": "그대로"}]


def _relays_default():
    """쿨러 R1~R9 · 히트건 R10. 켜기는 사람이 누르거나 측정 조건이 할 때만이다."""
    return [{"enabled": True, "name": ("히트건" if i == N_RELAY - 1 else "쿨러 %d" % (i + 1)), "max_on_s": 0}
            for i in range(N_RELAY)]


def _tools_default(z_work=44.0):
    return [
        {"name": TOOL_POINTER, "dx_mm": 0.0, "dy_mm": 0.0, "z_work_mm": 0.0,
         "tip_shape": "rect", "tip_w_mm": 10.0, "tip_h_mm": 2.0, "tip_angle_deg": 90.0,
         "tip_angle_img_deg": 90.0, "note": "스테이지 좌표가 가리키는 점"},
        {"name": TOOL_HEATGUN, "dx_mm": 0.0, "dy_mm": 0.0, "z_work_mm": float(z_work),
         "tip_shape": "circle", "tip_w_mm": 5.0, "tip_h_mm": 5.0, "tip_angle_deg": 90.0,
         "tip_angle_img_deg": 0.0, "note": "오프셋 0 유지"},
    ]


def I(key, default, type_, tab, group, label, tip, **kw):   # noqa: E743 - 표처럼 읽히게
    d = {"key": key, "default": default, "type": type_, "tab": tab, "group": group,
         "label": label, "tip": tip, "apply": kw.pop("apply", "즉시"),
         "advanced": kw.pop("advanced", False)}
    d.update(kw)
    return d


ITEMS = [
    # ---------------- 카메라 ----------------
    I("camera_index", 1, "int", "camera", "장치", "카메라 번호",
      "카메라 장치 번호입니다. 0번은 노트북 내장 카메라입니다.",
      min=0, max=9, step=1, apply="재연결"),
    I("width", 4080, "int", "camera", "장치", "가로 해상도",
      "카메라에 요청할 가로 해상도입니다. 4080×3060 이 4:3 전체 센서 화각입니다. 올리면 촬영과 검출이 느려집니다.",
      min=160, max=8000, step=1, unit="px", apply="재연결"),
    I("height", 3060, "int", "camera", "장치", "세로 해상도",
      "카메라에 요청할 세로 해상도입니다. 실제로 열린 값은 로그에 남습니다.",
      min=120, max=6000, step=1, unit="px", apply="재연결",
      action={"label": "최대 해상도로", "set": {"width": 4080, "height": 3060, "fourcc": "MJPG"},
              "tip": "4080×3060 · MJPG 로 맞춥니다. 4:3 전체 센서 화각이고 8160×6120 은 보간입니다. 저장해야 적용됩니다."}),
    I("fourcc", "MJPG", "enum", "camera", "장치", "영상 포맷",
      "카메라에 요청할 영상 포맷입니다. YUY2 는 무압축이라 고해상도가 되지 않습니다.",
      options=[{"value": "MJPG", "label": "MJPG"}, {"value": "YUY2", "label": "YUY2"}],
      apply="재연결"),
    I("wb_temperature", 5000, "int", "camera", "장치", "화이트밸런스",
      "색온도입니다. 운용 기본은 5000 K 고정입니다(미고정이면 색이 초록·노랑으로 흔들립니다). 0 이면 카메라 기본값을 씁니다.",
      min=0, max=10000, step=100, unit="K", apply="재연결",
      action={"label": "운용 기본값으로",
              "set": {"width": 4080, "height": 3060, "fourcc": "MJPG", "exposure": -5, "wb_temperature": 5000,
                      "bracket": False, "focus_mode": "fixed"},
              "tip": "천장 고정 카메라 운용값(4080×3060 · MJPG · 노출 -5 고정 · 화이트밸런스 5000 K · 브라케팅 끔 · 초점 고정)으로 한 번에 맞춥니다. 저장해야 적용됩니다."}),
    I("exposure", -5, "float", "camera", "장치", "노출",
      "카메라 노출값입니다. 운용 기본은 -5 고정입니다(어두운 노출일수록 회색 칩이 잘 보입니다). 0 이면 처음 한 번 자동 노출로 맞춘 값을 세션 동안 고정해 씁니다.",
      min=-20, max=10000, step=1, apply="재연결",
      action={"label": "노출 다시 찾기", "cmd": "exposure_refind",
              "tip": "저장해 둔 노출을 버리고 다음 촬영에서 자동 노출로 다시 맞춥니다. 조명이 바뀌었을 때 누릅니다."}),
    I("focus_mode", "fixed", "enum", "camera", "초점", "초점 모드",
      "자동은 카메라 자동 초점을 켭니다. 고정은 고정 초점값(또는 스윕)으로 맞춥니다.",
      options=[{"value": "auto", "label": "자동"}, {"value": "fixed", "label": "고정"}],
      apply="재연결"),
    I("focus", -1, "int", "camera", "초점", "고정 초점값",
      "고정 모드에서 쓰는 초점값입니다. -1 이면 카메라 값을 건드리지 않습니다.",
      min=-1, max=255, step=1, apply="재연결"),
    I("focus_sweep", True, "bool", "camera", "초점", "초점 스윕",
      "고정 모드에서 카메라를 열 때 초점을 훑어 가장 선명한 값으로 고정합니다.",
      apply="재연결"),
    I("af_settle_s", 5.0, "float", "camera", "초점", "자동 초점 대기 최대",
      "촬영 전 자동 초점이 수렴하기를 기다리는 최대 시간입니다.",
      min=0, max=30, step=0.5, unit="s", apply="다음 촬영", advanced=True),
    I("af_min_wait_s", 0.5, "float", "camera", "초점", "자동 초점 최소 대기",
      "자동 초점이 반응을 시작할 시간으로 최소한 이만큼 기다립니다.",
      min=0, max=10, step=0.1, unit="s", apply="다음 촬영", advanced=True),
    I("focus_step", 15, "int", "camera", "초점", "스윕 간격",
      "초점 스윕의 굵은 간격입니다. 굵게 훑은 뒤 1/4 간격으로 다시 훑습니다.",
      min=1, max=64, step=1, apply="재연결", advanced=True),
    I("focus_settle_s", 0.35, "float", "camera", "초점", "스윕 안정 대기",
      "초점값을 바꾼 뒤 렌즈가 멈추기를 기다리는 시간입니다.",
      min=0, max=3, step=0.05, unit="s", apply="재연결", advanced=True),
    I("capture_frames", 3, "int", "camera", "촬영", "촬영 프레임 수",
      "한 장을 찍을 때 연속으로 받아 가장 선명한 장을 고르는 프레임 수입니다. 고해상도는 프레임당 시간이 길어 적게 잡습니다.",
      min=1, max=60, step=1, apply="다음 촬영"),
    I("capture_retries", 2, "int", "camera", "촬영", "촬영 재시도",
      "촬영 중 프레임이 끊기면 카메라를 다시 열고 이만큼 다시 찍습니다.",
      min=0, max=10, step=1, apply="다음 촬영"),
    I("preview_fps", 4.0, "float", "camera", "실시간 · 복구", "실시간 주기",
      "실시간 그림을 새로 만드는 빈도입니다.",
      min=0.5, max=15, step=0.5, unit="fps"),
    I("preview_max_w", 1280, "int", "camera", "실시간 · 복구", "실시간 최대 가로",
      "실시간 그림을 이 가로 폭 이하로 줄여 보냅니다. 촬영과 검출은 원본 해상도로 합니다.",
      min=320, max=4096, step=10, unit="px"),
    I("cam_first_frame_s", 5.0, "float", "camera", "실시간 · 복구", "첫 프레임 대기",
      "카메라를 연 뒤 첫 프레임을 기다리는 최대 시간입니다. 4백만 화소를 넘으면 두 배로 기다립니다.",
      min=1, max=60, step=0.5, unit="s", advanced=True),
    I("cam_fail_limit", 3, "int", "camera", "실시간 · 복구", "연속 실패 허용",
      "프레임을 이만큼 연속으로 못 받으면 카메라를 닫고 다시 엽니다.",
      min=1, max=30, step=1),
    I("cam_reopen_s", 5.0, "float", "camera", "실시간 · 복구", "재개방 대기",
      "카메라를 닫은 뒤 다시 열기까지 기다리는 시간입니다. 실패가 이어지면 두 배씩 늘립니다.",
      min=0.5, max=120, step=0.5, unit="s"),
    I("cam_reopen_max_s", 30.0, "float", "camera", "실시간 · 복구", "재개방 대기 최대",
      "재개방 대기가 늘어날 수 있는 최대 시간입니다.",
      min=1, max=600, step=1, unit="s", advanced=True),
    # 유도 값(화면에 없음): focus_mode / exposure 에서 만든다

    # ---------------- 스테이지 ----------------
    I("serial_port", None, "str", "stage", "연결", "시리얼 포트",
      "스테이지 아두이노의 포트입니다. 비우면 하나뿐일 때 자동으로 고릅니다.",
      nullable=True, editor="port", apply="재연결"),
    I("motion.speed_pps", 6000, "int", "stage", "속도", "이동 속도",
      "절대 이동의 최고 속도입니다. 160 pps = 1 mm/s.",
      min=100, max=20000, step=100, unit="pps"),
    I("motion.accel", 40000, "int", "stage", "속도", "가속도",
      "가감속 기울기입니다. 연결 직후와 저장 때 펌웨어로 보냅니다.",
      min=1000, max=200000, step=1000, unit="pps²"),
    I("motion.start_pps", 300, "int", "stage", "속도", "시작 속도",
      "가속을 시작하는 속도입니다. 연결 직후와 저장 때 펌웨어로 보냅니다.",
      min=50, max=5000, step=50, unit="pps"),
    I("motion.jog_pps", 6000, "int", "stage", "속도", "조그 속도",
      "원점이 있을 때 수동 이동의 속도입니다.",
      min=100, max=20000, step=100, unit="pps"),
    I("motion.jog_slow_pps", 1500, "int", "stage", "속도", "원점 없을 때 조그 속도",
      "원점이 없을 때(상대 이동) 속도입니다. 끝단에 닿아도 살살 닿게 느리게 둡니다.",
      min=100, max=10000, step=100, unit="pps"),
    I("jog.steps", [0.1, 1, 10, 50], "list", "stage", "조그 · 끝단", "조그 스텝",
      "수동 이동 패드의 스텝 선택지입니다. 쉼표로 구분합니다.",
      item="float", validate="jog_steps"),
    I("jog.max_mm", 50.0, "float", "stage", "조그 · 끝단", "조그 한 번 한도",
      "조그 한 번에 움직일 수 있는 최대 거리입니다. 넘으면 거절합니다.",
      min=0.1, max=1000, step=0.1, unit="mm"),
    I("touch.default_mm", 5.0, "float", "stage", "조그 · 끝단", "끝단 이동 기본 거리",
      "끝단 이동 칸의 처음 값입니다.",
      min=1, max=1000, step=1, unit="mm"),
    I("touch.max_mm", 0.0, "float", "stage", "조그 · 끝단", "끝단 이동 최대 거리",
      "끝단 이동 한 번의 최대 거리입니다. 0 이면 그 축의 가동범위입니다.",
      min=0, max=1000, step=1, unit="mm", validate="touch_max"),
    I("limits.x_max_mm", 247.6, "float", "stage", "가동범위", "X 가동범위",
      "X 축이 0 에서 갈 수 있는 최대 거리입니다. 펌웨어 한계 안에서 정합니다.",
      min=1, max=1000, step=0.1, unit="mm"),
    I("limits.y_max_mm", 247.8, "float", "stage", "가동범위", "Y 가동범위",
      "Y 축이 0 에서 갈 수 있는 최대 거리입니다. 펌웨어 한계 안에서 정합니다.",
      min=1, max=1000, step=0.1, unit="mm"),
    I("limits.z_max_mm", 45.0, "float", "stage", "가동범위", "Z 가동범위",
      "Z 축이 맨 위(0)에서 내려갈 수 있는 최대 거리입니다.",
      min=1, max=1000, step=0.1, unit="mm"),
    I("autosave_s", 3.0, "float", "stage", "저장 · 통신", "자동 위치 저장 대기",
      "이동이 끝나고 이만큼 조용하면 위치를 펌웨어에 저장합니다.",
      min=0.5, max=60, step=0.5, unit="s"),
    I("status_poll_s", 2.0, "float", "stage", "저장 · 통신", "상태 조회 주기",
      "이동 중이 아닐 때 위치·원점 상태를 묻는 주기입니다.",
      min=0.5, max=30, step=0.5, unit="s", advanced=True),
    I("serial.line_timeout_s", 5.0, "float", "stage", "저장 · 통신", "응답 대기",
      "한 줄 응답(상태·저장·릴레이)을 기다리는 시간입니다.",
      min=1, max=60, step=0.5, unit="s", apply="재연결", advanced=True),
    I("serial.move_timeout_s", 30.0, "float", "stage", "저장 · 통신", "이동 응답 대기",
      "이동 완료 보고를 기다리는 최대 시간입니다. 가장 긴 이동보다 길어야 합니다.",
      min=5, max=600, step=1, unit="s", apply="재연결", advanced=True),
    I("serial.boot_wait_s", 5.0, "float", "stage", "저장 · 통신", "부팅 배너 대기",
      "포트를 열면 보드가 리셋됩니다. 부팅 배너를 기다리는 최대 시간입니다.",
      min=1, max=30, step=0.5, unit="s", apply="재연결", advanced=True),

    # ---------------- 위치 ----------------
    I("park_xy", [0.0, 90.0], "xy", "position", "자리", "대기 위치 위치",
      "측정가 끝나고 쉬는 자리입니다.", pick=True, validate="xy_in_limits", unit="mm"),
    I("capture_xy", [0.0, 90.0], "xy", "position", "자리", "촬영 위치",
      "촬영할 때 스테이지가 가는 자리입니다. 히트건이 웨이퍼를 가리지 않는 곳으로 둡니다.",
      pick=True, validate="xy_in_limits", unit="mm", apply="다음 촬영"),
    I("after_run", "park", "enum", "position", "자리", "측정 후 갈 곳",
      "측정가 끝나거나 정지했을 때 Z 를 올린 뒤 가는 곳입니다.",
      options=[{"value": "park", "label": "대기 위치 위치"},
               {"value": "capture", "label": "촬영 위치"},
               {"value": "origin", "label": "원점"},
               {"value": "stay", "label": "그 자리"}], apply="다음 측정"),
    I("flow.capture_lift_z", True, "bool", "position", "촬영 흐름", "촬영 전 Z 올리기",
      "촬영 위치로 가기 전에 Z 를 맨 위로 올립니다.", apply="다음 촬영"),
    I("flow.capture_goto", True, "bool", "position", "촬영 흐름", "촬영 위치로 이동",
      "촬영 전에 촬영 위치로 갑니다. 끄면 지금 자리에서 찍습니다.", apply="다음 촬영"),
    I("flow.require_fresh_capture", False, "bool", "position", "촬영 흐름",
      "측정 전 새 촬영 요구", "한 번 측정한 촬영으로는 다시 측정하지 않고 새 촬영을 요구합니다.",
      apply="다음 측정"),
    I("flow.after_photo", False, "bool", "position", "촬영 흐름", "측정 후 확인 촬영",
      "측정가 끝나면 촬영 위치로 가서 한 장 더 찍어 같은 폴더에 남깁니다.",
      apply="다음 측정"),

    # ---------------- 위치 확인 ----------------
    I("photohome.marker_id", 10, "int", "photohome", "헤드 마커", "헤드 마커 번호",
      "Z 모터 윗면에 붙인 ArUco 마커 번호입니다. 판 마커 번호와 겹치면 안 됩니다.",
      min=0, max=49, step=1, validate="head_id"),
    I("photohome.marker_mm", 40.0, "float", "photohome", "헤드 마커", "헤드 마커 크기",
      "헤드 마커 한 변의 실제 크기입니다.", min=5, max=200, step=1, unit="mm"),
    I("photohome.dict", "4X4_50", "enum", "photohome", "헤드 마커", "마커 사전",
      "헤드 마커의 ArUco 사전입니다. 판 마커와 같은 4X4_50 을 씁니다.",
      options=[{"value": v, "label": v} for v in ("4X4_50", "4X4_100", "5X5_50", "6X6_50")]),
    I("photohome.tol_mm", 0.3, "float", "photohome", "판정", "허용 오차",
      "기록된 위치와 이 안으로 맞으면 그대로 둡니다.",
      min=0.01, max=10, step=0.05, unit="mm", validate="tol_warn"),
    I("photohome.warn_mm", 5.0, "float", "photohome", "판정", "경고 오차",
      "허용 오차와 이 값 사이면 자동으로 바로잡고, 넘으면 확인을 받습니다.",
      min=0.1, max=100, step=0.5, unit="mm"),
    I("photohome.on_connect", True, "bool", "photohome", "자동 확인", "연결 직후 위치 판단",
      "스테이지를 연결하면 이동 없이 한 장 찍어 헤드 마커로 위치를 판단합니다. 헤드 위치 보정이 유효할 때만 합니다.",
      apply="재연결"),
    I("photohome.edge_margin_px", 0, "int", "photohome", "판정", "화면 끝 여백",
      "헤드 마커가 화면 가장자리에서 이만큼 안쪽에 걸치면 결과를 쓰지 않습니다. 0 이면 프레임 짧은 변의 3% 입니다.",
      min=0, max=1000, step=1, unit="px"),
    I("photohome.after_move", False, "bool", "photohome", "자동 확인", "샘플 이동 후 재확인",
      "측정 중 샘플 위에 도착할 때마다 헤드 마커로 위치를 확인합니다.", apply="다음 측정"),
    I("photohome.after_move_fail", "stop", "enum", "photohome", "자동 확인", "재확인 실패 시",
      "한 번 바로잡아도 허용 오차를 넘을 때의 동작입니다.",
      options=[{"value": "stop", "label": "정지"}, {"value": "continue", "label": "경고 후 계속"}],
      apply="다음 측정"),
    I("photohome.grid_n", 3, "int", "photohome", "보정", "보정 격자",
      "헤드 마커 보정 때 X·Y 로 나누는 점 수입니다(n×n).", min=2, max=7, step=1),
    I("photohome.grid_margin_mm", 20.0, "float", "photohome", "보정", "격자 여백",
      "가동범위 가장자리에서 이만큼 안쪽에 격자를 둡니다.", min=0, max=100, step=1, unit="mm"),
    I("photohome.rms_limit_mm", 0.5, "float", "photohome", "보정", "보정 잔차 한계",
      "보정 잔차 RMS 가 이 값을 넘으면 경고하고 다시 하기를 권합니다.",
      min=0.05, max=10, step=0.05, unit="mm"),
    I("photohome.settle_s", 0.5, "float", "photohome", "보정", "안정 대기",
      "격자 점에 도착한 뒤 찍기 전에 기다리는 시간입니다.",
      min=0, max=10, step=0.1, unit="s", advanced=True),
    I("photohome.shot_retries", 3, "int", "photohome", "보정", "마커 촬영 재시도",
      "헤드 마커를 못 찾거나 프레임이 끊기면 이만큼 다시 찍습니다.",
      min=0, max=10, step=1, advanced=True),

    # ---------------- 공구 ----------------
    I("tool.items", _tools_default(), "list", "tool", "공구 목록", "공구",
      "공구마다 오프셋(기준 포인터 대비) · 작업 높이 · 끝 모양입니다. 끝 모양은 촬영 사진에 실제 크기로 그려지고 샘플 윤곽 안에 들어가는지 검사합니다(각도는 스테이지 기준: 0 = 긴 변이 스테이지 X 축, 90 = Y 축 — 카메라 방향이 바뀌어도 실물 방향이 그대로입니다. 좌표 보정이 없을 때만 사진 기준 예비값 tip_angle_img_deg 를 씁니다). 기준 포인터는 지울 수 없습니다.",
      editor="tools", validate="tools", apply="다음 측정"),
    I("tool.fit_margin_mm", 0.3, "float", "tool", "공구 목록", "끝 모양 맞음 여유",
      "공구 끝 모양이 샘플 윤곽을 이만큼까지 벗어나는 것은 봐줍니다. 더 벗어나면 샘플에 핀 벗어남을 표시하고 측정 전에 확인을 받습니다.",
      min=0, max=5, step=0.1, unit="mm", apply="즉시"),
    I("tool.active", TOOL_POINTER, "str", "tool", "공구 목록", "활성 공구",
      "측정와 샘플 이동이 오프셋을 적용할 공구입니다.",
      editor="tool_active", validate="tool_active", apply="다음 측정"),

    # ---------------- 릴레이 ----------------
    I("relay.items", _relays_default(), "list", "relay", "릴레이", "릴레이",
      "릴레이마다 사용 여부 · 이름 · 최대 켜짐 시간입니다. 사용하지 않는 릴레이는 켤 수 없습니다.",
      editor="relays", validate="relays"),
    I("relay.on_estop", "off", "enum", "relay", "정책", "비상정지 시",
      "비상정지 때 켜진 릴레이를 어떻게 할지 정합니다.", options=POLICY_OPTS),
    I("relay.on_stop", "off", "enum", "relay", "정책", "정지 · 측정 종료 시",
      "측정가 정지하거나 끝났을 때의 처리입니다.", options=POLICY_OPTS),
    I("relay.on_error", "off", "enum", "relay", "정책", "오류 시",
      "측정가 오류로 멈췄을 때의 처리입니다.", options=POLICY_OPTS),
    I("relay.on_exit", "off", "enum", "relay", "정책", "종료 · 연결 해제 시",
      "프로그램 종료나 스테이지 연결 해제 때의 처리입니다.", options=POLICY_OPTS),
    I("relay.watchdog_ms", 5000, "int", "relay", "통신 감시", "통신 감시 시간",
      "펌웨어가 이 시간 동안 명령을 못 받으면 릴레이를 전부 끕니다. 0 이면 끕니다.",
      min=0, max=600000, step=500, unit="ms"),

    # ---------------- 릴레이 · 접촉 높이 (쿨러 · 히트건) ----------------
    I("cooler.zones", zones_mod.default_zones(), "object", "measure", "쿨러 구역",
      "쿨러 구역", "판 사각형 좌표(mm, 0~판 영역 실제 크기)를 행 × 열로 나눈 칸마다 쿨러 번호(표시용) · 릴레이 번호 · 이름입니다(행 우선, 사진 기준 위에서 아래 · 왼쪽에서 오른쪽). 샘플 중심이 속한 칸의 쿨러가 그 샘플의 쿨러입니다. 칸을 눌러 지정하고 경계는 mm 로 입력하거나 실시간에서 끕니다.",
      editor="zones", validate="zones", apply="즉시"),
    I("heater.relay", 10, "int", "measure", "히트건", "히트건 릴레이",
      "히트건 전원 릴레이 번호입니다. 이 릴레이는 측정 조건의 히트건 단계만 켭니다.", min=1, max=10, step=1,
      apply="다음 측정"),
    I("heater.max_on_s", 120.0, "float", "measure", "히트건", "히트건 최대 시간",
      "히트건 단계 한 번의 시간 상한입니다. 측정 조건의 시간은 이 값을 넘을 수 없고, 넘기면 시간이 되기 전에 끕니다.",
      min=1, max=3600, step=5, unit="s", apply="다음 측정"),
    I("contact_z_mm", None, "float", "measure", "접촉 높이", "접촉 높이",
      "Z 원점에서 핀이 샘플에 닿는 높이(절대 Z, mm)입니다. 장비 설정 7 에서 샘플 위에서 내려 정합니다. 비어 있으면 접촉 높이 단계가 있는 측정 조건은 시작하지 않습니다.",
      nullable=True, min=0.1, max=100, step=0.05, unit="mm", apply="즉시", validate="contact_z"),
    I("contact_tool", None, "str", "measure", "접촉 높이", "접촉 높이를 잰 공구",
      "접촉 높이를 저장할 때의 활성 공구 이름입니다. 다른 공구가 활성이면 접촉 높이로 내리지 않습니다.", nullable=True, hidden=True),
    I("approach_mm", 2.0, "float", "measure", "접촉 높이", "느린 구간",
      "접촉 높이 이만큼 앞까지는 보통 속도로, 남은 거리는 느린 속도로 내립니다.",
      min=0.2, max=10, step=0.1, unit="mm", apply="즉시"),
    I("sim.time_scale", 1.0, "float", "measure", "시뮬레이터(테스트용)", "시간 배율",
      "대기 · 히트건 단계의 시간을 이 배율로 빠르게 합니다(시뮬레이션에서만, 검증용. 실장비에서는 1).",
      min=0.1, max=1000, step=0.5, apply="즉시", advanced=True),
    # ---------------- R–T 측정 (저항–온도) ----------------
    I("rt.k2110_addr", "USB0::0x05E6::0x2110::INSTR", "str", "rt", "측정 장비", "K2110 주소",
      "온도(열전대)를 재는 Keithley 2110 의 VISA 주소입니다. 장비 설정 8 의 목록에서 고릅니다.", apply="다음 측정"),
    I("rt.k2450_addr", "GPIB0::18::INSTR", "str", "rt", "측정 장비", "K2450 주소",
      "전압을 걸고 전류를 재는 Keithley 2450 의 VISA 주소입니다.", apply="다음 측정"),
    I("rt.precool_c", 24.5, "float", "rt", "온도", "예비 냉각 목표", "측정 전에 이 온도 미만이 될 때까지 쿨러로 식힙니다.",
      min=-20, max=200, step=0.5, unit="°C", apply="다음 측정"),
    I("rt.hold_s", 0.0, "float", "rt", "온도", "도달 뒤 유지", "예비 냉각 목표 미만에 닿은 뒤 이 시간만큼 더 유지합니다.",
      min=0, max=3600, step=1, unit="s", apply="다음 측정"),
    I("rt.max_temp", 85.0, "float", "rt", "온도", "최고 온도", "가열이 이 온도를 넘으면 냉각으로 바꿉니다.",
      min=30, max=300, step=1, unit="°C", apply="다음 측정", validate="rt"),
    I("rt.break_temp", 25.0, "float", "rt", "온도", "냉각 끝 온도", "냉각 중 이 온도 미만이 되면 그 회차를 끝냅니다.",
      min=-20, max=200, step=0.5, unit="°C", apply="다음 측정", validate="rt"),
    I("rt.repeat", 3, "int", "rt", "온도", "반복 횟수", "가열과 냉각을 한 회로 세는 반복 횟수입니다.",
      min=1, max=50, step=1, apply="다음 측정"),
    I("rt.heat_rate", 0.0, "float", "rt", "온도", "가열 속도", "0 은 최대입니다. 값이 있으면 목표 온도선을 이 속도로 올리고 히트건을 켜고 끄며 따라갑니다.",
      min=0, max=600, step=0.5, unit="°C/분", apply="다음 측정"),
    I("rt.cool_rate", 0.0, "float", "rt", "온도", "냉각 속도", "0 은 최대입니다. 값이 있으면 선보다 빨리 식을 때만 히트건을 잠깐씩 켜서 늦춥니다.",
      min=0, max=600, step=0.5, unit="°C/분", apply="다음 측정"),
    I("rt.band_c", 0.5, "float", "rt", "온도 제어", "띠", "실제 온도가 목표선보다 이만큼 낮으면 켜고 높으면 끕니다.",
      min=0.05, max=20, step=0.05, unit="°C", apply="다음 측정"),
    I("rt.min_on_s", 2.0, "float", "rt", "온도 제어", "최소 켜짐 시간", "히트건을 한 번 켜면 최소 이 시간은 켜 둡니다.",
      min=0, max=60, step=0.5, unit="s", apply="다음 측정"),
    I("rt.min_off_s", 2.0, "float", "rt", "온도 제어", "최소 꺼짐 시간", "히트건을 한 번 끄면 최소 이 시간은 꺼 둡니다.",
      min=0, max=60, step=0.5, unit="s", apply="다음 측정"),
    I("rt.emergency_c", None, "float", "rt", "안전", "비상 차단 온도", "이 온도 이상이면 히트건을 즉시 끄고 측정을 오류로 끝냅니다. 비우면 최고 온도 + 10 °C 입니다.",
      nullable=True, min=30, max=400, step=1, unit="°C", apply="다음 측정", validate="rt"),
    I("rt.phase_limit_s", 1800.0, "float", "rt", "안전", "단계 시간 한도", "예비 냉각 · 가열 · 냉각 한 단계가 이 시간을 넘으면 오류로 끝냅니다.",
      min=10, max=86400, step=60, unit="s", apply="다음 측정"),
    I("rt.min_interval_s", 0.0, "float", "rt", "기록", "최소 기록 간격", "0 이면 장비가 읽는 속도대로 기록합니다.",
      min=0, max=60, step=0.1, unit="s", apply="다음 측정"),
    I("rt.stale_s", 3.0, "float", "rt", "안전", "온도 갱신 한도", "온도 값이 없거나 이 시간 동안 새 값이 없으면 히트건을 끄고 오류로 끝냅니다.",
      min=1, max=60, step=0.5, unit="s", apply="다음 측정"),
    I("rt.volt", 1.0, "float", "rt", "K2450", "Set voltage", "시료에 거는 전압입니다. 저항은 이 값을 전류로 나눠 구합니다.",
      min=0.001, max=200, step=0.1, unit="V", apply="다음 측정"),
    I("rt.ilim_a", 0.01, "float", "rt", "K2450", "전류 한도", "전압 소스의 전류 한도입니다. 기존 프로그램에는 없던 값이라 기본 10 mA 로 둡니다.",
      min=1e-6, max=1.0, step=0.001, unit="A", apply="다음 측정"),
    I("rt.four_wire", True, "bool", "rt", "K2450", "4단자", "원격 감지(4단자)를 켭니다.", apply="다음 측정"),
    I("rt.terminal", "FRONT", "enum", "rt", "K2450", "단자", "앞면 또는 뒷면 단자입니다.",
      options=[{"value": "FRONT", "label": "앞"}, {"value": "REAR", "label": "뒤"}], apply="다음 측정"),
    I("rt.filter_n", 10, "int", "rt", "K2450", "필터 개수", "반복 방식 평균 필터의 읽기 개수입니다.", min=1, max=100, step=1, apply="다음 측정"),
    I("rt.nplc", 1.0, "float", "rt", "K2450", "적분", "전류 측정 적분 시간(전원 주기 수)입니다.", min=0.01, max=10, step=0.01, unit="NPLC", apply="다음 측정"),
    I("rt.range_mode", "step", "enum", "rt", "K2450", "범위 방식",
      "단계는 기존 프로그램처럼 전류가 범위 끝 근처일 때 10배씩 올리고 내립니다. 자동은 장비 자동 범위입니다.",
      options=[{"value": "step", "label": "단계"}, {"value": "auto", "label": "장비 자동"}], apply="다음 측정"),
    I("rt.start_range", 0.001, "float", "rt", "K2450", "시작 범위", "전류 측정 범위의 시작값입니다.", min=1e-8, max=1.0, step=0.001, unit="A", apply="다음 측정", validate="rt"),
    I("rt.tc_type", "K", "enum", "rt", "열전대", "종류", "열전대 종류입니다. 기준점은 장비 내부입니다.",
      options=[{"value": v, "label": v} for v in ("K", "J", "T", "E", "N", "R", "S", "B")], apply="다음 측정"),
    I("rt.cooler_target", "sample", "enum", "rt", "쿨러", "쿨러", "샘플 구역은 지금 위치로 구역을 계산합니다. 직접은 아래 릴레이 번호를 씁니다.",
      options=[{"value": "sample", "label": "샘플 구역"}, {"value": "all", "label": "전부"}, {"value": "custom", "label": "직접"}], apply="다음 측정"),
    I("rt.cooler_custom", "", "str", "rt", "쿨러", "직접 선택", "쉼표로 나눈 릴레이 번호입니다(예 1,2,5). 쿨러 대상이 직접일 때만 씁니다.", apply="다음 측정", validate="rt"),
    I("rt.file_path", "\\\\VanaM_NAS\\VanaM_VO2\\VO2 data", "str", "rt", "저장", "File path", "측정 파일을 한 번 더 복사할 NAS 폴더입니다. 못 쓰면 경고만 하고 측정은 계속합니다. 비우면 PC 에만 저장합니다.", apply="다음 측정"),
    I("rt.file_name", "1-1_", "str", "rt", "저장", "File name", "파일 이름의 앞부분입니다. 뒤에 Set voltage · 회차가 붙습니다.", apply="다음 측정", validate="rt"),
    I("rt.cooler_off_end", True, "bool", "rt", "끝", "끝나면 쿨러 끔", "측정이 끝나면 쿨러를 끕니다.", apply="다음 측정"),
    I("rt.z_top_end", True, "bool", "rt", "끝", "끝나면 Z 맨 위", "측정이 끝나면 Z 를 맨 위로 올립니다.", apply="다음 측정"),
    # ---------------- 측정 순서 ----------------
    I("condition.items", conditions.default_conditions(), "list", "condition", "측정 조건", "측정 조건",
      "샘플마다 실행할 단계 순서입니다. 첫 단계는 샘플 위로 이동입니다.",
      editor="conditions", validate="conditions", apply="다음 측정"),
    I("condition.active", conditions.DEFAULT_NAME, "str", "condition", "측정 조건", "활성 측정 조건",
      "측정가 쓸 측정 조건입니다.", editor="condition_active", validate="condition_active",
      apply="다음 측정"),
    I("data_dir", "../data_v2", "str", "advanced", "기록", "데이터 폴더",
      "촬영 · 측정 결과 · 보정 파일을 두는 폴더입니다. 바꾸면 다시 시작해야 합니다.", apply="재연결", advanced=True),
    I("capture.settle_s", 0.8, "float", "camera", "촬영", "이동 뒤 안정 시간",
      "마지막 이동이 끝난 뒤 이 시간이 지난 프레임부터 씁니다.", min=0, max=10, step=0.1, unit="s", apply="다음 촬영"),
    I("capture.fresh_frames", 5, "int", "camera", "촬영", "버릴 프레임 수",
      "이동 뒤 카메라 큐에 남은 옛 프레임을 버리기 위해 새로 받아 건너뛰는 프레임 수입니다. 큐 깊이보다 커야 합니다.", min=1, max=30, step=1, apply="다음 촬영"),
    # ---------------- 검출 ----------------
    I("calib.residual_limit_mm", 1.0, "float", "detect", "보정 방식", "좌표 보정 잔차 한계",
      "좌표 보정 점의 잔차가 이 값을 넘으면 빨갛게 표시합니다.",
      min=0.05, max=20, step=0.05, unit="mm", apply="다음 촬영"),
    I("calib.area_margin_mm", 1.0, "float", "detect", "보정 방식", "판 영역 경계 여유",
      "고정 보정에서 판 영역 경계에 이만큼보다 가까이 닿은 검출은 버립니다. 새겨진 선이나 경계 이음매가 칩으로 잡히는 것을 막습니다.",
      min=0, max=10, step=0.1, unit="mm", apply="다음 촬영"),
    I("calib.est_mm_per_px", 0.143, "float", "detect", "보정 방식", "대략 배율",
      "판 영역이 없을 때만 쓰는 예비 배율입니다. 판 영역이 있으면 판 영역 실제 크기로, 좌표 보정을 하면 보정값을 씁니다.",
      min=0.005, max=1.0, step=0.001, unit="mm/px", apply="다음 촬영"),
    I("fixed.layout", "auto", "enum", "detect", "고정 환경 검출", "배치",
      "자동은 격자 맞춤의 인라이어가 후보의 일정 비율 이상일 때만 격자를 쓰고 아니면 자유 배치로 검출합니다. 격자는 격자 예측으로 빈 칸을 채우되 조건을 못 넘으면 예측 없이 자유 배치로 냅니다. 자유는 검출된 것만 냅니다.",
      options=[{"value": "auto", "label": "자동"}, {"value": "grid", "label": "격자"}, {"value": "free", "label": "자유"}],
      apply="다음 촬영", show_if={"calib.mode": "fixed"}),
    I("fixed.grid_rows", 3, "int", "detect", "고정 환경 검출", "격자 행 수",
      "격자 배치의 행 수입니다. 화면 가로·세로 어느 쪽이든 되고 열 수와 바꿔 맞춥니다.",
      min=1, max=20, step=1, apply="다음 촬영", show_if={"calib.mode": "fixed"}),
    I("fixed.grid_cols", 5, "int", "detect", "고정 환경 검출", "격자 열 수",
      "격자 배치의 열 수입니다.",
      min=1, max=20, step=1, apply="다음 촬영", show_if={"calib.mode": "fixed"}),
    I("fixed.emit_predicted", True, "bool", "detect", "고정 환경 검출", "예측 위치 샘플",
      "윤곽을 끝까지 못 찾은 칸도 예측 위치로 샘플을 냅니다(약함 표시 · 확인 필요).",
      apply="다음 촬영", show_if={"calib.mode": "fixed"}),
    I("fixed.chip_mm", 11.0, "float", "detect", "고정 환경 검출", "예상 칩 긴 변",
      "칩 긴 변의 대략 길이입니다. 자동 추정이 실패하면 배율로 환산해 씁니다.",
      min=1, max=100, step=0.5, unit="mm", apply="다음 촬영", show_if={"calib.mode": "fixed"}),
    I("fixed.grid_min_ratio", 0.8, "float", "detect", "고정 환경 검출", "격자 인라이어 비율",
      "자동 배치: 격자 맞춤의 인라이어 / 후보가 이 값 이상이고 인라이어가 행 × 열 × 0.6 이상일 때만 격자를 씁니다.",
      min=0.3, max=1, step=0.05, apply="다음 촬영", advanced=True, show_if={"calib.mode": "fixed"}),
    I("fixed.use_ref", True, "bool", "detect", "고정 환경 검출", "빈 판 사진 사용",
      "판 영역 탭에서 찍은 빈 판 사진 사진이 유효하면 기준과의 차이로 칩을 찾고, 촬영마다 카메라 이동도 보정합니다. 기준이 없거나 무효면 단일 사진으로 검출합니다.",
      apply="다음 촬영", show_if={"calib.mode": "fixed"}),
    I("fixed.ref_seed", fd.DEFAULTS["ref_seed"], "float", "detect", "빈 판 사진 차이", "차이 임계(d)",
      "기준과의 차이 d 가 이 값을 넘는 곳이 후보입니다.", min=1, max=30, step=0.1, apply="다음 촬영", advanced=True,
      show_if={"calib.mode": "fixed"}),
    I("fixed.ref_faint", fd.DEFAULTS["ref_faint"], "float", "detect", "빈 판 사진 차이", "약한 칩 임계",
      "σ2 로 흐린 차이 d 가 이 값을 넘고 닫힌 테두리를 이루는 덩어리를 약한 칩(보완)으로 더합니다.", min=0.5, max=10, step=0.1,
      apply="다음 촬영", advanced=True, show_if={"calib.mode": "fixed"}),
    I("fixed.ref_area_ratio", list(fd.DEFAULTS["ref_area_ratio"]), "list", "detect", "빈 판 사진 차이", "면적 범위(배)",
      "예상 칩 면적의 [하한 배, 상한 배]입니다.", item="float", apply="다음 촬영", advanced=True, show_if={"calib.mode": "fixed"}),
    I("fixed.ref_min_solidity", fd.DEFAULTS["ref_min_solidity"], "float", "detect", "빈 판 사진 차이", "최소 볼록도",
      "후보의 면적 / 볼록껍질 면적 하한입니다.", min=0.3, max=1, step=0.01, apply="다음 촬영", advanced=True, show_if={"calib.mode": "fixed"}),
    I("fixed.ref_max_aspect", fd.DEFAULTS["ref_max_aspect"], "float", "detect", "빈 판 사진 차이", "최대 종횡비",
      "후보의 최소외접사각형 긴 변 / 짧은 변 상한입니다.", min=1, max=20, step=0.1, apply="다음 촬영", advanced=True, show_if={"calib.mode": "fixed"}),
    I("fixed.ref_open", fd.DEFAULTS["ref_open"], "int", "detect", "빈 판 사진 차이", "열기 커널",
      "이진화 뒤 open 커널 크기(px)입니다.", min=1, max=21, step=2, apply="다음 촬영", advanced=True, show_if={"calib.mode": "fixed"}),
    I("fixed.ref_close", fd.DEFAULTS["ref_close"], "int", "detect", "빈 판 사진 차이", "닫기 커널",
      "이진화 뒤 close 커널 크기(px)입니다. 반투명 칩의 얇은 테두리를 이어 붙입니다.", min=1, max=31, step=2, apply="다음 촬영", advanced=True,
      show_if={"calib.mode": "fixed"}),
    I("fixed.ref_thin_k", fd.DEFAULTS["ref_thin_k"], "float", "detect", "빈 판 사진 차이", "긁힘 떼어 내기(칩 길이 배)",
      "덩어리에서 이 배보다 가는 선(칩 꼭짓점을 지나는 긁힘)을 원판 열기로 떼어 낸 뒤 모양(볼록도 · 면적)을 판정합니다. 0 이면 끕니다.",
      min=0, max=0.3, step=0.01, apply="다음 촬영", advanced=True, show_if={"calib.mode": "fixed"}),
    I("fixed.ref_edge_k", fd.DEFAULTS["ref_edge_k"], "float", "detect", "빈 판 사진 차이", "경계 근처 제외(칩 길이 배)",
      "판 영역 경계에서 칩 길이의 이 배 안에 닿은 후보는 칩이 아니라고 봅니다(새겨진 선 · 레일).", min=0, max=2, step=0.1,
      apply="다음 촬영", advanced=True, show_if={"calib.mode": "fixed"}),
    I("fixed.ref_check_blobs", fd.DEFAULTS["ref_check_blobs"], "int", "detect", "빈 판 사진 차이", "기준 점검 · 큰 덩어리 수",
      "칩 크기 밖의 큰 차이 덩어리가 이보다 많으면 빈 판 사진 다시 촬영을 권장합니다.", min=1, max=100, step=1, apply="다음 촬영", advanced=True,
      show_if={"calib.mode": "fixed"}),
    I("fixed.ref_check_resid", fd.DEFAULTS["ref_check_resid"], "float", "detect", "빈 판 사진 차이", "기준 점검 · 정렬 잔차",
      "정렬 잔차가 이보다 크면 빈 판 사진 다시 촬영을 권장합니다(0.75 이상이면 정렬 실패). 바른 정렬은 0.23~0.47, 틀린 정렬은 0.87 이상입니다.", min=0.1, max=0.74, step=0.05, apply="다음 촬영",
      advanced=True, show_if={"calib.mode": "fixed"}),
    I("fixed.cam_shift_warn_mm", fd.DEFAULTS["cam_shift_warn_mm"], "float", "detect", "카메라 이동 보정", "이동 경고",
      "기준에 대한 카메라 이동이 이보다 크면 경고합니다(기준이 있을 때 촬영마다 정렬로 잽니다).", min=0.1, max=50, step=0.5, unit="mm",
      apply="다음 촬영", show_if={"calib.mode": "fixed"}),
    I("fixed.cam_shift_max_mm", fd.DEFAULTS["cam_shift_max_mm"], "float", "detect", "카메라 이동 보정", "이동 한도",
      "기준에 대한 카메라 이동이 이보다 크면 좌표를 만들지 않고 '카메라 이동 큼 · 기준 · 보정 다시' 로 알립니다(정렬 실패도 같습니다).",
      min=0.5, max=100, step=0.5, unit="mm", apply="다음 촬영", show_if={"calib.mode": "fixed"}),
    I("fixed.seed", fd.DEFAULTS["seed"], "float", "detect", "고정 환경 검출", "검출 임계(d)",
      "채널 강건 z 를 합친 d 가 이 값을 넘는 곳이 후보입니다. 낮추면 회색 칩이 잡히지만 판 긁힘도 늘어납니다.",
      min=1, max=30, step=0.1, apply="다음 촬영", advanced=True, show_if={"calib.mode": "fixed"}),
    I("fixed.local_seeds", list(fd.DEFAULTS["local_seeds"]), "list", "detect", "고정 환경 검출", "빈 칸 재탐색 임계",
      "격자 칸에서 후보를 못 찾았을 때 그 칸 창 안에서 차례로 낮춰 가며 다시 찾는 임계들입니다.",
      item="float", apply="다음 촬영", advanced=True, show_if={"calib.mode": "fixed"}),
    I("fixed.bg_k_chips", fd.DEFAULTS["bg_k_chips"], "float", "detect", "고정 환경 검출", "배경 커널(칩 길이 배)",
      "배경 추정 중앙값 필터의 크기입니다. 칩 긴 변의 몇 배로 잡습니다.",
      min=1, max=10, step=0.5, apply="다음 촬영", advanced=True, show_if={"calib.mode": "fixed"}),
    I("fixed.blur_sigma", fd.DEFAULTS["blur_sigma"], "float", "detect", "고정 환경 검출", "블러 σ",
      "잔차를 만들기 전 가우시안 블러 크기입니다(작업 해상도 기준).",
      min=0, max=10, step=0.1, unit="px", apply="다음 촬영", advanced=True, show_if={"calib.mode": "fixed"}),
    I("fixed.min_solidity", fd.DEFAULTS["min_solidity"], "float", "detect", "고정 환경 검출", "최소 볼록도",
      "후보의 면적 / 볼록껍질 면적 하한입니다.",
      min=0.3, max=1, step=0.01, apply="다음 촬영", advanced=True, show_if={"calib.mode": "fixed"}),
    I("fixed.max_aspect", fd.DEFAULTS["max_aspect"], "float", "detect", "고정 환경 검출", "최대 종횡비",
      "후보의 최소외접사각형 긴 변 / 짧은 변 상한입니다. 가늘고 긴 긁힘을 거릅니다.",
      min=1, max=20, step=0.1, apply="다음 촬영", advanced=True, show_if={"calib.mode": "fixed"}),
    I("fixed.min_fill", fd.DEFAULTS["min_fill"], "float", "detect", "고정 환경 검출", "최소 채움률",
      "후보 면적 / 최소외접사각형 면적 하한입니다.",
      min=0.1, max=1, step=0.01, apply="다음 촬영", advanced=True, show_if={"calib.mode": "fixed"}),
    I("fixed.area_ratio", list(fd.DEFAULTS["area_ratio"]), "list", "detect", "고정 환경 검출", "면적 범위(배)",
      "예상 칩 면적의 [하한 배, 상한 배]입니다.",
      item="float", apply="다음 촬영", advanced=True, show_if={"calib.mode": "fixed"}),
    I("fixed.work_long_px", fd.DEFAULTS["work_long_px"], "int", "detect", "고정 환경 검출", "작업 칩 길이",
      "검출용으로 줄인 사진에서 칩 긴 변이 이 정도 픽셀이 되게 합니다.",
      min=20, max=300, step=5, unit="px", apply="다음 촬영", advanced=True, show_if={"calib.mode": "fixed"}),
    I("calib.outlier_n", 4.0, "float", "detect", "보정 방식", "이상점 배수",
      "새 보정점이 현재 변환의 예측과 이 값 × 다른 점 잔차 중앙값(하한 0.3 mm) 이상 어긋나면 이상점 의심으로 확인을 받습니다. 연속 2번이면 카메라 이동 의심입니다.",
      min=1.5, max=20, step=0.5, apply="즉시", show_if={"calib.mode": "fixed"}),
    I("calib.area_size_mm", [154.0, 154.0], "list", "detect", "보정 방식", "판 영역 실제 크기",
      "판에 새겨진 사각형의 실제 [가로, 세로] mm 입니다(15.4 × 15.4 cm). 좌표 보정 전에는 판 영역 네 변의 픽셀 길이와 이 크기로 배율을 계산하고, 좌표 보정을 적용하면 변 길이가 이 크기와 2% 넘게 다른지 확인합니다.",
      item="float", validate="area_size", apply="다음 촬영"),
    I("sample_add.snap", True, "bool", "detect", "수동 지정", "클릭 스냅",
      "샘플 추가 클릭 주변(칩 길이 정도 반경)에서 칩을 찾아 그 중심에 붙입니다. 못 찾으면 클릭한 자리 그대로 둡니다.",
      apply="즉시"),

    # ---------------- 고급 ----------------
    I("file_log", True, "bool", "advanced", "기록", "파일 로그",
      "화면 로그를 날짜별 파일에도 남깁니다.", advanced=True),
]

BY_KEY = {it["key"]: it for it in ITEMS}

# 목록형 설정의 길이 · 원소 범위(빈 값이 저장돼 촬영이 깨지는 일이 없도록)
LIST_RULES = {
    "jog.steps": {"min_len": 1, "max_len": 8, "elem": "float", "lo": 0.01, "hi": 100.0},
    "fixed.ref_area_ratio": {"len": 2, "elem": "float", "lo": 0.01, "hi": 100.0},
    "fixed.area_ratio": {"len": 2, "elem": "float", "lo": 0.01, "hi": 100.0},
    "fixed.local_seeds": {"min_len": 1, "max_len": 8, "elem": "float", "lo": 0.5, "hi": 50.0},
    "calib.area_size_mm": {"len": 2, "elem": "float", "lo": 10.0, "hi": 500.0},
    "tool.items": {"min_len": 1, "max_len": 20},
    "relay.items": {"len": 10},
    "condition.items": {"min_len": 1, "max_len": 30},
}


# --------------------------------------------------------------------------
# 경로 도우미
# --------------------------------------------------------------------------
def get_path(d, key, default=None):
    cur = d
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_path(d, key, value):
    parts = key.split(".")
    cur = d
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def has_path(d, key):
    sentinel = object()
    return get_path(d, key, sentinel) is not sentinel


def defaults():
    """스키마 기본값으로 만든 중첩 dict."""
    out = {}
    for it in ITEMS:
        set_path(out, it["key"], copy.deepcopy(it["default"]))
    return out


def app_keys():
    """settings.json 최상위에서 앱이 다루는 키(점 표기의 첫 마디)."""
    seen = []
    for it in ITEMS:
        top = it["key"].split(".")[0]
        if top not in seen:
            seen.append(top)
    return tuple(seen)


def deep_merge(base, over):
    """over 가 base 를 덮는다. dict 는 재귀, 그 밖(list 포함)은 통째로 바꾼다."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# --------------------------------------------------------------------------
# 이행(예전 settings.json) · 정규화
# --------------------------------------------------------------------------
def migrate(raw, fill=True):
    """예전 settings.json 을 지금 형식으로. 모르는 키는 지우지 않는다.

    fill=False(가져오기)면 파일에 있는 예전 키만 바꾸고, 없는 항목을 기본값으로
    채우지 않는다 - 일부만 담긴 파일을 가져올 때 공구·측정 조건가 기본값으로 덮이면 안 된다.

    - park_xy · marker_mm_xy 는 그대로
    - z_measure_mm → 히트건(기본 공구)의 z_work_mm
    - dwell_s → 기본 측정 조건의 대기 단계 값
    - autofocus/focus("auto") → focus_mode · focus_sweep
    - capture_xy 가 없으면 park_xy (예전 촬영은 대기 위치 위치에서 찍었다)
    돌려주는 것: (새 dict, 바뀐 것 설명 목록)
    """
    s = copy.deepcopy(raw or {})
    notes = []
    if not has_path(s, "tool.items") and "z_measure_mm" in s:
        z = s.get("z_measure_mm")
        try:
            z = float(z) if z is not None else 44.0
        except (TypeError, ValueError):
            z = 44.0
        set_path(s, "tool.items", _tools_default(z))
        if "z_measure_mm" in s:
            notes.append("z_measure_mm %g → 히트건 작업 높이" % z)
    if not has_path(s, "condition.items") and "dwell_s" in s:
        w = s.get("dwell_s")
        try:
            w = float(w) if w is not None else 5.0
        except (TypeError, ValueError):
            w = 5.0
        set_path(s, "condition.items", [conditions.default_condition(w)])
        if "dwell_s" in s:
            notes.append("dwell_s %g → 기본 측정 조건 대기" % w)
    if "focus_mode" not in s and "autofocus" in s:
        af = s.get("autofocus", True)
        s["focus_mode"] = "auto" if af in (True, None) else "fixed"
    if isinstance(s.get("focus"), str):
        # "auto" = 열고 나서 스윕으로 찾는다(예전 표기)
        s["focus_sweep"] = True
        s["focus"] = -1
        notes.append("focus \"auto\" → 초점 스윕")
    zz = get_path(s, "cooler.zones") if has_path(s, "cooler.zones") else None
    if isinstance(zz, dict) and "cells" not in zz:
        newz, _ch = zones_mod.migrate(zz)
        set_path(s, "cooler.zones", newz)
        notes.append("쿨러 구역 사진 분할 → 판 기준 등분 (%d×%d)" % (newz["rows"], newz["cols"]))
    if "capture_xy" not in s and isinstance(s.get("park_xy"), list):
        s["capture_xy"] = list(s["park_xy"])
    if s.get("wb_temperature") is None and "wb_temperature" in s:
        s["wb_temperature"] = 0
    return s, notes


def normalize(values):
    """유도 값을 맞춘다(화면에 없는 키). 제자리에서 고치고 돌려준다."""
    try:
        values["auto_exposure"] = float(values.get("exposure") or 0) == 0
    except (TypeError, ValueError):
        values["auto_exposure"] = True
    values["autofocus"] = values.get("focus_mode", "auto") == "auto"
    if isinstance(values.get("fourcc"), str):
        values["fourcc"] = values["fourcc"].upper()[:4]
    if values.get("serial_port") in ("", None):
        values["serial_port"] = None
    return values


# --------------------------------------------------------------------------
# 검증
# --------------------------------------------------------------------------
def _num(v):
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _check_type(it, v):
    """(정리된 값, 오류 문구|None)."""
    t = it["type"]
    if v is None and it.get("nullable"):
        return None, None
    if t == "int":
        f = _num(v)
        if f is None or abs(f - round(f)) > 1e-9:
            return v, "정수 필요"
        v = int(round(f))
    elif t == "float":
        f = _num(v)
        if f is None:
            return v, "숫자 필요"
        v = f
    elif t == "bool":
        if not isinstance(v, bool):
            return v, "켜기/끄기 필요"
    elif t == "str":
        if not isinstance(v, str):
            return v, "문자 필요"
        v = v.strip()
    elif t == "enum":
        opts = [o["value"] for o in it.get("options") or []]
        if v not in opts:
            return v, "선택지 밖"
    elif t == "xy":
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            return v, "X · Y 필요"
        a, b = _num(v[0]), _num(v[1])
        if a is None or b is None:
            return v, "X · Y 숫자 필요"
        v = [a, b]
    elif t == "list":
        if not isinstance(v, list):
            return v, "목록 필요"
        rule = LIST_RULES.get(it["key"], {})
        if "len" in rule and len(v) != rule["len"]:
            return v, "%d개 필요" % rule["len"]
        if len(v) < rule.get("min_len", 1):
            return v, "비어 있음 · %d개 이상 필요" % rule.get("min_len", 1)
        if len(v) > rule.get("max_len", 200):
            return v, "%d개 이하" % rule.get("max_len", 200)
        if rule.get("elem") == "float" or it.get("item") == "float":
            out = []
            for x in v:
                f = _num(x)
                if f is None:
                    return v, "숫자 목록 필요"
                if f < rule.get("lo", -1e18) or f > rule.get("hi", 1e18):
                    return v, "값 %g~%g" % (rule.get("lo", -1e18), rule.get("hi", 1e18))
                out.append(f)
            v = out
    elif t == "object":
        if not isinstance(v, dict):
            return v, "형식 오류"
    lo, hi = it.get("min"), it.get("max")
    if t in ("int", "float") and v is not None:
        if lo is not None and v < lo - 1e-9:
            return v, "%g 이상" % lo
        if hi is not None and v > hi + 1e-9:
            return v, "%g 이하" % hi
    return v, None


def _v_xy_in_limits(key, v, values, errs, warns):
    lx = get_path(values, "limits.x_max_mm")
    ly = get_path(values, "limits.y_max_mm")
    if _num(lx) is None or _num(ly) is None:
        return
    if not (0 <= v[0] <= lx and 0 <= v[1] <= ly):
        errs[key] = "가동범위 X 0~%g · Y 0~%g 밖" % (lx, ly)


def _v_contact_z(key, v, values, errs, warns):
    zmax = _num(get_path(values, "limits.z_max_mm"))
    if v is not None and zmax is not None and not (0 < v <= zmax + 1e-9):
        errs[key] = "Z 가동범위 0 초과 %g 이하" % zmax


def _v_seed_grow(key, v, values, errs, warns):
    seed = _num(values.get("stat_seed"))
    if seed is not None and v >= seed:
        errs[key] = "씨앗 임계(%g)보다 작아야 함" % seed


def _v_area_size(key, v, values, errs, warns):
    if len(v) != 2 or any(_num(x) is None or _num(x) <= 0 or _num(x) > 1000 for x in v):
        errs[key] = "[가로, 세로] 0 초과 1000 mm 이하"


def _v_zones(key, v, values, errs, warns):
    sz = get_path(values, "calib.area_size_mm") or [154.0, 154.0]
    try:
        size = (float(sz[0]), float(sz[1]))
    except (TypeError, ValueError, IndexError):
        size = (154.0, 154.0)
    z, _ch = zones_mod.migrate(v)
    hr = _num(get_path(values, "heater.relay"))
    e = zones_mod.validate(z, size, N_RELAY, None if hr is None else int(hr))
    if e:
        errs[key] = e


def _v_tol_warn(key, v, values, errs, warns):
    w = _num(get_path(values, "photohome.warn_mm"))
    if w is not None and v >= w:
        errs[key] = "경고 오차(%g)보다 작아야 함" % w


def _v_head_id(key, v, values, errs, warns):
    plate = set()
    for k in (values.get("marker_mm_xy") or {}):
        try:
            plate.add(int(k))
        except (TypeError, ValueError):
            pass
    plate |= {0, 1, 2, 3}
    if v in plate:
        errs[key] = "판 마커 번호(%s)와 겹침" % ",".join(str(i) for i in sorted(plate))


def _v_jog_steps(key, v, values, errs, warns):
    mx = _num(get_path(values, "jog.max_mm")) or 1e9
    if not 1 <= len(v) <= 6:
        errs[key] = "1~6개"
    elif any(x <= 0 or x > mx for x in v):
        errs[key] = "0 보다 크고 한도(%g) 이하" % mx


def _v_touch_max(key, v, values, errs, warns):
    if 0 < v < 1:
        errs[key] = "0 또는 1 이상"


def _v_markers(key, v, values, errs, warns):
    for k, xy in v.items():
        try:
            int(k)
        except (TypeError, ValueError):
            errs[key] = "마커 번호 오류 (%s)" % k
            return
        if (not isinstance(xy, (list, tuple)) or len(xy) != 2
                or _num(xy[0]) is None or _num(xy[1]) is None):
            errs["%s.%s" % (key, k)] = "X · Y 숫자 필요"
    if len(v) < 3:
        errs[key] = "마커 3개 이상"


def _v_tools(key, v, values, errs, warns):
    zmax = _num(get_path(values, "limits.z_max_mm")) or 0.0
    names = []
    for i, t in enumerate(v):
        p = "%s[%d]" % (key, i)
        if not isinstance(t, dict):
            errs[p] = "형식 오류"
            continue
        nm = str(t.get("name") or "").strip()
        if not nm:
            errs[p + ".name"] = "이름 필요"
        elif len(nm) > 20:
            errs[p + ".name"] = "이름 20자 이내"
        elif nm in names:
            errs[p + ".name"] = "이름 중복"
        names.append(nm)
        for f in ("dx_mm", "dy_mm"):
            x = _num(t.get(f))
            if x is None or abs(x) > 200:
                errs["%s.%s" % (p, f)] = "-200~200 mm"
        if t.get("tip_shape") not in (None, "rect", "circle"):
            errs[p + ".tip_shape"] = "rect · circle 중 하나"
        for f in ("tip_w_mm", "tip_h_mm"):
            if f in t:
                x = _num(t.get(f))
                if x is None or x <= 0 or x > 100:
                    errs["%s.%s" % (p, f)] = "0 초과 100 mm 이하"
        for f in ("tip_angle_deg", "tip_angle_img_deg"):
            if t.get(f) is not None:
                x = _num(t.get(f))
                if x is None or abs(x) > 360:
                    errs["%s.%s" % (p, f)] = "-360~360 도"
        z = _num(t.get("z_work_mm"))
        if z is None or z < 0 or z > zmax + 1e-9:
            errs[p + ".z_work_mm"] = "Z 가동범위 0~%g 밖" % zmax
        if len(str(t.get("note") or "")) > 80:
            errs[p + ".note"] = "메모 80자 이내"
    if TOOL_POINTER not in names:
        errs[key] = "'%s' 은 지울 수 없음" % TOOL_POINTER
    if len(v) > 20:
        errs[key] = "공구 20개 이내"


def _v_tool_active(key, v, values, errs, warns):
    names = [str((t or {}).get("name") or "").strip()
             for t in get_path(values, "tool.items") or [] if isinstance(t, dict)]
    if v not in names:
        errs[key] = "목록에 없는 공구"


def _v_relays(key, v, values, errs, warns):
    if len(v) != N_RELAY:
        errs[key] = "릴레이 %d개 필요" % N_RELAY
        return
    for i, r in enumerate(v):
        p = "%s[%d]" % (key, i)
        if not isinstance(r, dict):
            errs[p] = "형식 오류"
            continue
        if not isinstance(r.get("enabled"), bool):
            errs[p + ".enabled"] = "켜기/끄기 필요"
        nm = str(r.get("name") or "").strip()
        if not nm or len(nm) > 20:
            errs[p + ".name"] = "이름 1~20자"
        m = _num(r.get("max_on_s"))
        if m is None or m < 0 or m > 86400:
            errs[p + ".max_on_s"] = "0~86400 s"


def _v_conditions(key, v, values, errs, warns):
    zmax = _num(get_path(values, "limits.z_max_mm")) or 0.0
    names = []
    if not v:
        errs[key] = "측정 조건 1개 이상"
        return
    for i, r in enumerate(v):
        p = "%s[%d]" % (key, i)
        e, w = conditions.validate_condition(r, values, zmax, prefix=p)
        errs.update(e)
        warns.extend(w)
        nm = str((r or {}).get("name") or "").strip() if isinstance(r, dict) else ""
        if nm and nm in names:
            errs[p + ".name"] = "이름 중복"
        names.append(nm)
    if len(v) > 30:
        errs[key] = "측정 조건 30개 이내"


def _v_condition_active(key, v, values, errs, warns):
    names = [str((r or {}).get("name") or "").strip()
             for r in get_path(values, "condition.items") or [] if isinstance(r, dict)]
    if v not in names:
        errs[key] = "목록에 없는 측정 조건"


def _v_rt(key, v, values, errs, warns):
    mx, brk = _num(get_path(values, "rt.max_temp")), _num(get_path(values, "rt.break_temp"))
    if key == "rt.break_temp" and mx is not None and brk is not None and brk >= mx:
        errs[key] = "최고 온도(%g)보다 낮아야 함" % mx
    elif key == "rt.emergency_c" and v is not None and mx is not None and v <= mx:
        errs[key] = "최고 온도(%g)보다 높아야 함" % mx
    elif key == "rt.start_range":
        if not any(abs(math.log10(v / r)) < 1e-6 for r in (1e-8, 1e-7, 1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 1.0)):
            errs[key] = "10 nA~1 A 의 10배 단위"
    elif key == "rt.cooler_custom":
        txt = (v or "").strip()
        if txt:
            try:
                ns = [int(x) for x in txt.split(",")]
            except ValueError:
                errs[key] = "릴레이 번호를 쉼표로"
                return
            hr = _num(get_path(values, "heater.relay"))
            if any(n < 1 or n > N_RELAY or (hr is not None and n == int(hr)) for n in ns):
                errs[key] = "1~10 (히트건 제외)"
    elif key == "rt.file_name":
        if not v or any(c in v for c in '\\/:*?"<>|'):
            errs[key] = "파일 이름에 쓸 수 없는 글자 · 빈 값"


VALIDATORS = {
    "rt": _v_rt, "contact_z": _v_contact_z, "xy_in_limits": _v_xy_in_limits, "seed_grow": _v_seed_grow, "tol_warn": _v_tol_warn,
    "head_id": _v_head_id, "jog_steps": _v_jog_steps, "touch_max": _v_touch_max,
    "markers": _v_markers, "zones": _v_zones, "area_size": _v_area_size, "tools": _v_tools, "tool_active": _v_tool_active,
    "relays": _v_relays, "conditions": _v_conditions, "condition_active": _v_condition_active,
}


def validate(values):
    """(정리된 값, errors{key: 문구}, warnings[문구]). 값은 복사본이다.

    타입·범위를 먼저 보고, 그것이 통과한 항목만 교차 검증한다(타입이 틀린 값으로
    교차 검증을 하면 엉뚱한 사유가 나온다).
    """
    v = copy.deepcopy(values)
    errs, warns = {}, []
    for it in ITEMS:
        key = it["key"]
        if not has_path(v, key):
            set_path(v, key, copy.deepcopy(it["default"]))
        val, e = _check_type(it, get_path(v, key))
        if e:
            errs[key] = e
        else:
            set_path(v, key, val)
    normalize(v)
    for it in ITEMS:
        fn = VALIDATORS.get(it.get("validate") or "")
        key = it["key"]
        if fn is None or key in errs:
            continue
        fn(key, get_path(v, key), v, errs, warns)
    return v, errs, warns


def public_schema():
    """화면에 줄 스키마(직렬화 가능한 것만)."""
    out = []
    for it in ITEMS:
        d = {k: copy.deepcopy(x) for k, x in it.items()}
        out.append(d)
    return {"tabs": [{"id": t, "label": l} for t, l in TABS], "items": out,
            "apply": list(APPLY)}


def tab_keys(tab):
    """그 탭의 [기본값으로] 대상 키. 고급 탭은 advanced 항목 전부."""
    if tab == "advanced":
        return [it["key"] for it in ITEMS if it.get("advanced") and not it.get("hidden")]
    return [it["key"] for it in ITEMS
            if it["tab"] == tab and not it.get("advanced") and not it.get("hidden")]
