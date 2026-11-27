"""로그(app/logs.py · Controller): 번호 넘김 · 쓰기 실패 경보 · 버전 줄 · error.log(traceback) · command.log · 설정 변경 · 측정 시작 · 조건 · 샘플 끝 요약 ·
로그 묶음(log_bundle) · 송풍 꺼짐 예약이 끝나는 순간의 가짜 "히트건 끄기 실패" · "릴레이 꺼짐 감지" 정리."""

import threading
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import logs as logs_mod
from app.config import BY_KEY
from app.devices.stage import StageError
from app.jobs.run import DONE, RunAbort
from app.safety import Rejected
from app.server import create_app
from tests.conftest import FakeClock, wait_for

FAN, EL = 0, 9                                                       # 시험용 보드 릴레이 자리(R1 송풍 · R10 열선)


def _today_dir(root):
    return root / "logs" / time.strftime("%Y-%m-%d")


def _read(path):
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 저장 방식
# ---------------------------------------------------------------------------
def test_rotation_keeps_old_files(tmp_path, monkeypatch):
    monkeypatch.setattr(logs_mod, "MAX_BYTES", 200)
    monkeypatch.setattr(logs_mod, "_version", "abc1234 · 2026-11-26")
    lg = logs_mod.Logs(tmp_path)
    for i in range(30):
        lg.serial("->", "줄 %03d %s" % (i, "x" * 40))
    d = _today_dir(tmp_path)
    olds = sorted(d.glob("serial.*.log"))
    assert len(olds) >= 2, "번호 넘김이 이전 파일을 덮어쓰면 .1 하나만 남는다: %s" % olds
    text = "".join(_read(p) for p in olds) + _read(d / "serial.log")
    for i in range(30):
        assert "줄 %03d" % i in text, "줄 %d 이 없어졌다" % i


def test_app_log_first_line_and_start_line_have_version(tmp_path, monkeypatch):
    monkeypatch.setattr(logs_mod, "_version", "abc1234 · 2026-11-26")
    lg = logs_mod.Logs(tmp_path)
    lg.start_line(tmp_path)
    lines = _read(_today_dir(tmp_path) / "app.log").splitlines()
    assert "프로그램 버전 · abc1234 · 2026-11-26" in lines[0], lines[:2]
    assert "프로그램 시작 · 버전 abc1234 · 2026-11-26 · Python " in lines[1] and "데이터 폴더 %s" % tmp_path in lines[1], lines[:2]


def test_version_without_git_reads_dot_git(tmp_path, monkeypatch):
    g = tmp_path / ".git"
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/v3\n", encoding="utf-8")
    (g / "refs" / "heads" / "v3").write_text("1234567890abcdef\n", encoding="utf-8")
    monkeypatch.setattr(logs_mod, "_version", None)
    monkeypatch.setattr(logs_mod, "_git", lambda *a: None)
    monkeypatch.setattr(logs_mod, "ROOT", tmp_path)
    assert logs_mod.version_text() == "1234567 · 날짜 모름"
    monkeypatch.setattr(logs_mod, "_version", None)
    monkeypatch.setattr(logs_mod, "ROOT", tmp_path / "없음")
    assert logs_mod.version_text() == "버전 모름"


def test_write_failure_raises_and_clears_alarm(make_ctrl, tmp_path):
    ctrl = make_ctrl(connect=False)
    good = ctrl.logs.root
    blocker = tmp_path / "막힘"
    blocker.write_text("파일", encoding="utf-8")                     # 폴더 자리에 파일 - mkdir 이 실패한다
    ctrl.logs.root = blocker / "logs"
    ctrl.logs.app("이 줄은 못 쓴다")
    assert ctrl.state.has_alarm("log_fail") and ctrl.logs.fail_n >= 1 and ctrl.logs.fail_why
    a = next(x for x in ctrl.state.get("alarms") if x["code"] == "log_fail")
    assert a["text"].startswith("로그 저장 실패 · "), a
    ctrl.logs.root = good
    ctrl.logs.app("다시 써진다")
    assert not ctrl.state.has_alarm("log_fail")


def test_exception_goes_to_error_log_with_traceback(tmp_path, monkeypatch):
    monkeypatch.setattr(logs_mod, "_version", "abc1234")
    lg = logs_mod.Logs(tmp_path)
    try:
        raise ValueError("시험 예외")
    except ValueError:
        lg.exc("시험 자리")
    text = _read(_today_dir(tmp_path) / "error.log")
    assert "[시험 자리]" in text and "Traceback (most recent call last)" in text and "ValueError: 시험 예외" in text, text


