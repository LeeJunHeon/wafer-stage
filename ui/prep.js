/* prep.js - 초기 설정 탭: 왼쪽 단계 목록(260px) + 오른쪽 그 단계의 화면. 순서를 강제하지 않는다.
   앞 단계 값이 없어서 못 하는 일은 그 버튼만 회색으로 두고 이유를 한 줄 쓴다(gate). 확인 창은 띄우지 않는다. */
"use strict";

const STEPS = ["장비 연결", "원점 설정", "카메라 · 판 영역", "빈 판 사진", "좌표 보정", "접촉 높이 설정", "쿨러 배치"];
/* 장비마다: 저장된 값의 설정 이름(cfg) · 선택 목록(state.devlist 의 ports · visa · cameras) · 연결 · 끊기 */
const DEVS = [
  { key: "stage", name: "스테이지", cfg: "stage_port", list: "ports", conn: (v) => api("connect_stage", { port: v }), off: () => api("disconnect_stage") },
  { key: "k2110", name: "온도계 K2110", cfg: "k2110_addr", list: "visa", conn: (v) => api("connect_meter", { kind: "k2110", addr: v }), off: () => api("disconnect_meter", { kind: "k2110" }) },
  { key: "k2450", name: "저항계 K2450", cfg: "k2450_addr", list: "visa", conn: (v) => api("connect_meter", { kind: "k2450", addr: v }), off: () => api("disconnect_meter", { kind: "k2450" }) },
  { key: "camera", name: "카메라", cfg: "camera_index", list: "cameras", conn: (v) => api("connect_camera", { index: parseInt(v, 10) }), off: () => api("disconnect_camera") },
];
const CORNER = ["A", "B", "C", "D"];
const CORNER_AT = ["안쪽 왼쪽", "안쪽 오른쪽", "문 쪽 오른쪽", "문 쪽 왼쪽"];   // 꼭짓점 화면 이름(문 기준): 화면 왼쪽 위(안쪽 왼쪽)부터 시계 방향
/* 꼭짓점 이름은 모두 화면 이름(문 기준)이다. 저장 이름(사진 기준)과의 변환은 서버 한 곳(geometry.view_names)에서 한다 - 명령에는 view: true 를 붙인다 */
/* 네 변(사진상)의 길이 차이 한 줄: 점을 잘못 찍었는지 바로 보이게. 최대 차이 = (가장 긴 변 − 가장 짧은 변) ÷ 평균 */
function edgeDiffText(pts) {
  if (pts.length !== 4) return "";
  const L = pts.map((p, i) => Math.hypot(pts[(i + 1) % 4][0] - p[0], pts[(i + 1) % 4][1] - p[1]));
  const mean = L.reduce((a, b) => a + b, 0) / 4;
  return mean > 0 ? "변 길이 차이 최대 " + ((Math.max(...L) - Math.min(...L)) / mean * 100).toFixed(1) + " %" : "";
}
const prep = { step: 0, pages: [], items: [] };

/* 버튼 + 못 누르는 이유 한 줄 */
function gate(text, fn, cls) {
  const b = btn(text, fn, cls), w = h("span", { class: "why" });
  return { b, w, set(reason) { b.disabled = !!reason; w.textContent = reason || ""; } };
}
/* 누르면 끝날 때까지 잠깐 못 누르게(두 번 눌림 방지) */
function busyBtn(text, fn, cls) {
  const b = btn(text, async () => { if (b.dataset.busy) return; b.dataset.busy = "1"; b.classList.add("dis"); try { await fn(); } finally { b.dataset.busy = ""; b.classList.remove("dis"); } }, cls);
  return b;
}
function busyGate(text, fn, cls) {
  const g = gate(text, null, cls);
  g.b.addEventListener("click", async () => { if (g.b.dataset.busy) return; g.b.dataset.busy = "1"; g.b.classList.add("dis"); try { await fn(); } finally { g.b.dataset.busy = ""; g.b.classList.remove("dis"); } });
  return g;
}
const P = () => (S && S.prep) || {};
const homedAll = () => S && S.homed.x && S.homed.y && S.homed.z;
function whyStage(needXY, needZ) {
  if (running()) return "측정 중 실행 불가";
  if (!devOk("stage")) return "스테이지 연결 필요 (초기 설정 1)";
  if (S.estop_locked) return "비상정지 해제 필요";
  if (needXY && !(S.homed.x && S.homed.y)) return "X · Y 원점 필요 (초기 설정 2)";
  if (needZ && !S.homed.z) return "Z 원점 필요 (초기 설정 2)";
  return "";
}
const whyCamera = () => (devOk("camera") ? "" : "카메라 연결 필요 (초기 설정 1)");
const whyHead = () => (S.cfg.photo_head_xy ? "" : "촬영 위치 설정 필요 (초기 설정 3)");

function buildPrep() {
  prep.items = STEPS.map((t, i) => {
    const sub = h("div", { class: "sub" }), done = h("span", { class: "done" });
    const el = h("div", { class: "step", onclick: () => setStep(i) }, h("span", { class: "no", text: i + 1 }),
      h("div", { class: "txt" }, h("div", { class: "t", text: t }), sub), done);
    return { el, sub, done };
  });
  prep.allItem = h("div", { class: "step", onclick: () => setStep(7) }, h("span", { class: "no blank" }), h("div", { class: "txt" }, h("div", { class: "t", text: "전체 설정" })));
  const list = h("div", { class: "panel", id: "prep-list" }, h("h3", { text: "설정 단계" }), ...prep.items.map((x) => x.el), h("div", { class: "sep" }), prep.allItem);
  prep.main = h("div", { id: "prep-main" });
  prep.pages = [buildStep1(), buildStep2(), buildStep3(), buildStep4(), buildStep5(), buildStep6(), buildStep7(),
    typeof buildSettingsPage === "function" ? buildSettingsPage() : null];
  setKids($("#tab-prep"), list, prep.main);
  setStep(0);
}
function setStep(i) {
  if (!prep.pages[i]) return;
  prep.step = i;
  prep.items.forEach((x, k) => x.el.classList.toggle("on", k === i));
  prep.allItem.classList.toggle("on", i === 7);
  setKids(prep.main, prep.pages[i].el);
  if (prep.pages[i].show) prep.pages[i].show();
  if (S) renderPrep();
}
function gotoPrep(i) { setTab("prep"); setStep(i); }

/* ---------- 1 · 장비 연결 ----------
   장비마다 선택 상자(직접 입력 없음). 목록은 프로그램을 실행한 컴퓨터의 것이고 [새로고침] 하나가 세 목록(포트 · 측정 장비 · 카메라)을 다시 읽는다.
   읽기는 서버가 뒤에서 한다(state.devlist.busy 동안 버튼은 "읽는 중"). 목록을 읽는다고 포트 · 장비를 열지 않는다(카메라만 0~3 번을 잠깐 열어 본다).
   여는 것은 [연결] 과 시작 때의 자동 연결뿐이다. */
