/* core.js - 연결 · 상태 · 도우미 · 창(show + 막) · 툴팁. 화면은 요청만 보내고 서버 state 로만 바뀐다. */
"use strict";
const UI = {
  state: null, tab: "measure", ws: null, nid: 0, pend: {}, log: [], connected: false, renders: [], acts: {}, local: {},
};

/* ---- 도우미 ---- */
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const k in (attrs || {})) {
    const v = attrs[k];
    if (v == null || v === false) continue;
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(c));
  return e;
}
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
function esc(t) { return String(t == null ? "" : t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function f1(v, d) { return v == null || isNaN(v) ? "—" : Number(v).toFixed(d == null ? 1 : d); }
function xy(x, y) { return x == null ? "—" : "X " + f1(x) + " · Y " + f1(y); }
function mmss(s) { if (s == null) return "—"; s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); }
function btn(label, act, args, cls, tip, disWhy) {
  const dis = !!disWhy;
  return h("button", { class: "btn " + (cls || "") + (dis ? " dis" : ""), "data-act": dis ? null : act, "data-args": args ? JSON.stringify(args) : null,
    "data-tip": dis ? disWhy : tip, text: label });
}

/* ---- 연결 ---- */
function connect() {
  const ws = new WebSocket("ws://" + location.host + "/ws");
  UI.ws = ws;
  ws.onopen = () => { UI.connected = true; $("#band") && render(); };
  ws.onclose = () => { UI.connected = false; render(); setTimeout(connect, 1000); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "state") { UI.state = m.state; if (m.log) UI.logTail = m.log; render(); }
    else if (m.type === "log") { UI.log.push(m.e); if (UI.log.length > 600) UI.log.shift(); UI.logTail = UI.log.slice(-3); renderLog(); }
    else if (m.type === "ack") {
      const p = UI.pend[m.id]; delete UI.pend[m.id];
      if (!m.ok) toast(m.msg, true);
      if (p) p(m);
    }
  };
}
function send(cmd, args) {
  return new Promise((res) => {
    if (!UI.ws || UI.ws.readyState !== 1) { toast("서버 연결 끊김", true); return res({ ok: false }); }
    const id = ++UI.nid;
    UI.pend[id] = res;
    UI.ws.send(JSON.stringify({ id, cmd, args: args || {} }));
  });
}
function estop() {
  try { if (UI.ws && UI.ws.readyState === 1) UI.ws.send(JSON.stringify({ id: ++UI.nid, cmd: "estop" })); } catch (e) { /* HTTP 로 간다 */ }
  fetch("/estop", { method: "POST" }).catch(() => {});
}

/* ---- 토스트 · 툴팁 · 창 ---- */
let toastT = null;
function toast(msg, err) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.add("hidden"), 3500);
}
document.addEventListener("mousemove", (e) => {
  const t = $("#tip");
  const el = e.target.closest && e.target.closest("[data-tip]");
  if (!el) { t.classList.add("hidden"); return; }
  t.textContent = el.getAttribute("data-tip");
  t.classList.remove("hidden");
  const sc = UI.scale || 1;
  t.style.left = Math.min(window.innerWidth - 400, e.clientX + 14) + "px"; t.style.top = (e.clientY + 18) + "px";
});

/* 창은 showModal 을 쓰지 않는다: 막(#dimmer) + 창(#win) 을 보이고, 비상정지 버튼(#head z-index)은 막 위에 남는다. */
function openWin(node, cls) {
  const w = $("#win");
  w.className = "win " + (cls || "");
  w.replaceChildren(node);
  $("#dimmer").classList.remove("hidden"); w.classList.remove("hidden"); $("#estopDock").classList.remove("hidden");
}
function closeWin() { $("#win").classList.add("hidden"); $("#dimmer").classList.add("hidden"); $("#estopDock").classList.add("hidden"); $("#win").replaceChildren(); }
/* 대화상자: 제목은 질문, 버튼은 동작 이름. buttons = [{label, cls, fn}] */
function dialog(title, body, buttons) {
  const bs = (buttons || [{ label: "닫기" }]).map((b) => h("button", { class: "btn " + (b.cls || ""), text: b.label, onclick: () => { closeWin(); if (b.fn) b.fn(); } }));
  openWin(h("div", {}, h("h2", { text: title }), h("div", { text: body }), h("div", { class: "btns" }, bs)));
}

/* ---- 스케일 ---- */
function fit() {
  const sc = Math.min(window.innerWidth / 1920, window.innerHeight / 1040);
  UI.scale = sc;
  const c = $("#canvas");
  c.style.transform = "scale(" + sc + ")";
  const dk = $("#estopDock");
  dk.style.right = 16 * sc + "px"; dk.style.top = 9 * sc + "px"; dk.style.width = 200 * sc + "px"; dk.style.height = 54 * sc + "px"; dk.style.fontSize = 20 * sc + "px";
  $("#win").style.setProperty("--sc", sc);
  const st = $("#stage");
  st.style.width = 1920 * sc + "px"; st.style.height = 1040 * sc + "px";
}
window.addEventListener("resize", fit);

