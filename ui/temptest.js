/* temptest.js - 온도 시험 창(수동 제어 탭 릴레이 시험 칸의 [온도 시험]). 샘플 · 저항계 · 모터 없이 히트건 · 쿨러의 온도 거동만 잰다.
   왼쪽: 조건(자동 측정과 같은 설정 값 - 가열 방식 · 가열 속도는 여기서 바로, 나머지는 [조건]) · 결과(도달 표 · 가열 · 냉각 · 예비 냉각).
   오른쪽: 진행 한 줄 · 그래프(가로 시간 s · 세로 온도 °C · 온도 실선 · 목표선 점선(ramp) · 아래 띠 = 열선 켜진 구간 · 이름표는 축 밖 오른쪽 + 지시선).
   창을 닫아도 시험은 서버에서 계속된다. 다시 열면 이어서 보인다(점은 /api/temptest/points 로 새 점만 받는다). */
"use strict";

const tt = { open: false, id: null, pts: [], busy: false, edit: false };
const TT_COLORS = { T: "#1F2329", sp: "#2A78D6", el: "#C03A7D", grid: "#ECEEF0", axis: "#C4CAD1", lead: "#9AA1A9", ink2: "#5B636D" };
/* [조건] 으로 고치는 값(자동 측정과 같은 설정 + 온도 시험 설정). 가열 방식 · 가열 속도는 위에서 바로 바꾼다 */
const TT_KEYS = [
  ["run_start_c", "시작 온도", "°C"], ["run_heater_off_c", "가열 끝 온도", "°C"], ["run_cool_end_c", "냉각 끝 온도", "°C"],
  ["heater_band_c", "목표 아래 허용 폭", "°C"], ["heater_min_on_s", "열선 최소 켜짐", "s"], ["heater_min_off_s", "열선 최소 꺼짐", "s"],
  ["run_heat_hold_s", "가열 끝 유지 시간", "s"], ["heater_lead_c", "목표선 기다림 폭", "°C"], ["run_fan_during_cool", "냉각 중 송풍", "bool"], ["run_stall_s", "냉각 정체 시간", "s"],
  ["run_stall_c", "냉각 정체 기준", "°C"], ["run_phase_limit_s", "한 단계 시간 한도", "s"], ["temp_test_coolers", "쿨러", "ints"],
  ["temp_test_precool", "예비 냉각", "bool"], ["temp_test_interval_s", "읽기 간격", "s"], ["temp_test_mark_c", "도달 표 간격", "°C"],
];

function ttActive() { return !!(S && S.temptest && S.temptest.active); }
/* 시작할 수 없는 이유(화면이 미리 알린다 - 서버도 거절한다) */
function ttWhy() {
  if (ttActive()) return "";
  if (running()) return "자동 측정 중 사용 불가";
  if (!devOk("stage")) return NEED.stage;
  if (!devOk("k2110")) return NEED.k2110;
  if (S.estop_locked) return "비상정지 해제 필요";
  const ht = S.heater || {};
  if (ht.fan || ht.element) return "히트건 끈 뒤 시작";
  if ((S.relays || []).some((r) => r === true)) return "쿨러 끈 뒤 시작";
  return "";
}
function ttFmt(v, d, unit) { return v == null ? "—" : Number(v).toFixed(d) + (unit || ""); }
function ttCoolers(list) { return list && list.length ? list.map((n) => "R" + n).join(" ") : "없음"; }

