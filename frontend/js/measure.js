/* measure.js - 측정 탭: 준비(촬영 사진 · 샘플 · 측정 시작) 와 측정 중(온도 그래프 · 진행 · 기록 표). */
"use strict";
const M = { viewer: null, mode: "shot", zoom: "plate", tg: { no: true, pin: false, zone: false }, sig: {}, rightSig: "", leftSig: "", prepBuilt: false, mode_run: "continuous", cond: null };

/* ---- 그래프(측정 중 · 결과 공용) ---- */
function drawGraph(W, Hh, data) {
  const svg = sv("svg", { width: W, height: Hh, viewBox: "0 0 " + W + " " + Hh });
  const L = 58, R = 170, T = 14, B = 74, pw = W - L - R, ph = Hh - T - B;
  const ser = (data.series || []).filter((p) => p[1] != null);
  const tg = data.targets || {};
  const tmax = Math.max(60, (data.t_now || 0) * 1.12, ...(ser.length ? [ser[ser.length - 1][0] * 1.05] : [0]));
  const vals = ser.map((p) => p[1]).concat(Object.values(tg), [20]);
  let ymin = Math.floor(Math.min(...vals) / 10) * 10 - 5, ymax = Math.ceil(Math.max(...vals) / 10) * 10 + 10;
  if (ymax - ymin < 40) ymax = ymin + 40;
  const X = (t) => L + (t / tmax) * pw, Y = (v) => T + ph - ((v - ymin) / (ymax - ymin)) * ph;
  svg.append(sv("rect", { x: L, y: T, width: pw, height: ph, fill: "#fff", stroke: "#c3c7cc" }));
  for (let v = Math.ceil(ymin / 20) * 20; v <= ymax; v += 20) {
    svg.append(sv("line", { x1: L, x2: L + pw, y1: Y(v), y2: Y(v), stroke: "#e3e6e9" }));
    svg.append(sv("text", { x: L - 6, y: Y(v) + 4, "text-anchor": "end", "font-size": 12, fill: "#5a626b" }, String(v)));
  }
  const dt = tmax > 400 ? 100 : tmax > 150 ? 50 : 20;
  for (let t = 0; t <= tmax; t += dt) {
    svg.append(sv("line", { x1: X(t), x2: X(t), y1: T + ph, y2: T + ph + 4, stroke: "#5a626b" }));
    svg.append(sv("text", { x: X(t), y: T + ph + 17, "text-anchor": "middle", "font-size": 12, fill: "#5a626b" }, String(t)));
  }
  svg.append(sv("text", { x: L + pw / 2, y: Hh - 4, "text-anchor": "middle", "font-size": 12, fill: "#5a626b" }, "시간 s"));
  svg.append(sv("text", { x: 14, y: T + 10, "font-size": 12, fill: "#5a626b" }, "°C"));
  /* 단계 구간(축 아래 띠) */
  const BC = { "온도 대기": "#d9dde2", "가열": "#ffd3c1", "냉각": "#cfe0fb" };
  (data.bands || []).forEach((b) => {
    if (b.t0 == null) return;
    const t1 = b.t1 == null ? (data.t_now || b.t0) : b.t1;
    const x0 = X(b.t0), x1 = Math.max(X(t1), x0 + 2);
    svg.append(sv("rect", { x: x0, y: T + ph + 24, width: x1 - x0, height: 20, fill: BC[b.name] || "#ddd", stroke: "#9aa1a8" }));
    if (x1 - x0 > 46) svg.append(sv("text", { x: (x0 + x1) / 2, y: T + ph + 38, "text-anchor": "middle", "font-size": 12 }, b.name));
  });
  /* 목표선(점선) + 오른쪽 띠 라벨 + 지시선 */
  const labels = [];
  const lines = [["heat", "가열 목표", "#ff5a1f"], ["cool", "냉각 목표", "#2f6fdb"], ["start", "시작 온도", "#5a626b"]];
  lines.forEach(([k, name, col]) => {
    if (tg[k] == null) return;
    svg.append(sv("line", { x1: L, x2: L + pw, y1: Y(tg[k]), y2: Y(tg[k]), stroke: col, "stroke-dasharray": "6 4", "stroke-width": 1.5 }));
    labels.push({ y: Y(tg[k]), text: name + " " + tg[k] + " °C", col, x: L + pw });
  });
  if (ser.length) {
    svg.append(sv("polyline", { points: ser.map((p) => X(p[0]) + "," + Y(p[1])).join(" "), fill: "none", stroke: "#2f6fdb", "stroke-width": 2.2 }));
    const last = ser[ser.length - 1];
    svg.append(sv("circle", { cx: X(last[0]), cy: Y(last[1]), r: 4.5, fill: "#2f6fdb" }));
    labels.push({ y: Y(last[1]), text: "지금 " + f1(last[1]) + " °C", col: "#2f6fdb", x: X(last[0]) + 5 });
  }
  if (data.t_now != null) svg.append(sv("line", { x1: X(data.t_now), x2: X(data.t_now), y1: T, y2: T + ph + 48, stroke: "#1d2126", "stroke-dasharray": "2 3" }));
  if (data.t_now != null) svg.append(sv("text", { x: X(data.t_now), y: T + ph + 60, "text-anchor": "middle", "font-size": 12, "font-weight": 700 }, "지금"));
  /* 라벨은 그래프 오른쪽 바깥 띠에만 두고 지시선으로 잇는다 */
  labels.sort((a, b) => a.y - b.y);
  let prev = -1e9;
  labels.forEach((l) => { l.ly = Math.max(l.y, prev + 20); prev = l.ly; });
  const over = prev - (T + ph);
  if (over > 0) labels.forEach((l) => { l.ly -= over; });
  labels.forEach((l) => {
    svg.append(sv("polyline", { points: l.x + "," + l.y + " " + (L + pw + 8) + "," + l.y + " " + (L + pw + 18) + "," + l.ly, fill: "none", stroke: l.col, "stroke-width": 1 }));
    svg.append(sv("text", { x: L + pw + 22, y: l.ly + 4, "font-size": 13, fill: l.col, "font-weight": 700 }, l.text));
  });
  return svg;
}

