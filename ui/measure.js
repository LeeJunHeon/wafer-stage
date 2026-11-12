/* measure.js - 측정 탭: 판 사진을 보면서 샘플별로 진행을 본다.
   왼쪽 큰 패널 "판": 보정한 판 사진(정사각형) + 측정 순서 표. 오른쪽 패널 "샘플 <이름>": 진행 띠 · 지금 값 · 그래프.
   점 데이터는 state 에 없다 - /api/run/points 로 새 점만 받아 간다. */
"use strict";

const ms = { sel: null, pts: {}, key: "", overlay: false, editing: null, prevState: null };
const ORDER_W = 260, RIGHT_MIN_W = 440;                           // 순서 표 폭 · 오른쪽 패널(그래프) 최소 폭
const STAGE_NAMES = ["이동", "접촉", "냉각", "가열", "냉각", "저장"];
const R = () => (S && S.run) || { rows: [], gate: [], state: "idle" };
const rowOf = (id) => R().rows.find((r) => r.id === id);
const sampleOf = (id) => (S.samples || []).find((s) => s.id === id);

function buildMeasure() {
  /* ---- 왼쪽: 판 ---- */
  ms.ov = svgEl("g", {});
  ms.pb = photoBox({ ar: 1, note: "사진이 없습니다", cls: "plate-photo", onfit: () => drawPlate() });
  ms.pb.svg.append(ms.ov);
  ms.pb.inner.addEventListener("click", (ev) => {
    const [fx, fy] = ms.pb.toFrac(ev), sz = S.cfg.plate_size_mm, x = fx * sz, y = fy * sz;
    let best = null, bd = 9;                                     // 9 mm 안에서 가장 가까운 샘플(없으면 고르기를 풀고 지금 샘플을 따라간다)
    for (const s of S.samples || []) { const d = Math.hypot(s.plate[0] - x, s.plate[1] - y); if (d < bd) { bd = d; best = s.id; } }
    selectSample(best);
  });
  ms.btns = h("div", { class: "row wrap" });
  ms.gate = h("div", { class: "why gate" });
  ms.batch = h("input", { type: "text", class: "name", spellcheck: "false", onchange: async () => { const r = await api("batch_set", { name: ms.batch.value }); if (!r.ok) ms.batch.value = S.cfg.batch_name; } });
  ms.legend = h("div", { class: "small", text: "✓ 끝남 · ✕ 실패 · 흐림 = 제외 · 점선 = 불확실" });      // 샘플이 없으면 숨긴다(자리는 그대로)
  ms.info = h("div", { class: "small" });
  ms.tbody = h("tbody", {});
  ms.table = h("div", { class: "order" }, h("div", { class: "row" }, small("묶음"), ms.batch),
    h("div", { class: "tscroll fill" }, h("table", { class: "tb" }, h("thead", {}, h("tr", {}, h("th", { text: "순서" }), h("th", { text: "샘플" }), h("th", { text: "진행" }))), ms.tbody)),
    small("이름: 두 번 눌러 고침"));
  ms.body = h("div", { class: "plate-body fill" }, ms.pb.box, ms.table);
  const left = ms.left = h("div", { class: "panel fill", id: "ms-left" },
    h("div", { class: "row hd" }, h("h3", { text: "판" }), h("span", { class: "sp" }), ms.btns),
    ms.gate, ms.body, ms.legend, ms.info);
  const ro = new ResizeObserver(layoutPlate);
  ro.observe(ms.body); ro.observe($("#tab-measure"));

  /* ---- 오른쪽: 샘플 ---- */
  ms.title = h("h3", {});
  ms.sub = h("span", { class: "small" });
  ms.act = h("span", { class: "row" });
  ms.follow = btn("지금 샘플 보기", () => selectSample(null), "sm hidden");      // 측정 중에 다른 샘플을 보고 있을 때만
  ms.tLbl = small("온도 · K2110"); ms.rLbl = small("저항 · K2450");
  ms.band = h("div", { class: "band" }, ...STAGE_NAMES.map((n) => h("div", { class: "cell" }, h("i", {}), h("span", { text: n }))));
  ms.line = h("div", { class: "small line1" });
  ms.temp = h("div", { class: "val" }); ms.ohm = h("div", { class: "val" });
  ms.graph = rtGraph();
  ms.ovChk = h("input", { type: "checkbox", onchange: () => { ms.overlay = ms.ovChk.checked; renderMeasure(); } });
  const right = h("div", { class: "panel fill", id: "ms-right" },
    h("div", { class: "row hd" }, ms.title, ms.sub, ms.follow, h("span", { class: "sp" }), ms.act),
    ms.band, ms.line,
    h("div", { class: "vals" }, h("div", {}, ms.tLbl, ms.temp), h("div", {}, ms.rLbl, ms.ohm)),
    h("div", { class: "row" }, h("label", { class: "chk" }, ms.ovChk, "앞 샘플 겹치기")),
    h("div", { class: "fill gwrap" }, ms.graph.el));
  setKids($("#tab-measure"), left, right);
}