function openTempTest() {
  tt.mode = h("select", { onchange: () => putSettings({ run_heat_mode: tt.mode.value }) },
    h("option", { value: "ramp", text: "가열 속도 제어" }), h("option", { value: "full", text: "계속 가열" }));
  tt.rate = h("input", { type: "number", step: "0.1", class: "num", onchange: () => { const v = parseFloat(tt.rate.value); if (isFinite(v)) putSettings({ run_heat_rate_c_min: v }); } });
  tt.cmode = h("select", { onchange: () => putSettings({ run_cool_mode: tt.cmode.value }) },
    h("option", { value: "ramp", text: "냉각 속도 제어" }), h("option", { value: "free", text: "자연 냉각" }));
  tt.crate = h("input", { type: "number", step: "0.1", class: "num", onchange: () => { const v = parseFloat(tt.crate.value); if (isFinite(v)) putSettings({ run_cool_rate_c_min: v }); } });
  tt.expect = h("div", { class: "small" });
  tt.spacing = h("div", { class: "tt-sec" });
  tt.condView = h("div", { class: "tt-cond" });
  tt.condBtn = btn("조건", () => { tt.edit = !tt.edit; ttBuildCond(); });
  tt.prog = h("div", { class: "tt-prog" });
  tt.why = h("div", { class: "why hidden" });
  tt.marks = h("div", {});
  tt.heat = h("div", { class: "tt-sec" });
  tt.cool = h("div", { class: "tt-sec" });
  tt.pre = h("div", { class: "tt-sec" });
  tt.canvas = h("canvas", {});
  tt.box = h("div", { class: "tt-graph" }, tt.canvas);
  new ResizeObserver(ttDraw).observe(tt.box);
  const left = h("div", { class: "tt-left" },
    h("div", { class: "tt-sec" }, h("div", { class: "row" }, h("h4", { text: "조건" }), h("span", { class: "sp" }), tt.condBtn),
      h("div", { class: "tt-cond" }, "가열 방식", tt.mode, "가열 속도", h("span", { class: "row" }, tt.rate, small("°C/분")),
        "냉각 방식", tt.cmode, "냉각 속도", h("span", { class: "row" }, tt.crate, small("°C/분"))), tt.expect, tt.condView),
    h("div", { class: "tt-sec" }, h("h4", { text: "도달 표" }), tt.marks),
    h("div", { class: "tt-sec" }, h("h4", { text: "가열" }), tt.heat),
    h("div", { class: "tt-sec" }, h("h4", { text: "냉각" }), tt.cool),
    h("div", { class: "tt-sec" }, h("h4", { text: "점 간격" }), tt.spacing),
    h("div", { class: "tt-sec" }, h("h4", { text: "예비 냉각" }), tt.pre));
  const right = h("div", { class: "tt-right" }, tt.prog, tt.why, tt.box);
  tt.startBtn = btn("시작", () => api("temptest_start"), "on");
  tt.stopBtn = btn("정지", () => api("temptest_stop"));
  tt.folderBtn = btn("폴더 열기", () => api("temptest_open_folder"));
  tt.edit = false;
  tt.open = true;
  openDialog("온도 시험", h("div", { class: "tt" }, left, right), [tt.folderBtn, h("span", { class: "sp" }), tt.startBtn, tt.stopBtn, btn("닫기", () => { tt.open = false; closeDialog(); })],
    { cls: "tt-box", render: renderTempTest });
  tt.condSig = "";
  ttPoll();
}

/* 조건: 평소에는 값 한눈에 보기, [조건] 을 누르면 같은 자리에서 고친다(저장 = 설정 저장 - 자동 측정에도 그대로 쓰인다) */
function ttCondSrc() { return ttActive() && S.temptest.cond ? S.temptest.cond : S.cfg; }      // 시험 중에는 그 시험이 쓰는 값
function ttBuildCond() {
  const c = ttCondSrc();
  tt.condSig = JSON.stringify([tt.edit, TT_KEYS.map(([k]) => c[k])]);
  tt.condBtn.textContent = tt.edit ? "취소" : "조건";
  if (!tt.edit) {
    const show = (k, kind) => kind === "bool" ? (c[k] ? "켬" : "끔") : kind === "ints" ? ttCoolers(c[k]) : c[k] + " " + kind;
    setKids(tt.condView, ...TT_KEYS.flatMap(([k, label, kind]) => [small(label), h("span", { text: show(k, kind) })]));
    return;
  }
  const inputs = {};
  const kids = TT_KEYS.flatMap(([k, label, kind]) => {
    let el;
    if (kind === "bool") el = h("input", { type: "checkbox", checked: !!c[k] });
    else if (kind === "ints") el = h("input", { type: "text", class: "mono", value: JSON.stringify(c[k] || []) });
    else el = h("input", { type: "number", step: "any", value: c[k] });
    inputs[k] = [el, kind];
    return [small(label), kind === "bool" || kind === "ints" ? el : h("span", { class: "row" }, el, small(kind))];
  });
  const save = async () => {
    const patch = {};
    for (const [k, [el, kind]] of Object.entries(inputs)) {
      if (kind === "bool") patch[k] = el.checked;
      else if (kind === "ints") { try { patch[k] = JSON.parse(el.value); } catch (e) { toast("쿨러 형식 오류 · 예 [2, 3, 4]"); return; } }
      else { const v = parseFloat(el.value); if (!isFinite(v)) { toast(TT_KEYS.find((x) => x[0] === k)[1] + " · 숫자 필요"); return; } patch[k] = v; }
    }
    if (await putSettings(patch)) { tt.edit = false; ttBuildCond(); toast("조건 저장 완료"); }
  };
  setKids(tt.condView, ...kids, h("span", {}), h("span", { class: "row" }, btn("저장", save, "on")));
}

