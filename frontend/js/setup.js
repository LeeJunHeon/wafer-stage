/* setup.js - 장비 설정 탭: 7단계(카메라 · 판 영역 · 빈 판 사진 · 좌표 보정 · 헤드 위치 보정 · 공구 오프셋 · 측정 장비) + 고급 설정. */
"use strict";
const S = { step: 1, view: "live", zoom: "plate", viewer: null, built: false, jogStep: 1, area: null, areaFrom: null, leftSig: "", rightSig: "", lastVerify: null, tip: null };
const STEPS = ["카메라", "판 영역", "빈 판 사진", "좌표 보정", "헤드 위치 보정", "공구 오프셋", "측정 장비"];

function stepInfo(s, i) {
  const c = s.calib, st = s.settings;
  switch (i) {
    case 1: return ["done", "완료", (s.camera.size ? s.camera.size.join("×") : st.width + "×" + st.height) + " · 노출 " + st.exposure];
    case 2: return c.area ? ["done", "완료", c.size_mm ? c.size_mm[0] + " × " + c.size_mm[1] + " mm" : "지정함"] : ["need", "필요", "사진에서 네 꼭짓점"];
    case 3: return s.ref ? ["done", "완료", s.ref.time] : ["need", "필요", "판을 비우고 촬영"];
    case 4: return [c.status, c.status === "done" ? "완료" : c.status === "invalid" ? "무효" : "필요", c.text];
    case 5: return [c.head.status, c.head.status === "done" ? "완료" : c.head.status === "invalid" ? "무효" : "필요", c.head.text];
    case 6: { const t = s.tool; const set = (t.dx_mm || t.dy_mm); return [set ? "done" : "opt", set ? "완료" : "선택", t.name + " · dx " + f1(t.dx_mm, 2) + " · dy " + f1(t.dy_mm, 2)]; }
    case 7: { const ok = s.temp.kind !== "none"; return [ok ? "done" : "opt", ok ? "완료" : "선택", "온도 " + (ok ? "있음" : "없음") + " · 계측기 " + (s.meter.driver === "none" ? "미연결" : "연결")]; }
  }
}
function lockedBy(s) { return s.run && ["running", "paused", "stopping"].includes(s.run.state) ? "측정 중 · 변경 잠김" : ""; }

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
  setInterval(() => { const s = UI.state; if (s && UI.tab === "setup" && S.view === "live" && S.viewer) { const sz = s.camera.size || [1280, 960]; S.viewer.set("/preview.jpg", sz[0], sz[1], Date.now()); } }, 250);
  document.addEventListener("keydown", onJogKey);
  S.built = true;
}

function renderSetup(s) {
  if (!S.built) buildSetup();
  if (UI.tab !== "setup") return;
  /* 왼쪽 목록 */
  const items = [1, 2, 3, 4, 5, 6, 7].map((i) => {
    const [cls, st, sum] = stepInfo(s, i);
    return h("div", { class: "sitem " + cls + (S.step === i ? " sel" : ""), "data-act": "su-step", "data-args": JSON.stringify({ i }) }, h("div", { class: "no", text: i }),
      h("div", { class: "grow" }, h("div", { class: "t", text: STEPS[i - 1] }), h("div", { class: "s", text: sum })), h("div", { class: "st", text: st }));
  });
  const lsig = JSON.stringify(items.map((e) => e.outerHTML)) + S.step;
  if (lsig !== S.leftSig) {
    S.leftSig = lsig;
    S.list.replaceChildren(...items, h("div", { class: "sub3", text: "카메라를 옮기면 2–5 다시 설정 · 가열 측정에는 6 · 7 필요" }),
      h("div", { class: "row" }, btn("고급 설정", "su-adv", null, "", "속도 · 가동범위 · 촬영 위치 · 대기 위치 · 검출 값 등 모든 설정을 엽니다."), btn("이전 프로그램 설정 가져오기", "su-import", null, "sm", "이전 프로그램의 포트 · 카메라 · 가동범위 · 속도 · 위치 · 릴레이 · 쿨러 · 공구 · 온도 설정을 읽어 옵니다. 보정 파일은 가져오지 않습니다.", lockedBy(s))));
  }
  renderSetupView(s);
  const sig = JSON.stringify([S.step, lockedBy(s), s.calib, s.ref, s.stage && [s.stage.locked, s.stage.z_ok, s.stage.xy_ok, s.stage.moving], s.camera.busy, s.settings.photohome, s.tool, s.verify, S.jogStep, s.temp.kind, s.settings.relay.items, s.settings.cooler, s.settings.heater, s.settings.temp, s.settings.contact, s.settings.meter, s.settings.width, s.settings.exposure, s.settings.wb_temperature]);
  if (sig !== S.rightSig) { S.rightSig = sig; renderPanel(s); }
  /* 조그 위치 */
  const pos = $("#su-pos");
  if (pos && s.stage) pos.textContent = xy(s.stage.x, s.stage.y) + " · Z " + f1(s.stage.z, 2);
}
UI.acts["su-step"] = (a) => { S.step = a.i; if (UI.state && !UI.state.ref && S.view === "ref") S.view = "live"; else if (a.i === 3 && UI.state.ref) S.view = "ref"; S.leftSig = ""; S.rightSig = ""; S.area = null; render(); };

