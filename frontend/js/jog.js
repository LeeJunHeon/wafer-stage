/* jog.js - 조그 패드 하나(측정 화면 · 장비 설정 모두 이 컴포넌트).
   배치는 좌표 보정에서 계산한다: 스테이지 +X · +Y 가 사진에서 가리키는 방향을 위 · 아래 · 왼 · 오른쪽 중 가장 가까운 쪽에 둔다.
   좌표 보정이 없으면 기존 프로그램 배치(위 X+ · 왼 Y+ · 오른 Y− · 아래 X−). 키보드 화살표도 같은 매핑, PageUp/PageDown = Z.
   동작은 기존 프로그램 jog.js 와 같다: 한 번 누름 = 한 스텝, 0.5 s 이상 누르고 있으면 ack 를 받은 뒤 다음 스텝(큐에 쌓지 않는다),
   거절(끝단 · 이동 중 · 잠금)이면 멈춤, 키보드는 400 ms 데드맨, 창 포커스를 잃으면 멈춘다. */
"use strict";
const JOG = { step: 1, held: null, inFlight: false, lastKeyAt: 0, keySeq: 0, sentSeq: -1, heldSince: 0, timer: null, dirs: null, enabled: false };
const JOG_KEY_ALIVE_MS = 400, JOG_HOLD_REPEAT_MS = 500;

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

function jogStop() {
  JOG.held = null;                                  // 이미 나간 한 스텝은 끝까지 간다(중간에 못 끊는다)
  if (JOG.timer) { clearTimeout(JOG.timer); JOG.timer = null; }
}
async function jogSend() {
  const h_ = JOG.held;
  if (!h_ || JOG.inFlight || !JOG.enabled) return;
  JOG.inFlight = true;
  JOG.sentSeq = JOG.keySeq;
  let r;
  try { r = await send("jog", { axis: h_.axis, mm: h_.sign * JOG.step }); } catch (e) { r = { ok: false }; }
  JOG.inFlight = false;
  if (!r || r.ok === false) { jogStop(); return; }                        // 거절(끝단 · 이동 중 · 잠금)이면 이어 보내지 않는다
  const cur = JOG.held;
  if (!cur) return;
  if (cur.src === "key" && (JOG.keySeq === JOG.sentSeq || Date.now() - JOG.lastKeyAt > JOG_KEY_ALIVE_MS)) { jogStop(); return; }   // 새 keydown 없이는 이어지지 않는다
  if (cur.src === "pointer") {
    const left = JOG_HOLD_REPEAT_MS - (Date.now() - JOG.heldSince);        // 클릭 한 번은 정확히 한 스텝, 0.5 s 이상 눌러야 연속
    if (left > 0) {
      if (JOG.timer) clearTimeout(JOG.timer);
      JOG.timer = setTimeout(() => { JOG.timer = null; if (JOG.held && JOG.held.src === "pointer") jogSend(); }, left);
      return;
    }
  }
  jogSend();
}
function jogStart(axis, sign, src) {
  if (!JOG.enabled) return;
  if (JOG.timer) { clearTimeout(JOG.timer); JOG.timer = null; }
  JOG.held = { axis, sign, src };
  JOG.heldSince = Date.now();
  jogSend();
}
window.addEventListener("mouseup", jogStop);
window.addEventListener("blur", jogStop);
document.addEventListener("visibilitychange", () => { if (document.hidden) jogStop(); });

