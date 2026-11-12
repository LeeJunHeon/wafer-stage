/* manual.js - 수동 탭: 왼쪽(카메라 · 이동) / 가운데(수동 측정 · 릴레이 시험) / 오른쪽(직접 명령).
   카메라 "실시간" = 실시간 미리보기. "판 사진" = 마지막으로 찍은 판 사진(측정 탭과 같은 사진) + 샘플 윤곽 · 이름 + 지금 핀 자리(십자).
   실시간 화면을 보정해 보여 주지 않는 까닭: 헤드가 판 위에 있으면 헤드가 판을 가린다. */
"use strict";

const man = {};
const QUICK = { k2450: ["*IDN?", "SYST:ERR?", ":READ?"], k2110: ["*IDN?", "SYST:ERR?", ":READ?"], stage: ["st", "rs", "p"] };

function buildManual() {
  /* 왼쪽: 카메라 · 이동 */
  man.view = "raw";
  man.ov = svgEl("g", {});
  man.pb = photoBox({ ar: 4 / 3, note: "카메라 연결 안 됨", center: true, onfit: () => drawManualOverlay() });      // 사진은 칸 가운데에, 칸이 허락하는 만큼 크게
  man.pb.box.id = "preview-box";
  man.pb.svg.append(man.ov);
  const setView = (v) => { man.view = v; man.pb.setNote("미리보기 준비 중"); man.pb.show(""); refreshPreview(); renderManual(); };
  man.photoInfo = h("span", { class: "small" });
  man.retake = busyGate("사진 다시 찍기", () => api("samples_find"), "sm");             // 측정 탭의 [사진 다시 찍기] 와 같다(사진 + 샘플 찾기)
  man.fixRow = h("div", { class: "row hidden" }, man.photoInfo, man.retake.b, man.retake.w);
  man.viewRaw = h("input", { type: "radio", name: "view", checked: true, onchange: () => setView("raw") });
  man.viewFix = h("input", { type: "radio", name: "view", onchange: () => setView("fix") });
  man.clickGo = h("input", { type: "checkbox" });
  man.addHere = busyGate("보정 점 추가", () => api("coord_add_here"), "sm");
  man.camNote = h("div", { class: "why line" });
  man.pb.inner.addEventListener("click", (ev) => {                 // 판 사진 보기에서 사진을 누르면 그 자리로 간다(핀을 맨 위로 올린 뒤)
    if (man.view !== "fix" || !man.clickGo.checked || running()) return;
    const [fx, fy] = man.pb.toFrac(ev), sz = S.cfg.plate_size_mm;
    if (fx < 0 || fx > 1 || fy < 0 || fy > 1) return;
    api("plate_goto", { x: fx * sz, y: fy * sz });
  });
  const camPanel = h("div", { class: "panel", id: "man-cam" },
    h("div", { class: "row" }, h("h3", { text: "카메라" }), h("span", { class: "sp" }),
      h("label", { class: "chk" }, man.viewRaw, "실시간"), h("label", { class: "chk" }, man.viewFix, "판 사진")),
    man.pb.box, man.fixRow,
    h("div", { class: "row wrap" }, h("label", { class: "chk" }, man.clickGo, "눌러서 이동"), man.addHere.b),
    man.camNote);

  man.jog = jogPart();
  const goInput = (ph) => h("input", { type: "number", class: "num", step: "any", placeholder: ph });
  man.gx = goInput("X"); man.gy = goInput("Y"); man.gz = goInput("Z");
  man.head = btn("촬영 위치로", () => { const p = S.cfg.photo_head_xy; if (p) api("goto", { x: p[0], y: p[1] }); });
  man.headNote = h("span", { class: "why" });
  man.go = btn("가기", () => {
    const x = parseFloat(man.gx.value), y = parseFloat(man.gy.value), z = man.gz.value === "" ? null : parseFloat(man.gz.value);
    if (isNaN(x) || isNaN(y)) { toast("X 와 Y 를 입력하세요"); return; }
    api("goto", z == null || isNaN(z) ? { x, y } : { x, y, z });
  });
  const movePanel = h("div", { class: "panel", id: "man-move" }, h("h3", { text: "이동" }), man.jog.el,
    h("div", { class: "row" }, man.head, man.headNote),
    h("div", { class: "row" }, small("바로 가기"), "X", man.gx, "Y", man.gy, "Z", man.gz, man.go));
  const left = h("div", { class: "col", id: "man-left" }, camPanel, movePanel);

  /* 가운데: 수동 측정 · 릴레이 시험 */
  man.temp = h("div", { class: "big", text: "—" });
  man.ohm = h("div", { class: "big", text: "—" });
  man.cond = h("div", { class: "small" });
  man.mon = btn("계속 읽기", () => api("monitor", { on: !S.monitor }));
  man.readT = btn("한 번 읽기", () => api("read_temp"));
  man.readR = btn("한 번 읽기", () => api("read_resistance"));
  man.measWhy = h("div", { class: "why hidden" });
  man.contact = busyGate("접촉 탐색", () => api("contact_find"));
  man.contactLine = h("div", { class: "small" });
  const measure = h("div", { class: "panel" }, h("h3", { text: "수동 측정" }),
    h("div", { class: "reading" }, h("span", { class: "lbl", text: "온도 · K2110" }), man.temp, man.readT),
    h("div", { class: "reading" }, h("span", { class: "lbl", text: "저항 · K2450" }), man.ohm, man.readR),
    h("div", { class: "row" }, man.cond, h("span", { class: "sp" }), btn("조건", openCond)),
    h("div", { class: "row" }, man.mon, man.contact.b), man.contactLine, man.measWhy);
  man.hOn = btn("켜기", () => api("relay_on", { n: S.cfg.heater_relay }));
  man.hOff = btn("끄기", () => api("relay_off", { n: S.cfg.heater_relay }));
  man.hLbl = h("span", { class: "lbl small" });
  man.grid = h("div", { class: "relays" });
  man.gridSig = "";
  const relay = h("div", { class: "panel" }, h("h3", { text: "릴레이 시험" }),
    h("div", { class: "row" }, man.hLbl, man.hOn, man.hOff), h("div", { class: "small", text: "자주 켜고 끄지 마세요" }),
    man.grid,
    h("div", { class: "row" }, btn("모두 끄기", () => api("relays_off"))));
  const mid = h("div", { class: "col", id: "man-mid" }, measure, relay);

  /* 오른쪽: 직접 명령 */
  man.target = h("select", { onchange: () => { man.quick(); } }, h("option", { value: "k2450", text: "저항계 K2450" }), h("option", { value: "k2110", text: "온도계 K2110" }), h("option", { value: "stage", text: "스테이지" }));
  man.quickBox = h("div", { class: "quick" });
  man.line = h("input", { type: "text", class: "grow mono", spellcheck: "false", placeholder: "한 줄 입력", onkeydown: (e) => { if (e.key === "Enter") sendRaw(); } });
  man.log = h("div", { id: "cmd-log", class: "fill" });
  man.quick = () => setKids(man.quickBox, ...QUICK[man.target.value].map((q) => btn(q, () => { man.line.value = q; sendRaw(); })));
  man.quick();
  const cmd = h("div", { class: "panel", id: "man-right" }, h("h3", { text: "직접 명령" }), h("div", { class: "row" }, man.target), man.quickBox,
    h("div", { class: "row" }, man.line, btn("보내기", sendRaw)), man.log, h("div", { class: "small", text: "이동 · 릴레이는 버튼으로" }));
  setKids($("#tab-manual"), left, mid, cmd);
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
function renderManual() {
  const c = S.cfg || {};
  man.temp.textContent = S.temp.value == null ? "—" : S.temp.value.toFixed(1) + " °C";
  man.ohm.textContent = fmtOhm(S.resistance.ohm);
  man.cond.textContent = (c.meter_volt != null ? Number(c.meter_volt).toFixed(2) : "—") + " V · " + (c.meter_four_wire ? "4단자" : "2단자") + " · 평균 " + c.meter_avg_count + "회";
  man.mon.classList.toggle("on", !!S.monitor);
  const hp = c.photo_head_xy;
  man.head.disabled = !hp || running();
  man.headNote.textContent = hp ? "" : "준비 3 에서 정합니다";
  const hr = c.heater_relay || 1;
  man.hLbl.textContent = "히트건 R" + hr;
  const grid = c.cooler_grid || [];
  const sig = JSON.stringify(grid);
  if (sig !== man.gridSig) {
    man.gridSig = sig;
    man.cells = {};
    setKids(man.grid, ...grid.flat().map((n) => { const b = btn("R" + n, () => api(S.relays[n - 1] ? "relay_off" : "relay_on", { n })); man.cells[n] = b; return b; }));
  }
  const busy = running();
  for (const [n, b] of Object.entries(man.cells || {})) {
    const on = !!S.relays[n - 1];
    b.classList.toggle("on", on);
    b.textContent = "R" + n + (on ? " 켜짐" : "");
    b.disabled = busy;
  }
  for (const b of [man.hOn, man.hOff, man.readT, man.readR, man.mon, man.go]) b.disabled = busy;
  man.measWhy.textContent = busy ? "측정 중에는 쓸 수 없습니다" : "";
  man.measWhy.classList.toggle("hidden", !busy);
  const ct = (S.prep && S.prep.contact) || {};
  man.contact.b.disabled = !!(ct.active || whyContact());
  man.contact.b.title = whyContact();
  man.contactLine.textContent = contactLine(ct) || (whyContact() && !busy ? whyContact() : "");
  /* 카메라: 판 사진 보기 · 눌러서 이동 · 보정 점 추가 */
  const pr = S.prep || {}, hasPlate = !!pr.plate, ready = !!(pr.coords && pr.coords.ready);
  man.viewFix.disabled = !hasPlate;
  if (!hasPlate && man.view === "fix") { man.view = "raw"; man.viewRaw.checked = true; man.pb.show(""); refreshPreview(); }
  const fix = man.view === "fix";
  man.fixRow.classList.toggle("hidden", !fix);
  if (fix) {                                                     // 판 사진 보기: 마지막으로 찍은 판 사진(측정 탭과 같은 사진)
    const ph = pr.photo, has = !!(ph && ph.rect);
    man.pb.setAr(1);
    man.pb.setNote("사진이 없습니다");
    man.pb.show(has ? "/api/img/plate.jpg?v=" + ph.id + "-" + pr.plate.t : "");
    man.photoInfo.textContent = has ? "사진 " + fmtClock(ph.t) : "사진 없음";
    man.retake.set(whyStage(true, true) || whyCamera() || whyHead());
  }
  man.clickGo.disabled = man.view !== "fix" || !ready || busy;
  if (man.clickGo.disabled) man.clickGo.checked = false;
  man.addHere.set(busy ? "측정 중" : !ready ? "" : pr.last_click ? "" : "판 사진을 먼저 누르세요");
  man.addHere.b.disabled = busy || !ready || !pr.last_click;
  man.camNote.textContent = !hasPlate ? "준비 3 · 5 뒤에 쓸 수 있습니다" : !ready ? "준비 5 뒤에 쓸 수 있습니다" : fix ? "십자 = 핀 · 동그라미 = 누른 자리" : "";
  man.camNote.title = man.camNote.textContent;
  drawManualOverlay();
  man.jog.update();
}
function drawManualOverlay() {
  if (!S || !man.pb || !man.pb.w) return;
  if (man.view !== "fix") { setKids(man.ov); return; }
  const sz = S.cfg.plate_size_mm, k = sz / man.pb.w, kids = [], pr = S.prep || {};
  man.pb.svg.setAttribute("viewBox", "0 0 " + sz + " " + sz);
  const halo = (tag, a, wd) => { kids.push(svgEl(tag, Object.assign({}, a, { stroke: "#FFFFFF", "stroke-width": (wd + 2.5) * k, fill: "none" })), svgEl(tag, Object.assign({}, a, { stroke: "#1F2329", "stroke-width": wd * k, fill: "none" }))); };
  for (const s of S.samples || []) {                              // 샘플 윤곽 · 이름
    halo("polygon", { points: s.outline.map((p) => p.join(",")).join(" "), "stroke-linejoin": "round" }, 1.5);
    kids.push(svgEl("text", { x: s.plate[0], y: Math.min(...s.outline.map((p) => p[1])) - 4 * k, "font-size": 12 * k, "text-anchor": "middle", fill: "#1F2329", stroke: "#FFFFFF", "stroke-width": 3 * k, "paint-order": "stroke", text: s.name }));
  }
  const lc = pr.last_click;
  if (lc) halo("circle", { cx: lc.plate[0], cy: lc.plate[1], r: 6 * k }, 1.5);                 // 누른 자리 = 동그라미
  /* 지금 핀 자리 = 십자: 스테이지 X · Y 를 좌표 변환으로 판 위로 옮긴다. 변환이 없거나 핀이 판 밖이면 그리지 않는다 */
  const pin = S.pos && S.pos.x != null && S.pos.y != null ? applyH(pr.coords && pr.coords.to_plate, S.pos.x, S.pos.y) : null;
  if (pin && pin[0] >= 0 && pin[0] <= sz && pin[1] >= 0 && pin[1] <= sz) {
    const [x, y] = pin, r = 9 * k;
    halo("path", { d: "M" + (x - r) + " " + y + "H" + (x + r) + "M" + x + " " + (y - r) + "V" + (y + r) }, 1.5);
  }
  setKids(man.ov, ...kids);
}
/* "실시간" 보기의 미리보기: 1 초마다 새로 받는다(204 = 아직 프레임 없음). "판 사진" 보기는 찍어 둔 사진이라 새로 받지 않는다 */
let prevUrl = null;
async function refreshPreview() {
  if (tab !== "manual" || !S || man.view !== "raw") return;
  if (!devOk("camera")) { man.pb.setNote("카메라 연결 안 됨"); man.pb.show(""); return; }
  man.pb.setAr(4 / 3);
  try {
    const r = await fetch("/api/camera/preview.jpg?t=" + Date.now(), { cache: "no-store" });
    if (man.view !== "raw") return;                              // 받는 사이에 보기를 바꿨다
    if (r.status !== 200) { man.pb.setNote("미리보기 준비 중"); man.pb.show(""); return; }
    const url = URL.createObjectURL(await r.blob());
    if (man.view !== "raw") { URL.revokeObjectURL(url); return; }
    man.pb.show(url);
    if (prevUrl) URL.revokeObjectURL(prevUrl);
    prevUrl = url;
  } catch (e) { /* 다음 주기에 다시 */ }
}
setInterval(refreshPreview, 1000);

TABS.manual = { build: buildManual, render: renderManual, show: refreshPreview };