function renderTempTest() {
  if (!tt.open) return;
  const t = S.temptest || {}, act = !!t.active, c = ttCondSrc();
  if (t.id !== tt.id) { tt.id = t.id; tt.pts = []; ttDraw(); setTimeout(ttPoll, 0); }       // 새 시험
  /* 조건 */
  if (document.activeElement !== tt.mode) tt.mode.value = c.run_heat_mode;
  if (document.activeElement !== tt.rate) tt.rate.value = c.run_heat_rate_c_min;
  if (document.activeElement !== tt.cmode) tt.cmode.value = c.run_cool_mode;
  if (document.activeElement !== tt.crate) tt.crate.value = c.run_cool_rate_c_min;
  const lock = act || running();
  tt.mode.disabled = lock; tt.rate.disabled = lock || c.run_heat_mode !== "ramp"; tt.condBtn.disabled = lock;
  tt.cmode.disabled = lock; tt.crate.disabled = lock || c.run_cool_mode !== "ramp";
  tt.expect.textContent = expectText(c, 1);
  if (lock && tt.edit) tt.edit = false;
  const sig = JSON.stringify([tt.edit, TT_KEYS.map(([k]) => c[k])]);
  if (sig !== tt.condSig && !(tt.edit && tt.condView.contains(document.activeElement))) ttBuildCond();
  /* 진행 */
  const why = ttWhy();
  tt.startBtn.disabled = act || !!why;
  tt.stopBtn.disabled = !act;
  tt.folderBtn.disabled = !t.folder;
  setWhy(tt.why, act ? "" : why);
  if (act) {
    const ht = S.heater || {};
    setKids(tt.prog, h("b", { text: t.phase || "" }), " · " + fmtDur(t.t || 0) + " · 현재 " + ttFmt(t.T, 1, " °C") + (t.sp != null ? " · 목표 " + ttFmt(t.sp, 1, " °C") : ""),
      " · 열선 " + (ht.element ? "켜짐" : "꺼짐") + " · 송풍 " + (ht.fan ? "켜짐" : "꺼짐") + " · 쿨러 " + ttCoolers(t.coolers));
  } else if (t.result) {
    setKids(tt.prog, h("b", { text: "결과 · " + t.result }), t.notes && t.notes.length ? " · " + t.notes.join(" · ") : "");
  } else setKids(tt.prog, small("시작 전 · 조건 확인 후 [시작]"));
  /* 결과(진행 중에도 채운다) */
  const s = t.summary || {};
  const ms = s.marks || [];
  setKids(tt.marks, ms.length ? h("table", { class: "tb tt-marks" }, h("thead", {}, h("tr", {}, h("th", { text: "온도" }), h("th", { class: "num", text: "시간(s)" }))),
    h("tbody", {}, ...ms.map((m) => h("tr", {}, h("td", { text: m.c + " °C" + (m.top ? " · 목표" : "") }), h("td", { class: "num", text: ttFmt(m.s, 1) })))))
    : small(s.T0 != null ? "—" : "가열 시작 후 표시"));
  const hh = s.heat;
  setKids(tt.heat, ...(hh ? [
    h("div", { text: "목표 도달 " + ttFmt(hh.reach_s, 1, " s") + " · 평균 " + ttFmt(hh.rate_c_min, 2, " °C/분") }),
    h("div", { text: "열선 " + hh.on_n + "회 · 켜짐 " + ttFmt(hh.on_s, 0, " s") + " (" + (hh.on_ratio == null ? "—" : Math.round(hh.on_ratio * 100) + "%") + ")" }),
    h("div", { text: "열선 끈 뒤 최고 " + ttFmt(hh.peak_c, 1, " °C") + " · 넘침 " + (hh.over_c == null ? "—" : (hh.over_c >= 0 ? "+" : "") + hh.over_c.toFixed(1) + " °C") }),
    hh.rms_c != null ? h("div", { text: "목표선 차이 " + ttFmt(hh.rms_c, 2, " °C") + " (평균 제곱근) · 최대 " + ttFmt(hh.max_c, 2, " °C") }) : null,
  ] : [small("—")]));
  const cl = s.cool;
  setKids(tt.cool, ...(cl ? [h("div", { text: ttFmt(cl.from_c, 1) + " → " + ttFmt(cl.to_c, 1, " °C") + " · " + ttFmt(cl.s, 0, " s") + " · " + ttFmt(cl.rate_c_min, 2, " °C/분") + (cl.reached ? "" : " · 냉각 끝 온도 미도달") }),
    cl.rms_c != null ? h("div", { text: "목표선 차이 " + ttFmt(cl.rms_c, 2, " °C") + " (평균 제곱근) · 최대 " + ttFmt(cl.max_c, 2, " °C") }) : null] : [small("—")]));
  const sp = s.spacing;
  setKids(tt.spacing, ...(sp ? ["heat", "cool"].map((k) => h("div", { text: spacingText(sp, k) || (k === "heat" ? "가열" : "냉각") + " · —" })) : [small("—")]));
  const pre = s.pre;
  setKids(tt.pre, pre ? h("div", { text: (pre.stalled ? "정체 " : "도달 ") + ttFmt(pre.T, 1, " °C") + " · " + ttFmt(pre.s, 0, " s") }) : small(c.temp_test_precool ? "—" : "안 함"));
}