function buildStep1() {
  const rows = DEVS.map((d) => {
    const sel = h("select", { class: "addr", onchange: () => { sel.dataset.pick = sel.value; } });      // 사람이 고른 값(연결될 때까지 기억)
    const b = btn("연결", async () => {
      b.disabled = true;
      const on = S.devices[d.key].state === "연결";
      const r = await (on ? d.off() : d.conn(sel.value));
      if (r.ok && !on) delete sel.dataset.pick;                 // 연결되면 저장된 값을 따라간다
      b.disabled = false;
    });
    const stt = h("span", { class: "stt" }), resp = h("div", { class: "resp" });
    prep[d.key] = { sel, btn: b, stt, resp, sig: "" };
    return h("div", { class: "devrow" }, h("div", { class: "name", text: d.name }), sel, b, h("div", {}, stt, resp));
  });
  const auto = h("input", { type: "checkbox", onchange: () => putSettings({ auto_connect: auto.checked }) });
  prep.auto = auto;
  prep.refresh = btn("새로고침", () => refreshDevices(false));
  prep.listNote = h("div", { class: "why" });
  const el = h("div", { class: "panel prep-panel" }, h("h3", { text: "1 · 장비 연결" }),
    h("div", {}, ...rows),
    h("div", { class: "row" }, prep.refresh, h("label", { class: "chk" }, auto, "시작 시 자동 연결")), prep.listNote);
  return { el, render: renderStep1 };
}
function refreshDevices(quiet) { return api("devices_refresh", {}, quiet); }
function renderStep1() {
  const c = S.cfg || {}, dl = S.devlist || {};
  if (!prep.listAsked && dl.t == null && !dl.busy && !running()) { prep.listAsked = true; refreshDevices(true); }      // 처음 열 때 한 번 읽는다
  for (const d of DEVS) {
    const dev = S.devices[d.key], p = prep[d.key], on = dev.state === "연결", going = dev.state === "연결 중";
    const cls = { ok: "st-ok", warn: "st-warn", err: "st-err", off: "" }[dotOf(dev)];
    setKids(p.stt, h("i", { class: "dot " + dotOf(dev) }), h("span", { class: cls, text: dev.state + (dev.state === "오류" && dev.why ? " · " + dev.why : "") }));
    p.resp.textContent = on ? dev.info : "";
    p.btn.textContent = on ? "끊기" : "연결";
    p.btn.disabled = going;
    /* 선택 상자: 목록 + 고른 값. 고른 값(없으면 저장된 값)이 목록에 없으면 "COM5 (없음)" 항목으로 보이고 선택된 채로 둔다 */
    const saved = c[d.cfg] == null ? "" : String(c[d.cfg]);
    const want = (on || going) && dev.target ? dev.target : (p.sel.dataset.pick != null ? p.sel.dataset.pick : saved);
    const opts = (dl[d.list] || []).map((o) => [String(o.value), o.label, !!o.blocked]);      // blocked = 사용 안 할 포트("COM4 · … (사용 안 함)") - 보이지만 고를 수 없다
    if (!opts.some((o) => o[0] === want)) opts.unshift([want, want === "" ? "선택" : dl.t == null ? want : want + " (없음)", false]);
    const sig = JSON.stringify([opts, want]);
    if (sig !== p.sig) {                                       // 바뀔 때만 다시 그린다(펼쳐 놓은 목록이 닫히지 않게)
      p.sig = sig;
      setKids(p.sel, ...opts.map(([v, t, off]) => h("option", { value: v, text: t, disabled: off })));
      p.sel.value = want;
    }
    p.sel.disabled = on || going;                              // 연결된 장비는 잠근다(끊어야 바꿀 수 있다)
  }
  prep.refresh.textContent = dl.busy ? "읽는 중" : "새로고침";
  prep.refresh.disabled = !!dl.busy || running();
  prep.listNote.textContent = dl.note || "";
  prep.auto.checked = !!c.auto_connect;
}

/* ---------- 2 · 원점 맞추기 ---------- */
function buildStep2() {
  const pg = { jog: jogPart(), ax: {} };
  const HOW = { here: "현재 위치", end: "끝단 접촉" };
  const rows = ["x", "y", "z"].map((a) => {
    const dot = h("i", { class: "dot" }), txt = h("span", {}), when = h("span", { class: "small" });
    pg.ax[a] = { dot, txt, when };
    return h("div", { class: "row" }, h("b", { class: "axname", text: a.toUpperCase() + " 축" }), dot, txt, when);
  });
  const origin = (axis, mode) => () => (mode === "end" ? apiMove : api)("set_origin", { axis, mode });     // 끝단 접촉은 확인 창 먼저
  pg.here = [gate("X 등록", origin("x", "here")), gate("Y 등록", origin("y", "here")), gate("Z 등록", origin("z", "here")), gate("전체 등록", origin("all", "here"))];
  pg.end = [gate("X", origin("x", "end")), gate("Y", origin("y", "end")), gate("Z", origin("z", "end"))];
  pg.whyHere = h("div", { class: "why" }); pg.whyEnd = h("div", { class: "why" });
  pg.endDesc = h("div", { class: "small" });                     // 끝단 접촉 원점이 하는 일(거리는 서버가 준 실제 값 - state.hw)
  pg.mem = h("div", { class: "small" });                         // 연결 때 보드가 알려 준 위치 기억(배너의 EEPROM 줄)
  const el = h("div", { class: "panel prep-panel" }, h("h3", { text: "2 · 원점 설정" }),
    h("div", { class: "grp" }, ...rows, pg.mem),
    h("div", { class: "two" },
      h("div", { class: "grp" }, h("h4", { text: "현재 위치를 원점으로" }),
        h("div", { class: "small", text: "세 축을 모터 쪽 끝에 붙인 후 등록" }),
        pg.jog.el, h("div", { class: "row wrap" }, ...pg.here.map((g) => g.b)), pg.whyHere),
      h("div", { class: "grp" }, h("h4", { text: "끝단 접촉 원점" }),
        h("div", { class: "small", text: "끝단 접촉 시 소음 발생" }),
        h("div", { class: "row" }, ...pg.end.map((g) => g.b)), pg.endDesc, pg.whyEnd)));
  pg.el = el;
  pg.render = () => {
    const org = P().origin || {};
    for (const a of ["x", "y", "z"]) {
      const r = pg.ax[a], ok = !!S.homed[a], o = org[a];
      r.dot.className = "dot " + (ok ? "ok" : "off");
      r.txt.textContent = devOk("stage") ? (ok ? "원점 있음" : "원점 없음") : "—";
      r.when.textContent = ok && o ? (HOW[o.how] || o.how) + " · " + fmtWhen(o.t) : "";
    }
    const why = whyStage(false, false);
    pg.here.forEach((g) => { g.b.disabled = !!why; });
    pg.whyHere.textContent = why;
    const whyXY = why || (!S.homed.z ? "Z 원점 필요" : "");
    pg.end[0].b.disabled = pg.end[1].b.disabled = !!whyXY;
    pg.end[2].b.disabled = !!why;
    pg.whyEnd.textContent = why || (whyXY ? whyXY : "");
    const mem = S.stage_mem || {};
    pg.mem.textContent = devOk("stage") && mem.text ? mem.text : "";
    pg.mem.className = "small" + (mem.code && mem.code !== "restored" ? " st-warn" : "");
    const hw = S.hw || {};
    pg.endDesc.textContent = hw.find_push_mm == null ? "" : "0 쪽 끝으로 최대 " + hw.find_push_mm + " mm 밀고 " + hw.find_gap_mm + " mm 물러난 위치 = 0 · 끝에서 " + hw.find_push_mm + " mm 안쪽일 때만 사용";
    pg.jog.update();
  };
  return pg;
}

/* ---------- 초점 찾기 결과 그래프(canvas): 가로 초점 값 · 세로 선명도 · 고른 값 ----------
   글자는 축 밖 띠에만(왼쪽 = 선명도 · 아래 = 초점 값 · 위 = 고른 값) - 그래프 선과 겹치지 않는다. 고른 값은 위 띠의 글자에서 점까지 점선(지시선)으로 잇는다. */
