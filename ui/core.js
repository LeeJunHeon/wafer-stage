/* VO₂ 자동 측정 프로그램 화면 - 빌드 없는 JS. 서버가 상태의 주인이고, 화면은 요청만 보낸다(바뀌는 것은 서버 상태가 와야 바뀐다).
   core.js: 도우미 · 서버 요청 · 비상정지(걸기 · 해제 확인) · 알림 · 말풍선 · 탭 · 상태줄 · 경보 띠 · 사진 칸 · 서버 연결. 탭 화면은 TABS 에 등록한다. */
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
  e.append(..._kids(kids));
  return e;
}
/* 자식 목록: 배열은 펴고, null · undefined · false 는 그리지 않는다("null" 글자가 화면에 나오지 않게). */
function _kids(kids) { return kids.flat(Infinity).filter((c) => c != null && c !== false).map((c) => (c.nodeType ? c : document.createTextNode(String(c)))); }
function h(tag, attrs, ...kids) { return _fill(document.createElement(tag), attrs, kids); }
function svgEl(tag, attrs, ...kids) { return _fill(document.createElementNS("http://www.w3.org/2000/svg", tag), attrs, kids); }
/* 안의 내용을 통째로 바꾼다. replaceChildren 을 바로 쓰지 않는다(null 을 넘기면 "null" 글자가 된다). */
function setKids(el, ...kids) { el.replaceChildren(..._kids(kids)); return el; }
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
    if (!quiet) toast("서버 전송 실패");
    return { ok: false, msg: "서버 전송 실패" };
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
    if (!r.ok || j.ok === false) { toast(j.msg || "설정 저장 실패"); return false; }
    return true;
  } catch (e) { toast("설정 저장 실패"); return false; }
}

/* ---------- 비상정지 ----------
   거는 쪽: 버튼 pointerdown 즉시 · Esc 즉시(입력 칸 안이어도) · 확인 없음 · WS 응답을 기다리지 않는다. 정지 상태가 아닐 때 Esc 는 언제나 정지.
   푸는 쪽: 정지 중에는 같은 자리의 버튼이 [비상정지 중 · 해제] 가 된다. 그 버튼이나 Esc → 확인 창 "비상정지 해제?" →
            [해제] · Enter 로 해제, [취소] · Esc 로 창만 닫는다(정지 유지). 정지 직후 2초(S.estop_hold)는 해제를 받지 않는다(서버도 거절한다).
   키를 누르고 있어서 생기는 반복 입력(e.repeat)은 무시한다. */