/* 왼쪽 패널은 내용에 맞춘다: 판 사진(높이에 맞춘 정사각형) + 12px + 순서 표 260px + 안쪽 여백. 남는 폭은 전부 오른쪽 패널(그래프, 최소 440px)이 가져간다. */
function layoutPlate() {
  const pw = $("#tab-measure").clientWidth - 24, hh = ms.body.clientHeight;        // 탭 안쪽 여백 12px × 2
  if (pw <= 0 || !hh) return;
  const fixed = 12 + ORDER_W + 26;                                                 // 사진과 표 사이 12 + 순서 표 + 패널 안쪽 여백 12 × 2 + 테두리 2
  const side = Math.max(200, Math.floor(Math.min(hh, pw - 12 - RIGHT_MIN_W - fixed)));
  ms.left.style.width = side + fixed + "px";
  ms.pb.box.style.width = side + "px"; ms.pb.box.style.height = side + "px";
  ms.table.style.height = side + "px";
  ms.pb.fit();
}
function selectSample(id) { ms.sel = id; renderMeasure(); syncPoints(); }
function lastMeasured() {                                         // 마지막으로 잰 샘플(가장 늦게 시작한 것)
  let best = null;
  for (const r of R().rows) if (r.t0 != null && r.order != null && (best == null || r.t0 > best.t0)) best = r;
  return best ? best.id : null;
}
/* 오른쪽 패널에 보일 샘플: 고른 샘플 → (안 골랐으면) 지금 측정 중인 샘플 → 측정이 끝났거나 멈췄으면 마지막으로 잰 샘플 */
function shownId() {
  if (ms.sel != null && rowOf(ms.sel)) return ms.sel;
  const run = R();
  if (run.cur != null) return run.cur;
  return run.state === "stopped" || run.state === "done" ? lastMeasured() : null;
}

/* ---- 판 사진 위 표시: 상태는 색만이 아니라 모양으로 ---- */
function drawPlate() {
  if (!S || !ms.pb.w) return;
  const sz = S.cfg.plate_size_mm, k = sz / ms.pb.w, kids = [], shown = shownId();
  ms.pb.svg.setAttribute("viewBox", "0 0 " + sz + " " + sz);
  for (const s of S.samples || []) {
    const row = rowOf(s.id) || { status: s.use ? "대기" : "제외" };
    const st = row.status, pts = s.outline.map((p) => p.join(",")).join(" ");
    const out = st === "제외", now = st === "지금";
    const g = svgEl("g", { opacity: out ? 0.4 : 1 });
    g.append(svgEl("polygon", { points: pts, fill: "none", stroke: "#FFFFFF", "stroke-width": (now ? 5 : 3.5) * k, "stroke-linejoin": "round" }));
    g.append(svgEl("polygon", { points: pts, fill: "none", stroke: "#1F2329", "stroke-width": (now ? 3 : 1.5) * k, "stroke-linejoin": "round", "stroke-dasharray": s.weak ? 4 * k + " " + 3 * k : null }));
    const [cx, cy] = s.plate;
    if (now) { const r = 7 * k; g.append(svgEl("path", { d: "M" + (cx - r) + " " + cy + "H" + (cx + r) + "M" + cx + " " + (cy - r) + "V" + (cy + r), stroke: "#FFFFFF", "stroke-width": 3.5 * k, fill: "none" }));
      g.append(svgEl("path", { d: "M" + (cx - r) + " " + cy + "H" + (cx + r) + "M" + cx + " " + (cy - r) + "V" + (cy + r), stroke: "#1F2329", "stroke-width": 1.5 * k, fill: "none" })); }
    const mark = st === "끝남" ? "✓" : st === "실패" ? "✕" : "";
    if (mark) g.append(svgEl("text", { x: cx, y: cy + 5 * k, "font-size": 15 * k, "font-weight": 700, "text-anchor": "middle", fill: "#1F2329", stroke: "#FFFFFF", "stroke-width": 3 * k, "paint-order": "stroke", text: mark }));
    const top = Math.min(...s.outline.map((p) => p[1]));
    g.append(svgEl("text", { x: cx, y: top - 4 * k, "font-size": 12 * k, "font-weight": s.id === shown ? 700 : 400, "text-anchor": "middle", fill: "#1F2329", stroke: "#FFFFFF", "stroke-width": 3 * k, "paint-order": "stroke", text: row.name || s.name }));
    if (s.id === shown) {
      const xs = s.outline.map((p) => p[0]), ys = s.outline.map((p) => p[1]), m = 3 * k;
      g.append(svgEl("rect", { x: Math.min(...xs) - m, y: Math.min(...ys) - m, width: Math.max(...xs) - Math.min(...xs) + 2 * m, height: Math.max(...ys) - Math.min(...ys) + 2 * m, fill: "none", stroke: "#1F2329", "stroke-width": 1 * k, "stroke-dasharray": 2 * k + " " + 2 * k }));
    }
    kids.push(g);
  }
  setKids(ms.ov, ...kids);
}

