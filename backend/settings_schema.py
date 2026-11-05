"""settings_schema.py - 운용 설정의 단일 출처(기본값 · 타입 · 범위 · 화면 배치 · 검증).

원칙: 운용 값은 여기에만 기본값을 둔다. settings.json 이 덮어쓰고, 설정 화면은
이 스키마로 자동 생성된다. 코드 다른 곳에 운용 숫자를 박지 않는다.
(검출 파라미터는 예외 - detect.DEFAULTS 가 기본값이고 settings.json 을 직접
편집한다. 화면에 있던 stat_seed · stat_grow 만 여기로 옮겼다.)

키는 점 표기다. "relay.items" = settings["relay"]["items"].

항목 필드
  key, default, type(int|float|bool|str|enum|xy|list|object), min, max, step,
  unit, tab, group, label(명사형 짧게), tip(…합니다 체 1~2문장),
  apply("즉시"|"다음 촬영"|"재연결"|"다음 순회"), advanced(bool),
  validate(교차 검증 함수 이름), options(enum), editor(전용 편집기),
  hidden(화면에 안 보임 - 다른 값에서 유도), pick(xy: 현재 위치 넣기),
  nullable(None 허용), item(list 원소 타입), warn(바꾸면 경고 대화상자)

순수 모듈이다(state·장비를 모른다). state 가 이것으로 기본값을 만들고, 저장 전
검증(validate)과 예전 파일 이행(migrate)을 한다.
"""

import copy
import math

import recipe
from core import fixed_detect as fd

TABS = [
    ("camera", "카메라"),
    ("stage", "스테이지"),
    ("position", "위치"),
    ("photohome", "사진 호밍"),
    ("tool", "공구"),
    ("relay", "릴레이"),
    ("recipe", "측정 순서"),
    ("detect", "검출"),
    ("advanced", "고급"),
]
APPLY = ("즉시", "다음 촬영", "재연결", "다음 순회")

PLATE_MARKER_DEFAULT = {"0": [201, 160], "1": [201, 19], "2": [15, 160], "3": [15, 20]}
TOOL_POINTER = "기준 포인터"
TOOL_HEATGUN = "히트건"
N_RELAY = 10
POLICY_OPTS = [{"value": "off", "label": "전부 끄기"}, {"value": "keep", "label": "그대로"}]


def _relays_default():
    return [{"enabled": False, "name": "R%d" % (i + 1), "max_on_s": 0}
            for i in range(N_RELAY)]


