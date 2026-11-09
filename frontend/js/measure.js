/* measure.js - 측정 탭: 준비(촬영 사진 · 샘플 · 측정 시작)와 측정 중(판 맞춤 촬영 사진에 샘플 상태 색 · 현재 샘플 · 단계 목록).
   화면 원칙: 카드마다 주 동작 하나(보조는 ⋯) · 문제 있을 때만 상태 표시 · 모드 버튼 대신 사진을 직접 다룬다(끌어서 위치 수정 · 더블클릭으로 추가).
   온도 · 계측기는 이 프로그램에 없다(별도 프로그램). 남은 것은 이동 · 접촉 높이 · 쿨러 · 히트건 · 대기 · 확인 대기뿐. */
"use strict";
const M = { viewer: null, mode: "live", zoom: "plate", rightSig: "", prepBuilt: false, cond: null, showSteps: false, dragNo: null, sub: "here", subSig: "" };
const SCOL = { done: "#2b9a58", cur: "#2f6fdb", wait: "#8a929b", err: "#d03a3a", stop: "#e08a1e", skip: "#b9bdc2" };

/* 카메라가 없을 때 사진 자리 안내 */
function camEmpty(s) {
  const retry = s.camera && s.camera.state === "retry";
  return { text: retry ? "카메라 연결 중" : "카메라 연결 안 됨", btns: [{ label: retry ? "연결 중…" : "다시 연결", act: "cam_reconnect", why: retry ? "연결 중 · 잠시만" : "" }] };
}
function captureWhy(s) {
  const dv = s.dev || {};
  if (s.camera.busy) return s.camera.busy + " 중";
  if (!s.camera.open) return "카메라 연결 안 됨";
  if (!s.calib.area) return "판 영역 필요 · 장비 설정 3";
  return s.ref ? "" : "빈 판 사진 필요 · 장비 설정 4";
}
function isRunning(s) { return !!(s.run && ["running", "paused", "stopping"].includes(s.run.state)); }
function sampleState(s, p) {
  const run = s.run;
  if (!run || !isRunning(s)) return null;
  const r = run.results[p.no];
  if (p.no === run.cur && !r) return "cur";
  if (r) return r.status === "완료" ? "done" : r.status === "오류" ? "err" : r.status === "중지됨" ? "stop" : r.status === "측정 제외" ? "skip" : "done";
  return run.order.includes(p.no) ? "wait" : "skip";
}

