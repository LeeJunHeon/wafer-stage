/* results.js - 결과 탭 = 측정 기록: 측정 목록 · 결과 표(번호 · X · Y · 접촉 Z · 시작 · 끝 · 결과 · 사유) · [CSV 내보내기] [결과 폴더 열기].
   결과는 완료 / 오류 / 중지됨 / 측정 제외. 온도 · 측정값은 이 프로그램에 없다(별도 프로그램). */
"use strict";
const R = { name: null, data: null, sig: "" };

async function loadRun(name) {
  const r = await fetch("/run/" + encodeURIComponent(name) + "/run.json");
  if (!r.ok) return null;
  return r.json();
}

/* R–T 측정 기록: 시각 · File name · 회차 · 결과 · [그래프 보기] [폴더 열기] */
function kindBar(s, kind) {
  return h("div", { class: "msub" }, h("div", { class: "seg" }, btn("샘플 측정 기록", "r-kind", { k: "tour" }, kind === "tour" ? "on" : ""), btn("R–T 측정 기록", "r-kind", { k: "rt" }, kind === "rt" ? "on" : "")));
}
function renderRtResults(s) {
  const pane = $("#tab-results"), list = (s.rt && s.rt.results) || [];
  const sig = "rt" + JSON.stringify(list.map((x) => [x.dir, x.status, x.cycle]));
  if (sig === R.sig) return;
  R.sig = sig;
  const th = ["시각", "파일 이름", "회차", "결과", ""];
  const trs = list.map((x) => h("tr", { class: x.status === "오류" || x.status === "error" ? "fail" : x.status === "중지됨" ? "check" : "" },
    h("td", { text: x.time }), h("td", { text: (x.mode === "speed" ? "(속도 측정) " : "") + x.name }), h("td", { text: x.cycle }), h("td", { text: (x.status === "done" ? "완료" : x.status) + (x.message && x.status !== "완료" && x.status !== "done" ? " · " + x.message : "") }),
    h("td", {}, h("div", { class: "row" }, btn("그래프 보기", "r-graph", { dir: x.dir, name: x.name }, "sm", "회차별 R–T 그래프를 봅니다.", x.mode === "speed" || !(x.files || []).length ? "그래프 없음" : ""),
      btn("폴더 열기", "r-rt-open", { dir: x.dir }, "sm", "이 측정의 파일 폴더를 엽니다.")))));
  pane.replaceChildren(h("div", { class: "col grow" }, kindBar(s, "rt"), h("div", { class: "card grow scroll" }, list.length ? h("table", { class: "tb", id: "rt-table" }, h("tr", {}, th.map((t) => h("th", { text: t }))), ...trs) : h("div", { text: "R–T 측정 기록 없음 · 측정 탭의 '이 위치 측정' 에서 측정하면 여기에 쌓입니다." }))));
}
UI.acts["r-kind"] = (a) => { R.kind = a.k; R.sig = ""; render(); };
UI.acts["r-rt-open"] = (a) => send("open_folder", { measure: a.dir });
UI.acts["r-graph"] = async (a) => {
  const j = await (await fetch("/rt_data/" + encodeURIComponent(a.dir))).json();
  const svg = svgEl("svg", { class: "gsvg", id: "g-view" });
  const W = 1140, H = 620, pts = j.cycles.flatMap((c) => c.pts);
  const rg = UI.rtRange(pts.map((p) => p[0]), pts.map((p) => p[1]));
  const f = frame(svg, W, H, { l: 78, r: 190, t: 36, b: 52 }, rg.x, rg.y, { ylog: true, xlabel: "온도 [°C]", ylabel: "저항 [Ω]", xticks: 13 });
  const n = j.cycles.length;
  j.cycles.forEach((c, i) => {
    const hi = c.pts.reduce((m, p, k) => (p[0] > c.pts[m][0] ? k : m), 0);
    const col = (ph) => shade(ph ? C_HEAT : C_COOL, n <= 1 ? 1 : 0.15 + 0.85 * i / (n - 1));
    dots(f.g, c.pts.slice(0, hi + 1).map((p) => [f.px(p[0]), f.py(p[1])]), col(true), 2.6);
    dots(f.g, c.pts.slice(hi + 1).map((p) => [f.px(p[0]), f.py(p[1])]), col(false), 2.6);
  });
  svg.append(svgEl("text", { x: 78, y: 23, "font-size": 13, fill: "#3b434b" }, "가열(주황) · 냉각(파랑) · 회차가 늦을수록 진하게 · " + n + "회"));
  openWin(h("div", {}, h("h2", { text: "R–T 그래프 · " + a.name }), h("div", { class: "graph gview" }, svg), h("div", { class: "btns" }, h("button", { class: "btn", text: "닫기", onclick: closeWin }))), "big");
};

