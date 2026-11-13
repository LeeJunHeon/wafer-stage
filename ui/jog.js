/* jog.js - 이동(조그) 부품. 수동 측정 탭의 이동 패널과 초기 설정 단계가 같은 코드를 쓴다.
   jogPart() → {el, update()}. 한 번 누르면 이동 거리만큼 움직인다(이동 중에 누르면 서버가 대기 칸에 받아 둔다).
   X · Y 십자 가운데의 [정지] 는 이동만 멈춘다(잠그지 않는다 - 비상정지와 다르다). 명령 줄을 거치지 않는 /api/stop 으로 바로 보낸다. */
"use strict";

let lastStop = 0;
async function stopMove() {
  const now = performance.now();
  if (now - lastStop < 300) return;                              // 누른 순간(pointerdown)과 click 이 겹쳐도 한 번만
  lastStop = now;
  try {
    const r = await fetch("/api/stop", { method: "POST", keepalive: true });
    const j = await r.json();
    if (!j.ok && j.msg) toast(j.msg);
  } catch (e) { toast("서버 전송 실패"); }
}

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
    btn("Z 상승", jog("z", -1)), btn("Z 하강", jog("z", 1)), btn("핀 상승", () => api("z_top"))];
  const stopBtn = btn("정지", stopMove, "c stop");
  stopBtn.addEventListener("pointerdown", (e) => { if (!stopBtn.disabled) { e.preventDefault(); stopMove(); } });      // 손을 떼기 전에 바로 보낸다
  const why = h("div", { class: "why hidden" });
  const el = h("div", { class: "jog" },
    h("div", { class: "steps" }, small("이동 거리"), ...stepBtns, custom, small("mm")),
    h("div", { class: "jog-body" },
      h("div", { class: "pad" }, ...moveBtns.slice(0, 4), stopBtn),
      h("div", { class: "zcol" }, ...moveBtns.slice(4))),
    why);
  function update() {
    const run = running() ? "측정 중 사용 불가" : "";         // 서버도 거절한다 - 화면은 이유만 미리 보여 준다
    const block = run || (S.estop_locked ? "비상정지 해제 필요" : "");      // 비상정지 중에는 이동 버튼이 모두 꺼진다
    moveBtns.forEach((b) => { b.disabled = !!block; });
    stopBtn.disabled = !!run || !devOk("stage");                 // 스테이지가 연결돼 있으면 항상 누를 수 있다(측정 중에는 꺼진다)
    stopBtn.classList.toggle("moving", !stopBtn.disabled && !!(S && S.move));     // 이동 중에는 빨강 테두리
    stopBtn.title = run ? "측정 중 · 측정 중지 또는 비상정지 사용" : !devOk("stage") ? "스테이지 연결 필요 (초기 설정 1)" : "";
    why.textContent = block;
    why.classList.toggle("hidden", !block);
  }
  return { el, update, state: st };
}
