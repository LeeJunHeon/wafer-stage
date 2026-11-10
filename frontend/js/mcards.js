/* mcards.js - 측정 화면(하나): 오른쪽 카드 셋(위치 · 샘플 · 측정)과 전체 렌더.
   왼쪽은 위 카메라(measure.js) · 아래 R–T 그래프 두 개(rt.js). '이 위치 측정' · '샘플 순회' 하위 탭은 없다:
   측정할 곳 [지금 위치 | 샘플 N개] 로 고른다. 측정 동작(rt.py · measure.py) · 결과 · 파일 형식은 그대로다. */
"use strict";
const MC = { mv: { x: "", y: "" }, mvVer: 0, mvApplied: 0, pos: null, mvPx: null, target: "here", sampleNo: null, sigs: {}, showSteps: false };

function cardHead(title, ...right) { return h("div", { class: "chead" }, h("h3", { text: title }), h("span", { class: "grow" }), ...right.filter(Boolean)); }

/* 조그 패드가 꺼지는 이유(없으면 ""): 서버 판단 하나(jog_block)를 그대로 쓴다. "이동 중" 은 사유가 아니다(서버가 거절하고 [정지] 가 강조된다) */
function padWhy(s) {
  const st = s.stage, rl = typeof rtLock === "function" ? rtLock(s) : "";
  if (!st) return "스테이지 연결 안 됨";
  if (st.free_move) return rl || st.jog_block;                                // 원점 없이 이동 허용: 원점 · 잠금은 사유가 아니다(조그만)
  return rl || st.jog_block || (st.locked ? "비상정지 잠금" : "") || (!(st.z_ok && st.xy_ok) ? "원점 필요 · 스테이지 설정" : "");
}
/* 이동 명령(이동 · 샘플 위로 · Z 맨 위 …)이 안 되는 이유(없으면 "") */
function stageWhy(s) {
  const st = s.stage, rl = typeof rtLock === "function" ? rtLock(s) : "";
  if (!st) return "스테이지 연결 안 됨";
  return rl || st.move_block || (st.locked ? "비상정지 잠금" : "") || (!(st.z_ok && st.xy_ok) ? "원점 필요 · 스테이지 설정" : "");
}
/* 가동범위는 서버의 한 값(드라이버의 지금 값 - 설정 + 펌웨어 한계 자르기)만 쓴다 */
function limitsXY(s) { const l = s.stage && s.stage.limits; if (!l) return [247.6, 247.8]; return Array.isArray(l) ? [l[0], l[1]] : [l.x_max_mm, l.y_max_mm]; }

/* ---- 사진 좌표 ↔ 스테이지 좌표(좌표 보정의 판 영역 네 점으로 근사) ---- */
function areaQuads(s) {
  const c = s.calib, sh = s.shot, px = (M.mode === "shot" && sh && sh.area) ? sh.area : c.area;
  return c && c.area_mm && px ? { mm: c.area_mm, px } : null;
}
function pxToMm(s, u, v) { const q = areaQuads(s); if (!q) return null; return projective(q.px, q.mm)(u, v); }
function mmToPx(s, x, y) { const q = areaQuads(s); if (!q) return null; return projective(q.mm, q.px)(x, y); }

/* ---- 카드 1 위치 ----
   한 번만 만들고(buildPos) 이후에는 제자리에서 글자 · class · 속성만 바꾼다(updatePos). 입력 칸 · 패드 · 선택 칸은 다시 그려지지 않으므로
   친 글자 · 커서 · 포커스 · 누르고 있는 패드가 유지된다. */