function renderResults(s) {
  const pane = $("#tab-results");
  if (UI.tab !== "results") return;
  const run = s.run;
  const list = s.results || [];
  if (R.kind == null) R.kind = (s.rt && s.rt.results && s.rt.results.length && !list.length) ? "rt" : "tour";
  if (R.kind === "rt") { renderRtResults(s); return; }
  const want = R.name || (run && run.dir ? run.dir.split(/[\\/]/).pop() : null) || (list[0] && list[0].name);
  const sig = JSON.stringify([want, list.length, run && run.state, run && Object.keys(run.results).length]);
  if (sig === R.sig) return;
  R.sig = sig;
  if (!want) { pane.replaceChildren(h("div", { class: "col grow" }, kindBar(s, "tour"), h("div", { class: "card grow", text: "측정 기록 없음 · 측정을 하면 여기에 쌓입니다." }))); return; }
  if (R.name !== want || !R.data) {
    R.name = want;
    loadRun(want).then((d) => { R.data = d; R.sig = ""; render(); });
    if (!R.data) return;
  }
  const d = R.data;
  if (!d) return;
  const res = Object.values(d.results || {}).sort((a, b) => a.no - b.no);
  const sum = d.summary || {};
  const dur = d.end && d.start ? d.end - d.start : null;
  const title = h("div", { class: "card ttlcard" }, h("div", { class: "row" }, h("b", { class: "grow", text: new Date(d.start * 1000).toLocaleString("ko-KR") + " · " + (d.condition ? d.condition.name : "") + " · 소요 " + mmss(dur) }),
    h("span", { class: "chip ok", text: "완료 " + (sum["완료"] || 0) }), h("span", { class: "chip err", text: "오류 " + (sum["오류"] || 0) }),
    h("span", { class: "chip warn", text: "중지됨 " + (sum["중지됨"] || 0) }), h("span", { class: "chip no", text: "측정 제외 " + (sum["측정 제외"] || 0) })));
  const th = ["번호", "X", "Y", "접촉 Z", "시작", "끝", "결과", "사유"];
  const trs = res.map((r) => h("tr", { class: r.status === "오류" ? "fail" : r.status === "중지됨" ? "check" : "" },
    [r.no, f1(r.X), f1(r.Y), f1(r.contact_z, 2), r.start || "—", r.end || "—", r.status, r.reason || ""].map((t) => h("td", { text: String(t) }))));
  const tbl = h("table", { class: "tb", id: "res-table" }, h("tr", {}, th.map((t) => h("th", { text: t }))), ...trs);
  const left = h("div", { class: "col grow", id: "res-left" }, kindBar(s, "tour"), title, h("div", { class: "card grow scroll" }, tbl),
    h("div", { class: "row" }, btn("CSV 내보내기", "r-csv", null, "primary", "결과 표를 CSV 파일로 저장합니다."), btn("결과 폴더 열기", "r-open", null, "", "이 측정의 결과 폴더를 엽니다."), btn("다른 측정 보기", "r-other")));
  pane.replaceChildren(left);
}
UI.acts["r-open"] = () => send("open_folder", { name: R.name });
UI.acts["r-csv"] = () => {
  const a = h("a", { href: "/run/" + encodeURIComponent(R.name) + "/samples.csv", download: R.name + ".csv" });
  document.body.append(a); a.click(); a.remove();
  toast("CSV 내보내기 · " + R.name + ".csv");
};
UI.acts["r-other"] = () => {
  const list = UI.state.results || [];
  const box = h("div", { class: "scroll" }, h("table", { class: "tb" }, h("tr", {}, ["측정", "상태", "샘플"].map((t) => h("th", { text: t }))),
    list.map((x) => h("tr", { "data-act": "r-pick", "data-args": JSON.stringify({ name: x.name }) }, h("td", { text: x.name }), h("td", { text: x.state }), h("td", { text: x.n })))));
  box.style.maxHeight = "560px";
  openWin(h("div", {}, h("h2", { text: "어느 측정을 볼까요?" }), box, h("div", { class: "btns" }, h("button", { class: "btn", text: "닫기", onclick: closeWin }))));
};
UI.acts["r-pick"] = (a) => { closeWin(); R.name = a.name; R.data = null; R.sig = ""; render(); };
UI.renders.push(renderResults);