/* ---- 준비 화면의 왼쪽(사진 보기) ---- */
function buildPrep() {
  const pane = $("#tab-measure");
  M.viewer = new Viewer();
  M.head = h("div", { class: "vhead" });
  M.legend = h("div", { class: "legend", html: '<span><i class="lg-n"></i>인식</span><span><i class="lg-s"></i>선택</span><span><i class="lg-w"></i>확인 필요</span><span><i class="lg-x"></i>측정 제외</span>' });
  M.logbox = h("div", { class: "loglines" });
  M.mini = sv("svg", { class: "minimap", width: 190, height: 190, viewBox: "0 0 190 190" });
  M.viewer.el.append(M.mini);
  M.viewer.onClick = onViewClick;
  M.viewer.onApply = redrawOverlay;
  M.leftPrep = h("div", { class: "viewbox", id: "m-view", style: null }, M.head, M.viewer.el, M.legend, M.logbox);
  M.leftRun = h("div", { class: "col hidden", id: "m-run-left" });
  M.right = h("div", { class: "col", id: "m-right" });
  M.leftWrap = h("div", { class: "col", id: "m-left" }, M.leftPrep, M.leftRun);
  pane.replaceChildren(M.leftWrap, M.right);
  UI.logRenders = UI.logRenders || [];
  UI.logRenders.push(renderLogBox);
  setInterval(refreshLive, 250);
  M.prepBuilt = true;
}
function renderLogBox() {
  if (!M.logbox) return;
  const lines = (UI.logTail || []).slice(-3);
  M.logbox.replaceChildren(h("div", { class: "t" }, lines.map((l) => h("div", { class: l.lv, text: l.t + "  " + l.msg }))),
    h("button", { class: "btn sm", "data-act": "log-all", text: "전체 보기" }));
}
UI.acts["log-all"] = () => {
  const box = h("div", { class: "loglines mono scroll" }, UI.log.slice(-300).map((l) => h("div", { class: l.lv, text: l.t + "  " + l.msg })));
  box.style.height = "620px"; box.style.flexDirection = "column"; box.style.fontSize = "13px";
  openWin(h("div", {}, h("h2", { text: "로그 전체" }), box, h("div", { class: "btns" }, h("button", { class: "btn", text: "닫기", onclick: closeWin }))), "big");
};

function refreshLive() {
  const s = UI.state;
  if (!s || UI.tab !== "measure" || M.mode !== "live" || !M.viewer) return;
  const sz = s.camera.size || [1280, 960];
  M.viewer.set("/preview.jpg", sz[0], sz[1], Date.now());
}

function renderMeasure(s) {
  if (!M.prepBuilt) buildPrep();
  const run = s.run, running = run && ["running", "paused", "stopping"].includes(run.state);
  M.leftPrep.classList.toggle("hidden", !!running);
  M.leftRun.classList.toggle("hidden", !running);
  if (UI.tab !== "measure") return;
  if (running) renderRunLeft(s); else renderPrepLeft(s);
  const sig = JSON.stringify([running, s.samples, s.sel, s.calib.status, s.calib.text, s.shot && s.shot.id, s.camera.cap_text, s.camera.busy, s.phase, s.start_block, s.active_condition,
    s.conditions, s.can_undo, M.mode_run, run && [run.state, run.cur, run.waiting, run.done, run.paused, Object.keys(run.results)], s.stage && [s.stage.locked, s.stage.z_ok], s.ref && s.ref.time]);
  if (sig !== M.rightSig) { M.rightSig = sig; if (running) renderRunRight(s); else renderPrepRight(s); }
  if (running) updateRunDynamic(s);
}

