/* manual.js - 수동 측정 탭: 모터를 움직이지 않는다(원점 · 접촉 높이 · 샘플 선택이 필요 없다 · 끝나도 Z 를 올리지 않는다).
   [온도 1번] [저항 1번] [연속 측정] [R–T 시작] [정지] · 쿨러 위치(3×3 구역에서 고르거나 번호) · 릴레이 2 초 켜 보기 · 명령 직접 보내기(접힘).
   파라미터는 측정 탭과 같은 R–T 측정 조건(rt.*)을 쓴다. 왼쪽 그래프 둘: 저항–온도(세로 로그) · 시간–온도와 저항(오른쪽 축 로그).
   그래프 라벨 · 숫자는 그림 바깥 띠에 두고 지시선으로만 잇는다(rt.js 의 frame · readout). */
"use strict";
const MN = { pts: [], id: "", n: 0, busy: false, dirty: false, built: false, sig: "", scpi: false, out: [], dev: "k2110", relayN: null };

function manRun(s) { return s.manual && s.manual.run; }
function manLive(s) { const r = manRun(s); return !!(r && (r.state === "running" || r.state === "stopping")); }

/* ---- 그래프 ---- */
function drawManRT(svg) {
  const [W, H] = boxSize(svg, 900, 330), m = { l: 66, r: Math.max(108, Math.min(150, W * 0.22)), t: 34, b: 48 };
  const pts = MN.pts.filter((p) => p.T != null && p.R != null);
  const rg = UI.rtRange(pts.map((p) => p.T), pts.map((p) => p.R));
  const f = frame(svg, W, H, m, rg.x, rg.y, { ylog: true, xlabel: "온도 [°C]", ylabel: "저항 [Ω]", xticks: 13 });
  const cyc = Math.max(1, ...pts.map((p) => p.cy || 1));
  const groups = {};
  pts.forEach((p) => { const k = p.ph + p.cy; (groups[k] = groups[k] || { ph: p.ph, cy: p.cy, a: [] }).a.push([f.px(p.T), f.py(p.R)]); });
  Object.values(groups).sort((a, b) => a.cy - b.cy).forEach((gp) => dots(f.g, gp.a, shade(PHASE_COL(gp.ph), cyc <= 1 ? 1 : 0.15 + 0.85 * ((gp.cy || 1) - 1) / (cyc - 1)), 2.6));
  let x = m.l;
  [["가열", C_HEAT], ["냉각", C_COOL], ["그 밖", C_PRE]].forEach(([l, c]) => { svg.append(svgEl("circle", { cx: x + 6, cy: 18, r: 5, fill: c })); svg.append(svgEl("text", { x: x + 16, y: 23, "font-size": 13, fill: "#3b434b" }, l)); x += 64; });
  const last = pts.length ? pts[pts.length - 1] : null;
  readout(f, last ? [f.px(last.T), f.py(last.R)] : null, last ? [last.ph + (last.cy ? " " + last.cy + "회" : ""), f1(last.T, 1) + " °C", fmtE(last.R) + " Ω"] : ["값 없음"], last ? PHASE_COL(last.ph) : "#8a929b", pts.map((p) => [f.px(p.T), f.py(p.R)]));
}
function drawManTime(svg) {
  const [W, H] = boxSize(svg, 900, 300), m = { l: 66, r: Math.max(170, Math.min(210, W * 0.27)), t: 34, b: 48 };
  const pts = MN.pts, tv = pts.filter((p) => p.T != null), rv = pts.filter((p) => p.R != null && p.R > 0);
  const tmax = Math.max(60, ...pts.map((p) => p.t)), unitMin = tmax > 600;
  const ymin = Math.min(20, ...(tv.length ? tv.map((p) => p.T) : [20])), ymax = Math.max(90, ...(tv.length ? tv.map((p) => p.T) : [90]));
  const f = frame(svg, W, H, m, [0, unitMin ? tmax / 60 : tmax], [Math.floor(ymin / 5) * 5, Math.ceil(ymax / 5) * 5], { xlabel: "시간 [" + (unitMin ? "분" : "s") + "]", ylabel: "온도 [°C]", xticks: 12 });
  const X = (t) => f.px(unitMin ? t / 60 : t);
  /* 오른쪽 축: 저항(로그) - 눈금 숫자는 그림 바깥 오른쪽 띠 */
  const rr = UI.rtRange([], rv.map((p) => p.R)).y, l0 = Math.log10(rr[0]), l1 = Math.log10(rr[1]);
  const PY = (r) => f.m.t + f.ph - ((Math.log10(Math.max(r, 1e-300)) - l0) / (l1 - l0)) * f.ph;
  const right = m.l + f.pw;
  for (let e = Math.ceil(l0 - 1e-9); e <= l1 + 1e-9; e++) {
    svg.append(svgEl("line", { x1: right, x2: right + 5, y1: PY(Math.pow(10, e)), y2: PY(Math.pow(10, e)), stroke: "#7c5cbf" }));
    svg.append(svgEl("text", { x: right + 8, y: PY(Math.pow(10, e)) + 4, "font-size": 12, fill: "#7c5cbf" }, "1E+" + e));
  }
  svg.append(svgEl("text", { x: right + 8, y: m.t - 10, "font-size": 12, "font-weight": 700, fill: "#7c5cbf" }, "저항 [Ω]"));
  const segs = [];
  pts.forEach((p) => { const k = p.ph + "|" + p.cy; const sg = segs[segs.length - 1]; if (sg && sg.k === k) sg.a.push(p); else segs.push({ k, ph: p.ph, a: [p] }); });
  segs.forEach((sg) => poly(f.g, sg.a.filter((p) => p.T != null).map((p) => [X(p.t), f.py(p.T)]), PHASE_COL(sg.ph), 2));
  poly(f.g, rv.map((p) => [X(p.t), PY(p.R)]), "#7c5cbf", 1.5, "4 3");
  const lastT = tv.length ? tv[tv.length - 1] : null, lastR = rv.length ? rv[rv.length - 1] : null;
  readout(f, lastT ? [X(lastT.t), f.py(lastT.T)] : null, lastT ? [f1(lastT.T, 1) + " °C", lastR ? fmtE(lastR.R) + " Ω" : "저항 없음", lastT.h ? "히트건 켜짐" : "히트건 꺼짐"] : ["값 없음"], lastT ? PHASE_COL(lastT.ph) : "#8a929b", null, 58);
}
function redrawMan() { if (MN.svgRT) drawManRT(MN.svgRT); if (MN.svgTt) drawManTime(MN.svgTt); }