def _tools_default(z_work=44.0):
    return [
        {"name": TOOL_POINTER, "dx_mm": 0.0, "dy_mm": 0.0, "z_work_mm": 0.0,
         "note": "스테이지 좌표가 가리키는 점"},
        {"name": TOOL_HEATGUN, "dx_mm": 0.0, "dy_mm": 0.0, "z_work_mm": float(z_work),
         "note": "노즐이 비스듬해 가열 중심은 오프셋 보정으로 잡는다"},
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
    I("bracket", False, "bool", "camera", "촬영", "노출 브라케팅",
      "노출을 바꿔 여러 장을 받아 감지 영역만 융합합니다. 끄면 고정 노출 한 장입니다. 함체 안 조명이 일정하면 끄고, 한쪽에서 강한 빛이 들어오는 환경에서 켭니다.",
      apply="다음 촬영"),
    I("bracket_exposure", [], "list", "camera", "촬영", "브라케팅 노출 목록",
      "쉼표로 구분한 노출값입니다. 비우면 밝기를 보며 자동으로 정합니다.",
      item="float", apply="다음 촬영"),
    I("bracket_settle_s", 0.4, "float", "camera", "촬영", "브라케팅 안정 대기",
      "노출을 바꾼 뒤 장을 받기 전에 기다리는 시간입니다.",
      min=0, max=5, step=0.1, unit="s", apply="다음 촬영"),
    I("flat_field", True, "bool", "camera", "촬영", "조명 평탄화",
      "감지영역의 종이를 흰색 기준으로 조명 기울기와 화이트밸런스를 바로잡습니다. 고정 보정에서는 기준이 없어 건너뜁니다.",
      apply="다음 촬영"),
    I("capture_retries", 2, "int", "camera", "촬영", "촬영 재시도",
      "촬영 중 프레임이 끊기면 카메라를 다시 열고 이만큼 다시 찍습니다.",
      min=0, max=10, step=1, apply="다음 촬영"),
    I("preview_fps", 4.0, "float", "camera", "미리보기 · 복구", "미리보기 주기",
      "미리보기 그림을 새로 만드는 빈도입니다.",
      min=0.5, max=15, step=0.5, unit="fps"),
    I("preview_max_w", 1280, "int", "camera", "미리보기 · 복구", "미리보기 최대 가로",
      "미리보기 그림을 이 가로 폭 이하로 줄여 보냅니다. 촬영과 검출은 원본 해상도로 합니다.",
      min=320, max=4096, step=10, unit="px"),
    I("cam_first_frame_s", 5.0, "float", "camera", "미리보기 · 복구", "첫 프레임 대기",
      "카메라를 연 뒤 첫 프레임을 기다리는 최대 시간입니다. 4백만 화소를 넘으면 두 배로 기다립니다.",
      min=1, max=60, step=0.5, unit="s", advanced=True),
    I("cam_fail_limit", 3, "int", "camera", "미리보기 · 복구", "연속 실패 허용",
      "프레임을 이만큼 연속으로 못 받으면 카메라를 닫고 다시 엽니다.",
      min=1, max=30, step=1),
    I("cam_reopen_s", 5.0, "float", "camera", "미리보기 · 복구", "재개방 대기",
      "카메라를 닫은 뒤 다시 열기까지 기다리는 시간입니다. 실패가 이어지면 두 배씩 늘립니다.",
      min=0.5, max=120, step=0.5, unit="s"),
    I("cam_reopen_max_s", 30.0, "float", "camera", "미리보기 · 복구", "재개방 대기 최대",
      "재개방 대기가 늘어날 수 있는 최대 시간입니다.",
      min=1, max=600, step=1, unit="s", advanced=True),
    # 유도 값(화면에 없음): focus_mode / exposure 에서 만든다
    I("autofocus", True, "bool", "camera", "초점", "자동 초점", "", hidden=True),
    I("auto_exposure", True, "bool", "camera", "장치", "자동 노출", "", hidden=True),

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
    I("park_xy", [0.0, 90.0], "xy", "position", "자리", "파킹 위치",
      "순회가 끝나고 쉬는 자리입니다.", pick=True, validate="xy_in_limits", unit="mm"),
    I("capture_xy", [0.0, 90.0], "xy", "position", "자리", "촬영 위치",
      "촬영할 때 스테이지가 가는 자리입니다. 히트건이 웨이퍼를 가리지 않는 곳으로 둡니다.",
      pick=True, validate="xy_in_limits", unit="mm", apply="다음 촬영"),
    I("after_run", "park", "enum", "position", "자리", "순회 후 갈 곳",
      "순회가 끝나거나 정지했을 때 Z 를 올린 뒤 가는 곳입니다.",
      options=[{"value": "park", "label": "파킹 위치"},
               {"value": "capture", "label": "촬영 위치"},
               {"value": "origin", "label": "원점"},
               {"value": "stay", "label": "그 자리"}], apply="다음 순회"),
    I("flow.capture_lift_z", True, "bool", "position", "촬영 흐름", "촬영 전 Z 올리기",
      "촬영 위치로 가기 전에 Z 를 맨 위로 올립니다.", apply="다음 촬영"),
    I("flow.capture_goto", True, "bool", "position", "촬영 흐름", "촬영 위치로 이동",
      "촬영 전에 촬영 위치로 갑니다. 끄면 지금 자리에서 찍습니다.", apply="다음 촬영"),
    I("flow.require_fresh_capture", False, "bool", "position", "촬영 흐름",
      "순회 전 새 촬영 요구", "한 번 순회한 촬영으로는 다시 순회하지 않고 새 촬영을 요구합니다.",
      apply="다음 순회"),
    I("flow.after_photo", False, "bool", "position", "촬영 흐름", "순회 후 확인 촬영",
      "순회가 끝나면 촬영 위치로 가서 한 장 더 찍어 같은 폴더에 남깁니다.",
      apply="다음 순회"),

    # ---------------- 사진 호밍 ----------------
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
      "스테이지를 연결하면 이동 없이 한 장 찍어 헤드 마커로 위치를 판단합니다. 헤드 보정이 유효할 때만 합니다.",
      apply="재연결"),
    I("photohome.edge_margin_px", 0, "int", "photohome", "판정", "화면 끝 여백",
      "헤드 마커가 화면 가장자리에서 이만큼 안쪽에 걸치면 결과를 쓰지 않습니다. 0 이면 프레임 짧은 변의 3% 입니다.",
      min=0, max=1000, step=1, unit="px"),
    I("photohome.before_capture", True, "bool", "photohome", "자동 확인", "촬영 전 자동 확인",
      "촬영 위치에 도착하면 헤드 마커로 위치를 확인합니다.", apply="다음 촬영"),
    I("photohome.after_move", False, "bool", "photohome", "자동 확인", "샘플 이동 후 재확인",
      "순회 중 샘플 위에 도착할 때마다 헤드 마커로 위치를 확인합니다.", apply="다음 순회"),
    I("photohome.after_move_fail", "stop", "enum", "photohome", "자동 확인", "재확인 실패 시",
      "한 번 바로잡아도 허용 오차를 넘을 때의 동작입니다.",
      options=[{"value": "stop", "label": "정지"}, {"value": "continue", "label": "경고 후 계속"}],
      apply="다음 순회"),
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
      "공구마다 오프셋(기준 포인터 대비)과 작업 높이입니다. 기준 포인터는 지울 수 없습니다.",
      editor="tools", validate="tools", apply="다음 순회"),
    I("tool.active", TOOL_POINTER, "str", "tool", "공구 목록", "활성 공구",
      "순회와 샘플 이동이 오프셋을 적용할 공구입니다.",
      editor="tool_active", validate="tool_active", apply="다음 순회"),

    # ---------------- 릴레이 ----------------
    I("relay.items", _relays_default(), "list", "relay", "릴레이", "릴레이",
      "릴레이마다 사용 여부 · 이름 · 최대 켜짐 시간입니다. 사용하지 않는 릴레이는 켤 수 없습니다.",
      editor="relays", validate="relays"),
    I("relay.on_estop", "off", "enum", "relay", "정책", "비상정지 시",
      "비상정지 때 켜진 릴레이를 어떻게 할지 정합니다.", options=POLICY_OPTS),
    I("relay.on_stop", "off", "enum", "relay", "정책", "정지 · 순회 종료 시",
      "순회가 정지하거나 끝났을 때의 처리입니다.", options=POLICY_OPTS),
    I("relay.on_error", "off", "enum", "relay", "정책", "오류 시",
      "순회가 오류로 멈췄을 때의 처리입니다.", options=POLICY_OPTS),
    I("relay.on_exit", "off", "enum", "relay", "정책", "종료 · 연결 해제 시",
      "프로그램 종료나 스테이지 연결 해제 때의 처리입니다.", options=POLICY_OPTS),
    I("relay.watchdog_ms", 5000, "int", "relay", "통신 감시", "통신 감시 시간",
      "펌웨어가 이 시간 동안 명령을 못 받으면 릴레이를 전부 끕니다. 0 이면 끕니다.",
      min=0, max=600000, step=500, unit="ms"),

    # ---------------- 측정 순서 ----------------
    I("recipe.items", [recipe.default_recipe()], "list", "recipe", "레시피", "레시피",
      "샘플마다 실행할 단계 순서입니다. 첫 단계는 샘플 위로 이동입니다.",
      editor="recipes", validate="recipes", apply="다음 순회"),
    I("recipe.active", recipe.DEFAULT_NAME, "str", "recipe", "레시피", "활성 레시피",
      "순회가 쓸 레시피입니다.", editor="recipe_active", validate="recipe_active",
      apply="다음 순회"),
    I("measure.driver", "dummy", "enum", "recipe", "계측기", "계측기 드라이버",
      "측정 단계가 부르는 계측기입니다. 지금은 값 없이 통과하는 더미뿐입니다.",
      options=[{"value": "dummy", "label": "더미"}], apply="다음 순회"),

    # ---------------- 검출 ----------------
    I("stat_seed", 25.0, "float", "detect", "판정", "검출 씨앗 임계",
      "잡음으로 정규화한 Lab 잔차의 d² 가 이 값을 넘는 곳이 샘플의 씨앗입니다(χ²(3) 25 ≈ 1e-5).",
      min=1, max=500, step=1, unit="d²", apply="다음 촬영"),
    I("stat_grow", 12.0, "float", "detect", "판정", "검출 확장 임계",
      "씨앗에서 이어 붙이는 화소의 d² 하한입니다. 씨앗 임계보다 작아야 합니다.",
      min=1, max=500, step=1, unit="d²", apply="다음 촬영", validate="seed_grow"),
    I("calib.mode", "fixed", "enum", "detect", "보정 방식", "보정 방식",
      "고정은 판 마커 없이 저장한 감지 영역 · 판 보정으로 좌표를 만듭니다. 마커는 판 마커 4개를 매번 찍어 보정하는 예전 방식입니다.",
      options=[{"value": "fixed", "label": "고정"}, {"value": "markers", "label": "판 마커"}],
      apply="다음 촬영"),
    I("calib.residual_limit_mm", 1.0, "float", "detect", "보정 방식", "판 보정 잔차 한계",
      "판 보정 점의 잔차가 이 값을 넘으면 빨갛게 표시합니다.",
      min=0.05, max=20, step=0.05, unit="mm", apply="다음 촬영"),
    I("calib.area_margin_mm", 1.0, "float", "detect", "보정 방식", "감지 영역 경계 여유",
      "고정 보정에서 감지 영역 경계에 이만큼보다 가까이 닿은 검출은 버립니다. 새겨진 선이나 경계 이음매가 칩으로 잡히는 것을 막습니다.",
      min=0, max=10, step=0.1, unit="mm", apply="다음 촬영"),
    I("calib.est_mm_per_px", 0.143, "float", "detect", "보정 방식", "대략 배율",
      "판 보정 전 대략 배율입니다. 판 사각형 눈금 10 mm 간격의 픽셀 수로 잴 수 있습니다. 판 보정을 하면 보정값을 씁니다.",
      min=0.005, max=1.0, step=0.001, unit="mm/px", apply="다음 촬영"),
    I("fixed.detector", "fixed", "enum", "detect", "고정 환경 검출", "검출기",
      "고정은 천장 고정 카메라 · 판 위 샘플 · 격자 배치 전용 검출기입니다. 기존은 웨이퍼·철판 일반 검출기입니다.",
      options=[{"value": "fixed", "label": "고정 환경"}, {"value": "legacy", "label": "기존"}],
      apply="다음 촬영", show_if={"calib.mode": "fixed"}),
    I("fixed.layout", "grid", "enum", "detect", "고정 환경 검출", "배치",
      "격자는 샘플이 행·열 격자로 놓였다고 보고 빈 칸을 찾아 채웁니다. 자유는 검출된 것만 냅니다.",
      options=[{"value": "grid", "label": "격자"}, {"value": "free", "label": "자유"}],
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
    I("fixed.use_ref", False, "bool", "detect", "고정 환경 검출", "빈 판 기준 사용",
      "감지 영역 탭에서 찍은 빈 판 기준 사진이 유효하면 배경을 그 사진으로 씁니다(미검증). 무효면 쓰지 않습니다.",
      apply="다음 촬영", show_if={"calib.mode": "fixed"}),
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
    I("detect_max_px", 2000, "int", "detect", "보정 방식", "검출 최대 변",
      "기존 검출기에서 감지 영역 crop 의 긴 변이 이보다 크면 줄여서 검출하고 좌표를 원본으로 되돌립니다. 0 이면 줄이지 않습니다. 고정 환경 검출기는 작업 칩 길이로 스스로 줄입니다.",
      min=0, max=8000, step=100, unit="px", apply="다음 촬영"),
    I("marker_mm", 30.0, "float", "detect", "판 마커", "판 마커 크기",
      "베이스에 붙인 ArUco 마커 한 변의 실제 크기입니다.",
      min=5, max=200, step=1, unit="mm", apply="다음 촬영", show_if={"calib.mode": "markers"}),
    I("marker_mm_xy", copy.deepcopy(PLATE_MARKER_DEFAULT), "object", "detect", "판 마커",
      "판 마커 좌표", "판 마커 0~3 중심의 스테이지 좌표입니다. 보정 창에서 포인터로 다시 잡습니다.",
      editor="markers", validate="markers", apply="다음 촬영", show_if={"calib.mode": "markers"}),
    I("calib.min_markers", 3, "int", "detect", "판 마커", "판 마커 최소 개수",
      "좌표를 만들 수 있는 판 마커 최소 개수입니다. 3 이면 하나가 가려져도 됩니다.",
      min=3, max=4, step=1, apply="다음 촬영", show_if={"calib.mode": "markers"}),
    I("calib.confirm_3markers", True, "bool", "detect", "판 마커", "마커 3개면 확인",
      "판 마커가 3개만 보이면 순회 시작 전에 확인을 받습니다(오차 약 1 mm).",
      apply="다음 순회", show_if={"calib.mode": "markers"}),

    # ---------------- 고급 ----------------
    I("origin_gap_mm", 2.0, "float", "advanced", "원점", "원점 이격",
      "원점(0)은 끝단에서 이만큼 물러난 자리입니다. 바꾸면 판 마커 · 헤드 보정을 다시 해야 합니다.",
      min=0, max=10, step=0.1, unit="mm", advanced=True, warn=True),
    I("file_log", True, "bool", "advanced", "기록", "파일 로그",
      "화면 로그를 날짜별 파일에도 남깁니다.", advanced=True),
]

