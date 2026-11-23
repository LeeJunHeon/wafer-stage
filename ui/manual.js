/* manual.js - 수동 제어 탭: 2열. 왼쪽 = 카메라 · 이동 / 오른쪽 = 수동 측정 · 릴레이 시험 · 직접 명령(맨 아래, 작게).
   카메라 "실시간" = 실시간 미리보기. "판 사진" = 마지막으로 찍은 판 사진(자동 측정 탭과 같은 사진) + 샘플 윤곽 · 이름 · 핀 놓을 자리(사각형) + 지금 핀 자리(십자 · 사각형).
   [보정 점 추가] = [크게 보기] 창을 "보정 점 추가" 상태로 연다 - 사진에서 핀이 실제로 있는 자리를 누르고 [추가](coord_add_at · 스테이지는 안 움직임).
   실시간 화면을 보정해 보여 주지 않는 까닭: 헤드가 판 위에 있으면 헤드가 판을 가린다.
   장비가 연결되지 않았으면 그 장비가 필요한 버튼을 끄고 이유를 한 줄 보인다(서버도 거절한다 - 화면은 미리 알려 준다).
   히트건 = 송풍 + 열선. [켜기] 는 서버가 열선 → 송풍 순서로 켠다 · [끄기] 는 열선을 끄고 송풍은 heater_fan_after_s 뒤에 끈다.
   [온도 시험] 은 큰 창(ui/temptest.js)을 연다. 온도 시험 중에는 히트건 · 쿨러 버튼 · 접촉 탐색을 끈다([전체 끄기] 는 시험을 정지하고 끈다).
   판 사진 보기 아래 샘플 줄: [◀ 이전] [샘플 선택] [다음 ▶] = sample_goto(핀 놓을 자리로 · 핀 맨 위 확인) · "이동 후 내리기"(화면 값 · 시작 때 끔 · 켤 때 확인 창) =
   샘플로 간 뒤 접촉 높이까지(판 사진 · [크게 보기] 에서 샘플을 눌러도) · [자동 순회] = 포함 샘플을 차례로 이동 → 접촉 높이 → 머묾(app/jobs/tour.py) · 순회 중 [순회 정지]. */
"use strict";

const man = {};
/* 직접 명령의 빠른 버튼: 서버가 받는 것만 둔다(스테이지 = 읽기 전용 허용 목록 · 측정 장비 = 조회만. K2450 의 READ? 는 재는 동작이라 [1회 측정] 을 쓴다) */
const QUICK = { k2450: ["*IDN?", "SYST:ERR?", ":OUTP?"], k2110: ["*IDN?", "SYST:ERR?", "READ?"], stage: ["st", "rs", "p"] };
const NEED = { stage: "스테이지 연결 필요", k2110: "온도계 연결 필요", k2450: "저항계 연결 필요" };