function progText(row, s, brief) {                               // brief: 덧붙는 글(주의 한 줄)을 뺀다
  const st = row.status;
  if (st === "끝남") return "끝남 · " + fmtDur(row.dur) + (!brief && row.notes.length ? " · " + row.notes[0] : "");
  if (st === "지금") return "지금 · " + (STAGE_NAMES[R().stage] || "") + (S.temp.value != null ? " " + S.temp.value.toFixed(1) + " °C" : "");
  if (st === "실패") return "실패 · " + row.reason;
  if (st === "멈춤") return "멈춤" + (row.reason ? " · " + row.reason : "");
  if (st === "제외") return "제외" + (s && s.weak ? " · 불확실" : s && s.small ? " · 핀보다 작음" : "");
  return st;
}

/* ---- 순서 표 ---- */
function renderTable() {
  const run = R();
  const rows = run.rows.slice().sort((a, b) => (a.order == null ? 1e9 : a.order) - (b.order == null ? 1e9 : b.order) || a.id - b.id);
  const sig = JSON.stringify(rows.map((r) => [r.id, r.name, r.order, r.status])) + "|" + shownId() + "|" + ms.editing;
  if (sig !== ms.tsig) {
    ms.tsig = sig;
    ms.cells = {};
    setKids(ms.tbody, ...rows.map((r) => {
      let nameEl;
      if (ms.editing === r.id) {
        const inp = h("input", { type: "text", class: "name", value: r.name, spellcheck: "false" });
        const done = async (save) => { if (ms.editing !== r.id) return; ms.editing = null; if (save && inp.value !== r.name) await api("sample_rename", { id: r.id, name: inp.value }); ms.tsig = ""; renderTable(); };
        inp.addEventListener("keydown", (e) => { if (e.key === "Enter") done(true); e.stopPropagation(); });
        inp.addEventListener("blur", () => done(true));
        setTimeout(() => { inp.focus(); inp.select(); }, 0);
        nameEl = h("td", {}, inp);
      } else nameEl = h("td", { text: r.name, ondblclick: (e) => { e.stopPropagation(); if (r.status === "대기" || r.status === "제외") { ms.editing = r.id; ms.tsig = ""; renderTable(); } else toast("이미 잰 샘플의 이름은 고칠 수 없습니다"); } });
      const prog = h("td", { class: "prog" });
      ms.cells[r.id] = prog;
      return h("tr", { class: (r.id === shownId() ? "sel " : "") + (r.status === "제외" ? "out" : ""), onclick: () => selectSample(r.id) }, h("td", { class: "ord", text: r.order == null ? "" : r.order }), nameEl, prog);
    }));
  }
  for (const r of rows) { const c = ms.cells[r.id]; if (c) { const t = progText(r, sampleOf(r.id)); if (c.textContent !== t) { c.textContent = t; c.title = t; } } }
}