async function fetchManPts() {
  if (MN.busy) return;
  MN.busy = true;
  try {
    const r = await (await fetch("/rt.json?kind=manual&since=" + MN.n + "&id=" + encodeURIComponent(MN.id))).json();
    if (r.id !== MN.id) { MN.pts = []; MN.n = 0; MN.id = r.id; MN.dirty = true; }
    if (r.points.length) { MN.pts.push(...r.points); MN.n = r.n; MN.dirty = true; }
  } catch (e) { /* 다음 주기에 */ } finally { MN.busy = false; }
}
setInterval(() => {
  const s = UI.state;
  if (!s || UI.tab !== "manual" || !s.manual) return;
  const run = manRun(s);
  if (manLive(s) || (run && (run.n_points || 0) !== MN.n) || (!MN.id && run)) fetchManPts().then(() => { if (MN.dirty) { MN.dirty = false; redrawMan(); } });
}, 400);

/* ---- 화면 ---- */
function buildManual() {
  if (MN.built) return;
  MN.svgRT = svgEl("svg", { class: "gsvg", id: "gm-rt", preserveAspectRatio: "xMidYMid meet" });
  MN.svgTt = svgEl("svg", { class: "gsvg", id: "gm-tt", preserveAspectRatio: "xMidYMid meet" });
  const gl = h("div", { class: "mnleft" }, h("div", { class: "graph gbox", id: "gbm-rt" }, MN.svgRT), h("div", { class: "graph gbox", id: "gbm-tt" }, MN.svgTt));
  MN.main = h("div", { class: "card", id: "c-man" });
  MN.rest = h("div", { class: "mntop" });
  $("#tab-manual").replaceChildren(gl, h("div", { class: "mnright" }, MN.main, MN.rest));
  let raf = 0;
  const ro = new ResizeObserver(() => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => { if (UI.tab === "manual" && MN.svgRT.parentNode.clientWidth) redrawMan(); }); });
  ro.observe(gl); ro.observe(MN.svgRT.parentNode); ro.observe(MN.svgTt.parentNode);
  MN.built = true;
}
function manPhase(run) {
  const ph = run.phase || "";
  if (ph === "가열" || ph === "냉각") return ph + " " + run.cycle + " / " + run.repeat + "회";
  return ph || "시작 중";
}
function cardMan(s) {
  const mn = s.manual, run = mn.run, live = manLive(s), ik = (s.rt && s.rt.inst) || {};
  const dchip = (k, d) => h("span", { class: d && d.ok ? "chip ok" : "chip no", "data-tip": d ? (d.ok ? (d.idn || "") + " · " + (d.addr || "") : d.err || "") : "장비 연결을 누르거나 측정 버튼을 누르면 연결합니다.", text: k + (d && d.ok ? " 연결" : " 안 됨") });
  const connected = ik.k2110 && ik.k2110.ok && ik.k2450 && ik.k2450.ok;
  const head = h("div", { class: "chead" }, h("h3", { text: "수동 측정", "data-tip": "모터는 움직이지 않습니다. 원점 · 접촉 높이 · 샘플이 없어도 됩니다. 끝나도 Z 를 올리지 않습니다." }), h("span", { class: "grow" }),
    dchip("K2110", ik.k2110), dchip("K2450", ik.k2450),
    connected ? null : btn("장비 연결", "rt-connect", null, "sm", "K2110 · K2450 을 엽니다(시리얼 포트는 열지 않습니다).", mn.block_quick));
  const big = h("div", { class: "rtbig" }, h("div", {}, h("div", { class: "k", text: "온도" }), h("div", { class: "v", id: "mn-T", text: "—" })), h("div", {}, h("div", { class: "k", text: "저항" }), h("div", { class: "v", id: "mn-R", text: "—" })),
    h("div", {}, h("div", { class: "k", text: "전류" }), h("div", { class: "v", id: "mn-I", text: "—" })));
  const st = h("div", { class: "row" }, h("b", { class: "phase", id: "mn-phase", text: "대기" }), h("span", { class: "grow" }), h("span", { class: "sub nowrap", id: "mn-el", text: "" }),
    h("span", { class: "chip", id: "mn-heat", text: "히트건 —" }), h("span", { class: "chip", id: "mn-cool", text: "쿨러 —" }));
  const warns = (run && run.warns) || [];
  const why = live ? "" : mn.block_rt;
  const note = warns.length ? h("div", { class: "why one", "data-tip": warns.join(" / "), text: warns[warns.length - 1] + (warns.length > 1 ? "  (+" + (warns.length - 1) + ")" : "") })
    : why ? h("div", { class: "why one", "data-tip": why, text: "R–T 시작 불가 · " + why }) : null;
  const q = live ? "측정 중" : mn.block_quick;
  const row1 = h("div", { class: "row" }, btn("온도 1번", "mn-temp", null, "grow", "K2110 으로 온도를 한 번 읽습니다.", q), btn("저항 1번", "mn-res", null, "grow", "K2450 출력을 켜고 저항을 한 번 읽은 뒤 바로 끕니다.", q),
    btn("연속 측정", "mn-cont", null, "grow", "가열 없이 온도 · 저항을 [정지]까지 읽습니다(control.csv 만 저장).", live ? "측정 중" : mn.block_cont));
  const row2 = h("div", { class: "row" }, btn("R–T 시작", "mn-rt", null, "primary lg grow", "쿨러를 켜고 히트건으로 최고 온도까지 올린 뒤 끄고 냉각 끝 온도(또는 냉각 멈춤)까지 식히기를 반복합니다.", live ? "측정 중" : mn.block_rt),
    btn("정지", "mn-stop", null, "danger lg", "측정을 멈추고 히트건 → K2450 출력 → 쿨러 순으로 끕니다.", live ? "" : "측정 중 아님"));
  return [head, big, st, note, row1, row2].filter(Boolean);
}
function zoneOf(s) { return s.settings.cooler.zones; }
function cardManCfg(s) {
  const mn = s.manual, live = manLive(s), r = s.settings.rt, lock = live ? "측정 중 · 변경 잠김" : "";
  const path = UI.nasPath(r.file_path, r.date_folder, r.batch_folder);
  const cfg = h("div", { class: "card", id: "c-mancfg" }, h("div", { class: "chead" }, h("h3", { text: "조건" }), h("span", { class: "grow" }), btn("변경", "m-cond", null, "sm", "측정 탭과 같은 R–T 측정 조건(전압 · 온도 · 속도 · 저장)을 고칩니다.", lock)),
    h("div", { class: "sub", id: "mn-sum", text: rtSummary(s) + " · 냉각 끝 " + f1(r.break_temp, 0) + " °C · 비상 차단 " + f1(r.emergency_c, 0) + " °C" }),
    h("div", { class: "row" }, h("span", { class: "sub fl", text: "파일 이름" }), h("input", { type: "text", class: "grow", value: r.file_name, disabled: !!lock, "data-tip": "파일 이름의 앞부분입니다. 뒤에 전압과 회차 번호가 붙습니다.",
      onchange: async (e) => { const rr = await send("settings_save", { patch: { "rt.file_name": e.target.value } }); if (!rr.ok) e.target.value = r.file_name; } })),
    h("div", { class: "row" }, h("span", { class: "sub fl", text: "저장 위치" }), h("span", { class: "grow sub mono nasprev", "data-tip": "PC: 데이터 폴더의 manual 아래 · NAS: " + (path || "없음"), text: "PC manual" + (path ? " · NAS " + path : " · NAS 없음") })));
  /* 쿨러 · 히트건: 3×3 구역에서 고르거나 번호 입력 · 히트건은 표시만 */
  const z = zoneOf(s), on = new Set(mn.coolers), hn = mn.heater;
  const cells = (z.cells || []).map((c) => {
    const n = Number(c.relay), sel = on.has(n);
    return btn((c.name || ("쿨러 " + c.cooler)) + " · R" + n, "mn-zone", { n }, "sm" + (sel ? " on" : ""), "이 구역의 쿨러(R" + n + ")를 수동 측정 쿨러에 넣거나 뺍니다.", lock || (n === hn ? "히트건 릴레이" : ""));
  });
  const relayOpts = Array.from({ length: 10 }, (_, i) => i + 1);
  if (MN.relayN == null) MN.relayN = mn.coolers[0] || 2;
  const cool = h("div", { class: "card", id: "c-mancool" }, h("div", { class: "chead" }, h("h3", { text: "쿨러 · 히트건" }), h("span", { class: "grow" }),
    h("span", { class: "chip warn", "data-tip": "히트건 릴레이는 장비 설정 8 에서 바꿉니다.", text: "히트건 R" + hn })),
    h("div", { class: "zgrid mnz" }, ...cells),
    h("div", { class: "row" }, h("span", { class: "sub fl", text: "쿨러 번호" }), h("input", { type: "text", class: "grow mono", id: "mn-coolers", value: s.settings.manual.cooler_relays, disabled: !!lock,
      "data-tip": "켤 쿨러 릴레이 번호를 쉼표로 나눠 적습니다(예 2 · 2,3).",
      onchange: async (e) => { const rr = await send("settings_save", { patch: { "manual.cooler_relays": e.target.value.trim() } }); if (!rr.ok) e.target.value = s.settings.manual.cooler_relays; } })),
    h("div", { class: "row" }, h("span", { class: "sub fl", text: "켜 보기" }),
      h("select", { id: "mn-rln", class: "grow", disabled: !!lock, onchange: (e) => { MN.relayN = Number(e.target.value); } }, relayOpts.map((n) => h("option", { value: n, text: "R" + n + (n === hn ? " · 히트건" : on.has(n) ? " · 쿨러" : ""), selected: n === MN.relayN }))),
      btn("2 초 켜 보기", "mn-rltest", null, "sm", "고른 릴레이를 2 초만 켰다 끕니다. 쿨러 위치를 확인할 때 씁니다.", lock || (s.stage ? "" : "스테이지 연결 안 됨"))));
  /* 명령 직접 보내기(접힘): 조건 카드 아래 줄 */
  const sc = cfg;
  sc.append(h("div", { class: "row" }, h("b", { class: "grow", text: "명령 직접 보내기" }), btn(MN.scpi ? "접기 ▴" : "펼치기 ▾", "mn-scpi-toggle", null, "sm", "K2110 · K2450 에 SCPI 한 줄을 보냅니다(기록 · 점검용).")));
  if (MN.scpi) {
    const qwhy = live ? "측정 중" : mn.block_quick;
    sc.append(h("div", { class: "row" }, h("select", { id: "mn-dev", disabled: !!qwhy, onchange: (e) => { MN.dev = e.target.value; } }, [["k2110", "K2110"], ["k2450", "K2450"]].map(([v, l]) => h("option", { value: v, text: l, selected: v === MN.dev }))),
      h("input", { type: "text", class: "grow mono", id: "mn-cmd", placeholder: "예 *IDN? · SYST:ERR?", spellcheck: "false", disabled: !!qwhy, onkeydown: (e) => { if (e.key === "Enter") UI.acts["mn-send"](); } }),
      btn("보내기", "mn-send", null, "sm", "한 줄을 보냅니다. ? 로 끝나면 응답을 읽고, 보낸 뒤 오류 큐를 보여 줍니다. K2450 출력 켜기는 보내지 않습니다.", qwhy)),
      h("pre", { class: "mnout mono", id: "mn-out", text: MN.out.slice(-6).join("\n") || "응답이 여기에 보입니다." }));
  }
  return [cfg, cool];
}
function updateManDynamic(s) {
  const set = (id, t) => { const e = document.getElementById(id); if (e && e.textContent !== t) e.textContent = t; };
  const mn = s.manual, run = mn.run, live = manLive(s), last = mn.last || {};
  if (run && (live || run.status)) {
    set("mn-T", run.T == null ? "—" : f1(run.T, 1) + " °C"); set("mn-R", run.R == null ? "—" : fmtE(run.R) + " Ω"); set("mn-I", run.I == null ? "—" : fmtE(run.I) + " A");
    set("mn-phase", live ? manPhase(run) : run.status + (run.message && run.status !== "완료" && run.message !== run.status ? " · " + run.message : ""));
    set("mn-el", "경과 " + mmss(run.elapsed));
  } else {
    set("mn-T", last.T == null ? "—" : f1(last.T, 1) + " °C"); set("mn-R", last.R == null ? "—" : fmtE(last.R) + " Ω"); set("mn-I", last.I == null ? "—" : fmtE(last.I) + " A");
    set("mn-phase", "대기"); set("mn-el", "");
  }
  const hc = $("#mn-heat"), cc = $("#mn-cool"), on = mn.relays_on;
  if (hc && cc) {
    const heat = on != null && on.includes(mn.heater), cool = on == null ? [] : on.filter((n) => n !== mn.heater);
    const ht = on == null ? "히트건 —" : "히트건 " + (heat ? "켜짐" : "꺼짐"), ct = on == null ? "쿨러 —" : "쿨러 " + (cool.length ? "켜짐 R" + cool.join(",R") : "꺼짐");
    if (hc.textContent !== ht) { hc.textContent = ht; hc.className = "chip" + (heat ? " warn" : ""); }
    if (cc.textContent !== ct) { cc.textContent = ct; cc.className = "chip" + (cool.length ? " ok" : ""); }
  }
}
function renderManual(s) {
  if (UI.tab !== "manual" || !s.manual) return;
  buildManual();
  const mn = s.manual, run = mn.run;
  const sig = JSON.stringify([mn.block_rt, mn.block_cont, mn.block_quick, mn.coolers, mn.heater, run && [run.state, run.status, run.message, run.warns], s.rt && s.rt.inst && [s.rt.inst.k2110, s.rt.inst.k2450],
    s.settings.rt, s.settings.manual, s.settings.cooler, !!s.stage, MN.scpi, MN.out.length, MN.relayN]);
  if (sig !== MN.sig) {
    MN.sig = sig;
    MN.main.replaceChildren(...cardMan(s));
    MN.rest.replaceChildren(...cardManCfg(s));
  }
  updateManDynamic(s);
  if (!MN.drawn) { MN.drawn = true; redrawMan(); }
}
UI.renders.push(renderManual);