function renderSetupView(s) {
  const v = S.viewer, seg = h("div", { class: "seg" }, ...[["live", "실시간"], ["snap", "단발 촬영"], ["shot", "촬영 사진"], ["ref", "빈 판 사진"]].map(([k, l]) => btn(l, "su-view", { k }, S.view === k ? "on" : "")));
  S.vhead.replaceChildren(seg, btn("지금 한 장", "su-snap", null, "sm", "지금 자리에서 단발 촬영을 합니다.", s.camera.busy ? s.camera.busy + " 중" : ""), h("span", { class: "grow" }),
    btn("판 맞춤", "su-fit", { m: "plate" }, "sm " + (S.zoom === "plate" ? "on" : "")), btn("전체", "su-fit", { m: "all" }, "sm " + (S.zoom === "all" ? "on" : "")), btn("−", "su-zoom", { f: 0.8 }, "sm"), btn("+", "su-zoom", { f: 1.25 }, "sm"));
  S.vfoot.replaceChildren(h("span", { text: "점선 = 판 영역 · 번호 = 기준점" }), h("span", { class: "grow" }), h("span", { class: "mono", text: s.camera.text }));
  const sz = s.camera.size || [1280, 960];
  v.mode = S.zoom;
  v.area = s.calib.area;
  if (S.view === "snap") v.set("/snap.jpg", sz[0], sz[1], s.camera.busy + (S.snapV || 0));
  else if (S.view === "shot") s.shot ? v.set("/shot.jpg", s.shot.size[0], s.shot.size[1], s.shot.id) : v.set(null);
  else if (S.view === "ref") s.ref ? v.set("/ref.jpg", sz[0], sz[1], s.ref.id) : v.set(null);
  else if (!v.W) v.set("/preview.jpg", sz[0], sz[1], Date.now());
  if (S.step === 2) {
    if (!S.area) S.area = (s.calib.area || defaultArea(sz)).map((p) => p.slice());
    v.handles = S.area;
  } else v.handles = null;
  v.message(s.camera.cap_text && s.camera.busy ? s.camera.busy + " · " + s.camera.cap_text : "");
  redrawSetup();
}
function defaultArea(sz) { const w = sz[0], h_ = sz[1]; return [[w * 0.25, h_ * 0.2], [w * 0.75, h_ * 0.2], [w * 0.75, h_ * 0.8], [w * 0.25, h_ * 0.8]]; }
function frameOfView() { return S.view === "ref" ? "ref" : S.view === "snap" ? "snap" : S.view === "shot" ? "shot" : null; }
UI.acts["su-view"] = (a) => { S.view = a.k; S.viewer.W = 0; S.viewer.key = null; if (UI.ws) UI.ws.send(JSON.stringify({ cmd: "preview", on: a.k === "live" })); render(); };
UI.acts["su-fit"] = (a) => { S.zoom = a.m; S.viewer.fit(a.m, S.viewer.area); render(); };
UI.acts["su-zoom"] = (a) => S.viewer.zoomCenter(a.f);
UI.acts["su-snap"] = async () => { const r = await send("snap"); S.snapV = Date.now(); S.view = "snap"; S.viewer.W = 0; S.viewer.key = null; render(); if (r.ok && r.res && !r.res.aligned && r.res.why) toast("정렬 없음 · " + r.res.why, true); };