function buildManual() {
  /* ---- 왼쪽: 카메라 ---- */
  man.view = "raw";
  man.ov = svgEl("g", {});
  man.pb = photoBox({ ar: 4 / 3, note: "카메라 연결 안 됨", center: true, cls: "fill", onfit: () => drawManualOverlay(), dirs: true });     // 칸이 허락하는 만큼 크게 · 문 기준 화면
  man.pb.box.id = "preview-box";
  man.pb.svg.append(man.ov);
  const setView = (v) => { man.view = v; man.pb.setNote("미리보기 대기 중"); man.pb.show(""); refreshPreview(); renderManual(); };
  man.photoInfo = h("span", { class: "small" });
  man.retake = busyGate("재촬영", () => apiMove("samples_find"), "sm");             // 자동 측정 탭의 재촬영과 같다(사진 + 샘플 검출)
  man.bigView = btn("크게 보기", () => openPlateBig(false), "sm");
  man.fixPart = h("span", { class: "row hidden" }, man.photoInfo, man.retake.b, man.bigView, man.retake.w);
  man.viewRaw = h("input", { type: "radio", name: "view", checked: true, onchange: () => setView("raw") });
  man.viewFix = h("input", { type: "radio", name: "view", onchange: () => setView("fix") });
  man.clickGo = h("input", { type: "checkbox", onchange: () => {          // 켜면 "판 사진" 보기로 바꾼다(실시간 보기에서도 켤 수 있다)
    if (man.clickGo.checked && man.view !== "fix") { man.viewFix.checked = true; setView("fix"); }
  } });
  man.addHere = btn("보정 점 추가", () => openPlateBig(true), "sm");          // [크게 보기] 창을 "보정 점 추가" 상태로
  /* 샘플 줄: 순서 = S.samples 중 xy 가 있는 것(표 순서) · 지금 선택 = last_click.sample */
  man.sPrev = btn("◀ 이전", () => sampleStep(-1), "sm");
  man.sNext = btn("다음 ▶", () => sampleStep(1), "sm");
  man.sSel = h("select", { onchange: () => { const id = man.sSel.value; if (id) sampleGo(id); } });
  man.sSig = "";
  man.lowerChk = h("input", { type: "checkbox", onchange: () => { if (man.lowerChk.checked) { man.lowerChk.checked = false; askLower(); } } });
  man.tourBtn = btn("자동 순회", () => (touring() ? api("tour_stop") : apiMove("tour_start")), "sm");
  man.sWhy = h("span", { class: "why nowrap" });
  man.tourLine = h("div", { class: "small" });
  man.tourRows = h("div", { class: "small tour-rows hidden" });
  man.samplePart = h("div", { class: "row hidden" }, man.sPrev, man.sSel, man.sNext, h("label", { class: "chk" }, man.lowerChk, "이동 후 내리기"),
    h("span", { class: "sp" }), man.tourBtn, man.sWhy);
  man.camNote = h("span", { class: "why nowrap" });
  man.pb.inner.addEventListener("click", (ev) => {                 // 판 사진 보기에서 사진을 누르면 그 위치로 이동(핀을 맨 위로 올린 뒤)
    if (man.view !== "fix" || !man.pb.url) return;                // 사진이 없는 빈 상자는 누를 수 없다
    const [fx, fy] = man.pb.toFrac(ev);
    plateClick(fx, fy);
  });
  const camPanel = h("div", { class: "panel", id: "man-cam" },
    h("div", { class: "row" }, h("h3", { text: "카메라" }), h("span", { class: "sp" }),
      h("label", { class: "chk" }, man.viewRaw, "실시간"), h("label", { class: "chk" }, man.viewFix, "판 사진")),
    man.pb.box,
    h("div", { class: "row" }, man.fixPart, h("label", { class: "chk" }, man.clickGo, "클릭 이동"), man.addHere, man.camNote),
    man.samplePart, man.tourLine, man.tourRows);

  /* ---- 왼쪽: 이동(조그 · 촬영 위치 · 좌표 이동을 옆으로 놓아 높이를 줄인다 - 카메라가 그만큼 커진다) ---- */
  man.jog = jogPart();
  const goInput = (ph) => h("input", { type: "number", class: "num", step: "any", placeholder: ph });
  man.gx = goInput("X"); man.gy = goInput("Y"); man.gz = goInput("Z");
  man.head = btn("촬영 위치로 이동", () => { const p = S.cfg.photo_head_xy; if (p) api("goto", { x: p[0], y: p[1] }); });
  man.headNote = h("span", { class: "why" });
  man.here = btn("지금 위치", () => fillHere());                                  // 좌표 이동 칸에 현재 X · Y · Z
  man.lower = busyGate("접촉 높이로 내리기", () => apiMove("contact_lower"), "sm");   // 현재 X · Y 에서 접촉 높이까지(측정 접촉과 같은 규칙 · 확인 창)
  man.go = btn("이동", () => {
    const x = parseFloat(man.gx.value), y = parseFloat(man.gy.value), z = man.gz.value === "" ? null : parseFloat(man.gz.value);
    if (isNaN(x) || isNaN(y)) { toast("X · Y 입력 필요"); return; }
    api("goto", z == null || isNaN(z) ? { x, y } : { x, y, z });
  });
  man.moveWhy = h("div", { class: "why hidden" });
  const movePanel = h("div", { class: "panel", id: "man-move" }, h("h3", { text: "이동" }),
    h("div", { class: "move-body" }, man.jog.el,
      h("div", { class: "move-side" },
        h("div", { class: "row" }, man.head, man.headNote),
        small("좌표 이동"),
        h("div", { class: "row" }, "X", man.gx, "Y", man.gy),
        h("div", { class: "row" }, "Z", man.gz, man.go, man.here),
        h("div", { class: "row" }, man.lower.b, man.lower.w))),
    man.moveWhy);
  const left = h("div", { class: "col", id: "man-left" }, camPanel, movePanel);

  /* ---- 오른쪽: 수동 측정 ---- */
  man.temp = h("div", { class: "big", text: "—" });
  man.ohm = h("div", { class: "big", text: "—" });
  man.cond = h("div", { class: "small" });
  man.mon = btn("연속 측정", () => api("monitor", { on: !S.monitor }));
  man.readT = btn("1회 측정", () => api("read_temp"));
  man.readR = btn("1회 측정", () => api("read_resistance"));
  man.condBtn = btn("조건", openCond);
  man.measWhy = h("div", { class: "why hidden" });
  man.contact = busyGate("접촉 탐색", () => apiMove("contact_find"));
  man.contactLine = h("div", { class: "small" });
  const measure = h("div", { class: "panel" }, h("h3", { text: "수동 측정" }),
    h("div", { class: "reading" }, h("span", { class: "lbl", text: "온도 · K2110" }), man.temp, man.readT),
    h("div", { class: "reading" }, h("span", { class: "lbl", text: "저항 · K2450" }), man.ohm, man.readR, man.mon),
    h("div", { class: "row" }, man.cond, h("span", { class: "sp" }), man.contact.b, man.condBtn), man.contactLine, man.measWhy);

  /* ---- 오른쪽: 릴레이 시험(히트건 줄 · 쿨러 줄). 송풍 · 열선 · 미배정 릴레이는 개별 버튼으로 두지 않는다 ---- */
  man.hOn = btn("켜기", () => api("heater_on"));
  man.hEl = btn("열선 끄기", () => api("heater_element", { on: !(S.heater && S.heater.element) }));
  man.hOff = btn("끄기", () => api("heater_off"));
  man.hNow = btn("즉시 끄기", () => api("heater_off_now"));                    // 열선 · 송풍 바로 끔(확인 창 없음)
  man.hPct = h("input", { type: "number", class: "num", min: "0", max: "100", step: "0.1", value: "30" });          // 0.1 % 단위(V18)
  man.hPctGo = btn("적용", () => { const v = parseFloat(man.hPct.value); if (!isFinite(v)) { toast("출력 % 숫자 필요"); return; } api("heater_power", { pct: Math.round(v * 10) / 10 }); });
  man.hPctNow = h("span", { class: "small nowrap" });
  man.hState = h("span", { class: "small nowrap" });
  man.hSync = h("div", { class: "small" });                                  // 전원 맞춤 한 줄(core.js syncText)
  man.coolBox = h("span", { class: "relays" });
  man.coolSig = "";
  man.allOff = btn("전체 끄기", () => api("relays_off"));
  man.relWhy = h("div", { class: "why hidden" });
  man.tt = btn("온도 시험", openTempTest);
  man.ttLine = h("span", { class: "small nowrap" });
  const relay = h("div", { class: "panel" }, h("h3", { text: "릴레이 시험" }),
    h("div", { class: "row" }, h("span", { class: "lbl small", text: "히트건" }), man.hOn, man.hEl, man.hOff, man.hNow, man.hState),
    h("div", { class: "row" }, h("span", { class: "lbl small", text: "열선 출력" }), man.hPct, small("%"), man.hPctGo, man.hPctNow),
    man.hSync,
    h("div", { class: "small", text: "켜는 순서: 열선 → 송풍(자동) · 출력은 송풍 켜진 동안만" }),
    h("div", { class: "row" }, h("span", { class: "lbl small", text: "쿨러" }), man.coolBox, h("span", { class: "sp" }), man.allOff),
    h("div", { class: "row" }, h("span", { class: "lbl small", text: "온도" }), man.tt, man.ttLine),
    man.relWhy);

  /* ---- 오른쪽 맨 아래: 직접 명령(작게 - 결과는 최근 몇 줄만 보이는 상자, 넘치면 그 안에서만 스크롤) ---- */
  man.target = h("select", { onchange: () => { man.quick(); } }, h("option", { value: "k2450", text: "저항계 K2450" }), h("option", { value: "k2110", text: "온도계 K2110" }), h("option", { value: "stage", text: "스테이지" }));
  man.quickBox = h("span", { class: "quick" });
  man.line = h("input", { type: "text", class: "grow mono", spellcheck: "false", placeholder: "한 줄 입력", onkeydown: (e) => { if (e.key === "Enter") sendRaw(); } });
  man.log = h("div", { id: "cmd-log" });
  man.quick = () => setKids(man.quickBox, ...QUICK[man.target.value].map((q) => btn(q, () => { man.line.value = q; sendRaw(); })));
  man.quick();
  const cmd = h("div", { class: "panel", id: "man-cmd" },
    h("div", { class: "row" }, h("h3", { text: "직접 명령" }), small("읽기 · 조회만 가능"), h("span", { class: "sp" }), man.target),
    h("div", { class: "row" }, man.quickBox, man.line, btn("전송", sendRaw)), man.log);
  const right = h("div", { class: "col", id: "man-right" }, measure, relay, cmd);
  setKids($("#tab-manual"), left, right);
}
/* 판 사진에서 누른 자리(사진의 비율 0~1)로 클릭 이동 - 작은 사진 · [크게 보기] 창이 같은 규칙([클릭 이동] 켬 · 측정 중 아님) */
async function plateClick(fx, fy) {
  if (fx < 0 || fx > 1 || fy < 0 || fy > 1) return;
  if (!man.clickGo.checked || man.clickGo.disabled || running()) { if (!man.clickGo.checked) toast("클릭 이동 꺼짐"); return; }
  const sz = S.cfg.plate_size_mm;
  const r = await api("plate_goto", { x: fx * sz, y: fy * sz, view: true, lower: lowerOn() });   // 화면(문 기준) 판 mm - 서버가 저장 좌표로 바꾼다 · 샘플에 붙었을 때만 내린다
  if (r.ok) fillHere(r.result && r.result.pos);                               // 클릭 이동 뒤에도 좌표 이동 칸을 채운다
}
/* 샘플 줄: 이동할 수 있는 샘플(xy 있음 · 표 순서) · 지금 선택(last_click.sample 이름) */
const goSamples = () => (S.samples || []).filter((s) => s.xy);
const lowerOn = () => !!(man.lowerChk && man.lowerChk.checked && !man.lowerChk.disabled);
function curSampleIdx() {
  const lc = (S.prep || {}).last_click, list = goSamples();
  return lc && lc.sample ? list.findIndex((s) => s.name === lc.sample) : -1;
}
async function sampleGo(id) {
  const r = await api("sample_goto", { id: Number(id), lower: lowerOn() });
  if (r.ok) fillHere(r.result && r.result.pos);
}
function sampleStep(d) {
  const list = goSamples();
  if (!list.length) return;
  const i = curSampleIdx(), j = i < 0 ? (d > 0 ? 0 : list.length - 1) : i + d;
  if (j < 0 || j >= list.length) return;
  sampleGo(list[j].id);
}
/* "이동 후 내리기" 를 켤 때 한 번 확인(내용은 서버 move_plan "sample_lower" - 지금 접촉 높이 · 저속 거리) */
async function askLower() {
  const p = await api("move_plan", { name: "sample_lower" });
  if (!p.ok || !p.result) return;
  openDialog(p.result.title, planBody(p.result),
    [noEnter(btn("취소", closeDialog)), noEnter(btn("켜기", () => { closeDialog(); if (!man.lowerChk.disabled) man.lowerChk.checked = true; }, "on"))]);
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
}
/* 자동 순회: 버튼([자동 순회] / [순회 정지]) · 진행 한 줄 · 끝난 뒤 결과 목록(이름 · 저항 또는 "기록 없음"). 반환 = 시작할 수 없는 까닭 */
function renderTour(stage, ready, locked, ttOn) {
  const t = S.tour || {}, on = !!t.active, c = S.cfg || {}, ht = S.heater || {};
  const nUse = (S.samples || []).filter((s) => s.use && s.xy).length;
  const why = on ? "" : running() ? "측정 중 사용 불가" : ttOn ? "온도 시험 중 사용 불가" : !stage ? NEED.stage : locked ? "비상정지 해제 필요"
    : (ht.fan || ht.element) ? "히트건 끈 뒤 순회" : !(S.homed.x && S.homed.y && S.homed.z) ? "원점 필요 (초기 설정 2)" : !ready ? "초기 설정 5 후 사용 가능"
    : c.contact_z_mm == null ? "접촉 높이 필요 (초기 설정 6)" : !nUse ? "포함 샘플 없음" : "";
  man.tourBtn.textContent = on ? "순회 정지" : "자동 순회";
  man.tourBtn.classList.toggle("on", on);
  man.tourBtn.disabled = !on && !!why;
  man.tourLine.textContent = on ? "순회 " + t.i + "/" + t.n + " · " + (t.name || "") + " · " + (t.step || "") : t.result ? "순회 결과 · " + t.result : "";
  man.tourLine.className = "small" + (on ? " st-warn" : "");
  const rows = t.rows || [], sig = on ? "" : JSON.stringify(rows);
  if (sig !== man.tourSig) {
    man.tourSig = sig;
    setKids(man.tourRows, ...(on ? [] : rows.map((r) => h("div", { text: r.name + " · " + (r.ohm == null ? "기록 없음" : fmtOhm(r.ohm)) + (r.note ? " · " + r.note : "") }))));
  }
  man.tourRows.classList.toggle("hidden", on || !rows.length);
  return why;
}
/* 좌표 이동 칸(X · Y · Z)에 현재 위치(또는 받은 위치) */
function fillHere(pos) {
  const p = pos || S.pos || {};
  const put = (el, v) => { el.value = v == null ? "" : Number(v).toFixed(2); };
  put(man.gx, p.x); put(man.gy, p.y); put(man.gz, p.z);
}
/* 핀 긴 쪽 방향(화면 판 좌표 단위 벡터): 스테이지 긴 쪽 축(pin_long_axis)을 그 자리(스테이지 X · Y)에서 stageToView 로 옮긴 방향. 변환이 없으면 null */
function pinDir(sx, sy) {
  const ax = S.cfg.pin_long_axis === "y";
  const a = stageToView(ax ? sx : sx - 1, ax ? sy - 1 : sy), b = stageToView(ax ? sx : sx + 1, ax ? sy + 1 : sy);
  if (!a || !b) return null;
  const dx = b[0] - a[0], dy = b[1] - a[1], n = Math.hypot(dx, dy);
  return n > 1e-9 ? [dx / n, dy / n] : null;
}
/* 핀 사각형 꼭짓점(화면 판 mm · 가운데 c · 긴 쪽 pin_long_mm · 짧은 쪽 pin_short_mm) */
function pinRect(c, d) {
  const L = S.cfg.pin_long_mm / 2, W = S.cfg.pin_short_mm / 2, v = [-d[1], d[0]];
  return [[L, W], [L, -W], [-L, -W], [-L, W]].map(([a, b]) => [c[0] + a * d[0] + b * v[0], c[1] + a * d[1] + b * v[1]]);
}
/* 판 사진 덧그림(화면 판 mm 좌표 → 그림 좌표 u 배): 샘플 윤곽 · 이름 · 핀 놓을 자리(가는 사각형) · 누른 위치 · 핀 십자 + 사각형.
   작은 사진(u = 1, viewBox 가 mm) · [크게 보기](u = px/mm)가 같이 쓴다. o.add = 보정 점 추가 상태(선택 위치 원 대신 누른 자리 o.pick = 원 + 점선 핀 사각형).
   좌표는 서버가 화면 방향(문 기준)으로 준 값(voutline · vplate · vpin · last_click.vplate · coords.to_view + warp = stageToView)만 쓴다 */