/* ---- 준비 화면의 왼쪽(사진 보기) ---- */
function buildPrep() {
  const pane = $("#tab-measure");
  M.viewer = new Viewer();
  M.head = h("div", { class: "vhead" });
  M.news = h("div", { class: "news" });
  M.mini = sv("svg", { class: "minimap", width: 190, height: 190, viewBox: "0 0 190 190" });
  M.mini.classList.add("hidden");
  M.viewer.el.append(M.mini);
  M.viewer.onClick = onViewClick;
  M.viewer.onDblClick = (u, v) => { if (M.mode === "shot" && UI.state.shot && !isRunning(UI.state)) send("sample_add", { u, v }); };
  M.viewer.onHandle = () => redrawOverlay();
  M.viewer.onHandleEnd = () => { const hnd = M.viewer.handles && M.viewer.handles[0]; if (hnd && M.dragNo != null) send("sample_move", { no: M.dragNo, u: hnd[0], v: hnd[1] }); };
  M.viewer.onApply = redrawOverlay;
  M.leftPrep = h("div", { class: "viewbox", id: "m-view" }, M.head, M.viewer.el, M.news);
  buildHere();                                                            // R–T 그래프 둘(rt.js) - 카메라 아래
  M.cardPos = h("div", { class: "card", id: "c-pos" }); M.cardSam = h("div", { class: "card", id: "c-samples" }); M.cardRun = h("div", { class: "card", id: "c-run" });
  M.right = h("div", { class: "mright" }, h("div", { class: "mtop" }, M.cardPos, M.cardSam), M.cardRun);
  pane.replaceChildren(h("div", { class: "mleft" }, M.leftPrep, T.graphs), M.right);
  UI.logRenders = UI.logRenders || [];
  UI.logRenders.push(renderNews);
  setInterval(refreshLive, 250);
  document.addEventListener("keydown", (e) => {
    if (UI.tab === "measure" && e.ctrlKey && e.key.toLowerCase() === "z" && !/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) { e.preventDefault(); if (UI.state && UI.state.can_undo) send("undo_edit"); }
  });
  M.prepBuilt = true;
}
function renderNews() {
  if (!M.news) return;
  const l = (UI.logTail || []).slice(-1)[0];
  M.news.replaceChildren(h("span", { class: "t" + (l ? " " + l.lv : ""), text: l ? l.t + "  " + l.msg : "소식 없음" }), h("button", { class: "btn sm", "data-act": "log-all", text: "기록 보기" }));
}
/* 기록 보기: [기록] [시리얼] 탭. 시리얼 = 보낸 줄 · 받은 줄 원문(실장비 첫 시험에서 확인용) */
UI.acts["log-all"] = () => {
  let tab = "log", timer = null;
  const box = h("div", { class: "loglines mono scroll logbox" });
  const draw = async () => {
    if (tab === "log") box.replaceChildren(...UI.log.slice(-300).map((l) => h("div", { class: l.lv, text: l.t + "  " + l.msg })));
    else {
      try {
        const j = await (await fetch("/serial.json?n=300")).json();
        box.replaceChildren(...j.rows.map((r) => h("div", { class: r.d === "->" ? "tx" : "rx", text: r.t + "  " + r.d + "  " + r.x })));
        box.scrollTop = box.scrollHeight;
      } catch (e) { box.replaceChildren(h("div", { text: "시리얼 기록을 못 읽음" })); }
    }
  };
  const tb = h("div", { class: "seg" });
  const mkTabs = () => tb.replaceChildren(...[["log", "기록"], ["serial", "시리얼"]].map(([k, l]) => h("button", { class: "btn sm" + (tab === k ? " on" : ""), text: l, onclick: () => { tab = k; mkTabs(); draw(); } })));
  mkTabs(); draw();
  timer = setInterval(() => { if (tab === "serial") draw(); }, 1000);
  openWin(h("div", {}, h("h2", { text: "기록 보기" }), tb, box, h("div", { class: "btns" }, h("button", { class: "btn", text: "닫기", onclick: () => { clearInterval(timer); closeWin(); } }))), "big");
};

function refreshLive() {
  const s = UI.state;
  if (!s || UI.tab !== "measure" || M.mode !== "live" || !M.viewer || isRunning(s) || !s.camera.open) return;
  const sz = s.camera.size || [1280, 960];
  M.viewer.set("/preview.jpg", sz[0], sz[1], Date.now());
}