/* 키보드: 화면에 켜진 패드가 있을 때만(입력 칸에 글자 치는 중이면 제외) */
function jogPadVisible() { return JOG.enabled && Array.from(document.querySelectorAll(".jogpad")).some((p) => p.offsetParent !== null); }
document.addEventListener("keydown", (e) => {
  if (!jogPadVisible()) return;
  const t = e.target;
  if (t && /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName)) return;
  const map = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" };
  let m = null;
  if (map[e.key] && JOG.dirs) m = JOG.dirs[map[e.key]];
  else if (e.key === "PageUp") m = ["z", -1];
  else if (e.key === "PageDown") m = ["z", 1];
  if (!m) return;
  e.preventDefault();                                // 화살표 키가 스크롤이나 선택 칸을 바꾸지 않는다
  JOG.lastKeyAt = Date.now(); JOG.keySeq++;
  if (!JOG.held || JOG.held.axis !== m[0] || JOG.held.sign !== m[1]) jogStart(m[0], m[1], "key");
  else if (!JOG.inFlight) jogSend();
});
document.addEventListener("keyup", (e) => { if (JOG.held && JOG.held.src === "key" && /^(Arrow|Page)/.test(e.key)) jogStop(); });

/* 스텝 고르기: 설정 jog.steps 전부 + 직접 입력(mm). 고르면 화면 안의 모든 스텝 버튼에 바로 반영한다 */
function jogSetStep(v) {
  if (!(v > 0)) return;
  JOG.step = v;
  document.querySelectorAll(".jstep").forEach((b) => b.classList.toggle("on", Number(b.dataset.v) === v));
  document.querySelectorAll(".jstep-in").forEach((i) => { if (document.activeElement !== i) i.value = v; });
}
function jogStepRow(s) {
  const steps = (s.settings.jog && s.settings.jog.steps) || [0.1, 1, 10, 50], max = (s.settings.jog && s.settings.jog.max_mm) || 50;
  const inp = h("input", { type: "number", class: "jstep-in", min: 0.01, max, step: "any", value: JOG.step, "data-tip": "스텝을 직접 입력합니다(mm).",
    onchange: (e) => { const v = Number(e.target.value); if (v >= 0.01 && v <= max) jogSetStep(v); else { e.target.value = JOG.step; toast("스텝 0.01 ~ " + max + " mm", true); } } });
  return h("div", { class: "row jstep-row" }, ...steps.map((v) => h("button", { class: "btn sm jstep" + (JOG.step === v ? " on" : ""), "data-v": v, text: String(v), onclick: () => jogSetStep(v) })), inp, h("span", { class: "sub3", text: "mm" }));
}

/* 패드: opt.why = 비활성 사유(없으면 활성) */
function jogPad(s, opt) {
  opt = opt || {};
  const dirs = jogLayout(s), why = opt.why || "";
  JOG.dirs = dirs; JOG.enabled = !why;
  const b = (slot) => {
    const [axis, sign] = dirs[slot];
    const lab = JOG_ARROW[slot] + " " + axis.toUpperCase() + (sign > 0 ? "+" : "−");
    const el = h("button", { class: "btn jogb" + (why ? " dis" : ""), text: lab, "data-tip": why || (axis.toUpperCase() + (sign > 0 ? "+" : "−") + " 방향으로 스텝만큼 이동합니다. 0.5 초 이상 누르면 연속으로 이동합니다(키보드 " + JOG_ARROW[slot] + ").") });
    if (!why) { el.addEventListener("mousedown", (e) => { e.preventDefault(); jogStart(axis, sign, "pointer"); }); }
    return el;
  };
  const z = (lab, sign, key) => {
    const el = h("button", { class: "btn jogb" + (why ? " dis" : ""), text: lab, "data-tip": why || ("Z 를 " + (sign < 0 ? "위로" : "아래로") + " 스텝만큼 이동합니다(" + key + ")") });
    if (!why) el.addEventListener("mousedown", (e) => { e.preventDefault(); jogStart("z", sign, "pointer"); });
    return el;
  };
  const pad = h("div", { class: "pad" }, h("span"), b("up"), h("span"), b("left"), h("span", { class: "padc" }), b("right"), h("span"), b("down"), h("span"));
  return h("div", { class: "jogpad row" }, pad, h("div", { class: "col zpad" }, z("Z ↑", -1, "PageUp"), z("Z ↓", 1, "PageDown")));
}
