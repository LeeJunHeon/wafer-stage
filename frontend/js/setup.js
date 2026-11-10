/* setup.js - 장비 설정 탭: 안내형 8단계(스테이지 · 카메라 · 판 영역 · 빈 판 사진 · 좌표 보정 · 헤드 위치 보정 · 접촉 높이 · 릴레이) + 고급 설정.
   보기 탭은 없다: 단계에 맞는 화면을 자동으로 보인다(스테이지 · 카메라 · 헤드 · 접촉 높이: 실시간 / 판 영역 · 릴레이: 빈 판 사진 / 클릭이 필요할 때만 정지 화면). */
"use strict";
const S = { step: 1, zoom: "plate", viewer: null, built: false, jogStep: 1, area: null, leftSig: "", rightSig: "", freeze: false, showRef: false, showPts: false, showHead: false, viewKey: "", ctNo: null };
const STEPS = ["스테이지", "카메라", "판 영역", "빈 판 사진", "좌표 보정", "헤드 위치 보정", "접촉 높이", "측정 장비 · 릴레이"];
S.instItems = null; S.instMsg = "";
const EEPROM_TEXT = { restored: "원점 복원됨", unsaved: "저장 안 하고 꺼짐 · 원점 없음", none: "기록 없음" };
const rate = (r) => (r == null ? "" : r < 0.3 ? "좋음" : r < 0.8 ? "주의" : "나쁨");

/* 단계 상태 셋: 완료(설정됨 + 장치 정상) · 연결 안 됨(설정은 있으나 장치 없음, 빨강) · 필요(설정 없음). 장치 상태는 서버가 실제 장치에서 만든 s.dev 만 본다. */
const dvOk = (s, k) => !!(s.dev && s.dev[k] && s.dev[k].state === "ok");
const dvWhy = (s, k, name) => (s.dev && s.dev[k] && s.dev[k].state === "retry" ? "다시 연결 중" : k === "stage" ? "포트 번호 확인" : k === "camera" ? "카메라 번호 확인" : "연결 필요");
function stepInfo(s, i) {
  const c = s.calib, st = s.settings, sg = s.stage, stOk = dvOk(s, "stage") && !!sg, camOk = dvOk(s, "camera");
  const down = (why) => ["down", "연결 안 됨", why];
  const need = (why) => ["need", "필요", why];
  const done = (sum) => ["done", "완료", sum];
  const devGap = (needStage, needCam) => (needStage && !stOk ? "스테이지 연결 필요" : needCam && !camOk ? "카메라 연결 필요" : "");
  switch (i) {
    case 1: { if (!stOk) return down(dvWhy(s, "stage", "스테이지")); const ok = sg.z_ok && sg.xy_ok && !sg.locked; return ok ? done((s.port || "") + " · " + (s.fw || "") + " · 원점 정상") : need((s.port || "") + " · 원점 없음"); }
    case 2: return camOk ? done((s.camera.size ? s.camera.size.join("×") : "—") + " · 노출 " + st.exposure) : down(dvWhy(s, "camera", "카메라"));
    case 3: { if (!c.area) return need(camOk ? "사진에서 네 꼭짓점" : "카메라 연결 필요"); const g = devGap(false, true); return g ? down(g) : done(c.size_mm ? c.size_mm[0] + " × " + c.size_mm[1] + " mm" : "지정함"); }
    case 4: { if (!s.ref) return need(camOk ? "판을 비우고 촬영" : "카메라 연결 필요"); const g = devGap(false, true); return g ? down(g) : done(s.ref.time); }
    case 5: { if (c.status === "invalid") return ["invalid", "무효", c.text]; if (c.status !== "done") return need(!camOk ? "카메라 연결 필요" : !stOk ? "스테이지 연결 필요" : c.text); const g = devGap(true, true); return g ? down(g) : done(c.text); }
    case 6: { if (c.head.status === "invalid") return ["invalid", "무효", c.head.text]; if (c.head.status !== "done") return need(!camOk ? "카메라 연결 필요" : !stOk ? "스테이지 연결 필요" : c.head.text); const g = devGap(true, true); return g ? down(g) : done(c.head.text); }
    case 7: { const z = s.contact.z; if (z == null) return need("샘플에서 핀이 닿는 높이"); const g = devGap(true, false); return g ? down(g) : done("Z " + f1(z, 2) + " mm"); }
    case 8: {
      const used = st.relay.items.map((r, n) => (r.enabled && n + 1 !== st.heater.relay ? n + 1 : null)).filter(Boolean);
      const rel = "릴레이 · " + (stOk ? "쿨러 " + (used.length ? "R" + Math.min(...used) + "–R" + Math.max(...used) : "없음") + " · 히트건 R" + st.heater.relay : "스테이지 연결 필요");
      const instOk = dvOk(s, "k2110") && dvOk(s, "k2450");
      const inst = "측정 장비 · " + (instOk ? "연결" : "연결 안 됨");
      const bad = !stOk || !instOk;
      return [bad ? "down" : "done", bad ? "연결 안 됨" : "완료", rel, inst, [!stOk, !instOk]];
    }
  }
}
/* 장비 설정의 조그 패드 비활성 사유: 서버 판단(jog_block) 하나 + 측정 중 잠김. 비상정지 잠금 중에는 회복 조그를 해야 하므로 꺼지지 않는다 */
function stopBtn(tip) { return h("button", { tabindex: -1, class: "btn stopbtn", "data-act": "stop", text: "정지", "data-tip": tip }); }
function setupPadWhy(s) { return !s.stage ? "스테이지 연결 안 됨" : (s.stage.jog_block || lockedBy(s) || ""); }
function lockedBy(s) { return s.run && ["running", "paused", "stopping"].includes(s.run.state) ? "측정 중 · 변경 잠김" : ""; }

/* 단계에 맞는 화면 */
function viewFor(s) {
  if (S.freeze) return "snap";
  if (S.step === 3 || S.step === 8) return s.ref ? "ref" : "live";
  if (S.step === 4 && S.showRef && s.ref) return "ref";
  return "live";
}