/* ---- 왼쪽 ---- */
function renderLeft(s, running) {
  const sh = s.shot;
  if (running) {
    M.mode = "shot";
    M.head.replaceChildren(h("b", { text: "측정 중 · 판 맞춤 촬영 사진" }), h("span", { class: "sub", text: sh ? "촬영 " + sh.time : "" }), h("span", { class: "grow" }),
      h("span", { class: "legend" }, ...[["done", "완료"], ["cur", "진행"], ["wait", "대기"], ["err", "오류"]].map(([k, l]) => h("span", {}, h("i", { class: "lg-" + k }), l))));
    M.zoom = "plate";
  } else {
    const seg = h("div", { class: "seg" }, btn("실시간", "m-mode", { m: "live" }, M.mode === "live" ? "on" : ""), btn("촬영 사진", "m-mode", { m: "shot" }, M.mode === "shot" ? "on" : ""));
    const seg2 = h("div", { class: "seg" }, btn("판 맞춤", "m-fit", { m: "plate" }, M.zoom === "plate" ? "on" : ""), btn("전체", "m-fit", { m: "all" }, M.zoom === "all" ? "on" : ""));
    M.head.replaceChildren(seg, h("span", { class: "sub", text: M.mode === "shot" ? (sh ? "촬영 " + sh.time : "촬영 없음") : "" }), h("span", { class: "grow" }), seg2);
  }
  M.viewer.mode = M.zoom;
  if (M.mode === "shot") {
    if (sh) { M.viewer.area = sh.area || s.calib.area; M.viewer.set("/shot.jpg", sh.size[0], sh.size[1], sh.id); }
    else { M.viewer.setEmpty({ text: "촬영 사진 없음", btns: [{ label: "촬영", act: "capture", why: captureWhy(s) }] }); M.viewer.set(null); }
  } else if (!s.camera.open) {
    M.viewer.setEmpty(camEmpty(s)); M.viewer.set(null);
  } else {
    const sz = s.camera.size || [1280, 960];
    M.viewer.area = s.calib.area;
    M.viewer.setEmpty({ text: "실시간 화면 준비 중" });
    if (!M.viewer.W) M.viewer.set("/preview.jpg", sz[0], sz[1], Date.now());
  }
  /* 선택한 샘플은 끌어서 위치 수정(측정 중에는 못 끈다) */
  const sp = s.samples.find((p) => p.no === s.sel);
  if (!running && M.mode === "shot" && sp && !s.camera.busy && M.viewer.hd < 0) { M.viewer.handles = [[sp.u, sp.v]]; M.dragNo = sp.no; } else if (M.viewer.hd < 0) { M.viewer.handles = null; M.dragNo = null; }
  M.viewer.message(s.camera.cap_text && s.camera.busy ? s.camera.busy + " · " + s.camera.cap_text : "");
  redrawOverlay();
  renderNews();
  $$(".cap-err", M.viewer.el).forEach((e) => e.remove());
  const ce = s.camera.cap_error;
  if (ce && Date.now() / 1000 - ce.id < 8) M.viewer.el.append(h("div", { class: "cap-err", text: "촬영 실패 · " + ce.text }));
}
UI.acts["m-mode"] = (a) => { M.mode = a.m; M.viewer.W = 0; M.viewer.key = null; if (UI.ws) UI.ws.send(JSON.stringify({ cmd: "preview", on: a.m === "live" })); render(); };
UI.acts["m-fit"] = (a) => { M.zoom = a.m; M.viewer.fit(a.m, M.viewer.area); render(); };

function onViewClick(u, v) {
  const s = UI.state;
  if (isRunning(s)) return;
  let best = null, bd = 1e9;
  s.samples.forEach((p) => { const d = Math.hypot(p.u - u, p.v - v); if (d < bd) { bd = d; best = p; } });
  if (best && bd < 60 / M.viewer.k + 25) { send("sample_select", { no: best.no }); return; }
  const mm = pxToMm(s, u, v);                                              // 빈 곳을 한 번 누르면 그 자리 좌표를 X · Y 칸에 채운다
  if (mm) { MC.mv.x = f1(mm[0], 1); MC.mv.y = f1(mm[1], 1); MC.mvPx = [u, v]; MC.sigs.pos = ""; render(); redrawOverlay(); }
}

