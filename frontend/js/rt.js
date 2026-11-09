/* rt.js - 측정 탭 "이 위치 측정": R–T(저항–온도) 측정. 모터는 사람이 조그로 핀을 샘플에 대고, 온도를 바꿔 가며 저항을 잰다.
   왼쪽: 저항–온도 그래프(세로 로그 · 가열/냉각 색 · 회차 진하기) + 온도–시간 그래프(목표 온도선 · 축 아래 히트건 켜짐 띠).
   그래프 라벨 · 숫자는 그림 바깥 띠에 두고 지시선으로만 잇는다(데이터 선과 겹치지 않는다).
   오른쪽: 위치(조그 · 접촉 높이) · 측정 조건(마지막 값 기억 · 기본값으로) · 상태 + [측정 시작] [중지]. */
"use strict";
const T = { pts: [], id: "", n: 0, jog: 1, open: { temp: true, ctl: false, k: false, misc: false }, sig: "", sampleNo: null, lastFetch: 0, busy: false, el: null, tick: 0 };
const C_HEAT = "#e8502a", C_COOL = "#2f6fdb", C_PRE = "#7c8691";
const PHASE_COL = (ph) => (ph === "가열" ? C_HEAT : ph === "냉각" ? C_COOL : C_PRE);

function shade(hex, k) {            // k 0(연함) ~ 1(진함): 흰색과 섞는다
  const n = parseInt(hex.slice(1), 16), r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255, m = (c) => Math.round(255 - (255 - c) * (0.35 + 0.65 * k));
  return "rgb(" + m(r) + "," + m(g) + "," + m(b) + ")";
}
function fmtE(v) { return v == null ? "—" : Number(v).toExponential(3).replace("e+", "E+").replace("e-", "E-"); }

/* ---- 그래프 공통 ---- */
function niceStep(span, target) { const raw = span / target, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p; return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * p; }
function svgEl(tag, a, text) { const e = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const k in a || {}) if (a[k] != null) e.setAttribute(k, a[k]); if (text != null) e.textContent = text; return e; }