def test_thread_excepthook_goes_to_error_log(tmp_path, monkeypatch):
    monkeypatch.setattr(logs_mod, "_version", "abc1234")
    lg = logs_mod.Logs(tmp_path)
    old_sys, old_th = __import__("sys").excepthook, threading.excepthook
    try:
        lg.install_hooks()
        t = threading.Thread(target=lambda: 1 / 0, name="시험-스레드")
        t.start()
        t.join()
    finally:
        __import__("sys").excepthook, threading.excepthook = old_sys, old_th
    text = _read(_today_dir(tmp_path) / "error.log")
    assert "스레드 시험-스레드" in text and "ZeroDivisionError" in text, text


# ---------------------------------------------------------------------------
# command.log · 설정 변경
# ---------------------------------------------------------------------------
def test_command_log_done_rejected_error(make_ctrl):
    ctrl = make_ctrl(meters=False)
    ctrl.run_command("relay_on", {"n": 2})
    with pytest.raises(Rejected):
        ctrl.run_command("relay_on", {"n": 1})                        # 송풍 릴레이는 개별 버튼으로 안 켠다
    ctrl.relays_off = lambda: 1 / 0                                  # 예상 못 한 오류
    with pytest.raises(ZeroDivisionError):
        ctrl.run_command("relays_off")
    with TestClient(create_app(ctrl)) as c:
        assert c.put("/api/settings", json={"meter_volt": 0.2}).json() == {"ok": True}
        assert c.put("/api/settings", json={"meter_volt": 999}).status_code == 400
    lines = _read(_today_dir(ctrl.cfg.data_path()) / "command.log").splitlines()
    assert any(" relay_on {'n': 2} · 완료 · " in ln for ln in lines), lines
    assert any(" relay_on {'n': 1} · 거절 · 히트건 릴레이" in ln for ln in lines), lines
    assert any(" relays_off - · 오류 · ZeroDivisionError · " in ln for ln in lines), lines
    assert any("PUT /api/settings {'meter_volt': 0.2} · 완료" in ln for ln in lines), lines
    assert any("PUT /api/settings {'meter_volt': 999} · 거절 · " in ln for ln in lines), lines
    assert all(ln.rstrip().endswith(" ms") for ln in lines), lines


def test_settings_change_line(make_ctrl):
    ctrl = make_ctrl(connect=False)
    old = ctrl.cfg["meter_volt"]
    ctrl.update_settings({"meter_volt": 0.2, "run_start_c": 28.0})
    line = next(ln for ln in ctrl.logs.recent if "설정 변경 · " in ln)
    assert "%s %g → 0.2 V" % (BY_KEY["meter_volt"].label, old) in line and "%s " % BY_KEY["run_start_c"].label in line and "→ 28 °C" in line, line


# ---------------------------------------------------------------------------
# 측정 시작 · 조건 · 샘플 끝 요약 · 접촉 줄의 샘플 이름
# ---------------------------------------------------------------------------
HOLD_KW = dict(time_scale=200.0, run_heater_use=False, run_hold_s=5.0, run_interval_s=0.5, temp_read_s=0.1, meter_avg_count=1, meter_nplc=0.1)


def test_run_start_condition_and_sample_end_lines(ready_ctrl):
    ctrl = ready_ctrl(**HOLD_KW)
    ctrl.run_command("samples_find")
    use = [s for s in ctrl.prep.samples if s["use"]]
    for s in use[1:]:
        ctrl.run_command("sample_use", {"id": s["id"], "use": False})
    ctrl.run_command("run_start")
    assert wait_for(lambda: not ctrl.runner.active, 60)
    it = ctrl.runner.items[use[0]["id"]]
    assert it["status"] == DONE, (it["status"], it["reason"])
    rec = ctrl.logs.recent
    start = next(ln for ln in rec if "측정 시작 · " in ln)
    assert "측정 시작 · 측정 시작 · 묶음 " in start and it["name"] in start and "조건 지금 설정" in start, start
    cond = next(ln for ln in rec if "측정 조건 · " in ln)
    assert "0.10 V" in cond and "히트건 사용 안 함 · 기록 5 s" in cond and "접촉 높이 " in cond, cond
    end = next(ln for ln in rec if "샘플 %s 끝 · " % it["name"] in ln)
    assert "· 완료 · 점 %d(가열 0) · 온도 " % len(it["points"]) in end and "저항 " in end and "한도 걸림 0점" in end and "걸린 시간 " in end, end
    assert any("샘플 %s · 접촉 · 고정 높이 Z " % it["name"] in ln for ln in rec), [ln for ln in rec if "접촉" in ln]
    ctrl.run_command("run_one", {"id": use[0]["id"]})                # 기록이 있는 샘플 다시: 버튼 이름 · 조건 출처
    assert wait_for(lambda: not ctrl.runner.active, 60)
    again = [ln for ln in ctrl.logs.recent if "측정 시작 · " in ln][-1]
    assert "측정 시작 · 이 샘플 다시 측정 · " in again and "조건 기록의 조건" in again, again