/* ---- 준비: 왼쪽 ---- */
function renderPrepLeft(s) {
  const sh = s.shot;
  const head = M.head;
  const seg = h("div", { class: "seg" }, btn("실시간", "m-mode", { m: "live" }, M.mode === "live" ? "on" : ""), btn("촬영 사진", "m-mode", { m: "shot" }, M.mode === "shot" ? "on" : ""));
  head.replaceChildren(seg,
    h("span", { class: "sub", text: M.mode === "shot" ? (sh ? "촬영 " + sh.time : "촬영 없음") : "실시간" }),
    h("span", { class: "sub", text: sh ? "샘플 " + s.samples.length + "개" : "" }), h("span", { class: "grow" }),
    tgBtn("번호", "no"), tgBtn("핀", "pin"), tgBtn("쿨러 구역", "zone"),
    h("span", { class: "sub", text: "배율" }),
    btn("판 맞춤", "m-fit", { m: "plate" }, "sm " + (M.zoom === "plate" ? "on" : "")), btn("전체", "m-fit", { m: "all" }, "sm " + (M.zoom === "all" ? "on" : "")),
    btn("−", "m-zoom", { f: 1 / 1.25 }, "sm"), btn("+", "m-zoom", { f: 1.25 }, "sm"));
  M.viewer.mode = M.zoom;
  if (M.mode === "shot") {
    if (sh) { M.viewer.area = sh.area || s.calib.area; M.viewer.set("/shot.jpg", sh.size[0], sh.size[1], sh.id); }
    else M.viewer.set(null);
  } else {
    const sz = s.camera.size || [1280, 960];
    M.viewer.area = s.calib.area;
    if (!M.viewer.W) M.viewer.set("/preview.jpg", sz[0], sz[1], Date.now());
  }
  M.viewer.message(s.camera.cap_text && s.camera.busy ? s.camera.busy + " · " + s.camera.cap_text : "");
  redrawOverlay();
  renderLogBox();
  if (!M.viewer.el.querySelector(".cap-err")) { /* 촬영 실패 알림은 아래 */ }
  const ce = s.camera.cap_error;
  $$(".cap-err", M.viewer.el).forEach((e) => e.remove());
  if (ce && Date.now() / 1000 - ce.id < 8) M.viewer.el.append(h("div", { class: "cap-err", text: "촬영 실패 · " + ce.text }));
}
function tgBtn(label, key) { return btn(label, "m-tg", { k: key }, "sm " + (M.tg[key] ? "on" : "")); }
UI.acts["m-mode"] = (a) => { M.mode = a.m; M.viewer.W = 0; M.viewer.key = null; send("preview", {}); if (UI.ws) UI.ws.send(JSON.stringify({ cmd: "preview", on: a.m === "live" })); render(); };
UI.acts["m-fit"] = (a) => { M.zoom = a.m; M.viewer.fit(a.m, M.viewer.area); render(); };
UI.acts["m-zoom"] = (a) => M.viewer.zoomCenter(a.f);
UI.acts["m-tg"] = (a) => { M.tg[a.k] = !M.tg[a.k]; render(); };

function onViewClick(u, v, e) {
  const s = UI.state;
  if (M.mode !== "shot" || !s.shot) return;
  if (M.addMode) {
    M.addMode = false;
    send("sample_add", { u, v });
    return;
  }
  if (M.moveNo != null) {
    send("sample_move", { no: M.moveNo, u, v });
    M.moveNo = null;
    return;
  }
  /* 가장 가까운 샘플 선택 */
  let best = null, bd = 1e9;
  s.samples.forEach((p) => { const d = Math.hypot(p.u - u, p.v - v); if (d < bd) { bd = d; best = p; } });
  if (best && bd < 60 / M.viewer.k + 25) send("sample_select", { no: best.no });
}