/* 점: 1 초마다 새 점만 받는다. 시험 id 가 바뀌면 처음부터 */
async function ttPoll() {
  if (!tt.open || tt.busy) return;
  tt.busy = true;
  try {
    const reqId = tt.id, from = tt.pts.length;
    const j = await getJson("/api/temptest/points?from=" + from);
    if (j && tt.open) {
      if (tt.id !== reqId || tt.pts.length !== from) setTimeout(ttPoll, 0);                              // 받는 사이에 새 시험으로 바뀌었다 - 버리고 다시
      else if (j.id !== reqId) { tt.id = j.id; tt.pts = []; ttDraw(); if (j.n) setTimeout(ttPoll, 0); }  // 새 시험: 처음부터 다시 받는다
      else if (j.points && j.points.length) { tt.pts.push(...j.points); ttDraw(); }
    }
  } finally { tt.busy = false; }
}
setInterval(ttPoll, 1000);

/* 그래프(canvas): 가로 경과(s) · 세로 온도(°C). 온도 실선 · 목표선 점선 · 아래 띠 = 열선 켜진 구간.
   이름표는 축 밖 오른쪽 띠에 "짧은 색 선 + 글자", 그 선의 마지막 점에서 회색 지시선으로만 잇는다(데이터 선과 겹치지 않게) */
