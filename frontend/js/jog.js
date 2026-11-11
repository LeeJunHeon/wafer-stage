/* jog.js - 조그 패드 하나(측정 화면 · 장비 설정 모두 이 컴포넌트).
   배치는 좌표 보정에서 계산한다: 스테이지 +X · +Y 가 사진에서 가리키는 방향을 위 · 아래 · 왼 · 오른쪽 중 가장 가까운 쪽에 둔다.
   좌표 보정이 없으면 기존 프로그램 배치(위 X+ · 왼 Y+ · 오른 Y− · 아래 X−). 키보드 화살표도 같은 매핑, PageUp/PageDown = Z.
   가운데 칸 = [정지](움직이는 모터를 즉시 멈추고 이어질 이동을 취소한다. 잠그지 않는다. 비상정지와 같은 빠른 길).

   안전(모터 사고 방지):
   - 패드는 화면에 한 번 만들고 다시 만들지 않는다(키 = opt.key). 비활성 사유 · 방향 배치 · free 는 제자리에서 class · 속성 · 글자만 바꾼다.
     이벤트는 늘 걸려 있고 누를 수 있는지는 누르는 순간 판단한다. "이동 중" 은 패드를 끄는 사유가 아니다(서버가 거절한다).
   - 연속 조그(마우스): 서버 응답(ack)을 받으면 다음 스텝. 다음 스텝 직전에 그 버튼이 아직 화면에 있고 포인터를 잡고 있는지 본다.
     떼기 · 벗어남 · pointercancel · lostpointercapture · 창 포커스 잃음 · 탭 바뀜 · 패드 꺼짐 · 정지 · 비상정지에서 끝나고 JOG.held 를 비운다.
   - 키보드 조그는 패드(.jogpad 의 방향 · Z · 스텝 버튼)를 직접 눌렀을 때만 켜진다. 입력 칸 · 선택 칸 · 패드 밖 버튼을 누르거나 창 · 메뉴가 열리거나
     패드가 꺼지면 꺼진다. 새로 누른 키(repeat 가 아닌 keydown)로만 시작하고, 자동 반복 keydown 은 이미 그 키로 시작한 연속 조그를 잇는 데만 쓴다.
   - 패드를 누르는 순간 쓰는 스텝 = 화면에 보이는 값. "직접" 칸은 유효하면 입력 즉시 적용, 아니면 칸이 붉고 패드가 눌리지 않는다.
   - 연속 조그는 스텝 jog.repeat_max_mm 이하만, Z↓ 는 한 번에 한 스텝. 키보드는 400 ms 데드맨.
   - 비상정지 · 정지를 누르는 순간 조그를 멈추고, 그때 눌려 있던 키 · 버튼은 뗄 때까지 다시 시작하지 않는다. */
"use strict";
const JOG = { step: 1, zstep: 0.1, held: null, inFlight: false, lastKeyAt: 0, keySeq: 0, sentSeq: -1, heldSince: 0, timer: null, kbd: false,
  down: new Set(), dead: new Set(), ptr: false, stepBad: "", pads: {} };
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
/* 이 패드가 지금 눌릴 수 있나(비활성 사유가 없고 창이 안 열렸다) */
function jogPadOk(p) { return !!p && p.isConnected && !p.hasAttribute("data-off") && !jogWinOpen(); }
function jogEnabled() { return jogPadOk(jogVisiblePad()); }
function jogFree(p) { return !!(p && p.hasAttribute("data-free")); }
/* 이 방향이 연속(누르고 있으면 계속)으로 갈 수 있나: Z↓ 는 아니고, 스텝이 연속 한도 이하일 때만 */
function jogCanRepeat(axis, sign, mm) { return !(axis === "z" && sign > 0) && (mm != null ? mm : (axis === "z" ? JOG.zstep : JOG.step)) <= jogRepeatMax() + 1e-9; }