function redrawSetup() {
  const s = UI.state, v = S.viewer;
  if (!s || !v) return;
  v.clear();
  if (!v.W) return;
  const k = v.k, lw = (n) => n / k;
  const fr = frameOfView(), A = s.shot && s.shot.cam && s.shot.cam.M && fr === "shot" ? s.shot.cam.M : null;
  const toView = (p) => (A ? applyM(A, p) : p);
  const area = S.step === 2 && S.area ? S.area : (s.calib.area ? (fr === "shot" && s.shot.area ? s.shot.area : s.calib.area) : null);
  if (area) v.svg.append(sv("polygon", { points: area.map((p) => p.join(",")).join(" "), fill: "rgba(47,111,219,.08)", stroke: "#8fb4f2", "stroke-width": lw(2), "stroke-dasharray": lw(9) + " " + lw(6) }));
  if (S.step === 2 && S.area) S.area.forEach((p, i) => { v.svg.append(sv("circle", { cx: p[0], cy: p[1], r: lw(9), fill: "#2f6fdb", stroke: "#fff", "stroke-width": lw(2) })); v.svg.append(sv("text", { x: p[0] + lw(12), y: p[1] - lw(10), "font-size": lw(15), fill: "#fff", "font-weight": 700 }, String(i + 1))); });
  if (S.step === 4 || S.step === 5) {
    const pts = S.step === 4 ? s.calib.points : s.calib.head.pairs;
    pts.forEach((p, i) => {
      const q = fr === "shot" || fr === "snap" ? p : [p.u, p.v];
      const uv = fr === "ref" || !fr ? [p.u, p.v] : [p.u, p.v];
      v.svg.append(sv("circle", { cx: uv[0], cy: uv[1], r: lw(8), fill: "none", stroke: "#e08a1e", "stroke-width": lw(2.5) }));
      v.svg.append(sv("text", { x: uv[0] + lw(10), y: uv[1] - lw(8), "font-size": lw(15), fill: "#ffe08a", "font-weight": 700 }, String(p.no || i + 1)));
      void q; void toView;
    });
  }
  if (S.view === "snap" || S.view === "live") S.hint = "";
}
function applyM(M, p) { const w = M[2][0] * p[0] + M[2][1] * p[1] + M[2][2]; return [(M[0][0] * p[0] + M[0][1] * p[1] + M[0][2]) / w, (M[1][0] * p[0] + M[1][1] * p[1] + M[1][2]) / w]; }

async function onSetupClick(u, v) {
  const s = UI.state, fr = frameOfView();
  if (S.step === 4) {
    if (!fr) { toast("단발 촬영 · 촬영 사진 · 빈 판 사진에서 클릭", true); return; }
    if (!s.calib.pending) { toast("위치 기록 먼저", true); return; }
    const r = await send("calib_click", { u, v, frame: fr });
    if (r.ok) { toast("기준점 기록"); }
  } else if (S.step === 6 && S.tipClick) {
    if (!fr) { toast("단발 촬영에서 클릭", true); return; }
    const r = await send("tool_m2_click", { u, v, frame: fr });
    S.tipClick = false;
    if (r.ok) toast("공구 오프셋 저장");
  }
}

/* ---- 조그 ---- */
function onJogKey(e) {
  if (UI.tab !== "setup" || S.step !== 4) return;
  const t = e.target;
  if (t && /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName)) return;
  const map = { ArrowLeft: ["y", 1], ArrowRight: ["y", -1], ArrowUp: ["x", 1], ArrowDown: ["x", -1], PageUp: ["z", -1], PageDown: ["z", 1] };
  const m = map[e.key];
  if (!m) return;
  e.preventDefault();                                   // 화살표 키가 스텝 선택이나 숫자 칸을 바꾸지 않는다
  jog(m[0], m[1]);
}
function jog(axis, sign) {
  const s = UI.state;
  if (!s.stage || s.stage.moving) return;
  send("jog", { axis, mm: sign * S.jogStep });
}
UI.acts["su-jog"] = (a) => jog(a.axis, a.sign);
UI.acts["su-jstep"] = (a) => { S.jogStep = a.v; S.rightSig = ""; render(); };

/* ---- 오른쪽 패널 ---- */
function field(label, input) { return [h("label", { text: label }), input]; }
function numInput(id, val, step) { return h("input", { type: "number", id, value: val, step: step || "any" }); }
function selInput(id, val, opts) { return h("select", { id }, opts.map(([v, l]) => h("option", { value: v, text: l, selected: String(v) === String(val) }))); }
function saveFields(map, after) {
  return async () => {
    const patch = {};
    for (const k in map) { const el = $("#" + map[k]); if (!el) continue; patch[k] = el.type === "number" ? Number(el.value) : el.type === "checkbox" ? el.checked : el.value; }
    const r = await send("settings_save", { patch });
    if (r.ok) toast("저장"); if (after) after(r);
  };
}

