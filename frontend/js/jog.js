/* jog.js - 조그 패드 하나(측정 화면 · 장비 설정 모두 이 컴포넌트).
   배치는 좌표 보정에서 계산한다: 스테이지 +X · +Y 가 사진에서 가리키는 방향을 위 · 아래 · 왼 · 오른쪽 중 가장 가까운 쪽에 둔다.
   좌표 보정이 없으면 기존 프로그램 배치(위 X+ · 왼 Y+ · 오른 Y− · 아래 X−). 키보드 화살표도 같은 매핑, PageUp/PageDown = Z.
   동작은 기존 프로그램 jog.js 와 같다: 한 번 누름 = 한 스텝, 0.5 s 이상 누르고 있으면 ack 를 받은 뒤 다음 스텝(큐에 쌓지 않는다),
   거절(끝단 · 이동 중 · 잠금)이면 멈춤, 키보드는 400 ms 데드맨, 창 포커스를 잃으면 멈춘다.

   안전(모터 사고 방지):
   - 키보드 조그는 조그 카드가 '키보드 조그 켬' 상태일 때만: 카드 안을 누르면 켜지고, 카드 밖을 누르거나 탭이 바뀌거나 창이 열리거나 창 포커스를 잃으면 꺼진다.
   - 창(#win)이 열려 있으면 키보드 · 패드 조그 모두 안 된다.
   - 연속 조그는 스텝 jog.repeat_max_mm 이하만, Z↓ 는 한 번에 한 스텝(연속 없음). 큰 스텝 · Z↓ 는 키를 누르고 있어도 한 번만 나간다.
   - Z 스텝은 따로(jog.z_steps). 패드 입력은 pointer 이벤트 + setPointerCapture, 떼는 · 취소 · 벗어남 · 포커스 잃음 · 탭 숨김에서 멈춘다.
   - 비상정지를 누르는 순간 조그를 멈추고, 그때 눌려 있던 키 · 버튼은 뗄 때까지 다시 시작하지 않는다(자동 반복 keydown 포함). */
"use strict";
const JOG = { step: 1, zstep: 0.1, held: null, inFlight: false, lastKeyAt: 0, keySeq: 0, sentSeq: -1, heldSince: 0, timer: null, kbd: false,
  down: new Set(), dead: new Set(), ptr: false };
const JOG_KEY_ALIVE_MS = 400, JOG_HOLD_REPEAT_MS = 500;
const JOG_KEYS = ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "PageUp", "PageDown"];

/* 스테이지 +X · +Y 의 사진 방향 → 패드 배치. 반환 {up, down, left, right}: 각 [축, 부호] */
function jogLayout(s) {
  const def = { up: ["x", 1], down: ["x", -1], left: ["y", 1], right: ["y", -1] };
  const c = s && s.calib;
  if (!c || !c.area_mm || !c.area || typeof pinJac !== "function") return def;
  const jac = pinJac(s);
  if (!jac) return def;
  const slot = (v) => (Math.abs(v[0]) >= Math.abs(v[1]) ? (v[0] > 0 ? "right" : "left") : (v[1] > 0 ? "down" : "up"));    // 사진: u 오른쪽 · v 아래쪽
  const opp = { up: "down", down: "up", left: "right", right: "left" };
  const sx = slot(jac.x), sy = slot(jac.y);
  if (sx === sy || sx === opp[sy]) return def;
  const out = {};
  out[sx] = ["x", 1]; out[opp[sx]] = ["x", -1];
  out[sy] = ["y", 1]; out[opp[sy]] = ["y", -1];
  return ["up", "down", "left", "right"].every((k) => out[k]) ? out : def;
}
const JOG_ARROW = { up: "↑", down: "↓", left: "←", right: "→" };