function plateOverlayKids(k, u, o) {
  o = o || {};
  const sz = S.cfg.plate_size_mm, kids = [], pr = S.prep || {};
  const halo = (tag, a, wd) => { kids.push(svgEl(tag, Object.assign({}, a, { stroke: "#FFFFFF", "stroke-width": (wd + 2.5) * k, fill: "none" })), svgEl(tag, Object.assign({}, a, { stroke: "#1F2329", "stroke-width": wd * k, fill: "none" }))); };
  const poly = (pts) => pts.map((p) => p[0] * u + "," + p[1] * u).join(" ");
  const selName = pr.last_click && pr.last_click.sample;           // 선택된 샘플(샘플 줄 · 클릭 이동)은 윤곽을 굵게
  for (const s of S.samples || []) {
    halo("polygon", { points: poly(s.voutline), "stroke-linejoin": "round" }, s.name === selName ? 3 : 1.5);
    const d = s.vpin && s.xy ? pinDir(s.xy[0], s.xy[1]) : null;
    if (d) halo("polygon", { points: poly(pinRect(s.vpin, d)) }, 0.75);
    kids.push(svgEl("text", { x: s.vplate[0] * u, y: Math.min(...s.voutline.map((p) => p[1])) * u - 4 * k, "font-size": 12 * k, "text-anchor": "middle", fill: "#1F2329", stroke: "#FFFFFF", "stroke-width": 3 * k, "paint-order": "stroke", text: s.name }));
  }
  const lc = pr.last_click;
  if (!o.add && lc && lc.vplate) halo("circle", { cx: lc.vplate[0] * u, cy: lc.vplate[1] * u, r: 6 * k }, 1.5);
  const pos = S.pos && S.pos.x != null && S.pos.y != null ? S.pos : null;
  const pin = pinView();
  const pd = pos ? pinDir(pos.x, pos.y) : null;
  if (pin && pin[0] >= 0 && pin[0] <= sz && pin[1] >= 0 && pin[1] <= sz) {
    const x = pin[0] * u, y = pin[1] * u, r = 9 * k;
    halo("path", { d: "M" + (x - r) + " " + y + "H" + (x + r) + "M" + x + " " + (y - r) + "V" + (y + r) }, 1.5);
    if (pd) halo("polygon", { points: poly(pinRect(pin, pd)) }, 1.5);
  }
  if (o.add && o.pick) {
    halo("circle", { cx: o.pick[0] * u, cy: o.pick[1] * u, r: 6 * k }, 1.5);
    if (pd) halo("polygon", { points: poly(pinRect(o.pick, pd)), "stroke-dasharray": 4 * k + " " + 3 * k }, 1.5);
  }
  return kids;
}
/* 지금 핀 자리(화면 판 mm · 좌표 변환) - 없으면 null */
function pinView() {
  return S.pos && S.pos.x != null && S.pos.y != null ? stageToView(S.pos.x, S.pos.y) : null;
}
/* [크게 보기]: 판 사진(보정한 사진 - 원본 해상도 그대로)을 확대 · 이동 · 돋보기로 보고, 누른 자리로 클릭 이동(꼭짓점 · 확정 없음).
   옆 칸 두 상태: 보통(클릭 이동 · [보정 점 추가] · 보정 점 n개 · [마지막 지우기]) / 보정 점 추가(누른 자리 = 핀이 실제로 있는 자리 · 스테이지 안 움직임 · [추가] [취소]) */