function jogStop() {
  JOG.held = null;                                  // 이미 나간 한 스텝은 끝까지 간다(중간에 못 끊는다)
  if (JOG.timer) { clearTimeout(JOG.timer); JOG.timer = null; }
}
/* 서버 연결이 끊기면 ack 가 영영 오지 않는다 - 잠금을 풀어 두어야 다시 연결 뒤 패드가 산다. 다시 붙었을 때 키보드 조그는 꺼진 상태 */
function jogReset() {
  jogStop();
  JOG.inFlight = false;
  JOG.down.clear(); JOG.dead.clear(); JOG.ptr = false;
  jogKbdOff();
}
/* 비상정지 · 정지: 조그를 멈추고, 지금 눌려 있는 키는 뗄 때까지 다시 시작하지 않는다 */
function jogOnEstop() { jogStop(); JOG.dead = new Set(JOG.down); }
function jogOnStop() { jogStop(); JOG.dead = new Set(JOG.down); jogKbdOff(); }
function jogKbdOff() { if (JOG.kbd) { JOG.kbd = false; jogApplyKbd(); } jogStop(); }
function jogKbdOn() { if (!JOG.kbd) { JOG.kbd = true; jogApplyKbd(); } }
function jogApplyKbd() {
  document.querySelectorAll(".card, .panel").forEach((c) => {
    const pad = c.querySelector(".jogpad");
    c.classList.toggle("kbd", !!(JOG.kbd && pad && pad.offsetParent !== null));
  });
  document.querySelectorAll(".jogkbd").forEach((t) => { t.classList.toggle("on", JOG.kbd); });
}
/* 머리줄의 "키보드 조그" 표시(실제 상태와 늘 같다) */
function jogKbdTag() { return h("span", { class: "kbdtag jogkbd" + (JOG.kbd ? " on" : ""), text: "키보드 조그 켬", "data-tip": "패드의 방향 · Z 버튼을 누르면 켜지고, 그 밖을 누르면 꺼집니다(화살표 · PageUp/PageDown)." }); }

/* 스텝: 패드의 "직접" 칸이 유효하면 그 값이 곧 스텝. 유효하지 않으면 null(패드가 눌리지 않는다) */
function jogStepNow(pad) {
  const inp = pad && pad.querySelector(".jstep-in");
  if (inp && inp.classList.contains("bad")) { JOG.stepBad = inp.dataset.why || "스텝 값 오류"; return null; }
  if (inp) { const v = Number(inp.value); if (!(v > 0)) return null; if (v !== JOG.step) JOG.step = v; }
  return JOG.step;
}