function jogCfg() { return (UI.state && UI.state.settings && UI.state.settings.jog) || {}; }
function jogRepeatMax() { return Number(jogCfg().repeat_max_mm) || 1; }
function jogWinOpen() { const w = document.getElementById("win"); return !!w && !w.classList.contains("hidden"); }
function jogVisiblePad() { return Array.from(document.querySelectorAll(".jogpad")).find((p) => p.offsetParent !== null) || null; }
/* 지금 보이는 패드가 켜져 있나(비활성 사유가 없고 창이 안 열렸다) */
function jogEnabled() { const p = jogVisiblePad(); return !!p && !p.hasAttribute("data-off") && !jogWinOpen(); }
function jogFree() { const p = jogVisiblePad(); return !!(p && p.hasAttribute("data-free")); }
/* 이 방향이 연속(누르고 있으면 계속)으로 갈 수 있나: Z↓ 는 아니고, 스텝이 연속 한도 이하일 때만 */
function jogCanRepeat(axis, sign) { return !(axis === "z" && sign > 0) && (axis === "z" ? JOG.zstep : JOG.step) <= jogRepeatMax() + 1e-9; }

function jogStop() {
  JOG.held = null;                                  // 이미 나간 한 스텝은 끝까지 간다(중간에 못 끊는다)
  if (JOG.timer) { clearTimeout(JOG.timer); JOG.timer = null; }
}
/* 서버 연결이 끊기면 ack 가 영영 오지 않는다 - 잠금을 풀어 두어야 다시 연결 뒤 패드가 산다 */
function jogReset() {
  jogStop();
  JOG.inFlight = false;
  JOG.down.clear(); JOG.dead.clear(); JOG.ptr = false;
}
/* 비상정지: 조그를 멈추고, 지금 눌려 있는 키는 뗄 때까지 다시 시작하지 않는다 */
function jogOnEstop() {
  jogStop();
  JOG.dead = new Set(JOG.down);
}
function jogKbdOff() { if (JOG.kbd) { JOG.kbd = false; jogApplyKbd(); } jogStop(); }
function jogKbdOn() { if (!JOG.kbd) { JOG.kbd = true; jogApplyKbd(); } }
function jogApplyKbd() {
  document.querySelectorAll(".card").forEach((c) => {
    const pad = c.querySelector(".jogpad");
    c.classList.toggle("kbd", !!(JOG.kbd && pad && pad.offsetParent !== null));
  });
}

async function jogSend() {
  const h_ = JOG.held;
  if (!h_ || JOG.inFlight || !jogEnabled()) return;
  JOG.inFlight = true;
  JOG.sentSeq = JOG.keySeq;
  const mm = h_.axis === "z" ? JOG.zstep : JOG.step;
  const rep = h_.n > 0;
  h_.n++;
  let r;
  try { r = await send("jog", { axis: h_.axis, mm: h_.sign * mm, rep, free: jogFree() }); } catch (e) { r = { ok: false }; }
  JOG.inFlight = false;
  if (!r || r.ok === false) {                                              // 거절(끝단 · 이동 중 · 잠금)이면 이어 보내지 않는다. 키는 뗄 때까지 다시 시작하지 않는다
    if (h_.src === "key") JOG.dead.add(h_.code);
    jogStop(); return;
  }
  const cur = JOG.held;
  if (!cur || cur !== h_) return;
  if (!h_.cont) { jogStop(); return; }                                      // 큰 스텝 · Z↓ 는 한 번에 한 스텝
  if (cur.src === "key" && (JOG.keySeq === JOG.sentSeq || Date.now() - JOG.lastKeyAt > JOG_KEY_ALIVE_MS)) { jogStop(); return; }   // 새 keydown 없이는 이어지지 않는다
  if (cur.src === "pointer") {
    const left = JOG_HOLD_REPEAT_MS - (Date.now() - JOG.heldSince);        // 클릭 한 번은 정확히 한 스텝, 0.5 s 이상 눌러야 연속
    if (left > 0) {
      if (JOG.timer) clearTimeout(JOG.timer);
      JOG.timer = setTimeout(() => { JOG.timer = null; if (JOG.held === cur && cur.src === "pointer") jogSend(); }, left);
      return;
    }
  }
  jogSend();
}
function jogStart(axis, sign, src, code) {
  if (!jogEnabled()) return;
  if (JOG.timer) { clearTimeout(JOG.timer); JOG.timer = null; }
  JOG.held = { axis, sign, src, code: code || null, n: 0, cont: jogCanRepeat(axis, sign) };
  JOG.heldSince = Date.now();
  jogSend();
}
/* 패드 버튼: pointer 이벤트 + 캡처. 떼면 · 취소되면 · 벗어나면 · 캡처를 잃으면 멈춘다 */
function jogBindButton(el, axis, sign) {
  el.addEventListener("pointerdown", (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    e.preventDefault();
    JOG.ptr = true;
    jogKbdOn();
    try { el.setPointerCapture(e.pointerId); } catch (x) { /* 합성 이벤트 */ }
    jogStart(axis, sign, "pointer");
  });
  const up = () => { JOG.ptr = false; jogStop(); };
  ["pointerup", "pointercancel", "pointerleave", "lostpointercapture"].forEach((ev) => el.addEventListener(ev, up));
  el.addEventListener("pointermove", (e) => {                                  // 캡처 중에는 pointerleave 가 안 온다 - 버튼 밖으로 벗어나면 직접 멈춘다
    if (!JOG.held || JOG.held.src !== "pointer") return;
    const r = el.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) up();
  });
}
window.addEventListener("pointerup", () => { JOG.ptr = false; jogStop(); });
window.addEventListener("blur", () => { jogReset(); jogKbdOff(); });
document.addEventListener("visibilitychange", () => { if (document.hidden) { jogReset(); jogKbdOff(); } });