# ---------------------------------------------------------------------------
# 로그 묶음
# ---------------------------------------------------------------------------
def test_log_bundle_contents(make_ctrl):
    ctrl = make_ctrl(connect=False, log_bundle_days=3)
    root = ctrl.cfg.data_path()
    d0, d1, d2 = "2000-01-01", "2000-01-02", "2000-01-03"           # 오늘(시험 중 app.log 가 생긴 날) + d1 · d2 가 최근 3일 · d0 는 빠진다
    for d in (d0, d1, d2):
        (root / "logs" / d).mkdir(parents=True)
        (root / "logs" / d / "app.log").write_text("로그 %s\n" % d, encoding="utf-8")
    m = root / d2 / "101010_1"
    (m / "이전_101500").mkdir(parents=True)
    (m / "1-1_0.10V_0.dat").write_text("Temp   Resistance   \r\n", encoding="ascii")
    (m / "이전_101500" / "1-1_0.10V_0.dat").write_text("old", encoding="ascii")
    (m / "plate.jpg").write_bytes(b"\xff\xd8jpg")
    (m / "graph_1.png").write_bytes(b"png")
    (root / "heater_log").mkdir()
    (root / "heater_log" / "20000103_101010.csv").write_text("a\n", encoding="utf-8")
    (root / "heater_log" / "20000101_101010.csv").write_text("a\n", encoding="utf-8")
    (root / "temp_test" / "20000102_090000_맞추기").mkdir(parents=True)
    (root / "temp_test" / "20000102_090000_맞추기" / "temp.csv").write_text("a\n", encoding="utf-8")
    (root / "temp_test" / "20000102_090000_맞추기" / "graph.png").write_bytes(b"png")
    (root / "temp_test" / "index.csv").write_text("a\n", encoding="utf-8")
    (root / "calib").mkdir(exist_ok=True)
    (root / "calib" / "calib.json").write_text("{}", encoding="utf-8")
    r = ctrl.run_command("log_bundle")
    assert r["days"] == 3 and r["bytes"] > 0
    with zipfile.ZipFile(r["path"]) as z:
        names = set(z.namelist())
    assert "logs/%s/app.log" % d1 in names and "logs/%s/app.log" % d2 in names and "logs/%s/app.log" % d0 not in names, names
    assert "%s/101010_1/1-1_0.10V_0.dat" % d2 in names and "%s/101010_1/이전_101500/1-1_0.10V_0.dat" % d2 in names, names
    assert not any(n.endswith((".jpg", ".jpeg", ".png")) for n in names), names
    assert "heater_log/20000103_101010.csv" in names and "heater_log/20000101_101010.csv" not in names
    assert "temp_test/20000102_090000_맞추기/temp.csv" in names and "temp_test/index.csv" in names and "calib/calib.json" in names
    assert "version.txt" in names
    assert any("로그 묶음 저장 · " in ln and "· 3일" in ln for ln in ctrl.logs.recent)


def test_log_bundle_allowed_while_running():
    from app.control import RUN_ALLOWED, TOUR_ALLOWED
    assert "log_bundle" in RUN_ALLOWED and "log_bundle" in TOUR_ALLOWED