async function jogSend() {
  const h_ = JOG.held;
  if (!h_ || JOG.inFlight) return;
  if (!jogPadOk(h_.pad)) { jogStop(); return; }
  if (h_.src === "pointer" && h_.pid != null && !(h_.el.isConnected && h_.el.hasPointerCapture && h_.el.hasPointerCapture(h_.pid))) { jogStop(); return; }   // 버튼이 사라졌거나 포인터를 놓쳤다
  const mm = h_.axis === "z" ? JOG.zstep : jogStepNow(h_.pad);
  if (mm == null) { jogStop(); return; }
  JOG.inFlight = true;
  JOG.sentSeq = JOG.keySeq;
  const rep = h_.n > 0;
  h_.n++;
  let r;
  try { r = await send("jog", { axis: h_.axis, mm: h_.sign * mm, rep, free: jogFree(h_.pad) }); } catch (e) { r = { ok: false }; }
  JOG.inFlight = false;
  if (!r || r.ok === false) {                                              // 거절(끝단 · 이동 중 · 잠금)이면 이어 보내지 않는다. 키는 뗄 때까지 다시 시작하지 않는다
    if (h_.src === "key") JOG.dead.add(h_.code);
    if (JOG.held === h_) jogStop();
    return;
  }
  const cur = JOG.held;
  if (!cur || cur !== h_) return;
  if (!cur.cont) { jogStop(); return; }                                    // 큰 스텝 · Z↓ 는 한 번에 한 스텝
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
function jogStart(axis, sign, src, code, pad, el, pid) {
  pad = pad || jogVisiblePad();
  if (!pad) return;
  if (!jogPadOk(pad)) { if (src === "pointer" && pad.dataset.why) toast(pad.dataset.why, true); return; }
  if (src === "pointer" && settling(el)) return;                           // 화면이 스스로 바뀐 직후 400 ms 는 그 자리의 누름을 무시한다
  const mm = axis === "z" ? JOG.zstep : jogStepNow(pad);
  if (mm == null) { toast(JOG.stepBad || "스텝 값 오류", true); return; }
  if (JOG.timer) { clearTimeout(JOG.timer); JOG.timer = null; }
  JOG.held = { axis, sign, src, code: code || null, n: 0, cont: jogCanRepeat(axis, sign, mm), pad, el: el || null, pid: pid == null ? null : pid };
  JOG.heldSince = Date.now();
  jogSend();
}
/* 패드 버튼: pointer 이벤트 + 캡처. 떼면 · 취소되면 · 벗어나면 · 캡처를 잃으면 멈춘다. 방향은 누르는 순간의 dataset 에서 읽는다(배치가 제자리에서 바뀔 수 있다) */
function jogBindButton(el) {
  el.addEventListener("pointerdown", (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    e.preventDefault();
    JOG.ptr = true;
    const a = document.activeElement;
    if (a && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName)) a.blur();           // 패드를 누르면 입력 칸의 포커스를 뺀다(그 뒤 화살표 키는 조그)
    jogKbdOn();
    try { el.setPointerCapture(e.pointerId); } catch (x) { /* 합성 이벤트 */ }
    jogStart(el.dataset.axis, Number(el.dataset.sign), "pointer", null, el.closest(".jogpad"), el, e.pointerId);
  });
  const up = () => { JOG.ptr = false; jogStop(); };
  ["pointerup", "pointercancel", "pointerleave", "lostpointercapture"].forEach((ev) => el.addEventListener(ev, up));
  el.addEventListener("pointermove", (e) => {                                  // 캡처 중에는 pointerleave 가 안 온다 - 버튼 밖으로 벗어나면 직접 멈춘다
    if (!JOG.held || JOG.held.src !== "pointer") return;
    const r = el.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) up();
  });
}
const jogUp = () => { JOG.ptr = false; if (JOG.held && JOG.held.src === "pointer") jogStop(); };
window.addEventListener("pointerup", jogUp, true);
window.addEventListener("pointercancel", jogUp, true);
document.addEventListener("lostpointercapture", jogUp, true);               // document 수준에서도 듣는다(노드가 바뀌어도 잡히도록)
window.addEventListener("blur", () => { jogReset(); });
document.addEventListener("visibilitychange", () => { if (document.hidden) jogReset(); });

/* 키보드 조그 켜기 · 끄기: 패드의 방향 · Z · 스텝 버튼을 직접 눌렀을 때만 켜진다. 그 밖(입력 칸 · 선택 칸 · 패드 밖 버튼 · 메뉴 · 창 · [정지])을 누르면 꺼진다 */
document.addEventListener("pointerdown", (e) => {
  const t = e.target;
  const onPad = t.closest && t.closest(".jogpad") && t.closest("button.jogb, button.jstep, button.jzstep") && !t.closest(".jstop");
  if (onPad && !jogWinOpen()) {
    const a = document.activeElement;                                          // 패드 버튼(방향 · Z · 스텝)을 누르면 입력 칸의 포커스를 뺀다 - 그 뒤 화살표는 조그(칸의 숫자를 바꾸지 않는다)
    if (a && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName)) a.blur();
    jogKbdOn();
  } else jogKbdOff();
}, true);
document.addEventListener("focusin", (e) => {                                  // 패드 밖으로 포커스가 가면(Tab · 클릭) 꺼진다
  const t = e.target;
  if (!(t.closest && t.closest(".jogpad") && !/^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName))) jogKbdOff();
});

