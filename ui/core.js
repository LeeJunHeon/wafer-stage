/* 샘플 자동 측정 v3 화면 - 빌드 없는 JS. 서버가 상태의 주인이고, 화면은 요청만 보낸다(바뀌는 것은 서버 상태가 와야 바뀐다).
   core.js: 도우미 · 서버 요청 · 비상정지 · 알림 · 탭 · 상태줄 · 경보 띠 · 사진 칸 · 서버 연결. 탭 화면은 TABS 에 등록한다. */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
function _fill(e, attrs, kids) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.setAttribute("class", v);
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, "");
    else e.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(c));
  return e;
}
function h(tag, attrs, ...kids) { return _fill(document.createElement(tag), attrs, kids); }
function svgEl(tag, attrs, ...kids) { return _fill(document.createElementNS("http://www.w3.org/2000/svg", tag), attrs, kids); }
function btn(text, fn, cls) { return h("button", { class: "btn " + (cls || ""), type: "button", text, onclick: fn }); }
function small(text) { return h("span", { class: "small", text }); }

let S = null;                       // 서버 상태
let tab = "prep";
let wsOpen = false, lastMsg = 0;
const TABS = {};                    // 이름 → {build(), render(), show()} (탭 파일이 등록한다)

/* ---------- 서버 요청 ---------- */
async function api(name, args, quiet) {
  try {
    const r = await fetch("/api/cmd/" + name, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(args || {}) });
    const j = await r.json();
    if (!j.ok && j.msg && !quiet) toast(j.msg);              // 거절 이유(서버가 준 한 문장)
    return j;
  } catch (e) {
    if (!quiet) toast("서버에 보내지 못했습니다");
    return { ok: false, msg: "서버에 보내지 못했습니다" };
  }
}
async function getJson(url) {
  try {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) return null;
    return await r.json();
  } catch (e) { return null; }
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
const TAB_NAMES = ["prep", "measure", "results", "manual"];
document.querySelectorAll("#tabs .tab").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
function setTab(t) {
  tab = t;
  document.querySelectorAll("#tabs .tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  for (const p of TAB_NAMES) $("#tab-" + p).classList.toggle("hidden", p !== t);
  const m = TABS[t];
  if (m && m.show) m.show();
  if (S) renderTab();
}

/* ---------- 표시 도우미 ---------- */
const f1 = (v) => (v == null ? "—" : Number(v).toFixed(1));
const f2 = (v) => (v == null ? "—" : Number(v).toFixed(2));
const f3 = (v) => (v == null ? "—" : Number(v).toFixed(3));
function fmtOhm(v) {
  if (v == null) return "—";
  const a = Math.abs(v);
  return a >= 1e6 ? (v / 1e6).toPrecision(4) + " MΩ" : a >= 1e3 ? (v / 1e3).toPrecision(4) + " kΩ" : v.toPrecision(4) + " Ω";
}
function fmtOhm2(v) {                                       // 짧게(표 · 한 줄 안내용)
  if (v == null) return "—";
  const a = Math.abs(v);
  return a >= 1e6 ? (v / 1e6).toPrecision(3) + " MΩ" : a >= 1e3 ? (v / 1e3).toPrecision(3) + " kΩ" : v.toPrecision(3) + " Ω";
}
function fmtDur(sec) {
  if (sec == null) return "—";
  const s = Math.max(0, Math.round(sec));
  return s >= 60 ? Math.floor(s / 60) + "분" + (s % 60 ? " " + (s % 60) + "초" : "") : s + "초";
}
function fmtWhen(epoch) {                                   // 11-11 13:20
  if (!epoch) return "";
  const d = new Date(epoch * 1000), p = (n) => String(n).padStart(2, "0");
  return p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}
function fmtClock(epoch) {                                  // 13:47
  if (!epoch) return "";
  const d = new Date(epoch * 1000), p = (n) => String(n).padStart(2, "0");
  return p(d.getHours()) + ":" + p(d.getMinutes());
}
const DOT = { "연결": "ok", "연결 중": "warn", "오류": "err", "끊김": "off" };
const dotOf = (d) => DOT[d.state] || "off";
const devOk = (k) => S && S.devices[k].state === "연결";
const running = () => !!(S && S.run && S.run.active);

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
    s.fake ? h("span", { class: "item st-warn" }, "가짜 장비") : null,
    h("span", { class: "safety" }, (c.heater_max_c != null ? c.heater_max_c : 110) + " °C 넘으면 히트건 끔 · 통신 " + ((c.watchdog_ms || 2000) / 1000) + "초 끊기면 릴레이 끔"));
}
let alarmSig = "";
function renderAlarms() {
  const sig = JSON.stringify(S.alarms.map((a) => [a.code, a.text, a.level]));
  if (sig === alarmSig) return;
  alarmSig = sig;
  $("#alarms").replaceChildren(...S.alarms.map((a) => h("div", { class: "alarm " + a.level }, h("span", { class: "grow", text: a.text }),
    (a.code === "estop" || a.code === "reboot") ? btn("비상정지 풀기", () => api("estop_release"), "sm")
      : a.dismiss ? btn("지우기", () => api("alarm_clear", { code: a.code }), "sm") : null)));
}

