/* jog.js - 이동(조그) 부품. 수동 탭의 이동 패널과 준비 단계가 같은 코드를 쓴다.
   jogPart() → {el, update()}. 한 번 누르면 정한 거리만큼 움직인다(이동 중에 누르면 서버가 대기 칸에 받아 둔다). */
"use strict";

function jogPart() {
  const st = { step: 1 };
  const STEPS = [0.1, 1, 10];
  const custom = h("input", { type: "number", class: "num", min: "0", step: "any", placeholder: "직접",
    oninput: () => { const v = parseFloat(custom.value); if (v > 0) { st.step = v; sync(); } } });
  const stepBtns = STEPS.map((v) => btn(String(v), () => { st.step = v; custom.value = ""; sync(); }));
  function sync() { stepBtns.forEach((b, i) => b.classList.toggle("on", !custom.value && st.step === STEPS[i])); }
  sync();
  const jog = (axis, sign) => () => api("jog", { axis, mm: sign * st.step });
  const moveBtns = [btn("Y +", jog("y", 1), "u"), btn("X −", jog("x", -1), "l"), btn("X +", jog("x", 1), "r"), btn("Y −", jog("y", -1), "d"),
    btn("Z 위로", jog("z", -1)), btn("Z 아래로", jog("z", 1)), btn("핀 맨 위로 (Z 0)", () => api("z_top"))];
  const why = h("div", { class: "why hidden" });
  const el = h("div", { class: "jog" },
    h("div", { class: "steps" }, small("한 번 누르면"), ...stepBtns, custom, small("mm")),
    h("div", { class: "jog-body" },
      h("div", { class: "pad" }, ...moveBtns.slice(0, 4)),
      h("div", { class: "zcol" }, ...moveBtns.slice(4))),
    why);
  function update() {
    const block = running() ? "측정 중에는 손으로 움직일 수 없습니다" : "";       // 서버도 거절한다 - 화면은 이유만 미리 보여 준다
    moveBtns.forEach((b) => { b.disabled = !!block; });
    why.textContent = block;
    why.classList.toggle("hidden", !block);
  }
  return { el, update, state: st };
}