const FG = { W: 380, H: 150, L: 48, R: 12, T: 20, B: 20 };
function focusGraph() {
  const cv = h("canvas", { class: "fgraph", width: FG.W, height: FG.H });
  const draw = (pts, best, lo, hi) => {
    const g = cv.getContext("2d"), dpr = window.devicePixelRatio || 1;
    if (cv.width !== FG.W * dpr) { cv.width = FG.W * dpr; cv.height = FG.H * dpr; cv.style.width = FG.W + "px"; cv.style.height = FG.H + "px"; }
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, FG.W, FG.H);
    const x0 = FG.L, x1 = FG.W - FG.R, y0 = FG.T, y1 = FG.H - FG.B;
    g.strokeStyle = "#C4CAD1"; g.lineWidth = 1;
    g.strokeRect(x0 + 0.5, y0 + 0.5, x1 - x0, y1 - y0);
    g.fillStyle = "#5B636D"; g.font = "11px Pretendard, sans-serif";
    g.textBaseline = "top"; g.textAlign = "left"; g.fillText(String(lo), x0, y1 + 4);
    g.textAlign = "right"; g.fillText(String(hi), x1, y1 + 4);
    g.textAlign = "center"; g.fillText("초점 값", (x0 + x1) / 2, y1 + 4);
    if (!pts || !pts.length) return;
    const smax = Math.max(...pts.map((p) => p[1])), smin = Math.min(...pts.map((p) => p[1]));
    const span = Math.max(1e-9, smax - smin);
    const X = (v) => x0 + (v - lo) / Math.max(1, hi - lo) * (x1 - x0), Y = (s) => y1 - 4 - (s - smin) / span * (y1 - y0 - 8);
    g.textAlign = "right"; g.textBaseline = "top"; g.fillText(smax.toFixed(0), x0 - 4, y0);
    g.textBaseline = "bottom"; g.fillText(smin.toFixed(0), x0 - 4, y1);
    const srt = pts.slice().sort((a, b) => a[0] - b[0]);
    g.strokeStyle = "#1F2329"; g.beginPath();
    srt.forEach((p, i) => { if (i) g.lineTo(X(p[0]), Y(p[1])); else g.moveTo(X(p[0]), Y(p[1])); });
    g.stroke();
    for (const p of pts) { g.fillStyle = p[2] === "fine" ? "#1F2329" : "#9AA1A9"; g.beginPath(); g.arc(X(p[0]), Y(p[1]), 2.5, 0, 7); g.fill(); }
    if (best != null) {
      const bp = pts.find((p) => p[0] === best);
      const bx = X(best);
      g.strokeStyle = "#2A78D6"; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(bx, y0); g.lineTo(bx, bp ? Y(bp[1]) - 4 : y1); g.stroke(); g.setLineDash([]);
      g.fillStyle = "#2A78D6"; g.textBaseline = "bottom";
      const label = "선택 " + best;
      g.textAlign = bx < x0 + 40 ? "left" : bx > x1 - 40 ? "right" : "center";
      g.fillText(label, bx, y0 - 3);
    }
  };
  return { el: cv, draw };
}