/* 틀: 바깥 여백에 눈금 숫자 · 축 이름, 안쪽에 격자. 반환 {px, py, plot}. */
function frame(svg, W, H, m, xr, yr, o) {
  svg.replaceChildren();
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const px = (x) => m.l + ((x - xr[0]) / (xr[1] - xr[0])) * pw;
  const lg = o.ylog, ly = (y) => Math.log10(Math.max(y, 1e-300));
  const py = lg ? (y) => m.t + ph - ((ly(y) - ly(yr[0])) / (ly(yr[1]) - ly(yr[0]))) * ph : (y) => m.t + ph - ((y - yr[0]) / (yr[1] - yr[0])) * ph;
  svg.append(svgEl("rect", { x: m.l, y: m.t, width: pw, height: ph, fill: "#fbfcfd", stroke: "#9aa1a8" }));
  const xs = niceStep(xr[1] - xr[0], o.xticks || 10);
  for (let x = Math.ceil(xr[0] / xs) * xs; x <= xr[1] + 1e-9; x += xs) {
    svg.append(svgEl("line", { x1: px(x), x2: px(x), y1: m.t, y2: m.t + ph, stroke: "#e1e5e9" }));
    svg.append(svgEl("text", { x: px(x), y: m.t + ph + 18, "text-anchor": "middle", "font-size": 13, fill: "#3b434b" }, o.xfmt ? o.xfmt(x) : String(Math.round(x * 100) / 100)));
  }
  if (lg) {
    for (let e = Math.ceil(ly(yr[0]) - 1e-9); e <= ly(yr[1]) + 1e-9; e++) {
      const y = Math.pow(10, e);
      for (let k = 2; k < 10; k++) { const yy = k * y; if (yy < yr[1] && yy > yr[0]) svg.append(svgEl("line", { x1: m.l, x2: m.l + pw, y1: py(yy), y2: py(yy), stroke: "#eef1f3" })); }
      svg.append(svgEl("line", { x1: m.l, x2: m.l + pw, y1: py(y), y2: py(y), stroke: "#cfd5da" }));
      svg.append(svgEl("text", { x: m.l - 8, y: py(y) + 4, "text-anchor": "end", "font-size": 13, fill: "#3b434b" }, "1E+" + e));
    }
  } else {
    const ys = niceStep(yr[1] - yr[0], 6);
    for (let y = Math.ceil(yr[0] / ys) * ys; y <= yr[1] + 1e-9; y += ys) {
      svg.append(svgEl("line", { x1: m.l, x2: m.l + pw, y1: py(y), y2: py(y), stroke: "#e1e5e9" }));
      svg.append(svgEl("text", { x: m.l - 8, y: py(y) + 4, "text-anchor": "end", "font-size": 13, fill: "#3b434b" }, String(Math.round(y * 10) / 10)));
    }
  }
  svg.append(svgEl("text", { x: m.l + pw / 2, y: H - 6, "text-anchor": "middle", "font-size": 14, "font-weight": 700, fill: "#1d2126" }, o.xlabel));
  const yl = svgEl("text", { x: 16, y: m.t + ph / 2, "text-anchor": "middle", "font-size": 14, "font-weight": 700, fill: "#1d2126", transform: "rotate(-90 16 " + (m.t + ph / 2) + ")" }, o.ylabel);
  svg.append(yl);
  const clip = "clip" + Math.random().toString(36).slice(2, 7);
  const defs = svgEl("defs"); const cp = svgEl("clipPath", { id: clip }); cp.append(svgEl("rect", { x: m.l, y: m.t, width: pw, height: ph })); defs.append(cp); svg.append(defs);
  const g = svgEl("g", { "clip-path": "url(#" + clip + ")" });
  svg.append(g);
  return { px, py, g, pw, ph, m, W, H };
}
/* 많은 점은 path 하나로(둥근 끝 선 = 점) */
function dots(g, pts, col, r) { if (!pts.length) return; g.append(svgEl("path", { d: pts.map((p) => "M" + p[0].toFixed(1) + " " + p[1].toFixed(1) + "h0").join(""), stroke: col, "stroke-width": r * 2, "stroke-linecap": "round", fill: "none" })); }
function poly(g, pts, col, w, dash) { if (pts.length < 2) return; g.append(svgEl("path", { d: "M" + pts.map((p) => p[0].toFixed(1) + " " + p[1].toFixed(1)).join("L"), stroke: col, "stroke-width": w, fill: "none", "stroke-linejoin": "round", "stroke-dasharray": dash || null })); }
/* 읽는 값은 오른쪽 바깥 띠에 적고 지시선으로 마지막 점과 잇는다. 지시선은 데이터 점과 가장 덜 겹치는 방향(오른쪽 · 위 · 아래)으로 그림 밖까지 나간 뒤 바깥 띠를 따라 글자에 닿는다. */
function readout(f, last, lines, col, all) {
  const { m } = f, svg = f.g.parentNode, right = m.l + f.pw, bottom = m.t + f.ph;
  const x0 = right + 14, y0 = Math.max(m.t + 18, Math.min(bottom - 8 - lines.length * 18, last ? last[1] - 18 : m.t + 18));
  if (last && last[0] >= m.l && last[0] <= right) {
    const near = (ax, ay, bx, by) => (all || []).filter((p) => { if (Math.hypot(p[0] - last[0], p[1] - last[1]) < 9) return false; const dx = bx - ax, dy = by - ay, t = Math.max(0, Math.min(1, ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy || 1))); return Math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy)) < 5; }).length;
    const cand = [["r", near(last[0], last[1], right, last[1])], ["u", near(last[0], last[1], last[0], m.t)], ["d", near(last[0], last[1], last[0], bottom)]];
    cand.sort((a, b) => a[1] - b[1]);
    const dir = cand[0][0], ty = y0 + 8;
    let d;
    if (dir === "r") d = "M" + last[0] + " " + last[1] + "L" + right + " " + last[1] + "L" + (x0 - 4) + " " + ty;
    else if (dir === "u") d = "M" + last[0] + " " + last[1] + "L" + last[0] + " " + (m.t - 5) + "L" + (x0 - 4) + " " + (m.t - 5) + "L" + (x0 - 4) + " " + ty;
    else d = "M" + last[0] + " " + last[1] + "L" + last[0] + " " + (bottom + 4) + "L" + (x0 - 4) + " " + (bottom + 4) + "L" + (x0 - 4) + " " + ty;
    svg.append(svgEl("path", { d, stroke: col, "stroke-width": 1.2, fill: "none" }));
    svg.append(svgEl("circle", { cx: last[0], cy: last[1], r: 5.5, fill: "none", stroke: col, "stroke-width": 1.6 }));
  }
  lines.forEach((t, i) => svg.append(svgEl("text", { x: x0, y: y0 + 12 + i * 18, "font-size": i === 0 ? 15 : 13, "font-weight": i === 0 ? 700 : 400, fill: i === 0 ? col : "#3b434b" }, t)));
}