function setBtn(el, why, act, tip) {
  el.classList.toggle("dis", !!why);
  if (why) { el.removeAttribute("data-act"); el.setAttribute("data-tip", why); } else { if (act) el.setAttribute("data-act", act); if (tip) el.setAttribute("data-tip", tip); }
}
function posMenu(s) {
  const why = stageWhy(s), st = s.stage;
  const saveHere = (name, label) => ({ label: "지금 위치를 " + label + " 위치로 저장", fn: async () => { const r = await send("save_here", { name }); if (r.ok) toast(label + " 위치 저장"); }, why: !st ? "스테이지 연결 안 됨" : (st.moving ? "이동 중 · 끝난 뒤" : "") });
  return [
    { label: "대기 위치로 이동", fn: () => send("goto_named", { name: "park" }), why },
    { label: "촬영 위치로 이동", fn: () => send("goto_named", { name: "capture" }), why },
    saveHere("park", "대기"), saveHere("capture", "촬영"),
  ];
}
function buildPos(s) {
  const P = MC.pos = {};
  P.pos = h("b", { class: "mono nowrap", id: "m-pos", text: "—" });
  P.contact = h("span", { class: "sub nowrap" });
  const inp = (k, id) => h("input", { type: "number", class: "mvin", id, step: "any", value: MC.mv[k], placeholder: k.toUpperCase() + " mm",
    oninput: (e) => { MC.mv[k] = e.target.value; MC.mvVer = (MC.mvVer || 0); updatePos(UI.state); } });
  P.x = inp("x", "m-x"); P.y = inp("y", "m-y");
  P.sel = h("select", { class: "msel grow", onchange: (e) => { MC.sampleNo = e.target.value ? Number(e.target.value) : null; updatePos(UI.state); } });
  P.move = btn("이동", "m-move", null, "sm primary", "입력한 X · Y 로 옮깁니다. 사진에 이동 목표 십자가 먼저 보입니다. Z 를 먼저 맨 위로 올린 뒤 움직입니다.");
  P.fill = btn("지금 좌표 넣기", "m-fillnow", null, "sm", "지금 스테이지 좌표를 X · Y 칸에 넣습니다.");
  P.goto = btn("샘플 위로", "m-goto", null, "sm", "고른 샘플 위로 공구를 옮깁니다(좌표 보정이 있을 때).");
  P.down = btn("접촉 높이로 내리기", "rt-down", null, "sm primary", "");
  P.pad = jogPad(s, { key: "m", why: "", ztop: { act: "rt-ztop", why: "" } });
  P.more = moreBtn(() => posMenu(UI.state));
  P.selSig = null;
  M.cardPos.replaceChildren(
    h("div", { class: "chead" }, h("h3", { text: "위치" }), P.pos, jogKbdTag(), h("span", { class: "grow" }), P.contact, P.more),
    P.pad,
    h("div", { class: "row" }, h("span", { class: "sub", text: "X" }), P.x, h("span", { class: "sub", text: "Y" }), P.y, P.move, P.fill),
    h("div", { class: "row" }, P.sel, P.goto, P.down));
}
function updatePos(s) {
  if (!MC.pos) buildPos(s);
  const P = MC.pos, st = s.stage, why = stageWhy(s), lim = limitsXY(s);
  P.contact.textContent = s.contact.z == null ? "접촉 높이 없음" : "접촉 Z " + f1(s.contact.z, 2);
  P.contact.setAttribute("data-tip", s.contact.z == null ? "저장된 접촉 높이가 없습니다(장비 설정 7)." : "저장된 접촉 높이(절대 Z)입니다.");
  if (MC.mvApplied !== MC.mvVer) {                                          // 프로그램이 넣은 좌표(사진 클릭 · 지금 좌표 넣기)만 칸에 반영한다 - 친 글자는 건드리지 않는다
    MC.mvApplied = MC.mvVer;
    if (P.x.value !== MC.mv.x) P.x.value = MC.mv.x;
    if (P.y.value !== MC.mv.y) P.y.value = MC.mv.y;
  }
  const xv = parseFloat(P.x.value), yv = parseFloat(P.y.value);
  const bad = isNaN(xv) || isNaN(yv) ? "X · Y 를 입력" : (xv < 0 || xv > lim[0] || yv < 0 || yv > lim[1]) ? "가동범위 밖 · X 0~" + f1(lim[0], 0) + " · Y 0~" + f1(lim[1], 0) : "";
  setBtn(P.move, why || bad, "m-move");
  setBtn(P.fill, st ? "" : "스테이지 연결 안 됨", "m-fillnow");
  const ssig = JSON.stringify(s.samples.filter((p) => p.X != null).map((p) => [p.no, Math.round(p.X), Math.round(p.Y)]));
  if (P.selSig !== ssig) {                                                  // 샘플 목록이 바뀔 때만 선택 칸 안을 다시 채운다
    P.selSig = ssig;
    P.sel.replaceChildren(h("option", { value: "", text: "샘플 선택" }), ...s.samples.filter((p) => p.X != null).map((p) => h("option", { value: p.no, text: p.no + "번 · " + Math.round(p.X) + ", " + Math.round(p.Y) })));
    if (MC.sampleNo != null && !s.samples.some((p) => p.no === MC.sampleNo && p.X != null)) MC.sampleNo = null;
  }
  const want = MC.sampleNo == null ? "" : String(MC.sampleNo);
  if (P.sel.value !== want) P.sel.value = want;
  setBtn(P.goto, why || (MC.sampleNo == null ? "샘플을 먼저 고름" : ""), "m-goto");
  setBtn(P.down, why || (s.contact.z == null ? "접촉 높이 필요 · 장비 설정 7" : ""), "rt-down",
    "저장한 접촉 높이까지 내립니다. 지금 X · Y 에서 그대로 내리니 핀이 샘플 위인지 눈으로 확인하십시오. 마지막 " + s.contact.approach + " mm 는 느리게 내립니다.");
  jogPad(s, { key: "m", why: padWhy(s), ztop: { act: "rt-ztop", why } });
}
UI.acts["m-move"] = () => send("move_xy", { x: parseFloat(MC.pos.x.value), y: parseFloat(MC.pos.y.value) });
UI.acts["m-fillnow"] = () => { const st = UI.state.stage; if (!st) return; MC.mv.x = f1(st.x, 1); MC.mv.y = f1(st.y, 1); MC.mvPx = null; MC.mvVer = (MC.mvVer || 0) + 1; updatePos(UI.state); if (M.viewer) redrawOverlay(); };
UI.acts["m-goto"] = () => { if (MC.sampleNo != null) send("goto_sample", { no: MC.sampleNo }); };

