/* results.js - 결과 탭 3칸: 왼쪽(230px) 측정 기록 · 가운데(늘어남) 그래프 + 표 · 오른쪽(270px) 저장한 파일 · NAS · 그날 측정 조건.
   기록은 디스크에서 읽는다(/api/results). 그래프는 측정 탭과 같은 부품이고 한 번에 3개까지 넣는다. */
"use strict";

const rs = { list: [], rec: null, id: null, picks: [], pts: {} };

function buildResults() {
  rs.listEl = h("div", { class: "reclist fill" });
  rs.samples = h("div", { class: "picklist" });
  const left = h("div", { class: "panel fill", id: "rs-left" }, h("div", { class: "row" }, h("h3", { text: "측정 기록" }), h("span", { class: "sp" }), btn("새로 읽기", loadResults, "sm")),
    rs.listEl, h("h4", { class: "h4", text: "샘플 (그래프에 3개까지)" }), rs.samples);
  rs.graph = rtGraph();
  rs.tbody = h("tbody", {});
  rs.save = busyBtn("그림으로 저장", saveResultPng);
  rs.saved = h("span", { class: "small nowrap grow" });
  const mid = h("div", { class: "panel fill", id: "rs-mid" },
    h("div", { class: "row" }, h("h3", { text: "온도별 저항" }), h("span", { class: "sp" }), rs.saved, rs.save),
    h("div", { class: "fill gwrap" }, rs.graph.el),
    h("div", { class: "tscroll rtable" }, h("table", { class: "tb" }, h("thead", {}, h("tr", {},
      h("th", { text: "샘플" }), h("th", { class: "num", text: "시작 온도 저항" }), h("th", { class: "num", text: "최고 온도 저항" }), h("th", { class: "num", text: "저항 비" }),
      h("th", { class: "num", text: "가열 전이 온도" }), h("th", { class: "num", text: "냉각 전이 온도" }), h("th", { class: "num", text: "차이" }))), rs.tbody)),
    h("div", { class: "small", text: "전이 온도 = log10(저항)을 온도로 미분한 값의 크기가 가장 큰 온도(가열 · 냉각 따로, 5점 이동 평균 뒤)" }));
  rs.files = h("div", { class: "grp" });
  rs.nas = h("div", { class: "grp" });
  rs.cond = h("div", { class: "grp" });
  const right = h("div", { class: "panel", id: "rs-right" }, h("h3", { text: "저장한 파일" }), rs.files, h("h3", { text: "NAS 복사" }), rs.nas, h("h3", { text: "그날 측정 조건" }), rs.cond);
  $("#tab-results").replaceChildren(left, mid, right);
  renderRecord();
}

async function loadResults(keep) {
  const j = await getJson("/api/results");
  rs.list = Array.isArray(j) ? j : [];
  renderRecList();
  if (keep !== true && rs.id == null && rs.list.length) pickRecord(rs.list[0].id);
}
function renderRecList() {
  rs.listEl.replaceChildren(...(rs.list.length ? rs.list.map((r) => h("div", { class: "rec" + (r.id === rs.id ? " on" : ""), onclick: () => pickRecord(r.id) },
    fmtWhen(r.started) + " · 묶음 " + r.batch + " · " + r.n + "개")) : [small("아직 측정 기록이 없습니다")]));
}
async function pickRecord(id) {
  rs.id = id; rs.rec = null; rs.picks = []; rs.pts = {};
  renderRecList();
  const j = await getJson("/api/results/record?id=" + encodeURIComponent(id));
  if (!j || j.ok === false || rs.id !== id) { if (rs.id === id) { rs.rec = null; renderRecord(); toast("측정 기록을 읽지 못했습니다"); } return; }
  rs.rec = j;
  const have = j.samples.filter((s) => s.n > 0);
  rs.picks = have.slice(0, 1).map((s) => s.id);                 // 처음에는 첫 샘플 하나
  renderRecord();
  syncResultPoints();
}
function openResult(recId) {                                    // 측정 탭 [결과 보기]: 방금 끝난 기록을 골라서 연다
  setTab("results");
  rs.id = null;
  loadResults(true).then(() => { if (recId && rs.list.some((r) => r.id === recId)) pickRecord(recId); else if (rs.list.length) pickRecord(rs.list[0].id); });
}
function togglePick(id) {
  const i = rs.picks.indexOf(id);
  if (i >= 0) rs.picks.splice(i, 1);
  else { rs.picks.push(id); if (rs.picks.length > 3) rs.picks.shift(); }     // 4번째를 고르면 가장 먼저 고른 것이 빠진다
  renderRecord(); syncResultPoints();
}
async function syncResultPoints() {
  const id = rs.id;
  for (const sid of rs.picks.slice()) {
    if (rs.pts[sid]) continue;
    const j = await getJson("/api/results/points?id=" + encodeURIComponent(id) + "&sample=" + sid);
    if (rs.id !== id) return;
    rs.pts[sid] = j && j.points ? j.points : [];
  }
  drawResultGraph();
}
function drawResultGraph() {
  const rec = rs.rec;
  if (!rec) { rs.graph.set([]); return; }
  rs.graph.set(rs.picks.map((sid) => { const s = rec.samples.find((x) => x.id === sid); return { name: s ? s.name : String(sid), points: rs.pts[sid] || [] }; }));
}
const COND_ROWS = [["meter_volt", "거는 전압", "V"], ["meter_four_wire", "4단자", ""], ["meter_avg_count", "평균", "회"], ["meter_nplc", "NPLC", ""], ["meter_ilim_a", "전류 한도", "A"],
  ["run_start_c", "시작 온도", "°C"], ["run_heater_off_c", "히트건 끄는 온도", "°C"], ["run_cool_end_c", "식힘 끝 온도", "°C"], ["run_repeat", "반복", "회"], ["run_interval_s", "읽는 간격", "s"],
  ["run_cooler_mode", "쿨러", ""], ["run_cooler_during_heat", "가열 중 쿨러", ""], ["contact_z_mm", "접촉 높이", "mm"]];