/* ---- 저항–온도 그래프 ---- */
function drawRT(svg, s) {
  const W = 1200, H = 470, m = { l: 78, r: 190, t: 36, b: 52 };
  const pts = T.pts.filter((p) => p.T != null && p.R != null && (p.ph === "가열" || p.ph === "냉각"));
  let x0 = 25, x1 = 90, y0 = 1e2, y1 = 1e6;
  if (pts.length) {
    const tx = pts.map((p) => p.T), ry = pts.map((p) => p.R);
    x0 = Math.min(x0, Math.floor(Math.min(...tx) / 5) * 5); x1 = Math.max(x1, Math.ceil(Math.max(...tx) / 5) * 5);
    y0 = Math.min(y0, Math.pow(10, Math.floor(Math.log10(Math.min(...ry))))); y1 = Math.max(y1, Math.pow(10, Math.ceil(Math.log10(Math.max(...ry)))));
  }
  const f = frame(svg, W, H, m, [x0, x1], [y0, y1], { ylog: true, xlabel: "Temp [°C]", ylabel: "Resistance [Ohm]", xticks: 13 });
  const cyc = Math.max(1, ...pts.map((p) => p.cy));
  const groups = {};
  pts.forEach((p) => { const k = p.ph + p.cy; (groups[k] = groups[k] || { ph: p.ph, cy: p.cy, a: [] }).a.push([f.px(p.T), f.py(p.R)]); });
  Object.values(groups).sort((a, b) => a.cy - b.cy).forEach((gp) => dots(f.g, gp.a, shade(PHASE_COL(gp.ph), cyc <= 1 ? 1 : 0.15 + 0.85 * (gp.cy - 1) / (cyc - 1)), 2.6));
  /* 위쪽 띠: 범례(그래프 바깥) */
  let x = m.l;
  [["가열", C_HEAT], ["냉각", C_COOL]].forEach(([l, c]) => { svg.append(svgEl("circle", { cx: x + 6, cy: 18, r: 5, fill: c })); svg.append(svgEl("text", { x: x + 16, y: 23, "font-size": 13, fill: "#3b434b" }, l)); x += 64; });
  svg.append(svgEl("text", { x: x + 6, y: 23, "font-size": 13, fill: "#5a626b" }, "회차가 늦을수록 진하게" + (cyc > 1 ? " · 1~" + cyc + "회" : "")));
  const last = pts.length ? pts[pts.length - 1] : null;
  readout(f, last ? [f.px(last.T), f.py(last.R)] : null, last ? [last.ph + " " + last.cy + "회", f1(last.T, 1) + " °C", fmtE(last.R) + " Ω"] : ["값 없음"], last ? PHASE_COL(last.ph) : "#8a929b", pts.map((p) => [f.px(p.T), f.py(p.R)]));
}