/* ---- 샘플: 카메라 머리줄의 요약 칩 + 번호 칩 목록 팝업(측정 대상 켜고 끄기) ---- */
function sampleSummary(s) {
  const n = s.samples.length, nt = s.samples.filter((p) => !p.excluded).length;
  return n ? "샘플 " + n + " · 확인 필요 " + (s.n_weak || 0) + " · 측정 대상 " + nt + "/" + n : "샘플 없음";
}
function closeSamPop() { const p = document.getElementById("sampop"); if (p) p.remove(); MC.samOpen = false; }
function drawSamPop(s) {
  const pop = document.getElementById("sampop");
  if (!pop) return;
  const running = isRunning(s), sel = s.samples.find((p) => p.no === s.sel);
  const grid = h("div", { class: "sgrid" }, s.samples.map((p) => {
    const cb = h("input", { type: "checkbox", checked: !p.excluded, disabled: running, "data-tip": running ? "측정 중 · 변경 못 함" : "켜면 측정 대상, 끄면 제외합니다.",
      onchange: (e) => send("sample_exclude", { no: p.no, on: !e.target.checked }) });
    return h("div", { class: "sc" + (p.no === s.sel ? " sel" : "") + (p.weak ? " weak" : "") + (p.excluded ? " exc" : "") },
      h("label", { class: "scl" }, cb, h("b", { text: p.no })),
      h("span", { class: "scx", "data-act": "sel", "data-args": JSON.stringify({ no: p.no }), text: p.X == null ? "—" : Math.round(p.X) + ", " + Math.round(p.Y) }));
  }));
  const selrow = sel ? h("div", { class: "selrow" }, h("b", { text: sel.no + "번" }), h("span", { class: "mono", text: xy(sel.X, sel.Y) }), h("span", { class: "grow" }),
    sel.weak ? btn("확인 완료", "confirm_s", { no: sel.no }, "sm", "확인 필요 표시를 끕니다.") : null, btn("삭제", "delete", { no: sel.no }, "sm", "이 샘플을 지웁니다(되돌리기로 되살립니다).", running ? "측정 중" : "")) : null;
  pop.replaceChildren(...[h("div", { class: "pophead" }, h("b", { text: sampleSummary(s) }), h("span", { class: "sub3", text: "체크 = 측정 대상 · 번호 아래 좌표를 누르면 선택" })),
    s.samples.length ? grid : h("div", { class: "sub3", text: "촬영하면 샘플이 보입니다" }), selrow].filter(Boolean));
}
UI.acts["sam-pop"] = (a, el) => {
  if (document.getElementById("sampop")) { closeSamPop(); return; }
  closePop();
  const r = el.getBoundingClientRect(), pop = h("div", { class: "pop sampop", id: "sampop" });
  pop.style.left = Math.max(8, Math.min(window.innerWidth - 400, r.left)) + "px"; pop.style.top = r.bottom + 4 + "px";
  $("#stage").append(pop); MC.samOpen = true;
  drawSamPop(UI.state);
};
document.addEventListener("pointerdown", (e) => { if (MC.samOpen && !(e.target.closest && e.target.closest("#sampop, [data-act=\"sam-pop\"]"))) closeSamPop(); }, true);

