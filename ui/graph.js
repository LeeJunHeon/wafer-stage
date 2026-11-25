/* graph.js - 온도별 저항 그래프(자동 측정 탭 · 측정 결과 탭이 같이 쓴다).
   가로 = 온도(°C), 세로 = 저항(로그 눈금). 가열 = 실선, 냉각 = 점선, 지금 점 = 동그라미.
   세로 눈금(보이는 곡선 전체의 최대 ÷ 최소): 10배 이상 = 로그 · 10배마다 / 2 ~ 10배 = 로그 · 1 · 2 · 5 × 10^k(눈금 3개 미만이면 1 · 1.5 · 2 · 3 · 5 · 7 × 10^k)로 3~6개 /
   2배 미만 = 선형 · 간격 1 · 2 · 5 × 10^k 로 눈금 4~6개 · 데이터가 높이의 60 % 이상(기록 모드의 흔들림이 보이게 · 폭 0 이면 ±5 %). 범위는 눈금에 맞춤.
   눈금 글자 두 줄(위 "1E+6" · 아래 "1 MΩ" 작고 옅게) · 겹치면 건너뜀(두 줄 높이). 축 제목 "저항 (Ω)"(왼쪽 바깥 세로) · "온도 (°C)" / "경과 (s)"(아래)는 점이 없어도 늘 그린다.
   이름표는 축 밖 오른쪽 띠에 둔다: "그 샘플 색의 짧은 선(12px) + 검은 글자". 지시선(회색 1px)은 그 곡선의 온도가 가장 높은 점에서
   오른쪽으로만 나가 띠에서 이름표로 꺾인다(그래프 안에서 다른 곡선을 가로지르지 않는다). 이름표끼리는 14px 이상 벌린다.
   마우스를 올리면 그 온도의 값을 그래프 위쪽 바깥 한 줄에 보여 준다(선 위에 말풍선을 띄우지 않는다).
   그림은 속성만으로 그린다(바깥 CSS 에 기대지 않는다) - 그대로 PNG 로 저장할 수 있다. */
"use strict";

const G_GRID = "#E4E7EB", G_AXIS = "#9AA3AD", G_TEXT = "#5B636D", G_INK = "#1A1D21", G_FONT = "Pretendard, 'Malgun Gothic', sans-serif";
const G_COLORS = ["#1F2329", "#2A78D6", "#C03A7D"];              // 고른 샘플 · 겹쳐 보는 샘플 둘
const G_M = { l: 58, r: 68, t: 12, b: 34 };                    // l 은 최소값 - 실제 왼쪽 여백은 세로 눈금 글자 폭에 맞춘다(scales) · t 는 맨 위 눈금 글자 윗줄이 들어갈 만큼
const G_YT_W = 18, G_YL_GAP = 10, G_YL_H = 24;                   // 세로 축 제목 띠 · 눈금 글자와 축 사이 · 눈금 글자 두 줄 높이(이보다 가까운 눈금 글자는 건너뜀)
const G_TITLE_H = 20;                                            // PNG 로 저장할 때 위쪽 바깥에 넣는 제목 한 줄의 높이
const G_TAG_GAP = 14, G_KEY_W = 12;                              // 이름표 사이 · 이름표의 색 선 길이

