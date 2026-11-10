"""e2e_conn.py - 장치 연결 상태 e2e 시나리오 48~51 (tools/e2e.py 가 불러온다). dry · fake-serial 두 모드.

서버는 WAFER2_MISSING 으로 장치를 뺀 채 시작한다(stage · camera · visa) - 실제 포트 · 카메라 · VISA 는 열지 않는다.
  48 아무것도 연결 안 됨 · 49 포트 · 카메라 생김 → 자동 연결(+ 자동 확인 중 비상정지 지연) · 50 스테이지만 없음 · 51 카메라만 없음
화면 검사(깨진 이미지 · 비활성 버튼 · 띠)는 tools/shots_conn.py 가 맡는다.
"""
import time

import e2e
from e2e import case, check

FAST_AUTO = {"WAFER2_AUTO_LINK_S": "1", "WAFER2_AUTO_CAM_S": "1"}
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
    check(band_ids(c) == ["stage", "camera", "inst"], "측정 장비 실패 뒤 띠 셋 · 장비 둘은 한 줄 (%s)" % band_ids(c))
    check("inst_connect" in band_acts(c, "inst"), "측정 장비 띠에 [장비 연결]")
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
    # 연결 안 된 동안 누른 비상정지는 남는다(D2: 이동 계층이 없을 때의 비상정지 → estop.lock → 연결되면 잠금으로 시작). 띠 = 경보(잠금 이어짐) + 카메라
    check(s["dev"]["camera"]["state"] == "down" and band_ids(c) == ["alarm", "camera"], "카메라는 아직 없음 · 띠 = 비상정지 잠금 + 카메라 (%s)" % band_ids(c))
    check(s["stage"]["locked"] and s["alarm"] and "잠금" in s["alarm"]["text"], "연결 안 된 동안 누른 비상정지 → 잠금으로 시작 (%s)" % (s["alarm"] and s["alarm"]["text"]))
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


# ---------------------------------------------------------------------------
# 52~55: 쓰는 중 뽑힘 · 다시 꽂음 · 연결 겹침 · 깜빡임 없음 (dry · fake 두 모드)
# ---------------------------------------------------------------------------
def move_cmds(c):
    from e2e_cases import serial_cmds
    return [x for x in serial_cmds(c) if x.split(" ")[0] in ("g", "gx", "gy", "gz", "mx", "my", "mz", "jx", "jy", "jz", "fz", "home", "sp")]


@case("52 스테이지 쉬는 중 뽑힘 → 통신 끊김(빨강) · 비상정지 100 ms · 다시 꽂으면 자동 연결 · 움직임 0")
def c52(c):
    c.pump(1.5)
    check(c.state["dev"]["stage"]["state"] == "ok", "시작 = 스테이지 연결")
    n0 = len(move_cmds(c))
    # 1) 뽑힘 → 통신 끊김 → 다시 꽂음 → 자동 연결
    c.dry("unplug", device="stage")
    check(c.wait(lambda s: s["alarm"] and s["alarm"]["text"].startswith("통신 끊김"), 15, "통신 끊김"), "통신 끊김 경보")
    b = [x for x in c.state["bands"] if x["id"] == "alarm"]
    check(b and b[0]["err"] is True, "통신 끊김 띠 = 빨강 (%s)" % [x["text"] for x in b])
    check(c.state["dev"]["stage"]["state"] == "down", "스테이지 상태 = 끊김")
    c.dry("replug", device="stage")
    check(c.wait(lambda s: s["dev"]["stage"]["state"] == "ok" and not (s["alarm"] and s["alarm"]["text"].startswith("통신 끊김")), 20, "자동 연결"), "다시 꽂으면 10 s 안에 자동 연결 · 경보 닫힘")
    s = c.state
    check(s["stage"] is not None and s["stage"]["moving"] is False, "연결됨 · 움직이지 않음")
    check(s["eeprom"] in ("restored", "unsaved", "none"), "재부팅 처리(EEPROM %s) 그대로" % s["eeprom"])
    check(("원점" in ((s["band"] or {}).get("text", "")) or s["stage"]["z_ok"]) or s["eeprom"] != "restored", "원점 복원 또는 원점 필요 띠 (%s / %s)" % (s["stage"]["z_ok"], (s["band"] or {}).get("text")))
    check(len(move_cmds(c)) == n0, "그동안 자동으로 움직인 명령 0 (%d → %d)" % (n0, len(move_cmds(c))))
    # 2) 끊긴 동안 비상정지 지연
    c.dry("unplug", device="stage")
    check(c.wait(lambda s: s["alarm"] and s["alarm"]["text"].startswith("통신 끊김"), 15, "통신 끊김 2"), "다시 뽑힘 = 통신 끊김")
    lat = [estop_ms(c)[0]]
    check(lat[0] <= 100, "끊긴 동안 비상정지 요청 %.0f ms (기준 100 ms 이하)" % lat[0])
    c.dry("replug", device="stage")
    check(c.wait(lambda s: s["stage"] is not None and bool(s["alarm"]) and "잠금 이어짐" in s["alarm"]["text"], 20, "자동 연결 2"), "다시 꽂음 → 자동 연결(비상정지 잠금 이어짐 안내)")
    check(c.state["stage"]["locked"] is True, "비상정지 잠금은 이어짐(사람의 회복 동작으로만 풀림)")
    check(len(move_cmds(c)) == n0, "움직인 명령 0")