document.addEventListener("keydown", (e) => {
  if (!JOG_KEYS.includes(e.key)) return;
  JOG.down.add(e.key);                               // 켜져 있지 않아도 기록한다: 카드 밖에서 누른 채 카드 안을 눌러도 시작하지 않게
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
  if (JOG.dead.has(e.key)) return;                   // 비상정지 · 정지 · 거절 때 눌려 있던 키: 뗄 때까지 다시 시작하지 않는다
  if (e.repeat) {                                    // 자동 반복은 이미 이 키로 시작한 연속 조그를 잇는 데만
    const hd = JOG.held;
    if (!hd || hd.src !== "key" || hd.code !== e.key || !hd.cont) return;
    JOG.lastKeyAt = Date.now(); JOG.keySeq++;
    if (!JOG.inFlight) jogSend();
    return;
  }
  JOG.lastKeyAt = Date.now(); JOG.keySeq++;
  jogStart(m[0], m[1], "key", e.key, jogVisiblePad());
});
document.addEventListener("keyup", (e) => {
  JOG.down.delete(e.key); JOG.dead.delete(e.key);
  if (JOG.held && JOG.held.src === "key" && JOG_KEYS.includes(e.key)) jogStop();
});

/* 스텝 고르기: 설정 jog.steps 전부 + 직접 입력(mm). 고르면 화면 안의 모든 패드에 바로 반영한다 */
function jogMarkStepBtns() {
  document.querySelectorAll(".jstep").forEach((b) => b.classList.toggle("on", Number(b.dataset.v) === JOG.step));
}
function jogSetStep(v) {
  if (!(v > 0)) return;
  JOG.step = v; JOG.stepBad = "";
  jogMarkStepBtns();
  document.querySelectorAll(".jstep-in").forEach((i) => { i.value = v; i.classList.remove("bad"); });
}
function jogSetZStep(v) {
  if (!(v > 0)) return;
  JOG.zstep = v;
  document.querySelectorAll(".jzstep").forEach((b) => b.classList.toggle("on", Number(b.dataset.v) === v));
}
/* 칸에 친 값 검사: 유효하면 입력 즉시 스텝이 된다. 0 · 빈 칸 · 한도 초과는 붉게 + 패드 눌림 거절 */
function jogCheckStepInput(inp, max) {
  const raw = inp.value, v = Number(raw);
  let why = "";
  if (raw === "" || !isFinite(v)) why = "스텝을 입력";
  else if (v < 0.01) why = "스텝은 0.01 mm 이상";
  else if (v > max) why = "스텝은 " + max + " mm 이하(한도)";
  inp.classList.toggle("bad", !!why);
  inp.dataset.why = why;
  if (why) { JOG.stepBad = why; inp.setAttribute("data-tip", why); if (JOG.held && JOG.held.src !== "key") jogStop(); return; }
  inp.setAttribute("data-tip", "스텝을 직접 입력합니다(mm). 입력하는 즉시 적용됩니다.");
  JOG.step = v; JOG.stepBad = "";
  jogMarkStepBtns();
  document.querySelectorAll(".jstep-in").forEach((i) => { if (i !== inp) { i.value = raw; i.classList.remove("bad"); } });
}
/* 스텝 열(패드 오른쪽): X · Y 스텝(설정 jog.steps 전부 + 직접 입력) · Z 스텝(jog.z_steps, 따로). 패드와 함께 한 번만 만들고 설정이 바뀌면 버튼만 다시 채운다 */
function jogStepBtns(s, col) {
  const jc = s.settings.jog || {}, steps = jc.steps || [0.1, 1, 10, 50], max = jc.max_mm || 50, zs = jc.z_steps || [0.05, 0.1, 0.5, 1];
  const sig = JSON.stringify([steps, zs, max, jogRepeatMax()]);
  if (col.dataset.sig === sig) return;
  col.dataset.sig = sig;
  if (!zs.includes(JOG.zstep)) JOG.zstep = zs.includes(0.1) ? 0.1 : zs[0];
  const inp = col.querySelector(".jstep-in");
  inp.max = max;
  inp._max = max;
  col.querySelector(".jxy").replaceChildren(...steps.map((v) => h("button", { tabindex: -1, class: "btn sm jstep" + (JOG.step === v ? " on" : ""), "data-v": v, text: String(v), onclick: () => jogSetStep(v),
    "data-tip": v > jogRepeatMax() ? "이 스텝은 누를 때마다 한 번만 움직입니다(누르고 있어도 연속으로 가지 않음)." : "0.5 초 이상 누르고 있으면 연속으로 움직입니다." })));
  col.querySelector(".jz").replaceChildren(...zs.map((v) => h("button", { tabindex: -1, class: "btn sm jzstep" + (JOG.zstep === v ? " on" : ""), "data-v": v, text: String(v), onclick: () => jogSetZStep(v), "data-tip": "Z 버튼이 한 번에 움직이는 거리입니다." })));
}
/* 원점 없이 이동 허용: 켤 때는 확인 창(위험 설명), 끌 때는 바로 */
function jogFreeToggle() {
  const st = UI.state && UI.state.stage;
  if (!st) { toast("스테이지 연결 안 됨", true); return; }
  if (st.free_move) { send("free_move", { on: false }); return; }
  dialog("원점 없이 이동을 허용할까요?", "Z 높이를 모릅니다. 핀이 내려가 있으면 X · Y 이동이 샘플을 긁을 수 있습니다. 끝단 범위를 모르므로 끝단에 부딪힐 수 있습니다. 먼저 Z↑ 로 핀을 올리고 보면서 조금씩 움직이십시오. 조그(패드 · 키보드 · 스텝 · 직접 거리)만 되고 좌표 이동 · 측정은 원점이 있어야 합니다. 비상정지 · 정지는 그대로입니다.",
    [{ label: "취소" }, { label: "허용", cls: "danger", fn: () => send("free_move", { on: true }) }]);
}
function jogStepsCol() {
  const inp = h("input", { type: "number", class: "jstep-in", min: 0.01, step: "any", value: JOG.step, "data-tip": "스텝을 직접 입력합니다(mm). 입력하는 즉시 적용됩니다." });
  inp.addEventListener("input", () => jogCheckStepInput(inp, inp._max || 50));
  return h("div", { class: "col jsteps" },
    h("span", { class: "sub3", text: "X · Y 스텝 (mm)" }), h("div", { class: "jbtns jxy" }),
    h("div", { class: "row jin" }, inp, h("span", { class: "sub3", text: "직접" })),
    h("span", { class: "sub3", text: "Z 스텝 (mm)" }), h("div", { class: "jbtns jz" }));
}

