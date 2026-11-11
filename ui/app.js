/* 샘플 자동 측정 v3 화면 - 빌드 없는 JS. 서버가 상태의 주인이고, 화면은 요청만 보낸다(바뀌는 것은 서버 상태가 와야 바뀐다). */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, "");
    else e.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(c));
  return e;
}

let S = null;                       // 서버 상태
let tab = "prep";
let wsOpen = false, lastMsg = 0;

/* ---------- 서버 요청 ---------- */
async function api(name, args) {
  try {
    const r = await fetch("/api/cmd/" + name, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(args || {}) });
    const j = await r.json();
    if (!j.ok && j.msg) toast(j.msg);                       // 거절 이유(서버가 준 한 문장)
    return j;
  } catch (e) {
    toast("서버에 보내지 못했습니다");
    return { ok: false, msg: "서버에 보내지 못했습니다" };
  }
}
async function putSettings(patch) {
  try {
    const r = await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
    const j = await r.json();
    if (!r.ok || j.ok === false) { toast(j.msg || "설정을 저장하지 못했습니다"); return false; }
    return true;
  } catch (e) { toast("설정을 저장하지 못했습니다"); return false; }
}

/* ---------- 비상정지: 어디서든 바로(WS 응답을 기다리지 않는다) ---------- */
let lastEstop = 0;
function estop() {
  const now = performance.now();
  if (now - lastEstop < 300) return;
  lastEstop = now;
  fetch("/api/estop", { method: "POST", keepalive: true }).catch(() => {});
}
$("#estop").addEventListener("pointerdown", (e) => { e.preventDefault(); estop(); });
$("#estop").addEventListener("keydown", (e) => { e.preventDefault(); });                 // Enter · Space 로 눌리지 않는다(Esc 가 비상정지)
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); estop(); } }, true);   // 입력 칸 안이어도

/* ---------- 알림(쌓이지 않는다) ---------- */
let toastT = null;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastT);
  toastT = setTimeout(() => t.classList.add("hidden"), 3000);
}