/* ====================================================================
   사진 칸: 비율(ar = 가로 ÷ 세로)을 지켜 칸 안에 맞춘다. 위 · 왼쪽부터 채우고 남는 자리는 패널 바탕색.
   svg 덧그림은 사진과 같은 크기(좌표는 쓰는 쪽이 viewBox 로 정한다). toFrac(ev) = 사진 안 위치(0~1).
   ==================================================================== */
function photoBox(opts) {
  opts = opts || {};
  const img = h("img", { alt: "", draggable: "false" });
  const svg = svgEl("svg", { preserveAspectRatio: "none" });
  const inner = h("div", { class: "photo-in hidden" }, img, svg);
  const note = h("div", { class: "photo-note", text: opts.note || "" });
  const box = h("div", { class: "photo-box " + (opts.cls || "") }, inner, note);
  const pb = { box, inner, img, svg, note, ar: opts.ar || 1, w: 0, h: 0, url: "" };
  pb.fit = () => {
    const cw = box.clientWidth, ch = box.clientHeight;
    if (!cw || !ch) return;
    const w = Math.floor(Math.min(cw, ch * pb.ar)), hh = Math.floor(w / pb.ar);
    if (w === pb.w && hh === pb.h) return;
    pb.w = w; pb.h = hh;
    inner.style.width = w + "px"; inner.style.height = hh + "px";
    if (opts.onfit) opts.onfit(pb);
  };
  new ResizeObserver(pb.fit).observe(box);
  pb.setAr = (ar) => { if (ar > 0 && Math.abs(ar - pb.ar) > 1e-6) { pb.ar = ar; pb.w = 0; pb.fit(); } };
  pb.show = (url) => {                                     // url 이 없으면 안내 글만
    if (url) { if (url !== pb.url) { pb.url = url; img.src = url; } inner.classList.remove("hidden"); note.classList.add("hidden"); pb.fit(); }
    else { pb.url = ""; inner.classList.add("hidden"); note.classList.remove("hidden"); }
  };
  pb.setNote = (t) => { note.textContent = t; };
  pb.toFrac = (ev) => { const r = inner.getBoundingClientRect(); return [(ev.clientX - r.left) / Math.max(1, r.width), (ev.clientY - r.top) / Math.max(1, r.height)]; };
  return pb;
}

/* ====================================================================
   서버 연결 · 그리기
   ==================================================================== */
function renderTab() {
  const m = TABS[tab];
  if (!m || !m.render) return;
  try { m.render(); } catch (e) { console.error(e); toast("화면을 그리지 못했습니다 · " + String(e && e.message ? e.message : e).slice(0, 60)); }
}
function render() {
  renderStatus(); renderAlarms(); renderTab();
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
