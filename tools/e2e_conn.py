"""e2e_conn.py - 장치 연결 상태 e2e 시나리오 48~51 (tools/e2e.py 가 불러온다). dry · fake-serial 두 모드.

서버는 WAFER2_MISSING 으로 장치를 뺀 채 시작한다(stage · camera · visa) - 실제 포트 · 카메라 · VISA 는 열지 않는다.
  48 아무것도 연결 안 됨 · 49 포트 · 카메라 생김 → 자동 연결(+ 자동 확인 중 비상정지 지연) · 50 스테이지만 없음 · 51 카메라만 없음
화면 검사(깨진 이미지 · 비활성 버튼 · 띠)는 tools/shots_conn.py 가 맡는다.
"""
import time

import e2e
from e2e import case, check

FAST_AUTO = {"WAFER2_AUTO_LINK_S": "1"}
NEVER_AUTO = {"WAFER2_AUTO_LINK_S": "3600"}


def estop_ms(c):
    t0 = time.time()
    code, _ = c.http("/estop", "POST")
    return (time.time() - t0) * 1000.0, code


def band_ids(c):
    return [b["id"] for b in c.state.get("bands", [])]


def band_acts(c, bid):
    return [a["act"] for b in c.state.get("bands", []) if b["id"] == bid for a in b.get("actions", [])]


@case("48 아무것도 연결 안 됨 · 연결됨으로 보이는 칸 없음 · 장치마다 띠 + 다시 연결 · 거절 사유")
def c48(c):
    s = c.pump(1.5)
    dv = s["dev"]
    check(dv["stage"]["state"] == "down" and dv["camera"]["state"] == "down", "스테이지 · 카메라 = 연결 안 됨 (%s / %s)" % (dv["stage"], dv["camera"]))
    check(dv["k2110"]["state"] == "unknown" and dv["k2450"]["state"] == "unknown", "측정 장비 = 확인 안 됨 (연결을 누르기 전)")
    check(s["stage"] is None and s["relays"] is None and s["fw"] is None and s["port"] is None, "스테이지에 딸린 값(위치 · 릴레이 · 펌웨어 · 포트) 없음")
    check(s["camera"]["open"] is False and s["camera"]["size"] is None and s["camera"]["text"] == "—", "카메라에 딸린 값(해상도 · 노출) 없음 (%s)" % s["camera"]["text"])
    check(s["phase"] == "대기", "상태 필은 동작 상태만 (%s)" % s["phase"])
    check(band_ids(c) == ["stage", "camera"], "띠 = 스테이지 · 카메라 한 줄씩 (%s)" % band_ids(c))
    check("reconnect" in band_acts(c, "stage") and "ui_setup" in band_acts(c, "stage"), "스테이지 띠에 [다시 연결] [장비 설정 1]")
    check("cam_reconnect" in band_acts(c, "camera") and "ui_setup" in band_acts(c, "camera"), "카메라 띠에 [다시 연결] [장비 설정 2]")
    check(s["band"] is None and s["alarm"] is None, "경보 띠(비상정지 · 원점)는 없음 - 연결 문제는 장치 띠가 맡는다")
    check(bool(s["start_block"]["why"]) and "\n" not in s["start_block"]["why"], "시작 막는 사유 한 줄 (%s)" % s["start_block"]["why"])
    # 거절 사유
    ok, r = c.send("capture")
    check(not ok and "카메라" in str(r), "촬영 거절 · 카메라 (%s)" % (r,))
    ok, r = c.send("jog", axis="x", mm=1.0)
    check(not ok and "스테이지" in str(r), "조그 거절 · 스테이지 (%s)" % (r,))
    ok, r = c.send("rt_start")
    check(not ok, "R–T 측정 시작 거절 (%s)" % (r,))
    ok, r = c.send("measure_start", condition="접촉 시험", nos=[1], ok_weak=True)
    check(not ok, "샘플 순회 시작 거절 (%s)" % (r,))
    ok, r = c.send("reconnect")
    check(not ok and "포트" in str(r), "[다시 연결] 실패 사유 (%s)" % (r,))
    ok, r = c.send("cam_reconnect")
    check(not ok and "카메라" in str(r), "카메라 [다시 연결] 실패 사유 (%s)" % (r,))
    ok, r = c.send("inst_connect")
    check(not ok and "드라이버" in str(r), "장비 연결 실패 · 드라이버 없음 (%s)" % (r,))
    c.pump(0.5)
    check(band_ids(c) == ["stage", "camera", "k2110", "k2450"], "측정 장비 실패 뒤 띠 넷 (%s)" % band_ids(c))
    check("inst_connect" in band_acts(c, "k2110") and "inst_connect" in band_acts(c, "k2450"), "측정 장비 띠에 [장비 연결]")
    ms, code = estop_ms(c)
    check(code == 200 and ms <= 100, "비상정지 요청 %.0f ms (기준 100 ms 이하 · 스테이지 없음)" % ms)