function ttDraw() {
  if (!tt.open || !tt.canvas) return;
  const r = tt.box.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  const W = Math.floor(r.width), H = Math.floor(r.height);
  if (W < 50 || H < 50) return;
  tt.canvas.width = W * dpr; tt.canvas.height = H * dpr;
  const g = tt.canvas.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);
  g.font = "12px Pretendard, 'Malgun Gothic', sans-serif";
  const L = 48, R = 104, T = 8, B = 64;
  const x0 = L, x1 = W - R, y0 = T, y1 = H - B, by0 = y1 + 36, by1 = y1 + 52;
  const P = tt.pts, c = ttCondSrc();
  const tMax = Math.max(60, ...P.map((p) => p[0]));
  const vals = P.map((p) => p[1]).concat(P.filter((p) => p[2] != null).map((p) => p[2]), [c.run_heater_off_c, c.run_start_c]);
  const lo = Math.floor(Math.min(...vals) / 5) * 5, hi = Math.ceil(Math.max(...vals) / 5) * 5 + 5;
  const X = (t) => x0 + (x1 - x0) * t / tMax, Y = (v) => y1 - (y1 - y0) * (v - lo) / (hi - lo);
  const nice = (x) => { const e = Math.pow(10, Math.floor(Math.log10(x))); for (const m of [1, 2, 5, 10]) if (x <= m * e) return m * e; return 10 * e; };
  g.strokeStyle = TT_COLORS.grid; g.fillStyle = TT_COLORS.ink2; g.lineWidth = 1;
  const st = nice(tMax / 8);
  g.textAlign = "center";
  for (let k = 0; k <= tMax + 1e-9; k += st) { g.beginPath(); g.moveTo(X(k) + .5, y0); g.lineTo(X(k) + .5, y1); g.stroke(); g.fillText(String(Math.round(k)), X(k), y1 + 14); }
  g.fillText("경과 (s)", (x0 + x1) / 2, y1 + 28);
  const sc = nice((hi - lo) / 6);
  g.textAlign = "right";
  for (let v = Math.ceil(lo / sc) * sc; v <= hi + 1e-9; v += sc) { g.beginPath(); g.moveTo(x0, Y(v) + .5); g.lineTo(x1, Y(v) + .5); g.stroke(); g.fillText(String(v), x0 - 6, Y(v) + 4); }
  g.textAlign = "left";
  g.fillText("°C", 4, y0 + 10);
  g.strokeStyle = TT_COLORS.axis; g.strokeRect(x0 + .5, y0 + .5, x1 - x0, y1 - y0);
  /* 열선 띠 */
  g.strokeRect(x0 + .5, by0 + .5, x1 - x0, by1 - by0);
  g.fillStyle = TT_COLORS.el;
  for (let i = 0; i < P.length; i++) if (P[i][3]) { const a = X(P[i][0]), b = i + 1 < P.length ? X(P[i + 1][0]) : a + 1; g.fillRect(a, by0 + 1, Math.max(1, b - a), by1 - by0 - 1); }
  /* 목표선(점선) · 온도 */
  const sp = P.filter((p) => p[2] != null && p[5] !== 0);
  const line = (pts, yi, col, w, dash) => {
    if (pts.length < 2) return;
    g.strokeStyle = col; g.lineWidth = w; g.setLineDash(dash || []);
    g.beginPath(); pts.forEach((p, i) => (i ? g.lineTo(X(p[0]), Y(p[yi])) : g.moveTo(X(p[0]), Y(p[yi])))); g.stroke(); g.setLineDash([]);
  };
  /* 목표선은 이어진 구간마다 따로(가열 끝 → 냉각 시작 사이를 잇지 않는다 - 계속 가열의 목표는 가열 끝 온도 한 줄) */
  const segs = [];
  sp.forEach((p, i) => { if (!i || p[5] !== sp[i - 1][5] && !(p[5] === 2 && sp[i - 1][5] === 1)) segs.push([]); segs[segs.length - 1].push(p); });
  segs.forEach((sg) => line(sg, 2, TT_COLORS.sp, 1.5, [6, 4]));
  line(P, 1, TT_COLORS.T, 2);
  /* 이름표: 축 밖 오른쪽, 마지막 점에서 지시선 */
  const labels = [];
  if (P.length) labels.push({ t: "온도", col: TT_COLORS.T, x: X(P[P.length - 1][0]), y: Y(P[P.length - 1][1]) });
  if (sp.length) labels.push({ t: "목표", col: TT_COLORS.sp, x: X(sp[sp.length - 1][0]), y: Y(sp[sp.length - 1][2]) });
  labels.sort((a, b) => a.y - b.y);
  let prev = -1e9;
  for (const lb of labels) {
    const ly = Math.min(y1, Math.max(lb.y, prev + 16)); prev = ly;
    g.strokeStyle = TT_COLORS.lead; g.lineWidth = 1;
    g.beginPath(); g.moveTo(lb.x, lb.y); g.lineTo(x1 + 8, ly); g.stroke();
    g.strokeStyle = lb.col; g.lineWidth = 2; g.beginPath(); g.moveTo(x1 + 10, ly); g.lineTo(x1 + 22, ly); g.stroke();
    g.fillStyle = "#1A1D21"; g.fillText(lb.t, x1 + 26, ly + 4);
  }
  g.fillStyle = TT_COLORS.el; g.fillRect(x1 + 10, (by0 + by1) / 2 - 1, 12, 3);
  g.fillStyle = "#1A1D21"; g.fillText("열선", x1 + 26, (by0 + by1) / 2 + 4);
  if (!P.length) { g.fillStyle = TT_COLORS.ink2; g.textAlign = "center"; g.fillText("기록 없음", (x0 + x1) / 2, (y0 + y1) / 2); }
}