let lastEstop = 0;
function estop() {
  const now = performance.now();
  if (now - lastEstop < 300) return;
  lastEstop = now;
  fetch("/api/estop", { method: "POST", keepalive: true }).catch(() => {});
}
/* "정지 중"은 서버 상태가 살아 있을 때만 믿는다. 상태를 못 받고 있으면(연결 끊김) Esc · 버튼은 해제를 묻지 않고 정지를 보낸다 */
const stateLive = () => wsOpen && (!lastMsg || Date.now() - lastMsg <= 4000);
const estopLocked = () => !!(S && S.estop_locked) && stateLive();
/* 확인 창: 비상정지 해제 하나에만 쓴다(다른 일에는 확인 창을 띄우지 않는다). 다른 창(#dlg) 위에 따로 뜬다 */
let confirmOk = null;                                         // 확인 창이 떠 있으면 [확인] 때 할 일(없으면 null)
function openConfirm(title, okText, fn) {
  let done = false;
  confirmOk = () => { if (done) return; done = true; closeConfirm(); fn(); };
  const ok = btn(okText, () => { if (confirmOk) confirmOk(); }, "on");
  setKids($("#confirm"), h("div", { class: "box" }, h("h3", { text: title }), h("div", { class: "foot" }, btn("취소", closeConfirm), ok)));
  $("#confirm").classList.remove("hidden");
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();      // 입력 칸에 남은 초점이 Enter 를 가져가지 않게
}
function closeConfirm() { confirmOk = null; $("#confirm").classList.add("hidden"); }
async function releaseEstop() {
  try {
    const r = await fetch("/api/estop/release", { method: "POST" });
    const j = await r.json();
    if (!j.ok && j.msg) toast(j.msg);                        // 예) 비상정지 직후 · 2초 뒤 해제 가능
  } catch (e) { toast("서버 전송 실패"); }
}
function askRelease() {
  if (!estopLocked() || S.estop_hold || confirmOk) return;   // 정지 직후 2초는 받지 않는다
  openConfirm("비상정지 해제?", "해제", releaseEstop);
}
$("#estop").addEventListener("pointerdown", (e) => { e.preventDefault(); if (estopLocked()) askRelease(); else estop(); });
$("#estop").addEventListener("keydown", (e) => { e.preventDefault(); });                 // Enter · Space 로 눌리지 않는다(Esc 가 비상정지)
document.addEventListener("keydown", (e) => {                                            // 입력 칸 안이어도(캡처 단계)
  if (e.key === "Escape") {
    e.preventDefault(); e.stopPropagation();
    if (e.repeat) return;
    if (!estopLocked()) { closeConfirm(); estop(); return; }                             // 정지 상태가 아니면 언제나 정지
    if (confirmOk) closeConfirm(); else askRelease();                                    // 정지 중: 확인 창을 닫거나(정지 유지) 해제를 묻는다
  } else if (e.key === "Enter" && confirmOk) {
    if ($("#confirm").contains(document.activeElement)) return;                          // 창 안의 버튼에 초점이 있으면 그 버튼이 눌린다
    e.preventDefault(); e.stopPropagation();
    if (!e.repeat) confirmOk();
  }
}, true);
/* 비상정지 버튼 · 위쪽 띠를 서버 상태(estop_locked · estop_hold)에 맞춘다 */
function renderEstop() {
  const b = $("#estop"), locked = !!S.estop_locked;
  if (b.dataset.locked !== String(locked)) {
    b.dataset.locked = String(locked);
    b.classList.toggle("locked", locked);
    setKids(b, ...(locked ? ["비상정지 중 · 해제"] : ["비상정지", h("small", { text: "Esc" })]));
  }
  /* 정지 직후 2초: 해제 버튼을 꺼 둔다(흐린 글 · 눌러도 askRelease 가 받지 않는다). disabled 속성은 쓰지 않는다 -
     비상정지 버튼이 눌리지 않는 상태로 남는 일이 없게(상태가 끊기면 이 버튼은 다시 정지를 보낸다) */
  b.classList.toggle("hold", locked && !!S.estop_hold);
  const a = S.alarms.find((x) => x.code === "estop");
  const band = $("#estop-band");
  if (a && band.textContent !== a.text) band.textContent = a.text;
  band.classList.toggle("hidden", !locked);
  if (!locked && confirmOk) closeConfirm();                  // 다른 길로 풀렸으면 확인 창을 닫는다
}

/* ---------- 알림(쌓이지 않는다) ---------- */
let toastT = null;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastT);
  toastT = setTimeout(() => t.classList.add("hidden"), 3000);
}

/* ---------- 창: 제목 · 내용(넘치면 내용만 안에서 스크롤) · 아래에 항상 보이는 버튼 줄. 높이는 화면 높이 − 80px 를 넘지 않는다 ---------- */
function openDialog(title, body, buttons) {
  setKids($("#dlg"), h("div", { class: "box" }, h("h3", { text: title }), h("div", { class: "dlg-body" }, body), h("div", { class: "dlg-foot" }, buttons)));
  $("#dlg").classList.remove("hidden");
}
function closeDialog() { $("#dlg").classList.add("hidden"); }

/* ---------- 말풍선(공용): tip(el, 내용) - 마우스를 올리고 0.3초 뒤에 뜨고, 떠나면 닫힌다. 화면 밖으로 나가지 않는다.
   내용 = 글, 또는 {title, text, hint}, 또는 그것을 돌려주는 함수. 브라우저 기본 title 은 쓰지 않는다(느리고 작다). ---------- */
let tipT = null, tipFor = null;
function hideTip() { clearTimeout(tipT); tipT = null; tipFor = null; $("#tip").classList.add("hidden"); }
function showTip(el, content) {
  const c = typeof content === "function" ? content() : content;
  if (!c) return;
  const o = typeof c === "string" ? { text: c } : c, t = $("#tip");
  setKids(t, o.title ? h("b", { text: o.title }) : null, o.text || "", o.hint ? h("span", { class: "hint", text: o.hint }) : null);
  t.style.left = "0px"; t.style.top = "0px";
  t.classList.remove("hidden");
  const r = el.getBoundingClientRect(), w = t.offsetWidth, hh = t.offsetHeight;
  const vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
  const x = Math.max(8, Math.min(r.left, vw - w - 8));       // 오른쪽 · 왼쪽 끝을 넘지 않는다
  let y = r.bottom + 4;
  if (y + hh > vh - 8) y = Math.max(8, r.top - hh - 4);      // 아래에 자리가 없으면 위에
  t.style.left = Math.round(x) + "px"; t.style.top = Math.round(y) + "px";
}
function tip(el, content) {
  el.classList.add("tipped");
  el.addEventListener("mouseenter", () => { clearTimeout(tipT); tipFor = el; tipT = setTimeout(() => { if (tipFor === el && el.isConnected) showTip(el, content); }, 300); });
  el.addEventListener("mouseleave", () => { if (tipFor === el) hideTip(); });
  return el;
}
document.addEventListener("pointerdown", hideTip, true);
document.addEventListener("scroll", hideTip, true);

