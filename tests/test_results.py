"""결과 탭(app/results.py · app/jobs/analysis.py): 기록 목록 · 기록 하나 · 점 · 전이 온도와 저항 비 · 그림 저장 · NAS 다시 복사 · 서버 길.
가짜 장비로 2개를 끝까지 재고, 저장된 파일에서 다시 읽은 값이 가짜 모델(sim/world.py 의 샘플 값)과 맞는지 본다."""

import json
import math
import os

import pytest
from fastapi.testclient import TestClient

from app import results
from app.jobs import analysis
from app.safety import Rejected
from app.server import create_app
from tests.conftest import wait_for

FAST = 200.0
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _sig(x):
    return 1.0 / (1.0 + math.exp(-x))


def _measured(ready_ctrl, tmp_path, n=2, **cfg):
    ctrl = ready_ctrl(time_scale=FAST, **cfg)
    ctrl.run_command("samples_find")
    use = [s for s in ctrl.prep.samples if s["use"]]
    for s in use[n:]:
        ctrl.run_command("sample_use", {"id": s["id"], "use": False})
    ctrl.run_command("run_start")
    return ctrl, [s["id"] for s in use[:n]]


def _model(ctrl, sid):
    """그 샘플 자리에 있는 가짜 세계의 샘플(전이 온도 · 저항 값)."""
    s = next(x for x in ctrl.prep.samples if x["id"] == sid)
    w = ctrl.world
    k = min(range(len(w.samples)), key=lambda i: abs(w.samples[i]["px"] - s["plate"][0]) + abs(w.samples[i]["py"] - s["plate"][1]))
    return w.samples[k]


# ---------------------------------------------------------------------------
def test_analysis_matches_model_curve():
    """모델 곡선을 그대로 넣으면(온도계 지연 없음) 전이 온도 · 저항 비가 모델 값과 맞는다."""
    t_up, t_down, width, r_hi, r_lo = 66.4, 58.3, 1.6, 3.45e5, 2.2e2
    lg = lambda m: math.log10(r_hi) * (1 - m) + math.log10(r_lo) * m                       # noqa: E731
    heat = [(25 + 0.5 * i, 10 ** lg(_sig((25 + 0.5 * i - t_up) / width)), 0) for i in range(125)]            # 25 → 87 °C
    cool = [(87 - 0.5 * i, 10 ** lg(_sig((87 - 0.5 * i - t_down) / width)), 1) for i in range(1, 115)]       # 87 → 30 °C
    a = analysis.analyze(heat + cool)
    assert abs(a["t_heat"] - t_up) < 0.5 and abs(a["t_cool"] - t_down) < 0.5
    assert abs(a["hyst"] - (t_up - t_down)) < 1.0
    assert a["t_max"] == 87.0 and a["n"] == len(heat) + len(cool)
    assert abs(a["ratio"] - r_hi / r_lo) < 0.01 * r_hi / r_lo
    assert a["r_start"] == heat[0][1] and a["r_peak"] == heat[-1][1]
    # 점이 모자라거나 없으면 값을 지어내지 않는다
    assert analysis.analyze([])["ratio"] is None
    assert analysis.analyze(heat[:4])["t_heat"] is None