const COOLER_NAMES = { zone: "샘플 아래 구역만", all: "전부", none: "안 씀" };
function renderRecord() {
  const rec = rs.rec;
  rs.save.disabled = !rec || !rs.picks.length;
  if (!rec) {
    rs.samples.replaceChildren(small(rs.list.length ? "왼쪽에서 기록을 고르세요" : ""));
    rs.tbody.replaceChildren(); rs.files.replaceChildren(small("—")); rs.nas.replaceChildren(small("—")); rs.cond.replaceChildren(small("—"));
    rs.graph.set([]); rs.saved.textContent = "";
    return;
  }
  rs.samples.replaceChildren(...rec.samples.map((s) => {
    const c = h("input", { type: "checkbox", checked: rs.picks.includes(s.id), disabled: !s.n, onchange: () => togglePick(s.id) });
    return h("label", { class: "chk" }, c, s.name + (s.status !== "끝남" ? " · " + s.status : ""));
  }));
  const e2 = (v) => (v == null ? "—" : fmtOhm2(v)), t1 = (v) => (v == null ? "—" : v.toFixed(1) + " °C");
  rs.tbody.replaceChildren(...rec.samples.map((s) => {
    const a = s.sum || {};
    return h("tr", { class: rs.picks.includes(s.id) ? "sel" : "", onclick: () => { if (s.n) togglePick(s.id); } }, h("td", { text: s.name + (s.status !== "끝남" ? " (" + s.status + ")" : "") }),
      h("td", { class: "num", text: e2(a.r_start) }), h("td", { class: "num", text: e2(a.r_peak) }), h("td", { class: "num", text: a.ratio == null ? "—" : a.ratio >= 100 ? Math.round(a.ratio).toLocaleString() : a.ratio.toFixed(1) }),
      h("td", { class: "num", text: t1(a.t_heat) }), h("td", { class: "num", text: t1(a.t_cool) }), h("td", { class: "num", text: t1(a.hyst) }));
  }));
  rs.files.replaceChildren(h("div", { class: "small path", text: rec.folder }), h("div", { class: "flist" }, ...rec.files.map((f) => h("div", { text: f }))),
    h("div", { class: "row" }, btn("폴더 열기", () => api("result_open_folder", { id: rec.id }))));
  const nas = rec.nas;
  rs.nas.replaceChildren(...(!nas || !nas.dir ? [small("NAS 를 쓰지 않았습니다 (PC 에만 저장)")] : [
    h("div", { class: "small path", text: nas.dir }),
    h("div", {}, h("i", { class: "dot " + (nas.ok ? "ok" : "warn") }), " ", nas.ok ? "끝남" : "못 씀" + (nas.pending && nas.pending.length ? " · " + nas.pending.length + "개 남음" : "")),
    h("div", { class: "row" }, busyBtn("다시 복사", async () => { const r = await api("result_nas_copy", { id: rec.id }); if (r.ok) toast("NAS 로 " + r.result.copied + "개 복사했습니다"); pickRecordKeep(rec.id); }))]));
  const c = rec.cond || {};
  rs.cond.replaceChildren(h("div", { class: "kv" }, ...COND_ROWS.filter(([k]) => c[k] != null).flatMap(([k, name, unit]) => {
    let v = c[k];
    if (typeof v === "boolean") v = v ? "켬" : "끔";
    else if (k === "run_cooler_mode") v = COOLER_NAMES[v] || v;
    return [h("span", { class: "small", text: name }), h("span", { text: v + (unit ? " " + unit : "") })];
  })));
  drawResultGraph();
}
async function pickRecordKeep(id) {                             // 같은 기록을 다시 읽는다(고른 샘플은 그대로)
  const j = await getJson("/api/results/record?id=" + encodeURIComponent(id));
  if (j && j.ok !== false && rs.id === id) { rs.rec = j; renderRecord(); }
}
async function saveResultPng() {
  if (!rs.rec) return;
  const blob = await rs.graph.toPng();
  if (!blob) { toast("그림을 만들지 못했습니다"); return; }
  try {
    const r = await fetch("/api/results/png?id=" + encodeURIComponent(rs.rec.id), { method: "POST", headers: { "Content-Type": "image/png" }, body: blob });
    const j = await r.json();
    if (j.ok) { rs.saved.textContent = "저장함 · " + j.result.path; rs.saved.title = j.result.path; pickRecordKeep(rs.rec.id); } else toast(j.msg || "그림을 저장하지 못했습니다");
  } catch (e) { toast("그림을 저장하지 못했습니다"); }
}

TABS.results = { build: buildResults, render: () => {}, show: () => loadResults() };
