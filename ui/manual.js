/* manual.js - 수동 탭: 왼쪽(카메라 · 이동) / 가운데(수동 측정 · 릴레이 시험) / 오른쪽(장비에 직접 명령). */
"use strict";

const man = {};
const QUICK = { k2450: ["*IDN?", "SYST:ERR?", ":READ?"], k2110: ["*IDN?", "SYST:ERR?", ":READ?"], stage: ["st", "rs", "p"] };

function buildManual() {
  /* 왼쪽: 카메라 · 이동 */
  man.pb = photoBox({ ar: 4 / 3, note: "카메라 연결 안 됨" });
  man.pb.box.id = "preview-box";
  man.viewRaw = h("input", { type: "radio", name: "view", checked: true });
  man.viewFix = h("input", { type: "radio", name: "view", disabled: true });
  man.camNote = h("div", { class: "why", text: "보정 보기 · 판 누르기는 준비 3 · 5단계 뒤에 쓸 수 있습니다" });
  const camPanel = h("div", { class: "panel", id: "man-cam" },
    h("div", { class: "row" }, h("h3", { text: "카메라" }), h("span", { class: "sp" }),
      h("label", { class: "chk" }, man.viewRaw, "원본"), h("label", { class: "chk" }, man.viewFix, "보정")),
    man.pb.box, man.camNote);

  man.jog = jogPart();
  const goInput = (ph) => h("input", { type: "number", class: "num", step: "any", placeholder: ph });
  man.gx = goInput("X"); man.gy = goInput("Y"); man.gz = goInput("Z");
  man.head = btn("사진 찍을 때 헤드 자리로", () => { const p = S.cfg.photo_head_xy; if (p) api("goto", { x: p[0], y: p[1] }); });
  man.headNote = h("span", { class: "why" });
  man.go = btn("가기", () => {
    const x = parseFloat(man.gx.value), y = parseFloat(man.gy.value), z = man.gz.value === "" ? null : parseFloat(man.gz.value);
    if (isNaN(x) || isNaN(y)) { toast("X 와 Y 를 입력하십시오"); return; }
    api("goto", z == null || isNaN(z) ? { x, y } : { x, y, z });
  });
  const movePanel = h("div", { class: "panel", id: "man-move" }, h("h3", { text: "이동" }), man.jog.el,
    h("div", { class: "row" }, man.head, man.headNote),
    h("div", { class: "row" }, small("바로 가기"), "X", man.gx, "Y", man.gy, "Z", man.gz, man.go));
  const left = h("div", { class: "col", id: "man-left" }, camPanel, movePanel);

  /* 가운데: 수동 측정 · 릴레이 시험 */
  man.temp = h("div", { class: "big", text: "—" });
  man.ohm = h("div", { class: "big", text: "—" });
  man.cond = h("div", { class: "small" });
  man.mon = btn("1초마다 계속 읽기", () => api("monitor", { on: !S.monitor }));
  man.readT = btn("한 번 읽기", () => api("read_temp"));
  man.readR = btn("한 번 읽기", () => api("read_resistance"));
  man.measWhy = h("div", { class: "why hidden" });
  const measure = h("div", { class: "panel" }, h("h3", { text: "수동 측정" }),
    h("div", { class: "reading" }, h("span", { class: "lbl", text: "온도 · K2110" }), man.temp, man.readT),
    h("div", { class: "reading" }, h("span", { class: "lbl", text: "저항 · K2450" }), man.ohm, man.readR),
    h("div", { class: "row" }, man.cond, h("span", { class: "sp" }), btn("조건", openCond)),
    h("div", { class: "row" }, man.mon), man.measWhy);
  man.hOn = btn("켜기", () => api("relay_on", { n: S.cfg.heater_relay }));
  man.hOff = btn("끄기", () => api("relay_off", { n: S.cfg.heater_relay }));
  man.hLbl = h("span", { class: "lbl small" });
  man.grid = h("div", { class: "relays" });
  man.gridSig = "";
  const relay = h("div", { class: "panel" }, h("h3", { text: "릴레이 시험" }),
    h("div", { class: "row" }, man.hLbl, man.hOn, man.hOff), h("div", { class: "small", text: "켜고 끄기를 자주 하지 마세요" }),
    man.grid,
    h("div", { class: "row" }, btn("모두 끄기", () => api("relays_off"))),
    h("div", { class: "small", text: "자리 배치는 준비 7단계에서 정합니다" }));
  const mid = h("div", { class: "col", id: "man-mid" }, measure, relay);

  /* 오른쪽: 장비에 직접 명령 */
  man.target = h("select", { onchange: () => { man.quick(); } }, h("option", { value: "k2450", text: "저항계 K2450" }), h("option", { value: "k2110", text: "온도계 K2110" }), h("option", { value: "stage", text: "스테이지" }));
  man.quickBox = h("div", { class: "quick" });
  man.line = h("input", { type: "text", class: "grow mono", spellcheck: "false", placeholder: "한 줄 입력", onkeydown: (e) => { if (e.key === "Enter") sendRaw(); } });
  man.log = h("div", { id: "cmd-log", class: "fill" });
  man.quick = () => man.quickBox.replaceChildren(...QUICK[man.target.value].map((q) => btn(q, () => { man.line.value = q; sendRaw(); })));
  man.quick();
  const cmd = h("div", { class: "panel", id: "man-right" }, h("h3", { text: "장비에 직접 명령" }), h("div", { class: "row" }, man.target), man.quickBox,
    h("div", { class: "row" }, man.line, btn("보내기", sendRaw)), man.log, h("div", { class: "small", text: "이동 · 원점 · 릴레이 켜기는 버튼으로 합니다" }));
  $("#tab-manual").replaceChildren(left, mid, cmd);
}
function logLine(cls, text) {
  const t = new Date();
  const hh = [t.getHours(), t.getMinutes(), t.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
  man.log.append(h("div", { class: cls, text: hh + " " + text }));
  while (man.log.childElementCount > 200) man.log.firstChild.remove();
  man.log.scrollTop = man.log.scrollHeight;
}
async function sendRaw() {
  const line = man.line.value.trim();
  if (!line) return;
  const target = man.target.value;
  logLine("tx", "→ " + line);
  man.line.value = "";
  const r = await api("raw", { target, line }, true);
  if (r.ok) (r.result.reply || "").split("\n").forEach((l) => l && logLine("rx", "← " + l));
  else logLine("er", "! " + r.msg);
}
function openCond() {
  const c = S.cfg;
  const volt = h("input", { type: "number", step: "0.01", value: c.meter_volt }), four = h("input", { type: "checkbox", checked: !!c.meter_four_wire }),
    avg = h("input", { type: "number", step: "1", value: c.meter_avg_count }), nplc = h("input", { type: "number", step: "0.01", value: c.meter_nplc }),
    ilim = h("input", { type: "number", step: "any", value: c.meter_ilim_a });
  const close = () => $("#dlg").classList.add("hidden");
  $("#dlg").replaceChildren(h("div", { class: "box" }, h("h3", { text: "저항 측정 조건" }),
    h("div", { class: "form" }, "거는 전압 (V)", volt, "4단자", four, "평균 횟수", avg, "NPLC", nplc, "전류 한도 (A)", ilim,
      h("div", { class: "row" }, btn("닫기", close), btn("저장", async () => {
        const ok = await putSettings({ meter_volt: parseFloat(volt.value), meter_four_wire: four.checked, meter_avg_count: parseInt(avg.value, 10), meter_nplc: parseFloat(nplc.value), meter_ilim_a: parseFloat(ilim.value) });
        if (ok) close();
      }, "on")))));
  $("#dlg").classList.remove("hidden");
}
function renderManual() {
  const c = S.cfg || {};
  man.temp.textContent = S.temp.value == null ? "—" : S.temp.value.toFixed(1) + " °C";
  man.ohm.textContent = fmtOhm(S.resistance.ohm);
  man.cond.textContent = (c.meter_volt != null ? Number(c.meter_volt).toFixed(2) : "—") + " V · " + (c.meter_four_wire ? "4단자" : "2단자") + " · 평균 " + c.meter_avg_count + " · NPLC " + c.meter_nplc;
  man.mon.classList.toggle("on", !!S.monitor);
  const hp = c.photo_head_xy;
  man.head.disabled = !hp || running();
  man.headNote.textContent = hp ? "" : "준비 3단계에서 정합니다";
  const hr = c.heater_relay || 1;
  man.hLbl.textContent = "히트건 R" + hr;
  const grid = c.cooler_grid || [];
  const sig = JSON.stringify(grid);
  if (sig !== man.gridSig) {
    man.gridSig = sig;
    man.cells = {};
    man.grid.replaceChildren(...grid.flat().map((n) => { const b = btn("R" + n, () => api(S.relays[n - 1] ? "relay_off" : "relay_on", { n })); man.cells[n] = b; return b; }));
  }
  const busy = running();
  for (const [n, b] of Object.entries(man.cells || {})) {
    const on = !!S.relays[n - 1];
    b.classList.toggle("on", on);
    b.textContent = "R" + n + (on ? " 켜짐" : "");
    b.disabled = busy;
  }
  for (const b of [man.hOn, man.readT, man.readR, man.mon, man.go]) b.disabled = busy;
  man.measWhy.textContent = busy ? "측정 중에는 수동 측정 · 릴레이 · 이동을 쓸 수 없습니다" : "";
  man.measWhy.classList.toggle("hidden", !busy);
  man.jog.update();
}
/* 미리보기: 1 초마다 새로 받는다(204 = 아직 프레임 없음) */
let prevUrl = null;
async function refreshPreview() {
  if (tab !== "manual" || !S) return;
  if (!devOk("camera")) { man.pb.setNote("카메라 연결 안 됨"); man.pb.show(""); return; }
  try {
    const r = await fetch("/api/camera/preview.jpg?t=" + Date.now(), { cache: "no-store" });
    if (r.status !== 200) { man.pb.setNote("미리보기 준비 중"); man.pb.show(""); return; }
    const url = URL.createObjectURL(await r.blob());
    man.pb.show(url);
    if (prevUrl) URL.revokeObjectURL(prevUrl);
    prevUrl = url;
  } catch (e) { /* 다음 주기에 다시 */ }
}
setInterval(refreshPreview, 1000);

TABS.manual = { build: buildManual, render: renderManual, show: refreshPreview };