function renderPanel(s) {
  const P = S.panel, lock = lockedBy(s), st = s.settings, c = s.calib, nostage = !s.stage;
  const kids = [];
  const card = (title, ...x) => h("div", { class: "card" }, h("h3", { text: title }), ...x);
  const jogCard = () => {
    const sg = s.stage;
    const dis = nostage ? "스테이지 연결 안 됨" : sg.moving ? "이동 중" : "";
    const b = (l, axis, sign) => btn(l, "su-jog", { axis, sign }, "", "", dis);
    return card("조그", h("div", { class: "mono", id: "su-pos", text: sg ? xy(sg.x, sg.y) + " · Z " + f1(sg.z, 2) : "—" }),
      h("div", { class: "row" }, ...[0.1, 1, 10, 50].map((v) => btn(String(v), "su-jstep", { v }, "sm " + (S.jogStep === v ? "on" : "")))),
      h("div", { class: "row" }, h("div", { class: "pad" }, h("span"), b("X+", "x", 1), h("span"), b("Y+", "y", 1), h("span", { class: "sub3", text: "mm" }), b("Y−", "y", -1), h("span"), b("X−", "x", -1), h("span")),
        h("div", { class: "col" }, b("Z↑", "z", -1), b("Z↓", "z", 1))),
      sg && sg.z > 0.5 ? h("div", { class: "why", text: "Z 가 내려가 있음 · 다른 점으로 가기 전 Z↑" }) : null,
      h("div", { class: "sub3", text: "화살표 키 = X Y · PageUp / PageDown = Z" }));
  };
  if (S.step === 1) {
    kids.push(card("카메라", h("div", { class: "kv" }, ...field("카메라 번호", numInput("f-ci", st.camera_index, 1)), ...field("가로", numInput("f-w", st.width, 1)), ...field("세로", numInput("f-h", st.height, 1)),
      ...field("노출", numInput("f-ex", st.exposure)), ...field("화이트밸런스 K", numInput("f-wb", st.wb_temperature, 100)),
      ...field("초점", selInput("f-fm", st.focus_mode, [["fixed", "고정"], ["auto", "자동"]]))),
      h("div", { class: "row" }, btn("저장", "su-save-cam", null, "primary", "", lock), btn("초점 다시 맞추기", "su-focus", null, "", "판 영역 안의 선명도로 초점을 다시 찾습니다.", lock)),
      h("div", { class: "sub3", text: "카메라 설정을 바꾸면 2–5 를 다시 확인" })));
  } else if (S.step === 2) {
    const sz = c.size_mm;
    kids.push(card("판 영역", h("div", { class: "sub", text: "사진에서 네 꼭짓점을 끌어 판 사각형에 맞춥니다." }),
      h("div", { class: "kv" }, h("span", { text: "실측 크기" }), h("b", { text: sz ? sz[0] + " × " + sz[1] + " mm" : "좌표 보정 뒤 표시" })),
      h("div", { class: "row" }, btn("적용", "su-area", null, "primary", "", lock || (frameOfView() ? "" : "사진 보기 필요 · 단발 촬영 / 빈 판 사진")), btn("초기화", "su-area-reset", null, "sm")),
      h("div", { class: "sub3", text: "빈 판 사진이 있으면 그 사진 위에서 지정" })));
  } else if (S.step === 3) {
    const hist = (s.ref && s.ref.history) || [];
    kids.push(card("빈 판 사진", h("div", { class: "sub", text: "판 위의 샘플을 모두 치웁니다. 촬영 위치로 자동 이동한 뒤 찍습니다." }),
      h("div", { class: "row" }, btn("촬영 위치로 이동 · 촬영", "su-ref", null, "primary", "", lock || (c.area ? "" : "판 영역 필요") || (s.camera.busy ? s.camera.busy + " 중" : ""))),
      S.refRes ? h("div", { class: "sub", text: "교체 결과 · " + (S.refRes.moved ? "이동 " + S.refRes.moved.shift_px + " px · 잔차 " + S.refRes.moved.resid + " · " + S.refRes.moved.model + " · 성공" : "기하 이동 없음") }) : null,
      h("h3", { text: "이력" }), ...hist.slice(0, 8).map((x) => h("div", { class: "row" }, h("span", { class: "grow mono", text: x.time }), btn("되돌리기", "su-ref-restore", { id: x.id }, "sm", "이 사진으로 되돌리고 기하를 옮깁니다.", lock)))));
  } else if (S.step === 4) {
    const n = c.points.length;
    const cand = c.area ? c.area.map((p, i) => btn("꼭짓점 " + (i + 1), "su-cand", { i }, "sm", "기록한 위치에 이 꼭짓점을 기준점으로 대응시킵니다.", c.pending ? "" : "위치 기록 먼저")) : [];
    const sampleCand = UI.state.samples.slice(0, 15).map((p) => btn(String(p.no), "su-cand-s", { no: p.no }, "sm", "기록한 위치에 이 칩 가운데를 대응시킵니다.", c.pending ? "" : "위치 기록 먼저"));
    kids.push(card("좌표 보정", h("ol", { class: "sub" }, h("li", { text: "핀 가운데를 기준점에 맞추기 · Z 는 판 위 1–2 mm" }), h("li", { text: "위치 기록" }), h("li", { text: "사진에서 같은 점 클릭" })),
      h("div", { class: "row" }, btn("위치 기록 · " + (n + 1) + "번 점", "su-record", null, "primary", "", lock || (nostage ? "스테이지 연결 안 됨" : "")), c.pending ? h("span", { class: "chip warn", text: "대기 " + xy(c.pending.x, c.pending.y) }) : null)));
    kids.push(jogCard());
    const rowsT = c.points.map((p) => h("tr", {}, h("td", { text: p.no }), h("td", { text: f1(p.x) }), h("td", { text: f1(p.y) }), h("td", { text: p.res_mm != null ? f1(p.res_mm, 2) : "—" }), h("td", {}, btn("삭제", "su-pdel", { no: p.no }, "sm", "", lock))));
    kids.push(card("기준점 " + n, h("div", { class: "scroll" }, h("table", { class: "tb" }, h("tr", {}, ["점", "X", "Y", "잔차", ""].map((t) => h("th", { text: t }))), rowsT)),
      ...c.warnings.map((w) => h("div", { class: "why", text: w })),
      h("div", { class: "row" }, btn("적용 · 5점부터", "su-capply", null, "primary", "", lock || (n >= c.min_apply ? "" : "기준점 " + n + "개 · " + c.min_apply + "개부터"))),
      h("div", { class: "sub3", text: c.text }), h("div", { class: "sub3", text: "기준점 후보" }), h("div", { class: "row wrap" }, ...cand), h("div", { class: "row wrap" }, ...sampleCand)));
  } else if (S.step === 5) {
    const hc = c.head, v = s.verify;
    kids.push(card("헤드 위치 보정", h("div", { class: "sub", text: "격자 n×n 으로 자동 이동 · 촬영해 헤드 마커 위치를 잽니다." }),
      h("div", { class: "row" }, btn("격자 자동 보정", "su-head", null, "primary", "", lock || (c.status === "done" ? "" : "좌표 보정 필요") || (nostage ? "스테이지 연결 안 됨" : "")),
        btn("적용", "su-head-apply", null, "", "", lock || (hc.pairs.length >= 5 ? "" : "점 5개부터"))),
      h("div", { class: "sub", text: hc.text + " · 점 " + hc.pairs.length + (hc.rms != null ? " · 잔차 " + f1(hc.rms, 2) + " mm" : "") }),
      h("div", { class: "scroll" }, h("table", { class: "tb" }, h("tr", {}, ["X", "Y", "잔차"].map((t) => h("th", { text: t }))), hc.pairs.map((p) => h("tr", {}, h("td", { text: f1(p.x) }), h("td", { text: f1(p.y) }), h("td", { text: p.res_mm != null ? f1(p.res_mm, 2) : "—" }))))),
      h("div", { class: "row" }, btn("지금 위치 확인", "su-verify", null, "", "지금 자리에서 한 장 찍어 헤드 마커로 실제 위치를 잽니다.", lock || (hc.status === "done" ? "" : "헤드 위치 보정 필요")),
        v ? btn("적용 (sp)", "su-apply-pos", null, "primary", "위치 확인 결과를 스테이지 위치로 적용합니다. 비상정지 회복의 마지막 단계입니다.") : null),
      v ? h("div", { class: "sub", text: "마커 " + xy(v.x, v.y) + " · 펌웨어 " + xy(v.fw_x, v.fw_y) + " · 차이 " + f1(v.d, 2) + " mm · " + v.verdict }) : null),
      jogCard());
  } else if (S.step === 6) {
    const t = s.tool;
    kids.push(card("공구 오프셋 · " + t.name, h("div", { class: "kv" }, h("span", { text: "dx" }), h("b", { text: f1(t.dx_mm, 2) + " mm" }), h("span", { text: "dy" }), h("b", { text: f1(t.dy_mm, 2) + " mm" })),
      h("h3", { text: "방법 1 · 같은 점에 핀 · 공구" }), h("div", { class: "sub", text: "핀 가운데를 한 점에 맞춰 기록 → 공구 끝을 같은 점에 맞춰 기록" }),
      h("div", { class: "row" }, btn("핀 위치 기록", "su-m1", { which: "pointer" }, "", "", lock || nostage && "스테이지 연결 안 됨"), btn("공구 위치 기록", "su-m1", { which: "tool" }, "primary", "", lock || nostage && "스테이지 연결 안 됨")),
      h("h3", { text: "방법 2 · 가열 자국 클릭" }), h("div", { class: "sub", text: "시간 한도 안에서 가열한 뒤 단발 촬영에서 자국을 누릅니다." }),
      h("div", { class: "row" }, btn("가열 3 s", "su-m2", { seconds: 3 }, "", "히트건 릴레이를 3 초만 켭니다.", lock || (c.status === "done" ? "" : "좌표 보정 필요")), btn("자국 클릭 시작", "su-m2-click", null, "", "", S.tipClick ? "" : "")),
      h("div", { class: "sub3", text: S.tipClick ? "단발 촬영 사진에서 자국을 누르세요" : "" })),
      card("공구 끝 모양", drawTip(t)));
  } else if (S.step === 7) {
    const zr = st.cooler.zones;
    kids.push(card("온도 센서", h("div", { class: "kv" }, ...field("드라이버", selInput("m-td", st.temp.driver, [["none", "센서 없음"], ["dummy", "더미(dry 전용)"]])), ...field("현재 값", h("b", { text: s.temp.c == null ? "—" : s.temp.c + " °C" }))),
      h("div", { class: "row" }, btn("저장", "su-save-dev", null, "primary", "", lock), btn("연결 시험", "su-test-temp", null, "", "", ""))),
      card("계측기", h("div", { class: "kv" }, ...field("드라이버", selInput("m-md", st.meter.driver, [["none", "없음"], ["dummy", "더미(dry 전용)"]]))),
        h("div", { class: "sub", text: "채널 " + st.meter.channels.map((c) => c.name + "(" + c.unit + ")").join(", ") }), h("div", { class: "row" }, btn("연결 시험", "su-test-meter", null, ""))),
      card("접촉", h("div", { class: "kv" }, ...field("드라이버", selInput("m-cd", st.contact.driver, [["none", "사용 안 함"], ["dummy", "더미(dry 전용)"], ["manual", "수동"]]))), h("div", { class: "row" }, btn("저장", "su-save-dev", null, "primary", "", lock))),
      card("릴레이 배정", h("div", { class: "kv" }, ...field("히트건", selInput("m-hr", st.heater.relay, Array.from({ length: 10 }, (_, i) => [i + 1, "R" + (i + 1)])))),
        h("div", { class: "sub", text: "쿨러 R1–R9 · 히트건 릴레이는 가열 단계 전용" }),
        h("div", { class: "row wrap" }, ...st.relay.items.map((r, i) => h("label", { class: "chk" }, h("input", { type: "checkbox", id: "m-r" + i, checked: r.enabled }), "R" + (i + 1))))),
      card("쿨러 구역 " + zr.rows + "×" + zr.cols, h("div", { class: "sub", text: "칸마다 릴레이 번호" }),
        h("div", { class: "sgrid" }, zr.cells.map((cl, i) => h("div", { class: "row" }, h("span", { text: "칸" + (i + 1) }), h("input", { type: "number", id: "m-z" + i, value: cl.relay, min: 1, max: 10, step: 1 })))),
        h("div", { class: "row" }, btn("저장", "su-save-dev", null, "primary", "", lock))));
  }
  P.replaceChildren(...kids);
}
function drawTip(t) {
  const W = 200, Hh = 110, svg = sv("svg", { width: W, height: Hh, viewBox: "0 0 " + W + " " + Hh });
  const w = Number(t.tip_w_mm || 10), hh = Number(t.tip_h_mm || 2), a = ((t.tip_angle_deg == null ? 90 : t.tip_angle_deg) * Math.PI) / 180, sc = 6;
  const pt = (x, y) => [100 + (Math.cos(a) * x - Math.sin(a) * y) * sc, 55 - (Math.sin(a) * x + Math.cos(a) * y) * sc];
  const pts = t.tip_shape === "circle" ? null : [pt(-w / 2, -hh / 2), pt(w / 2, -hh / 2), pt(w / 2, hh / 2), pt(-w / 2, hh / 2)];
  svg.append(sv("line", { x1: 10, y1: 55, x2: 190, y2: 55, stroke: "#c3c7cc" }), sv("line", { x1: 100, y1: 5, x2: 100, y2: 105, stroke: "#c3c7cc" }));
  svg.append(pts ? sv("polygon", { points: pts.map((p) => p.join(",")).join(" "), fill: "rgba(47,111,219,.25)", stroke: "#2f6fdb" }) : sv("circle", { cx: 100, cy: 55, r: (w / 2) * sc, fill: "rgba(255,90,31,.25)", stroke: "#ff5a1f" }));
  svg.append(sv("text", { x: 192, y: 50, "text-anchor": "end", "font-size": 11, fill: "#5a626b" }, "X →"), sv("text", { x: 104, y: 14, "font-size": 11, fill: "#5a626b" }, "Y ↑"));
  return h("div", {}, svg, h("div", { class: "sub3", text: t.tip_shape + " " + w + " × " + hh + " mm · 각도 " + (t.tip_angle_deg == null ? 90 : t.tip_angle_deg) + "° (스테이지 기준)" }));
}