function buildSetup() {
  const pane = $("#tab-setup");
  S.viewer = new Viewer();
  S.viewer.onClick = onSetupClick;
  S.viewer.onApply = redrawSetup;
  S.viewer.onHandle = (i, p) => { S.area[i] = p; redrawSetup(); };
  S.list = h("div", { class: "col slist" });
  S.vhead = h("div", { class: "vhead" });
  S.vfoot = h("div", { class: "legend" });
  S.center = h("div", { class: "viewbox grow" }, S.vhead, S.viewer.el, S.vfoot);
  S.panel = h("div", { class: "col panel" });
  pane.replaceChildren(S.list, S.center, S.panel);
  setInterval(() => { const s = UI.state; if (s && UI.tab === "setup" && viewFor(s) === "live" && S.viewer && s.camera.open) { const sz = s.camera.size || [1280, 960]; S.viewer.set("/preview.jpg", sz[0], sz[1], Date.now()); liveTick(S.viewer); } }, 250);
  S.built = true;
}

function renderSetup(s) {
  if (!S.built) buildSetup();
  if (UI.tab !== "setup") return;
  const items = [1, 2, 3, 4, 5, 6, 7, 8].map((i) => {
    const [cls, st, sum, sum2, bads] = stepInfo(s, i);
    return h("div", { class: "sitem " + cls + (S.step === i ? " sel" : ""), "data-act": "su-step", "data-args": JSON.stringify({ i }) }, h("div", { class: "no", text: i }),
      h("div", { class: "grow" }, h("div", { class: "t", text: STEPS[i - 1] }), h("div", { class: "s" + (bads && bads[0] ? " bad" : ""), text: sum }), sum2 ? h("div", { class: "s2" + (bads && bads[1] ? " bad" : ""), text: sum2 }) : null), h("div", { class: "st", text: st }));
  });
  const lsig = JSON.stringify(items.map((e) => e.outerHTML)) + S.step;
  if (lsig !== S.leftSig) {
    S.leftSig = lsig;
    S.list.replaceChildren(...items, h("div", { class: "sub3", text: "카메라를 옮기면 3–6 다시 설정" }),
      h("div", { class: "row" }, btn("고급 설정", "su-adv", null, "", "속도 · 가동범위 · 촬영 위치 · 대기 위치 · 검출 값 등 모든 설정을 엽니다."),
        btn("이전 설정 가져오기", "su-import", null, "sm", "이전 프로그램의 포트 · 카메라 · 가동범위 · 속도 · 위치 · 릴레이 · 쿨러 설정을 읽어 옵니다. 가져온 뒤 스테이지를 다시 연결합니다. 보정 파일은 가져오지 않습니다.", lockedBy(s))));
  }
  renderSetupView(s);
  const sig = JSON.stringify([S.step, s.dev, s.camera.state, s.cam_scan, lockedBy(s), s.calib, s.ref, s.stage && [s.stage.locked, s.stage.z_ok, s.stage.xy_ok, s.stage.jog_block, s.stage.done, s.stage.fw_homed, s.stage.z > 0.05], s.camera.busy, s.settings.photohome, s.tool, s.verify, JOG.step, S.freeze, S.showPts, S.showHead, S.showRef,
    s.eeprom, s.port, s.fw, s.contact, s.samples.length, s.sel, S.ctNo, s.settings.relay.items, s.settings.cooler, s.settings.heater, s.settings.width, s.settings.exposure, s.settings.wb_temperature,
    s.rt && s.rt.inst, s.settings.rt && [s.settings.rt.k2110_addr, s.settings.rt.k2450_addr], S.instItems, S.instMsg, s.camera.busy]);
  if (sig !== S.rightSig) { S.rightSig = sig; renderPanel(s); }
  const pd = JOG.pads.s;                                                   // 패드는 다시 만들지 않는다 - 비활성 사유 · 배치만 제자리에서 갱신
  if (pd && pd.isConnected) jogPad(s, { key: "s", why: setupPadWhy(s), free: S.step === 7 });
  const pos = $("#su-pos");
  if (pos && s.stage) pos.textContent = xy(s.stage.x, s.stage.y) + " · Z " + f1(s.stage.z, 2);
}
UI.acts["su-step"] = (a) => { S.step = a.i; S.leftSig = ""; S.rightSig = ""; S.area = null; S.freeze = false; S.showRef = false; render(); if (a.i === 8 && S.instItems == null) UI.acts["su-inst-list"](); };
UI.acts["su-next"] = () => { if (S.step < 8) UI.acts["su-step"]({ i: S.step + 1 }); };

function renderSetupView(s) {
  const v = S.viewer, kind = viewFor(s), sz = s.camera.size || [1280, 960];
  const key = kind + ":" + (kind === "ref" ? s.ref && s.ref.id : kind === "snap" ? S.snapV : "");
  if (key !== S.viewKey) { S.viewKey = key; v.W = 0; v.key = null; }
  const title = { live: "실시간", ref: "빈 판 사진", snap: "정지 화면" }[kind];
  S.vhead.replaceChildren(h("b", { class: kind === "live" ? "livetitle" : "", text: title + " · " + STEPS[S.step - 1] }), h("span", { class: "grow" }),
    h("div", { class: "seg" }, btn("판 맞춤", "su-fit", { m: "plate" }, S.zoom === "plate" ? "on" : ""), btn("전체", "su-fit", { m: "all" }, S.zoom === "all" ? "on" : "")));
  S.vfoot.replaceChildren(h("span", { text: S.step === 5 ? "점선 = 판 영역 · 번호 = 기준점" : S.step === 8 ? "점선 격자 = 쿨러 구역" : S.step === 6 && kind === "snap" ? "주황 상자 = 헤드 마커(정렬에서 뺀 영역)" : "점선 = 판 영역" }), h("span", { class: "grow" }), h("span", { class: "mono", text: s.camera.text }));
  v.mode = S.zoom;
  v.area = s.calib.area;
  const noPhoto = kind === "live" && !s.camera.open;               // 사진이 없으면 사진 자리에 안내 · 꼭짓점 편집기는 사진이 있을 때만
  if (noPhoto) { v.setEmpty(camEmpty(s)); v.set(null); }
  else if (kind === "snap") v.set("/snap.jpg", sz[0], sz[1], S.snapV);
  else if (kind === "ref") v.set("/ref.jpg", sz[0], sz[1], s.ref.id);
  else { v.setEmpty({ text: "실시간 화면 준비 중" }); if (!v.W) v.set("/preview.jpg", sz[0], sz[1], Date.now()); }
  if (S.step === 3 && !noPhoto) { if (!S.area) S.area = (s.calib.area || defaultArea(sz)).map((p) => p.slice()); v.handles = S.area; } else v.handles = null;
  v.message(s.camera.cap_text && s.camera.busy ? s.camera.busy + " · " + s.camera.cap_text : "");
  $$(".hint", v.el).forEach((e) => e.remove());
  if (S.freeze && S.step === 5) v.el.append(h("div", { class: "hint", text: "핀 가운데와 같은 점을 클릭" }));
  redrawSetup();
}
function defaultArea(sz) { const w = sz[0], h_ = sz[1]; return [[w * 0.25, h_ * 0.2], [w * 0.75, h_ * 0.2], [w * 0.75, h_ * 0.8], [w * 0.25, h_ * 0.8]]; }
function frameOfView() { const k = viewFor(UI.state); return k === "ref" ? "ref" : k === "snap" ? "snap" : null; }
UI.acts["su-fit"] = (a) => { S.zoom = a.m; S.viewer.fit(a.m, S.viewer.area); render(); };
async function takeSnap() {
  const r = await send("snap");
  if (r.ok) { S.snapV = Date.now(); S.freeze = true; S.viewKey = ""; S.rightSig = ""; render(); if (r.res && !r.res.aligned && r.res.why) toast("정렬 없음 · " + r.res.why, true); }
  return r;
}