/* ---------- 3 · 카메라 · 판 영역 ---------- */
function buildStep3() {
  const pg = { jog: jogPart(), pts: [], drag: -1, photoId: null, dirty: false, magT: 0 };
  pg.ov = svgEl("g", {});
  pg.pb = photoBox({ ar: 4 / 3, note: "사진 없음", onfit: () => drawPts(), dirs: true });     // 화면 방향(문 기준) 사진 · 둘레 방향 글
  pg.pb.svg.append(pg.ov);
  pg.headSet = busyGate("현재 위치로 설정", () => api("photo_head_set"));
  pg.headTxt = h("div", {});
  pg.take = busyGate("촬영", async () => { const r = await apiMove("photo_take"); if (r.ok) { pg.dirty = false; pg.photoId = null; } });
  pg.list = h("div", { class: "small" });
  const redo = () => { pg.pts = []; pg.sel = -1; pg.dirty = true; syncAll(); };
  const apply = async () => {                                     // 작은 사진 · 크게 선택 창이 같은 명령(plate_set)
    const r = await api("plate_set", { corners: pg.pts, view: true });     // 화면 픽셀 · 화면 이름 순서 → 서버가 저장 기준으로 바꾼다
    if (r.ok) { pg.dirty = false; toast(r.result.note || "판 영역 설정 완료"); }
    return r;
  };
  pg.redo = btn("다시 선택", redo);
  pg.apply = busyGate("확정", apply, "on");
  pg.big = btn("크게 선택", () => openPlatePick());
  pg.sel = -1;                                                    // 고른 점(방향키로 옮긴다)
  pg.mag = h("div", { class: "mag hidden" }, h("i", { class: "cross-h" }), h("i", { class: "cross-v" }));
  pg.pb.box.append(pg.mag);                                     // 확대창은 사진 위 구석에 띄운다(누르는 자리의 반대쪽)
  /* ---- 초점: [초점 찾기](판 영역 안 선명도) · 그래프 · 미세 조정 · [적용]. 판 영역이 없으면 사진에 범위 상자를 그린다 ---- */
  pg.roi = null; pg.boxMode = false; pg.boxStart = null;
  pg.fcur = h("div", {});
  pg.fgraph = focusGraph();
  pg.fres = h("div", { class: "small" });
  pg.fnote = h("div", {});
  pg.fnudge = h("span", { class: "small" });
  pg.ffind = busyGate("초점 찾기", () => apiMove("focus_find", { roi: P().plate ? null : pg.roi, view: true }));
  pg.fbox = btn("범위 상자", () => { pg.boxMode = !pg.boxMode; pg.fbox.classList.toggle("on", pg.boxMode); });
  pg.fminus = busyGate("−", () => api("focus_nudge", { d: -1 }));
  pg.fplus = busyGate("+", () => api("focus_nudge", { d: 1 }));
  pg.fapply = busyGate("적용", async () => { const r = await api("focus_apply"); if (r.ok && r.result.note) toast(r.result.note); });
  pg.el = h("div", { class: "panel prep-panel photo" }, h("h3", { text: "3 · 카메라 · 판 영역" }),
    photoSplit(pg.pb, h("div", {},
      h("div", { class: "grp" }, h("h4", { text: "촬영 위치" }),
        h("div", { class: "small", text: "헤드가 판을 가리지 않고 마커가 보이는 위치" }),
        pg.jog.el, h("div", { class: "row" }, pg.headSet.b, pg.headSet.w), pg.headTxt),
      h("div", { class: "grp" }, h("h4", { text: "판 영역" }),
        h("div", { class: "row" }, pg.take.b, pg.big, pg.take.w),
        h("div", { class: "small", text: "꼭짓점을 왼쪽 위부터 시계 방향으로 선택" }),
        pg.list, h("div", { class: "row" }, pg.redo, pg.apply.b, pg.apply.w)),
      h("div", { class: "grp" }, h("h4", { text: "초점" }),
        h("div", { class: "small", text: "샘플을 올려 둔 채 실행 권장 · 판 높이 기준" }),
        pg.fcur, h("div", { class: "row" }, pg.ffind.b, pg.fbox, pg.ffind.w), pg.fgraph.el, pg.fres, pg.fnote,
        h("div", { class: "row" }, pg.fminus.b, pg.fplus.b, pg.fnudge, pg.fapply.b), pg.fapply.w))));

  /* 찍은 사진(화면 방향): w · h = 화면 방향 크기(90 · 270° 면 가로 · 세로가 바뀐다). 점 · 상자 · 돋보기 좌표는 모두 화면 픽셀 */
  const photo = () => { const ph = P().photo; return ph ? Object.assign({}, ph, { w: ph.vw || ph.w, h: ph.vh || ph.h }) : null; };
  function toPx(ev) { const ph = photo(); const [fx, fy] = pg.pb.toFrac(ev); return [Math.max(0, Math.min(ph.w - 1, fx * ph.w)), Math.max(0, Math.min(ph.h - 1, fy * ph.h))]; }
  /* 꼭짓점 덧그림(사진 픽셀 좌표 · k = 화면 1 px 의 사진 픽셀 수): 작은 사진 · 크게 선택 창이 같이 쓴다 */
  function cornerKids(k) {
    const kids = [];
    if (pg.pts.length >= 2) kids.push(svgEl(pg.pts.length === 4 ? "polygon" : "polyline", { points: pg.pts.map((p) => p.join(",")).join(" "), fill: "none", stroke: "#1F2A37", "stroke-width": 1.5 * k }));
    pg.pts.forEach((p, i) => {
      kids.push(svgEl("circle", { cx: p[0], cy: p[1], r: 6 * k, fill: i === pg.sel ? "#C03A7D" : "#fff", stroke: "#1F2A37", "stroke-width": 2 * k }));
      kids.push(svgEl("text", { x: p[0] + 10 * k, y: p[1] - 8 * k, "font-size": 14 * k, "font-weight": 700, fill: "#1F2A37", stroke: "#fff", "stroke-width": 3 * k, "paint-order": "stroke", text: CORNER[i] }));
    });
    return kids;
  }
  function drawPts() {
    const ph = photo();
    if (!ph || !pg.pb.w) { setKids(pg.ov); return; }
    pg.pb.svg.setAttribute("viewBox", "0 0 " + ph.w + " " + ph.h);
    const k = ph.w / pg.pb.w, r = pg.roi;                         // 화면 1px = 사진 ph.w / pg.pb.w 픽셀
    setKids(pg.ov, ...cornerKids(k),
      r && !P().plate ? svgEl("rect", { x: r[0], y: r[1], width: r[2], height: r[3], fill: "none", stroke: "#2A78D6", "stroke-width": 2 * k, "stroke-dasharray": 6 * k }) : null);
  }
  const ptsText = () => (pg.pts.length ? pg.pts.map((p, i) => CORNER[i] + " (" + Math.round(p[0]) + ", " + Math.round(p[1]) + ")").join("  ") : "선택한 점 없음");
  const nextText = () => (pg.pts.length < 4 ? "다음: " + CORNER[pg.pts.length] + " · " + CORNER_AT[pg.pts.length] : edgeDiffText(pg.pts));
  function renderPts() {
    pg.list.textContent = ptsText() + (pg.pts.length === 4 ? " · " + edgeDiffText(pg.pts) : "");
    if (S) syncApply();                                           // 누르는 그 자리에서 바로(서버 상태가 올 때까지 기다리지 않는다)
    if (pg.bigSync) pg.bigSync();
  }
  function syncAll() { drawPts(); renderPts(); }
  /* 누른 자리: 기존 점 가까이(tol 사진 픽셀)면 그 점을 잡고, 아니면 다음 점을 더한다(4점이면 무시). 잡은 점 번호 · 없으면 -1 */
  function pickAt(p, tol) {
    let hit = -1, best = tol;
    pg.pts.forEach((q, i) => { const d = Math.hypot(q[0] - p[0], q[1] - p[1]); if (d < best) { best = d; hit = i; } });
    if (hit < 0) {
      if (pg.pts.length >= 4) return -1;
      pg.pts.push(p); hit = pg.pts.length - 1;
    }
    pg.sel = hit; pg.dirty = true;
    return hit;
  }
  /* 방향키: 고른 점을 1 px · Shift 10 px(원본 사진 픽셀) */
  function nudge(ev) {
    const d = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[ev.key];
    const ph = photo();
    if (!d || pg.sel < 0 || !pg.pts[pg.sel] || !ph) return false;
    const n = ev.shiftKey ? 10 : 1, q = pg.pts[pg.sel];
    pg.pts[pg.sel] = [Math.max(0, Math.min(ph.w - 1, q[0] + d[0] * n)), Math.max(0, Math.min(ph.h - 1, q[1] + d[1] * n))];
    pg.dirty = true; syncAll();
    return true;
  }

  /* ---- [크게 선택]: 위 띠만 남기고 화면 전체. 확대 · 이동 · 돋보기(zoomView) + 꼭짓점 목록 · 다음 점 안내 · [다시 선택] [확정] ---- */
  function openPlatePick() {
    const ph = photo();
    if (!ph) { toast("촬영 필요"); return; }
    const z = zoomView({ src: "raw", empty: "사진 없음",
      draw: (g, k) => setKids(g, ...cornerKids(k)),
      onDown: (p) => { const i = pickAt(p, 14 / z.s); if (i < 0) return false; pg.drag = i; syncAll(); return true; },
      onDrag: (p) => { if (pg.drag >= 0) { pg.pts[pg.drag] = p; syncAll(); } },
      onUp: () => { pg.drag = -1; syncAll(); },
      onKey: nudge });
    const list = h("div", { class: "zv-pts" }), next = h("div", { class: "small" });
    const bApply = busyGate("확정", apply, "on"), bRedo = btn("다시 선택", redo);
    const side = h("div", { class: "col" }, h("h4", { text: "꼭짓점" }), small("왼쪽 위부터 시계 방향 · 점을 끌어 옮김 · 고른 점은 방향키 1 px · Shift 10 px"),
      list, next, h("div", { class: "row" }, bRedo, bApply.b), bApply.w);
    pg.bigSync = () => {
      setKids(list, ...CORNER.flatMap((c, i) => { const p = pg.pts[i]; const cls = i === pg.sel ? "sel" : ""; return [h("b", { class: cls, text: c }), h("span", { class: cls, text: p ? Math.round(p[0]) + ", " + Math.round(p[1]) : CORNER_AT[i] })]; }));
      next.textContent = nextText();
      const phn = photo();
      bApply.set(!phn ? "촬영 필요" : pg.pts.length < 4 ? "꼭짓점 " + pg.pts.length + " / 4" : running() ? "측정 중 실행 불가" : "");
      bRedo.disabled = !pg.pts.length;
      z.redraw();
    };
    const close = () => { pg.bigSync = null; closeDialog(); syncAll(); };
    const setPhoto = () => { const p2 = photo(); z.set(p2 ? [p2.w, p2.h] : null, p2 ? "/api/img/raw.jpg?v=" + p2.id : "", p2 ? String(p2.id) : "", p2 ? Math.min(1600, p2.w) : 0); };
    openZoomDialog("판 영역 · 꼭짓점 선택", z, side, [btn("닫기", close)], () => { setPhoto(); pg.bigSync && pg.bigSync(); });
    setPhoto(); pg.bigSync();
  }
  function syncApply() {                                          // [이대로 정하기] · [다시 누르기] 를 누를 수 있는지와 그 이유
    const ph = photo(), pl = P().plate;
    pg.apply.set(!ph ? "촬영 필요" : pg.pts.length < 4 ? "꼭짓점 " + pg.pts.length + " / 4" : running() ? "측정 중 실행 불가" : "");
    if (pg.pts.length === 4 && pl && !pg.dirty) pg.apply.w.textContent = pl.stale ? "초점 바뀜 · 다시 확정 필요" : "설정 완료 · " + fmtWhen(pl.t);
    pg.redo.disabled = !pg.pts.length;
  }
  function showMag(p) {
    const now = performance.now();
    if (now - pg.magT < 120) return;                            // 끄는 동안 주변을 크게 보여 준다(1 초에 8번까지)
    pg.magT = now;
    const ph = photo();
    pg.mag.classList.remove("hidden");
    pg.mag.classList.toggle("left", p[0] > ph.w / 2);
    pg.mag.style.backgroundImage = "url(/api/img/crop.jpg?view=1&x=" + Math.round(p[0]) + "&y=" + Math.round(p[1]) + "&half=50&t=" + Math.round(now) + ")";
  }
  pg.pb.inner.addEventListener("pointerdown", (ev) => {
    const ph = photo();
    if (!ph || ev.button !== 0) return;
    ev.preventDefault();
    const p = toPx(ev), k = ph.w / Math.max(1, pg.pb.w);
    if (pg.boxMode) {                                               // 초점 찾기 범위 상자(판 영역이 없을 때)
      pg.boxStart = p; pg.roi = [Math.round(p[0]), Math.round(p[1]), 1, 1];
      pg.pb.inner.setPointerCapture(ev.pointerId); drawPts();
      return;
    }
    const hit = pickAt(p, 14 * k);
    if (hit < 0) return;
    pg.drag = hit;
    pg.pb.inner.setPointerCapture(ev.pointerId);
    pg.magT = 0; showMag(pg.pts[hit]); drawPts(); renderPts();
  });
  pg.pb.inner.addEventListener("pointermove", (ev) => {
    if (pg.boxStart) {
      const p = toPx(ev), a = pg.boxStart;
      pg.roi = [Math.round(Math.min(a[0], p[0])), Math.round(Math.min(a[1], p[1])), Math.max(1, Math.round(Math.abs(p[0] - a[0]))), Math.max(1, Math.round(Math.abs(p[1] - a[1])))];
      drawPts();
      return;
    }
    if (pg.drag < 0) return;
    pg.pts[pg.drag] = toPx(ev);
    showMag(pg.pts[pg.drag]); drawPts(); renderPts();
  });
  const endDrag = () => {
    if (pg.boxStart) { pg.boxStart = null; pg.boxMode = false; pg.fbox.classList.remove("on"); drawPts(); return; }
    if (pg.drag >= 0) { pg.drag = -1; pg.magT = 0; pg.mag.classList.add("hidden"); renderPts(); } };
  pg.pb.inner.addEventListener("pointerup", endDrag);
  pg.pb.inner.addEventListener("pointercancel", endDrag);

  pg.render = () => {
    const ph = photo(), pl = (P().view || {}).plate, c = S.cfg;                 // pl = 저장된 꼭짓점을 화면 픽셀 · 화면 이름 순서로(서버)
    if (ph) { pg.pb.setAr(ph.w / ph.h); pg.pb.show("/api/img/raw.jpg?v=" + ph.id); } else pg.pb.show("");
    if (ph && pg.photoId !== ph.id && !pg.dirty && pg.drag < 0) {    // 새 사진: 저장된 꼭짓점이 이 사진 크기와 맞으면 그것부터 보여 준다
      pg.photoId = ph.id;
      pg.pts = pl && pl.size[0] === ph.w && pl.size[1] === ph.h ? pl.corners.map((p) => [p[0], p[1]]) : [];
      pg.sel = -1;
      drawPts(); renderPts();
    }
    pg.headSet.set(whyStage(true, false) || whyCamera());
    const hp = c.photo_head_xy, seen = P().marker_seen;
    setKids(pg.headTxt, hp ? "X " + f1(hp[0]) + " · Y " + f1(hp[1]) : "미설정",
      hp && seen != null ? h("span", { class: seen ? "st-ok" : "st-warn", text: seen ? " · 마커 보임" : " · 마커 안 보임" }) : "");
    pg.take.set(running() ? "측정 중 실행 불가" : whyCamera());
    pg.big.disabled = !ph;
    syncApply();
    renderFocus();
    pg.jog.update();
  };
  function renderFocus() {
    const f = P().focus || {}, fc = P().focus_cfg || {}, active = !!f.active;
    const saved = fc.value == null || fc.value < 0 ? "미설정" : String(fc.value);
    pg.fcur.textContent = "현재 " + (f.value == null ? "—" : f.value) + " · 저장 " + saved + (active && f.phase ? " · " + f.phase : "");
    pg.fcur.className = fc.value == null || fc.value < 0 ? "st-warn" : "";
    const noPlate = !P().plate;
    pg.fbox.classList.toggle("hidden", !noPlate);
    const busy = running() ? "측정 중 실행 불가" : whyCamera() || (active ? "초점 찾기 중" : "");
    pg.ffind.set(busy || whyStage(true, false) || whyHead() || (noPlate && !pg.roi ? "판 영역 없음 · [범위 상자] 로 사진에 상자 지정" : ""));
    pg.fminus.set(busy); pg.fplus.set(busy);
    pg.fapply.set(busy || (f.value == null ? "[초점 찾기] 필요" : ""));
    pg.fgraph.draw(f.points || [], f.best, fc.lo, fc.hi);
    pg.fres.textContent = (f.result || "") + (f.skipped && f.skipped.length ? " · 범위 밖 " + f.skipped.length + "개 건너뜀" : "");
    pg.fnote.textContent = f.note || "";
    pg.fnote.className = f.note ? "st-warn" : "";
    pg.fnudge.textContent = f.nudge ? f.nudge.value + " · 선명도 " + f.nudge.sharp.toFixed(1) : "";
  }
  renderPts();
  return pg;
}