/* ---- 온도–시간 그래프 ---- */
function drawTt(svg, s) {
  const W = 1200, H = 330, m = { l: 78, r: 190, t: 40, b: 70 };
  const pts = T.pts;
  const tmax = Math.max(60, ...pts.map((p) => p.t));
  const tv = pts.filter((p) => p.T != null);
  const ymin = Math.min(20, ...(tv.length ? tv.map((p) => p.T) : [20])), ymax = Math.max(90, ...(tv.length ? tv.map((p) => p.T) : [90]), ...pts.map((p) => p.tgt == null ? 0 : p.tgt));
  const unitMin = tmax > 600;
  const f = frame(svg, W, H, m, [0, unitMin ? tmax / 60 : tmax], [Math.floor(ymin / 5) * 5, Math.ceil(ymax / 5) * 5], { xlabel: "Time [" + (unitMin ? "min" : "s") + "]", ylabel: "Temp [°C]", xticks: 12 });
  const X = (t) => f.px(unitMin ? t / 60 : t);
  const segs = [];
  pts.forEach((p) => { const k = p.ph + "|" + p.cy; const sg = segs[segs.length - 1]; if (sg && sg.k === k) { sg.a.push(p); } else segs.push({ k, ph: p.ph, cy: p.cy, a: [p] }); });
  segs.forEach((sg) => poly(f.g, sg.a.filter((p) => p.T != null).map((p) => [X(p.t), f.py(p.T)]), PHASE_COL(sg.ph), 2));
  poly(f.g, pts.filter((p) => p.tgt != null).map((p) => [X(p.t), f.py(p.tgt)]), "#2b3138", 1.5, "6 4");
  /* 축 아래 띠: 히트건 켜짐 구간 */
  const by = m.t + f.ph + 26;
  svg.append(svgEl("rect", { x: m.l, y: by, width: f.pw, height: 12, fill: "#eceef0", stroke: "#c3c7cc" }));
  let on = null;
  pts.forEach((p, i) => { if (p.h && on == null) on = p.t; if ((!p.h || i === pts.length - 1) && on != null) { const a = X(on), b = Math.max(a + 1.5, X(p.t)); f.g.parentNode.append(svgEl("rect", { x: a, y: by, width: b - a, height: 12, fill: "#ff5a1f" })); on = null; } });
  svg.append(svgEl("text", { x: m.l - 8, y: by + 10, "text-anchor": "end", "font-size": 12, fill: "#ff5a1f", "font-weight": 700 }, "히트건"));
  /* 위쪽 띠: 단계 이름(그래프 바깥), 경계는 위로 짧은 눈금 */
  segs.forEach((sg) => {
    const a = X(sg.a[0].t), b = X(sg.a[sg.a.length - 1].t);
    svg.append(svgEl("line", { x1: a, x2: a, y1: m.t - 10, y2: m.t, stroke: "#8a929b" }));
    if (b - a > 44) svg.append(svgEl("text", { x: (a + b) / 2, y: m.t - 14, "text-anchor": "middle", "font-size": 12, fill: PHASE_COL(sg.ph) }, sg.ph === "예비 냉각" ? "예비 냉각" : sg.ph + " " + sg.cy));
  });
  svg.append(svgEl("line", { x1: m.l + f.pw + 14, x2: m.l + f.pw + 44, y1: H - 22, y2: H - 22, stroke: "#2b3138", "stroke-dasharray": "6 4", "stroke-width": 1.5 }));
  svg.append(svgEl("text", { x: m.l + f.pw + 50, y: H - 18, "font-size": 12, fill: "#3b434b" }, "목표 온도선"));
  const last = tv.length ? tv[tv.length - 1] : null;
  readout(f, last ? [X(last.t), f.py(last.T)] : null, last ? [f1(last.T, 1) + " °C", last.tgt != null ? "목표 " + f1(last.tgt, 1) + " °C" : "목표선 없음(최대)", last.h ? "히트건 켜짐" : "히트건 꺼짐"] : ["값 없음"], last ? PHASE_COL(last.ph) : "#8a929b");
}

/* ---- 서버에서 점 받기 ---- */
async function fetchPts() {
  if (T.busy) return;
  T.busy = true;
  try {
    const r = await (await fetch("/rt.json?since=" + T.n + "&id=" + encodeURIComponent(T.id))).json();
    if (r.id !== T.id) { T.pts = []; T.n = 0; T.id = r.id; if (r.points.length === 0) { T.dirty = true; return; } }
    if (r.points.length) { T.pts.push(...r.points); T.n = r.n; T.dirty = true; }
  } catch (e) { /* 다음 주기에 */ } finally { T.busy = false; }
}
setInterval(() => {
  const s = UI.state;
  if (!s || UI.tab !== "measure" || M.sub !== "here") return;
  const run = s.rt && s.rt.run;
  const active = run && (run.state === "running" || run.state === "stopping");
  const need = active || (run && (run.n_points || 0) !== T.n) || !T.id;
  if (need) fetchPts().then(() => { if (T.dirty) { T.dirty = false; redrawGraphs(); } });
}, 400);
function redrawGraphs() { if (T.svgRT) drawRT(T.svgRT, UI.state); if (T.svgTt) drawTt(T.svgTt, UI.state); }