/* 키보드 조그 켜기 · 끄기: 카드 안을 누르면 켜지고, 카드 밖을 누르면 꺼진다 */
document.addEventListener("pointerdown", (e) => {
  const card = e.target.closest && e.target.closest(".card");
  if (card && card.querySelector(".jogpad") && !jogWinOpen()) jogKbdOn(); else jogKbdOff();
}, true);
document.addEventListener("focusin", (e) => {                                  // 카드 밖으로 포커스가 가면(Tab) 꺼진다
  const card = e.target.closest && e.target.closest(".card");
  if (!(card && card.querySelector(".jogpad"))) jogKbdOff();
});

document.addEventListener("keydown", (e) => {
  if (!JOG_KEYS.includes(e.key)) return;
  if (!JOG.kbd || jogWinOpen() || !jogEnabled()) return;
  const t = e.target;
  if (t && /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName)) return;               // 입력 칸에 글자 치는 중
  const map = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" };
  let m = null;
  if (map[e.key]) m = jogLayout(UI.state)[map[e.key]];
  else if (e.key === "PageUp") m = ["z", -1];
  else if (e.key === "PageDown") m = ["z", 1];
  if (!m) return;
  e.preventDefault();                                // 화살표 키가 스크롤이나 선택 칸을 바꾸지 않는다
  JOG.down.add(e.key);
  if (JOG.dead.has(e.key)) return;                   // 비상정지 · 거절 때 눌려 있던 키: 뗄 때까지 다시 시작하지 않는다
  if (e.repeat && !jogCanRepeat(m[0], m[1])) return;  // 큰 스텝 · Z↓ 는 누르고 있어도 한 번만
  JOG.lastKeyAt = Date.now(); JOG.keySeq++;
  if (!JOG.held || JOG.held.axis !== m[0] || JOG.held.sign !== m[1]) jogStart(m[0], m[1], "key", e.key);
  else if (!JOG.inFlight) jogSend();
});
document.addEventListener("keyup", (e) => {
  JOG.down.delete(e.key); JOG.dead.delete(e.key);
  if (JOG.held && JOG.held.src === "key" && JOG_KEYS.includes(e.key)) jogStop();
});

