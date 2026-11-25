/* measure.js - 자동 측정 탭: 판 사진을 보면서 샘플별로 진행을 본다.
   왼쪽 큰 패널 "판": 보정한 판 사진(정사각형) + 측정 순서 표. 오른쪽 패널 "샘플 <이름>": 진행 띠 · 지금 값 · 그래프.
   점 데이터는 state 에 없다 - /api/run/points 로 새 점만 받아 간다. */
"use strict";

const ms = { sel: null, pts: {}, key: "", overlay: false, editing: null, prevState: null };
const ORDER_W = 288, RIGHT_MIN_W = 440;                           // 순서 표 폭(체크 열 28 + 순서 · 샘플 · 진행 260 - app.css .order 와 같게) · 오른쪽 패널(그래프) 최소 폭
const STAGE_NAMES = ["이동", "접촉", "냉각", "가열", "냉각", "저장"];
const HOLD_NAMES = ["이동", "접촉", "기록", "저장"];                      // 기록 모드(히트건 사용 끔): 띠 네 칸 · 서버 단계 0 · 1 · 6 · 5 → 칸 0 · 1 · 2 · 3
const HOLD_CELL = { 0: 0, 1: 1, 6: 2, 5: 3 };
const stageName = (i) => (i === 6 ? "기록" : STAGE_NAMES[i] || "");
const R = () => (S && S.run) || { rows: [], gate: [], state: "idle" };
const rowOf = (id) => R().rows.find((r) => r.id === id);
const sampleOf = (id) => (S.samples || []).find((s) => s.id === id);

