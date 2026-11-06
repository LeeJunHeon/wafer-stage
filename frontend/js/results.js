/* results.js - 결과 탭 = 측정 기록: 측정 목록 · 결과 표(번호 · X · Y · 접촉 Z · 시작 · 끝 · 결과 · 사유) · [CSV 내보내기] [결과 폴더 열기].
   결과는 완료 / 오류 / 중지됨 / 측정 제외. 온도 · 측정값은 이 프로그램에 없다(별도 프로그램). */
"use strict";
const R = { name: null, data: null, sig: "" };

async function loadRun(name) {
  const r = await fetch("/run/" + encodeURIComponent(name) + "/run.json");
  if (!r.ok) return null;
  return r.json();
}

function renderResults(s) {
  const pane = $("#tab-results");
  if (UI.tab !== "results") return;
  const run = s.run;
  const list = s.results || [];
  const want = R.name || (run && run.dir ? run.dir.split(/[\\/]/).pop() : null) || (list[0] && list[0].name);
  const sig = JSON.stringify([want, list.length, run && run.state, run && Object.keys(run.results).length]);
  if (sig === R.sig) return;
  R.sig = sig;
  if (!want) { pane.replaceChildren(h("div", { class: "card grow", text: "측정 기록 없음 · 측정을 하면 여기에 쌓입니다." })); return; }
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
  const title = h("div", { class: "card" }, h("div", { class: "row" }, h("b", { class: "grow", text: new Date(d.start * 1000).toLocaleString("ko-KR") + " · " + (d.condition ? d.condition.name : "") + " · 소요 " + mmss(dur) }),
    h("span", { class: "chip ok", text: "완료 " + (sum["완료"] || 0) }), h("span", { class: "chip err", text: "오류 " + (sum["오류"] || 0) }),
    h("span", { class: "chip warn", text: "중지됨 " + (sum["중지됨"] || 0) }), h("span", { class: "chip no", text: "측정 제외 " + (sum["측정 제외"] || 0) })));
  const th = ["번호", "X", "Y", "접촉 Z", "시작", "끝", "결과", "사유"];
  const trs = res.map((r) => h("tr", { class: r.status === "오류" ? "fail" : r.status === "중지됨" ? "check" : "" },
    [r.no, f1(r.X), f1(r.Y), f1(r.contact_z, 2), r.start || "—", r.end || "—", r.status, r.reason || ""].map((t) => h("td", { text: String(t) }))));
  const tbl = h("table", { class: "tb", id: "res-table" }, h("tr", {}, th.map((t) => h("th", { text: t }))), ...trs);
  const left = h("div", { class: "col grow", id: "res-left" }, title, h("div", { class: "card grow scroll" }, tbl),
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