/* ---- 카드 3 측정 ---- */
function rtSummary(s) {
  const r = s.settings.rt;
  return f1(r.volt, 2) + " V · 최고 " + f1(r.max_temp, 0) + " °C · 반복 " + r.repeat + "회 · 가열 " + (r.heat_rate > 0 ? f1(r.heat_rate, 1) + " °C/분" : "최대") + " · 냉각 " + (r.cool_rate > 0 ? f1(r.cool_rate, 1) + " °C/분" : "최대");
}
function cardRun(s) {
  const rt = s.rt, run = rt.run, tour = s.run, running = isRunning(s);
  const rtActive = run && (run.state === "running" || run.state === "stopping");
  const conds = s.conditions, act = M.cond && conds.find((c) => c.name === M.cond) ? M.cond : s.active_condition;
  const cv = conds.find((c) => c.name === act) || conds[0];
  const tgt = running ? "samples" : rtActive ? "here" : MC.target;
  const locked = running || rtActive;
  const nTarget = s.samples.filter((p) => !p.excluded).length;
  const seg = h("div", { class: "seg mtarget" }, btn("지금 위치", "m-target", { t: "here" }, tgt === "here" ? "on" : "", "핀이 지금 있는 자리에서 저항–온도를 잽니다.", locked ? "측정 중" : ""),
    btn("샘플 " + s.samples.length + "개", "m-target", { t: "samples" }, tgt === "samples" ? "on" : "", "촬영한 샘플을 차례로 돌며 측정 단계대로 측정합니다.", locked ? "측정 중" : (s.samples.length ? "" : "촬영한 샘플 없음")));
  const top = [h("div", { class: "chead" }, h("h3", { text: "측정" }), seg, h("span", { class: "grow" }), btn("조건 편집", "m-cond", null, "sm", "전압 · 온도 · 속도 · 저장 위치 등 측정 조건을 고칩니다.", locked ? "측정 중 · 변경 잠김" : ""))];
  const mid = [], foot = [];
  /* 파일 이름 · 저장 위치(위 고정 줄 - 낮은 창에서도 잘리지 않는다. 가운데 스크롤은 조건 요약 · 단계만) */
  const r = s.settings.rt;
  const path = UI.nasPath(r.file_path, r.date_folder, r.batch_folder);
  top.push(h("div", { class: "row fnrow" }, h("span", { class: "sub nowrap", text: "파일" }),             /* 파일 이름 · 저장 위치 한 줄(낮은 창에서도 잘리지 않게) */
    h("input", { type: "text", class: "fnin", value: r.file_name, disabled: locked, "data-tip": "파일 이름의 앞부분입니다. 뒤에 전압과 회차 번호가 붙습니다" + (tgt === "samples" ? "(샘플 쪽은 샘플 번호도 붙습니다)." : "."),
      onchange: async (e) => { const rr = await send("settings_save", { patch: { "rt.file_name": e.target.value } }); if (!rr.ok) e.target.value = r.file_name; } }),
    h("span", { class: "sub nowrap", text: "저장" }), h("span", { class: "grow sub mono nasprev", id: "m-path", "data-tip": "저장 위치 · " + (path || "PC 에만 저장"), text: path || "PC 에만 저장" }),
    btn("변경", "m-cond", { cat: "save" }, "sm", "저장 위치를 바꿉니다.", locked ? "측정 중 · 변경 잠김" : "")));
  /* 조건 요약 · 측정 단계 고르기도 고정 줄. 가운데(안에서 스크롤)는 단계 목록 · 진행 단계만 */
  top.push(h("div", { class: "sub", id: "m-sum", text: rtSummary(s) }));
  if (tgt === "samples") {
    const sel_ = h("select", { class: "msel", disabled: locked, onchange: (e) => { M.cond = e.target.value; send("settings_save", { patch: { "condition.active": M.cond } }); } }, conds.map((c) => h("option", { value: c.name, text: c.name, selected: c.name === act })));
    top.push(h("div", { class: "row wrap" }, h("span", { class: "sub", text: "측정 단계" }), sel_, btn(MC.showSteps ? "단계 접기 ▴" : "단계 보기 ▾", "steps-toggle", null, "sm"), btn("편집", "cond-edit", null, "sm", "측정 단계를 고칩니다.", locked ? "측정 중" : "")),
      h("div", { class: "sub", text: cv ? estText(cv) : "" }));
    if (MC.showSteps && !running) mid.push(h("ol", { class: "steps" }, (cv ? cv.steps : []).map((st) => h("li", {}, h("span", { text: (st.i + 1) + ". " + st.text })))));
  }
  if (running && tour) mid.push(h("ul", { class: "steps msteps", id: "run-steps" }, tour.steps.map((st) => h("li", { class: st.state }, h("span", { text: (st.state === "done" ? "✓ " : "") + (st.i + 1) + ". " + st.text }), h("span", { class: "val", text: st.value || "" })))));
  /* 아래(늘 보임): 상태 숫자 · 진행 · 시작/중지 */
  foot.push(h("div", { class: "rtbig" }, h("div", {}, h("div", { class: "k", text: "온도" }), h("div", { class: "v", id: "m-T", text: "—" })), h("div", {}, h("div", { class: "k", text: "저항" }), h("div", { class: "v", id: "m-R", text: "—" })),
    h("div", {}, h("div", { class: "k", text: "전류" }), h("div", { class: "v", id: "m-I", text: "—" }))),
  h("div", { class: "row" }, h("b", { class: "phase", id: "m-phase", text: "대기" }), h("span", { class: "grow" }), h("span", { class: "sub nowrap", id: "m-el", text: "" }), h("span", { class: "chip", id: "m-heat", text: "히트건 —" }), h("span", { class: "chip", id: "m-cool", text: "쿨러 —" })));
  if (running && tour) {
    const cs = tour.cur_sample;
    foot.push(h("div", { class: "row" }, h("b", { class: "nowrap", text: "샘플 " + (tour.cur != null ? tour.cur + "번" : "—") }), h("span", { class: "mono nowrap", id: "m-cur", text: cs ? xy(cs.X, cs.Y) : "" }), h("span", { class: "grow" }), h("span", { class: "sub nowrap", id: "run-prog-t", text: "" })));
    const pct = tour.n ? (100 * tour.done) / tour.n : 0, bar = h("i"); bar.style.width = pct + "%";
    foot.push(h("div", { class: "bar" }, bar));
  }
  /* 시작 막는 사유 한 곳 한 줄 · 경고 한 줄 */
  const whyText = tgt === "here" ? rt.block : s.start_block.why;
  const gate = locked ? null : gateOf(whyText, s, { camera: tgt === "samples" && !s.samples.length, inst: tgt === "here" || (cv ? cv.steps : []).some((x) => x.type === "rt"), down: tgt === "here" });
  const warns = tgt === "here" || (run && run.mode === "rt") ? (run && run.warns) || [] : (tour && tour.warns) || [];
  if (gate) foot.push(gateRow(gate));
  if (warns.length) foot.push(h("div", { class: "why one", "data-tip": warns.join(" / "), text: warns[warns.length - 1] + (warns.length > 1 ? "  (+" + (warns.length - 1) + ")" : "") }));
  else if (!gate && tgt === "samples" && (s.start_block.notes || []).length && !running) foot.push(h("div", { class: "sub3", text: s.start_block.notes.join(" · ") }));
  /* 시작 · 중지 */
  let ctl;
  const blockWhy = gate ? "시작할 수 없음 · 위 안내 먼저" : (tgt === "samples" && !nTarget ? "측정 대상 없음" : "");
  if (running && tour) {
    const paused = tour.paused || tour.state === "paused";
    ctl = h("div", { class: "row" }, tour.waiting ? btn("다음", "manswer", { v: "next" }, "primary lg grow", "별도 프로그램으로 측정하는 동안 핀을 샘플에 댄 채 기다립니다. 측정을 마쳤으면 눌러 다음 단계로 이어갑니다.") : null,
      paused ? btn("다시 시작", "mresume", null, tour.waiting ? "lg" : "primary lg grow") : btn("일시정지", "mpause", null, "lg" + (tour.waiting ? "" : " grow")), btn("중지", "mstop", null, "danger lg"));
  } else if (tgt === "samples") ctl = h("div", { class: "row" }, btn("측정 시작 · " + nTarget + "개", "mstart", null, "primary lg grow", "선택한 측정 단계로 샘플을 차례로 측정합니다.", blockWhy), btn("중지", "mstop", null, "danger lg", "", "측정 중 아님"));
  else ctl = h("div", { class: "row" }, btn("측정 시작", "rt-start", null, "primary lg grow", "예비 냉각 → 출력 켬 → 가열 · 냉각 반복을 시작합니다. 핀이 접촉 높이에 있어야 합니다.", rtActive ? "측정 중" : blockWhy),
    btn("중지", "rt-stop", null, "danger lg", "측정을 멈추고 히트건 · 출력을 끕니다.", rtActive ? "" : "측정 중 아님"));
  foot.push(ctl);
  return h("div", { class: "card", id: "c-run" }, h("div", { class: "rtop" }, ...top), h("div", { class: "rmid" }, ...mid), h("div", { class: "rfoot" }, ...foot));
}
UI.acts["m-target"] = (a) => { MC.target = a.t; MC.sigs.run = ""; render(); };
UI.acts["m-cond"] = (a) => openSettings("rt", a && a.cat);

