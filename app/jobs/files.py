"""files.py - 측정 파일(기존 LabVIEW 프로그램과 같은 형식)과 NAS 복사. v2 backend/rt.py 의 함수를 그대로 옮겼다.

  File name = 샘플 이름 + "_"   (LabVIEW 의 File name 은 "1-1_" 처럼 밑줄까지다)
  회차 파일  <File name><전압 소수 2자리>V_<회차−1>.dat     예) 1-1_1.00V_0.dat   (회차 번호는 0 부터)
  합친 파일  <File name><전압>V_merged.dat                 예) 1-1_1.00V_merged.dat (머리글 없음)
  회차 파일 첫 줄 "Temp   Resistance   "(Temp 뒤 공백 3 · Resistance 뒤 공백 3 · 탭 없음), 값은 LabVIEW %.7E(지수에 0 을 채우지 않음,
  예 2.3691786E+1), 온도와 저항 사이는 탭, 줄끝은 CRLF(마지막 줄도).
  NAS 폴더 = 기본 폴더 / 연도 / YYYYMMDD(측정 시작 날짜) / 묶음. .dat 파일만, 별도 스레드로 복사한다(못 쓰면 주의만 · 측정은 계속).
"""

import ntpath
import os
import queue
import shutil
import threading
import time

HEADER = "Temp   Resistance   "          # 회차 파일 첫 줄(Temp 뒤 공백 3 · Resistance 뒤 공백 3 · 탭 없음). merged 파일에는 머리글이 없다.
CRLF = "\r\n"
BAD_NAME_CHARS = '\\/:*?"<>|'


def file_stem(file_name, volt):
    """[File name][Set voltage 소수 2자리]V_  (예: 1-1_1.00V_)"""
    return "%s%.2fV_" % (file_name, float(volt))


def cycle_name(file_name, volt, cycle):
    """회차 파일 이름. 파일 번호는 0 부터(기존 LabVIEW 파일과 같게) - 화면의 "n회" 는 1 부터이므로 cycle(1 부터) - 1."""
    return "%s%d.dat" % (file_stem(file_name, volt), int(cycle) - 1)


def merged_name(file_name, volt):
    return "%smerged.dat" % file_stem(file_name, volt)


def fmt_e(x):
    """LabVIEW %.7E 형식: 가수 소수 7자리, 지수는 부호를 붙이고 0 을 채우지 않는다(E+1 · E-1 · E+0)."""
    m, e = ("%.7E" % float(x)).split("E")
    return "%sE%s%d" % (m, "-" if int(e) < 0 else "+", abs(int(e)))


def data_line(t, r):
    return "%s\t%s" % (fmt_e(t), fmt_e(r))


def nas_target(base, auto_date, batch, now=None):
    """NAS 저장 폴더 = 기본 폴더 / [연도 / YYYYMMDD(측정 시작 날짜)] / [묶음]. 기본 폴더가 비면 "" (PC 에만 저장).
    날짜 자동을 끄면 기본 폴더 그대로(묶음이 있으면 그 아래)."""
    base = (base or "").strip()
    if not base:
        return ""
    t = time.localtime(time.time() if now is None else now)
    parts = []
    if auto_date:
        parts += [time.strftime("%Y", t), time.strftime("%Y%m%d", t)]
    if (batch or "").strip():
        parts.append(batch.strip())
    # Windows 형식 기본 폴더(\\NAS\... · D:\...)는 Windows 규칙으로, 그 밖(시험의 임시 폴더 등)은 운영체제 규칙으로 잇는다.
    win = "\\" in base or (len(base) >= 2 and base[1] == ":" and base[0].isalpha())
    return (ntpath.join if win else os.path.join)(base, *parts)


def unique_path(path):
    """같은 이름이 있으면 덮어쓰지 않고 _1, _2 … 를 붙인다. (경로, 바뀌었는가)"""
    if not os.path.exists(path):
        return path, False
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists("%s_%d%s" % (base, n, ext)):
        n += 1
    return "%s_%d%s" % (base, n, ext), True


def name_problem(name):
    """파일 이름으로 못 쓰는 이름이면 이유(쉬운 한 문장), 괜찮으면 ""."""
    n = str(name)
    if not n.strip():
        return "이름이 비어 있습니다"
    if any(ch in BAD_NAME_CHARS or ord(ch) < 32 for ch in n):
        return "이름에 못 쓰는 글자가 있습니다 · \\ / : * ? \" < > |"
    if n != n.strip() or n.endswith("."):
        return "이름 앞뒤의 빈칸과 끝의 마침표는 못 씁니다"
    if len(n) > 60:
        return "이름이 너무 깁니다 · 60자까지"
    return ""


class NasCopier(threading.Thread):
    """NAS 로 복사하는 별도 스레드. 못 쓰면 대기 목록에 두고 주의만 낸다(측정은 계속)."""

    def __init__(self, nas_dir, warn, log=None):
        super().__init__(name="nas-copy", daemon=True)
        self.nas_dir, self.warn = nas_dir, warn
        self.log = log or (lambda text: None)
        self.q = queue.Queue()
        self.names = {}                   # 로컬 경로 -> NAS 에 쓴 경로
        self.pending = []                 # 못 쓴 로컬 경로
        self.done = []
        self.ok = None                    # None 아직 · True · False

    def put(self, local):
        self.q.put(str(local))

    def run(self):
        while True:
            local = self.q.get()
            if local is None:
                return
            self.copy(local)

    def finish(self):
        self.q.put(None)

    def copy(self, local):
        if not self.nas_dir:
            return
        try:
            if not os.path.isdir(self.nas_dir):
                os.makedirs(self.nas_dir, exist_ok=True)
            dst = self.names.get(local)
            if dst is None:
                dst, changed = unique_path(os.path.join(self.nas_dir, os.path.basename(local)))
                if changed:
                    self.warn("NAS 에 같은 이름이 있어 %s 로 저장했습니다" % os.path.basename(dst))
                self.names[local] = dst
            shutil.copyfile(local, dst)
            if local in self.pending:
                self.pending.remove(local)
            if local not in self.done:
                self.done.append(local)
            self.ok = True if not self.pending else self.ok
        except OSError as e:
            if local not in self.pending:
                self.pending.append(local)
            if self.ok is not False:
                self.warn("NAS 에 쓰지 못했습니다. 결과 탭에서 다시 복사하세요.")
                self.log("NAS 쓰기 오류 · %s" % e)
            self.ok = False