/* ---------- 4 · 빈 판 사진 ---------- */
function buildStep4() {
  const pg = {};
  pg.pb = photoBox({ ar: 1, note: "빈 판 사진 없음", dirs: true });
  pg.take = busyGate("빈 판 촬영", () => apiMove("empty_take"), "on");
  pg.txt = h("div", {});
  /* 비교: 마지막 판 사진(보정 사진 · 같은 방향)을 나란히 - 조명 · 문 상태 · 판 위치가 달라 정렬이 실패하는지 눈으로 본다 */
  pg.cmp = photoBox({ ar: 1, note: "판 사진 없음", cls: "cmp-photo" });
  pg.cmpTxt = h("div", { class: "small" });
  pg.findNote = h("div", { class: "small" });
  pg.el = h("div", { class: "panel prep-panel photo" }, h("h3", { text: "4 · 빈 판 사진" }),
    photoSplit(pg.pb, h("div", {},
      h("div", { class: "grp" },
        h("div", { class: "small", text: "샘플 제거 후 촬영" }),
        h("div", { class: "row" }, pg.take.b, pg.take.w), pg.txt,
        h("div", { class: "small", text: "없어도 측정 가능" })),
      h("div", { class: "grp" }, h("h4", { text: "마지막 판 사진 (비교)" }), pg.cmp.box, pg.cmpTxt, pg.findNote))));
  pg.render = () => {
    const e = P().empty, pl = P().plate;
    pg.take.set(whyStage(true, true) || whyCamera() || whyHead());
    pg.txt.textContent = e ? "촬영 " + fmtWhen(e.t) : "";
    pg.pb.setNote(e ? (pl ? "사진을 불러오는 중" : "판 영역 설정 필요 (초기 설정 3)") : "빈 판 사진 없음");
    pg.pb.show(e && pl ? "/api/img/empty.jpg?v=" + e.t + "-" + pl.t : "");
    const ph = P().photo, fd = P().find;
    pg.cmp.show(pl && ph && ph.rect ? "/api/img/plate.jpg?v=" + ph.id + "-" + pl.t : "");
    pg.cmpTxt.textContent = ph ? "촬영 " + fmtWhen(ph.t) : "";
    const bad = fd && (fd.notes || []).find((t) => t.startsWith("빈 판 사진 불일치"));         // 마지막 샘플 검출의 정렬 결과(숫자 · 할 일)
    pg.findNote.textContent = bad || (fd && fd.path === "빈 판 비교" ? "마지막 검출 · 빈 판 비교 정상" : "");
    pg.findNote.className = bad ? "st-warn" : "small";
  };
  return pg;
}