function redrawSetup() {
  const s = UI.state, v = S.viewer;
  if (!s || !v) return;
  v.clear();
  if (!v.W) return;
  const k = v.k, lw = (n) => n / k;
  const area = S.step === 3 && S.area ? S.area : s.calib.area;
  if (area) v.svg.append(sv("polygon", { points: area.map((p) => p.join(",")).join(" "), fill: "rgba(47,111,219,.08)", stroke: "#8fb4f2", "stroke-width": lw(2), "stroke-dasharray": lw(9) + " " + lw(6) }));
  if (S.step === 3 && S.area) S.area.forEach((p, i) => { v.svg.append(sv("circle", { cx: p[0], cy: p[1], r: lw(9), fill: "#2f6fdb", stroke: "#fff", "stroke-width": lw(2) })); v.svg.append(sv("text", { x: p[0] + lw(12), y: p[1] - lw(10), "font-size": lw(15), fill: "#fff", "font-weight": 700 }, String(i + 1))); });
  if (S.step === 5 || S.step === 6) {
    const pts = S.step === 5 ? s.calib.points : s.calib.head.pairs;
    pts.forEach((p, i) => {
      v.svg.append(sv("circle", { cx: p.u, cy: p.v, r: lw(8), fill: "none", stroke: "#e08a1e", "stroke-width": lw(2.5) }));
      v.svg.append(sv("text", { x: p.u + lw(10), y: p.v - lw(8), "font-size": lw(15), fill: "#ffe08a", "font-weight": 700 }, String(p.no || i + 1)));
    });
  }
  if (S.step === 8 && area && s.calib.status === "done") drawZones(v, s, area, lw);
  /* 헤드 마커 상자(위치 확인 결과): 정지 화면에 정렬에서 뺀 영역을 그린다 */
  if (S.step === 6 && viewFor(s) === "snap" && s.verify && s.verify.box) {
    v.svg.append(sv("polygon", { points: s.verify.box.map((q) => q.join(",")).join(" "), fill: "rgba(224,138,30,.15)", stroke: "#e08a1e", "stroke-width": lw(3) }));
    v.svg.append(sv("text", { x: s.verify.box[0][0], y: s.verify.box[0][1] - lw(8), "font-size": lw(16), fill: "#ffcd7a", "font-weight": 700 }, "헤드 마커"));
  }
}

/* 좌표 보정 클릭: 판 꼭짓점 · 칩 가운데 가까이면 자동으로 붙는다(후보 버튼 줄은 없다) */
function snapCandidate(u, v, k) {
  const s = UI.state, tol = 26 / k;
  let best = null, bd = tol;
  const cand = (s.calib.area || []).map((p) => ({ u: p[0], v: p[1], src: "click" })).concat(s.samples.filter((p) => p.ru != null).map((p) => ({ u: p.ru, v: p.rv, src: "sample", no: p.no })));
  cand.forEach((c) => { const d = Math.hypot(c.u - u, c.v - v); if (d < bd) { bd = d; best = c; } });
  return best;
}
async function onSetupClick(u, v) {
  const s = UI.state, fr = frameOfView();
  if (S.step === 5 && S.freeze) {
    const c = snapCandidate(u, v, S.viewer.k);
    const r = c ? await send("calib_click", { u: c.u, v: c.v, frame: "ref", src: c.src, shot: c.src === "sample" && s.shot ? s.shot.id + ":" + c.no : null }) : await send("calib_click", { u, v, frame: "snap" });
    if (r.ok) { S.freeze = false; S.viewKey = ""; toast(c ? "기준점 기록 · 가까운 점에 붙임" : "기준점 기록"); }
  }
}

/* ---- 오른쪽 패널 ---- */
function field(label, input) { return [h("label", { text: label }), input]; }
function numInput(id, val, step) { return h("input", { type: "number", id, value: val, step: step || "any" }); }
function selInput(id, val, opts) { return h("select", { id }, opts.map(([v, l]) => h("option", { value: v, text: l, selected: String(v) === String(val) }))); }
function saveFields(map) {
  return async () => {
    const patch = {};
    for (const k in map) { const el = $("#" + map[k]); if (!el) continue; patch[k] = el.type === "number" ? Number(el.value) : el.type === "checkbox" ? el.checked : el.value; }
    const r = await send("settings_save", { patch });
    if (r.ok) toast("저장");
  };
}
function stepDone(s, i) { return stepInfo(s, i)[0] === "done"; }

