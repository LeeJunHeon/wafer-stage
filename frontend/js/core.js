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
  ws.onopen = () => { UI.connected = true; UI.previewOn = null; $("#band") && render(); };
  ws.onclose = () => {
    UI.connected = false;
    const pend = UI.pend; UI.pend = {};                                      // 대기 중인 명령은 모두 실패로 끝낸다(ack 가 영영 안 온다)
    Object.keys(pend).forEach((k) => { try { pend[k]({ ok: false, msg: "서버 연결 끊김" }); } catch (e) { /* 무시 */ } });
    if (typeof jogReset === "function") jogReset();
    render(); setTimeout(connect, 1000);
  };
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
  if (typeof jogOnEstop === "function") jogOnEstop();                      // 조그를 먼저 멈춘다(눌려 있던 키 · 버튼은 뗄 때까지 다시 시작하지 않는다)
  UI.estopT = performance.now();
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
  t.style.left = Math.max(4, Math.min(window.innerWidth - t.offsetWidth - 8, e.clientX + 14)) + "px"; t.style.top = Math.max(4, Math.min(window.innerHeight - t.offsetHeight - 8, e.clientY + 18)) + "px";
});

/* 창은 showModal 을 쓰지 않는다: 막(#dimmer) + 창(#win) 을 보이고, 비상정지 버튼(#head z-index)은 막 위에 남는다. */
function openWin(node, cls) {
  const w = $("#win");
  w.className = "win " + (cls || "");
  w.replaceChildren(node);
  $("#tip").classList.add("hidden");                                      // 창이 열리면 설명 풍선은 사라진다
  if (typeof jogKbdOff === "function") jogKbdOff();                       // 창이 열리면 키보드 조그는 꺼진다
  $("#dimmer").classList.remove("hidden"); w.classList.remove("hidden"); $("#estopDock").classList.remove("hidden");
}
function closeWin() { $("#tip").classList.add("hidden"); $("#win").classList.add("hidden"); $("#dimmer").classList.add("hidden"); $("#estopDock").classList.add("hidden"); $("#win").replaceChildren(); }
/* 대화상자: 제목은 질문, 버튼은 동작 이름. buttons = [{label, cls, fn}] */
function dialog(title, body, buttons) {
  const bs = (buttons || [{ label: "닫기" }]).map((b) => h("button", { class: "btn " + (b.cls || ""), text: b.label, onclick: () => { closeWin(); if (b.fn) b.fn(); } }));
  openWin(h("div", {}, h("h2", { text: title }), h("div", { text: body }), h("div", { class: "btns" }, bs)));
}

/* "⋯" 보조 동작 메뉴: items = [{label, fn, why}] (why 가 있으면 비활성 + 사유 툴팁) */
function popMenu(anchor, items) {
  closePop();
  const r = anchor.getBoundingClientRect(), sc = UI.scale || 1;
  const m = h("div", { class: "pop", id: "pop" }, items.map((it) => h("button", { class: "btn sm block" + (it.why ? " dis" : ""), text: it.label, "data-tip": it.why || null,
    onclick: () => { if (it.why) return; closePop(); it.fn(); } })));
  m.style.left = Math.min(window.innerWidth - 240, r.left) + "px"; m.style.top = r.bottom + 4 + "px";
  $("#stage").append(m);
  setTimeout(() => document.addEventListener("click", closePop, { once: true }), 0);
}
function closePop() { const p = $("#pop"); if (p) p.remove(); }
function moreBtn(items) { const b = h("button", { class: "btn sm", text: "⋯", "data-tip": "더 보기" }); b.addEventListener("click", (e) => { e.stopPropagation(); popMenu(b, items); }); return b; }

/* ---- 스케일 ---- */
function fit() { UI.scale = 1; }                  /* 창 크기에 맞춰 배치가 늘고 줄며 글자는 줄이지 않는다(좌표 변환용 UI.scale 은 항상 1) */
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
UI.acts.estop = () => { if (UI.estopT && performance.now() - UI.estopT < 800) return; estop(); };     // 누르는 순간(pointerdown)에 이미 보냈으면 click 은 다시 보내지 않는다
document.addEventListener("pointerdown", (e) => {                           // 비상정지는 누르는 순간 보낸다(클릭이 끝나기를 기다리지 않는다)
  if (e.button !== undefined && e.button !== 0) return;
  if (e.target.closest && e.target.closest("[data-act=\"estop\"]")) estop();
}, true);
UI.acts.tab = (a) => { UI.tab = a.tab; $("#tip").classList.add("hidden"); if (typeof jogKbdOff === "function") jogKbdOff(); render(); };