function openPlateBig(addMode) {
  const ph = (S.prep || {}).photo, pl = (S.prep || {}).plate;
  if (!ph || !ph.rect || !pl) { toast("사진 없음"); return; }
  const url = "/api/img/plate.jpg?v=" + ph.id + "-" + pl.t;          // 화면 방향(문 기준) 판 사진 - 정사각형이라 크기는 그대로
  const st = { add: !!addMode, pick: null, busy: false };
  const z = zoomView({ src: "plate", empty: "사진 없음",
    draw: (g, k) => setKids(g, ...plateOverlayKids(k, z.W / S.cfg.plate_size_mm, st)),
    onDown: (p) => {
      if (st.add) { const sz = S.cfg.plate_size_mm; st.pick = [p[0] / z.W * sz, p[1] / z.H * sz]; render(); }     // 화면 판 mm · 스테이지는 안 움직인다
      else plateClick(p[0] / z.W, p[1] / z.H);
      return false;
    } });
  const go = h("input", { type: "checkbox", checked: man.clickGo.checked, onchange: () => { man.clickGo.checked = go.checked; } });
  const why = h("div", { class: "why" });
  const toAdd = btn("보정 점 추가", () => { st.add = true; st.pick = null; render(); });
  const nLine = h("div", { class: "small" });
  const delLast = btn("마지막 지우기", async () => {
    const n = (((S.prep || {}).coords || {}).extra || []).length;
    if (!n) return;
    const r = await api("coord_remove", { index: n - 1 });
    if (r.ok) toast("보정 점 지움 · " + r.result.n + "개");
  }, "sm");
  const normal = h("div", { class: "col" }, h("h4", { text: "판 사진" }), h("label", { class: "chk" }, go, "클릭 이동"), small("켜면 누른 위치로 이동 · 핀 맨 위로 올린 뒤"),
    h("div", { class: "row" }, toAdd), why, small("십자 · 사각형 = 핀 · 원 = 선택 위치"), nLine, h("div", { class: "row" }, delLast));
  const dist = h("div", { class: "small" });
  const addBtn = btn("추가", async () => {
    if (!st.pick || st.busy) return;
    st.busy = true; render();
    try {
      const r = await api("coord_add_at", { x: st.pick[0], y: st.pick[1], view: true });
      if (r.ok) {
        const q = r.result;
        toast((q.replaced ? "보정 점 교체 · " : "보정 점 추가 · ") + q.n + "개 · 계산 위치와 " + q.moved.toFixed(2) + " mm 보정");
        st.add = false; st.pick = null;
      }
    } finally { st.busy = false; render(); }
  }, "on");
  const cancel = btn("취소", () => { st.add = false; st.pick = null; render(); });
  const adding = h("div", { class: "col hidden" }, h("h4", { text: "보정 점 추가" }), small("핀이 실제로 있는 자리를 사진에서 누름"), small("십자 · 사각형 = 계산 위치"),
    dist, h("div", { class: "row" }, addBtn, cancel));
  const side = h("div", { class: "col" }, normal, adding);
  const render = () => {
    go.disabled = man.clickGo.disabled; if (go.disabled) go.checked = false; else go.checked = man.clickGo.checked;
    const addOff = man.addHere.disabled;
    if (st.add && addOff && !st.busy) { st.add = false; st.pick = null; }      // 동작이 시작되면(측정 · 순회 · 이동 · 끊김) 보통 상태로
    normal.classList.toggle("hidden", st.add);
    adding.classList.toggle("hidden", !st.add);
    toAdd.disabled = addOff;
    setWhy(why, man.camWhy);                                     // 막는 까닭이 있을 때만(안내 글은 아래 한 줄)
    const n = (((S.prep || {}).coords || {}).extra || []).length;
    nLine.textContent = "보정 점 " + n + "개";
    delLast.classList.toggle("hidden", !n);
    delLast.disabled = running() || touring();
    const pv = pinView();
    dist.textContent = !st.pick ? "자리 선택 필요" : pv ? "계산 위치와 " + Math.hypot(st.pick[0] - pv[0], st.pick[1] - pv[1]).toFixed(2) + " mm" : "";
    addBtn.disabled = !st.pick || st.busy || addOff;
    z.redraw();
  };
  const img = new Image();                                       // 판 사진의 실제 크기(보정 사진 · 1 mm = 8 px)를 읽고 연다
  img.onload = () => {
    openZoomDialog("판 사진", z, side, [btn("닫기", closeDialog)], render);
    z.set([img.naturalWidth, img.naturalHeight], url, url, 0);
    render();
  };
  img.onerror = () => toast("사진 읽기 실패");
  img.src = url + vq();
}
function logLine(cls, text) {
  const t = new Date();
  const hh = [t.getHours(), t.getMinutes(), t.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
  man.log.append(h("div", { class: cls, text: hh + " " + text }));
  while (man.log.childElementCount > 200) man.log.firstChild.remove();
  man.log.scrollTop = man.log.scrollHeight;
}
async function sendRaw() {
  const line = man.line.value.trim();
  if (!line) return;
  const target = man.target.value;
  logLine("tx", "→ " + line);
  man.line.value = "";
  const r = await api("raw", { target, line }, true);
  if (r.ok) (r.result.reply || "").split("\n").forEach((l) => l && logLine("rx", "← " + l));
  else logLine("er", "! " + r.msg);
}
function openCond() {
  const c = S.cfg;
  const volt = h("input", { type: "number", step: "0.01", value: c.meter_volt }), four = h("input", { type: "checkbox", checked: !!c.meter_four_wire }),
    avg = h("input", { type: "number", step: "1", value: c.meter_avg_count }), nplc = h("input", { type: "number", step: "0.01", value: c.meter_nplc }),
    ilim = h("input", { type: "number", step: "any", value: c.meter_ilim_a });
  const save = async () => {
    const ok = await putSettings({ meter_volt: parseFloat(volt.value), meter_four_wire: four.checked, meter_avg_count: parseInt(avg.value, 10), meter_nplc: parseFloat(nplc.value), meter_ilim_a: parseFloat(ilim.value) });
    if (ok) closeDialog();
  };
  openDialog("저항 측정 조건", h("div", { class: "form" }, "측정 전압 (V)", volt, "4단자", four, "평균 횟수", avg, "NPLC", nplc, "전류 한도 (A)", ilim),
    [btn("닫기", closeDialog), btn("저장", save, "on")]);
}
/* 이유 한 줄(.why): 비어 있으면 숨긴다 */
function setWhy(el, text) { el.textContent = text || ""; el.classList.toggle("hidden", !text); }
/* 히트건 상태 한 줄: 송풍 · 열선 · 송풍을 끄기까지 남은 초 */
function heaterText(ht) {
  if (ht.fan == null && ht.element == null) return "—";
  if (ht.element) return "송풍 " + (ht.fan ? "켜짐" : "꺼짐") + " · 열선 " + (ht.pct == null ? "켜짐" : ht.pct + " %");
  if (ht.fan) return ht.fan_off_at ? "열선 꺼짐 · 송풍 " + Math.max(0, Math.ceil(ht.fan_off_at - Date.now() / 1000)) + "초 뒤 끔" : "송풍 켜짐 · 열선 꺼짐";
  return "꺼짐";
}
function renderManual() {
  const c = S.cfg || {};
  const busy = running() || touring(), locked = !!S.estop_locked;      // 측정 중 · 순회 중 · 비상정지 중: 이동 · 릴레이 켜기가 꺼진다(끄기는 된다)
  const busyText = running() ? "측정 중 사용 불가" : "순회 중 사용 불가";
  const tts = S.temptest || {}, ttOn = !!tts.active;              // 온도 시험 중: 히트건 · 쿨러 버튼 · 접촉 탐색은 시험이 쓴다
  const stage = devOk("stage"), tOk = devOk("k2110"), rOk = devOk("k2450");
  man.temp.textContent = S.temp.value == null ? "—" : S.temp.value.toFixed(1) + " °C";
  man.ohm.textContent = fmtOhm(S.resistance.ohm);
  man.cond.textContent = (c.meter_volt != null ? Number(c.meter_volt).toFixed(2) : "—") + " V · " + (c.meter_four_wire ? "4단자" : "2단자") + " · 평균 " + c.meter_avg_count + "회";
  man.mon.classList.toggle("on", !!S.monitor);

  /* 이동: 스테이지 연결 필요 */
  const hp = c.photo_head_xy;
  man.head.disabled = !hp || busy || locked || !stage;
  man.headNote.textContent = hp ? "" : "초기 설정 3 에서 설정";
  man.go.disabled = busy || locked || !stage;
  man.here.disabled = !stage;
  man.lower.set(busy ? busyText : !stage ? NEED.stage : locked ? "비상정지 해제 필요" : c.contact_z_mm == null ? "접촉 높이 필요 (초기 설정 6)" : !S.homed.z ? "Z 원점 필요 (초기 설정 2)" : "");
  setWhy(man.moveWhy, "");                                        // 이동 패드의 이유 한 줄(jogPart)이 스테이지 · 측정 · 비상정지를 알린다

  /* 수동 측정: 온도 1회 = 온도계 · 저항 1회 · 연속 측정 = 저항계 */
  man.readT.disabled = busy || !tOk;
  man.readR.disabled = busy || !rOk;
  man.mon.disabled = busy || (!S.monitor && (locked || !rOk));
  man.condBtn.disabled = busy;
  const miss = [!tOk ? NEED.k2110 : "", !rOk ? NEED.k2450 : ""].filter(Boolean);
  setWhy(man.measWhy, busy ? busyText : miss.join(" · "));
  const ct = (S.prep && S.prep.contact) || {};
  const search = c.contact_mode === "search";                    // [접촉 탐색] 은 접촉 방식이 전기 탐색일 때만 보인다
  man.contact.b.classList.toggle("hidden", !search);
  man.contactLine.classList.toggle("hidden", !search);
  man.contact.b.disabled = !!(ct.active || whyContact() || ttOn);
  man.contactLine.textContent = contactLine(ct) || (whyContact() && !busy ? whyContact() : "");

  /* 릴레이 시험: 스테이지 연결 필요. 히트건 줄 · 쿨러 줄(cooler_relays 만) */
  const ht = S.heater || {}, hOn = !!(ht.fan || ht.element);
  const relayBlock = busy ? busyText : ttOn ? "온도 시험 중 · 창의 [정지] 사용" : !stage ? NEED.stage : locked ? "비상정지 해제 필요" : "";
  man.hOn.disabled = !!relayBlock || !!ht.element;
  man.hEl.textContent = ht.element ? "열선 끄기" : "열선 켜기";
  man.hEl.disabled = busy || ttOn || !stage || !ht.fan || (!ht.element && locked);        // 송풍이 켜진 동안만(끄기는 비상정지 중에도)
  man.hOff.disabled = busy || ttOn || !stage || !hOn;
  man.hNow.disabled = !stage || !hOn;                                        // 즉시 끄기는 측정 · 온도 시험 중에도 된다(끄기)
  const v12 = !!ht.heat_fw;
  man.hPct.disabled = man.hPctGo.disabled = !!relayBlock || !ht.fan || !v12;
  setSyncLine(man.hSync, stage ? ht : null);
  man.hPctNow.textContent = !stage ? "" : !v12 ? "펌웨어 V18 필요" : bootText(ht) ? bootText(ht) + (ht.boot_pending != null ? " · " + Number(ht.boot_pending).toFixed(1) + " %" : "")
    : !ht.fan ? "송풍 켠 뒤 적용" : "현재 " + (ht.pct == null ? "—" : Number(ht.pct).toFixed(1)) + " %" + (ht.method ? " · " + ht.method : "");
  man.hState.textContent = heaterText(ht);
  man.hState.className = "small nowrap" + (hOn ? " st-warn" : "");
  const cool = c.cooler_relays || [];
  const sig = JSON.stringify(cool);
  if (sig !== man.coolSig) {
    man.coolSig = sig;
    man.cells = {};
    setKids(man.coolBox, ...cool.map((n) => { const b = btn("R" + n, () => api(S.relays[n - 1] ? "relay_off" : "relay_on", { n })); man.cells[n] = b; return b; }));
  }
  for (const [n, b] of Object.entries(man.cells || {})) {
    const on = !!S.relays[n - 1];
    b.classList.toggle("on", on);
    b.textContent = "R" + n + (on ? " 켜짐" : "");
    b.disabled = busy || ttOn || !stage || (locked && !on);
  }
  man.allOff.disabled = busy || !stage;
  setWhy(man.relWhy, relayBlock);
  man.tt.classList.toggle("on", ttOn);
  man.ttLine.textContent = ttOn ? "온도 시험 중 · " + (tts.phase || "") : tts.result ? "마지막 결과 · " + tts.result : "히트건 · 쿨러 온도만 측정";

  /* 카메라: 판 사진 보기 · 눌러서 이동 · 보정 점 추가 */
  const pr = S.prep || {}, hasPlate = !!pr.plate, ready = !!(pr.coords && pr.coords.ready);
  man.viewFix.disabled = !hasPlate;
  if (!hasPlate && man.view === "fix") { man.view = "raw"; man.viewRaw.checked = true; man.pb.show(""); refreshPreview(); }
  const fix = man.view === "fix";
  man.fixPart.classList.toggle("hidden", !fix);
  if (fix) {                                                     // 판 사진 보기: 마지막으로 찍은 판 사진(자동 측정 탭과 같은 사진)
    const ph = pr.photo, has = !!(ph && ph.rect);
    man.pb.setAr(1);
    man.pb.setNote("사진 없음");
    man.pb.show(has ? "/api/img/plate.jpg?v=" + ph.id + "-" + pr.plate.t : "");
    man.photoInfo.textContent = has ? "사진 " + fmtClock(ph.t) : "사진 없음";
    man.retake.set(whyStage(true, true) || whyCamera() || whyHead());
    man.bigView.disabled = !has;
  }
  man.clickGo.disabled = !hasPlate || !ready || busy || locked || !stage;     // 실시간 보기에서도 켤 수 있다(켜면 판 사진 보기로)
  if (man.clickGo.disabled) man.clickGo.checked = false;
  const hasPhoto = !!(pr.photo && pr.photo.rect);
  man.addHere.disabled = busy || !ready || !stage || !hasPhoto || !!S.move;   // 좌표 보정 완료 · 스테이지 연결 · 동작 중 아님 · 판 사진 있음
  man.camWhy = !hasPlate ? "초기 설정 3 · 5 후 사용 가능" : !ready ? "초기 설정 5 후 사용 가능" : !stage ? NEED.stage
    : busy ? busyText : locked ? "비상정지 해제 필요" : "";                  // 막는 까닭만([크게 보기] 옆 칸도 이것만 보인다)
  man.camNote.textContent = man.camWhy || (fix ? "십자 · 사각형 = 핀 · 원 = 선택 위치" : "");
  /* 샘플 줄(판 사진 보기일 때 · 순회 중에는 보기와 상관없이): 이전 · 선택 · 다음 · 이동 후 내리기 · 자동 순회 */
  const list = goSamples(), sSig = list.map((s) => s.id + ":" + s.name).join("|");
  if (sSig !== man.sSig) {
    man.sSig = sSig;
    setKids(man.sSel, h("option", { value: "", text: list.length ? "샘플 선택" : "샘플 없음" }), ...list.map((s) => h("option", { value: String(s.id), text: s.name })));
  }
  const ci = curSampleIdx();
  if (document.activeElement !== man.sSel) man.sSel.value = ci < 0 ? "" : String(list[ci].id);
  const goOff = !ready || !stage || busy || locked || !list.length;
  man.sSel.disabled = goOff;
  man.sPrev.disabled = goOff || ci === 0;
  man.sNext.disabled = goOff || (ci >= 0 && ci === list.length - 1);
  const lowWhy = c.contact_z_mm == null ? "내리기 · 접촉 높이 필요" : !S.homed.z ? "내리기 · Z 원점 필요" : "";
  man.lowerChk.disabled = !!lowWhy || busy || locked || !stage;
  if (man.lowerChk.disabled) man.lowerChk.checked = false;
  const tourWhy = renderTour(stage, ready, locked, ttOn);
  man.samplePart.classList.toggle("hidden", !(fix || touring()));
  man.sWhy.textContent = ready && stage && !busy && !list.length ? "샘플 검출 필요" : lowWhy || tourWhy;
  drawManualOverlay();
  man.jog.update();
}
function drawManualOverlay() {
  if (!S || !man.pb || !man.pb.w) return;
  if (man.view !== "fix") { setKids(man.ov); return; }
  const sz = S.cfg.plate_size_mm;
  man.pb.svg.setAttribute("viewBox", "0 0 " + sz + " " + sz);
  /* 샘플 윤곽 · 이름 · 핀 놓을 자리 · 누른 위치 = 동그라미 · 지금 핀 위치 = 십자 + 사각형(스테이지 X · Y 를 좌표 변환으로 판 위로. 변환이 없거나 판 밖이면 그리지 않는다) */
  setKids(man.ov, ...plateOverlayKids(sz / man.pb.w, 1));
}
/* "실시간" 보기의 미리보기: 1 초마다 새로 받는다(204 = 아직 프레임 없음). "판 사진" 보기는 찍어 둔 사진이라 새로 받지 않는다 */
let prevUrl = null;
async function refreshPreview() {
  if (tab !== "manual" || !S || man.view !== "raw") return;
  man.pb.setAr([90, 270].includes(Number(S.cfg.view_rot)) ? 3 / 4 : 4 / 3);     // 사진이 없어도 상자는 카메라 화면 크기 그대로(화면 방향 - 90 · 270° 면 세로)
  if (!devOk("camera")) { man.pb.setNote("카메라 연결 안 됨"); man.pb.show(""); return; }
  try {
    const r = await fetch("/api/camera/preview.jpg?t=" + Date.now(), { cache: "no-store" });
    if (man.view !== "raw") return;                              // 받는 사이에 보기를 바꿨다
    if (r.status !== 200) { man.pb.setNote("미리보기 대기 중"); man.pb.show(""); return; }
    const url = URL.createObjectURL(await r.blob());
    if (man.view !== "raw") { URL.revokeObjectURL(url); return; }
    man.pb.show(url);
    if (prevUrl) URL.revokeObjectURL(prevUrl);
    prevUrl = url;
  } catch (e) { /* 다음 주기에 다시 */ }
}
setInterval(refreshPreview, 1000);

TABS.manual = { build: buildManual, render: renderManual, show: refreshPreview };