@case("53 카메라 뽑힘 → 실시간 빈 상태 · 다시 꽂으면 자동으로 열림 · 깨진 이미지 없음")
def c53(c):
    c.pump(1.5)
    check(c.state["dev"]["camera"]["state"] == "ok", "시작 = 카메라 연결")
    c.dry("unplug", device="camera")
    check(c.wait(lambda s: s["dev"]["camera"]["state"] == "down", 10, "카메라 뽑힘"), "카메라 뽑힘 = 연결 안 됨")
    s = c.state
    check(s["camera"]["open"] is False and s["camera"]["size"] is None and "camera" in band_ids(c), "값 없음 · 띠 생김")
    code, body = c.http("/preview.jpg")
    check(code == 204 or not body, "옛 프레임을 계속 주지 않음 (%s)" % code)
    ok, r = c.send("capture")
    check(not ok and "카메라" in str(r), "촬영은 이동 전에 거절 (%s)" % (r,))
    c.dry("replug", device="camera")
    check(c.wait(lambda s: s["dev"]["camera"]["state"] == "ok", 20, "카메라 자동 열림"), "다시 꽂으면 자동으로 열림")
    check("camera" not in band_ids(c) and c.state["camera"]["size"], "띠 사라짐 · 해상도 있음")
    code, body = c.http("/preview.jpg")
    check(code in (200, 204), "실시간 요청 정상 (%s)" % code)


@case("54 연결 겹침: [다시 연결] 여러 번 + 자동 시도가 동시 → 포트 열기 정확히 1번 · 오류 띠 없음")
def c54(c):
    c.pump(1.5)
    check(c.state["dev"]["stage"]["state"] == "down", "시작 = 스테이지 없음")
    c.dry("open_delay", stage=1.5)
    c.dry("replug", device="stage")
    ids = [c.send_nowait("reconnect") for _ in range(4)]
    res = [c.wait_ack(i, 60) for i in ids]
    check(c.wait(lambda s: s["dev"]["stage"]["state"] == "ok", 30, "연결"), "연결됨")
    ok, r = c.dry("counts")
    check(r["stage_opens"] == 1, "포트 열기 정확히 1번 (%s)" % (r,))
    check(all(o for o, _ in res), "겹친 [다시 연결]이 오류로 끝나지 않음 (%s)" % [x[1] for x in res])
    c.pump(2.0)
    check(c.state["alarm"] is None and "stage" not in band_ids(c), "오류 띠 없음 (%s)" % band_ids(c))


@case("55 깜빡임 없음: 아무것도 연결 안 됨 + 카메라 열기 1.5 s + 자동 시도 20 s → 띠 · 머리줄 상태가 한 번도 안 바뀜, 눌렀을 때만 연결 중")
def c55(c):
    c.pump(1.0)
    c.dry("open_delay", camera=1.5)

    def sig(s):
        return (tuple((b["id"], b["text"], b["err"], tuple((a["label"], a.get("busy")) for a in b["actions"])) for b in s["bands"]),
                s["dev"]["stage"]["state"], s["dev"]["camera"]["state"], s["phase"])
    c.pump(3.0)
    base = sig(c.state)
    seen = set()
    t0 = time.time()
    while time.time() - t0 < 20:
        c.pump(0.1)
        seen.add(sig(c.state))
    check(len(seen) == 1 and base in seen, "20 s 동안 한 가지 모양 (%d 가지)" % len(seen))
    ok, cnt = c.dry("counts")
    check(cnt["camera_opens"] >= 3, "그 사이 자동 시도는 계속 돌았다 (카메라 열기 %d 번)" % cnt["camera_opens"])
    check(all(b["err"] is False for b in c.state["bands"]), "장치 연결 안 됨 띠는 주황(err 아님)")
    i = c.send_nowait("cam_reconnect")
    got = c.wait(lambda s: any(b["id"] == "camera" and "연결 중" in b["text"] and any(a.get("busy") for a in b["actions"]) for b in s["bands"]), 5, "연결 중")
    check(got, "[다시 연결]을 눌렀을 때만 '연결 중…'")
    c.wait_ack(i, 20)
    check(c.wait(lambda s: any(b["id"] == "camera" and "연결 안 됨" in b["text"] for b in s["bands"]), 10, "다시 연결 안 됨"), "실패하면 연결 안 됨으로 돌아옴")


for _n, _env in ((52, {"WAFER2_AUTO_LINK_S": "0.5"}), (53, {"WAFER2_AUTO_LINK_S": "0.5", "WAFER2_AUTO_CAM_S": "0.5"}),
                 (54, {"WAFER2_AUTO_LINK_S": "0.3", "WAFER2_MISSING": "stage"}), (55, {"WAFER2_AUTO_LINK_S": "0.5", "WAFER2_AUTO_CAM_S": "0.5", "WAFER2_MISSING": "stage,camera,visa"})):
    for _k, _fn in list(e2e.CASES.items()):
        if _k.startswith("%d " % _n):
            _fn.env = dict(_env)
            _fn.patch = {"sim": {"time_scale": 60.0}}