function sampleBBox(p) {
  if (p.verts && p.verts.length >= 3) {
    const xs = p.verts.map((q) => q[0]), ys = p.verts.map((q) => q[1]);
    return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
  }
  return [p.u - 12, p.v - 12, p.u + 12, p.v + 12];
}
/* 쿨러 구역 경계: 판 사각형(사진 프레임) 위의 투영 변환으로 mm 격자를 사진에 그린다(장비 설정 8 에서만 쓴다) */
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
function drawZones(v, s, area, lw) {
  const size = s.calib.size_mm || s.settings.calib.area_size_mm;
  const z = s.settings.cooler.zones, f = projective([[0, 0], [size[0], 0], [size[0], size[1]], [0, size[1]]], area);
  const re = z.row_edges || Array.from({ length: z.rows + 1 }, (_, i) => (size[1] * i) / z.rows), ce = z.col_edges || Array.from({ length: z.cols + 1 }, (_, i) => (size[0] * i) / z.cols);
  const gl = [];
  re.forEach((y) => gl.push([f(ce[0], y), f(ce[ce.length - 1], y)]));
  ce.forEach((x) => gl.push([f(x, re[0]), f(x, re[re.length - 1])]));
  gl.forEach((l) => v.svg.append(sv("line", { x1: l[0][0], y1: l[0][1], x2: l[1][0], y2: l[1][1], stroke: "#ffe08a", "stroke-width": lw(1.5), "stroke-dasharray": lw(4) + " " + lw(4) })));
  z.cells.forEach((c, i) => { const r = Math.floor(i / z.cols), cc = i % z.cols, p = f(ce[cc] + 2, re[r] + 5);
    v.svg.append(sv("text", { x: p[0], y: p[1], "text-anchor": "start", "font-size": lw(15), fill: "#ffe08a" }, "R" + (c.relay || "?"))); });
}