# ---------------------------------------------------------------------------
# 송풍 꺼짐 예약이 끝나는 순간: 가짜 "히트건 끄기 실패" 가 나지 않는다 · 진짜 실패는 경보
# ---------------------------------------------------------------------------
def _pending(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    ctrl.run_command("heater_on")
    ctrl.run_command("heater_off")                                   # 열선 끔 · 송풍 30초 뒤
    assert ctrl.heater_fan_pending() and ctrl.fake_fw.relay[FAN]
    ctrl.runner.cond = {"run_interval_s": 0.5}
    return ctrl, clock


def test_wait_heater_off_does_not_abort_when_fan_turns_off_meanwhile(make_ctrl):
    ctrl, clock = _pending(make_ctrl)
    real_check = ctrl.check_heater
    ctrl.check_heater = lambda now=None: None                        # 기다리는 쪽 · 감시 스레드는 끄지 않는다 - 끄는 것은 아래 스레드 하나
    relay = ctrl.stage.relay

    def slow(n, on, gen, prio=5):                                    # 송풍 끄기에 지연: 예약은 지워졌고 릴레이는 아직 켜진 동안
        if n == FAN + 1 and not on:
            time.sleep(0.5)
        return relay(n, on, gen, prio=prio)
    ctrl.stage.relay = slow
    clock.advance(40.0)                                              # 꺼질 시각이 지났다
    box = {}

    def wait():
        try:
            ctrl.runner._wait_heater_off(ctrl.safety.generation)
        except Exception as e:                                       # noqa: BLE001
            box["err"] = e
    m = threading.Thread(target=wait)
    m.start()
    time.sleep(0.05)
    t = threading.Thread(target=real_check)
    t.start()
    t.join(5)
    m.join(5)
    assert not m.is_alive() and "err" not in box, box
    assert not ctrl.fake_fw.relay[FAN] and not ctrl.state.has_alarm("heater_off_fail")


def test_off_state_is_read_at_once(make_ctrl):
    ctrl, clock = _pending(make_ctrl)
    relay = ctrl.stage.relay
    seen = []

    def slow(n, on, gen, prio=5):
        if n == FAN + 1 and not on:
            time.sleep(0.3)
        return relay(n, on, gen, prio=prio)
    ctrl.stage.relay = slow
    clock.advance(40.0)
    t = threading.Thread(target=ctrl._fan_off_due)
    t.start()
    t_end = time.monotonic() + 1.0
    while t.is_alive() and time.monotonic() < t_end:
        seen.append(ctrl.heater_off_state()[:2])
    t.join(5)
    assert (True, False) not in seen, "켜짐 · 예약 없음(끄기 실패로 보이는 상태)이 보였다: %s" % set(seen)


def test_real_fan_off_failure_still_alarms(make_ctrl):
    ctrl, clock = _pending(make_ctrl)
    relay = ctrl.stage.relay

    def refuse(n, on, gen, prio=5):
        if n == FAN + 1 and not on:
            raise StageError("보드 응답 없음")
        return relay(n, on, gen, prio=prio)
    ctrl.stage.relay = refuse
    clock.advance(40.0)
    ctrl._fan_off_due()
    assert ctrl.state.has_alarm("heater_off_fail") and ctrl.fake_fw.relay[FAN]
    ctrl.check_heater = lambda now=None: None
    with pytest.raises((RunAbort, Rejected)):                        # 끄기를 다시 해도 거절(Rejected) · 예약 없이 켜짐(RunAbort) - 어느 쪽이든 멈춘다
        ctrl.runner._wait_heater_off(ctrl.safety.generation)
    assert ctrl.fake_fw.relay[FAN]


# ---------------------------------------------------------------------------
# "히트건 끔 · 릴레이 꺼짐 감지" 는 프로그램이 끄지 않았을 때만
# ---------------------------------------------------------------------------
def _off_lines(ctrl):
    return [ln for ln in ctrl.logs.recent if "히트건 끔" in ln or "히트건 즉시 끔" in ln]


def test_program_off_logs_reason_once_without_detect_line(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    ctrl.run_command("heater_on")
    ctrl.run_command("heater_element", {"on": False})                # 열선만 끔(송풍 켜짐 · 예약 없음)
    clock.advance(40.0)
    n0 = len(ctrl.logs.recent)
    ctrl.run_command("heater_off")                                   # 송풍 끌 시각이 지났다 - 송풍도 바로 끔
    new = _off_lines(ctrl)[-3:]
    assert not any("릴레이 꺼짐 감지" in ln for ln in ctrl.logs.recent[n0:]), ctrl.logs.recent[n0:]
    assert sum(1 for ln in ctrl.logs.recent[n0:] if "히트건 끔 · 수동 · 송풍 끔" in ln) == 1, new
    ctrl.run_command("heater_on")
    n0 = len(ctrl.logs.recent)
    ctrl.run_command("heater_off_now")
    assert not any("릴레이 꺼짐 감지" in ln for ln in ctrl.logs.recent[n0:]), ctrl.logs.recent[n0:]
    assert sum(1 for ln in ctrl.logs.recent[n0:] if "히트건 즉시 끔" in ln) == 1


def test_run_stop_logs_no_detect_line(ready_ctrl):
    from tests.conftest import SEARCH
    ctrl = ready_ctrl(time_scale=200.0, **SEARCH)
    ctrl.run_command("samples_find")
    use = [s for s in ctrl.prep.samples if s["use"]]
    for s in use[1:]:
        ctrl.run_command("sample_use", {"id": s["id"], "use": False})
    n0 = len(ctrl.logs.recent)
    ctrl.run_command("run_start")
    assert wait_for(lambda: ctrl.runner.stage == 3 and ctrl.fake_fw.relay[FAN], 60), "가열까지 가지 못했다"
    ctrl.run_command("run_stop")
    assert wait_for(lambda: not ctrl.runner.active, 60)
    assert wait_for(lambda: not ctrl.fake_fw.relay[FAN], 10)
    new = ctrl.logs.recent[n0:]
    assert not any("릴레이 꺼짐 감지" in ln for ln in new), [ln for ln in new if "히트건" in ln]
    assert sum(1 for ln in new if ln.split("] ", 1)[-1].startswith("히트건 끔 · 측정")) == 1, [ln for ln in new if "히트건" in ln]