/* ---- 동작 ---- */
UI.acts["su-save-cam"] = saveFields({ camera_index: "f-ci", width: "f-w", height: "f-h", exposure: "f-ex", wb_temperature: "f-wb", focus_mode: "f-fm" });
UI.acts["su-focus"] = async () => { const r = await send("focus_refind"); if (r.ok) toast(r.res && r.res.msg ? r.res.msg : "초점 완료"); };
UI.acts["su-area"] = async () => { const r = await send("area_set", { points: S.area, frame: frameOfView(), size: UI.state.camera.size }); if (r.ok) { toast("판 영역 적용"); S.area = null; } };
UI.acts["su-area-reset"] = () => { S.area = defaultArea(UI.state.camera.size || [1280, 960]); S.viewer.handles = S.area; redrawSetup(); };
UI.acts["su-ref"] = async () => { const r = await send("ref_capture"); if (r.ok) { S.refRes = r.res; S.view = "ref"; S.viewer.W = 0; S.viewer.key = null; S.rightSig = ""; render(); } };
UI.acts["su-ref-restore"] = (a) => dialog("이 빈 판 사진으로 되돌릴까요?", "좌표 보정은 새 사진 기준으로 옮겨집니다. 정렬이 안 되면 거절됩니다.", [{ label: "취소" }, { label: "되돌리기", cls: "primary", fn: async () => { const r = await send("ref_restore", a); if (r.ok) { S.refRes = r.res; S.rightSig = ""; render(); } } }]);
UI.acts["su-record"] = () => send("calib_record");
UI.acts["su-pdel"] = (a) => send("calib_delete", a);
UI.acts["su-capply"] = async () => { const r = await send("calib_apply"); if (r.ok) toast("좌표 보정 적용 · 잔차 " + r.res.rms + " mm"); };
UI.acts["su-cand"] = async (a) => { const p = UI.state.calib.area[a.i]; await send("calib_click", { u: p[0], v: p[1], frame: "ref" }); };
UI.acts["su-cand-s"] = async (a) => { const p = UI.state.samples.find((x) => x.no === a.no); if (p && p.ru != null) await send("calib_click", { u: p.ru, v: p.rv, frame: "ref", src: "sample", shot: UI.state.shot && UI.state.shot.id + ":" + a.no }); };
UI.acts["su-head"] = async () => { const r = await send("head_calib"); if (r.ok) toast("헤드 위치 보정 · 점 " + r.res.n + " · 잔차 " + r.res.rms + " mm"); };
UI.acts["su-head-apply"] = async () => { const r = await send("head_apply"); if (r.ok) toast("적용"); };
UI.acts["su-verify"] = async () => { const r = await send("verify_position"); if (r.ok) toast("위치 확인 · 차이 " + r.res.d + " mm"); };
UI.acts["su-apply-pos"] = () => dialog("위치를 적용할까요?", "스테이지 위치를 마커로 잰 값으로 바꿉니다.", [{ label: "취소" }, { label: "적용", cls: "primary", fn: () => send("apply_position") }]);
UI.acts["su-m1"] = async (a) => { const r = await send("tool_m1", a); if (r.ok && r.res.dx != null) toast("공구 오프셋 dx " + r.res.dx + " dy " + r.res.dy); };
UI.acts["su-m2"] = (a) => send("tool_m2_heat", a);
UI.acts["su-m2-click"] = () => { S.tipClick = true; S.view = "snap"; toast("지금 한 장을 찍고 자국을 누르세요"); S.rightSig = ""; render(); };
UI.acts["su-save-dev"] = async () => {
  const patch = {};
  const v = (id) => { const e = $("#" + id); return e ? e.value : null; };
  if (v("m-td")) patch["temp.driver"] = v("m-td");
  if (v("m-md")) patch["meter.driver"] = v("m-md");
  if (v("m-cd")) patch["contact.driver"] = v("m-cd");
  if (v("m-hr")) patch["heater.relay"] = Number(v("m-hr"));
  const st = UI.state.settings;
  patch["relay.items"] = st.relay.items.map((r, i) => Object.assign({}, r, { enabled: $("#m-r" + i) ? $("#m-r" + i).checked : r.enabled }));
  const z = JSON.parse(JSON.stringify(st.cooler.zones));
  z.cells.forEach((c, i) => { const e = $("#m-z" + i); if (e) c.relay = Number(e.value); });
  patch["cooler.zones"] = z;
  const r = await send("settings_save", { patch });
  if (r.ok) toast("저장");
};
UI.acts["su-test-temp"] = () => toast(UI.state.temp.c == null ? "온도 읽기 실패 · 센서 없음" : "온도 " + UI.state.temp.c + " °C", UI.state.temp.c == null);
UI.acts["su-test-meter"] = () => toast(UI.state.meter.driver === "none" ? "계측기 미연결" : "계측기 응답 정상", UI.state.meter.driver === "none");
UI.acts["su-import"] = () => dialog("이전 프로그램 설정을 가져올까요?", "포트 · 카메라 · 가동범위 · 속도 · 위치 · 릴레이 · 쿨러 · 공구 · 온도 설정을 읽어 옵니다. 보정 파일은 가져오지 않으니 다시 보정하십시오.", [{ label: "취소" }, { label: "가져오기", cls: "primary", fn: async () => { const r = await send("settings_import_old"); if (r.ok) toast("가져옴 · " + r.res.imported.length + "개 항목"); } }]);

