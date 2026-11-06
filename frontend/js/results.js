/* results.js - 결과 탭: 측정 제목 · 요약 · 결과 표 · 선택 샘플 상세(온도 곡선). */
"use strict";
const R = { name: null, data: null, sel: null, curve: null, sig: "" };

function parseCsv(t) {
  const rows = t.replace(/^﻿/, "").trim().split(/\r?\n/).map((l) => l.split(","));
  return rows;
}
async function loadRun(name) {
  const r = await fetch("/run/" + encodeURIComponent(name) + "/run.json");
  if (!r.ok) return null;
  return r.json();
}
async function loadCurve(name, no) {
  const r = await fetch("/run/" + encodeURIComponent(name) + "/sample_" + String(no).padStart(2, "0") + ".csv");
  if (!r.ok) return null;
  const rows = parseCsv(await r.text());
  const hdr = rows[0];
  const it = hdr.indexOf("온도 °C");
  return rows.slice(1).map((x) => [Number(x[0]), x[it] === "" ? null : Number(x[it])]).filter((p) => p[1] != null);
}

function renderResults(s) {
  const pane = $("#tab-results");
  if (UI.tab !== "results") return;
  const run = s.run;
  const list = s.results || [];
  const want = R.name || (run && run.dir ? run.dir.split(/[\\/]/).pop() : null) || (list[0] && list[0].name);
  const sig = JSON.stringify([want, R.sel, list.length, run && run.state, run && Object.keys(run.results).length]);
  if (sig === R.sig) return;
  R.sig = sig;
  if (!want) { pane.replaceChildren(h("div", { class: "card grow", text: "측정 결과 없음 · 측정을 하면 여기에 쌓입니다." })); return; }
  if (R.name !== want || !R.data) {
    R.name = want;
    loadRun(want).then((d) => { R.data = d; R.sig = ""; R.curve = null; render(); });
    if (!R.data) return;
  }
  const d = R.data;
  if (!d) return;
  const res = Object.values(d.results || {}).sort((a, b) => a.no - b.no);
  const sum = d.summary || {};
  const chans = (d.settings && d.settings.meter && d.settings.meter.channels || []).map((c) => c.name);
  const noMeter = !res.some((r) => Object.keys(r.values || {}).length);
  const dur = d.end && d.start ? d.end - d.start : null;
  const title = h("div", { class: "card" }, h("div", { class: "row" }, h("b", { class: "grow", text: new Date(d.start * 1000).toLocaleString("ko-KR") + " · " + (d.condition ? d.condition.name : "") + " · 소요 " + mmss(dur) }),
    h("span", { class: "chip ok", text: "완료 " + (sum["완료"] || 0) }), h("span", { class: "chip warn", text: "확인 필요 " + (sum["확인 필요"] || 0) }), h("span", { class: "chip err", text: "실패 " + ((sum["실패"] || 0) + (sum["오류"] || 0)) }),
    h("span", { class: "chip no", text: "측정 제외 " + (sum["측정 제외"] || 0) })));
  const th = ["번호", "X", "Y", "쿨러", "접촉 Z", "최고 온도", "가열", "냉각", ...chans.map((c) => "측정값 " + c), "결과"];
  const trs = [];
  res.forEach((r) => {
    trs.push(h("tr", { class: (r.no === R.sel ? "sel " : "") + (r.status === "확인 필요" ? "check" : ["실패", "오류"].includes(r.status) ? "fail" : ""), "data-act": "r-sel", "data-args": JSON.stringify({ no: r.no }) },
      [r.no, f1(r.X), f1(r.Y), r.cooler || "—", f1(r.contact_z, 2), f1(r.t_max), f1(r.heat_s, 0), f1(r.cool_s, 0), ...chans.map((c) => (r.values && r.values[c] != null ? r.values[c] : "—")), r.status].map((t) => h("td", { text: String(t) }))));
    if (r.reason) trs.push(h("tr", { class: "check" }, h("td", { colspan: th.length, class: "sub", text: r.no + "번 · " + r.reason })));
  });
  const tbl = h("table", { class: "tb" }, h("tr", {}, th.map((t) => h("th", { text: t }))), ...trs);
  const left = h("div", { class: "col", id: "res-left" }, title, h("div", { class: "card grow scroll" }, tbl),
    noMeter ? h("div", { class: "sub", text: "측정값 · 계측기 연결 후 표시" }) : null,
    h("div", { class: "row" }, btn("CSV 내보내기", "r-csv", null, "", "결과 표 CSV 가 있는 폴더를 엽니다."), btn("결과 폴더 열기", "r-open"), btn("다른 측정 보기", "r-other")));
  const r = res.find((x) => x.no === R.sel);
  let right;
  if (!r) right = h("div", { class: "card grow", text: "표에서 샘플을 고르면 상세가 보입니다." });
  else {
    const gbox = h("div", { class: "graph" });
    if (R.curve && R.curve.no === r.no) gbox.append(drawGraph(560, 300, { series: R.curve.rows, targets: tgOf(d), bands: [], t_now: null, ended: true }));
    else { gbox.append(h("div", { class: "sub", text: "곡선 불러오는 중" })); loadCurve(R.name, r.no).then((rows) => { R.curve = { no: r.no, rows: rows || [] }; R.sig = ""; render(); }); }
    const cell = (k, v) => h("div", { class: "stat" }, h("div", { class: "l", text: k }), h("div", { class: "num", text: v }));
    right = h("div", { class: "col" }, h("div", { class: "card" }, h("h3", { text: r.no + "번 샘플 · " + r.status })), gbox,
      h("div", { class: "stats" }, cell("접촉 Z", f1(r.contact_z, 2)), cell("최고 온도", f1(r.t_max)), cell("가열 s", f1(r.heat_s, 0)), cell("냉각 s", f1(r.cool_s, 0))),
      h("div", { class: "card" }, h("div", { text: "측정값 " + (Object.keys(r.values || {}).length ? Object.entries(r.values).map(([k, v]) => k + " " + v).join(" · ") : "—") }),
        h("div", { class: "sub3", text: "기록 " + (r.log || "—") })),
      h("div", { class: "row" }, btn("기록 파일 열기", "r-open"), btn("촬영 사진에서 보기", "r-photo", { no: r.no }), btn("이 샘플만 다시 측정", "r-redo", { no: r.no }, "", "같은 측정 폴더에 재측정 기록을 추가합니다.", s.run && ["running", "paused"].includes(s.run.state) ? "측정 중" : "")));
  }
  right.style.width = "600px";
  pane.replaceChildren(left, right);
  left.style.flex = "1";
}
function tgOf(d) {
  const t = {};
  ((d.condition && d.condition.steps) || []).forEach((st) => { if (st.type === "heat") t.heat = st.target_c; if (st.type === "cool") t.cool = st.target_c; if (st.type === "wait_temp") t.start = st.target_c; });
  return t;
}
UI.acts["r-sel"] = (a) => { R.sel = a.no; R.curve = null; R.sig = ""; render(); };
UI.acts["r-open"] = () => send("open_folder", { path: R.data && R.data.name ? null : null });
UI.acts["r-csv"] = () => send("open_folder", {});
UI.acts["r-photo"] = (a) => { M.mode = "shot"; send("sample_select", { no: a.no }); UI.tab = "measure"; render(); };
UI.acts["r-redo"] = (a) => dialog("이 샘플만 다시 측정할까요?", "같은 측정 폴더에 재측정 기록을 추가합니다. 이전 기록은 보존됩니다.", [{ label: "취소" }, { label: "다시 측정", cls: "primary", fn: () => send("measure_redo_sample", { no: a.no }) }]);
UI.acts["r-other"] = () => {
  const list = UI.state.results || [];
  const box = h("div", { class: "scroll" }, h("table", { class: "tb" }, h("tr", {}, ["측정", "상태", "샘플"].map((t) => h("th", { text: t }))),
    list.map((x) => h("tr", { "data-act": "r-pick", "data-args": JSON.stringify({ name: x.name }) }, h("td", { text: x.name }), h("td", { text: x.state }), h("td", { text: x.n })))));
  box.style.maxHeight = "560px";
  openWin(h("div", {}, h("h2", { text: "어느 측정을 볼까요?" }), box, h("div", { class: "btns" }, h("button", { class: "btn", text: "닫기", onclick: closeWin }))));
};
UI.acts["r-pick"] = (a) => { closeWin(); R.name = a.name; R.data = null; R.sel = null; R.sig = ""; render(); };
UI.renders.push(renderResults);