BY_KEY = {it["key"]: it for it in ITEMS}


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
    채우지 않는다 - 일부만 담긴 파일을 가져올 때 공구·레시피가 기본값으로 덮이면 안 된다.

    - park_xy · marker_mm_xy 는 그대로
    - z_measure_mm → 히트건(기본 공구)의 z_work_mm
    - dwell_s → 기본 레시피의 대기 단계 값
    - autofocus/focus("auto") → focus_mode · focus_sweep
    - capture_xy 가 없으면 park_xy (예전 촬영은 파킹 위치에서 찍었다)
    돌려주는 것: (새 dict, 바뀐 것 설명 목록)
    """
    s = copy.deepcopy(raw or {})
    notes = []
    if not has_path(s, "tool.items") and (fill or "z_measure_mm" in s):
        z = s.get("z_measure_mm")
        try:
            z = float(z) if z is not None else 44.0
        except (TypeError, ValueError):
            z = 44.0
        set_path(s, "tool.items", _tools_default(z))
        if "z_measure_mm" in s:
            notes.append("z_measure_mm %g → 히트건 작업 높이" % z)
    if not has_path(s, "recipe.items") and (fill or "dwell_s" in s):
        w = s.get("dwell_s")
        try:
            w = float(w) if w is not None else 5.0
        except (TypeError, ValueError):
            w = 5.0
        set_path(s, "recipe.items", [recipe.default_recipe(w)])
        if "dwell_s" in s:
            notes.append("dwell_s %g → 기본 레시피 대기" % w)
    if "focus_mode" not in s and (fill or "autofocus" in s):
        af = s.get("autofocus", True)
        s["focus_mode"] = "auto" if af in (True, None) else "fixed"
    if isinstance(s.get("focus"), str):
        # "auto" = 열고 나서 스윕으로 찾는다(예전 표기)
        s["focus_sweep"] = True
        s["focus"] = -1
        notes.append("focus \"auto\" → 초점 스윕")
    if fill and not has_path(s, "calib.mode"):
        set_path(s, "calib.mode", "fixed")
        notes.append("calib.mode 없음 → fixed (판 마커 없이 고정 보정)")
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
        if it.get("item") == "float":
            out = []
            for x in v:
                f = _num(x)
                if f is None:
                    return v, "숫자 목록 필요"
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


def _v_seed_grow(key, v, values, errs, warns):
    seed = _num(values.get("stat_seed"))
    if seed is not None and v >= seed:
        errs[key] = "씨앗 임계(%g)보다 작아야 함" % seed


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


def _v_recipes(key, v, values, errs, warns):
    zmax = _num(get_path(values, "limits.z_max_mm")) or 0.0
    names = []
    if not v:
        errs[key] = "레시피 1개 이상"
        return
    for i, r in enumerate(v):
        p = "%s[%d]" % (key, i)
        e, w = recipe.validate_recipe(r, values, zmax, prefix=p)
        errs.update(e)
        warns.extend(w)
        nm = str((r or {}).get("name") or "").strip() if isinstance(r, dict) else ""
        if nm and nm in names:
            errs[p + ".name"] = "이름 중복"
        names.append(nm)
    if len(v) > 30:
        errs[key] = "레시피 30개 이내"


def _v_recipe_active(key, v, values, errs, warns):
    names = [str((r or {}).get("name") or "").strip()
             for r in get_path(values, "recipe.items") or [] if isinstance(r, dict)]
    if v not in names:
        errs[key] = "목록에 없는 레시피"


VALIDATORS = {
    "xy_in_limits": _v_xy_in_limits, "seed_grow": _v_seed_grow, "tol_warn": _v_tol_warn,
    "head_id": _v_head_id, "jog_steps": _v_jog_steps, "touch_max": _v_touch_max,
    "markers": _v_markers, "tools": _v_tools, "tool_active": _v_tool_active,
    "relays": _v_relays, "recipes": _v_recipes, "recipe_active": _v_recipe_active,
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