/* ---------- 5 · 좌표 맞추기 ---------- */
function buildStep5() {
  const pg = { jog: jogPart() };
  /* 작은 안내 그림(160 px 정사각형): 판 사진 축소본(없으면 빈 사각형) + A~D 원(등록된 점은 채움). 누르는 그림이 아니다 */
  const GUIDE = 160;
  pg.img = svgEl("image", { x: 0, y: 0, width: GUIDE, height: GUIDE, preserveAspectRatio: "none" });
  pg.ov = svgEl("g", {});
  pg.guide = svgEl("svg", { class: "guide", width: GUIDE, height: GUIDE, viewBox: "0 0 " + GUIDE + " " + GUIDE },
    svgEl("rect", { x: 0.5, y: 0.5, width: GUIDE - 1, height: GUIDE - 1, fill: "#F7F8F9", stroke: "#D5D9DE" }), pg.img, pg.ov);
  pg.set = CORNER.map((n) => busyGate(n + " 등록", () => apiMove("coord_set", { name: n, view: true })));     // 화면 이름 · 4개째면 헤드 마커 등록이 이어진다(확인 창)
  pg.whySet = h("div", { class: "why" });
  pg.gotoD = busyGate("D 예상 위치로 이동", () => api("coord_goto_d"));
  pg.cells = CORNER.map(() => [h("td", { class: "num" }), h("td", { class: "num" })]);
  pg.pts = h("table", { class: "tb ctab" }, h("thead", {}, h("tr", {}, h("th", { text: "꼭짓점" }), h("th", { class: "num", text: "X (mm)" }), h("th", { class: "num", text: "Y (mm)" }))),
    h("tbody", {}, ...CORNER.map((n, i) => h("tr", {}, h("td", { text: n }), pg.cells[i][0], pg.cells[i][1]))));
  pg.q = h("div", { class: "small" });
  pg.marker = h("div", { class: "small" });
  pg.learn = busyGate("마커 재등록", () => apiMove("marker_learn"), "sm");
  pg.extra = h("div", { class: "exlist" });
  /* 지우는 버튼 둘: 확인 창 뒤 실행(Enter · Space 로 눌리지 않게 noEnter). [좌표 보정 초기화] = 꼭짓점 칸 · [보정 점 모두 지우기] = 보정 점 칸 */
  const askErase = (title, text, okText, cmd) => openDialog(title, h("div", { text }),
    [noEnter(btn("취소", closeDialog)), noEnter(btn(okText, () => { closeDialog(); api(cmd); }, "on"))]);
  pg.clear = btn("좌표 보정 초기화", () => {
    const n = ((P().coords || {}).extra || []).length;
    askErase("좌표 보정 초기화?", "꼭짓점 4개 · 보정 점 " + n + "개 · 헤드 마커 삭제 · 되돌릴 수 없음", "초기화", "coord_clear");
  }, "sm");
  pg.clearEx = btn("보정 점 모두 지우기", () => {
    const n = ((P().coords || {}).extra || []).length;
    askErase("보정 점 모두 지우기?", "보정 점 " + n + "개 삭제", "지우기", "coord_extra_clear");
  }, "sm");
  pg.markerNote = h("div", { class: "small" });                 // D 등록 뒤 자동으로 하는 일(이동 거리는 설정 marker_probe_mm)
  pg.el = h("div", { class: "panel prep-panel" }, h("h3", { text: "5 · 좌표 보정" }),
    h("div", { class: "two" },
      h("div", { class: "grp" }, h("h4", { text: "핀 이동" }), h("div", { class: "small", text: "핀을 꼭짓점에 맞춘 후 등록" }), pg.jog.el),
      h("div", { class: "grp" }, h("h4", { text: "꼭짓점 등록" }),
        h("div", { class: "row top" }, pg.guide, h("div", { class: "col" }, small("A = 안쪽 왼쪽 · 시계 방향"), pg.pts)),
        h("div", { class: "row wrap" }, ...pg.set.map((g) => g.b)), pg.whySet, pg.markerNote,
        h("div", { class: "row" }, pg.gotoD.b, pg.gotoD.w), h("div", { class: "row" }, pg.clear))),
    h("div", { class: "three" },
      h("div", { class: "grp" }, h("h4", { text: "정확도" }), pg.q),
      h("div", { class: "grp" }, h("h4", { text: "헤드 마커" }), pg.marker, h("div", { class: "row" }, pg.learn.b, pg.learn.w)),
      h("div", { class: "grp" }, h("h4", { text: "보정 점" }),
        h("div", { class: "small", text: "수동 제어 탭 [보정 점 추가] · 사진에서 핀 위치 누름" }),
        pg.extra, h("div", { class: "row" }, pg.clearEx))));
  pg.draw = () => {
    const co = P().coords;
    if (!S) return;
    const at = [[0, 0], [GUIDE, 0], [GUIDE, GUIDE], [0, GUIDE]], kids = [];
    CORNER.forEach((n, i) => {
      const has = co && co.view_stage && co.view_stage[n];          // 화면 이름(문 기준) - 그림도 화면 방향(서버가 돌린 판 사진)
      const x = at[i][0] + (i === 1 || i === 2 ? -1 : 1) * 14, y = at[i][1] + (i >= 2 ? -1 : 1) * 14;
      kids.push(svgEl("circle", { cx: x, cy: y, r: 11, fill: has ? "#1F2A37" : "#fff", stroke: "#1F2A37", "stroke-width": 1.5 }));
      kids.push(svgEl("text", { x, y: y + 4.5, "font-size": 12, "font-weight": 700, "text-anchor": "middle", fill: has ? "#fff" : "#1F2A37", text: n }));
    });
    setKids(pg.ov, ...kids);
  };
  pg.render = () => {
    const co = P().coords || { stage: {}, extra: [] }, pl = P().plate, ph = P().photo, e = P().empty;
    const src = pl && ph && ph.rect ? "/api/img/plate.jpg?v=" + ph.id + "-" + pl.t : pl && e ? "/api/img/empty.jpg?v=" + e.t + "-" + pl.t : "";
    const srcV = src ? src + vq() : "";
    if (srcV !== pg.src) { pg.src = srcV; if (srcV) pg.img.setAttribute("href", srcV); else pg.img.removeAttribute("href"); }     // 없으면 빈 사각형 · 화면 방향(문 기준) 그림
    pg.markerNote.textContent = "4번째 등록 후 촬영 위치로 이동해 헤드 마커 자동 등록 · " + S.cfg.marker_probe_mm + " mm 씩 이동";
    const why = whyStage(true, false);
    pg.set.forEach((g) => { g.b.disabled = !!why; });
    pg.whySet.textContent = why || (pl ? "" : "판 영역 설정 필요 (초기 설정 3)");
    const vs = co.view_stage || {}, nReg = Object.keys(vs).length;
    pg.gotoD.b.textContent = (co.guess_name || "D") + " 예상 위치로 이동";      // 남은 꼭짓점(화면 이름)
    pg.gotoD.set(whyStage(true, true) || (co.d_guess ? "" : nReg >= 4 ? "꼭짓점 4개 등록됨" : "꼭짓점 3개 등록 필요"));
    CORNER.forEach((n, i) => { const p = vs[n]; pg.cells[i][0].textContent = p ? f1(p[0]) : "—"; pg.cells[i][1].textContent = p ? f1(p[1]) : "—"; });
    const q = co.quality;
    if (q) {
      setKids(pg.q, 
        h("div", { text: "변 길이 오차 최대 " + q.max_side_err.toFixed(1) + " mm" }),
        h("div", { text: "직각 오차 최대 " + q.max_angle_err.toFixed(2) + "°" }),
        q.warn ? h("div", { class: "st-warn", text: "오차 큼 · 재설정 권장" }) : null);
    } else pg.q.textContent = "꼭짓점 4개 등록 후 표시";
    pg.marker.textContent = co.marker ? "등록됨 · " + fmtWhen(co.marker.t) : "미등록";
    pg.learn.set(whyStage(true, true) || whyCamera() || whyHead());
    const sig = JSON.stringify(co.extra);
    if (sig !== pg.exSig) {
      pg.exSig = sig;
      setKids(pg.extra, ...(co.extra.length ? co.extra.map((x, i) => h("div", { class: "row" },
        h("span", { class: "grow", text: (i + 1) + ". X " + f1(x.xy[0]) + " Y " + f1(x.xy[1]) + " · 보정 " + (x.err == null ? "—" : x.err.toFixed(2) + " mm") + " · " + fmtWhen(x.t) }),
        btn("지우기", () => api("coord_remove", { index: i }), "sm"))) : [small("없음")]));
    }
    pg.clear.disabled = running() || !Object.keys(co.stage).length;
    pg.clearEx.disabled = running() || !(co.extra || []).length;
    pg.draw();
    pg.jog.update();
  };
  return pg;
}