function redrawOverlay() {
  const s = UI.state, v = M.viewer;
  if (!s || !v || !v.W) { if (v) v.clear(); return; }
  v.clear();
  if (M.mode !== "shot" || !s.shot) {
    if (s.calib.area && M.mode === "live") v.svg.append(sv("polygon", { points: s.calib.area.map((p) => p.join(",")).join(" "), fill: "none", stroke: "#2f6fdb", "stroke-width": v.px(2), "stroke-dasharray": v.px(8) + " " + v.px(5) }));
    drawStageMarks(v, s);
    return;
  }
  const running = isRunning(s);
  const k = v.k, lw = (n) => n / k;
  const area = s.shot.area || s.calib.area;
  if (area) v.svg.append(sv("polygon", { points: area.map((p) => p.join(",")).join(" "), fill: "none", stroke: "#8fb4f2", "stroke-width": lw(1.5), "stroke-dasharray": lw(8) + " " + lw(5) }));
  const items = s.samples.map((p) => ({ bbox: sampleBBox(p), p }));
  items.forEach(({ p }) => {
    const rs = running ? sampleState(s, p) : null;
    const sel = !running && p.no === s.sel, col = rs ? SCOL[rs] : sel ? "#2f6fdb" : p.excluded ? "#9aa1a8" : p.weak ? "#e08a1e" : "#f2f4f6";
    const sw = lw(rs ? (rs === "cur" ? 5 : 3.5) : sel ? 4 : 1.6);
    const fill = rs && rs !== "skip" ? col : "none";
    if (p.verts && p.verts.length >= 3) v.svg.append(sv("polygon", { points: p.verts.map((q) => q.join(",")).join(" "), fill, "fill-opacity": rs ? 0.28 : null, stroke: col, "stroke-width": sw, "stroke-dasharray": p.excluded ? lw(5) + " " + lw(4) : null }));
    else v.svg.append(sv("circle", { cx: p.u, cy: p.v, r: lw(12), fill, "fill-opacity": rs ? 0.28 : null, stroke: col, "stroke-width": sw }));
    if (sel) drawPin(v, s, p, lw);                                   // 핀은 선택한 샘플에만
  });
  const bw = lw(30), bh = lw(20);
  const pl = placeBadges(items, v.W, v.H, bw, bh);                  // 번호는 항상 보인다
  items.forEach(({ p }, i) => {
    const { box, from } = pl[i], rs = running ? sampleState(s, p) : null, sel = !running && p.no === s.sel;
    const col = rs ? SCOL[rs] : sel ? "#2f6fdb" : p.weak ? "#e08a1e" : "#2b3138";
    const cx = (box[0] + box[2]) / 2, cy = (box[1] + box[3]) / 2;
    v.svg.append(sv("line", { x1: from[0], y1: from[1], x2: cx, y2: cy, stroke: col, "stroke-width": lw(1) }));
    v.svg.append(sv("rect", { x: box[0], y: box[1], width: bw, height: bh, rx: lw(3), fill: col, opacity: 0.94 }));
    v.svg.append(sv("text", { x: cx, y: cy + lw(5), "text-anchor": "middle", "font-size": lw(14), fill: "#fff", "font-weight": 700 }, String(p.no)));
  });
  const hd = v.handles && v.handles[0];
  if (hd) v.svg.append(sv("circle", { cx: hd[0], cy: hd[1], r: lw(6), fill: "#2f6fdb", stroke: "#fff", "stroke-width": lw(2) }));
  drawStageMarks(v, s);
  drawMinimap(s);
}
/* 좌표 보정이 있으면 지금 핀 위치 십자(빨강)와 클릭으로 정한 이동 목표(파랑 원)를 사진 위에 그린다 */
function drawStageMarks(v, s) {
  const lw = (n) => n / v.k, st = s.stage;
  if (st && st.x != null && s.calib.area_mm) {
    const q = mmToPx(s, st.x, st.y);
    if (q && isFinite(q[0])) v.svg.append(sv("path", { d: "M" + (q[0] - lw(14)) + " " + q[1] + "H" + (q[0] + lw(14)) + "M" + q[0] + " " + (q[1] - lw(14)) + "V" + (q[1] + lw(14)), stroke: "#ff3b30", "stroke-width": lw(3) }));
  }
  if (MC.mvPx) v.svg.append(sv("circle", { cx: MC.mvPx[0], cy: MC.mvPx[1], r: lw(9), fill: "none", stroke: "#2f6fdb", "stroke-width": lw(3) }));
}
function drawPin(v, s, p, lw) {
  const tool = s.tool, jac = pinJac(s);
  if (!jac) return;
  const w = Number(tool.tip_w_mm || 10), hh = Number(tool.tip_h_mm || 2), ang = (tool.tip_angle_deg * Math.PI) / 180;     // 스테이지 기준 각도(설정 하나)
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
  const q = c.area, m = c.area_mm;
  const dx = [(q[1][0] - q[0][0] + q[2][0] - q[3][0]) / 2, (q[1][1] - q[0][1] + q[2][1] - q[3][1]) / 2];
  const dy = [(q[3][0] - q[0][0] + q[2][0] - q[1][0]) / 2, (q[3][1] - q[0][1] + q[2][1] - q[1][1]) / 2];
  const mx = [(m[1][0] - m[0][0] + m[2][0] - m[3][0]) / 2, (m[1][1] - m[0][1] + m[2][1] - m[3][1]) / 2];
  const my = [(m[3][0] - m[0][0] + m[2][0] - m[1][0]) / 2, (m[3][1] - m[0][1] + m[2][1] - m[1][1]) / 2];
  const det = mx[0] * my[1] - my[0] * mx[1];
  if (Math.abs(det) < 1e-9) return null;
  const inv = [[my[1] / det, -my[0] / det], [-mx[1] / det, mx[0] / det]];
  const P = [[dx[0], dy[0]], [dx[1], dy[1]]];
  const J = [[P[0][0] * inv[0][0] + P[0][1] * inv[1][0], P[0][0] * inv[0][1] + P[0][1] * inv[1][1]], [P[1][0] * inv[0][0] + P[1][1] * inv[1][0], P[1][0] * inv[0][1] + P[1][1] * inv[1][1]]];
  return { x: [J[0][0], J[1][0]], y: [J[0][1], J[1][1]] };
}
function drawMinimap(s) {
  const m = M.mini; m.replaceChildren();
  const am = s.calib.area_mm;
  m.classList.toggle("hidden", !am || !s.stage);
  if (!am || !s.stage) return;
  const xs = am.map((p) => p[0]), ys = am.map((p) => p[1]);
  const x0 = Math.min(...xs) - 10, x1 = Math.max(...xs) + 10, y0 = Math.min(...ys) - 10, y1 = Math.max(...ys) + 10;
  const sc = Math.min(160 / (x1 - x0), 150 / (y1 - y0));
  const P = (x, y) => [15 + (y - y0) * sc, 20 + (x - x0) * sc];
  m.append(sv("polygon", { points: am.map((p) => P(p[0], p[1]).join(",")).join(" "), fill: "#fff", stroke: "#8fb4f2", "stroke-dasharray": "4 3" }));
  s.samples.forEach((p) => { if (p.X == null) return; const q = P(p.X, p.Y), rs = sampleState(s, p); m.append(sv("circle", { cx: q[0], cy: q[1], r: p.no === s.sel || rs === "cur" ? 5 : 3, fill: rs ? SCOL[rs] : p.no === s.sel ? "#2f6fdb" : p.weak ? "#e08a1e" : "#5a626b" })); });
  if (s.stage.x != null) { const q = P(s.stage.x, s.stage.y); m.append(sv("path", { d: "M" + (q[0] - 6) + " " + q[1] + "H" + (q[0] + 6) + "M" + q[0] + " " + (q[1] - 6) + "V" + (q[1] + 6), stroke: "#d03a3a", "stroke-width": 2 })); }
  m.append(sv("text", { x: 176, y: 14, "text-anchor": "end", "font-size": 11, fill: "#5a626b" }, "스테이지 방향 · 가로 Y · 세로 X"));
}