/* ---------- [찾아보기]: 이 컴퓨터의 폴더 선택 창을 띄우고 고른 경로를 입력 칸에 넣는다. 취소하면 그대로.
   창으로 실행했을 때만 쓸 수 있다(S.can_pick_folder) - 쓸 수 없으면 쓰는 쪽이 버튼을 숨긴다. ---------- */
function browseBtn(input, cls) {
  const b = btn("찾아보기", async () => {
    if (b.dataset.busy) return;
    b.dataset.busy = "1";
    try {
      const r = await api("pick_folder", { start: input.value });
      if (r.ok && r.result.path) { input.value = r.result.path; input.dispatchEvent(new Event("change")); }
    } finally { b.dataset.busy = ""; }
  }, cls);
  return b;
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
function fmtDate(epoch) {                                   // 2026-11-12 09:50
  if (!epoch) return "";
  const d = new Date(epoch * 1000), p = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}
/* 그래프 선과 같은 색의 짧은 선(12px) - 이름 앞에 붙여 어느 선인지 알려 준다 */
function swatch(color, width) { return h("i", { class: "swatch", style: "border-top-color:" + color + ";border-top-width:" + (width || 2) + "px" }); }
/* 3×3 호모그래피로 점 하나를 옮긴다(스테이지 X · Y → 판 mm 등). 못 옮기면 null */
function applyH(H, x, y) {
  if (!H) return null;
  const w = H[2][0] * x + H[2][1] * y + H[2][2];
  if (!isFinite(w) || Math.abs(w) < 1e-12) return null;
  return [(H[0][0] * x + H[0][1] * y + H[0][2]) / w, (H[1][0] * x + H[1][1] * y + H[1][2]) / w];
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
  setKids($("#status"), 
    dev("stage", "스테이지" + port), dev("k2110", "온도계"), dev("k2450", "저항계"), dev("camera", "카메라"),
    posEl,
    h("span", { class: "item " + (heaterOn ? "st-warn" : "") }, "히트건 " + (heaterOn ? "켜짐" : "꺼짐")),
    h("span", { class: "item" }, "쿨러 " + (unknown ? "—" : cool.length ? "켜짐 " + cool.join("·") : "꺼짐")),
    h("span", { class: "safety" }, "차단 " + (c.heater_max_c != null ? c.heater_max_c : 110) + " °C · 통신 감시 " + ((c.watchdog_ms || 2000) / 1000) + "초"));
}
let alarmSig = "";
function renderAlarms() {
  const list = S.alarms.filter((a) => a.code !== "estop");   // 비상정지는 위쪽 띠와 버튼이 알린다(같은 상태를 두 곳에 보여 주지 않는다). 해제도 위 버튼 하나로
  const sig = JSON.stringify(list.map((a) => [a.code, a.text, a.level]));
  if (sig === alarmSig) return;
  alarmSig = sig;
  setKids($("#alarms"), ...list.map((a) => h("div", { class: "alarm " + a.level }, h("span", { class: "grow", text: a.text }),
    a.dismiss ? btn("지우기", () => api("alarm_clear", { code: a.code }), "sm") : null)));
}

/* ====================================================================
   사진 칸: 비율(ar = 가로 ÷ 세로)을 지켜 칸 안에 맞춘다. 위 · 왼쪽부터 채우고 남는 자리는 패널 바탕색(opts.center 면 칸 가운데).
   사진이 들어갈 크기 그대로 상자(1px 테두리 · 옅은 바탕)를 항상 그린다: 사진이 없으면 안내 글을 상자 가운데에, 있으면 사진을 그 안에.
   svg 덧그림은 사진과 같은 크기(좌표는 쓰는 쪽이 viewBox 로 정한다). toFrac(ev) = 사진 안 위치(0~1).
   ==================================================================== */
function photoBox(opts) {
  opts = opts || {};
  const img = h("img", { alt: "", draggable: "false", class: "hidden" });
  const svg = svgEl("svg", { preserveAspectRatio: "none", class: "hidden" });
  const note = h("div", { class: "photo-note", text: opts.note || "" });
  const inner = h("div", { class: "photo-in" }, img, svg, note);      // 상자: 사진이 없어도 사진 크기 그대로 보인다
  const box = h("div", { class: "photo-box " + (opts.cls || "") }, inner);
  const pb = { box, inner, img, svg, note, ar: opts.ar || 1, w: 0, h: 0, url: "" };
  pb.fit = () => {
    /* 칸의 실제 크기(소수)를 내림해서 쓴다. clientHeight 는 반올림이라 실제보다 클 수 있고, 그러면 상자의 아래 테두리 한 줄이 칸 밖으로 나가 잘린다.
       상자 크기(w × hh)는 테두리를 포함한다(box-sizing: border-box). */
    const rc = box.getBoundingClientRect(), cw = Math.floor(rc.width), ch = Math.floor(rc.height);
    if (!cw || !ch) return;
    const w = Math.floor(Math.min(cw, ch * pb.ar)), hh = Math.floor(w / pb.ar);
    if (opts.center) { inner.style.left = Math.floor((cw - w) / 2) + "px"; inner.style.top = Math.floor((ch - hh) / 2) + "px"; }
    if (w === pb.w && hh === pb.h) return;
    pb.w = w; pb.h = hh;
    inner.style.width = w + "px"; inner.style.height = hh + "px";
    if (opts.onfit) opts.onfit(pb);
  };
  new ResizeObserver(pb.fit).observe(box);
  pb.setAr = (ar) => { if (ar > 0 && Math.abs(ar - pb.ar) > 1e-6) { pb.ar = ar; pb.w = 0; if (pb.relayout) pb.relayout(); pb.fit(); } };
  pb.show = (url) => {                                     // url 이 없으면 빈 상자 + 가운데 안내 글
    const has = !!url;
    if (has && url !== pb.url) img.src = url;
    pb.url = has ? url : "";
    img.classList.toggle("hidden", !has); svg.classList.toggle("hidden", !has); note.classList.toggle("hidden", has);
    pb.fit();
  };
  pb.setNote = (t) => { note.textContent = t; };
  pb.toFrac = (ev) => {                                    // 테두리 안쪽(사진이 그려진 영역) 기준
    const r = inner.getBoundingClientRect(), bw = inner.clientLeft, bh = inner.clientTop;
    return [(ev.clientX - r.left - bw) / Math.max(1, inner.clientWidth), (ev.clientY - r.top - bh) / Math.max(1, inner.clientHeight)];
  };
  return pb;
}

/* 사진 + 조작 칸(400px): 조작 칸을 사진 바로 오른쪽(16px)에 붙인다. 사진 칸의 폭을 "사진이 실제로 차지하는 폭"으로 정해
   큰 창에서도 조작 칸이 사진에서 멀어지지 않는다(남는 자리는 조작 칸 오른쪽). 조작 칸이 넘치면 그 칸 안에서만 스크롤한다. */
const CTL_W = 400, SPLIT_GAP = 16, PHOTO_MIN_W = 440;
function photoSplit(pb, ctl) {
  ctl.classList.add("ctl");
  const el = h("div", { class: "split fill" }, pb.box, ctl);
  pb.relayout = () => {
    const w = el.clientWidth, hh = el.clientHeight;
    if (!w || !hh) return;
    const pw = Math.max(Math.min(PHOTO_MIN_W, w - SPLIT_GAP - CTL_W), Math.floor(Math.min(w - SPLIT_GAP - CTL_W, hh * pb.ar)));
    if (pb.box.style.width !== pw + "px") pb.box.style.width = pw + "px";
  };
  new ResizeObserver(pb.relayout).observe(el);
  return el;
}

/* ====================================================================
   서버 연결 · 그리기
   ==================================================================== */
function renderTab() {
  const m = TABS[tab];
  if (!m || !m.render) return;
  try { m.render(); } catch (e) { console.error(e); toast("화면 표시 실패 · " + String(e && e.message ? e.message : e).slice(0, 60)); }
}
function render() {
  renderEstop(); renderStatus(); renderAlarms(); renderTab();
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