/* 세로 눈금 m × 10^k Ω(m = 1 ~ 9.x) 의 두 줄 글자: ["1.5E+6", "1.5 MΩ"] · sig = 유효 숫자(선형 눈금은 간격에 맞춰 늘린다) */
function ohmTick(m, k, sig) {
  const ms = String(Number(m.toPrecision(sig || 3)));
  const ex = ms + "E" + (k < 0 ? "-" : "+") + Math.abs(k);
  const g = Math.floor(k / 3), unit = { "-1": "mΩ", 0: "Ω", 1: "kΩ", 2: "MΩ", 3: "GΩ", 4: "TΩ" }[g];
  return [ex, unit ? String(Number((m * Math.pow(10, k - 3 * g)).toPrecision(sig || 3))) + " " + unit : ex + " Ω"];
}
/* 선형 눈금(최대 ÷ 최소 < 2): 간격 1 · 2 · 5 × 10^k 중 눈금 4~6개 · 데이터가 범위의 60 % 이상인 가장 촘촘한 것(없으면 4~7개 중 데이터 비율이 가장 큰 것) */
function linTicks(r0, r1) {
  if (!(r1 > r0)) { r0 *= 0.95; r1 *= 1.05; }                    // 값이 전부 같다: 위아래 ±5 %
  const span = r1 - r0;
  let best = null, alt = null;
  for (let k = Math.floor(Math.log10(span / 20)) - 1; k <= Math.ceil(Math.log10(span)) + 1 && !best; k++) for (const m of [1, 2, 5]) {
    const st = m * Math.pow(10, k), lo = Math.floor(r0 / st + 1e-9) * st, hi = Math.ceil(r1 / st - 1e-9) * st;
    const n = Math.round((hi - lo) / st) + 1, frac = span / (hi - lo);
    if (n >= 4 && n <= 6 && frac >= 0.6) { best = { st, lo, hi, n }; break; }
    if (n >= 4 && n <= 7 && (!alt || frac > alt.frac)) alt = { st, lo, hi, n, frac };
  }
  const p = best || alt;
  const ticks = [];
  for (let i = 0; i < p.n; i++) {
    const v = p.lo + i * p.st;
    if (v <= 0) continue;
    const k = Math.floor(Math.log10(v) + 1e-9);
    ticks.push({ v, m: v / Math.pow(10, k), k, sig: Math.min(6, Math.max(2, k - Math.floor(Math.log10(p.st) + 1e-9) + 1)) });
  }
  return { ticks, lo: Math.max(0, p.lo), hi: p.hi, lin: true };
}
/* 세로 눈금 [{v, m, k}] 와 범위 [lo, hi](Ω): 폭 10배 이상 = 10배마다 · 좁으면 1 · 2 · 5 → (3개 미만) 1 · 1.5 · 2 · 3 · 5 · 7 × 10^k(그래도 모자라면 다음 눈금까지 넓힘) */
function ohmTicks(r0, r1) {
  if (r1 / r0 < 2) return linTicks(r0, r1);
  if (r1 / r0 >= 10 - 1e-9) {
    const e0 = Math.floor(Math.log10(r0) + 1e-9), e1 = Math.max(e0 + 1, Math.ceil(Math.log10(r1) - 1e-9)), ticks = [];
    for (let e = e0; e <= e1; e++) ticks.push({ v: Math.pow(10, e), m: 1, k: e });
    return { ticks, lo: ticks[0].v, hi: ticks[ticks.length - 1].v };
  }
  const k0 = Math.floor(Math.log10(r0)) - 1, k1 = Math.ceil(Math.log10(r1)) + 1;
  let best = null;
  for (const ms of [[1, 2, 5], [1, 1.5, 2, 3, 5, 7]]) {
    const cand = [];
    for (let k = k0; k <= k1; k++) for (const m of ms) cand.push({ v: m * Math.pow(10, k), m, k });
    const tol = 1e-9;
    let a = 0, b = cand.length - 1;
    while (a + 1 < cand.length && cand[a + 1].v <= r0 * (1 + tol)) a++;       // 데이터 아래 가장 가까운 눈금
    while (b - 1 >= 0 && cand[b - 1].v >= r1 * (1 - tol)) b--;                // 데이터 위 가장 가까운 눈금
    best = { cand, a, b };
    if (b - a + 1 >= 3) break;
  }
  let { cand, a, b } = best;
  for (let up = true; b - a + 1 < 3; up = !up) { if (up && b + 1 < cand.length) b++; else if (a > 0) a--; else b++; }    // 아주 좁으면 위 · 아래로 한 눈금씩
  const ticks = cand.slice(a, b + 1);
  return { ticks, lo: ticks[0].v, hi: ticks[ticks.length - 1].v };
}
function niceStep(raw) {                                         // 1 · 2 · 5 × 10^k 중 raw 이상에서 가장 작은 것(경과 눈금)
  const p = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1e-9))));
  for (const m of [1, 2, 5, 10]) if (m * p >= raw - 1e-12) return m * p;
  return 10 * p;
}
function textW(s) { let w = 0; for (const ch of String(s)) w += ch.charCodeAt(0) > 0x2E7F ? 11 : 6.5; return w; }   // 11px 글자의 대략 폭(한글은 넓다)

/* series = [{name, points[[온도, 저항, 단계 0 가열 · 1 냉각]], color?, live?, main?}] (3개까지).
   main(굵은 선 · 굵은 이름)을 안 주면 첫 번째가 main 이다.
   g.set(series, {x: "time"}) = 기록 모드(히트건 사용 끔): points 의 첫 값이 경과(s) · 가로축 "경과 (s)"(0 부터) · 마우스 값도 초. 축 이름 · 이름표는 그대로 축 밖 띠. */