/* ---- 위쪽 버튼 ---- */
function renderButtons() {
  const run = R(), n = (S.samples || []).length, used = (S.samples || []).filter((s) => s.use).length;
  const sig = [run.state, n > 0, run.pause_after, run.record].join("|");
  if (sig !== ms.bsig) {
    ms.bsig = sig;
    const b = [];
    if (run.state === "running") {
      b.push(busyBtn("건너뛰기", () => api("run_skip")));
      b.push(btn("끝나면 멈춤", () => api("run_pause_after", { on: !R().pause_after }), run.pause_after ? "on" : ""));
      b.push(busyBtn("측정 중지", () => api("run_stop")));
    } else if (!n) {
      ms.find = busyBtn("사진 찍고 샘플 찾기", () => api("samples_find"), "on"); b.push(ms.find);
    } else {
      if (run.state === "stopped") { ms.resume = busyBtn("이어서 측정", () => api("run_resume"), "on"); b.push(ms.resume); }
      if (run.state === "done") b.push(btn("결과 보기", () => { if (typeof openResult === "function") openResult(R().record); }, "on"));
      ms.start = busyBtn(run.state === "idle" ? "측정 시작" : "전부 다시 측정", () => api("run_start"), run.state === "idle" ? "on" : ""); b.push(ms.start);
      ms.find = busyBtn("사진 다시 찍기", () => api("samples_find")); b.push(ms.find);
      b.push(btn("조건", openRunCond));
    }
    setKids(ms.btns, ...b);
  }
  /* 준비가 덜 됐으면 [측정 시작] 을 회색으로 두고 빠진 것을 한 줄로(누르면 그 준비 단계로) */
  const gate = run.gate || [];
  const kids = [];
  if (run.state !== "running") {
    const findWhy = whyStage(true, true) || whyCamera() || whyHead() || (P().plate ? "" : "판 영역(준비 3)을 먼저 정하세요");
    if (ms.find) ms.find.disabled = !!findWhy;
    const block = gate.length > 0 || (n > 0 && used === 0);
    if (ms.start && n) ms.start.disabled = block;
    if (ms.resume && run.state === "stopped") ms.resume.disabled = gate.length > 0;
    if (gate.length) {
      kids.push("먼저 할 것: ");
      gate.forEach((m, i) => { kids.push(m.step ? h("a", { href: "#", text: m.text, onclick: (e) => { e.preventDefault(); gotoPrep(m.step - 1); } }) : m.text); if (i < gate.length - 1) kids.push(" · "); });
    } else if (!n && findWhy) kids.push(findWhy);
    else if (n && used === 0) kids.push("측정할 샘플이 없습니다");
    else if (run.summary) kids.push(run.summary + (run.msg ? " · " + run.msg : ""));
  }                                                               // 측정 중에는 비워 둔다([끝나면 멈춤] 은 버튼이 켜진 모양으로 보인다)
  setKids(ms.gate, ...kids);
  ms.gate.title = ms.gate.textContent;                            // 한 줄로만 보인다(길면 줄임) - 전체 글은 마우스를 올리면
}