/* 패드: 키(opt.key)마다 한 번만 만든다. opt.why = 비활성 사유(없으면 활성), opt.free = 접촉 높이 화면, opt.ztop = {act, why}.
   다시 부르면 같은 노드를 제자리에서 고쳐 돌려준다. */
function jogPad(s, opt) {
  opt = opt || {};
  const key = opt.key || "m";
  let el = JOG.pads[key];
  if (!el) {
    const dirBtn = (slot) => { const b = h("button", { tabindex: -1, class: "btn jogb", "data-slot": slot }); jogBindButton(b); return b; };
    const zBtn = (lab, sign) => { const b = h("button", { tabindex: -1, class: "btn jogb", text: lab, "data-axis": "z", "data-sign": sign }); jogBindButton(b); return b; };
    const stop = h("button", { tabindex: -1, class: "btn jogb jstop", "data-act": "stop", text: "정지",
      "data-tip": "움직이는 모터를 지금 멈추고 이어서 하려던 이동을 모두 취소합니다. 잠그지 않으며 히트건 · 원점은 그대로입니다. 완전히 끄려면 비상정지." });
    const b = { up: dirBtn("up"), down: dirBtn("down"), left: dirBtn("left"), right: dirBtn("right") };
    const pad = h("div", { class: "pad" }, h("span"), b.up, h("span"), b.left, h("span", { class: "padc" }, stop), b.right, h("span"), b.down, h("span"));
    const zUp = zBtn("Z ↑", -1), zDn = zBtn("Z ↓", 1);
    const zt = opt.ztop ? btn("Z 맨 위", opt.ztop.act, null, "jogb", "Z 를 맨 위로 올립니다.", opt.ztop.why || "") : null;
    /* 원점 없이 이동 허용(원점이 없거나 비상정지 잠금일 때만 보임): 상태는 서버 것을 그대로 - 체크 칸은 누름만 받는다 */
    const fm = h("input", { type: "checkbox", tabindex: -1 });
    const fml = h("label", { class: "chk freemv hidden", "data-tip": "원점이 없거나 비상정지 잠금일 때 사람이 보면서 하는 조그만 허용합니다(켤 때 확인 창). 좌표 이동 · 측정은 원점이 있어야 합니다." }, fm, "원점 없이 이동 허용");
    fm.addEventListener("click", (e) => { e.preventDefault(); jogFreeToggle(); });
    const sc = jogStepsCol();
    /* 체크 칸은 패드 · Z 아래 빈 자리에(스텝 열 끝에 두면 그 열이 패드보다 길어져 낮은 창에서 측정 카드의 [측정 시작] 을 밀어냈다) */
    el = h("div", { class: "jogpad row" }, h("div", { class: "col jleft" }, h("div", { class: "row jpz" }, pad, h("div", { class: "col zpad" }, zUp, zDn, zt)), fml), sc);
    el._j = { b, zUp, zDn, zt, stop, ztopOpt: opt.ztop, fm, fml };
    JOG.pads[key] = el;
  }
  jogPadUpdate(el, s, opt);
  return el;
}
function jogPadUpdate(el, s, opt) {
  const j = el._j, why = opt.why || "", dirs = jogLayout(s);
  for (const slot of ["up", "down", "left", "right"]) {
    const [axis, sign] = dirs[slot], b = j.b[slot], sg = axis.toUpperCase() + (sign > 0 ? "+" : "−");
    b.dataset.axis = axis; b.dataset.sign = sign;
    const t = JOG_ARROW[slot] + " " + sg;
    if (b.textContent !== t) b.textContent = t;
    b.setAttribute("data-tip", why || (sg + " 방향으로 스텝만큼 이동합니다. 스텝이 " + jogRepeatMax() + " mm 이하면 0.5 초 이상 눌러 연속으로 움직일 수 있습니다(키보드 " + JOG_ARROW[slot] + ")."));
    b.classList.toggle("dis", !!why);
  }
  j.zUp.setAttribute("data-tip", why || "Z 를 위로 한 스텝 이동합니다(PageUp).");
  j.zDn.setAttribute("data-tip", why || "Z 를 아래로 한 스텝만 이동합니다(PageDown). 누르고 있어도 연속으로 내려가지 않습니다.");
  j.zUp.classList.toggle("dis", !!why); j.zDn.classList.toggle("dis", !!why);
  if (j.zt && opt.ztop) { const zw = opt.ztop.why || ""; j.zt.classList.toggle("dis", !!zw); if (zw) { j.zt.removeAttribute("data-act"); j.zt.setAttribute("data-tip", zw); } else { j.zt.setAttribute("data-act", opt.ztop.act); j.zt.setAttribute("data-tip", "Z 를 맨 위로 올립니다."); } }
  const st = s.stage;
  if (j.fml) {
    const need = !!(st && (st.free_move || !(st.z_ok && st.xy_ok) || st.locked));
    j.fml.classList.toggle("hidden", !need);
    j.fm.checked = !!(st && st.free_move);
    j.fml.classList.toggle("on", !!(st && st.free_move));
  }
  j.stop.classList.toggle("mv", !!(st && st.moving));                       // 모터가 움직이는 동안은 붉은 채움(색만 바꾼다)
  j.stop.classList.toggle("dis", !st);                                      // 꺼지는 사유는 "스테이지 연결 안 됨" 하나뿐
  if (!st) j.stop.setAttribute("data-tip", "스테이지 연결 안 됨");
  const wasOff = el.hasAttribute("data-off");
  if (why) { el.setAttribute("data-off", "1"); el.dataset.why = why; } else { el.removeAttribute("data-off"); delete el.dataset.why; }
  el.setAttribute("data-tip", why ? why : "키보드 조그: 패드의 방향 · Z 버튼을 누르면 켜집니다(화살표 · PageUp/PageDown).");
  if (opt.free) el.setAttribute("data-free", "1"); else el.removeAttribute("data-free");
  if (why && !wasOff) { if (JOG.held && JOG.held.pad === el) jogStop(); if (JOG.kbd) jogKbdOff(); }       // 패드가 꺼지면 키보드 조그도 꺼진다
  jogStepBtns(s, el.querySelector(".jsteps"));
}
UI.renders.push(() => jogApplyKbd());