/* ---------- 6 · 접촉 높이 찾기 ---------- */
function contactLine(ct) {                                       // 접촉 찾기의 진행 · 결과 한 줄(초기 설정 6단계 · 수동 제어 탭이 같이 쓴다)
  if (!ct) return "";
  if (ct.active) return "하강 중 · Z " + f3(ct.z) + " mm · " + (ct.text || "");
  if (ct.result) return "접촉 Z " + f2(ct.result.first_z) + " mm · 2단자 " + fmtOhm2(ct.result.r2) + " · 4단자 " + fmtOhm2(ct.result.r4);
  if (ct.fail) return ct.fail + " · 핀 상승 완료";
  return "";
}
function whyContact() {
  return whyStage(false, true) || (devOk("k2450") ? "" : "저항계 연결 필요 (초기 설정 1)");
}
/* 기본(접촉 방식 = 고정 높이): 핀을 작은 단위로 내리고 올려 샘플에 댄 뒤 [현재 높이로 저장]. 측정은 그 높이까지만 내린다.
   전기 탐색을 고른 때(접촉 방식 = 전기 탐색)에만 [접촉 탐색] · [높이 저장] 이 보인다. 맨 아래에 저장된 높이 · 접촉 방식 · 수동 Z 하강 한도를 보인다. */
const FINE_STEPS = [0.01, 0.05, 0.1, 1];                          // 핀 미세 이동 단위(mm). 실제 이동은 펄스 단위(0.00625 mm)로 맞춰진다
const CONTACT_MODES = { fixed: "고정 높이", search: "전기 탐색" };
function buildStep6() {
  const pg = { jog: jogPart(), fine: 0.1 };
  const fineBtns = FINE_STEPS.map((v) => btn(String(v), () => { pg.fine = v; syncFine(); }));
  function syncFine() { fineBtns.forEach((b, i) => b.classList.toggle("on", pg.fine === FINE_STEPS[i])); }
  syncFine();
  pg.zUp = btn("Z 상승", () => api("jog", { axis: "z", mm: -pg.fine }));            // 순서 · 이름은 이동 패드(jogPart)와 같게: [Z 상승] [Z 하강]
  pg.zDown = btn("Z 하강", () => api("jog", { axis: "z", mm: pg.fine }));
  pg.zNow = h("span", {});
  pg.whyFine = h("div", { class: "why" });
  pg.saveHere = busyGate("현재 높이로 저장", () => api("contact_save_here"), "on");
  pg.find = busyGate("접촉 탐색", () => apiMove("contact_find"));
  pg.line = h("div", {});
  pg.save = busyGate("높이 저장", () => api("contact_save"));
  pg.search = h("div", { class: "grp" }, h("h4", { text: "전기 탐색" }),
    h("div", { class: "small", text: "핀을 샘플 가까이 내린 후 누름" }),
    h("div", { class: "row" }, pg.find.b, pg.find.w), pg.line,
    h("div", { class: "row" }, pg.save.b, pg.save.w));
  pg.saved = h("div", {}); pg.mode = h("div", { class: "small" }); pg.floor = h("div", { class: "small" });
  pg.el = h("div", { class: "panel prep-panel" }, h("h3", { text: "6 · 접촉 높이 설정" }),
    h("div", { class: "two" },
      h("div", { class: "grp" }, h("h4", { text: "샘플 위로 이동" }), pg.jog.el),
      h("div", { class: "grp" }, h("h4", { text: "핀 높이 조정" }),
        h("div", { class: "small", text: "핀을 조금씩 내려 샘플에 댄 후 저장" }),
        h("div", { class: "steps" }, small("이동 거리"), ...fineBtns, small("mm")),
        h("div", { class: "row" }, pg.zUp, pg.zDown, pg.zNow), pg.whyFine,
        h("div", { class: "row" }, pg.saveHere.b, pg.saveHere.w),
        pg.search)),
    h("div", { class: "grp" }, pg.saved, pg.mode, pg.floor));
  pg.render = () => {
    const ct = P().contact || {}, c = S.cfg, why = whyStage(false, true);
    pg.zDown.disabled = pg.zUp.disabled = !!why;
    pg.whyFine.textContent = why;
    pg.zNow.textContent = S.pos.z == null ? "" : "현재 Z " + f3(S.pos.z) + " mm";
    pg.saveHere.set(why || (S.move ? "이동 중" : ""));
    const search = c.contact_mode === "search";                  // [접촉 탐색] 은 전기 탐색을 고른 때에만 보인다
    pg.search.classList.toggle("hidden", !search);
    if (search) {
      pg.find.set(ct.active ? "탐색 중" : whyContact());
      pg.line.textContent = contactLine(ct);
      pg.line.className = ct.fail && !ct.active ? "st-warn" : "";
      pg.save.set(running() ? "측정 중 실행 불가" : ct.result && !ct.active ? "" : "[접촉 탐색] 필요");
    }
    const z = c.contact_z_mm, zf = P().z_floor || {};
    pg.saved.textContent = z == null ? "접촉 높이 없음" : "접촉 높이 Z " + f2(z) + " mm";
    pg.mode.textContent = "접촉 방식 " + (CONTACT_MODES[c.contact_mode] || c.contact_mode);
    pg.floor.textContent = zf.mode === "off" || !zf.mode ? "Z 하강 한도 끔" : zf.mm != null ? "Z 하강 한도 " + f2(zf.mm) + " mm" : zf.why;
    pg.jog.update();
  };
  return pg;
}

/* ---------- 7 · 쿨러 배치: "켜 보기"(쿨러 릴레이 하나씩 켜 보기)와 "칸 정하기"(고른 값을 누른 칸에 - 쿨러를 켜지 않아도 됨)를 나눈다 ----------
   칸마다 쿨러 릴레이 하나 또는 빈 칸(같은 릴레이를 여러 칸에 써도 된다 - 릴레이 하나에 쿨러 여러 개). 누를 때마다 저장(cooler_cell - 같은 값이 든 칸은 비운다).
   보이는 방향: 장비 앞 기준(아래 = 앞쪽 - state.prep.cooler.layout 이 초기 설정 5 꼭짓점으로 돌린 배치) / 사진 기준(왼쪽 위 = A). 저장은 언제나 cooler_grid(사진 기준).
   송풍 · 열선 · 미배정 릴레이는 여기 나오지 않는다. */