/* ---- 동작 ---- */
UI.acts["mn-temp"] = async () => { const r = await send("man_temp"); if (r.ok) toast("온도 " + f1(r.res.T, 2) + " °C"); };
UI.acts["mn-res"] = async () => { const r = await send("man_res"); if (r.ok) toast("저항 " + fmtE(r.res.R) + " Ω · 전류 " + fmtE(r.res.I) + " A" + (r.res.trip ? " · 한도 걸림" : "")); };
UI.acts["mn-cont"] = async () => { MN.pts = []; MN.n = 0; MN.id = ""; const r = await send("man_cont"); if (r.ok) toast("연속 측정 시작"); };
UI.acts["mn-rt"] = () => {
  const s = UI.state, mn = s.manual;
  dialog("수동 R–T 측정을 시작할까요?", "쿨러 R" + mn.coolers.join(",R") + " 를 켜고 히트건 R" + mn.heater + " 로 최고 " + f1(s.settings.rt.max_temp, 0) + " °C 까지 올립니다. 모터는 움직이지 않습니다. 열전대가 히트건 바람을 직접 맞지 않는지 확인하십시오.",
    [{ label: "취소" }, { label: "시작", cls: "primary", fn: async () => { MN.pts = []; MN.n = 0; MN.id = ""; const r = await send("man_rt"); if (r.ok) toast("수동 R–T 측정 시작"); } }]);
};
UI.acts["mn-stop"] = () => send("man_stop");
UI.acts["mn-zone"] = async (a) => {
  const cur = new Set(UI.state.manual.coolers);
  if (cur.has(a.n)) cur.delete(a.n); else cur.add(a.n);
  if (!cur.size) { toast("쿨러는 하나 이상", true); return; }
  await send("settings_save", { patch: { "manual.cooler_relays": Array.from(cur).sort((x, y) => x - y).join(",") } });
};
UI.acts["mn-rltest"] = () => {
  const n = Number(($("#mn-rln") || {}).value || MN.relayN), hn = UI.state.manual.heater;
  const go = async () => { const r = await send("relay_test", { n }); if (r.ok) toast("R" + n + " · 2 s 켰다 껐습니다"); };
  if (n === hn) dialog("히트건을 2 초 켤까요?", "R" + n + " 은 히트건입니다. 히트건 앞에 손이나 물건이 없는지 확인하십시오.", [{ label: "취소" }, { label: "2 초 켜기", cls: "danger", fn: go }]);
  else go();
};
UI.acts["mn-scpi-toggle"] = () => { MN.scpi = !MN.scpi; MN.sig = ""; render(); };
UI.acts["mn-send"] = async () => {
  const inp = $("#mn-cmd"), dev = ($("#mn-dev") || {}).value || MN.dev, cmd = inp ? inp.value.trim() : "";
  if (!cmd) return;
  const r = await send("man_scpi", { dev, line: cmd });
  const tag = dev.toUpperCase() + " > " + cmd;
  if (r.ok) MN.out.push(tag + (r.res.resp != null ? "\n  " + r.res.resp : "") + ((r.res.errs || []).length ? "\n  오류 큐: " + r.res.errs.join(" | ") : "\n  오류 없음"));
  else MN.out.push(tag + "\n  거절: " + (r.msg || ""));
  MN.out = MN.out.slice(-20);
  MN.sig = ""; render();
  const i2 = $("#mn-cmd"); if (i2) { i2.value = ""; }
};