/* ---- 클릭 위임 ---- */
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const act = el.getAttribute("data-act");
  const args = el.getAttribute("data-args") ? JSON.parse(el.getAttribute("data-args")) : {};
  const fn = UI.acts[act];
  if (fn) fn(args, el, e);
});
UI.acts.estop = () => estop();
UI.acts.tab = (a) => { UI.tab = a.tab; render(); };

/* ---- 렌더 ---- */
function render() {
  const s = UI.state;
  if (!s) return;
  renderHead(s);
  for (const t of ["measure", "results", "setup"]) $("#tab-" + t).classList.toggle("hidden", UI.tab !== t);
  $$("#tabs .tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === UI.tab));
  for (const fn of UI.renders) fn(s);
}
function renderLog() { for (const fn of UI.logRenders || []) fn(); }

const PILL = { "대기": "ok", "촬영 중": "busy", "이동 중": "busy", "일시정지": "warn", "중지 중": "warn", "비상정지": "err", "오류": "err", "연결 안 됨": "idle" };
function renderHead(s) {
  const p = $("#pill");
  const ph = UI.connected ? s.phase : "연결 안 됨";
  p.textContent = ph;
  p.className = "pill " + (ph.startsWith("측정 중") ? "busy" : (PILL[ph] || "idle"));
  const st = s.stage, c = s.calib, running = s.run && ["running", "paused", "stopping"].includes(s.run.state);
  const cells = [];
  const mk = (k, v, cls) => h("div", { class: "cell " + (cls || "") }, h("div", { class: "k", text: k }), h("div", { class: "v", text: v }));
  if (!running) {
    cells.push(mk("스테이지", !st ? "연결 안 됨" : "연결 · " + st.origin_text, !st ? "err" : (st.z_ok && st.xy_ok && !st.locked ? "ok" : "warn")));
    cells.push(mk("카메라", s.camera.open ? "연결 · " + (s.camera.size ? s.camera.size.join("×") : "") : "연결 안 됨", s.camera.open ? "ok" : "err"));
    cells.push(mk("좌표 보정", c.status === "done" ? "완료" : c.status === "invalid" ? c.text : "필요", c.status === "done" ? "ok" : c.status === "invalid" ? "err" : "warn"));
    cells.push(mk("온도", s.temp.c == null ? "센서 없음" : s.temp.c + " °C", s.temp.c == null ? "" : "ok"));
    cells.push(mk("계측기", s.meter.driver === "none" ? "미연결" : "연결", s.meter.driver === "none" ? "" : "ok"));
  } else {
    cells.push(mk("스테이지 위치", st ? xy(st.x, st.y) + " · Z " + f1(st.z) : "—", "ok"));
    cells.push(mk("히트건", s.relays && s.relays.heater_on ? "켜짐" : "꺼짐", s.relays && s.relays.heater_on ? "heat" : "ok"));
    const co = s.relays ? Object.keys(s.relays.owners).filter((n) => s.relays.owners[n] === "cooler").length : 0;
    cells.push(mk("쿨러", co ? "켜짐 " + co + "개" : "꺼짐", co ? "ok" : ""));
    cells.push(mk("온도", s.temp.c == null ? "센서 없음" : s.temp.c + " °C", "ok"));
    cells.push(mk("계측기", s.meter.driver === "none" ? "미연결 · 온도만 기록" : "연결", ""));
  }
  const box = $("#cells");
  box.replaceChildren(...cells);
  const band = $("#band");
  const al = s.alarm;
  if (al) {
    band.className = "band " + (/비상정지|오류|재부팅/.test(al.text) ? "err" : "");
    band.replaceChildren(h("span", { text: al.text }), h("span", { class: "next", text: al.next ? "· " + al.next : "" }));
  } else if (st && st.locked) {
    band.className = "band err";
    band.replaceChildren(h("span", { text: "비상정지 잠금" }), h("span", { class: "next", text: "· 회복: " + st.recovery.join(" → ") }));
  } else band.className = "band hidden";
}

/* 종료 요청(창 X): 화면이 확인을 받는다 */
window.requestExit = function () {
  dialog("프로그램을 종료할까요?", "스테이지 위치를 저장하고 히트건을 끈 뒤 종료합니다.", [
    { label: "계속 사용" },
    { label: "종료", cls: "danger", fn: async () => { const r = await send("shutdown"); if (!r.ok) toast(r.msg || "종료 거절", true); } },
  ]);
  return true;
};
window.addEventListener("load", () => { fit(); connect(); });