const PHOTO_CORNERS = { A: [0, 0], B: [0, 2], C: [2, 2], D: [2, 0] };
function buildStep7() {
  const pg = { relSig: "", pick: undefined, lastOn: null };
  pg.cells = [];
  pg.gridEl = h("div", { class: "cgrid" });
  for (let i = 0; i < 9; i++) {
    const b = btn("", () => put(i), "ccell");
    pg.cells.push(b); pg.gridEl.append(b);
  }
  pg.relBtns = {};
  pg.relBox = h("div", { class: "relays" });
  pg.pickBtns = [];
  pg.pickBox = h("div", { class: "row wrap pick" });
  pg.note = h("div", {});
  pg.why = h("div", { class: "why" });
  pg.cellWhy = h("div", { class: "why" });
  pg.off = btn("전체 끄기", () => api("cooler_only", { n: 0 }));
  pg.title = h("h4", {});
  pg.side = { top: h("div", { class: "cside top" }), bottom: h("div", { class: "cside bottom" }), left: h("div", { class: "cside left" }), right: h("div", { class: "cside right" }) };
  pg.el = h("div", { class: "panel prep-panel" }, h("h3", { text: "7 · 쿨러 배치" }),
    h("div", { class: "small", text: "쿨러를 켜 보고 냉각되는 칸 확인 · 값 고른 후 칸 누름" }),
    h("div", { class: "two" },
      h("div", { class: "grp" }, h("h4", { text: "켜 보기" }), pg.relBox, h("div", { class: "row" }, pg.off), pg.why),
      h("div", { class: "grp" }, pg.title, pg.pickBox,
        h("div", { class: "cboard" }, pg.side.top, pg.side.left, pg.gridEl, pg.side.right, pg.side.bottom),
        pg.cellWhy, pg.note)));
  const coolers = () => S.cfg.cooler_relays || [];
  function layout() {                                            // 문 기준 화면 배치(서버 - 공용 변환 geometry.to_view)
    const cv = P().cooler || {};
    return cv.layout || null;
  }
  function at(i) {                                               // 화면 칸 i(위 → 아래 · 왼쪽 → 오른쪽) → cooler_grid [행, 열]
    const r = Math.floor(i / 3), c = i % 3, lay = layout();
    return lay ? lay.cells[r][c] : [r, c];
  }
  async function put(i) {
    if (pg.pick === undefined) return;
    const [row, col] = at(i);
    await api("cooler_cell", { row, col, n: pg.pick });           // 같은 값이 든 칸이면 서버가 비운다
  }
  function onRelay() {                                           // 지금 켜 본 쿨러 릴레이(하나만 켜진다)
    for (const n of coolers()) if (S.relays[n - 1]) return n;
    return null;
  }
  function choose(v) { pg.pick = v; pg.render(); }
  pg.render = () => {
    const rs = JSON.stringify(coolers());
    if (rs !== pg.relSig) {                                      // 쿨러 릴레이 목록(전체 설정)이 바뀌면 버튼을 다시 만든다
      pg.relSig = rs;
      pg.relBtns = {};
      setKids(pg.relBox, ...coolers().map((n) => {
        const b = btn("R" + n, () => api("cooler_only", { n: S.relays[n - 1] ? 0 : n }));
        pg.relBtns[n] = b; return b;
      }));
      pg.pickBtns = [...coolers().map((n) => ({ v: n, b: btn("R" + n, () => choose(n)) })), { v: null, b: btn("비우기", () => choose(null)) }];
      setKids(pg.pickBox, ...pg.pickBtns.map((x) => x.b));
      if (pg.pick != null && coolers().indexOf(pg.pick) < 0) pg.pick = undefined;
    }
    const on = onRelay();
    if (on != null && on !== pg.lastOn) pg.pick = on;             // 켜 본 릴레이가 처음 고른 값
    pg.lastOn = on;
    if (pg.pick === undefined) pg.pick = coolers().length ? coolers()[0] : null;
    // ---- 켜 보기 ----
    const tt = !!(S.temptest && S.temptest.active);
    const why = whyStage(false, false) || (tt ? "온도 시험 중 실행 불가" : "");
    for (const [n, b] of Object.entries(pg.relBtns)) { const k = Number(n); b.classList.toggle("on", k === on); b.textContent = "R" + k + (k === on ? " 켜짐" : ""); b.disabled = !!why; }
    pg.off.disabled = !!why;
    pg.why.textContent = why;
    // ---- 칸 정하기(스테이지 없이도 됨 · 측정 · 온도 시험 중에는 설정을 바꾸지 않는다 - 서버도 거절) ----
    const cellWhy = running() ? "측정 중 실행 불가" : tt ? "온도 시험 중 실행 불가" : "";
    pg.pickBtns.forEach((x) => { x.b.classList.toggle("on", x.v === pg.pick); x.b.disabled = !!cellWhy; });
    const g = S.cfg.cooler_grid, lay = layout(), cv = P().cooler || {};
    const corners = lay ? lay.corners : PHOTO_CORNERS, tagAt = {};
    for (const n of "ABCD") tagAt[corners[n][0] * 3 + corners[n][1]] = n;
    pg.cells.forEach((b, i) => {
      const [r, c] = at(i), n = g[r][c], k = tagAt[i];
      const pos = k ? (corners[k][0] ? "b" : "t") + (corners[k][1] ? "r" : "l") : "";
      setKids(b, k ? h("span", { class: "ctag " + pos, text: k }) : null, h("span", { text: n == null ? "—" : "R" + n }));
      b.classList.toggle("on", n != null && n === on);
      b.classList.toggle("empty", n == null);
      b.disabled = !!cellWhy;
    });
    pg.cellWhy.textContent = cellWhy;
    pg.title.textContent = "판 · 문 기준 (아래 = 문)";
    pg.side.bottom.textContent = lay ? "문(" + cv.front + ")" : "";
    pg.side.top.textContent = lay ? "안쪽(" + cv.back + ")" : "";
    pg.side.right.textContent = lay ? "오른쪽(" + cv.right + ")" : "";
    pg.side.left.textContent = lay ? "왼쪽(" + cv.left + ")" : "";
    // ---- 상태 한 줄 ----
    const flat = g.flat(), empty = flat.filter((n) => n == null).length;
    const unused = coolers().filter((n) => flat.indexOf(n) < 0).map((n) => "R" + n);
    const saved = !!((P().steps || [])[6] || {}).done;
    pg.note.textContent = empty ? "빈 칸 " + empty + "개" : unused.length ? "배치에 없는 릴레이: " + unused.join(" · ") : saved ? "저장된 배치" : "기본 배치 · 확인 필요";
    pg.note.className = empty || unused.length || !saved ? "st-warn" : "small";
  };
  return pg;
}

/* ---------- 왼쪽 목록: 단계마다 끝났는지 + 저장된 값 한 줄 ---------- */
function renderPrepList() {
  const st = P().steps || [];
  prep.items.forEach((x, i) => {
    const s = st[i] || {};
    x.sub.textContent = s.text || "";
    x.sub.classList.toggle("st-warn", !!s.warn);                    // 주의가 필요한 단계 글(예: 7 "빈 칸 n개")
    x.sub.title = s.text || "";
    x.done.textContent = s.done ? "✓" : "";
  });
}
function renderPrep() {
  renderPrepList();
  const pg = prep.pages[prep.step];
  if (pg && pg.render) pg.render();
}

TABS.prep = { build: buildPrep, render: renderPrep };