/* ---- 고급 설정: 스키마로 자동 생성 ---- */
UI.acts["su-adv"] = async () => {
  const j = await (await fetch("/schema")).json();
  const sch = j.schema, vals = j.values, locked = lockedBy(UI.state);
  const tabs = sch.tabs;
  let cur = tabs[0].id;
  const body = h("div", { class: "scroll" });
  body.style.height = "620px";
  const edits = {};
  const get = (o, k) => k.split(".").reduce((a, p) => (a == null ? a : a[p]), o);
  const draw = () => {
    body.replaceChildren();
    const g = h("div", { class: "setgrid" });
    let grp = null;
    sch.items.filter((it) => it.tab === cur && it.type !== "object" && it.editor == null).forEach((it) => {
      if (it.group !== grp) { grp = it.group; g.append(h("div", { class: "g", text: grp })); }
      const v0 = edits[it.key] !== undefined ? edits[it.key] : get(vals, it.key);
      let el;
      if (it.type === "bool") el = h("input", { type: "checkbox", checked: !!v0, onchange: (e) => { edits[it.key] = e.target.checked; } });
      else if (it.type === "enum") el = h("select", { onchange: (e) => { edits[it.key] = e.target.value; } }, (it.options || []).map((o) => h("option", { value: o.value, text: o.label, selected: o.value === v0 })));
      else if (it.type === "int" || it.type === "float") el = h("input", { type: "number", value: v0, step: it.step || "any", onchange: (e) => { edits[it.key] = Number(e.target.value); } });
      else if (it.type === "xy" || it.type === "list") el = h("input", { type: "text", value: JSON.stringify(v0), onchange: (e) => { try { edits[it.key] = JSON.parse(e.target.value); } catch (x) { toast("형식 오류", true); } } });
      else el = h("input", { type: "text", value: v0 == null ? "" : v0, onchange: (e) => { edits[it.key] = e.target.value; } });
      if (locked) el.disabled = true;
      g.append(h("div", { text: it.label + (it.unit ? " (" + it.unit + ")" : ""), "data-tip": it.tip }), el);
    });
    body.append(g);
  };
  const tabbar = h("div", { class: "row wrap" }, tabs.map((t) => btn(t.label, "noop", null, "sm", "", "").cloneNode(true)));
  tabbar.replaceChildren(...tabs.map((t) => h("button", { class: "btn sm", text: t.label, onclick: () => { cur = t.id; draw(); } })));
  draw();
  openWin(h("div", {}, h("h2", { text: "고급 설정" }), locked ? h("div", { class: "why", text: locked }) : null, tabbar, body,
    h("div", { class: "btns" }, h("button", { class: "btn", text: "닫기", onclick: closeWin }),
      h("button", { class: "btn primary" + (locked ? " dis" : ""), text: "저장", onclick: async () => {
        if (locked) return;
        const r = await send("settings_save", { patch: edits });
        if (r.ok) { closeWin(); toast("저장"); }
      } }))), "big");
};
UI.renders.push(renderSetup);