function buildMeasure() {
  /* ---- 왼쪽: 판 ---- */
  ms.ov = svgEl("g", {});
  ms.pb = photoBox({ ar: 1, note: "사진 없음", cls: "plate-photo", onfit: () => drawPlate(), dirs: true });     // 문 기준 화면
  ms.pb.svg.append(ms.ov);
  ms.pb.inner.addEventListener("click", (ev) => {
    const [fx, fy] = ms.pb.toFrac(ev), sz = S.cfg.plate_size_mm, x = fx * sz, y = fy * sz;
    let best = null, bd = 9;                                     // 9 mm 안에서 가장 가까운 샘플(없으면 고르기를 풀고 지금 샘플을 따라간다)
    for (const s of S.samples || []) { const d = Math.hypot(s.vplate[0] - x, s.vplate[1] - y); if (d < bd) { bd = d; best = s.id; } }     // 화면 판 mm
    selectSample(best);
  });
  ms.btns = h("div", { class: "row wrap" });
  ms.gate = h("div", { class: "why gate" });
  ms.batch = tk(h("input", { type: "text", class: "name", spellcheck: "false", onchange: async () => { const r = await api("batch_set", { name: ms.batch.value }); if (!r.ok) ms.batch.value = S.cfg.batch_name; } }), "cfg:batch_name");
  ms.legend = h("div", { class: "small", text: "✓ 완료 · ✕ 실패 · 흐림 = 체크 안 함 ·점선 = 미확인" });      // 샘플이 없으면 숨긴다(자리는 그대로)
  ms.info = h("div", { class: "small" });
  ms.tbody = h("tbody", {});
  /* 머리줄 체크 = 전체 선택 / 전체 해제(고를 수 있는 샘플 = "핀보다 작음" · "미확인" 이 아닌 것이 모두 체크면 체크 · 일부면 가운데 줄) */
  ms.allChk = tk(h("input", { type: "checkbox", onclick: (e) => { e.stopPropagation(); api("sample_use_all", { use: !allPicked() }); } }), "ms.useAll");
  ms.table = h("div", { class: "order" }, h("div", { class: "row" }, small("묶음"), ms.batch),
    h("div", { class: "tscroll fill" }, h("table", { class: "tb" }, h("thead", {}, h("tr", {}, h("th", { class: "chk" }, ms.allChk), h("th", { text: "순서" }), h("th", { text: "샘플" }), h("th", { text: "진행" }))), ms.tbody)),
    small("이름 수정: 더블클릭"));
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
  ms.follow = btn("현재 샘플", () => selectSample(null), "sm hidden");      // 측정 중에 다른 샘플을 보고 있을 때만
  ms.tLbl = small("온도 · K2110"); ms.rLbl = small("저항 · K2450");
  ms.band = h("div", { class: "band" }, ...STAGE_NAMES.map((n) => h("div", { class: "cell" }, h("i", {}), h("span", { text: n }))));
  ms.line = h("div", { class: "small line1" });
  ms.spacing = h("div", { class: "small line1" });                    // 점 간격(가열 · 냉각) - 몇 °C 마다 1점
  ms.temp = h("div", { class: "val" }); ms.ohm = h("div", { class: "val" });
  ms.graph = rtGraph();
  ms.ovChk = tk(h("input", { type: "checkbox", onchange: () => { ms.overlay = ms.ovChk.checked; renderMeasure(); } }), "ms.overlay");
  const right = h("div", { class: "panel fill", id: "ms-right" },
    h("div", { class: "row hd" }, ms.title, ms.sub, ms.follow, h("span", { class: "sp" }), ms.act),
    ms.band, ms.line, ms.spacing,
    h("div", { class: "vals" }, h("div", {}, ms.tLbl, ms.temp), h("div", {}, ms.rLbl, ms.ohm)),
    h("div", { class: "row" }, h("label", { class: "chk" }, ms.ovChk, "이전 샘플 겹쳐 보기")),
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
const pickable = () => (S.samples || []).filter((s) => !s.small && !s.weak);     // 전체 선택이 체크하는 샘플
function allPicked() { const p = pickable(); return p.length > 0 && p.every((s) => s.use); }
function lastMeasured() {                                         // 마지막으로 잰 샘플(가장 늦게 시작한 것) - 점이 있는 샘플을 먼저(마지막이 건너뜀이면 빈 그래프가 남았다)
  const pick = (withPts) => { let best = null; for (const r of R().rows) if (r.t0 != null && r.order != null && (!withPts || r.n > 0) && (best == null || r.t0 > best.t0)) best = r; return best; };
  const best = pick(true) || pick(false);
  return best ? best.id : null;
}
const REDO = "이 샘플 다시 측정", ONLY = "이 샘플만 측정";
/* 건너뜀 · 실패 · 중지 샘플의 한 줄: 까닭 · 다음 할 일 - 예) "안 닿음 · 9.54 GΩ · 접촉 높이 · 샘플 위치 확인 뒤 [이 샘플 다시 측정]" */
function nextText(row) {
  const r = row.reason || "";
  const todo = /안 닿음|금속 판|접촉/.test(r) ? "접촉 높이 · 샘플 위치 확인 뒤" : /시작 온도/.test(r) ? "쿨러 · 주위 온도 확인 뒤" : r ? "원인 확인 뒤" : "다시 재려면";
  return (r ? r + " · " : "") + todo + " [" + REDO + "]";
}
/* 오른쪽 패널에 보일 샘플: 고른 샘플 → (안 골랐으면) 지금 측정 중인 샘플 → 측정 중인데 샘플이 없으면(샘플 사이 · 마지막 샘플 뒤 정리 중) ·
   측정이 끝났거나 멈췄으면 마지막으로 잰 샘플. 정리 중에 null 을 주면 그래프가 비고 끝난 뒤 다시 그려지지 않았다 */
function shownId() {
  if (ms.sel != null && rowOf(ms.sel)) return ms.sel;
  const run = R();
  if (run.cur != null) return run.cur;
  return run.state === "running" || run.state === "stopped" || run.state === "done" ? lastMeasured() : null;
}

/* ---- 판 사진 위 표시: 상태는 색만이 아니라 모양으로 ---- */
function drawPlate() {
  if (!S || !ms.pb.w) return;
  const sz = S.cfg.plate_size_mm, k = sz / ms.pb.w, kids = [], shown = shownId();
  ms.pb.svg.setAttribute("viewBox", "0 0 " + sz + " " + sz);
  for (const s of S.samples || []) {
    const row = rowOf(s.id) || { status: s.use ? "대기" : "제외" };
    const st = row.status, pts = s.voutline.map((p) => p.join(",")).join(" ");        // 화면(문 기준) 판 mm
    const now = st === "측정 중";
    const dim = !s.use && !now && s.id !== shown;                  // 진하게 = 체크한 샘플 · 측정 중 · 보고 있는 샘플(기록에 든 샘플이어도 체크를 풀면 흐림)
    const box = svgEl("g"), g = svgEl("g", { opacity: dim ? 0.4 : 1 });     // 상태 표시(✓ · ✕)는 흐리게 하지 않는다(box 에 따로)
    box.append(g);
    g.append(svgEl("polygon", { points: pts, fill: "none", stroke: "#FFFFFF", "stroke-width": (now ? 5 : 3.5) * k, "stroke-linejoin": "round" }));
    g.append(svgEl("polygon", { points: pts, fill: "none", stroke: "#1F2329", "stroke-width": (now ? 3 : 1.5) * k, "stroke-linejoin": "round", "stroke-dasharray": s.weak ? 4 * k + " " + 3 * k : null }));
    const [cx, cy] = s.vplate;
    if (now) { const r = 7 * k; g.append(svgEl("path", { d: "M" + (cx - r) + " " + cy + "H" + (cx + r) + "M" + cx + " " + (cy - r) + "V" + (cy + r), stroke: "#FFFFFF", "stroke-width": 3.5 * k, fill: "none" }));
      g.append(svgEl("path", { d: "M" + (cx - r) + " " + cy + "H" + (cx + r) + "M" + cx + " " + (cy - r) + "V" + (cy + r), stroke: "#1F2329", "stroke-width": 1.5 * k, fill: "none" })); }
    const mark = st === "완료" ? "✓" : st === "실패" ? "✕" : "";
    if (mark) box.append(svgEl("text", { x: cx, y: cy + 5 * k, "font-size": 15 * k, "font-weight": 700, "text-anchor": "middle", fill: "#1F2329", stroke: "#FFFFFF", "stroke-width": 3 * k, "paint-order": "stroke", text: mark }));
    const top = Math.min(...s.voutline.map((p) => p[1]));
    g.append(svgEl("text", { x: cx, y: top - 4 * k, "font-size": 12 * k, "font-weight": s.id === shown ? 700 : 400, "text-anchor": "middle", fill: "#1F2329", stroke: "#FFFFFF", "stroke-width": 3 * k, "paint-order": "stroke", text: row.name || s.name }));
    if (s.id === shown) {
      const xs = s.voutline.map((p) => p[0]), ys = s.voutline.map((p) => p[1]), m = 3 * k;
      g.append(svgEl("rect", { x: Math.min(...xs) - m, y: Math.min(...ys) - m, width: Math.max(...xs) - Math.min(...xs) + 2 * m, height: Math.max(...ys) - Math.min(...ys) + 2 * m, fill: "none", stroke: "#1F2329", "stroke-width": 1 * k, "stroke-dasharray": 2 * k + " " + 2 * k }));
    }
    kids.push(box);
  }
  setKids(ms.ov, ...kids);
}

/* 가열 · 냉각 중 한 줄: 목표 온도 · 열선 상태(속도 제어 - 목표선이 기다리는 중이면 "목표 대기"). 계속 가열이면 열선 상태만. 그 밖에는 "" */
function heatLine(run) {
  const ht = run.heat;
  if (!ht || (run.stage !== 3 && run.stage !== 4)) return "";
  const boot = bootText(S.heater);
  const el = boot || "열선 " + (ht.pct != null ? ht.pct + " %" : ht.element ? "켜짐" : "꺼짐");
  return ht.mode === "ramp" ? (ht.wait ? "목표 대기 " : "목표 ") + Number(ht.sp).toFixed(1) + " °C · " + el : (run.line ? run.line + " · " : "") + el;
}

/* 샘플 크기 "9.8 × 4.1 mm"(긴 변 × 짧은 변 - "핀보다 작음" 판정을 확인할 수 있게) */
function sizeText(s) {
  if (!s || s.w == null || s.h == null) return "";
  const a = Math.max(s.w, s.h), b = Math.min(s.w, s.h);
  return a.toFixed(1) + " × " + b.toFixed(1) + " mm";
}
function progText(row, s, brief) {                               // brief: 덧붙는 글(주의 한 줄)을 뺀다
  const st = row.status;
  if (st === "완료" && row.hold && row.hold.n) return "완료 · " + holdText(row.hold, true);    // 기록 모드: "완료 · 912 kΩ ± 0.8 %"
  if (st === "완료") return "완료 · " + fmtDur(row.dur) + (!brief && row.notes.length ? " · " + row.notes[0] : "");
  if (st === "측정 중") return "측정 중 · " + stageName(R().stage) + (S.temp.value != null ? " " + S.temp.value.toFixed(1) + " °C" : "");
  if (st === "실패") return "실패 · " + row.reason;
  if (st === "중지") return "중지" + (row.reason ? " · " + row.reason : "");
  if ((st === "건너뜀" || st === "대기") && row.reason) return st + " · " + row.reason;      // 예) 건너뜀 · 안 닿음 · 2.56 GΩ / 대기 · 시작 온도 미도달
  if (st === "제외") return "제외" + (s && s.weak ? " · 미확인" : s && s.small ? " · 핀보다 작음 · " + sizeText(s) : "");
  return st;
}

/* ---- 순서 표 ---- */
function renderTable() {
  const run = R();
  const rows = run.rows.slice().sort((a, b) => (a.order == null ? 1e9 : a.order) - (b.order == null ? 1e9 : b.order) || a.id - b.id);
  const running = run.state === "running";
  const sig = JSON.stringify(rows.map((r) => { const s = sampleOf(r.id); return [r.id, r.name, r.order, r.status, !!(s && s.use)]; })) + "|" + shownId() + "|" + ms.editing + "|" + running;
  if (sig !== ms.tsig) {
    ms.tsig = sig;
    ms.cells = {};
    setKids(ms.tbody, ...rows.map((r) => {
      let nameEl;
      if (ms.editing === r.id) {
        const inp = tk(h("input", { type: "text", class: "name", value: r.name, spellcheck: "false" }), "ms.rename");
        const done = async (save) => { if (ms.editing !== r.id) return; ms.editing = null; if (save && inp.value !== r.name) await api("sample_rename", { id: r.id, name: inp.value }); ms.tsig = ""; renderTable(); };
        inp.addEventListener("keydown", (e) => { if (e.key === "Enter") done(true); e.stopPropagation(); });
        inp.addEventListener("blur", () => done(true));
        setTimeout(() => { inp.focus(); inp.select(); }, 0);
        nameEl = h("td", {}, inp);
      } else nameEl = h("td", { text: r.name, ondblclick: (e) => { e.stopPropagation(); if (r.status === "대기" || r.status === "제외") { ms.editing = r.id; ms.tsig = ""; renderTable(); } else toast("측정한 샘플은 이름 수정 불가"); } });
      const prog = h("td", { class: "prog" });
      ms.cells[r.id] = prog;
      /* 체크 = 그 샘플을 잴지(use) · 누르면 sample_use - 줄 클릭(그 샘플 보기)으로 번지지 않게 */
      const s = sampleOf(r.id);
      const ck = tk(h("input", { type: "checkbox", checked: !!(s && s.use), onclick: async (e) => { e.stopPropagation(); const el = e.target, r0 = await api("sample_use", { id: r.id, use: el.checked }); if (!r0.ok) el.checked = !el.checked; } }), "ms.use");     // 거절되면 되돌림
      setOff(ck, running ? "측정 중 변경 불가" : !s ? "샘플 없음" : "");
      const ckTd = h("td", { class: "chk", onclick: (e) => e.stopPropagation() }, ck);
      return h("tr", { class: (r.id === shownId() ? "sel " : "") + (r.status === "제외" ? "out" : ""), onclick: () => selectSample(r.id) }, ckTd, h("td", { class: "ord", text: r.order == null ? "" : r.order }), nameEl, prog);
    }));
  }
  for (const r of rows) { const c = ms.cells[r.id]; if (c) { const t = progText(r, sampleOf(r.id)); if (c.textContent !== t) { c.textContent = t; c.title = t; } } }
  const all = S.samples || [], nUse = all.filter((s) => s.use).length;
  ms.allChk.checked = allPicked();
  ms.allChk.indeterminate = !ms.allChk.checked && nUse > 0;
  setOff(ms.allChk, running ? "측정 중 변경 불가" : !all.length ? "샘플 없음" : "");
}

/* ---- 위쪽 버튼 ---- */
function renderButtons() {
  const run = R(), n = (S.samples || []).length, used = (S.samples || []).filter((s) => s.use).length;
  const rec = run.state === "stopped" || run.state === "done", rest = run.rest || 0;     // 측정 기록이 있다 · [나머지 측정] 이 잴 수(서버 rest_ids)
  const sig = [run.state, n > 0, run.pause_after, run.record, used, rest].join("|");
  if (sig !== ms.bsig) {
    ms.bsig = sig;
    const b = [];
    ms.start = ms.resume = null;
    if (run.state === "running") {
      b.push(busyBtn("건너뛰기", () => api("run_skip")));
      b.push(btn("완료 후 정지", () => api("run_pause_after", { on: !R().pause_after }), run.pause_after ? "on" : ""));
      b.push(busyBtn("측정 중지", () => api("run_stop")));
    } else if (!n) {
      ms.find = busyBtn("촬영 · 샘플 검출", () => apiMove("samples_find"), "on"); b.push(ms.find);
    } else {
      /* 기록 없음: [측정 시작 (n개)] = 체크한 샘플 · 새 폴더 / 기록 있음: [나머지 측정 (n개)] = 체크한 샘플 중 완료 아닌 것 · 같은 폴더 · [새로 측정 (m개)] = 체크한 샘플 전부 · 새 폴더 */
      if (rec) { ms.resume = tk(busyBtn("나머지 측정 (" + rest + "개)", () => apiMove("run_resume"), rest > 0 ? "on" : ""), "ms.rest"); b.push(ms.resume); }
      ms.start = tk(busyBtn((rec ? "새로 측정" : "측정 시작") + " (" + used + "개)", () => apiMove("run_start"), rec ? "" : "on"), rec ? "ms.new" : "ms.start"); b.push(ms.start);
      ms.find = busyBtn("재촬영", () => apiMove("samples_find")); b.push(ms.find);
      b.push(tk(btn("조건", openRunCond), "ms.cond"));
    }
    setKids(ms.btns, ...b);
  }
  /* 초기 설정이 덜 됐으면 [측정 시작] 을 회색으로 두고 빠진 것을 한 줄로(누르면 그 초기 설정 단계로) */
  const gate = run.gate || [];
  const kids = [];
  if (run.state !== "running") {
    const findWhy = whyStage(true, true) || whyCamera() || whyHead() || (P().plate ? "" : "판 영역 설정 필요 (초기 설정 3)");
    if (ms.find) setOff(ms.find, findWhy);
    const gateWhy = gate.length ? "필요 항목 · " + gate.map((g) => g.text).join(" · ") : "";
    if (ms.start && n) setOff(ms.start, gateWhy || (n > 0 && used === 0 ? "측정할 샘플 없음 · 샘플 체크 필요" : ""));
    if (ms.resume) setOff(ms.resume, gateWhy || (rest ? "" : used ? "체크한 샘플이 모두 완료" : "측정할 샘플 없음 · 샘플 체크 필요"));
    if (gate.length) {
      kids.push("필요 항목: ");
      gate.forEach((m, i) => { kids.push(m.step ? h("a", { href: "#", text: m.text, onclick: (e) => { e.preventDefault(); gotoPrep(m.step - 1); } }) : m.text); if (i < gate.length - 1) kids.push(" · "); });
    } else if (!n && findWhy) kids.push(findWhy);
    else if (n && used === 0) kids.push("측정할 샘플 없음");
    else if (run.summary) kids.push(run.summary + (run.msg ? " · " + run.msg : ""));
  }                                                               // 측정 중에는 비워 둔다([끝나면 멈춤] 은 버튼이 켜진 모양으로 보인다)
  /* 요약 줄 오른쪽 끝에 [결과 보기](끝난 측정) - 버튼 줄에는 두지 않는다(한 줄에 들어가게) */
  const showRes = run.state === "done" && !gate.length && run.record;
  const gsig = JSON.stringify([kids.map((k) => (typeof k === "string" ? k : k.textContent)), !!showRes]);
  if (gsig !== ms.gateSig) {                                      // 같은 글이면 다시 그리지 않는다(결과 보기 버튼을 누르는 중에 바뀌지 않게)
    ms.gateSig = gsig;
    ms.gateTxt = h("span", { class: "gtxt" }, ...kids);
    setKids(ms.gate, ms.gateTxt, showRes ? btn("결과 보기", () => { if (typeof openResult === "function") openResult(R().record); }, "sm") : null);
    ms.gate.title = ms.gateTxt.textContent;                       // 한 줄로만 보인다(길면 줄임) - 전체 글은 마우스를 올리면
  }
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
    ms.sub.textContent = (S.samples || []).length ? "샘플 선택" : "";
    setKids(ms.act); ms.line.textContent = ""; ms.spacing.textContent = "";
    setBand(-1, 0);
    ms.graph.set([]);
    ms.gsig = "";                                                 // 비운 것을 기억하지 않는다 - 다음에 샘플이 보이면 반드시 다시 그린다
    return;
  }
  ms.title.textContent = "샘플 " + row.name;
  const isNow = row.status === "측정 중";
  const el = isNow && row.t0 ? Math.max(0, Date.now() / 1000 - row.t0) : null;
  ms.sub.textContent = (row.order != null ? "순서 " + row.order + " / " + (run.total || run.rows.filter((r) => r.order != null).length) + " · " : "")
    + (isNow ? fmtDur(el) + " 경과" + (run.repeat > 1 ? " · " + run.cycle + " / " + run.repeat + "회" : "") : progText(row, s, true));
  /* 고른 샘플에 할 수 있는 일 */
  const asig = id + "|" + row.status + "|" + run.state + "|" + !!s;
  if (asig !== ms.asig) {
    ms.asig = asig;
    const b = [];                                                 // 잴지 말지는 순서 표의 체크로 고른다 · 여기 버튼은 그 샘플 하나만 바로 잰다(확인 창 → 진행)
    if (run.state !== "running" && s) {
      if (["완료", "실패", "건너뜀", "중지"].includes(row.status)) b.push(tk(busyBtn(REDO, () => apiMove("run_one", { id })), "ms.redo"));
      else if (row.status === "대기" || row.status === "제외") b.push(tk(busyBtn(ONLY, () => apiMove("run_one", { id })), "ms.only"));
    }
    setKids(ms.act, ...b);
  }
  const hold = run.mode === "hold";
  const names = hold ? HOLD_NAMES : STAGE_NAMES;                  // 띠 칸(기록 모드는 네 칸 · 가열 모드는 여섯 칸) - 방식이 바뀔 때만 다시 그린다
  if (ms.bandSig !== names.join("|")) { ms.bandSig = names.join("|"); setKids(ms.band, ...names.map((n) => h("div", { class: "cell" }, h("i", {}), h("span", { text: n })))); }
  ms.spacing.textContent = row.hold ? holdText(row.hold) : row.curve ? [curveText(row.curve, "heat"), curveText(row.curve, "cool")].filter(Boolean).join(" / ") : spacingText(row.spacing);
  if (isNow) { setBand(hold ? (HOLD_CELL[run.stage] != null ? HOLD_CELL[run.stage] : -1) : run.stage, run.frac); ms.line.textContent = heatLine(run) || run.line || ""; }
  else {
    setBand(row.status === "완료" ? 6 : -1, 0);
    ms.line.textContent = ["건너뜀", "실패", "중지"].includes(row.status) ? nextText(row)
      : [s ? "크기 " + sizeText(s) : "", row.status === "제외" && s ? (s.weak ? "미확인 샘플 · 확인 후 체크" : s.small ? "핀보다 작음" : "") : row.notes.join(" · ")].filter(Boolean).join(" · ");
  }
  /* 그래프: 고른 샘플(진한 색) + 겹쳐 보기면 바로 앞에 끝난 두 샘플 */
  const pv = (pid) => (hold ? ptsOf(pid).filter((p) => p.length > 3).map((p) => [p[3], p[1], 2]) : ptsOf(pid));   // 기록 모드: [경과, 저항] - 저항–시간
  const series = [{ name: row.name, points: pv(id), live: isNow, main: true }];
  if (ms.overlay) {
    const before = run.rows.filter((r) => r.status === "완료" && r.id !== id && r.order != null && (row.order == null || r.order < row.order)).sort((a, b) => b.order - a.order).slice(0, 2);
    before.forEach((r, i) => series.push({ name: r.name, points: pv(r.id), color: G_COLORS[i + 1], main: false }));
  }
  const gsig = (hold ? "h|" : "") + series.map((x) => x.name + ":" + x.points.length + ":" + (x.live ? 1 : 0)).join("|");
  if (gsig !== ms.gsig) { ms.gsig = gsig; ms.graph.set(series, hold ? { x: "time" } : null); }
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
  if (ms.overlay) run.rows.filter((r) => r.status === "완료").forEach((r) => want.add(r.id));
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
  const f = { run_heater_use: chk("run_heater_use"), run_hold_s: num("run_hold_s", "1"),
    contact_check: h("select", {}, ...[["off", "사용 안 함"], ["skip", "그 샘플 건너뜀"], ["stop", "측정 멈춤"]].map(([v, t]) => h("option", { value: v, text: t, selected: c.contact_check === v }))),
    contact_max_ohm: num("contact_max_ohm"), contact_min_ohm: h("input", { type: "number", step: "any", value: c.contact_min_ohm == null ? "" : c.contact_min_ohm, placeholder: "없음" }),
    meter_volt: num("meter_volt", "0.01"), meter_four_wire: chk("meter_four_wire"), meter_avg_count: num("meter_avg_count", "1"), meter_nplc: num("meter_nplc", "0.01"), meter_ilim_a: num("meter_ilim_a"),
    run_start_c: num("run_start_c", "1"), run_heater_off_c: num("run_heater_off_c", "1"), run_cool_end_c: num("run_cool_end_c", "1"), run_repeat: num("run_repeat", "1"),
    run_read_mode: h("select", {}, ...[["seq", "순차"], ["sync", "동시 · 연속"]].map(([v, t]) => h("option", { value: v, text: t, selected: (c.run_read_mode || "seq") === v }))),
    run_interval_s: num("run_interval_s", "0.1"),
    run_cooler_mode: h("select", {}, ...[["zone", "샘플 아래 구역"], ["all", "전체"], ["none", "사용 안 함"]].map(([v, t]) => h("option", { value: v, text: t, selected: c.run_cooler_mode === v }))),
    run_cooler_during_heat: chk("run_cooler_during_heat"),
    run_heat_mode: h("select", {}, ...[["ramp", "가열 속도 제어"], ["full", "계속 가열"]].map(([v, t]) => h("option", { value: v, text: t, selected: c.run_heat_mode === v }))),
    run_rate_by: h("select", {}, ...[["step", "점 간격"], ["time", "걸리는 시간"]].map(([v, t]) => h("option", { value: v, text: t, selected: c.run_rate_by === v }))),
    run_heat_time_s: num("run_heat_time_s", "1"), run_cool_time_s: num("run_cool_time_s", "1"),
    run_point_step_c: num("run_point_step_c", "0.01"), run_fan_during_cool: chk("run_fan_during_cool"), cool_rate_separate: chk("cool_rate_separate"),
    run_cool_mode: h("select", {}, ...[["ramp", "냉각 속도 제어"], ["free", "자연 냉각"]].map(([v, t]) => h("option", { value: v, text: t, selected: c.run_cool_mode === v }))),
    run_cool_rate_c_min: num("run_cool_rate_c_min", "0.1"), heater_lead_c: num("heater_lead_c", "0.5"),
    contact_mode: h("select", {}, ...[["fixed", "고정 높이"], ["search", "전기 탐색"]].map(([v, t]) => h("option", { value: v, text: t, selected: c.contact_mode === v }))),
    nas_dir: h("input", { type: "text", value: c.nas_dir || "", spellcheck: "false" }), nas_date_folder: chk("nas_date_folder") };
  for (const [k, el] of Object.entries(f)) tk(el, "cfg:" + k);   // 말풍선 = 그 설정의 설명
  const save = async () => {
    const patch = {};
    if (await putSettings(condPatch())) closeDialog();
  };
  const condPatch = () => {
    const patch = {};
    for (const [k, e] of Object.entries(f)) patch[k] = e.type === "checkbox" ? e.checked : k === "contact_min_ohm" ? (e.value.trim() === "" ? null : parseFloat(e.value))   // 비우면 하한 안 봄
      : e.type === "number" ? (["meter_avg_count", "run_repeat"].includes(k) ? parseInt(e.value, 10) : parseFloat(e.value)) : e.value;
    return patch;
  };
  /* 예상 시간: 고치는 값으로 바로 다시 계산(샘플 수 × 반복) */
  const expect = h("div", { class: "small" });
  const nUse = (S.samples || []).filter((x) => x.use).length || 1;
  /* 속도 정하는 방법: 걸리는 시간이면 가열 · 냉각 시간만 · 점 간격이면 목표 점 간격 · 냉각 속도 따로 · 냉각 속도만 쓴다(안 쓰는 칸은 잠금) */
  /* 히트건 사용 끔(기록 모드): 시작 · 가열 끝 · 냉각 끝 온도 · 반복 · 가열 · 냉각 · 쿨러 칸을 잠근다 / 켜면 기록 시간을 잠근다 */
  const HEAT_ONLY = ["run_start_c", "run_heater_off_c", "run_cool_end_c", "run_repeat", "run_rate_by", "run_heat_time_s", "run_cool_time_s", "run_heat_mode",
    "run_point_step_c", "run_cool_mode", "cool_rate_separate", "run_cool_rate_c_min", "heater_lead_c", "run_fan_during_cool", "run_cooler_mode", "run_cooler_during_heat"];
  const lockRate = () => {
    const t = f.run_rate_by.value === "time", heat = f.run_heater_use.checked;
    const byStep = t ? "" : "속도 정하는 방법 · 점 간격이면 안 씀", byTime = t ? "속도 정하는 방법 · 걸리는 시간이면 안 씀" : "";
    setOff(f.run_heat_time_s, byStep); setOff(f.run_cool_time_s, byStep);
    setOff(f.run_point_step_c, byTime); setOff(f.cool_rate_separate, byTime); setOff(f.run_cool_rate_c_min, byTime);
    if (!heat) for (const k of HEAT_ONLY) setOff(f[k], "히트건 사용 끔이면 안 씀");
    setOff(f.run_hold_s, heat ? "히트건 사용 켬이면 안 씀" : "");
    setOff(f.run_interval_s, f.run_read_mode.value === "sync" ? "읽기 방식 동시 · 쉬지 않고 연속이라 측정 간격을 쓰지 않음" : "");
  };
  const upd = () => { lockRate(); const pt = condPatch(); expect.textContent = expectText(Object.assign({}, c, pt), pt.run_heater_use === false ? nUse : nUse * (pt.run_repeat || 1)); };
  Object.values(f).forEach((e) => { e.addEventListener("input", upd); e.addEventListener("change", upd); });
  upd();
  const left = h("div", { class: "form" },
    h("h4", { text: "저항 측정" }), "측정 전압 (V)", f.meter_volt, "4단자", f.meter_four_wire, "평균 횟수", f.meter_avg_count, "NPLC", f.meter_nplc, "전류 한도 (A)", f.meter_ilim_a,
    h("h4", { text: "온도" }), "히트건 사용", f.run_heater_use, "기록 시간 (s)", f.run_hold_s, "시작 온도 (°C)", f.run_start_c, "가열 끝 온도 (°C)", f.run_heater_off_c,
    "냉각 끝 온도 (°C)", f.run_cool_end_c, "반복 (회)", f.run_repeat, "읽기 방식", f.run_read_mode, "측정 간격 (s)", f.run_interval_s);
  const right = h("div", { class: "form" },
    h("h4", { text: "가열 · 냉각" }), "속도 정하는 방법", f.run_rate_by, "가열 시간 (s)", f.run_heat_time_s, "냉각 시간 (s)", f.run_cool_time_s,
    "가열 방식", f.run_heat_mode, "목표 점 간격 (°C)", f.run_point_step_c,
    "냉각 방식", f.run_cool_mode, "냉각 속도 따로", f.cool_rate_separate, "냉각 속도 (°C/분)", f.run_cool_rate_c_min, "목표선 기다림 폭 (°C)", f.heater_lead_c, "냉각 중 송풍", f.run_fan_during_cool,
    h("div", { style: "grid-column: 1 / -1" }, expect),
    h("h4", { text: "쿨러" }), "쿨러", f.run_cooler_mode, "가열 중 쿨러 켬", f.run_cooler_during_heat,
    h("h4", { text: "접촉" }), "접촉 방식", f.contact_mode, "접촉 확인", f.contact_check, "접촉 판정 상한 (Ω)", f.contact_max_ohm, "금속 판 판정 하한 (Ω)", f.contact_min_ohm,
    h("h4", { text: "저장" }), h("span", {}, "NAS 폴더", h("br"), small("비우면 PC 에만 저장")),
    S.can_pick_folder ? h("div", { class: "row" }, f.nas_dir, browseBtn(f.nas_dir)) : f.nas_dir, "날짜 폴더", f.nas_date_folder);
  openDialog("측정 조건", h("div", { class: "form2" }, left, right), [btn("닫기", closeDialog), btn("저장", save, "on")]);
}

function renderMeasure() {
  const run = R(), ph = P().photo, pl = P().plate, find = P().find;
  if (run.state === "running" && ms.prevState !== "running") ms.sel = null;       // 측정을 시작하거나 이어서 하면 고른 샘플을 풀어 지금 샘플을 따라간다
  ms.prevState = run.state;
  ms.pb.setNote(pl ? "사진 없음" : "판 영역 설정 필요 (초기 설정 3)");
  ms.pb.show(pl && ph && ph.rect ? "/api/img/plate.jpg?v=" + ph.id + "-" + pl.t : "");
  const n = (S.samples || []).length, used = (S.samples || []).filter((s) => s.use).length;
  const offWarn = ph && ph.pos_off_mm != null && ph.pos_off_mm > (S.cfg.pos_warn_mm != null ? S.cfg.pos_warn_mm : 2);       // 헤드 위치 차이는 주의 거리(2 mm)를 넘을 때만 붙인다
  ms.info.textContent = find && ph ? "사진 " + fmtClock(find.t) + " · " + n + "개 중 " + used + "개 측정" + (offWarn ? " · 헤드 위치 차이 " + ph.pos_off_mm.toFixed(1) + " mm" : "") : "";
  ms.legend.style.visibility = n ? "" : "hidden";                 // 샘플이 없으면 범례를 숨긴다(자리는 그대로 - 사진이 밀리지 않게)
  if (document.activeElement !== ms.batch) ms.batch.value = S.cfg.batch_name;
  setOff(ms.batch, run.state === "running" ? "측정 중 변경 불가" : "");
  renderButtons(); renderTable(); renderSample(); drawPlate();
}

TABS.measure = { build: buildMeasure, render: renderMeasure, show: () => { layoutPlate(); syncPoints(); } };