/* 스텝 고르기: 설정 jog.steps 전부 + 직접 입력(mm). 고르면 화면 안의 모든 스텝 버튼에 바로 반영한다 */
function jogSetStep(v) {
  if (!(v > 0)) return;
  JOG.step = v;
  document.querySelectorAll(".jstep").forEach((b) => b.classList.toggle("on", Number(b.dataset.v) === v));
  document.querySelectorAll(".jstep-in").forEach((i) => { if (document.activeElement !== i) i.value = v; });
}
function jogSetZStep(v) {
  if (!(v > 0)) return;
  JOG.zstep = v;
  document.querySelectorAll(".jzstep").forEach((b) => b.classList.toggle("on", Number(b.dataset.v) === v));
}
/* 스텝 열(패드 오른쪽): X · Y 스텝(설정 jog.steps 전부 + 직접 입력) · Z 스텝(jog.z_steps, 따로). 고르면 화면 안의 모든 스텝 버튼에 바로 반영한다 */
function jogSteps(s) {
  const jc = s.settings.jog || {}, steps = jc.steps || [0.1, 1, 10, 50], max = jc.max_mm || 50, zs = jc.z_steps || [0.05, 0.1, 0.5, 1];
  if (!zs.includes(JOG.zstep)) JOG.zstep = zs.includes(0.1) ? 0.1 : zs[0];
  const inp = h("input", { type: "number", class: "jstep-in", min: 0.01, max, step: "any", value: JOG.step, "data-tip": "스텝을 직접 입력합니다(mm).",
    onchange: (e) => { const v = Number(e.target.value); if (v >= 0.01 && v <= max) jogSetStep(v); else { e.target.value = JOG.step; toast("스텝 0.01 ~ " + max + " mm", true); } } });
  return h("div", { class: "col jsteps" },
    h("span", { class: "sub3", text: "X · Y 스텝 (mm)" }),
    h("div", { class: "jbtns" }, ...steps.map((v) => h("button", { class: "btn sm jstep" + (JOG.step === v ? " on" : ""), "data-v": v, text: String(v), onclick: () => jogSetStep(v),
      "data-tip": v > jogRepeatMax() ? "이 스텝은 누를 때마다 한 번만 움직입니다(누르고 있어도 연속으로 가지 않음)." : "0.5 초 이상 누르고 있으면 연속으로 움직입니다." }))),
    h("div", { class: "row jin" }, inp, h("span", { class: "sub3", text: "직접" })),
    h("span", { class: "sub3", text: "Z 스텝 (mm)" }),
    h("div", { class: "jbtns" }, ...zs.map((v) => h("button", { class: "btn sm jzstep" + (JOG.zstep === v ? " on" : ""), "data-v": v, text: String(v), onclick: () => jogSetZStep(v), "data-tip": "Z 버튼이 한 번에 움직이는 거리입니다." }))));
}

/* 패드: opt.why = 비활성 사유(없으면 활성), opt.free = 접촉 높이 화면(Z 아래 한도 없음) */
function jogPad(s, opt) {
  opt = opt || {};
  const dirs = jogLayout(s), why = opt.why || "";
  const b = (slot) => {
    const [axis, sign] = dirs[slot];
    const lab = JOG_ARROW[slot] + " " + axis.toUpperCase() + (sign > 0 ? "+" : "−");
    const el = h("button", { class: "btn jogb" + (why ? " dis" : ""), text: lab, "data-tip": why || (axis.toUpperCase() + (sign > 0 ? "+" : "−") + " 방향으로 스텝만큼 이동합니다. 스텝이 " + jogRepeatMax() + " mm 이하면 0.5 초 이상 눌러 연속으로 움직일 수 있습니다(키보드 " + JOG_ARROW[slot] + ").") });
    if (!why) jogBindButton(el, axis, sign);
    return el;
  };
  const z = (lab, sign, key) => {
    const el = h("button", { class: "btn jogb" + (why ? " dis" : ""), text: lab, "data-tip": why || ("Z 를 " + (sign < 0 ? "위로" : "아래로 한 스텝만") + " 이동합니다(" + key + ")" + (sign > 0 ? ". 누르고 있어도 연속으로 내려가지 않습니다." : "")) });
    if (!why) jogBindButton(el, "z", sign);
    return el;
  };
  const pad = h("div", { class: "pad" }, h("span"), b("up"), h("span"), b("left"), h("span", { class: "padc" }, h("span", { class: "kbdtag", text: "키보드 조그" })), b("right"), h("span"), b("down"), h("span"));
  const zt = opt.ztop ? btn("Z 맨 위", opt.ztop.act, null, "jogb", "Z 를 맨 위로 올립니다.", opt.ztop.why || "") : null;
  const el = h("div", { class: "jogpad row", "data-tip": why ? null : "키보드 조그: 이 카드 안을 누르면 켜집니다(화살표 · PageUp/PageDown)." }, pad, h("div", { class: "col zpad" }, z("Z ↑", -1, "PageUp"), z("Z ↓", 1, "PageDown"), zt), jogSteps(s));
  if (why) el.setAttribute("data-off", "1");
  if (opt.free) el.setAttribute("data-free", "1");
  return el;
}
UI.renders.push(() => jogApplyKbd());