function renderPanel(s) {
  const P = S.panel, lock = lockedBy(s), st = s.settings, c = s.calib, nostage = !s.stage;
  const camWhy = s.camera.open ? "" : "카메라 연결 안 됨";                 // 사진이 필요한 작업은 카메라가 없으면 비활성 + 사유
  const noPhoto = viewFor(s) === "live" && !s.camera.open;
  const kids = [];
  const card = (title, ...x) => h("div", { class: "card" }, typeof title === "string" ? h("h3", { text: title }) : title, ...x.filter(Boolean));
  const jogCard = () => {
    const sg = s.stage;
    const dis = setupPadWhy(s);
    const jc = card(cardHead("조그", h("b", { class: "mono nowrap", id: "su-pos", text: sg ? xy(sg.x, sg.y) + " · Z " + f1(sg.z, 2) : "—" }), jogKbdTag()),
      jogPad(s, { key: "s", why: dis, free: S.step === 7 }),
      sg && sg.z > 0.5 ? h("div", { class: "sub3", text: "Z 가 내려가 있음 · 다른 점으로 가기 전 Z↑" }) : null);
    jc.classList.add("jogcardx");
    return jc;
  };
  let footNode = null;                                   // [다음 단계] 는 패널 맨 아래 오른쪽(카드 밖에 떠 있지 않게)
  const foot = (extra) => { footNode = S.step < 8 ? h("div", { class: "foot" }, extra, btn("다음 단계", "su-next", null, stepDone(s, S.step) ? "primary" : "")) : null; return null; };
  const axisBtn = (ax) => {
    const sg = s.stage;
    let why = lock || (nostage ? "스테이지 연결 안 됨" : "");
    if (!why && ax !== "z") why = !sg.z_ok ? "Z 원점 먼저" : (sg.z != null && Math.abs(sg.z) > 0.05 ? "Z 맨 위 먼저 · Z↑" : "");
    const have = ax === "z" ? sg && sg.z_ok : sg && (sg.xy_ok || sg.done[ax]);
    return h("div", { class: "axrow" }, h("span", { class: "chip " + (have ? "ok" : "warn"), text: ax.toUpperCase() + " " + (have ? "있음" : "없음") }),
      btn("원점 잡기", "su-origin", { axis: ax }, have ? "" : "primary", ax.toUpperCase() + " 를 끝단 쪽으로 최대 10 mm 밀어 2 mm 물러난 자리를 원점으로 잡고 저장합니다.", why));
  };
  if (S.step === 1) {
    const sg = s.stage, miss = sg ? (!sg.z_ok ? "z" : !(sg.xy_ok || sg.done.x) ? "x" : !(sg.xy_ok || sg.done.y) ? "y" : null) : null;
    const hint = !sg ? "스테이지 연결 뒤 원점 잡기" : miss ? (sg.fw_homed[miss] ? miss.toUpperCase() + " · 펌웨어 위치가 남아 있어 끝단 3 mm 앞까지 자동으로 다가간 뒤 잡습니다." : miss.toUpperCase() + " · 조그로 끝단 10 mm 안까지 옮긴 뒤 누르세요.") : "원점이 모두 있습니다.";
    if (!S.ports && !S.portsBusy) setTimeout(() => UI.acts["su-ports"](), 0);                 // 처음 들어오면 포트 목록을 읽는다(목록만 읽고 열지 않는다)
    const pl = (S.ports && S.ports.ports) || [], cur = S.portSel || s.port || (S.ports && S.ports.selected) || "";
    const portSel = h("select", { class: "portsel", id: "su-port", "data-tip": "연결할 시리얼 포트를 고릅니다. Arduino 로 보이는 것에 표시가 붙습니다.", disabled: !!lock, onchange: (e) => { S.portSel = e.target.value; } },
      h("option", { value: "", text: pl.length ? "포트 선택" : (S.portsBusy ? "목록 읽는 중…" : "포트 없음 · USB 확인") }),
      ...pl.map((x) => h("option", { value: x.device, text: x.device + " · " + x.desc.replace(/\s*\(COM\d+\)\s*$/, "") + (x.arduino ? " · Arduino" : ""), selected: x.device === cur })));
    kids.push(card(cardHead("스테이지", moreBtn([{ label: "대기 위치로", fn: () => UI.acts["su-park"](), why: lock || (nostage ? "스테이지 연결 안 됨" : !(sg.z_ok && sg.xy_ok && !sg.locked) ? "원점 필요" : "") }])),
      h("div", { class: "row" }, h("span", { class: "sub fl", text: "포트" }), portSel,
        btn("연결", "su-port-connect", null, "primary sm", "고른 포트로 저장하고 연결합니다.", lock || (!pl.length ? "포트 없음 · USB 확인" : !(S.portSel || cur) ? "포트를 먼저 고름" : "")),
        btn("목록 새로고침", "su-ports", null, "sm", "시리얼 포트 목록을 다시 읽습니다.", S.portsBusy ? "읽는 중" : "")),
      h("div", { class: "row wrap kv1" }, h("span", { class: "sub", text: "연결된 포트" }), h("b", { text: s.port || "—" }), h("span", { class: "sub", text: "펌웨어" }), h("b", { text: s.fw || "—" }),
        h("span", { class: "sub", text: "저장 기록" }), h("b", { text: EEPROM_TEXT[s.eeprom] || "—" })),
      h("div", { class: "axes" }, axisBtn("z"), axisBtn("x"), axisBtn("y")),
      h("div", { class: "sub", text: hint + " 순서 Z → X → Y · 끝단에 닿는 드르륵 소리는 정상입니다." })));
    kids.push(jogCard());
    kids.push(foot());
  } else if (S.step === 2) {
    kids.push(card("카메라", h("div", { class: "kv" }, ...field("카메라 번호", numInput("f-ci", st.camera_index, 1)), ...field("가로", numInput("f-w", st.width, 1)), ...field("세로", numInput("f-h", st.height, 1)),
      ...field("노출", numInput("f-ex", st.exposure)), ...field("화이트밸런스 K", numInput("f-wb", st.wb_temperature, 100)),
      ...field("초점", selInput("f-fm", st.focus_mode, [["fixed", "고정"], ["auto", "자동"]]))),
      h("div", { class: "row" }, btn("저장", "su-save-cam", null, "primary", "", lock), btn("카메라 찾기", "su-cam-scan", null, "", "번호 0–5 를 차례로 열어 보고 열리는 카메라를 목록으로 보입니다. 백그라운드로 돌립니다.", lock || (s.cam_scan && s.cam_scan.running ? "찾는 중" : "")),
        moreBtn([{ label: "초점 다시 맞추기", fn: () => UI.acts["su-focus"](), why: lock || camWhy }])),
      s.cam_scan ? h("div", { class: "cands" }, s.cam_scan.running ? h("div", { class: "sub", text: "찾는 중 …" }) : null,
        ...s.cam_scan.items.filter((x) => x.ok).map((x) => h("div", { class: "cand" }, x.thumb ? h("img", { class: "cthumb", src: "/camthumb/" + x.index + ".jpg?t=" + (s.cam_scan.running ? 0 : s.v), alt: "카메라 " + x.index }) : h("span", { class: "cthumb none", text: "사진 없음" }),
          h("b", { text: "번호 " + x.index }), h("span", { class: "grow", text: (x.busy ? "사용 중 · " : "열림 · ") + (x.size ? x.size.join("×") : "") }),
          btn(st.camera_index === x.index ? "선택됨" : "이 번호로", "su-cam-pick", { index: x.index }, "sm", "카메라 번호로 저장합니다.", lock || (st.camera_index === x.index ? "이미 선택됨" : "")))),
        !s.cam_scan.running && !s.cam_scan.items.some((x) => x.ok) ? h("div", { class: "sub", text: "열리는 카메라 없음 · USB 확인" }) : null) : null,
      h("div", { class: "sub3", text: "카메라 설정을 바꾸면 3–6 을 다시 확인" })), foot());
  } else if (S.step === 3) {
    const sz = c.size_mm;
    kids.push(card("판 영역", h("div", { class: "sub", text: "사진에서 네 꼭짓점을 끌어 판 사각형에 맞춥니다." }),
      h("div", { class: "kv" }, h("span", { text: "실측 크기" }), h("b", { text: sz ? sz[0] + " × " + sz[1] + " mm" : "좌표 보정 뒤 표시" })),
      h("div", { class: "row" }, btn("적용", "su-area", null, "primary", "", lock || (noPhoto ? "사진 없음 · 카메라 연결 안 됨" : "")), moreBtn([{ label: "초기화", fn: () => UI.acts["su-area-reset"]() }]))), foot());
  } else if (S.step === 4) {
    const hist = (s.ref && s.ref.history) || [];
    kids.push(card("빈 판 사진", h("div", { class: "sub", text: "판 위의 샘플을 모두 치웁니다. 촬영 위치로 자동 이동한 뒤 찍습니다." }),
      h("div", { class: "row" }, btn("촬영 위치로 이동 · 촬영", "su-ref", null, "primary", "", lock || (c.area ? "" : "판 영역 필요") || (s.camera.busy ? s.camera.busy + " 중" : "") || camWhy),
        s.camera.busy && nostage === false ? stopBtn("움직이는 모터를 멈추고 이 작업을 중단합니다(잠그지 않음).") : null),
      S.refRes ? h("div", { class: "sub", text: "교체 결과 · " + (S.refRes.moved ? "이동 " + S.refRes.moved.shift_px + " px · 잔차 " + S.refRes.moved.resid + " · 성공" : "기하 이동 없음") }) : null,
      hist.length ? h("div", { class: "row" }, h("span", { class: "sub grow", text: "이력 " + hist.length + "개" }), moreBtn(hist.slice(0, 8).map((x) => ({ label: x.time + " 로 되돌리기", fn: () => UI.acts["su-ref-restore"]({ id: x.id }), why: lock })))) : null), foot());
  } else if (S.step === 5) {
    const n = c.points.length;
    kids.push(card("좌표 보정", h("ol", { class: "sub" }, h("li", { text: "핀 가운데를 기준점에 맞추기 · Z 는 판 위 1–2 mm" }), h("li", { text: "위치 기록 → 자동으로 한 장 찍힘" }), h("li", { text: "사진에서 같은 점 클릭 (꼭짓점 · 칩 가운데는 자동으로 붙음)" })),
      h("div", { class: "row" }, btn("위치 기록 · " + (n + 1) + "번 점", "su-record", null, "primary", "", lock || (nostage ? "스테이지 연결 안 됨" : "") || camWhy),
        c.pending || S.freeze ? btn("취소", "su-rec-cancel", null, "sm") : null)));
    kids.push(jogCard());
    const rowsT = c.points.map((p) => h("tr", {}, h("td", { text: p.no }), h("td", { text: f1(p.x) }), h("td", { text: f1(p.y) }), h("td", { text: p.res_mm != null ? f1(p.res_mm, 2) : "—" }), h("td", {}, btn("삭제", "su-pdel", { no: p.no }, "sm", "", lock))));
    kids.push(card("기준점", h("div", { class: "row" }, h("span", { class: "grow", text: "점 " + n + (c.rms != null ? " · 잔차 " + f1(c.rms, 2) + " mm · " + rate(c.rms) : "") }), btn(S.showPts ? "점 접기 ▴" : "점 보기 ▾", "su-pts", null, "sm")),
      S.showPts ? h("div", { class: "scroll" }, h("table", { class: "tb" }, h("tr", {}, ["점", "X", "Y", "잔차", ""].map((t) => h("th", { text: t }))), ...rowsT)) : null,
      ...c.warnings.map((w) => h("div", { class: "why", text: w })),
      h("div", { class: "row" }, btn("적용 · 5점부터", "su-capply", null, "primary", "", lock || (n >= c.min_apply ? "" : "기준점 " + n + "개 · " + c.min_apply + "개부터"))),
      h("div", { class: "sub3", text: c.text }), foot()));
  } else if (S.step === 6) {
    const hc = c.head, v = s.verify;
    kids.push(card("헤드 위치 보정", h("div", { class: "sub", text: "격자 n×n 으로 자동 이동 · 촬영해 헤드 마커 위치를 잽니다." }),
      h("div", { class: "row" }, btn("자동 보정", "su-head", null, "primary", "", lock || (c.status === "done" ? "" : "좌표 보정 필요") || (nostage ? "스테이지 연결 안 됨" : "") || camWhy),
        s.camera.busy && nostage === false ? stopBtn("움직이는 모터를 멈추고 자동 보정을 중단합니다. 이전 보정은 그대로 남습니다.") : null,
        moreBtn([{ label: "지금 위치 확인", fn: () => UI.acts["su-verify"](), why: lock || (hc.status === "done" ? "" : "헤드 위치 보정 필요") || camWhy || (nostage ? "스테이지 연결 안 됨" : "") }])),
      h("div", { class: "row" }, h("span", { class: "grow", text: hc.pairs.length ? "점 " + hc.pairs.length + (hc.rms != null ? " · 잔차 " + f1(hc.rms, 2) + " mm · " + rate(hc.rms) : "") : "아직 안 함" }), btn(S.showHead ? "점 접기 ▴" : "점 보기 ▾", "su-hpts", null, "sm")),
      S.showHead ? h("div", { class: "scroll" }, h("table", { class: "tb" }, h("tr", {}, ["X", "Y", "잔차"].map((t) => h("th", { text: t }))), ...hc.pairs.map((p) => h("tr", {}, h("td", { text: f1(p.x) }), h("td", { text: f1(p.y) }), h("td", { text: p.res_mm != null ? f1(p.res_mm, 2) : "—" }))))) : null,
      v ? h("div", { class: "row wrap" }, v.d != null ? h("span", { class: "chip " + (v.verdict === "정상" ? "ok" : "warn"), text: "위치 확인 " + v.verdict + " · 차이 " + f1(v.d, 2) + " mm" }) : h("span", { class: "chip warn", text: v.why || "위치 확인 못 함" }),
        v.d != null && v.verdict !== "정상" ? btn("적용 (sp)", "su-apply-pos", null, "primary sm", "위치 확인 결과를 스테이지 위치로 적용합니다. 비상정지 회복의 마지막 단계입니다.") : null,
        S.freeze ? btn("실시간으로", "su-rec-cancel", null, "sm") : null) : null), foot());
  } else if (S.step === 7) {
    const ctr = s.contact, withX = s.samples.filter((p) => p.X != null);
    if (S.ctNo == null || !withX.some((p) => p.no === S.ctNo)) S.ctNo = (withX.find((p) => p.no === s.sel) || withX[0] || {}).no;
    const sg = s.stage;
    const base = lock || (nostage ? "스테이지 연결 안 됨" : sg && !(sg.z_ok && sg.xy_ok && !sg.locked) ? "원점 필요 · 단계 1" : "");
    const noS = withX.length ? "" : "샘플 필요 · 측정 탭에서 촬영";
    const sel = h("select", { id: "ct-no", onchange: (e) => { S.ctNo = Number(e.target.value); S.rightSig = ""; } }, withX.map((p) => h("option", { value: p.no, text: p.no + "번 · " + xy(p.X, p.Y), selected: p.no === S.ctNo })));
    kids.push(card("접촉 높이", h("div", { class: "sub", text: "1 샘플 선택 · [샘플 위로] / 2 Z 조그(0.1 mm)로 핀이 닿을 때까지 / 3 [지금 높이를 접촉 높이로]" }),
      h("div", { class: "row" }, sel, btn("샘플 위로", "su-ct-goto", null, "", "고른 샘플 위로 공구를 옮깁니다(Z 를 먼저 올립니다).", base || noS)),
      h("div", { class: "row" }, btn("지금 높이를 접촉 높이로", "su-ct-save", null, "primary", "지금 Z 높이(절대값)를 접촉 높이로 저장합니다. 고른 샘플 위일 때만 저장됩니다.", base || noS || (sg && sg.z < 0.1 ? "Z 를 먼저 내리기" : ""))),
      h("div", { class: "kv" }, h("span", { text: "현재 값" }), h("b", { text: ctr.z == null ? "비어 있음 · 측정 시작 불가" : "Z " + f1(ctr.z, 2) + " mm" })),
      h("div", { class: "row" }, btn("접촉 높이로 내려 보기", "su-ct-try", null, "", "저장한 높이까지 내립니다. 마지막 " + ctr.approach + " mm 는 느리게 내립니다.", base || noS || (ctr.z == null ? "접촉 높이 필요" : "")),
        btn("Z 맨 위", "su-ztop", null, "", "Z 를 맨 위로 올립니다.", base),
        moreBtn([{ label: "접촉 높이 지우기", fn: () => send("contact_clear"), why: lock || (ctr.z == null ? "저장한 값 없음" : "") }]))));
    kids.push(jogCard());
    kids.push(foot());
  } else if (S.step === 8) {
    const zr = st.cooler.zones, hn = st.heater.relay;
    const relayOpts = Array.from({ length: 10 }, (_, i) => i + 1).filter((n) => n !== hn);
    const zcells = zr.cells.map((cl, i) => h("div", {}, h("div", { class: "sub3", text: "칸 " + (i + 1) }), selInput("m-z" + i, cl.relay, relayOpts.map((n) => [n, "R" + n]))));
    const rows = st.relay.items.map((r, i) => h("div", { class: "rlrow" }, h("label", { class: "chk" }, h("input", { type: "checkbox", id: "rl-" + i, checked: r.enabled, disabled: !!lock }), "R" + (i + 1)),
      h("span", { class: "grow", text: i + 1 === hn ? "히트건" : "쿨러" }),
      btn("2 s 켜 보기", "su-rl-test", { n: i + 1 }, "sm", "이 릴레이를 2 초만 켰다 끕니다.", lock || (nostage ? "스테이지 연결 안 됨" : "") || (r.enabled ? "" : "사용 안 함") || (s.stage && s.stage.locked ? "비상정지 잠금" : ""))));
    const ik = (s.rt && s.rt.inst) || {}, rts = st.rt;
    /* 주소: 목록(pyvisa 가 찾은 GPIB · USB 전부)에서 고르거나 직접 입력 */
    const addrSel = (key, val) => h("span", { class: "row grow" }, h("input", { type: "text", class: "grow mono", id: "ia-" + key, list: "ia-list", value: val || "", spellcheck: "false", disabled: !!lock,
      onchange: async (e) => { const r = await send("settings_save", { patch: { ["rt." + key]: e.target.value.trim() } }); if (!r.ok) e.target.value = val || ""; } }),
      h("span", { class: "chip", text: addrKind(val) || "?" }));
    const devLine = (k, d) => h("div", { class: "kv" }, h("span", { text: k }), h("span", { class: d && d.ok ? "chip ok" : "chip no", text: d ? (d.ok ? "연결 · " + (d.kind ? d.kind + " · " : "") + (d.idn || "").split(",").slice(0, 2).join(" ") : d.err) : "연결 안 됨" }));
    const tst = ik.test;
    kids.push(card("측정 장비", h("datalist", { id: "ia-list" }, ...(S.instItems || []).map((v) => h("option", { value: v, label: addrKind(v) }))), h("div", { class: "sub", text: "K2110(열전대 온도) · K2450(전압 걸고 전류 측정). VISA 주소를 목록에서 고릅니다." }),
      ik.msg ? h("div", { class: "why", text: ik.msg }) : null,
      h("div", { class: "kv" }, h("span", { text: "K2110" }), addrSel("k2110_addr", rts.k2110_addr)),
      h("div", { class: "kv" }, h("span", { text: "K2450" }), addrSel("k2450_addr", rts.k2450_addr)),
      h("div", { class: "row" }, btn("목록 새로고침", "su-inst-list", null, "sm", "연결된 VISA 장비 주소를 다시 읽습니다."), btn("연결 시험", "su-inst-test", null, "primary", "두 장비에 연결해 장비 이름 · 온도 1회 · 저항 1회를 잽니다. 저항 1회를 잴 때만 출력을 짧게 켜고 바로 끕니다.", lock || (s.camera.busy ? s.camera.busy + " 중" : "")),
        btn("연결 끊기", "su-inst-off", null, "sm", "두 장비 연결을 닫습니다.", lock || (ik.k2110 || ik.k2450 ? "" : "연결 안 됨"))),
      devLine("K2110", ik.k2110), devLine("K2450", ik.k2450),
      tst ? h("div", { class: "sub", text: "시험 " + tst.t + " · 온도 " + (tst.temp == null ? "값 없음" : f1(tst.temp, 1) + " °C") + " · 저항 " + (tst.R == null ? "—" : Number(tst.R).toExponential(3) + " Ω") + " · 전류 " + (tst.I == null ? "—" : Number(tst.I).toExponential(3) + " A") + (tst.k2450_kind ? " · K2450 " + tst.k2450_kind : "") + (tst.k2110_kind ? " · K2110 " + tst.k2110_kind : "") + (tst.terminal ? " · 단자 " + (tst.terminal === "REAR" ? "뒷면" : "앞면") : "") + (tst.open ? " · 연결 없음 · 핀 접촉 · 앞/뒤 단자 설정 확인" : "") + (tst.err ? " · " + tst.err : "") }) : null));
    kids.push(card("릴레이", h("div", { class: "sub", text: "히트건 R" + hn + " · 쿨러 R1–R9. 쓰는 릴레이만 켜 둡니다." }), ...rows,
      h("h3", { text: "쿨러 구역 " + zr.rows + "×" + zr.cols }), h("div", { class: "zgrid" }, ...zcells),
      h("div", { class: "row" }, btn("저장", "su-save-dev", null, "primary", "", lock))));
  }
  let live = kids.filter(Boolean);
  const jogEl = live.find((k) => k.classList && k.classList.contains("jogcardx"));
  P.classList.toggle("pj", !!jogEl);
  if (jogEl) {                                                  // 조그 카드는 패널 아래에 고정(스크롤 밖) · 그 안 아래 오른쪽에 [다음 단계]
    if (footNode) jogEl.append(footNode);
    P.replaceChildren(h("div", { class: "pscroll" }, ...live.filter((k) => k !== jogEl)), jogEl);
    return;
  }
  const lastCard = live.length ? live[live.length - 1] : null;
  if (footNode && lastCard && lastCard.classList && lastCard.classList.contains("card")) lastCard.append(footNode);          // [다음 단계] 는 그 단계 마지막 카드 안 아래 오른쪽
  P.replaceChildren(...live, ...(footNode && !(lastCard && lastCard.contains && lastCard.contains(footNode)) ? [footNode] : []));
}
/* ---- 동작 ---- */
UI.acts["su-ports"] = async () => {
  S.portsBusy = true; S.rightSig = ""; render();
  const r = await send("ports");
  S.portsBusy = false;
  if (r.ok) { S.ports = r.res; if (!S.portSel && r.res.selected) S.portSel = r.res.selected; } else S.ports = { ports: [], selected: null };
  S.rightSig = ""; render();
};
UI.acts["su-port-connect"] = async () => {
  const sel = document.getElementById("su-port"), port = (sel && sel.value) || S.portSel || UI.state.port;
  const r = await send("port_connect", { port });
  if (r.ok) { toast("포트 " + port + " 연결"); S.ports = null; S.rightSig = ""; render(); }
};
UI.acts["su-cam-scan"] = async () => { const r = await send("cam_scan"); if (r.ok) toast("카메라를 찾는 중"); };
UI.acts["su-cam-pick"] = async (a) => { const r = await send("settings_save", { patch: { camera_index: a.index } }); if (r.ok) toast("카메라 번호 " + a.index + " 저장"); };
UI.acts["su-save-cam"] = saveFields({ camera_index: "f-ci", width: "f-w", height: "f-h", exposure: "f-ex", wb_temperature: "f-wb", focus_mode: "f-fm" });
UI.acts["su-focus"] = async () => { const r = await send("focus_refind"); if (r.ok) toast(r.res && r.res.msg ? r.res.msg : "초점 완료"); };
UI.acts["su-area"] = async () => {
  let fr = frameOfView();
  if (fr === null) { const r = await takeSnap(); if (!r.ok) return; fr = "snap"; }
  const r = await send("area_set", { points: S.area, frame: fr, size: UI.state.camera.size });
  if (r.ok) { toast("판 영역 적용"); S.area = null; S.freeze = false; S.viewKey = ""; }
};
UI.acts["su-area-reset"] = () => { S.area = defaultArea(UI.state.camera.size || [1280, 960]); S.viewer.handles = S.area; redrawSetup(); };
UI.acts["su-ref"] = async () => { const r = await send("ref_capture"); if (r.ok) { S.refRes = r.res; S.showRef = true; S.viewKey = ""; S.rightSig = ""; render(); } };
UI.acts["su-ref-restore"] = (a) => dialog("이 빈 판 사진으로 되돌릴까요?", "좌표 보정은 새 사진 기준으로 옮겨집니다. 정렬이 안 되면 거절됩니다.", [{ label: "취소" }, { label: "되돌리기", cls: "primary", fn: async () => { const r = await send("ref_restore", a); if (r.ok) { S.refRes = r.res; S.showRef = true; S.rightSig = ""; render(); } } }]);
UI.acts["su-record"] = async () => { const r = await send("calib_record"); if (r.ok) await takeSnap(); };
UI.acts["su-rec-cancel"] = () => { S.freeze = false; S.viewKey = ""; S.rightSig = ""; render(); };
UI.acts["su-pdel"] = (a) => send("calib_delete", a);
UI.acts["su-pts"] = () => { S.showPts = !S.showPts; S.rightSig = ""; render(); };
UI.acts["su-hpts"] = () => { S.showHead = !S.showHead; S.rightSig = ""; render(); };
UI.acts["su-capply"] = async () => { const r = await send("calib_apply"); if (r.ok) toast("좌표 보정 적용 · 잔차 " + r.res.rms + " mm"); };
UI.acts["su-head"] = async () => {
  const r = await send("head_calib");
  if (!r.ok) return;
  const a = await send("head_apply");                    // 자동 보정 하나로 끝: 잔차가 한도 안이면 바로 적용
  toast(a.ok ? "헤드 위치 보정 · 점 " + r.res.n + " · 잔차 " + r.res.rms + " mm · 적용" : "적용 못 함 · " + (a.msg || ""), !a.ok);
};
UI.acts["su-verify"] = async () => {
  const r = await send("verify_position");
  S.snapV = Date.now(); S.freeze = true; S.viewKey = ""; S.rightSig = ""; render();      // 헤드 마커 상자를 정지 화면에 그린다(실패해도 보인다)
  if (r.ok) toast("위치 확인 · 차이 " + r.res.d + " mm");
};
UI.acts["su-apply-pos"] = () => dialog("위치를 적용할까요?", "스테이지 위치를 마커로 잰 값으로 바꿉니다.", [{ label: "취소" }, { label: "적용", cls: "primary", fn: () => send("apply_position") }]);
UI.acts["su-origin"] = (a) => {
  const sg = UI.state.stage, X = a.axis.toUpperCase();
  const tip = X + " 를 끝단 쪽으로 최대 10 mm 밀어 2 mm 물러난 자리를 원점으로 잡고 저장합니다. " + (sg && sg.fw_homed[a.axis]
    ? "펌웨어 위치가 남아 있어 끝단 3 mm 앞까지 자동으로 다가간 뒤 잡습니다. 실제 위치가 그보다 7 mm 넘게 멀면 틀린 원점이 잡히니 끝단 가까이인지 눈으로 확인하십시오." : "먼저 조그로 끝단 10 mm 안까지 옮겨 두어야 합니다.");
  bandAct({ act: "origin", args: { axis: a.axis }, label: X + " 원점 잡기", tip, confirm: true });
};
UI.acts["su-park"] = async () => { const r = await send("goto_named", { name: "park" }); if (r.ok) toast("대기 위치 · 위치 저장"); };
UI.acts["su-ztop"] = () => send("z_top");
UI.acts["su-ct-goto"] = () => send("goto_sample", { no: S.ctNo });
UI.acts["su-ct-save"] = async () => { const r = await send("contact_save", { no: S.ctNo }); if (r.ok) toast("접촉 높이 저장 · Z " + r.res.z + " mm"); };
UI.acts["su-ct-try"] = async () => { const r = await send("contact_try", { no: S.ctNo }); if (r.ok) toast("접촉 높이까지 내림"); };
const addrKind = (a) => { a = String(a || "").toUpperCase(); return a.startsWith("GPIB") ? "GPIB" : a.startsWith("USB") ? "USB" : a.startsWith("TCPIP") ? "LAN" : a.startsWith("ASRL") ? "시리얼" : ""; };
UI.acts["su-inst-list"] = async () => { const r = await send("inst_list"); if (r.ok) { S.instItems = r.res.items || []; S.instMsg = r.res.msg || ""; S.rightSig = ""; render(); } };
UI.acts["su-inst-test"] = async () => { const r = await send("inst_test"); if (r.ok) toast("연결 시험 · " + (r.res.temp != null ? f1(r.res.temp, 1) + " °C" : "") + " · " + (r.res.R != null ? Number(r.res.R).toExponential(2) + " Ω" : "")); };
UI.acts["su-inst-off"] = () => send("inst_disconnect");
UI.acts["su-rl-test"] = async (a) => { const r = await send("relay_test", a); if (r.ok) toast("R" + a.n + " · 2 s 켰다 껐습니다"); };
UI.acts["su-save-dev"] = async () => {
  const st = UI.state.settings;
  const z = JSON.parse(JSON.stringify(st.cooler.zones));
  z.cells.forEach((c, i) => { const e = $("#m-z" + i); if (e) c.relay = Number(e.value); });
  const items = st.relay.items.map((r, i) => Object.assign({}, r, { enabled: $("#rl-" + i) ? $("#rl-" + i).checked : r.enabled }));
  const r = await send("settings_save", { patch: { "cooler.zones": z, "relay.items": items } });
  if (r.ok) toast("저장");
};
UI.acts["su-import"] = () => dialog("이전 프로그램 설정을 가져올까요?", "포트 · 카메라 · 가동범위 · 속도 · 위치 · 릴레이 · 쿨러 설정을 읽어 오고 스테이지를 다시 연결합니다. 보정 파일은 가져오지 않으니 다시 보정하십시오.", [{ label: "취소" }, { label: "가져오기", cls: "primary", fn: async () => { const r = await send("settings_import_old"); if (r.ok) toast("가져옴 · " + r.res.imported.length + "개 항목"); } }]);

/* ---- 고급 설정: 스키마로 자동 생성 ---- */
UI.renders.push(renderSetup);
