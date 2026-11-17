"""이미지 파일 입출력(app/imgio.py) - 한글 경로.

Windows 의 OpenCV 는 경로를 ANSI 코드 페이지로 넘겨서 한글 경로면 cv2.imread 가 오류 없이 None 을 돌려준다(쓰기는 imencode + write_bytes 라 됐다).
그래서 데이터 폴더에 한글이 있으면 다시 켠 뒤 빈 판 사진을 못 읽고 말없이 "사진 한 장" 검출로 넘어갔다.
Linux 에서는 그 실패가 재현되지 않으므로 구조로 막는다: app/ 에 cv2.imread · cv2.imwrite 가 없고, 이미지 파일은 imgio 로만 읽고 쓴다.
"""

import ast
from pathlib import Path

import numpy as np
import pytest

from app import imgio
from tests.conftest import alarm_lines

APP = Path(__file__).resolve().parent.parent / "app"
KO_DIR = ("01_장비", "자동 측정", "data_v3")                          # 측정 PC 의 실제 경로 모양(한글 · 공백)


def test_app_has_no_cv2_imread_or_imwrite():
    """app/ 아래 .py 에 cv2.imread · cv2.imwrite(이름만 가져와 쓰는 것 포함)가 없다 - 주석 · 설명 글은 보지 않는다(코드만)."""
    bad = []
    for p in sorted(APP.rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"), str(p))
        for node in ast.walk(tree):
            name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
            if isinstance(node, ast.ImportFrom):
                name = next((a.name for a in node.names if a.name in ("imread", "imwrite")), None)
            if name in ("imread", "imwrite"):
                bad.append("%s:%d %s" % (p.relative_to(APP.parent), node.lineno, name))
    assert bad == [], "이미지 파일은 app/imgio.py 로만 읽고 쓴다: %s" % bad


def test_roundtrip_in_a_korean_folder_with_spaces(tmp_path):
    folder = tmp_path / "자동 측정" / "빈 판"
    img = (np.arange(48 * 64 * 3, dtype=np.uint32) % 251).astype(np.uint8).reshape(48, 64, 3)
    p = folder / "빈 판 사진.png"
    imgio.write(p, img)                                              # 폴더가 없으면 만든다
    assert p.is_file() and np.array_equal(imgio.read(p), img), "PNG 는 같은 영상을 돌려준다"
    assert [q.name for q in folder.iterdir()] == ["빈 판 사진.png"], "임시 파일이 남지 않는다(원자적 쓰기)"
    j = folder / "판.jpg"
    imgio.write(j, img)
    assert imgio.read(j).shape == img.shape
    imgio.write(p, img[::-1].copy())                                 # 덮어쓰기
    assert np.array_equal(imgio.read(p), img[::-1])
    raw = folder / "그림.png"
    imgio.write_bytes(raw, imgio.encode(img, ".png"))
    assert np.array_equal(imgio.decode(raw.read_bytes()), img)


def test_read_failures_are_errors_not_none(tmp_path):
    folder = tmp_path / "한글 폴더"
    with pytest.raises(imgio.ImageIOError) as e:
        imgio.read(folder / "없음.png")
    assert str(e.value) == "파일 없음"
    folder.mkdir()
    (folder / "깨짐.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 40)
    with pytest.raises(imgio.ImageIOError) as e:
        imgio.read(folder / "깨짐.png")
    assert str(e.value) == "이미지 해석 실패"
    (folder / "빈 파일.png").write_bytes(b"")
    with pytest.raises(imgio.ImageIOError):
        imgio.read(folder / "빈 파일.png")
    with pytest.raises(imgio.ImageIOError):
        imgio.encode(np.zeros((0, 0, 3), np.uint8))


def _restart(make_ctrl, ctrl, data_dir):
    """프로그램을 다시 켠 것처럼: 앞 Controller 를 닫고 같은 설정 파일 · 데이터 폴더로 새로 만든다(읽어 둔 빈 판 사진은 메모리에 없다)."""
    ctrl.shutdown()
    new = make_ctrl(scale=0.02, camera=True, camera_width=1280, camera_height=960, data_dir=data_dir)
    new.lab.read_dt = 0.002
    assert new.prep._empty is None
    return new


def test_empty_plate_is_read_back_from_a_korean_data_folder_after_restart(ready_ctrl, make_ctrl, tmp_path):
    data_dir = str(tmp_path.joinpath(*KO_DIR))
    ctrl = ready_ctrl(data_dir=data_dir)                             # 초기 설정 값을 채우며 빈 판 촬영까지(fill_prep)
    path = ctrl.calib.empty_path
    assert "자동 측정" in str(path) and path.is_file()
    new = _restart(make_ctrl, ctrl, data_dir)
    assert new.calib.d["empty"] and new.prep.steps()[3]["done"]
    r = new.run_command("samples_find")
    assert new.prep.find_info["path"] == "빈 판 비교", "다시 켠 뒤에도 빈 판 사진과 비교한다: %s" % r["notes"]
    assert r["n"] == 9 and not new.state.has_alarm("empty_read"), alarm_lines(new)
    assert new.prep.empty_frame() is not None and new.prep.steps()[3]["done"]


def _damaged_start(make_ctrl, ctrl, data_dir, damage):
    """빈 판 파일을 지우거나(delete) 깨뜨린 뒤(corrupt) 다시 켠다."""
    path = ctrl.calib.empty_path
    if damage == "delete":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes()[:200])                    # 앞부분만 남은 깨진 파일
    return _restart(make_ctrl, ctrl, data_dir)


def test_unreadable_empty_plate_raises_one_notice_and_falls_back_to_one_photo(ready_ctrl, make_ctrl, tmp_path):
    data_dir = str(tmp_path.joinpath(*KO_DIR))
    ctrl = ready_ctrl(data_dir=data_dir)
    path = ctrl.calib.empty_path
    # 깨진 파일: 주의 · 사진 한 장 검출 · 로그에 경로와 까닭
    new = _damaged_start(make_ctrl, ctrl, data_dir, "corrupt")
    new.run_command("samples_find")
    assert new.prep.find_info["path"] == "사진 한 장" and new.state.has_alarm("empty_read"), alarm_lines(new)
    assert [ln for ln in new.logs.recent if "빈 판 사진 읽기 실패" in ln and str(path) in ln and "이미지 해석 실패" in ln]
    # 지운 파일: 같은 규칙(아래에서 자세히 본다)
    damage = "delete"
    new = _damaged_start(make_ctrl, new, data_dir, damage)
    r = new.run_command("samples_find")
    assert r["n"] == 9 and new.prep.find_info["path"] == "사진 한 장", "검출은 사진 한 장으로 계속한다"
    al = [a for a in new.state.get("alarms") if a["code"] == "empty_read"]
    assert len(al) == 1 and al[0]["text"] == "빈 판 사진 읽기 실패 · 초기 설정 4 재촬영 필요" and al[0]["level"] == "주의", alarm_lines(new)
    assert not any("빈 판 사진 없음" in n for n in r["notes"]), "있다고 적혀 있는 사진을 '없음' 으로 알리지 않는다"
    logs = [ln for ln in new.logs.recent if "빈 판 사진 읽기 실패" in ln]
    assert len(logs) == 1 and str(path) in logs[0] and ("파일 없음" if damage == "delete" else "이미지 해석 실패") in logs[0], logs
    assert new.prep.steps()[3] == {"done": False, "text": "읽기 실패 · 재촬영 필요"}
    # 한 번만 띄운다: 지운 뒤 다시 검출해도 다시 뜨지 않는다(같은 사진을 다시 읽지 않는다)
    new.run_command("alarm_clear", {"code": "empty_read"})
    new.run_command("samples_find")
    assert not new.state.has_alarm("empty_read") and len([ln for ln in new.logs.recent if "빈 판 사진 읽기 실패" in ln]) == 1
    # 다시 촬영하면 풀린다
    new.world.set_loaded(False)
    new.run_command("empty_take")
    new.world.set_loaded(True)
    assert path.is_file() and new.prep.steps()[3]["done"] and not new.state.has_alarm("empty_read")
    new.run_command("samples_find")
    assert new.prep.find_info["path"] == "빈 판 비교"
