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
  const xs = niceStep(xr[1] - xr[0], Math.max(3, Math.min(o.xticks || 10, Math.floor(pw / 62))));
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
function readout(f, last, lines, col, all, dx) {
  if (!last && lines.length === 1 && lines[0] === "값 없음") {                  // 데이터가 없으면 그래프 칸 안 가운데 옅은 안내
    const svg0 = f.g.parentNode;
    const cx = f.m.l + f.pw / 2, cy = f.m.t + f.ph / 2, two = f.pw < 240;                       // 칸이 좁으면 두 줄(눈금 글자와 겹치지 않게)
    (two ? ["측정하면", "여기에 그려집니다"] : ["측정하면 여기에 그려집니다"]).forEach((t, i, arr) =>
      svg0.append(svgEl("text", { x: cx, y: cy + (i - (arr.length - 1) / 2) * 18 + 5, "text-anchor": "middle", "font-size": two ? 13 : 15, fill: "#8a929b", opacity: 0.85 }, t)));
    return;
  }
  const { m } = f, svg = f.g.parentNode, right = m.l + f.pw, bottom = m.t + f.ph;
  const x0 = right + (dx || 14), y0 = Math.max(m.t + 18, Math.min(bottom - 8 - lines.length * 18, last ? last[1] - 18 : m.t + 18));
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
  const [W, H] = boxSize(svg, 1200, 470), m = { l: 66, r: Math.max(108, Math.min(150, W * 0.24)), t: 34, b: 48 };
  const pts = T.pts.filter((p) => p.T != null && p.R != null && (p.ph === "가열" || p.ph === "냉각"));
  const rg = UI.rtRange(pts.map((p) => p.T), pts.map((p) => p.R));
  const f = frame(svg, W, H, m, rg.x, rg.y, { ylog: true, xlabel: "온도 [°C]", ylabel: "저항 [Ω]", xticks: 13 });
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
  const [W, H] = boxSize(svg, 1200, 330), m = { l: 66, r: Math.max(108, Math.min(150, W * 0.24)), t: 38, b: 64 };
  const pts = T.pts;
  const tmax = Math.max(60, ...pts.map((p) => p.t));
  const tv = pts.filter((p) => p.T != null);
  const ymin = Math.min(20, ...(tv.length ? tv.map((p) => p.T) : [20])), ymax = Math.max(90, ...(tv.length ? tv.map((p) => p.T) : [90]), ...pts.map((p) => p.tgt == null ? 0 : p.tgt));
  const unitMin = tmax > 600;
  const f = frame(svg, W, H, m, [0, unitMin ? tmax / 60 : tmax], [Math.floor(ymin / 5) * 5, Math.ceil(ymax / 5) * 5], { xlabel: "시간 [" + (unitMin ? "분" : "s") + "]", ylabel: "온도 [°C]", xticks: 12 });
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
  svg.append(svgEl("line", { x1: m.l + f.pw + 14, x2: m.l + f.pw + 40, y1: H - 22, y2: H - 22, stroke: "#2b3138", "stroke-dasharray": "6 4", "stroke-width": 1.5 }));
  svg.append(svgEl("text", { x: m.l + f.pw + 46, y: H - 18, "font-size": 12, fill: "#3b434b" }, "목표선"));
  const last = tv.length ? tv[tv.length - 1] : null;
  readout(f, last ? [X(last.t), f.py(last.T)] : null, last ? [f1(last.T, 1) + " °C", last.tgt != null ? "목표 " + f1(last.tgt, 1) + " °C" : "목표 없음", last.h ? "히트건 켜짐" : "히트건 꺼짐"] : ["값 없음"], last ? PHASE_COL(last.ph) : "#8a929b");
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
  if (!s || UI.tab !== "measure") return;
  const run = s.rt && s.rt.run;
  const active = run && (run.state === "running" || run.state === "stopping");
  const need = active || (run && (run.n_points || 0) !== T.n) || !T.id;
  if (need) fetchPts().then(() => { if (T.dirty) { T.dirty = false; redrawGraphs(); } });
}, 400);
function redrawGraphs() { if (T.svgRT) drawRT(T.svgRT, UI.state); if (T.svgTt) drawTt(T.svgTt, UI.state); }

/* ---- 화면 만들기: 그래프 둘(카메라 아래) ---- */
function boxSize(svg, dw, dh) { const p = svg.parentNode, w = p && p.clientWidth, hh = p && p.clientHeight; return w > 200 && hh > 120 ? [w, hh] : [dw, dh]; }
function buildHere() {
  if (T.built) return;
  T.svgRT = svgEl("svg", { class: "gsvg", id: "g-rt", preserveAspectRatio: "xMidYMid meet" });
  T.svgTt = svgEl("svg", { class: "gsvg", id: "g-tt", preserveAspectRatio: "xMidYMid meet" });
  T.graphs = h("div", { class: "mgraphs" }, h("div", { class: "graph gbox", id: "gb-rt" }, T.svgRT), h("div", { class: "graph gbox", id: "gb-tt" }, T.svgTt));
  let raf = 0;
  const ro = new ResizeObserver(() => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => { if (T.svgRT && T.svgRT.parentNode.clientWidth) redrawGraphs(); }); });
  ro.observe(T.graphs); ro.observe(T.svgRT.parentNode); ro.observe(T.svgTt.parentNode);
  T.built = true;
}
function rtEdit(s) { return s.settings.rt; }
function rtLock(s) { const r = s.rt && s.rt.run; return r && (r.state === "running" || r.state === "stopping") ? "R–T 측정 중 · 변경 잠김" : ""; }
function phaseLabel(run) {
  const ph = run.phase || "";
  if (ph === "가열" || ph === "냉각") return ph + " " + run.cycle + " / " + run.repeat + "회";
  return ph || "시작 중";
}
UI.acts["rt-down"] = async () => { const r = await send("contact_try", { free: true }); if (r.ok) toast("접촉 높이까지 내림"); };
UI.acts["rt-ztop"] = () => send("z_top");
UI.acts["rt-goto"] = () => { if (T.sampleNo != null) send("goto_sample", { no: T.sampleNo }); };
UI.acts["rt-defaults"] = async () => { const r = await send("rt_defaults"); if (r.ok) toast("기본값으로"); };
UI.acts["rt-connect"] = async () => { const r = await send("inst_connect"); if (r.ok) toast("장비 연결"); };
UI.acts["rt-start"] = async () => { T.pts = []; T.n = 0; T.id = ""; T.drawn = false; const r = await send("rt_start"); if (r.ok) toast("R–T 측정 시작"); };
UI.acts["rt-stop"] = () => dialog("측정을 중지할까요?", "지금 단계를 끊고 히트건을 끄고 K2450 출력을 끕니다.", [{ label: "계속 측정" }, { label: "중지", cls: "danger", fn: () => send("rt_stop") }]);