/* ---- 렌더 ---- */
function render() {
  const s = UI.state;
  if (!s) return;
  renderHead(s);
  for (const t of ["measure", "results", "setup"]) $("#tab-" + t).classList.toggle("hidden", UI.tab !== t);
  $$("#tabs .tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === UI.tab));
  for (const fn of UI.renders) fn(s);
  syncPreview(s);
}
/* 실시간 화면이 필요할 때만 서버가 미리보기를 만든다(측정 탭 실시간 · 장비 설정의 실시간 단계) */
function syncPreview(s) {
  let want = false;
  try { want = (UI.tab === "measure" && typeof M !== "undefined" && M.mode === "live" && !isRunning(s)) || (UI.tab === "setup" && typeof viewFor === "function" && viewFor(s) === "live"); } catch (e) { want = false; }
  if (want !== UI.previewOn && UI.ws && UI.ws.readyState === 1) { UI.previewOn = want; UI.ws.send(JSON.stringify({ cmd: "preview", on: want })); }
}
function renderLog() { for (const fn of UI.logRenders || []) fn(); }

/* 상태 필은 동작 상태만 보인다(연결 문제는 상태 칸과 띠가 맡는다) */
const PILL = { "대기": "idle", "촬영 중": "busy", "이동 중": "busy", "일시정지": "warn", "확인 대기": "warn", "중지 중": "warn", "비상정지": "err", "오류": "err", "R–T 측정 중": "busy", "속도 측정 중": "busy", "서버 끊김": "err" };
/* 장치 상태 → 상태 칸 문구 · 색 */
const DEV_CLS = { ok: "ok", down: "warn", retry: "warn", unknown: "" };
function devText(d, okText) { return !d ? "확인 안 됨" : d.state === "ok" ? okText : d.state === "down" ? "연결 안 됨" : d.state === "retry" ? "다시 연결 중" : "확인 안 됨"; }
function relayText(s) {
  const rl = s.relays || { owners: {}, heater_on: false };
  const co = Object.keys(rl.owners).filter((n) => Number(n) !== Number(rl.heater) && rl.owners[n]).length;
  return { co, heat: !!rl.heater_on };
}
function renderHead(s) {
  const p = $("#pill");
  const ph = UI.connected ? s.phase : "서버 끊김";
  p.textContent = ph;
  p.className = "pill " + (ph.startsWith("측정 중") ? "busy" : (PILL[ph] || "idle"));
  const st = s.stage, c = s.calib, running = s.run && ["running", "paused", "stopping"].includes(s.run.state);
  const cells = [];
  const mk = (k, v, cls) => h("div", { class: "cell " + (cls || ""), "data-tip": k + " · " + v }, h("div", { class: "k", text: k }), h("div", { class: "v", text: v }));
  const rt = relayText(s);
  if (!running) {
    const dv = s.dev || {};
    const stOk = dv.stage && dv.stage.state === "ok" && st;
    cells.push(mk("스테이지", stOk ? (s.dry ? "가상 · " : "연결 · ") + st.origin_text : devText(dv.stage), stOk ? (st.z_ok && st.xy_ok && !st.locked ? "ok" : "warn") : (dv.stage && dv.stage.text === "통신 끊김") ? "err" : DEV_CLS[(dv.stage || {}).state] || ""));
    cells.push(mk("카메라", s.camera.open ? "연결 · " + (s.camera.size ? s.camera.size.join("×") : "") : devText(dv.camera), DEV_CLS[(dv.camera || {}).state] || ""));
    cells.push(mk("좌표 보정", c.status === "done" ? "완료" : c.status === "invalid" ? c.text : "필요", c.status === "done" ? "ok" : c.status === "invalid" ? "err" : "warn"));
    cells.push(mk("릴레이", stOk ? "쿨러 " + (rt.co ? rt.co + "개 켜짐" : "꺼짐") + " · 히트건 " + (rt.heat ? "켜짐" : "꺼짐") : "알 수 없음", (stOk ? (rt.heat ? "heat" : rt.co ? "ok" : "") : "") + " relay"));
  } else {
    cells.push(mk("위치", st ? xy(st.x, st.y) + " · Z " + f1(st.z) : "—", "ok"));
    cells.push(mk("쿨러", rt.co ? "켜짐 " + rt.co + "개" : "꺼짐", rt.co ? "ok" : ""));
    cells.push(mk("히트건", rt.heat ? "켜짐" : "꺼짐", rt.heat ? "heat" : "ok"));
  }
  const box = $("#cells");
  box.replaceChildren(...cells);
  /* 띠: 경보 · 원점 회복(맨 위)과 장치마다 한 줄. 띠의 모든 안내는 누를 수 있는 버튼이다(막다른 상태 없음) */
  const band = $("#band"), bds = s.bands || [];
  const sig = JSON.stringify(bds);
  if (bds.length) {
    if (band.dataset.sig !== sig) {
      band.dataset.sig = sig;
      band.className = "bandbox";
      band.replaceChildren(...bds.map((bd) => h("div", { class: "band " + (bd.err ? "err" : ""), "data-id": bd.id }, h("span", { text: bd.text }), h("span", { class: "next", text: bd.next ? "· " + bd.next : "" }), h("span", { class: "grow" }),
        ...(bd.actions || []).map((a) => h("button", { class: "btn sm" + (a.busy ? " dis" : ""), text: a.label, "data-tip": a.busy ? "연결 중 · 잠시만" : (a.tip || null), onclick: () => { if (!a.busy) bandAct(a); } })))));
    }
  } else { band.dataset.sig = ""; band.className = "bandbox hidden"; band.replaceChildren(); }
}
/* 측정 시작을 막는 사유는 한 곳에 하나만: 가장 먼저 해결할 것 한 줄 + 바로 가기 버튼. opt = {camera, inst} 이 필요한지. 없으면 null. */
function gateOf(why, s, opt) {
  opt = opt || {};
  const dv = s.dev || {}, st = dv.stage || {};
  if (st.state && st.state !== "ok") return { text: st.state === "retry" ? "스테이지 다시 연결 중" : "스테이지 연결 안 됨", act: "reconnect", label: "다시 연결" };
  if (opt.camera && dv.camera && dv.camera.state !== "ok") return { text: dv.camera.state === "retry" ? "카메라 다시 연결 중" : "카메라 연결 안 됨", act: "cam_reconnect", label: "다시 연결" };
  if (opt.inst && ["k2110", "k2450"].some((k) => !dv[k] || dv[k].state !== "ok")) return { text: "측정 장비 연결 안 됨", act: "inst_connect", label: "장비 연결" };
  if (!why) return null;
  const m = /\s*·\s*장비 설정 (\d)\s*$/.exec(why);
  if (m) return { text: why.slice(0, m.index), act: "ui_setup", args: { step: Number(m[1]) }, label: "장비 설정 " + m[1] };
  const m2 = /^(.*?)\s*·\s*(접촉 높이로 내리기)\s*$/.exec(why);
  if (m2 && opt.down) return { text: m2[1], act: "rt-down", label: m2[2] };
  if (/^촬영 필요/.test(why)) return { text: "촬영 사진 없음", act: "capture", label: "촬영" };
  return { text: why };
}
function gateRow(g) {
  return g ? h("div", { class: "gate", id: "gate" }, h("span", { class: "grow", text: g.text }), g.act ? btn(g.label, g.act, g.args || null, "sm primary") : null) : null;
}
UI.acts.reconnect = async () => { const r = await send("reconnect"); if (r.ok) toast("스테이지 연결"); };
UI.acts.cam_reconnect = async () => { const r = await send("cam_reconnect"); if (r.ok) toast("카메라 연결"); };
UI.acts.inst_connect = async () => { const r = await send("inst_connect"); if (r.ok) toast("장비 연결"); };
UI.acts.ui_setup = (a) => gotoSetup(a.step || 1);
function gotoSetup(step) { UI.tab = "setup"; if (typeof S !== "undefined") { S.step = step; S.leftSig = ""; S.rightSig = ""; S.area = null; S.freeze = false; S.showRef = false; } render(); }
function bandAct(a) {
  if (a.act === "ui_setup") { gotoSetup((a.args || {}).step || 1); return; }
  const go = async () => { const r = await send(a.act, a.args || {}); if (r.ok && a.act === "origin") toast((a.args.axis || "").toUpperCase() + " 원점 잡기 완료"); };
  if (a.act === "capture" && UI.acts.capture) { UI.acts.capture(); return; }
  if (a.confirm) dialog(a.label + " 할까요?", (a.tip || "") + " 끝단에 닿는 드르륵 소리는 정상입니다.", [{ label: "취소" }, { label: a.label, cls: "primary", fn: go }]);
  else go();
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