/* ---- 화면 만들기 ---- */
function buildHere() {
  T.svgRT = svgEl("svg", { class: "gsvg", id: "g-rt", preserveAspectRatio: "xMidYMid meet" });
  T.svgTt = svgEl("svg", { class: "gsvg", id: "g-tt", preserveAspectRatio: "xMidYMid meet" });
  T.left = h("div", { class: "col", id: "rt-left" }, h("div", { class: "graph gbox", id: "gb-rt" }, T.svgRT), h("div", { class: "graph gbox", id: "gb-tt" }, T.svgTt));
  T.right = h("div", { class: "col", id: "rt-right" });
  T.built = true;
}
function rtEdit(s) { return s.settings.rt; }
function rtLock(s) { const r = s.rt && s.rt.run; return r && (r.state === "running" || r.state === "stopping") ? "R–T 측정 중 · 변경 잠김" : ""; }
function numField(s, key, unit, step, lock, nullable) {
  const v = rtEdit(s)[key];
  const inp = h("input", { type: "number", step: step || "any", value: v == null ? "" : v, disabled: !!lock, "data-key": key,
    onchange: async (e) => { const raw = e.target.value; const val = raw === "" && nullable ? null : Number(raw); const r = await send("settings_save", { patch: { ["rt." + key]: val } }); if (!r.ok) { e.target.value = v == null ? "" : v; } } });
  return h("span", { class: "fld" }, inp, h("span", { class: "u", text: unit || "" }));
}
function selField(s, key, opts, lock) {
  const v = rtEdit(s)[key];
  return h("select", { disabled: !!lock, onchange: (e) => send("settings_save", { patch: { ["rt." + key]: e.target.value } }) }, opts.map(([val, l]) => h("option", { value: val, text: l, selected: val === v })));
}
function txtField(s, key, lock, w) {
  const v = rtEdit(s)[key];
  return h("input", { type: "text", value: v == null ? "" : v, disabled: !!lock, class: w || "", onchange: async (e) => { const r = await send("settings_save", { patch: { ["rt." + key]: e.target.value } }); if (!r.ok) e.target.value = v; } });
}
function chkField(s, key, label, lock) {
  const v = rtEdit(s)[key];
  return h("label", { class: "chk" }, h("input", { type: "checkbox", checked: !!v, disabled: !!lock, onchange: (e) => send("settings_save", { patch: { ["rt." + key]: e.target.checked } }) }), label);
}
function kvRow(label, ...ctl) { return [h("label", { text: label }), h("span", { class: "row" }, ...ctl)]; }
function group(name, title, ...kids) {
  return h("div", { class: "grp" }, h("div", { class: "gtitle", "data-act": "rt-open", "data-args": JSON.stringify({ g: name }), text: (T.open[name] ? "▾ " : "▸ ") + title }), T.open[name] ? h("div", { class: "rtgrid" }, ...kids.flat()) : null);
}

function renderHere(s) {
  if (!T.built) buildHere();
  const rt = s.rt, run = rt.run, active = run && (run.state === "running" || run.state === "stopping");
  const lock = rtLock(s), st = s.stage;
  if (M.body.firstChild !== T.left) M.body.replaceChildren(T.left, T.right);
  const sig = JSON.stringify([s.settings.rt, rt.inst, rt.block, rt.block_speed, T.open, T.jog, T.sampleNo, s.samples.map((p) => p.no + ":" + p.X), st && [st.locked, st.z_ok, st.moving, st.z > 0.05], s.contact, active, run && [run.state, run.status, run.message, run.warns, run.nas, run.speed], s.camera.busy, s.phase === "이동 중", s.run && s.run.state]);
  if (sig !== T.sig) { T.sig = sig; renderHereRight(s); }
  updateHereDynamic(s);
  if (T.pts.length && !T.drawn) { T.drawn = true; redrawGraphs(); }
  if (!T.drawn) { T.drawn = true; redrawGraphs(); }
}