@case("49 포트 · 카메라가 생기면 자동 연결 · 자동 확인 중에도 비상정지 100 ms 이하")
def c49(c):
    c.pump(1.5)
    check(c.state["dev"]["stage"]["state"] == "down" and c.state["dev"]["camera"]["state"] == "down", "시작 = 둘 다 연결 안 됨")
    # 자동 확인(1 s 간격 시도)이 도는 동안 비상정지 지연
    lat = []
    for _ in range(4):
        time.sleep(0.6)
        lat.append(estop_ms(c)[0])
    check(max(lat) <= 100, "자동 확인 중 비상정지 요청 %s ms (기준 100 ms 이하)" % [round(x) for x in lat])
    ok, r = c.dry("missing", stage=False)
    check(ok, "포트 생김 (%s)" % (r,))
    check(c.wait(lambda s: s["dev"]["stage"]["state"] == "ok", 20, "스테이지 자동 연결"), "스테이지 자동 연결")
    s = c.state
    check(s["stage"] is not None and s["relays"] is not None and s["port"], "연결되면 딸린 값이 채워짐 (포트 %s)" % (s.get("port"),))
    check(s["dev"]["camera"]["state"] == "down" and band_ids(c) == ["camera"], "카메라는 아직 없음 · 띠 하나 (%s)" % band_ids(c))
    check(s["eeprom"] in ("restored", "unsaved", "none", ""), "스테이지 연결 규칙(EEPROM 확인)을 그대로 탐 (%s)" % s["eeprom"])
    ok, r = c.dry("missing", camera=False)
    check(c.wait(lambda s: s["dev"]["camera"]["state"] == "ok", 20, "카메라 자동 연결"), "카메라 자동 연결")
    s = c.state
    check(s["camera"]["open"] and s["camera"]["size"] and "×" in s["camera"]["text"], "카메라 값 채워짐 (%s)" % s["camera"]["text"])
    check(not [b for b in s["bands"] if b["id"] in ("stage", "camera")], "연결되면 띠가 사라짐 (%s)" % band_ids(c))
    ok, r = c.send("capture")
    check(not ok and ("판 영역" in str(r)), "연결 뒤 촬영 명령은 정상 경로(판 영역 필요) (%s)" % (r,))
    # 카메라가 빠졌다가 다시 꽂히면 다시 자동 연결
    c.dry("missing", camera=True)
    check(c.wait(lambda s: s["dev"]["camera"]["state"] in ("down", "retry"), 10, "카메라 빠짐"), "카메라 빠짐 = 연결 안 됨")
    check("camera" in band_ids(c), "카메라 띠 생김")
    c.dry("missing", camera=False)
    check(c.wait(lambda s: s["dev"]["camera"]["state"] == "ok", 20, "카메라 다시 자동 연결"), "카메라 다시 자동 연결")
    ms, code = estop_ms(c)
    check(code == 200 and ms <= 100, "비상정지 요청 %.0f ms" % ms)


@case("50 스테이지만 없음 (카메라 연결) · 띠 하나 · 사진은 정상")
def c50(c):
    s = c.pump(1.5)
    check(band_ids(c) == ["stage"], "띠 = 스테이지 하나 (%s)" % band_ids(c))
    check(s["dev"]["camera"]["state"] == "ok" and s["camera"]["open"] and s["camera"]["size"], "카메라 정상 · 해상도 있음")
    check(s["relays"] is None and s["stage"] is None, "릴레이 · 위치 없음")
    ok, r = c.send("jog", axis="x", mm=1.0)
    check(not ok and "스테이지" in str(r), "조그 거절 (%s)" % (r,))
    code, body = c.http("/preview.jpg")
    check(code in (200, 204), "실시간 요청 응답 (%s)" % code)
    why = s["start_block"]["why"]
    check("스테이지" in why, "시작 사유 = 스테이지 (%s)" % why)


@case("51 카메라만 없음 (스테이지 연결) · 띠 하나 · 이동은 정상")
def c51(c):
    s = c.pump(1.5)
    check(band_ids(c) == ["camera"], "띠 = 카메라 하나 (%s)" % band_ids(c))
    check(s["dev"]["stage"]["state"] == "ok" and s["stage"] is not None and s["relays"] is not None, "스테이지 정상 · 릴레이 값 있음")
    check(s["camera"]["open"] is False and s["camera"]["size"] is None, "카메라 값 없음")
    ok, r = c.send("capture")
    check(not ok and "카메라" in str(r), "촬영은 이동하기 전에 거절 (%s)" % (r,))
    check(c.pump(0.3)["stage"]["moving"] is False, "거절된 촬영이 스테이지를 움직이지 않음")
    ok, r = c.send("snap")
    check(not ok and "카메라" in str(r), "단발 촬영 거절 (%s)" % (r,))
    ok, r = c.send("cam_scan")
    check(ok, "[카메라 찾기] 시작 (%s)" % (r,))
    check(c.wait(lambda s: s.get("cam_scan") and not s["cam_scan"]["running"], 20, "카메라 찾기 끝"), "[카메라 찾기] 끝")
    its = c.state["cam_scan"]["items"]
    check(len(its) == 6 and not any(i["ok"] for i in its), "카메라 없음 → 열리는 번호 없음 (%d개 시도)" % len(its))


for _n, _env in ((48, NEVER_AUTO), (49, FAST_AUTO), (50, NEVER_AUTO), (51, NEVER_AUTO)):
    for _k, _fn in list(e2e.CASES.items()):
        if _k.startswith("%d " % _n):
            _fn.env = dict(_env, WAFER2_MISSING={48: "stage,camera,visa", 49: "stage,camera,visa", 50: "stage,visa", 51: "camera,visa"}[_n])
            _fn.patch = {"sim": {"time_scale": 60.0}}