/* ---- 오른쪽 패널 ---- */
function renderSample() {
  const run = R(), id = shownId(), row = id == null ? null : rowOf(id), s = id == null ? null : sampleOf(id);
  ms.temp.textContent = S.temp.value == null ? "—" : S.temp.value.toFixed(1) + " °C";
  ms.ohm.textContent = fmtOhm(S.resistance.ohm);
  /* 측정 중에 다른 샘플을 보고 있으면: [지금 샘플 보기] + 큰 숫자가 누구 값인지(측정 중인 샘플의 값이다) */
  const curRow = run.state === "running" && run.cur != null ? rowOf(run.cur) : null;
  const other = !!curRow && id !== run.cur;
  ms.follow.classList.toggle("hidden", !other);
  ms.tLbl.textContent = other ? "온도 · " + curRow.name + " 측정 중" : "온도 · K2110";
  ms.rLbl.textContent = other ? "저항 · " + curRow.name + " 측정 중" : "저항 · K2450";
  if (!row) {
    ms.title.textContent = "샘플";
    ms.sub.textContent = (S.samples || []).length ? "샘플을 고르세요" : "";
    setKids(ms.act); ms.line.textContent = "";
    setBand(-1, 0);
    ms.graph.set([]);
    return;
  }
  ms.title.textContent = "샘플 " + row.name;
  const isNow = row.status === "지금";
  const el = isNow && row.t0 ? Math.max(0, Date.now() / 1000 - row.t0) : null;
  ms.sub.textContent = (row.order != null ? "순서 " + row.order + " / " + (run.total || run.rows.filter((r) => r.order != null).length) + " · " : "")
    + (isNow ? fmtDur(el) + "째" + (run.repeat > 1 ? " · " + run.cycle + " / " + run.repeat + "회" : "") : progText(row, s, true));
  /* 고른 샘플에 할 수 있는 일 */
  const asig = id + "|" + row.status + "|" + run.state + "|" + (s && s.use);
  if (asig !== ms.asig) {
    ms.asig = asig;
    const b = [];
    if (run.state !== "running" && s && (run.state === "idle" || !row.order)) b.push(btn(s.use ? "제외" : "포함", () => api("sample_use", { id, use: !s.use })));
    if (run.state !== "running" && ["끝남", "실패", "건너뜀", "멈춤"].includes(row.status)) b.push(btn("다시 측정", () => api("run_requeue", { id })));
    setKids(ms.act, ...b);
  }
  if (isNow) { setBand(run.stage, run.frac); ms.line.textContent = run.line || ""; }
  else {
    setBand(row.status === "끝남" ? 6 : -1, 0);
    ms.line.textContent = row.status === "제외" && s ? (s.weak ? "불확실한 샘플 · 확인한 뒤 [포함]" : s.small ? "핀보다 작아 제외" : "") : row.notes.join(" · ");
  }
  /* 그래프: 고른 샘플(진한 색) + 겹쳐 보기면 바로 앞에 끝난 두 샘플 */
  const series = [{ name: row.name, points: ptsOf(id), live: isNow, main: true }];
  if (ms.overlay) {
    const before = run.rows.filter((r) => r.status === "끝남" && r.id !== id && r.order != null && (row.order == null || r.order < row.order)).sort((a, b) => b.order - a.order).slice(0, 2);
    before.forEach((r, i) => series.push({ name: r.name, points: ptsOf(r.id), color: G_COLORS[i + 1], main: false }));
  }
  const gsig = series.map((x) => x.name + ":" + x.points.length + ":" + (x.live ? 1 : 0)).join("|");
  if (gsig !== ms.gsig) { ms.gsig = gsig; ms.graph.set(series); }
}
function setBand(stage, frac) {
  Array.from(ms.band.children).forEach((c, i) => {
    c.classList.toggle("done", stage > i);
    c.classList.toggle("now", stage === i);
    c.firstChild.style.width = stage > i ? "100%" : stage === i ? Math.round(Math.max(0.04, Math.min(1, frac || 0)) * 100) + "%" : "0";
  });
}

/* ---- 점: 새 점만 받아 온다 ---- */
function ptsOf(id) { const p = ms.pts[id]; return p ? p.points : []; }
async function syncPoints() {
  if (!S || tab !== "measure") return;
  const run = R(), key = run.folder || "";
  if (key !== ms.key) { ms.key = key; ms.pts = {}; ms.gsig = ""; }
  const want = new Set();
  const id = shownId();
  if (id != null) want.add(id);
  if (ms.overlay) run.rows.filter((r) => r.status === "끝남").forEach((r) => want.add(r.id));
  let changed = false;
  for (const sid of want) {
    const row = rowOf(sid);
    if (!row) continue;
    let p = ms.pts[sid];
    if (!p || row.n < p.points.length || row.t0 !== p.t0) p = ms.pts[sid] = { points: [], t0: row.t0, busy: false };   // 다시 재면 처음부터
    if (row.n <= p.points.length || p.busy) continue;
    p.busy = true;
    const j = await getJson("/api/run/points?sample=" + sid + "&from=" + p.points.length);
    p.busy = false;
    if (j && j.points && j.points.length) { p.points = p.points.concat(j.points); changed = true; }
  }
  if (changed) renderSample();
}
setInterval(syncPoints, 1000);