/* ---------- 탭 ---------- */
document.querySelectorAll("#tabs .tab").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
function setTab(t) {
  tab = t;
  document.querySelectorAll("#tabs .tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  for (const p of ["prep", "measure", "results", "manual"]) $("#tab-" + p).classList.toggle("hidden", p !== t);
  if (t === "manual") refreshPreview();
}

/* ---------- 표시 도우미 ---------- */
const f2 = (v) => (v == null ? "—" : Number(v).toFixed(2));
function fmtOhm(v) {
  if (v == null) return "—";
  const a = Math.abs(v);
  return a >= 1e6 ? (v / 1e6).toPrecision(4) + " MΩ" : a >= 1e3 ? (v / 1e3).toPrecision(4) + " kΩ" : v.toPrecision(4) + " Ω";
}
const DOT = { "연결": "ok", "연결 중": "warn", "오류": "err", "끊김": "off" };
const dotOf = (d) => DOT[d.state] || "off";
const devOk = (k) => S && S.devices[k].state === "연결";

/* ====================================================================
   아래 상태줄(장비 상태는 여기 한 곳) · 경보 띠
   ==================================================================== */
function renderStatus() {
  const s = S, c = s.cfg || {};
  const dev = (k, label) => h("span", { class: "item" }, h("i", { class: "dot " + dotOf(s.devices[k]) }), label);
  const mv = s.move;
  const pos = s.pos;
  let posEl;
  if (mv) {
    const t = mv.target || {};
    const pend = mv.pending ? " · 다음 " + mv.pending.axis.toUpperCase() + " " + (mv.pending.mm > 0 ? "+" : "") + mv.pending.mm + " mm" : "";
    posEl = h("span", { class: "item st-warn" }, "이동 중 → X " + f2(t.x) + " Y " + f2(t.y) + " Z " + f2(t.z) + pend);
  } else {
    posEl = h("span", { class: "item" }, "핀 X " + f2(pos.x) + " Y " + f2(pos.y) + " Z " + f2(pos.z) + " mm");
  }
  const heaterOn = s.heater_on_since != null;
  const hr = (c.heater_relay || 1);
  const cool = s.relays.map((r, i) => (r && i + 1 !== hr ? i + 1 : null)).filter(Boolean);
  const unknown = s.relays.every((r) => r == null);
  const port = s.devices.stage.target ? "(" + s.devices.stage.target + ")" : "";
  $("#status").replaceChildren(
    dev("stage", "스테이지" + port), dev("k2110", "온도계"), dev("k2450", "저항계"), dev("camera", "카메라"),
    posEl,
    h("span", { class: "item " + (heaterOn ? "st-warn" : "") }, "히트건 " + (heaterOn ? "켜짐" : "꺼짐")),
    h("span", { class: "item" }, "쿨러 " + (unknown ? "—" : cool.length ? "켜짐 " + cool.join("·") : "꺼짐")),
    h("span", { class: "safety" }, (c.heater_max_c != null ? c.heater_max_c : 110) + " °C 넘으면 히트건 끔 · 통신 " + ((c.watchdog_ms || 2000) / 1000) + "초 끊기면 릴레이 끔"));
}
let alarmSig = "";
function renderAlarms() {
  const sig = JSON.stringify(S.alarms.map((a) => [a.code, a.text, a.level]));
  if (sig === alarmSig) return;
  alarmSig = sig;
  $("#alarms").replaceChildren(...S.alarms.map((a) => h("div", { class: "alarm " + a.level }, h("span", { class: "grow", text: a.text }),
    (a.code === "estop" || a.code === "reboot") ? h("button", { class: "btn", type: "button", text: "비상정지 풀기", onclick: () => api("estop_release") }) : null)));
}

/* ====================================================================
   준비 탭 - 1 · 장비 연결
   ==================================================================== */
const STEPS = ["장비 연결", "원점 맞추기", "카메라 · 판 영역", "빈 판 사진", "좌표 맞추기", "접촉 높이 찾기", "쿨러 구역 확인"];
const DEVS = [
  { key: "stage", name: "스테이지", cfg: "stage_port", list: "dl-ports", ph: "COM 번호", conn: (v) => api("connect_stage", { port: v }), off: () => api("disconnect_stage") },
  { key: "k2110", name: "온도계 K2110", cfg: "k2110_addr", list: "dl-visa", ph: "VISA 주소", conn: (v) => api("connect_meter", { kind: "k2110", addr: v }), off: () => api("disconnect_meter", { kind: "k2110" }) },
  { key: "k2450", name: "저항계 K2450", cfg: "k2450_addr", list: "dl-visa", ph: "VISA 주소", conn: (v) => api("connect_meter", { kind: "k2450", addr: v }), off: () => api("disconnect_meter", { kind: "k2450" }) },
  { key: "camera", name: "카메라", cfg: "camera_index", list: "dl-cams", ph: "장치 번호", conn: (v) => api("connect_camera", { index: parseInt(v, 10) }), off: () => api("disconnect_camera") },
];
const prep = {};
function buildPrep() {
  const list = h("div", { class: "panel", id: "prep-list" }, h("h3", { text: "처음 한 번 준비" }),
    ...STEPS.map((t, i) => h("div", { class: "step" + (i === 0 ? " on" : ""), onclick: () => { if (i > 0) toast("다음 단계에서 만듭니다"); } }, h("span", { class: "no", text: i + 1 }), t)));
  const rows = DEVS.map((d) => {
    const input = h("input", { type: "text", list: d.list, placeholder: d.ph, spellcheck: "false", oninput: () => { input.dataset.dirty = "1"; } });
    const btn = h("button", { class: "btn", type: "button", text: "연결", onclick: async () => {
      btn.disabled = true;
      const r = await (S.devices[d.key].state === "연결" ? d.off() : d.conn(input.value.trim()));
      if (r.ok) input.dataset.dirty = "";
      btn.disabled = false;
    } });
    const stt = h("span", { class: "stt" }), resp = h("div", { class: "resp" });
    prep[d.key] = { input, btn, stt, resp };
    return h("div", { class: "devrow" }, h("div", { class: "name", text: d.name }), input, btn, h("div", { class: "grow" }, stt, resp));
  });
  const auto = h("input", { type: "checkbox", onchange: () => putSettings({ auto_connect: auto.checked }) });
  prep.auto = auto;
  const main = h("div", { class: "panel", id: "prep-main" }, h("h3", { text: "1 · 장비 연결" }),
    h("div", { class: "small", text: "포트를 열면 보드가 다시 시작합니다. COM4 는 예전 장비입니다. 사람이 고른 것 하나만 엽니다." }),
    h("datalist", { id: "dl-ports" }), h("datalist", { id: "dl-visa" }), h("datalist", { id: "dl-cams" }),
    h("div", {}, ...rows),
    h("div", { class: "row" }, h("button", { class: "btn", type: "button", text: "목록 새로 읽기", onclick: readLists }),
      h("label", { class: "chk" }, auto, "시작할 때 자동 연결")));
  $("#tab-prep").replaceChildren(list, main);
}
async function readLists() {
  const r = await api("devices");
  if (!r.ok) return;
  const d = r.result;
  $("#dl-ports").replaceChildren(...d.serial_ports.map((p) => h("option", { value: p })));
  $("#dl-visa").replaceChildren(...d.visa.map((a) => h("option", { value: a })));
  $("#dl-cams").replaceChildren(...d.cameras.map((n) => h("option", { value: String(n) })));
  if (d.note) toast(d.note);
}
function renderPrep() {
  const c = S.cfg || {};
  for (const d of DEVS) {
    const dev = S.devices[d.key], p = prep[d.key];
    const cls = { ok: "st-ok", warn: "st-warn", err: "st-err", off: "" }[dotOf(dev)];
    p.stt.replaceChildren(h("i", { class: "dot " + dotOf(dev) }), h("span", { class: cls, text: dev.state + (dev.state === "오류" && dev.why ? " · " + dev.why : "") }));
    p.resp.textContent = dev.state === "연결" ? dev.info : "";
    p.resp.title = dev.info || "";
    p.btn.textContent = dev.state === "연결" ? "끊기" : "연결";
    p.btn.disabled = dev.state === "연결 중";
    if (!p.input.dataset.dirty && document.activeElement !== p.input && c[d.cfg] != null) p.input.value = String(c[d.cfg]);
  }
  prep.auto.checked = !!c.auto_connect;
}

/* ====================================================================
   수동 탭
   ==================================================================== */
const man = { step: 1 };
const QUICK = { k2450: ["*IDN?", "SYST:ERR?", ":READ?"], k2110: ["*IDN?", "SYST:ERR?", ":READ?"], stage: ["st", "rs", "p"] };
function btn(text, fn, cls) { return h("button", { class: "btn " + (cls || ""), type: "button", text, onclick: fn }); }

function buildManual() {
  /* 왼쪽: 카메라 · 이동 */
  man.img = h("img", { alt: "" });
  man.prevBox = h("div", { id: "preview-box" }, h("span", { text: "카메라 연결 안 됨" }));
  const camPanel = h("div", { class: "panel", style: "flex:1" }, h("h3", { text: "카메라" }), man.prevBox,
    h("div", { class: "row" }, h("label", { class: "chk" }, h("input", { type: "radio", name: "view", checked: true }), "원본"),
      h("label", { class: "chk small" }, h("input", { type: "radio", name: "view", disabled: true }), "보정"),
      h("label", { class: "chk small" }, h("input", { type: "checkbox", disabled: true }), "판을 누르면 그 자리로 이동")),
    h("div", { class: "small", text: "보정 · 판 누르기는 준비 3 · 5단계 뒤에 쓸 수 있음" }));

  const stepBtns = [0.1, 1, 10].map((v) => btn(String(v), () => { man.step = v; man.custom.value = ""; man.syncStep(); }));
  man.custom = h("input", { type: "number", class: "num", min: "0", step: "any", placeholder: "직접", oninput: () => { const v = parseFloat(man.custom.value); if (v > 0) { man.step = v; man.syncStep(); } } });
  man.syncStep = () => stepBtns.forEach((b, i) => b.classList.toggle("on", !man.custom.value && man.step === [0.1, 1, 10][i]));
  man.syncStep();
  const jog = (axis, sign) => () => api("jog", { axis, mm: sign * man.step });
  const goInput = (ph) => h("input", { type: "number", class: "num", step: "any", placeholder: ph });
  man.gx = goInput("X"); man.gy = goInput("Y"); man.gz = goInput("Z");
  man.head = btn("사진 찍을 때 헤드 자리로", () => { const p = S.cfg.photo_head_xy; if (p) api("goto", { x: p[0], y: p[1] }); });
  man.headNote = h("span", { class: "small" });
  const movePanel = h("div", { class: "panel" }, h("h3", { text: "이동" }),
    h("div", { class: "steps" }, h("span", { class: "small", text: "한 번 누르면" }), ...stepBtns, man.custom, h("span", { class: "small", text: "mm" })),
    h("div", { class: "pad" }, btn("Y +", jog("y", 1), "u"), btn("X −", jog("x", -1), "l"), btn("X +", jog("x", 1), "r"), btn("Y −", jog("y", -1), "d")),
    h("div", { class: "row" }, btn("Z 위로", jog("z", -1)), btn("Z 아래로", jog("z", 1)), btn("핀 맨위로 (Z 0)", () => api("z_top"))),
    h("div", { class: "row" }, man.head, man.headNote),
    h("div", { class: "row" }, h("span", { class: "small", text: "바로 가기" }), "X", man.gx, "Y", man.gy, "Z", man.gz,
      btn("가기", () => {
        const x = parseFloat(man.gx.value), y = parseFloat(man.gy.value), z = man.gz.value === "" ? null : parseFloat(man.gz.value);
        if (isNaN(x) || isNaN(y)) { toast("X 와 Y 를 입력하십시오"); return; }
        api("goto", z == null || isNaN(z) ? { x, y } : { x, y, z });
      })));
  const left = h("div", { class: "col", id: "man-left" }, camPanel, movePanel);

  /* 가운데: 수동 측정 · 릴레이 시험 */
  man.temp = h("div", { class: "big", text: "—" });
  man.ohm = h("div", { class: "big", text: "—" });
  man.cond = h("div", { class: "small" });
  man.mon = btn("1초마다 계속 읽기", () => api("monitor", { on: !S.monitor }));
  const measure = h("div", { class: "panel" }, h("h3", { text: "수동 측정" }),
    h("div", { class: "reading" }, h("span", { class: "lbl", text: "온도 · K2110" }), man.temp, btn("한 번 읽기", () => api("read_temp"))),
    h("div", { class: "reading" }, h("span", { class: "lbl", text: "저항 · K2450" }), man.ohm, btn("한 번 읽기", () => api("read_resistance"))),
    h("div", { class: "row" }, man.cond, h("span", { class: "grow" }), btn("조건", openCond)),
    h("div", { class: "row" }, man.mon));
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
  man.line = h("input", { type: "text", class: "grow mono", spellcheck: "false", placeholder: "한 줄 입력", onkeydown: (e) => { if (e.key === "Enter") send(); } });
  man.log = h("div", { id: "cmd-log" });
  man.logN = 0;
  man.quick = () => man.quickBox.replaceChildren(...QUICK[man.target.value].map((q) => btn(q, () => { man.line.value = q; send(); })));
  man.quick();
  const cmd = h("div", { class: "panel", id: "man-right", style: "flex:none" }, h("h3", { text: "장비에 직접 명령" }), man.target, man.quickBox,
    h("div", { class: "row" }, man.line, btn("보내기", send)), man.log, h("div", { class: "small", text: "이동 · 원점 · 릴레이 켜기는 버튼으로 합니다" }));
  $("#tab-manual").replaceChildren(left, mid, cmd);
}
function logLine(cls, text) {
  const t = new Date();
  const hh = [t.getHours(), t.getMinutes(), t.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
  man.log.append(h("div", { class: cls, text: hh + " " + text }));
  while (man.log.childElementCount > 200) man.log.firstChild.remove();
  man.log.scrollTop = man.log.scrollHeight;
}
async function send() {
  const line = man.line.value.trim();
  if (!line) return;
  const target = man.target.value;
  logLine("tx", "→ " + line);
  man.line.value = "";
  const r = await api("raw", { target, line });
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
    h("div", { class: "form" }, "전압 (V)", volt, "4단자", four, "평균 횟수", avg, "NPLC", nplc, "전류 한도 (A)", ilim,
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
  man.head.disabled = !hp;
  man.headNote.textContent = hp ? "" : "준비 3단계에서 정함";
  const hr = c.heater_relay || 1;
  man.hLbl.textContent = "히트건 R" + hr;
  const grid = c.cooler_grid || [];
  const sig = JSON.stringify(grid);
  if (sig !== man.gridSig) {
    man.gridSig = sig;
    man.cells = {};
    man.grid.replaceChildren(...grid.flat().map((n) => { const b = btn("R" + n, () => api(S.relays[n - 1] ? "relay_off" : "relay_on", { n })); man.cells[n] = b; return b; }));
  }
  for (const [n, b] of Object.entries(man.cells || {})) {
    const on = !!S.relays[n - 1];
    b.classList.toggle("on", on);
    b.textContent = "R" + n + (on ? " 켜짐" : "");
  }
}
/* 미리보기: 1 초마다 새로 받는다(204 = 아직 프레임 없음) */
let prevUrl = null;
async function refreshPreview() {
  if (tab !== "manual" || !S) return;
  if (!devOk("camera")) { man.prevBox.replaceChildren(h("span", { text: "카메라 연결 안 됨" })); return; }
  try {
    const r = await fetch("/api/camera/preview.jpg?t=" + Date.now(), { cache: "no-store" });
    if (r.status !== 200) { man.prevBox.replaceChildren(h("span", { text: "미리보기 준비 중" })); return; }
    const url = URL.createObjectURL(await r.blob());
    man.img.src = url;
    if (!man.img.parentNode) man.prevBox.replaceChildren(man.img);
    if (prevUrl) URL.revokeObjectURL(prevUrl);
    prevUrl = url;
  } catch (e) { /* 다음 주기에 다시 */ }
}
setInterval(refreshPreview, 1000);

/* ====================================================================
   서버 연결 · 그리기
   ==================================================================== */
function render() {
  renderStatus(); renderAlarms(); renderPrep(); renderManual();
}
function showOffline(on) { $("#offline").classList.toggle("hidden", !on); }
function connectWS() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onopen = () => { wsOpen = true; };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type !== "state") return;
    S = m.state; lastMsg = Date.now(); wsOpen = true;
    showOffline(false);
    render();
  };
  ws.onclose = () => { wsOpen = false; showOffline(true); setTimeout(connectWS, 1000); };       // 자동으로 다시 잇는다
  ws.onerror = () => { try { ws.close(); } catch (e) { /* 이미 닫힘 */ } };
}
setInterval(() => { if (lastMsg && Date.now() - lastMsg > 4000) showOffline(true); }, 1000);       // 서버가 1 초마다 보낸다 - 4 초 조용하면 끊김

buildPrep();
buildManual();
connectWS();
fetch("/api/state").then((r) => r.json()).then((s) => { if (!S) { S = s; render(); readLists(); } }).catch(() => {});