/* ---- 동적 값(버튼을 다시 만들지 않고 제자리에서) ---- */
function updateMeasureDynamic(s) {
  const set = (id, t) => { const e = document.getElementById(id); if (e && e.textContent !== t) e.textContent = t; };
  const st = s.stage, run = s.rt && s.rt.run, tour = s.run;
  set("m-pos", st ? "X " + f1(st.x) + " · Y " + f1(st.y) + " · Z " + f1(st.z, 2) : "—");
  const live = run && (run.state === "running" || run.state === "stopping");
  if (isRunning(s) && tour) {                                               // 샘플 측정 중: 샘플 쪽 상태만(이전 R–T 측정 결과 · '중지됨' 이 남지 않게)
    const cur = tour.steps.find((x) => x.state === "active" || x.state === "cur" || x.state === "running");
    set("m-T", live && run.T != null ? f1(run.T, 1) + " °C" : "—"); set("m-R", live && run.R != null ? fmtE(run.R) + " Ω" : "—"); set("m-I", live && run.I != null ? fmtE(run.I) + " A" : "—");
    set("m-phase", tour.state === "paused" || tour.paused ? "일시정지" : tour.waiting ? "확인 대기" : cur ? (cur.i + 1) + ". " + cur.text : "측정 중");
    set("m-el", "경과 " + mmss(tour.elapsed));
  } else if (run) {
    set("m-T", run.T == null ? "—" : f1(run.T, 1) + " °C"); set("m-R", run.R == null ? "—" : fmtE(run.R) + " Ω"); set("m-I", run.I == null ? "—" : fmtE(run.I) + " A");
    set("m-phase", live ? phaseLabel(run) : run.status ? run.status + (run.message && run.status !== "완료" && run.message !== run.status ? " · " + run.message : "") : "대기");
    set("m-el", live || run.status ? "경과 " + mmss(run.elapsed) : "");
  } else { set("m-T", "—"); set("m-R", "—"); set("m-I", "—"); set("m-phase", "대기"); set("m-el", ""); }
  const stOk = !!(s.dev && s.dev.stage && s.dev.stage.state === "ok");
  const hc = $("#m-heat"), cc = $("#m-cool");
  if (hc && cc) {
    const rl = s.relays || { owners: {}, heater_on: false };
    const co = Object.keys(rl.owners).filter((n) => Number(n) !== Number(rl.heater) && rl.owners[n]);
    const heat = live && run ? !!run.heater : rl.heater_on, cool = live && run ? (run.cooler || []).map(Number) : co.map(Number);
    const ht = stOk ? "히트건 " + (heat ? "켜짐" : "꺼짐") : "히트건 —", ct = stOk ? "쿨러 " + (cool.length ? "켜짐 R" + cool.join(",R") : "꺼짐") : "쿨러 —";
    if (hc.textContent !== ht) { hc.textContent = ht; hc.className = "chip" + (stOk && heat ? " warn" : ""); }
    if (cc.textContent !== ct) { cc.textContent = ct; cc.className = "chip" + (stOk && cool.length ? " ok" : ""); }
  }
  if (isRunning(s) && tour) {
    const li = $$("#run-steps li");
    tour.steps.forEach((x, i) => { const v = li[i] && li[i].querySelector(".val"); if (v && v.textContent !== (x.value || "")) v.textContent = x.value || ""; });
    set("run-prog-t", tour.done + " / " + tour.n + " · 경과 " + mmss(tour.elapsed) + " · 남은 시간 " + (tour.remain == null ? "—" : "약 " + mmss(tour.remain)));
    if (tour.cur_sample) set("m-cur", xy(tour.cur_sample.X, tour.cur_sample.Y));
  }
  if (T.dirty) { T.dirty = false; redrawGraphs(); }
}