/* ---- [조건] 창: 왼쪽 "저항 측정 · 온도" / 오른쪽 "쿨러 · 저장". [닫기] [저장] 은 창 아래에 항상 보인다 ---- */
function openRunCond() {
  const c = S.cfg;
  const num = (k, step) => h("input", { type: "number", step: step || "any", value: c[k] });
  const chk = (k) => h("input", { type: "checkbox", checked: !!c[k] });
  const f = { meter_volt: num("meter_volt", "0.01"), meter_four_wire: chk("meter_four_wire"), meter_avg_count: num("meter_avg_count", "1"), meter_nplc: num("meter_nplc", "0.01"), meter_ilim_a: num("meter_ilim_a"),
    run_start_c: num("run_start_c", "1"), run_heater_off_c: num("run_heater_off_c", "1"), run_cool_end_c: num("run_cool_end_c", "1"), run_repeat: num("run_repeat", "1"), run_interval_s: num("run_interval_s", "0.1"),
    run_cooler_mode: h("select", {}, ...[["zone", "샘플 아래 구역만"], ["all", "전부"], ["none", "안 씀"]].map(([v, t]) => h("option", { value: v, text: t, selected: c.run_cooler_mode === v }))),
    run_cooler_during_heat: chk("run_cooler_during_heat"), nas_dir: h("input", { type: "text", value: c.nas_dir || "", spellcheck: "false" }), nas_date_folder: chk("nas_date_folder") };
  const save = async () => {
    const patch = {};
    for (const [k, e] of Object.entries(f)) patch[k] = e.type === "checkbox" ? e.checked : e.type === "number" ? (["meter_avg_count", "run_repeat"].includes(k) ? parseInt(e.value, 10) : parseFloat(e.value)) : e.value;
    if (await putSettings(patch)) closeDialog();
  };
  const left = h("div", { class: "form" },
    h("h4", { text: "저항 측정" }), "측정 전압 (V)", f.meter_volt, "4단자", f.meter_four_wire, "평균 횟수", f.meter_avg_count, "NPLC", f.meter_nplc, "전류 한도 (A)", f.meter_ilim_a,
    h("h4", { text: "온도" }), "시작 온도 (°C)", f.run_start_c, "가열 끝 온도 (°C)", f.run_heater_off_c,
    "냉각 끝 온도 (°C)", f.run_cool_end_c, "반복 (회)", f.run_repeat, "읽는 간격 (s)", f.run_interval_s);
  const right = h("div", { class: "form" },
    h("h4", { text: "쿨러" }), "쿨러", f.run_cooler_mode, "가열 중 쿨러 켬", f.run_cooler_during_heat,
    h("h4", { text: "저장" }), h("span", {}, "NAS 폴더", h("br"), small("비우면 PC 에만")), f.nas_dir, "날짜 폴더", f.nas_date_folder);
  openDialog("측정 조건", h("div", { class: "form2" }, left, right), [btn("닫기", closeDialog), btn("저장", save, "on")]);
}

function renderMeasure() {
  const run = R(), ph = P().photo, pl = P().plate, find = P().find;
  if (run.state === "running" && ms.prevState !== "running") ms.sel = null;       // 측정을 시작하거나 이어서 하면 고른 샘플을 풀어 지금 샘플을 따라간다
  ms.prevState = run.state;
  ms.pb.setNote(pl ? "사진이 없습니다" : "판 영역(준비 3)을 먼저 정하세요");
  ms.pb.show(pl && ph && ph.rect ? "/api/img/plate.jpg?v=" + ph.id + "-" + pl.t : "");
  const n = (S.samples || []).length, used = (S.samples || []).filter((s) => s.use).length;
  const offWarn = ph && ph.pos_off_mm != null && ph.pos_off_mm > (S.cfg.pos_warn_mm != null ? S.cfg.pos_warn_mm : 2);       // 헤드 위치 차이는 주의 거리(2 mm)를 넘을 때만 붙인다
  ms.info.textContent = find && ph ? "사진 " + fmtClock(find.t) + " · " + n + "개 중 " + used + "개 측정" + (offWarn ? " · 헤드 위치 차이 " + ph.pos_off_mm.toFixed(1) + " mm" : "") : "";
  ms.legend.style.visibility = n ? "" : "hidden";                 // 샘플이 없으면 범례를 숨긴다(자리는 그대로 - 사진이 밀리지 않게)
  if (document.activeElement !== ms.batch) ms.batch.value = S.cfg.batch_name;
  ms.batch.disabled = run.state === "running";
  renderButtons(); renderTable(); renderSample(); drawPlate();
}

TABS.measure = { build: buildMeasure, render: renderMeasure, show: () => { layoutPlate(); syncPoints(); } };