function renderHereRight(s) {
  const rt = s.rt, run = rt.run, st = s.stage;
  const active = run && (run.state === "running" || run.state === "stopping");
  const lock = rtLock(s), tourBusy = s.run && ["running", "paused", "stopping"].includes(s.run.state);
  const busyWhy = lock || (tourBusy ? "샘플 순회 중" : "") || (s.camera.busy ? s.camera.busy + " 중" : "") || (!st ? "스테이지 연결 안 됨" : "") || (st && st.locked ? "비상정지 잠금" : "") || (st && !(st.z_ok && st.xy_ok) ? "원점 필요" : "");
  const moveWhy = busyWhy || (st && st.moving ? "이동 중" : "");
  /* 위치 카드 */
  const jb = (l, axis, mm, why) => btn(l, "rt-jog", { axis, mm }, "", "", why);
  const stepSel = h("div", { class: "row" }, ...(s.settings.jog.steps || [0.1, 1, 5, 10]).map((v) => btn(String(v), "rt-jstep", { v }, "sm " + (T.jog === v ? "on" : ""))), h("span", { class: "sub3", text: "mm" }));
  const pad = h("div", { class: "pad" }, h("span"), jb("Y−", "y", -T.jog, moveWhy), h("span"), jb("X−", "x", -T.jog, moveWhy), h("span", { class: "padc", text: "XY" }), jb("X+", "x", T.jog, moveWhy), h("span"), jb("Y+", "y", T.jog, moveWhy), h("span"));
  const zpad = h("div", { class: "col zpad" }, jb("Z ↑", "z", -T.jog, moveWhy), jb("Z ↓", "z", T.jog, moveWhy));
  const sampleSel = h("select", { onchange: (e) => { T.sampleNo = e.target.value ? Number(e.target.value) : null; T.sig = ""; } }, h("option", { value: "", text: "샘플 선택" }),
    ...s.samples.filter((p) => p.X != null).map((p) => h("option", { value: p.no, text: p.no + "번 · " + Math.round(p.X) + ", " + Math.round(p.Y), selected: T.sampleNo === p.no })));
  const pos = h("div", { class: "card" }, h("h3", { text: "위치" }),
    h("div", { class: "row" }, h("b", { class: "mono", id: "rt-pos", text: st ? xy(st.x, st.y) + " · Z " + f1(st.z, 2) : "—" }), h("span", { class: "grow" }), h("span", { class: "sub", text: s.contact.z == null ? "접촉 높이 없음" : "접촉 높이 Z " + f1(s.contact.z, 2) + " mm" })),
    h("div", { class: "row jrow" }, pad, zpad, h("div", { class: "col grow" }, stepSel,
      h("div", { class: "row" }, btn("접촉 높이로 내리기", "rt-down", null, "primary", "저장한 접촉 높이까지 내립니다. 지금 X · Y 에서 그대로 내리니 핀이 샘플 위인지 눈으로 확인하십시오. 마지막 " + s.contact.approach + " mm 는 느리게 내립니다.", moveWhy || (s.contact.z == null ? "접촉 높이 필요 · 장비 설정 7" : "")),
        btn("Z 맨 위", "rt-ztop", null, "", "Z 를 맨 위로 올립니다.", busyWhy)))),
    h("div", { class: "row" }, sampleSel, btn("샘플 위로", "rt-goto", null, "sm", "고른 샘플 위로 공구를 옮깁니다(좌표 보정이 있을 때).", moveWhy || (T.sampleNo == null ? "샘플을 먼저 고름" : ""))));
  /* 측정 조건 카드 */
  const L = lock, cs = rtEdit(s);
  const rows = [
    group("temp", "온도", ...kvRow("예비 냉각 목표", numField(s, "precool_c", "°C", 0.5, L)), ...kvRow("도달 뒤 유지", numField(s, "hold_s", "s", 1, L)),
      ...kvRow("max temp", numField(s, "max_temp", "°C", 1, L)), ...kvRow("temp to break", numField(s, "break_temp", "°C", 0.5, L)), ...kvRow("#repeat", numField(s, "repeat", "회", 1, L)),
      ...kvRow("가열 속도", numField(s, "heat_rate", "°C/분 · 0 최대", 0.5, L)), ...kvRow("냉각 속도", numField(s, "cool_rate", "°C/분 · 0 최대", 0.5, L))),
    group("ctl", "온도 제어 · 안전", ...kvRow("띠", numField(s, "band_c", "°C", 0.05, L)), ...kvRow("최소 켜짐", numField(s, "min_on_s", "s", 0.5, L)), ...kvRow("최소 꺼짐", numField(s, "min_off_s", "s", 0.5, L)),
      ...kvRow("비상 차단", numField(s, "emergency_c", "°C · 비움 = max+10", 1, L, true)), ...kvRow("단계 시간 한도", numField(s, "phase_limit_s", "s", 60, L)),
      ...kvRow("온도 갱신 한도", numField(s, "stale_s", "s", 0.5, L)), ...kvRow("최소 기록 간격", numField(s, "min_interval_s", "s", 0.1, L))),
    group("k", "K2450 · 열전대", ...kvRow("Set voltage", numField(s, "volt", "V", 0.1, L)), ...kvRow("전류 한도", numField(s, "ilim_a", "A", 0.001, L)),
      ...kvRow("필터 개수", numField(s, "filter_n", "", 1, L)), ...kvRow("NPLC", numField(s, "nplc", "", 0.01, L)),
      ...kvRow("범위 방식", selField(s, "range_mode", [["step", "단계(기존)"], ["auto", "장비 자동"]], L)), ...kvRow("시작 범위", numField(s, "start_range", "A", 0.001, L)),
      ...kvRow("4단자 · 단자", chkField(s, "four_wire", "4P", L), selField(s, "terminal", [["FRONT", "FRONT"], ["REAR", "REAR"]], L)), ...kvRow("열전대", selField(s, "tc_type", ["K", "J", "T", "E", "N", "R", "S", "B"].map((v) => [v, v]), L), h("span", { class: "sub", text: "기준점 내부" }))),
    group("misc", "쿨러 · 저장", ...kvRow("쿨러", selField(s, "cooler_target", [["sample", "샘플 구역"], ["all", "전부"], ["custom", "직접"]], L)),
      ...(cs.cooler_target === "custom" ? kvRow("릴레이 번호", txtField(s, "cooler_custom", L)) : []),
      ...kvRow("File path", txtField(s, "file_path", L, "wide")), ...kvRow("File name", txtField(s, "file_name", L)),
      ...kvRow("끝나면", chkField(s, "cooler_off_end", "쿨러 끔", L), chkField(s, "z_top_end", "Z 맨 위", L))),
  ];
  const cond = h("div", { class: "card grow scroll", id: "rt-cond" }, h("div", { class: "row" }, h("h3", { class: "grow", text: "측정 조건" }), btn("기본값으로", "rt-defaults", null, "sm", "측정 조건을 기본값(기존 프로그램 값)으로 되돌립니다. 장비 주소는 그대로입니다.", lock)), ...rows);
  /* 상태 카드 */
  const ik = rt.inst, ok1 = ik.k2110 && ik.k2110.ok, ok2 = ik.k2450 && ik.k2450.ok;
  const instRow = h("div", { class: "row wrap" }, h("span", { class: "chip " + (ok1 ? "ok" : "no"), text: "K2110 " + (ok1 ? "연결" : "연결 안 됨") }), h("span", { class: "chip " + (ok2 ? "ok" : "no"), text: "K2450 " + (ok2 ? "연결" : "연결 안 됨") }),
    ok1 && ok2 ? null : btn("장비 연결", "rt-connect", null, "sm primary", "두 장비에 연결합니다(읽기만 - 출력은 켜지 않습니다). 주소는 장비 설정 8 에서 고릅니다.", lock),
    h("span", { class: "grow" }), moreBtn([
      { label: "최대 속도 재기 (히트건 20 s · 쿨러 20 s)", fn: () => send("rt_speed", { s: 20 }), why: active ? "측정 중" : rt.block_speed },
      { label: "NAS 로 다시 복사", fn: async () => { const r = await send("rt_nas_retry"); if (r.ok) toast("NAS 로 복사함"); }, why: active ? "측정 중" : !run || run.mode !== "rt" ? "끝난 측정 없음" : "" },
      { label: "결과 폴더 열기", fn: () => send("open_folder", { path: run && run.dir }), why: run ? "" : "측정 없음" }]));
  const phaseText = !run ? "대기" : run.state === "running" || run.state === "stopping" ? phaseLabel(run) : run.status ? run.status + (run.message && run.status !== "완료" ? " · " + run.message : "") : "대기";
  const startWhy = active ? "측정 중" : rt.block;
  const spd = run && run.speed ? h("div", { class: "sub", text: "최대 속도 · 가열 " + run.speed.heat + " °C/분 · 냉각 " + run.speed.cool + " °C/분 (" + run.speed.s + " s 기준)" }) : null;
  const stat = h("div", { class: "card", id: "rt-stat" }, h("h3", { text: "상태" }), instRow,
    h("div", { class: "rtbig" }, h("div", {}, h("div", { class: "k", text: "온도" }), h("div", { class: "v", id: "rt-T", text: "—" })), h("div", {}, h("div", { class: "k", text: "저항" }), h("div", { class: "v", id: "rt-R", text: "—" })),
      h("div", {}, h("div", { class: "k", text: "전류" }), h("div", { class: "v", id: "rt-I", text: "—" }))),
    h("div", { class: "row" }, h("b", { id: "rt-phase", text: phaseText }), h("span", { class: "grow" }), h("span", { class: "sub", id: "rt-el", text: "" }), h("span", { class: "chip", id: "rt-heat", text: "히트건 꺼짐" }), h("span", { class: "chip", id: "rt-cool", text: "쿨러 꺼짐" })),
    spd, run && run.warns && run.warns.length ? h("div", { class: "why one", "data-tip": run.warns.join(" / "), text: run.warns[run.warns.length - 1] + (run.warns.length > 1 ? "  (+" + (run.warns.length - 1) + ")" : "") }) : null,
    h("div", { class: "row" }, btn("측정 시작", "rt-start", null, "primary lg grow", "예비 냉각 → 출력 켬 → 가열 · 냉각 반복을 시작합니다. 핀이 접촉 높이에 있어야 합니다.", startWhy), btn("중지", "rt-stop", null, "danger lg", "측정을 멈추고 히트건 · 출력을 끕니다.", active ? "" : "측정 중 아님")),
    h("div", { class: "why", text: startWhy && !active ? startWhy : "" }));
  T.right.replaceChildren(pos, cond, stat);
}
function phaseLabel(run) {
  const ph = run.phase || "";
  if (ph === "가열" || ph === "냉각") return ph + " " + run.cycle + " / " + run.repeat + "회";
  return ph || "시작 중";
}
function updateHereDynamic(s) {
  const run = s.rt && s.rt.run, st = s.stage;
  const set = (id, t) => { const e = $("#" + id); if (e && e.textContent !== t) e.textContent = t; };
  if (st) set("rt-pos", xy(st.x, st.y) + " · Z " + f1(st.z, 2));
  const live = run && (run.state === "running" || run.state === "stopping");
  if (run) {
    set("rt-T", run.T == null ? "—" : f1(run.T, 1) + " °C"); set("rt-R", run.R == null ? "—" : fmtE(run.R) + " Ω"); set("rt-I", run.I == null ? "—" : fmtE(run.I) + " A");
    if (live) set("rt-phase", phaseLabel(run)); set("rt-el", live || run.status ? "경과 " + mmss(run.elapsed) : "");
    const h_ = $("#rt-heat"), c_ = $("#rt-cool");
    if (h_) { h_.textContent = "히트건 " + (run.heater ? "켜짐" : "꺼짐"); h_.className = "chip" + (run.heater ? " warn" : ""); }
    if (c_) { c_.textContent = "쿨러 " + (run.cooler && run.cooler.length ? "켜짐 R" + run.cooler.join(",R") : "꺼짐"); c_.className = "chip" + (run.cooler && run.cooler.length ? " ok" : ""); }
  }
  if (T.dirty) { T.dirty = false; redrawGraphs(); }
}