def test_results_from_a_finished_run(ready_ctrl, tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    ctrl, ids = _measured(ready_ctrl, tmp_path, nas_dir=str(nas))
    assert wait_for(lambda: not ctrl.runner.active, 40), "측정이 끝나지 않았다"
    data = ctrl.cfg.data_path()
    run = ctrl.runner

    # 기록 목록
    lst = results.list_records(data)
    assert len(lst) == 1
    rid = lst[0]["id"]
    assert rid == "%s/%s" % (run.folder.parent.name, run.folder.name) == ctrl.state.get("run", "record")
    assert (lst[0]["n"], lst[0]["done"], lst[0]["state"], lst[0]["batch"]) == (2, 2, "done", "1")

    # 기록 하나: 파일에서 다시 읽은 요약이 가짜 모델 값과 맞는다
    rec = results.load_record(data, rid)
    assert [s["id"] for s in rec["samples"]] == ids and all(s["status"] == "끝남" for s in rec["samples"])
    assert {"run.json", "summary.csv", "plate.jpg"} <= set(rec["files"])
    assert rec["cond"]["run_heater_off_c"] == ctrl.cfg["run_heater_off_c"] and rec["nas"]["ok"] is True
    for s in rec["samples"]:
        m, a = _model(ctrl, s["id"]), s["sum"]
        assert abs(a["ratio"] - m["r_hi"] / m["r_lo"]) < 0.02 * m["r_hi"] / m["r_lo"], "저항 비"
        assert abs(a["r_peak"] - m["r_lo"]) < 0.01 * m["r_lo"], "최고 온도 저항 = 금속 쪽 저항"
        # 온도계는 샘플보다 늦게 따라간다 → 가열 때는 모델보다 조금 낮게, 냉각 때는 조금 높게 보인다(2.5 °C 안)
        assert m["t_up"] - 2.5 < a["t_heat"] <= m["t_up"] + 0.5, (a["t_heat"], m["t_up"])
        assert m["t_down"] - 0.5 <= a["t_cool"] < m["t_down"] + 2.5, (a["t_cool"], m["t_down"])
        assert a["hyst"] == round(a["t_heat"] - a["t_cool"], 2) > 0
        assert a["t_max"] >= ctrl.cfg["run_heater_off_c"], "히트건을 끈 뒤 더 오른 구간까지 들어 있다"
        live = run.items[s["id"]]["sum"]                              # 측정 때 낸 요약과 같다(화면용 점은 온도를 0.001 °C 로, 파일은 유효 숫자 8자리로 둔다)
        assert a["n"] == live["n"] and abs(a["t_max"] - live["t_max"]) <= 0.01
        assert all(abs(a[k] - live[k]) <= 1.0 for k in ("t_heat", "t_cool")), "전이 온도는 많아야 이웃 점 하나 차이"
        assert all(abs(a[k] - live[k]) <= 1e-6 * live[k] for k in ("r_start", "r_peak", "ratio"))

    # 점: 파일에서 읽은 것이 측정 때 받은 것과 같다(가열 0 → 냉각 1 한 번만 바뀐다)
    for sid in ids:
        p = results.load_points(data, rid, sid)
        got, want = p["points"], run.items[sid]["points"]
        assert p["name"] == run.items[sid]["name"] and len(got) == len(want) > 50
        assert [x[2] for x in got] == [x[2] for x in want]
        phases = [x[2] for x in got]
        assert phases[0] == 0 and phases[-1] == 1 and sum(1 for i in range(1, len(phases)) if phases[i] != phases[i - 1]) == 1
        assert all(abs(g[0] - w[0]) <= 1e-3 and abs(g[1] - w[1]) <= 1e-6 * w[1] for g, w in zip(got, want))
    with pytest.raises(Rejected):
        results.load_points(data, rid, 9999)

    # 없는 기록 · 데이터 폴더 밖을 가리키는 id 는 거절
    for bad in ("", "2026-11-11", "../" + run.folder.name, "2026-11-11/..", rid + "/..", "2000-01-01/none"):
        with pytest.raises(Rejected):
            results.load_record(data, bad)

    # 그림 저장: 그 기록 폴더에 PNG, 같은 이름이 있으면 덮어쓰지 않는다
    p1 = results.save_png(data, rid, PNG)["path"]
    p2 = results.save_png(data, rid, PNG)["path"]
    assert p1 != p2 and all(os.path.dirname(p) == str(run.folder) and open(p, "rb").read() == PNG for p in (p1, p2))
    with pytest.raises(Rejected):
        results.save_png(data, rid, b"not a png")

    # NAS 다시 복사: 지워진 파일이 다시 생기고 run.json 의 상태가 고쳐진다
    dst = rec["nas"]["dir"]
    dats = sorted(f for f in rec["files"] if f.endswith(".dat"))
    assert sorted(os.listdir(dst)) == dats, "NAS 에는 .dat 만"
    for f in dats[:2]:
        os.remove(os.path.join(dst, f))
    assert ctrl.run_command("result_nas_copy", {"id": rid}) == {"copied": len(dats)}
    assert sorted(os.listdir(dst)) == dats
    assert json.loads((run.folder / "run.json").read_text(encoding="utf-8"))["nas"]["ok"] is True
    # NAS 에 못 쓰면 거절 + 남은 파일을 적어 둔다(측정 기록은 그대로)
    meta = json.loads((run.folder / "run.json").read_text(encoding="utf-8"))
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    meta["nas"]["dir"] = str(blocker / "sub")
    (run.folder / "run.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(Rejected, match="NAS"):
        results.nas_copy(data, rid)
    after = json.loads((run.folder / "run.json").read_text(encoding="utf-8"))["nas"]
    assert after["ok"] is False and sorted(after["pending"]) == dats

    # 폴더 열기: 탐색기가 없는 컴퓨터에서는 경로를 알려 주며 거절, 있으면 그 폴더를 연다
    monkeypatch.delattr(os, "startfile", raising=False)
    with pytest.raises(Rejected, match="폴더"):
        ctrl.run_command("result_open_folder", {"id": rid})
    opened = []
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)
    assert ctrl.run_command("result_open_folder", {"id": rid})["folder"] == str(run.folder.resolve())
    assert opened == [str(run.folder.resolve())]


def test_results_server_and_settings_lock_during_run(ready_ctrl, tmp_path):
    ctrl, ids = _measured(ready_ctrl, tmp_path, n=1)
    with TestClient(create_app(ctrl)) as c:
        # 측정 중에는 설정을 못 바꾼다(측정이 쓰는 값이 도중에 바뀌지 않게)
        r = c.put("/api/settings", json={"run_heater_off_c": 70})
        assert r.status_code == 409 and "측정 중" in r.json()["msg"]
        assert ctrl.cfg["run_heater_off_c"] == 85.0
        assert wait_for(lambda: not ctrl.runner.active, 40)
        assert c.put("/api/settings", json={"run_heater_off_c": 80}).json() == {"ok": True}

        lst = c.get("/api/results").json()
        assert len(lst) == 1 and lst[0]["n"] == 1
        rid = lst[0]["id"]
        rec = c.get("/api/results/record", params={"id": rid}).json()
        assert rec["id"] == rid and rec["samples"][0]["id"] == ids[0] and rec["samples"][0]["sum"]["t_heat"] is not None
        assert rec["cond"]["run_heater_off_c"] == 85.0, "그날 측정 조건은 측정 때 값 그대로"
        pts = c.get("/api/results/points", params={"id": rid, "sample": ids[0]}).json()
        assert len(pts["points"]) == rec["samples"][0]["n"] > 50
        assert c.get("/api/results/record", params={"id": "2000-01-01/none"}).status_code == 404
        assert c.get("/api/results/points", params={"id": rid, "sample": 9999}).status_code == 404

        r = c.post("/api/results/png", params={"id": rid}, content=PNG, headers={"Content-Type": "image/png"}).json()
        assert r["ok"] and os.path.isfile(r["result"]["path"])
        assert c.post("/api/results/png", params={"id": rid}, content=b"zz").json()["ok"] is False
        assert os.path.basename(r["result"]["path"]) in c.get("/api/results/record", params={"id": rid}).json()["files"]


def test_settings_table_for_the_all_settings_page(tmp_path):
    """"설정 값 모두 보기": 항목마다 이름 · 묶음이 있고, 아직 모르는 하드웨어 값에는 "실장비 확인 필요" 표시가 있다."""
    from app.config import Config
    rows = Config(tmp_path / "settings.json").load().describe()
    assert all(r["label"] and r["group"] and "unit" in r and "value" in r for r in rows)
    assert len({r["key"] for r in rows}) == len(rows)
    verify = {r["key"] for r in rows if r["verify"]}
    assert {"cooler_grid", "plate_size_mm", "pin_long_axis"} <= verify
    assert all(r["choices"] for r in rows if r["kind"] == "choice")