/* ---- 준비: 오른쪽 ---- */
function estText(c) {
  const sec = c.est_s || 0;
  return c.steps.length + "단계 · 샘플당 약 " + (sec >= 90 ? Math.round(sec / 60) + "분" : sec + "초");
}
UI.acts.sel = (a) => send("sample_select", a);
UI.acts.capture = () => send("capture");
UI.acts.goto = (a) => send("goto_sample", a);
UI.acts.exclude = (a) => send("sample_exclude", a);
UI.acts.confirm_s = (a) => send("sample_confirm", a);
UI.acts.delete = (a) => send("sample_delete", a);
UI.acts["steps-toggle"] = () => { MC.showSteps = !MC.showSteps; MC.sigs.run = ""; render(); };

/* 측정 조건 편집: 단계 목록 + 위/아래/삭제 + 단계 추가 + 값 칸(대기 · 히트건 시간, 쿨러 대상) - 서버가 검증한 뒤 저장 */
const STEP_NAMES = { contact: "접촉 높이로 내리기", cooler: "쿨러", heat: "히트건", wait: "대기", confirm: "확인 대기", z_top: "Z 맨 위" };
UI.acts["cond-edit"] = () => {
  const s = UI.state, cur = s.conditions.find((c) => c.name === (M.cond || s.active_condition)) || s.conditions[0];
  const orig = (s.settings.condition.items || []).find((c) => c.name === cur.name);
  const work = JSON.parse(JSON.stringify(orig));
  const box = h("div", { class: "stepedit" });
  const maxHeat = s.settings.heater.max_on_s;
  const draw = () => {
    box.replaceChildren(...work.steps.map((st, i) => {
      const num = (key, lab, mx) => h("span", { class: "row" }, h("input", { type: "number", value: st[key], step: "any", min: 0, max: mx, onchange: (e) => { st[key] = Number(e.target.value); } }), h("span", { class: "sub", text: lab }));
      let body = null;
      if (st.type === "wait") body = num("s", "s");
      else if (st.type === "heat") body = num("s", "s · 상한 " + maxHeat + " s", maxHeat);
      else if (st.type === "cooler") body = h("span", { class: "row" },
        h("select", { onchange: (e) => { st.action = e.target.value; } }, [["on", "켜기"], ["off", "끄기"]].map(([v, l]) => h("option", { value: v, text: l, selected: st.action === v }))),
        h("select", { onchange: (e) => { st.target = e.target.value; } }, [["sample", "샘플 구역"], ["all", "전부"]].map(([v, l]) => h("option", { value: v, text: l, selected: st.target === v }))));
      const fixed = st.type === "move_sample";
      return h("div", { class: "row se" }, h("b", { class: "n", text: i + 1 }), h("span", { class: "nm", text: fixed ? "샘플 위로 이동" : STEP_NAMES[st.type] || st.type }), body, h("span", { class: "grow" }),
        fixed ? null : h("button", { class: "btn sm", text: "↑", onclick: () => { if (i > 1) { [work.steps[i - 1], work.steps[i]] = [work.steps[i], work.steps[i - 1]]; draw(); } } }),
        fixed ? null : h("button", { class: "btn sm", text: "↓", onclick: () => { if (i < work.steps.length - 1) { [work.steps[i + 1], work.steps[i]] = [work.steps[i], work.steps[i + 1]]; draw(); } } }),
        fixed ? null : h("button", { class: "btn sm", text: "삭제", onclick: () => { work.steps.splice(i, 1); draw(); } }));
    }));
  };
  draw();
  const add = h("button", { class: "btn sm", text: "단계 추가 ▾" });
  const defs = { contact: { type: "contact" }, cooler: { type: "cooler", action: "on", target: "sample" }, heat: { type: "heat", s: 20 }, wait: { type: "wait", s: 5 }, confirm: { type: "confirm" }, z_top: { type: "z_top" } };
  add.addEventListener("click", (e) => { e.stopPropagation(); popMenu(add, Object.keys(defs).map((k) => ({ label: STEP_NAMES[k], fn: () => { work.steps.push(JSON.parse(JSON.stringify(defs[k]))); draw(); } }))); });
  openWin(h("div", {}, h("h2", { text: "'" + cur.name + "' 순회 단계를 고칠까요?" }), h("div", { class: "sub", text: "첫 단계(샘플 위로 이동)는 고정입니다. 저장하면 서버가 검증합니다." }), box, h("div", { class: "row" }, add),
    h("div", { class: "btns" }, h("button", { class: "btn", text: "닫기", onclick: closeWin }),
      h("button", { class: "btn primary", text: "저장", onclick: async () => {
        const items = s.settings.condition.items.map((c) => (c.name === cur.name ? work : c));
        const r = await send("settings_save", { patch: { "condition.items": items } });
        if (r.ok) { closeWin(); toast("저장"); }
      } }))), "big");
};
UI.acts.mstart = async () => {
  const s = UI.state;
  const go = async (okWeak) => { const r = await send("measure_start", { condition: M.cond || s.active_condition, ok_weak: okWeak }); if (r.ok) toast("측정 시작"); };
  if (s.n_weak > 0) dialog("확인 필요 샘플이 남았습니다. 그대로 측정할까요?", "확인 필요 " + s.n_weak + "개. 위치를 확인하지 않고 측정하면 공구가 어긋날 수 있습니다.", [{ label: "사진 확인" }, { label: "그대로 측정", cls: "primary", fn: () => go(true) }]);
  else go(false);
};

UI.acts.mpause = () => send("measure_pause");
UI.acts.mresume = () => send("measure_resume");
UI.acts.manswer = (a) => send("measure_answer", { value: a.v });
UI.acts.mstop = () => dialog("측정을 중지할까요?", "지금 단계를 끊고 히트건을 끈 뒤 Z 를 올리고 대기 위치로 돌아갑니다.", [{ label: "계속 측정" }, { label: "중지", cls: "danger", fn: () => send("measure_stop") }]);