UI.acts["rt-open"] = (a) => { T.open[a.g] = !T.open[a.g]; T.sig = ""; render(); };
UI.acts["rt-jstep"] = (a) => { T.jog = a.v; T.sig = ""; render(); };
UI.acts["rt-jog"] = (a) => send("jog", { axis: a.axis, mm: a.mm });
UI.acts["rt-down"] = async () => { const r = await send("contact_try", { free: true }); if (r.ok) toast("접촉 높이까지 내림"); };
UI.acts["rt-ztop"] = () => send("z_top");
UI.acts["rt-goto"] = () => { if (T.sampleNo != null) send("goto_sample", { no: T.sampleNo }); };
UI.acts["rt-defaults"] = async () => { const r = await send("rt_defaults"); if (r.ok) toast("기본값으로"); };
UI.acts["rt-connect"] = async () => { const r = await send("inst_connect"); if (r.ok) toast("장비 연결"); };
UI.acts["rt-start"] = async () => { T.pts = []; T.n = 0; T.id = ""; T.drawn = false; const r = await send("rt_start"); if (r.ok) toast("R–T 측정 시작"); };
UI.acts["rt-stop"] = () => dialog("측정을 중지할까요?", "지금 단계를 끊고 히트건을 끄고 K2450 출력을 끕니다.", [{ label: "계속 측정" }, { label: "중지", cls: "danger", fn: () => send("rt_stop") }]);