function rtGraph() {
  const svg = svgEl("svg", { xmlns: "http://www.w3.org/2000/svg" });
  const read = h("div", { class: "g-read" });
  const box = h("div", { class: "g-box" }, svg);
  const el = h("div", { class: "graph" }, read, box);
  const g = { el, svg, series: [], w: 0, h: 0, hoverT: null, title: "", xTime: false };
  let sc = null;                                                 // 지금 눈금(가로 · 세로 변환)

  function scales() {
    let t0 = Infinity, t1 = -Infinity, r0 = Infinity, r1 = -Infinity;
    for (const s of g.series) for (const p of s.points) {
      if (p[0] == null || !(p[1] > 0)) continue;
      if (p[0] < t0) t0 = p[0]; if (p[0] > t1) t1 = p[0];
      if (p[1] < r0) r0 = p[1]; if (p[1] > r1) r1 = p[1];
    }
    if (t0 === Infinity) { t0 = g.xTime ? 0 : 20; t1 = g.xTime ? 60 : 100; r0 = 10; r1 = 1e6; }
    if (g.xTime) { t0 = 0; t1 = Math.max(t1, 1); const st = niceStep(t1 / 6); t1 = Math.ceil(t1 / st - 1e-9) * st; }      // 경과: 0 부터 · 눈금 간격에 맞춰 끝
    else { t0 = Math.floor(Math.min(t0, 20) / 10) * 10; t1 = Math.ceil(Math.max(t1, 90) / 10) * 10; }
    const yt = ohmTicks(r0, r1), L0 = yt.lin ? yt.lo : Math.log10(yt.lo), L1 = yt.lin ? yt.hi : Math.log10(yt.hi);
    const labels = yt.ticks.map((t) => ohmTick(t.m, t.k, t.sig));
    const lw = Math.max(0, ...labels.map(([a, b]) => Math.max(textW(a), textW(b) * 10 / 11)));       // 눈금 글자 폭(윗줄 11px · 아랫줄 10px)
    const l = Math.max(G_M.l, Math.ceil(G_YT_W + lw + G_YL_GAP + 2));                                // 왼쪽 여백 = 축 제목 띠 + 눈금 글자 + 사이
    const top = G_M.t + (g.title ? G_TITLE_H : 0);
    const right = Math.min(180, Math.max(G_M.r, Math.ceil(10 + G_KEY_W + 4 + Math.max(0, ...g.series.map((s) => textW(s.name))) + 6)));   // 이름이 길면 띠를 넓힌다
    const pw = Math.max(10, g.w - l - right), ph = Math.max(10, g.h - top - G_M.b);
    return { t0, t1, l, pw, ph, top, yticks: yt.ticks, ylabels: labels,
      x: (t) => l + (t - t0) / (t1 - t0) * pw,
      y: (r) => top + (L1 - (yt.lin ? r : Math.log10(r))) / (L1 - L0) * ph,      // 선형(좁은 범위) · 로그
      tAt: (px) => t0 + (px - l) / pw * (t1 - t0) };
  }

  function path(pts) { return pts.map((p, i) => (i ? "L" : "M") + sc.x(p[0]).toFixed(1) + " " + sc.y(p[1]).toFixed(1)).join(""); }

  g.draw = () => {
    if (!g.w || !g.h) return;
    sc = scales();
    svg.setAttribute("width", g.w); svg.setAttribute("height", g.h); svg.setAttribute("viewBox", "0 0 " + g.w + " " + g.h);
    const kids = [svgEl("rect", { x: 0, y: 0, width: g.w, height: g.h, fill: "#FFFFFF" })];
    const txt = (x, y, t, anchor, fill, weight, size) => svgEl("text", { x, y, "font-size": size || 11, "font-family": G_FONT, fill: fill || G_TEXT, "text-anchor": anchor || "middle", "font-weight": weight || 400, text: t });
    if (g.title) kids.push(txt(sc.l, 14, g.title, "start", G_INK, 600, 12));          // 그림으로 저장할 때만: 위쪽 바깥 제목 한 줄
    /* 격자 · 눈금 */
    const stepT = g.xTime ? niceStep((sc.t1 - sc.t0) / Math.max(2, Math.floor(sc.pw / 60))) : (sc.t1 - sc.t0) / sc.pw * 60 > 10 ? 20 : 10;
    for (let t = sc.t0; t <= sc.t1 + 1e-9; t += stepT) {
      const x = sc.x(t);
      kids.push(svgEl("line", { x1: x, y1: sc.top, x2: x, y2: sc.top + sc.ph, stroke: G_GRID, "stroke-width": 1 }));
      kids.push(txt(x, sc.top + sc.ph + 14, String(Number(t.toPrecision(6)))));
    }
    /* 세로 눈금: 선은 모두 · 글자(윗줄 지수 · 아랫줄 단위)는 앞 글자와 두 줄 높이 넘게 떨어질 때만(아래부터) · 글자 오른쪽 끝은 축에서 G_YL_GAP 안쪽(첫 가로 눈금 글자와 안 겹치게) */
    let lastY = null;
    sc.yticks.forEach((t, i) => {
      const y = sc.y(t.v);
      kids.push(svgEl("line", { x1: sc.l, y1: y, x2: sc.l + sc.pw, y2: y, stroke: G_GRID, "stroke-width": 1 }));
      if (lastY != null && Math.abs(lastY - y) < G_YL_H) return;
      lastY = y;
      const [ex, oh] = sc.ylabels[i];
      kids.push(txt(sc.l - G_YL_GAP, y - 1, ex, "end"));
      kids.push(txt(sc.l - G_YL_GAP, y + 10, oh, "end", G_AXIS, 400, 10));
    });
    kids.push(svgEl("path", { d: "M" + sc.l + " " + sc.top + "V" + (sc.top + sc.ph) + "H" + (sc.l + sc.pw), fill: "none", stroke: G_AXIS, "stroke-width": 1 }));
    kids.push(txt(sc.l + sc.pw / 2, g.h - 4, g.xTime ? "경과 (s)" : "온도 (°C)"));
    const ytx = G_YT_W - 5, yty = sc.top + sc.ph / 2;                // 세로 축 제목: 왼쪽 바깥 띠에 세로로(아래 → 위로 읽음)
    const yTitle = txt(ytx, yty, "저항 (Ω)");
    yTitle.setAttribute("transform", "rotate(-90 " + ytx + " " + yty + ")");
    kids.push(yTitle);
    /* 선: 가열 실선 · 냉각 점선(단계가 바뀌는 점은 두 선이 같이 쓴다 - 끊기지 않게). 반복 측정은 모든 회차가 같은 색으로 이어진다. */
    const tags = [], lines = [], dots = [];
    g.series.forEach((s, si) => {
      const main = s.main != null ? !!s.main : si === 0;
      const col = s.color || G_COLORS[si % 3], wd = main ? 2.5 : 1.5;
      const pts = s.points.filter((p) => p[0] != null && p[1] > 0);
      let run = [];
      const flush = (ph) => { if (run.length > 1) lines.push(svgEl("path", { d: path(run), fill: "none", stroke: col, "stroke-width": wd, "stroke-linejoin": "round", "stroke-dasharray": ph === 1 ? "6 4" : null })); };
      for (let i = 0; i < pts.length; i++) {
        if (i && pts[i][2] !== pts[i - 1][2]) { flush(pts[i - 1][2]); run = [pts[i - 1]]; }
        run.push(pts[i]);
      }
      if (!pts.length) return;
      flush(pts[pts.length - 1][2]);
      if (s.live) { const last = pts[pts.length - 1]; dots.push(svgEl("circle", { cx: sc.x(last[0]), cy: sc.y(last[1]), r: 4, fill: "#FFFFFF", stroke: col, "stroke-width": 2 })); }   // 지금 점(이름표와 따로)
      let hot = pts[0];                                          // 이름표는 온도가 가장 높은 점(가장 오른쪽 점)에 단다
      for (const p of pts) if (p[0] > hot[0]) hot = p;
      tags.push({ name: s.name, col, wd, main, x: sc.x(hot[0]), y: sc.y(hot[1]), ty: sc.y(hot[1]) });
    });
    /* 이름표: 축 밖 오른쪽 띠 · 겹치면 위아래로 벌린다(14px 이상) · 회색 지시선은 곡선의 가장 오른쪽 점에서 오른쪽으로만 나간다 */
    const leaders = [], labels = [];
    tags.sort((a, b) => a.ty - b.ty);
    for (let i = 1; i < tags.length; i++) if (tags[i].ty - tags[i - 1].ty < G_TAG_GAP) tags[i].ty = tags[i - 1].ty + G_TAG_GAP;
    const over = tags.length ? tags[tags.length - 1].ty - (sc.top + sc.ph) : 0;
    if (over > 0) tags.forEach((t) => { t.ty -= over; });
    const edge = sc.l + sc.pw, lx = edge + 10;
    for (const t of tags) {
      leaders.push(svgEl("path", { d: "M" + (t.x + 3).toFixed(1) + " " + t.y.toFixed(1) + "H" + (edge + 3) + "L" + (lx - 3) + " " + t.ty.toFixed(1), fill: "none", stroke: G_AXIS, "stroke-width": 1 }));
      labels.push(svgEl("line", { x1: lx, y1: t.ty, x2: lx + G_KEY_W, y2: t.ty, stroke: t.col, "stroke-width": t.wd }));
      labels.push(txt(lx + G_KEY_W + 4, t.ty + 4, t.name, "start", G_INK, t.main ? 700 : 400));
    }
    kids.push(...leaders, ...lines, ...dots, ...labels);          // 지시선은 곡선 아래에 깔린다
    /* 마우스 자리의 세로 안내선(글자는 위쪽 바깥 줄에) */
    if (g.hoverT != null) {
      const x = sc.x(g.hoverT);
      kids.push(svgEl("line", { x1: x, y1: sc.top, x2: x, y2: sc.top + sc.ph, stroke: G_AXIS, "stroke-width": 1, "stroke-dasharray": "2 3" }));
    }
    setKids(svg, ...kids);
  };

  function nearest(pts, ph, t) {
    let best = null, bd = g.xTime && sc ? (sc.t1 - sc.t0) / 50 : 2.0;     // 2 °C(기록 모드는 가로 폭의 1/50) 안에서 가장 가까운 점
    for (const p of pts) { if (p[2] !== ph || p[0] == null) continue; const d = Math.abs(p[0] - t); if (d < bd) { bd = d; best = p; } }
    return best;
  }
  function readout() {
    if (g.hoverT == null) { read.textContent = ""; return; }      // 마우스를 올렸을 때만 값이 보인다(줄 높이는 CSS 가 잡아 둔다)
    const parts = [g.hoverT.toFixed(1) + (g.xTime ? " s" : " °C")];
    for (const s of g.series) {
      if (g.xTime) { const c = nearest(s.points, 2, g.hoverT); if (c) parts.push(s.name + " " + fmtOhm2(c[1])); continue; }
      const a = nearest(s.points, 0, g.hoverT), b = nearest(s.points, 1, g.hoverT);
      if (a || b) parts.push(s.name + " " + (a ? "가열 " + fmtOhm2(a[1]) : "") + (a && b ? " / " : "") + (b ? "냉각 " + fmtOhm2(b[1]) : ""));
    }
    read.textContent = parts.join("  ·  ");
  }
  svg.addEventListener("mousemove", (ev) => {
    if (!sc) return;
    const r = svg.getBoundingClientRect(), px = ev.clientX - r.left;
    g.hoverT = px >= sc.l && px <= sc.l + sc.pw ? sc.tAt(px) : null;
    readout(); g.draw();
  });
  svg.addEventListener("mouseleave", () => { g.hoverT = null; readout(); g.draw(); });

  new ResizeObserver(() => {
    const w = Math.floor(box.clientWidth), hh = Math.floor(box.clientHeight);
    if (w === g.w && hh === g.h) return;
    g.w = w; g.h = hh; g.draw();
  }).observe(box);

  g.set = (series, opts) => { g.xTime = !!(opts && opts.x === "time"); g.series = series.slice(0, 3); readout(); g.draw(); };

  /* 지금 그림을 PNG 로(가로 2배 해상도). title 을 주면 그래프 위쪽 바깥에 제목 한 줄을 넣는다. Promise<Blob | null> */
  g.toPng = (title) => new Promise((resolve) => {
    if (!g.w || !g.h) { resolve(null); return; }
    const keep = g.hoverT;
    g.hoverT = null; g.title = title || ""; g.draw();
    const xml = new XMLSerializer().serializeToString(svg);
    g.hoverT = keep; g.title = ""; g.draw();                     // 화면 그림은 그대로 돌려 둔다
    const img = new Image();
    img.onload = () => {
      const cv = document.createElement("canvas");
      cv.width = g.w * 2; cv.height = g.h * 2;
      const ctx = cv.getContext("2d");
      ctx.fillStyle = "#FFFFFF"; ctx.fillRect(0, 0, cv.width, cv.height);
      ctx.drawImage(img, 0, 0, cv.width, cv.height);
      cv.toBlob((b) => resolve(b), "image/png");
    };
    img.onerror = () => resolve(null);
    img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(xml);
  });
  return g;
}