function sampleBBox(p) {
  if (p.verts && p.verts.length >= 3) {
    const xs = p.verts.map((q) => q[0]), ys = p.verts.map((q) => q[1]);
    return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
  }
  return [p.u - 12, p.v - 12, p.u + 12, p.v + 12];
}
/* 쿨러 구역 경계: 판 사각형(촬영 프레임) 위의 투영 변환으로 mm 격자를 사진에 그린다 */
function projective(src, dst) {
  const A = [], b = [];
  for (let i = 0; i < 4; i++) {
    const [x, y] = src[i], [u, v] = dst[i];
    A.push([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.push(u);
    A.push([0, 0, 0, x, y, 1, -v * x, -v * y]); b.push(v);
  }
  const n = 8;
  for (let i = 0; i < n; i++) {
    let p = i; for (let r = i + 1; r < n; r++) if (Math.abs(A[r][i]) > Math.abs(A[p][i])) p = r;
    [A[i], A[p]] = [A[p], A[i]]; [b[i], b[p]] = [b[p], b[i]];
    for (let r = i + 1; r < n; r++) { const f = A[r][i] / A[i][i]; for (let c = i; c < n; c++) A[r][c] -= f * A[i][c]; b[r] -= f * b[i]; }
  }
  const x = new Array(n);
  for (let i = n - 1; i >= 0; i--) { let t = b[i]; for (let c = i + 1; c < n; c++) t -= A[i][c] * x[c]; x[i] = t / A[i][i]; }
  return (X, Y) => { const w = x[6] * X + x[7] * Y + 1; return [(x[0] * X + x[1] * Y + x[2]) / w, (x[3] * X + x[4] * Y + x[5]) / w]; };
}

function redrawOverlay() {
  const s = UI.state, v = M.viewer;
  if (!s || !v || !v.W) { if (v) v.clear(); return; }
  v.clear();
  if (M.mode !== "shot" || !s.shot) {
    if (s.calib.area && M.mode === "live") v.svg.append(sv("polygon", { points: s.calib.area.map((p) => p.join(",")).join(" "), fill: "none", stroke: "#2f6fdb", "stroke-width": v.px(2), "stroke-dasharray": v.px(8) + " " + v.px(5) }));
    return;
  }
  const k = v.k, lw = (n) => n / k;
  const area = s.shot.area || s.calib.area;
  if (area) v.svg.append(sv("polygon", { points: area.map((p) => p.join(",")).join(" "), fill: "none", stroke: "#8fb4f2", "stroke-width": lw(1.5), "stroke-dasharray": lw(8) + " " + lw(5) }));
  const size = s.calib.size_mm || s.settings.calib.area_size_mm;
  if (M.tg.zone && area && s.calib.status === "done") {
    const z = s.settings.cooler.zones, f = projective([[0, 0], [size[0], 0], [size[0], size[1]], [0, size[1]]], area);
    const re = z.row_edges || Array.from({ length: z.rows + 1 }, (_, i) => (size[1] * i) / z.rows), ce = z.col_edges || Array.from({ length: z.cols + 1 }, (_, i) => (size[0] * i) / z.cols);
    const gl = [];
    re.forEach((y) => gl.push([f(ce[0], y), f(ce[ce.length - 1], y)]));
    ce.forEach((x) => gl.push([f(x, re[0]), f(x, re[re.length - 1])]));
    gl.forEach((l) => v.svg.append(sv("line", { x1: l[0][0], y1: l[0][1], x2: l[1][0], y2: l[1][1], stroke: "#ffe08a", "stroke-width": lw(1), "stroke-dasharray": lw(4) + " " + lw(4) })));
    z.cells.forEach((c, i) => { const r = Math.floor(i / z.cols), cc = i % z.cols, p = f((ce[cc] + ce[cc + 1]) / 2, (re[r] + re[r + 1]) / 2);
      v.svg.append(sv("text", { x: p[0], y: p[1], "text-anchor": "middle", "font-size": lw(15), fill: "#ffe08a", opacity: 0.8 }, "쿨러 " + (c.cooler || i + 1))); });
  }
  const items = s.samples.map((p) => ({ bbox: sampleBBox(p), p }));
  items.forEach(({ p }) => {
    const sel = p.no === s.sel, col = sel ? "#2f6fdb" : p.excluded ? "#9aa1a8" : p.weak ? "#e08a1e" : "#f2f4f6";
    const sw = lw(sel ? 4 : 1.6);
    if (p.verts && p.verts.length >= 3) v.svg.append(sv("polygon", { points: p.verts.map((q) => q.join(",")).join(" "), fill: "none", stroke: col, "stroke-width": sw, "stroke-dasharray": p.excluded ? lw(5) + " " + lw(4) : null }));
    else v.svg.append(sv("circle", { cx: p.u, cy: p.v, r: lw(12), fill: "none", stroke: col, "stroke-width": sw }));
    if (M.tg.pin) drawPin(v, s, p, lw);
  });
  if (M.tg.no) {
    const bw = lw(30), bh = lw(20);
    const pl = placeBadges(items, v.W, v.H, bw, bh);
    items.forEach(({ p }, i) => {
      const { box, from } = pl[i], sel = p.no === s.sel;
      const col = sel ? "#2f6fdb" : p.weak ? "#e08a1e" : "#2b3138";
      const cx = (box[0] + box[2]) / 2, cy = (box[1] + box[3]) / 2;
      v.svg.append(sv("line", { x1: from[0], y1: from[1], x2: cx, y2: cy, stroke: col, "stroke-width": lw(1) }));
      v.svg.append(sv("rect", { x: box[0], y: box[1], width: bw, height: bh, rx: lw(3), fill: col, opacity: 0.94 }));
      v.svg.append(sv("text", { x: cx, y: cy + lw(5), "text-anchor": "middle", "font-size": lw(14), fill: "#fff", "font-weight": 700 }, String(p.no)));
    });
  }
  /* 선택 샘플 보이기 */
  const sp = s.samples.find((p) => p.no === s.sel);
  if (sp && M.focusSel !== s.sel) { M.focusSel = s.sel; }
  drawMinimap(s);
}
function drawPin(v, s, p, lw) {
  const tool = s.tool, jac = pinJac(s);
  if (!jac) return;
  const w = Number(tool.tip_w_mm || 10), hh = Number(tool.tip_h_mm || 2), ang = ((tool.tip_angle_deg == null ? 90 : tool.tip_angle_deg) * Math.PI) / 180;
  const ex = [Math.cos(ang), Math.sin(ang)], ey = [-Math.sin(ang), Math.cos(ang)];
  const pt = (a, b) => { const mx = (ex[0] * a + ey[0] * b), my = (ex[1] * a + ey[1] * b); return [p.u + (jac.x[0] * mx + jac.y[0] * my), p.v + (jac.x[1] * mx + jac.y[1] * my)]; };
  const pts = [pt(-w / 2, -hh / 2), pt(w / 2, -hh / 2), pt(w / 2, hh / 2), pt(-w / 2, hh / 2)];
  let out = false;
  if (p.verts && p.verts.length >= 3) out = !pts.every((q) => pointInPoly(q, p.verts));
  v.svg.append(sv("polygon", { points: pts.map((q) => q.join(",")).join(" "), fill: out ? "rgba(224,138,30,.25)" : "rgba(43,154,88,.2)", stroke: out ? "#e08a1e" : "#2b9a58", "stroke-width": lw(1.5) }));
  if (out) v.svg.append(sv("text", { x: p.u, y: p.v + lw(30), "text-anchor": "middle", "font-size": lw(13), fill: "#e08a1e", "font-weight": 700 }, "핀 벗어남"));
}
function pinJac(s) {
  const c = s.calib;
  if (!c.area_mm || !c.area) return null;
  const q = c.area, m = c.area_mm;           // 기준 프레임 사각형 ↔ mm : 국소 야코비안 근사(사각형 변으로)
  const dx = [(q[1][0] - q[0][0] + q[2][0] - q[3][0]) / 2, (q[1][1] - q[0][1] + q[2][1] - q[3][1]) / 2];
  const dy = [(q[3][0] - q[0][0] + q[2][0] - q[1][0]) / 2, (q[3][1] - q[0][1] + q[2][1] - q[1][1]) / 2];
  const mx = [(m[1][0] - m[0][0] + m[2][0] - m[3][0]) / 2, (m[1][1] - m[0][1] + m[2][1] - m[3][1]) / 2];
  const my = [(m[3][0] - m[0][0] + m[2][0] - m[1][0]) / 2, (m[3][1] - m[0][1] + m[2][1] - m[1][1]) / 2];
  // px = J * mm  → J = P * inv(Mm)
  const det = mx[0] * my[1] - my[0] * mx[1];
  if (Math.abs(det) < 1e-9) return null;
  const inv = [[my[1] / det, -my[0] / det], [-mx[1] / det, mx[0] / det]];
  const P = [[dx[0], dy[0]], [dx[1], dy[1]]];
  const J = [[P[0][0] * inv[0][0] + P[0][1] * inv[1][0], P[0][0] * inv[0][1] + P[0][1] * inv[1][1]], [P[1][0] * inv[0][0] + P[1][1] * inv[1][0], P[1][0] * inv[0][1] + P[1][1] * inv[1][1]]];
  return { x: [J[0][0], J[1][0]], y: [J[0][1], J[1][1]] };       // 스테이지 +X · +Y 1 mm 의 사진 이동(px)
}
function drawMinimap(s) {
  const m = M.mini; m.replaceChildren();
  const am = s.calib.area_mm;
  m.classList.toggle("hidden", !am || !s.stage);
  if (!am || !s.stage) return;
  const xs = am.map((p) => p[0]), ys = am.map((p) => p[1]);
  const x0 = Math.min(...xs) - 10, x1 = Math.max(...xs) + 10, y0 = Math.min(...ys) - 10, y1 = Math.max(...ys) + 10;
  const sc = Math.min(160 / (x1 - x0), 150 / (y1 - y0));
  const P = (x, y) => [15 + (y - y0) * sc, 20 + (x - x0) * sc];           // 위 X+, 왼쪽 Y+ 가 아니라 단순 배치: 가로 Y · 세로 X
  m.append(sv("polygon", { points: am.map((p) => P(p[0], p[1]).join(",")).join(" "), fill: "#fff", stroke: "#8fb4f2", "stroke-dasharray": "4 3" }));
  s.samples.forEach((p) => { if (p.X == null) return; const q = P(p.X, p.Y); m.append(sv("circle", { cx: q[0], cy: q[1], r: p.no === s.sel ? 5 : 3, fill: p.no === s.sel ? "#2f6fdb" : p.weak ? "#e08a1e" : "#5a626b" })); });
  if (s.stage.x != null) { const q = P(s.stage.x, s.stage.y); m.append(sv("path", { d: "M" + (q[0] - 6) + " " + q[1] + "H" + (q[0] + 6) + "M" + q[0] + " " + (q[1] - 6) + "V" + (q[1] + 6), stroke: "#d03a3a", "stroke-width": 2 })); }
  m.append(sv("text", { x: 176, y: 14, "text-anchor": "end", "font-size": 11, fill: "#5a626b" }, "가로 Y → · 세로 X ↓"));
}

/* ---- 준비: 오른쪽 ---- */
function renderPrepRight(s) {
  const r = M.right, sh = s.shot, sel = s.samples.find((p) => p.no === s.sel);
  const busy = !!s.camera.busy;
  const noStage = !s.stage;
  /* 1 촬영 */
  const refTxt = s.ref ? "빈 판 사진 " + s.ref.time : "빈 판 사진 없음 · 장비 설정 3";
  const card1 = h("div", { class: "card" }, h("h3", {}, h("span", { class: "n", text: "1" }), "촬영"),
    h("div", { class: "row" }, btn("촬영", "capture", null, "primary lg", "촬영 위치로 가서 한 장 찍고 칩을 인식합니다.", busy ? s.camera.busy + " 중" : (s.run && ["running", "paused"].includes(s.run.state)) ? "측정 중" : s.calib.area ? "" : "판 영역 필요 · 장비 설정 2"),
      h("span", { class: "sub grow", text: refTxt })),
    h("div", { class: "stats" }, stat("인식", sh ? s.samples.length : "—"), stat("확인 필요", sh ? s.n_weak : "—", s.n_weak ? "warn" : ""),
      stat("카메라 이동", sh && sh.cam ? f1(sh.cam.shift_mm, 2) + " mm" : "—")),
    sh && sh.warnings.length ? h("div", { class: "sub3", text: sh.warnings.slice(0, 2).join(" · ") }) : null);
  /* 2 샘플 */
  const nTarget = s.samples.filter((p) => !p.excluded).length;
  const grid = h("div", { class: "sgrid" }, s.samples.map((p) => h("div", { class: "sc" + (p.no === s.sel ? " sel" : "") + (p.weak ? " weak" : "") + (p.excluded ? " exc" : ""), "data-act": "sel", "data-args": JSON.stringify({ no: p.no }) },
    h("b", { text: p.no }), h("span", { text: p.X == null ? "—" : Math.round(p.X) + ", " + Math.round(p.Y) }))));
  const selrow = sel ? h("div", { class: "selrow" }, h("b", { text: sel.no + "번" }), h("span", { text: xy(sel.X, sel.Y) }), h("span", { class: "sub", text: sel.cooler ? "쿨러 " + sel.cooler : "쿨러 —" }),
    btn("이동", "goto", { no: sel.no }, "sm", "이 샘플 위로 공구를 옮깁니다.", sel.X == null ? "좌표 없음 · 좌표 보정 필요" : noStage ? "스테이지 연결 안 됨" : ""),
    btn(sel.excluded ? "측정 포함" : "측정 제외", "exclude", { no: sel.no, on: !sel.excluded }, "sm"),
    btn("위치 수정", "movepos", { no: sel.no }, "sm" + (M.moveNo === sel.no ? " on" : ""), "사진에서 새 위치를 누릅니다."),
    sel.weak ? btn("확인 완료", "confirm_s", { no: sel.no }, "sm") : null, btn("삭제", "delete", { no: sel.no }, "sm")) : h("div", { class: "sub3", text: "샘플을 사진 · 칩 · 지도에서 고릅니다." });
  const card2 = h("div", { class: "card" }, h("h3", {}, h("span", { class: "n", text: "2" }), "샘플"),
    h("div", { class: "row" }, h("span", { class: "grow", text: s.samples.length + "개 · 측정 대상 " + nTarget }), btn("추가", "addmode", null, "sm" + (M.addMode ? " on" : ""), "사진에서 칩을 눌러 추가합니다.", sh ? "" : "촬영 필요"),
      btn("현재 위치를 샘플로", "here", null, "sm", "공구가 있는 자리를 샘플로 추가합니다.", sh ? (s.calib.status === "done" ? "" : "좌표 보정 필요") : "촬영 필요"),
      btn("되돌리기", "undo", null, "sm", "", s.can_undo ? "" : "되돌릴 편집 없음")),
    grid, selrow);
  /* 3 측정 */
  const conds = s.conditions, act = M.cond && conds.find((c) => c.name === M.cond) ? M.cond : s.active_condition;
  const cv = conds.find((c) => c.name === act) || conds[0];
  const sel_ = h("select", { onchange: (e) => { M.cond = e.target.value; send("settings_save", { patch: { "condition.active": M.cond } }); } }, conds.map((c) => h("option", { value: c.name, text: c.name, selected: c.name === act })));
  const chips = readyChips(s);
  const nStart = M.mode_run === "selected" ? (s.sel != null ? 1 : 0) : nTarget;
  const block = s.start_block.why;
  const startBtn = btn("측정 시작 · " + nStart + "개", "mstart", null, "primary lg block", "선택한 측정 조건으로 측정을 시작합니다.", block || (nStart ? "" : "측정 대상 없음"));
  const card3 = h("div", { class: "card" }, h("h3", {}, h("span", { class: "n", text: "3" }), "측정"),
    h("div", { class: "row" }, sel_, btn("편집", "cond-edit", null, "sm", "측정 조건을 편집합니다.", s.run && ["running", "paused"].includes(s.run.state) ? "측정 중" : "")),
    h("ol", { class: "steps" }, (cv ? cv.steps : []).map((st) => h("li", {}, h("span", { text: (st.i + 1) + ". " + st.text })))),
    h("div", { class: "row wrap" }, chips),
    h("div", { class: "row" }, ...[["continuous", "연속"], ["confirm", "샘플마다 확인"], ["selected", "선택 샘플만"]].map(([v, l]) => btn(l, "mmode", { m: v }, "sm " + (M.mode_run === v ? "on" : "")))),
    startBtn, h("div", { class: "why", text: block || (s.start_block.notes || []).join(" · ") }));
  r.replaceChildren(card1, card2, card3);
}
function stat(l, v, cls) { return h("div", { class: "stat" }, h("div", { class: "l", text: l }), h("div", { class: "num " + (cls || ""), text: String(v) })); }
function readyChips(s) {
  const st = s.settings, relays = st.relay.items, hn = st.heater.relay;
  const coolers = (st.cooler.zones.cells || []).map((c) => c.relay).filter((n) => relays[n - 1] && relays[n - 1].enabled);
  const c = (t, ok, warn) => h("span", { class: "chip " + (ok ? "ok" : warn ? "warn" : "no"), text: t });
  return [c("온도 센서 " + (s.temp.kind === "none" ? "없음" : "있음"), s.temp.kind !== "none"), c("히트건 R" + hn, relays[hn - 1] && relays[hn - 1].enabled, true),
    c("쿨러 R" + (coolers.length ? Math.min(...coolers) + "–" + Math.max(...coolers) : "—"), coolers.length > 0, true), c("계측기 " + (s.meter.driver === "none" ? "미연결" : "연결"), s.meter.driver !== "none", true)];
}
UI.acts.sel = (a) => send("sample_select", a);
UI.acts.capture = () => send("capture");
UI.acts.goto = (a) => send("goto_sample", a);
UI.acts.exclude = (a) => send("sample_exclude", a);
UI.acts.movepos = (a) => { M.moveNo = M.moveNo === a.no ? null : a.no; M.addMode = false; toast(M.moveNo != null ? "사진에서 새 위치를 누르세요" : "취소"); render(); M.rightSig = ""; render(); };
UI.acts.confirm_s = (a) => send("sample_confirm", a);
UI.acts.delete = (a) => send("sample_delete", a);
UI.acts.addmode = () => { M.addMode = !M.addMode; M.moveNo = null; toast(M.addMode ? "사진에서 칩을 누르세요" : "취소"); M.rightSig = ""; render(); };
UI.acts.here = () => send("sample_here");
UI.acts.undo = () => send("undo_edit");
UI.acts.mmode = (a) => { M.mode_run = a.m; M.rightSig = ""; render(); };
UI.acts["cond-edit"] = () => {
  const s = UI.state, cur = s.conditions.find((c) => c.name === (M.cond || s.active_condition)) || s.conditions[0];
  const orig = (s.settings.condition.items || []).find((c) => c.name === cur.name);
  const ta = h("textarea", { class: "mono", rows: 22, cols: 90, spellcheck: "false" });
  ta.value = JSON.stringify(orig, null, 1);
  openWin(h("div", {}, h("h2", { text: "측정 조건을 편집할까요?" }), h("div", { class: "sub", text: "단계 목록을 고치면 서버가 검증한 뒤 저장합니다." }), ta,
    h("div", { class: "btns" }, h("button", { class: "btn", text: "닫기", onclick: closeWin }),
      h("button", { class: "btn primary", text: "저장", onclick: async () => {
        let obj; try { obj = JSON.parse(ta.value); } catch (e) { toast("형식 오류", true); return; }
        const items = s.settings.condition.items.map((c) => (c.name === cur.name ? obj : c));
        const r = await send("settings_save", { patch: { "condition.items": items } });
        if (r.ok) { closeWin(); toast("저장"); }
      } }))), "big");
};
UI.acts.mstart = async () => {
  const s = UI.state, nos = M.mode_run === "selected" ? [s.sel] : null;
  const go = async (okWeak) => { const r = await send("measure_start", { condition: M.cond || s.active_condition, mode: M.mode_run, nos, ok_weak: okWeak }); if (r.ok) toast("측정 시작"); };
  if (s.n_weak > 0) dialog("확인 필요 샘플이 남았습니다. 그대로 측정할까요?", "확인 필요 " + s.n_weak + "개. 위치를 확인하지 않고 측정하면 공구가 어긋날 수 있습니다.", [{ label: "사진 확인" }, { label: "그대로 측정", cls: "primary", fn: () => go(true) }]);
  else go(false);
};

/* ---- 측정 중 ---- */
function renderRunLeft(s) {
  if (!M.runBuilt) {
    M.graphBox = h("div", { class: "graph" });
    M.minip = h("div", { class: "minip" });
    M.tableBox = h("div", { class: "scroll" });
    M.leftRun.append(M.graphBox, h("div", { class: "row", id: "run-low" }, h("div", { class: "card" }, h("h3", { text: "진행" }), M.minip), h("div", { class: "card grow scroll" }, h("h3", { text: "측정 기록" }), M.tableBox)));
    M.runBuilt = true;
  }
}
function updateRunDynamic(s) {
  const run = s.run;
  const cur = run.cur, curS = s.samples.find((p) => p.no === cur);
  const cv = run;
  M.graphBox.replaceChildren(h("div", { class: "gh" }, h("b", { text: (cur != null ? cur + "번 샘플 · 온도" : "온도") }), h("span", { class: "big mono", text: f1(cv.cur_temp) + " °C" }),
    h("span", { class: "sub", text: cv.text || (cv.waiting || (cv.state === "stopping" ? "중지 중" : "")) })), drawGraph(1170, 330, cv));
  /* 진행 미니 판 */
  const xs = s.samples.map((p) => p.X).filter((v) => v != null), ys = s.samples.map((p) => p.Y).filter((v) => v != null);
  const cols = 5;
  M.minip.style.gridTemplateColumns = "repeat(" + cols + ", 52px)";
  M.minip.replaceChildren(...s.samples.map((p) => h("div", { class: "m " + (p.no === cur ? "cur" : run.results[p.no] ? "done" : run.order.includes(p.no) ? "" : "skip"), text: p.no })));
  const chans = (s.settings.meter.channels || []).map((c) => c.name);
  const rows = Object.values(run.results).sort((a, b) => a.no - b.no);
  M.tableBox.replaceChildren(h("table", { class: "tb" }, h("tr", {}, ["번호", "접촉 Z", "최고 온도", "가열", "냉각", ...chans.map((c) => "측정값 " + c), "결과"].map((t) => h("th", { text: t }))),
    rows.map((r) => h("tr", { class: r.status === "확인 필요" ? "check" : r.status === "실패" || r.status === "오류" ? "fail" : "" },
      [r.no, f1(r.contact_z, 2), f1(r.t_max), f1(r.heat_s, 0), f1(r.cool_s, 0), ...chans.map((c) => (r.values[c] != null ? r.values[c] : "—")), r.status].map((t) => h("td", { text: String(t) }))))));
  const hw = $("#hw-cells");
  if (hw) updateHw(hw, s);
  const pg = $("#run-prog");
  if (pg) pg.replaceChildren(h("div", { text: run.done + " / " + run.n + "  ·  경과 " + mmss(run.elapsed) + "  ·  남은 시간 " + (run.remain == null ? "—" : "약 " + mmss(run.remain)) }), h("div", { class: "bar" }, h("i", { style: null })));
  if (pg) pg.querySelector("i").style.width = (run.n ? (100 * run.done) / run.n : 0) + "%";
  const stl = $("#run-steps");
  if (stl) stl.replaceChildren(...run.steps.map((st) => h("li", { class: st.state }, h("span", { text: (st.state === "done" ? "✓ " : "") + (st.i + 1) + ". " + st.text }), h("span", { class: "val", text: st.value || "" }))));
}
function updateHw(box, s) {
  const run = s.run, rl = s.relays || { owners: {}, heater_on: false };
  const co = Object.keys(rl.owners).filter((n) => rl.owners[n] === "cooler");
  const lastR = run.results[run.cur];
  const c = (k, v, cls) => h("div", { class: "h " + (cls || "") }, h("div", { class: "k", text: k }), h("div", { class: "v", text: v }));
  box.replaceChildren(c("히트건", rl.heater_on ? "켜짐" : "꺼짐", rl.heater_on ? "heat" : "ok"), c("쿨러", co.length ? "켜짐 R" + co.join(",R") : "꺼짐", co.length ? "ok" : ""),
    c("접촉", s.settings.contact.driver === "none" ? "없음" : s.stage && s.stage.z != null ? "Z " + f1(s.stage.z, 2) + " mm" : "—", "ok"), c("온도 센서", s.temp.kind === "none" ? "없음" : "정상", s.temp.kind === "none" ? "warn" : "ok"),
    c("계측기", s.meter.driver === "none" ? "미연결" : "연결", s.meter.driver === "none" ? "warn" : "ok"), c("기록 파일", run.warns.length ? run.warns[0] : (run.dir || "").split(/[\\/]/).pop(), run.warns.length ? "warn" : "ok"));
  void lastR;
}
function renderRunRight(s) {
  const run = s.run, cur = s.samples.find((p) => p.no === run.cur);
  const paused = run.paused || run.state === "paused";
  const card1 = h("div", { class: "card" }, h("h3", { text: "현재 샘플" }), h("div", { class: "row" }, h("span", { class: "num", text: (run.cur != null ? run.cur + "번" : "—") }),
    h("span", { class: "grow", text: cur ? xy(cur.X, cur.Y) : "" }), h("span", { class: "chip", text: cur && cur.cooler ? "쿨러 " + cur.cooler : "쿨러 —" })));
  const card2 = h("div", { class: "card" }, h("h3", { text: "단계 · " + run.cond }), h("ul", { class: "steps", id: "run-steps" }));
  const card3 = h("div", { class: "card" }, h("h3", { text: "장비 상태" }), h("div", { class: "hw", id: "hw-cells" }));
  const card4 = h("div", { class: "card" }, h("h3", { text: "전체 진행" }), h("div", { id: "run-prog" }));
  let ctl;
  if (run.waiting === "다음 샘플 확인") ctl = h("div", { class: "row" }, btn("다음 샘플", "manswer", { v: "next" }, "primary lg"), btn("다시 측정", "manswer", { v: "redo" }, "lg"), btn("중지", "mstop", null, "danger lg"));
  else if (run.waiting) ctl = h("div", { class: "row" }, btn("계속", "manswer", { v: "next" }, "primary lg"), btn("중지", "mstop", null, "danger lg"));
  else if (paused) ctl = h("div", { class: "row" }, btn("다시 시작", "mresume", null, "primary lg"), btn("중지", "mstop", null, "danger lg"));
  else ctl = h("div", { class: "row" }, btn("일시정지", "mpause", null, "lg"), btn("중지", "mstop", null, "danger lg"));
  const jog = paused ? h("div", { class: "sub3", text: "일시정지 중 · 장비 설정에서 조그와 기준점 기록을 쓸 수 있음" }) : null;
  M.right.replaceChildren(...[card1, card2, card3, card4, ctl, jog].filter(Boolean));
}
UI.acts.mpause = () => send("measure_pause");
UI.acts.mresume = () => send("measure_resume");
UI.acts.manswer = (a) => send("measure_answer", { value: a.v });
UI.acts.mstop = () => dialog("측정을 중지할까요?", "지금 단계를 끊고 히트건을 끈 뒤 Z 를 올리고 대기 위치로 돌아갑니다.", [{ label: "계속 측정" }, { label: "중지", cls: "danger", fn: () => send("measure_stop") }]);

UI.renders.push(renderMeasure);