/* ---- 전체 렌더 ---- */
function renderMeasure(s) {
  if (!M.prepBuilt) buildPrep();
  if (UI.tab !== "measure") return;
  const running = isRunning(s);
  renderLeft(s, running);
  const run = s.rt && s.rt.run;
  updatePos(s);                                                              // 위치 카드는 다시 만들지 않는다(제자리 갱신)
  if (MC.samOpen) drawSamPop(s);
  const tour = s.run;
  const sigRun = JSON.stringify([MC.target, MC.showSteps, M.cond, s.active_condition, s.conditions, s.settings.rt, s.settings.condition, s.dev, s.rt.block, s.start_block, s.samples.length, s.samples.filter((p) => !p.excluded).length, run && [run.state, run.status, run.message, run.warns, run.mode],
    tour && [tour.state, tour.cur, tour.waiting, tour.paused, tour.done, Object.keys(tour.results).length, tour.steps.map((x) => x.state), tour.warns.length], s.relays && s.relays.heater_on, s.camera.busy]);
  if (sigRun !== MC.sigs.run) { MC.sigs.run = sigRun; M.cardRun.replaceChildren(...cardRun(s).childNodes); }
  updateMeasureDynamic(s);
  redrawIfNeeded(s);
}
function redrawIfNeeded(s) { if (!T.drawn) { T.drawn = true; redrawGraphs(); } }
UI.renders.push(renderMeasure);
