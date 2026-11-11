/* prep.js - 준비 탭: 왼쪽 단계 목록(260px) + 오른쪽 그 단계의 화면. 순서를 강제하지 않는다.
   앞 단계 값이 없어서 못 하는 일은 그 버튼만 회색으로 두고 이유를 한 줄 쓴다(gate). 확인 창은 띄우지 않는다. */
"use strict";

const STEPS = ["장비 연결", "원점 맞추기", "카메라 · 판 영역", "빈 판 사진", "좌표 맞추기", "접촉 높이 찾기", "쿨러 구역 확인"];
const DEVS = [
  { key: "stage", name: "스테이지", cfg: "stage_port", list: "dl-ports", ph: "COM 번호", conn: (v) => api("connect_stage", { port: v }), off: () => api("disconnect_stage") },
  { key: "k2110", name: "온도계 K2110", cfg: "k2110_addr", list: "dl-visa", ph: "VISA 주소", conn: (v) => api("connect_meter", { kind: "k2110", addr: v }), off: () => api("disconnect_meter", { kind: "k2110" }) },
  { key: "k2450", name: "저항계 K2450", cfg: "k2450_addr", list: "dl-visa", ph: "VISA 주소", conn: (v) => api("connect_meter", { kind: "k2450", addr: v }), off: () => api("disconnect_meter", { kind: "k2450" }) },
  { key: "camera", name: "카메라", cfg: "camera_index", list: "dl-cams", ph: "장치 번호", conn: (v) => api("connect_camera", { index: parseInt(v, 10) }), off: () => api("disconnect_camera") },
];
const CORNER = ["A", "B", "C", "D"];
const prep = { step: 0, pages: [], items: [] };

/* 버튼 + 못 누르는 이유 한 줄 */
function gate(text, fn, cls) {
  const b = btn(text, fn, cls), w = h("span", { class: "why" });
  return { b, w, set(reason) { b.disabled = !!reason; w.textContent = reason || ""; } };
}
/* 누르면 끝날 때까지 잠깐 못 누르게(두 번 눌림 방지) */
function busyBtn(text, fn, cls) {
  const b = btn(text, async () => { if (b.dataset.busy) return; b.dataset.busy = "1"; b.classList.add("dis"); try { await fn(); } finally { b.dataset.busy = ""; b.classList.remove("dis"); } }, cls);
  return b;
}
function busyGate(text, fn, cls) {
  const g = gate(text, null, cls);
  g.b.addEventListener("click", async () => { if (g.b.dataset.busy) return; g.b.dataset.busy = "1"; g.b.classList.add("dis"); try { await fn(); } finally { g.b.dataset.busy = ""; g.b.classList.remove("dis"); } });
  return g;
}
const P = () => (S && S.prep) || {};
const homedAll = () => S && S.homed.x && S.homed.y && S.homed.z;
function whyStage(needXY, needZ) {
  if (running()) return "측정 중에는 할 수 없습니다";
  if (!devOk("stage")) return "스테이지를 먼저 연결하세요 (준비 1)";
  if (S.estop_locked) return "비상정지를 먼저 풀어 주세요";
  if (needXY && !(S.homed.x && S.homed.y)) return "X · Y 원점이 없습니다 (준비 2)";
  if (needZ && !S.homed.z) return "Z 원점이 없습니다 (준비 2)";
  return "";
}
const whyCamera = () => (devOk("camera") ? "" : "카메라를 먼저 연결하세요 (준비 1)");
const whyHead = () => (S.cfg.photo_head_xy ? "" : "사진 찍을 때 헤드 자리를 먼저 정하세요 (준비 3)");

function buildPrep() {
  prep.items = STEPS.map((t, i) => {
    const sub = h("div", { class: "sub" }), done = h("span", { class: "done" });
    const el = h("div", { class: "step", onclick: () => setStep(i) }, h("span", { class: "no", text: i + 1 }),
      h("div", { class: "txt" }, h("div", { class: "t", text: t }), sub), done);
    return { el, sub, done };
  });
  prep.allItem = h("div", { class: "step link", onclick: () => setStep(7) }, h("div", { class: "txt" }, h("div", { class: "t", text: "설정 값 모두 보기" })));
  prep.fakeFill = busyBtn("가짜 준비 값 채우기", () => api("fake_fill"));
  prep.fakeLoad = busyBtn("가짜 샘플 올리기", () => api("fake_samples", { on: !P().fake_loaded }));
  prep.fakeBox = h("div", { class: "fakebox hidden" }, small("가짜 장비"), prep.fakeFill, prep.fakeLoad);
  const list = h("div", { class: "panel", id: "prep-list" }, h("h3", { text: "처음 한 번 준비" }), ...prep.items.map((x) => x.el), prep.allItem, prep.fakeBox);
  prep.main = h("div", { id: "prep-main" });
  prep.pages = [buildStep1(), buildStep2(), buildStep3(), buildStep4(), buildStep5(), buildStep6(), buildStep7(),
    typeof buildSettingsPage === "function" ? buildSettingsPage() : null];
  $("#tab-prep").replaceChildren(list, prep.main);
  setStep(0);
}
function setStep(i) {
  if (!prep.pages[i]) { toast("다음 단계에서 만듭니다"); return; }
  prep.step = i;
  prep.items.forEach((x, k) => x.el.classList.toggle("on", k === i));
  prep.allItem.classList.toggle("on", i === 7);
  prep.main.replaceChildren(prep.pages[i].el);
  if (prep.pages[i].show) prep.pages[i].show();
  if (S) renderPrep();
}
function gotoPrep(i) { setTab("prep"); setStep(i); }

/* ---------- 1 · 장비 연결 ---------- */
function buildStep1() {
  const rows = DEVS.map((d) => {
    const input = h("input", { type: "text", class: "addr", list: d.list, placeholder: d.ph, spellcheck: "false", oninput: () => { input.dataset.dirty = "1"; } });
    const b = btn("연결", async () => {
      b.disabled = true;
      const r = await (S.devices[d.key].state === "연결" ? d.off() : d.conn(input.value.trim()));
      if (r.ok) input.dataset.dirty = "";
      b.disabled = false;
    });
    const stt = h("span", { class: "stt" }), resp = h("div", { class: "resp" });
    prep[d.key] = { input, btn: b, stt, resp };
    return h("div", { class: "devrow" }, h("div", { class: "name", text: d.name }), input, b, h("div", {}, stt, resp));
  });
  const auto = h("input", { type: "checkbox", onchange: () => putSettings({ auto_connect: auto.checked }) });
  prep.auto = auto;
  const el = h("div", { class: "panel prep-panel" }, h("h3", { text: "1 · 장비 연결" }),
    h("div", { class: "small", text: "포트를 열면 보드가 다시 시작합니다. COM4 는 예전 장비입니다. 사람이 고른 것 하나만 엽니다." }),
    h("datalist", { id: "dl-ports" }), h("datalist", { id: "dl-visa" }), h("datalist", { id: "dl-cams" }),
    h("div", {}, ...rows),
    h("div", { class: "row" }, btn("목록 새로 읽기", readLists), h("label", { class: "chk" }, auto, "시작할 때 자동 연결")));
  return { el, render: renderStep1 };
}
async function readLists() {
  const r = await api("devices");
  if (!r.ok) return;
  const d = r.result;
  $("#dl-ports").replaceChildren(...d.serial_ports.map((p) => h("option", { value: p })));
  $("#dl-visa").replaceChildren(...d.visa.map((a) => h("option", { value: a })));
  $("#dl-cams").replaceChildren(...d.cameras.map((n) => h("option", { value: String(n) })));
  if (d.note) toast(d.note);
}
function renderStep1() {
  const c = S.cfg || {};
  for (const d of DEVS) {
    const dev = S.devices[d.key], p = prep[d.key];
    const cls = { ok: "st-ok", warn: "st-warn", err: "st-err", off: "" }[dotOf(dev)];
    p.stt.replaceChildren(h("i", { class: "dot " + dotOf(dev) }), h("span", { class: cls, text: dev.state + (dev.state === "오류" && dev.why ? " · " + dev.why : "") }));
    p.resp.textContent = dev.state === "연결" ? dev.info : "";
    p.resp.title = dev.info || "";
    p.btn.textContent = dev.state === "연결" ? "끊기" : "연결";
    p.btn.disabled = dev.state === "연결 중";
    if (!p.input.dataset.dirty && document.activeElement !== p.input && c[d.cfg] != null) p.input.value = String(c[d.cfg]);
  }
  prep.auto.checked = !!c.auto_connect;
}

/* ---------- 2 · 원점 맞추기 ---------- */
function buildStep2() {
  const pg = { jog: jogPart(), ax: {} };
  const HOW = { here: "눈으로 맞춰 등록", end: "끝단까지 밀어서", "가짜": "가짜 값" };
  const rows = ["x", "y", "z"].map((a) => {
    const dot = h("i", { class: "dot" }), txt = h("span", {}), when = h("span", { class: "small" });
    pg.ax[a] = { dot, txt, when };
    return h("div", { class: "row" }, h("b", { class: "axname", text: a.toUpperCase() + " 축" }), dot, txt, when);
  });
  const origin = (axis, mode) => () => api("set_origin", { axis, mode });
  pg.here = [gate("X 등록", origin("x", "here")), gate("Y 등록", origin("y", "here")), gate("Z 등록", origin("z", "here")), gate("세 축 모두 등록", origin("all", "here"))];
  pg.end = [gate("X", origin("x", "end")), gate("Y", origin("y", "end")), gate("Z", origin("z", "end"))];
  pg.whyHere = h("div", { class: "why" }); pg.whyEnd = h("div", { class: "why" });
  const el = h("div", { class: "panel prep-panel" }, h("h3", { text: "2 · 원점 맞추기" }),
    h("div", { class: "grp" }, ...rows),
    h("div", { class: "two" },
      h("div", { class: "grp" }, h("h4", { text: "(가) 눈으로 맞추고 등록" }),
        h("div", { class: "small", text: "0 쪽 끝에 붙인 뒤 누르세요. 끝에서 떨어진 자리를 0 으로 잡으면 반대쪽 끝에 부딪힐 수 있습니다." }),
        pg.jog.el, h("div", { class: "row wrap" }, ...pg.here.map((g) => g.b)), pg.whyHere),
      h("div", { class: "grp" }, h("h4", { text: "(나) 끝단까지 밀어서 잡기" }),
        h("div", { class: "small", text: "끝에 부딪힐 때 드르륵 소리가 납니다." }),
        h("div", { class: "row" }, ...pg.end.map((g) => g.b)), pg.whyEnd)));
  pg.el = el;
  pg.render = () => {
    const org = P().origin || {};
    for (const a of ["x", "y", "z"]) {
      const r = pg.ax[a], ok = !!S.homed[a], o = org[a];
      r.dot.className = "dot " + (ok ? "ok" : "off");
      r.txt.textContent = devOk("stage") ? (ok ? "원점 있음" : "원점 없음") : "—";
      r.when.textContent = ok && o ? (HOW[o.how] || o.how) + " · " + fmtWhen(o.t) : "";
    }
    const why = whyStage(false, false);
    pg.here.forEach((g) => { g.b.disabled = !!why; });
    pg.whyHere.textContent = why;
    const whyXY = why || (!S.homed.z ? "X · Y 는 Z 원점을 먼저 잡아야 합니다" : "");
    pg.end[0].b.disabled = pg.end[1].b.disabled = !!whyXY;
    pg.end[2].b.disabled = !!why;
    pg.whyEnd.textContent = why || (whyXY ? whyXY : "");
    pg.jog.update();
  };
  return pg;
}

/* ---------- 3 · 카메라 · 판 영역 ---------- */
function buildStep3() {
  const pg = { jog: jogPart(), pts: [], drag: -1, photoId: null, dirty: false, magT: 0 };
  pg.ov = svgEl("g", {});
  pg.pb = photoBox({ ar: 4 / 3, note: "아직 찍은 사진이 없습니다", onfit: () => drawPts() });
  pg.pb.svg.append(pg.ov);
  pg.headSet = busyGate("지금 자리로 정하기", () => api("photo_head_set"));
  pg.headTxt = h("div", {});
  pg.take = busyGate("사진 찍기", async () => { const r = await api("photo_take"); if (r.ok) { pg.dirty = false; pg.photoId = null; } });
  pg.list = h("div", { class: "small" });
  pg.redo = btn("다시 누르기", () => { pg.pts = []; pg.dirty = true; drawPts(); renderPts(); });
  pg.apply = busyGate("이대로 정하기", async () => {
    const r = await api("plate_set", { corners: pg.pts });
    if (r.ok) { pg.dirty = false; toast(r.result.note || "판 영역을 정했습니다"); }
  }, "on");
  pg.mag = h("div", { class: "mag hidden" }, h("i", { class: "cross-h" }), h("i", { class: "cross-v" }));
  pg.pb.box.append(pg.mag);                                     // 확대창은 사진 위 구석에 띄운다(누르는 자리의 반대쪽)
  pg.el = h("div", { class: "panel prep-panel photo" }, h("h3", { text: "3 · 카메라 · 판 영역" }),
    h("div", { class: "split fill" }, pg.pb.box,
      h("div", { class: "ctl" },
        h("div", { class: "grp" }, h("h4", { text: "사진 찍을 때 헤드 자리" }),
          h("div", { class: "small", text: "헤드가 판을 가리지 않고 헤드 마커가 사진에 보이는 자리로 옮긴 뒤 누르세요." }),
          pg.jog.el, h("div", { class: "row" }, pg.headSet.b, pg.headSet.w), pg.headTxt),
        h("div", { class: "grp" }, h("h4", { text: "판 영역" }),
          h("div", { class: "row" }, pg.take.b, pg.take.w),
          h("div", { class: "small", text: "사진에서 판에 새겨진 사각형의 꼭짓점을 왼쪽 위부터 시계 방향으로 A · B · C · D 순서로 누르세요. 찍은 점은 끌어서 옮길 수 있습니다." }),
          pg.list, h("div", { class: "row" }, pg.redo, pg.apply.b, pg.apply.w)))));

  const photo = () => P().photo;
  function toPx(ev) { const ph = photo(); const [fx, fy] = pg.pb.toFrac(ev); return [Math.max(0, Math.min(ph.w - 1, fx * ph.w)), Math.max(0, Math.min(ph.h - 1, fy * ph.h))]; }
  function drawPts() {
    const ph = photo();
    if (!ph || !pg.pb.w) { pg.ov.replaceChildren(); return; }
    pg.pb.svg.setAttribute("viewBox", "0 0 " + ph.w + " " + ph.h);
    const k = ph.w / pg.pb.w;                                   // 화면 1px = 사진 k 픽셀
    const kids = [];
    if (pg.pts.length >= 2) kids.push(svgEl(pg.pts.length === 4 ? "polygon" : "polyline", { points: pg.pts.map((p) => p.join(",")).join(" "), fill: "none", stroke: "#1F2A37", "stroke-width": 1.5 * k }));
    pg.pts.forEach((p, i) => {
      kids.push(svgEl("circle", { cx: p[0], cy: p[1], r: 6 * k, fill: "#fff", stroke: "#1F2A37", "stroke-width": 2 * k }));
      kids.push(svgEl("text", { x: p[0] + 10 * k, y: p[1] - 8 * k, "font-size": 14 * k, "font-weight": 700, fill: "#1F2A37", stroke: "#fff", "stroke-width": 3 * k, "paint-order": "stroke", text: CORNER[i] }));
    });
    pg.ov.replaceChildren(...kids);
  }
  function renderPts() {
    pg.list.textContent = pg.pts.length ? pg.pts.map((p, i) => CORNER[i] + " (" + Math.round(p[0]) + ", " + Math.round(p[1]) + ")").join(" · ") : "아직 누른 점이 없습니다";
  }
  function showMag(p) {
    const now = performance.now();
    if (now - pg.magT < 120) return;                            // 끄는 동안 주변을 크게 보여 준다(1 초에 8번까지)
    pg.magT = now;
    const ph = photo();
    pg.mag.classList.remove("hidden");
    pg.mag.classList.toggle("left", p[0] > ph.w / 2);
    pg.mag.style.backgroundImage = "url(/api/img/crop.jpg?x=" + Math.round(p[0]) + "&y=" + Math.round(p[1]) + "&half=50&t=" + Math.round(now) + ")";
  }
  pg.pb.inner.addEventListener("pointerdown", (ev) => {
    const ph = photo();
    if (!ph || ev.button !== 0) return;
    ev.preventDefault();
    const p = toPx(ev), k = ph.w / Math.max(1, pg.pb.w);
    let hit = -1, best = 14 * k;
    pg.pts.forEach((q, i) => { const d = Math.hypot(q[0] - p[0], q[1] - p[1]); if (d < best) { best = d; hit = i; } });
    if (hit < 0) {
      if (pg.pts.length >= 4) return;
      pg.pts.push(p); hit = pg.pts.length - 1;
    }
    pg.drag = hit; pg.dirty = true;
    pg.pb.inner.setPointerCapture(ev.pointerId);
    pg.magT = 0; showMag(pg.pts[hit]); drawPts(); renderPts();
  });
  pg.pb.inner.addEventListener("pointermove", (ev) => {
    if (pg.drag < 0) return;
    pg.pts[pg.drag] = toPx(ev);
    showMag(pg.pts[pg.drag]); drawPts(); renderPts();
  });
  const endDrag = () => { if (pg.drag >= 0) { pg.drag = -1; pg.magT = 0; pg.mag.classList.add("hidden"); renderPts(); } };
  pg.pb.inner.addEventListener("pointerup", endDrag);
  pg.pb.inner.addEventListener("pointercancel", endDrag);

  pg.render = () => {
    const ph = photo(), pl = P().plate, c = S.cfg;
    if (ph) { pg.pb.setAr(ph.w / ph.h); pg.pb.show("/api/img/raw.jpg?v=" + ph.id); } else pg.pb.show("");
    if (ph && pg.photoId !== ph.id && !pg.dirty && pg.drag < 0) {    // 새 사진: 저장된 꼭짓점이 이 사진 크기와 맞으면 그것부터 보여 준다
      pg.photoId = ph.id;
      pg.pts = pl && pl.size[0] === ph.w && pl.size[1] === ph.h ? pl.corners.map((p) => [p[0], p[1]]) : [];
      drawPts(); renderPts();
    }
    pg.headSet.set(whyStage(true, false) || whyCamera());
    const hp = c.photo_head_xy, seen = P().marker_seen;
    pg.headTxt.replaceChildren(hp ? "정한 자리 X " + f1(hp[0]) + " · Y " + f1(hp[1]) : "아직 정하지 않았습니다",
      hp && seen != null ? h("span", { class: seen ? "st-ok" : "st-warn", text: seen ? " · 마커 보임" : " · 마커 안 보임" }) : "");
    pg.take.set(running() ? "측정 중에는 할 수 없습니다" : whyCamera());
    pg.apply.set(!ph ? "사진을 먼저 찍으세요" : pg.pts.length < 4 ? "꼭짓점 " + pg.pts.length + " / 4" : running() ? "측정 중에는 할 수 없습니다" : "");
    if (pg.pts.length === 4 && pl && !pg.dirty) pg.apply.w.textContent = "정함 · " + fmtWhen(pl.t);
    pg.redo.disabled = !pg.pts.length;
    pg.jog.update();
  };
  renderPts();
  return pg;
}

/* ---------- 4 · 빈 판 사진 ---------- */
function buildStep4() {
  const pg = {};
  pg.pb = photoBox({ ar: 1, note: "아직 빈 판 사진이 없습니다" });
  pg.take = busyGate("빈 판 사진 찍기", () => api("empty_take"), "on");
  pg.txt = h("div", {});
  pg.el = h("div", { class: "panel prep-panel photo" }, h("h3", { text: "4 · 빈 판 사진" }),
    h("div", { class: "split fill" }, pg.pb.box,
      h("div", { class: "ctl" },
        h("div", { class: "grp" },
          h("div", { class: "small", text: "판 위의 샘플을 모두 치운 뒤 누르세요. 핀을 맨 위로 올리고 사진 찍을 자리로 가서 한 장 찍습니다. 언제든 다시 찍어 바꿀 수 있습니다." }),
          h("div", { class: "row" }, pg.take.b, pg.take.w), pg.txt,
          h("div", { class: "small", text: "빈 판 사진이 있으면 샘플을 더 잘 찾습니다. 없어도 측정할 수 있습니다." })))));
  pg.render = () => {
    const e = P().empty, pl = P().plate;
    pg.take.set(whyStage(true, true) || whyCamera() || whyHead());
    pg.txt.textContent = e ? "찍은 때 " + fmtWhen(e.t) : "";
    pg.pb.setNote(e ? (pl ? "사진을 불러오는 중" : "판 영역(준비 3)을 정하면 위에서 본 모습으로 보입니다") : "아직 빈 판 사진이 없습니다");
    pg.pb.show(e && pl ? "/api/img/empty.jpg?v=" + e.t + "-" + pl.t : "");
  };
  return pg;
}

/* ---------- 5 · 좌표 맞추기 ---------- */
function buildStep5() {
  const pg = { jog: jogPart() };
  pg.ov = svgEl("g", {});
  pg.pb = photoBox({ ar: 1, note: "판 영역(준비 3)을 정하면 판 사진이 보입니다", onfit: () => pg.draw() });
  pg.pb.svg.append(pg.ov);
  pg.set = CORNER.map((n) => busyGate("여기가 " + n, () => api("coord_set", { name: n })));
  pg.whySet = h("div", { class: "why" });
  pg.gotoD = busyGate("D 근처로 가기", () => api("coord_goto_d"));
  pg.pts = h("div", { class: "small" });
  pg.q = h("div", { class: "small" });
  pg.marker = h("div", { class: "small" });
  pg.learn = busyGate("헤드 마커 다시 기억", () => api("marker_learn"), "sm");
  pg.extra = h("div", { class: "exlist" });
  pg.clear = btn("처음부터 다시", () => api("coord_clear"), "sm");
  pg.el = h("div", { class: "panel prep-panel photo" }, h("h3", { text: "5 · 좌표 맞추기" }),
    h("div", { class: "split fill" }, pg.pb.box,
      h("div", { class: "ctl" },
        h("div", { class: "grp" },
          h("div", { class: "small", text: "판 꼭짓점 A · B · C · D 에 핀을 차례로 가져다 대고 누르세요. 조그로 옮기고 핀을 눈으로 꼭짓점 위에 내립니다." }),
          pg.jog.el, h("div", { class: "row wrap" }, ...pg.set.map((g) => g.b)), pg.whySet,
          h("div", { class: "row" }, pg.gotoD.b, pg.gotoD.w), pg.pts),
        h("div", { class: "grp" }, h("h4", { text: "맞춘 정도" }), pg.q),
        h("div", { class: "grp" }, h("h4", { text: "헤드 마커" }), pg.marker, h("div", { class: "row" }, pg.learn.b, pg.learn.w)),
        h("div", { class: "grp" }, h("h4", { text: "보정 점" }),
          h("div", { class: "small", text: "수동 탭 카메라를 '보정' 보기로 두고 판 사진을 눌러 그 자리로 간 뒤, 어긋나 있으면 조그로 맞추고 [지금 자리를 보정에 더하기] 를 누르세요." }),
          pg.extra, h("div", { class: "row" }, pg.clear)))));
  pg.draw = () => {
    const c = S && S.cfg, co = P().coords;
    if (!S || !pg.pb.w) return;
    const sz = c.plate_size_mm, k = sz / pg.pb.w;
    pg.pb.svg.setAttribute("viewBox", "0 0 " + sz + " " + sz);
    const at = [[0, 0], [sz, 0], [sz, sz], [0, sz]], kids = [];
    CORNER.forEach((n, i) => {
      const has = co && co.stage[n];
      const x = at[i][0] + (i === 1 || i === 2 ? -1 : 1) * 14 * k, y = at[i][1] + (i >= 2 ? -1 : 1) * 14 * k;
      kids.push(svgEl("circle", { cx: x, cy: y, r: 11 * k, fill: has ? "#1F2A37" : "#fff", stroke: "#1F2A37", "stroke-width": 1.5 * k }));
      kids.push(svgEl("text", { x, y: y + 4.5 * k, "font-size": 12 * k, "font-weight": 700, "text-anchor": "middle", fill: has ? "#fff" : "#1F2A37", text: n }));
    });
    pg.ov.replaceChildren(...kids);
  };
  pg.render = () => {
    const co = P().coords || { stage: {}, extra: [] }, pl = P().plate, ph = P().photo, e = P().empty;
    const src = pl && ph && ph.rect ? "/api/img/plate.jpg?v=" + ph.id + "-" + pl.t : pl && e ? "/api/img/empty.jpg?v=" + e.t + "-" + pl.t : "";
    pg.pb.setNote(pl ? "사진을 찍으면 판 사진이 보입니다 (준비 3 · 4)" : "판 영역(준비 3)을 정하면 판 사진이 보입니다");
    pg.pb.show(src);
    const why = whyStage(true, false);
    pg.set.forEach((g) => { g.b.disabled = !!why; });
    pg.whySet.textContent = why || (pl ? "" : "판 영역(준비 3)을 정해야 사진과 이어집니다 · 꼭짓점 자리는 지금 찍어 둘 수 있습니다");
    pg.gotoD.set(whyStage(true, true) || (co.d_guess ? "" : co.stage.D ? "D 를 이미 찍었습니다" : "A · B · C 를 먼저 찍으세요"));
    pg.pts.textContent = CORNER.map((n) => n + " " + (co.stage[n] ? "X " + f1(co.stage[n][0]) + " · Y " + f1(co.stage[n][1]) : "—")).join("   ");
    const q = co.quality;
    if (q) {
      pg.q.replaceChildren(
        h("div", { text: "네 변 " + q.sides.map((v) => v.toFixed(1)).join(" · ") + " mm (판 한 변 " + S.cfg.plate_size_mm + " mm 와 차이 최대 " + q.max_side_err.toFixed(1) + " mm)" }),
        h("div", { text: "모서리 직각 오차 최대 " + q.max_angle_err.toFixed(2) + "°" }),
        q.warn ? h("div", { class: "st-warn", text: "차이가 큽니다 · 꼭짓점을 다시 찍어 보세요 (막지는 않습니다)" }) : null);
    } else pg.q.textContent = "꼭짓점 4개를 찍으면 보입니다";
    pg.marker.textContent = co.marker ? "기억함 · " + fmtWhen(co.marker.t) + " · 사진을 찍을 때마다 헤드 위치를 확인합니다" : "기억하지 않음 · 위치 확인을 하지 않습니다";
    pg.learn.set(whyStage(true, true) || whyCamera() || whyHead());
    const sig = JSON.stringify(co.extra);
    if (sig !== pg.exSig) {
      pg.exSig = sig;
      pg.extra.replaceChildren(...(co.extra.length ? co.extra.map((x, i) => h("div", { class: "row" },
        h("span", { class: "grow", text: (i + 1) + " · X " + f1(x.xy[0]) + " · Y " + f1(x.xy[1]) + " · 오차 " + (x.err == null ? "—" : x.err.toFixed(2) + " mm") + " · " + fmtWhen(x.t) }),
        btn("지우기", () => api("coord_remove", { index: i }), "sm"))) : [small("아직 없습니다")]));
    }
    pg.clear.disabled = running() || !Object.keys(co.stage).length;
    pg.draw();
    pg.jog.update();
  };
  return pg;
}

/* ---------- 6 · 접촉 높이 찾기 ---------- */
function contactLine(ct) {                                       // 접촉 찾기의 진행 · 결과 한 줄(준비 6단계 · 수동 탭이 같이 쓴다)
  if (!ct) return "";
  if (ct.active) return "내리는 중 · Z " + f3(ct.z) + " mm · " + (ct.text || "");
  if (ct.result) return "샘플에 닿음 · Z " + f2(ct.result.first_z) + " mm · 2단자 " + fmtOhm2(ct.result.r2) + " · 4단자 " + fmtOhm2(ct.result.r4);
  if (ct.fail) return ct.fail + " · 핀을 맨 위로 올렸습니다";
  return "";
}
function whyContact() {
  return whyStage(false, true) || (devOk("k2450") ? "" : "저항계(K2450)를 먼저 연결하세요 (준비 1)");
}
function buildStep6() {
  const pg = { jog: jogPart() };
  pg.find = busyGate("전기로 찾기", () => api("contact_find"), "on");
  pg.line = h("div", {});
  pg.save = busyGate("이 높이로 저장", () => api("contact_save"));
  pg.saved = h("div", {});
  pg.el = h("div", { class: "panel prep-panel" }, h("h3", { text: "6 · 접촉 높이 찾기" }),
    h("div", { class: "grp" },
      h("div", { class: "small", text: "핀을 샘플 위로 옮기고, 샘플 위 5 mm 안쪽 높이까지 내린 뒤 누르세요. 아주 약한 전압으로 재면서 조금씩 내려가다가 닿으면 멈춥니다." }),
      pg.jog.el,
      h("div", { class: "row" }, pg.find.b, pg.find.w), pg.line,
      h("div", { class: "row" }, pg.save.b, pg.save.w)),
    h("div", { class: "grp" }, pg.saved,
      h("div", { class: "small", text: "측정할 때는 저장한 높이보다 0.3 mm 위에서부터 다시 확인하며 내리고, 저장한 높이보다 0.5 mm 아래로는 내려가지 않습니다." })));
  pg.render = () => {
    const ct = P().contact || {};
    pg.find.set(ct.active ? "찾는 중" : whyContact());
    pg.line.textContent = contactLine(ct);
    pg.line.className = ct.fail && !ct.active ? "st-warn" : "";
    pg.save.set(running() ? "측정 중에는 할 수 없습니다" : ct.result && !ct.active ? "" : "먼저 찾아야 합니다");
    const z = S.cfg.contact_z_mm;
    pg.saved.textContent = z == null ? "저장한 접촉 높이가 없습니다" : "저장한 접촉 높이 Z " + f2(z) + " mm";
    pg.jog.update();
  };
  return pg;
}

/* ---------- 7 · 쿨러 구역 확인 ---------- */
function buildStep7() {
  const pg = { grid: null, base: "", sel: null };
  pg.cells = [];
  pg.gridEl = h("div", { class: "cgrid" });
  for (let i = 0; i < 9; i++) {
    const b = btn("", () => put(i), "ccell");
    pg.cells.push(b); pg.gridEl.append(b);
  }
  pg.relBtns = {};
  pg.relBox = h("div", { class: "relays" });
  for (let n = 2; n <= 10; n++) {
    const b = btn("R" + n, async () => { const r = await api("cooler_only", { n: S.relays[n - 1] ? 0 : n }); if (r.ok) pg.sel = n; });
    pg.relBtns[n] = b; pg.relBox.append(b);
  }
  pg.note = h("div", {});
  pg.why = h("div", { class: "why" });
  pg.off = btn("모두 끄기", () => api("cooler_only", { n: 0 }));
  pg.el = h("div", { class: "panel prep-panel" }, h("h3", { text: "7 · 쿨러 구역 확인" }),
    h("div", { class: "small", text: "릴레이를 하나씩 켜고(한 번에 하나만 켜집니다), 판에서 차가워지는 칸을 눌러 그 릴레이의 자리를 정하세요. 칸은 판 사진과 같은 방향입니다(왼쪽 위가 A)." }),
    h("div", { class: "two" },
      h("div", { class: "grp" }, h("h4", { text: "릴레이" }), pg.relBox, h("div", { class: "row" }, pg.off), pg.why),
      h("div", { class: "grp" }, h("h4", { text: "판을 위에서 본 칸" }), pg.gridEl, pg.note)));
  async function put(i) {
    const on = onRelay();
    if (on == null) { toast("먼저 릴레이를 하나 켜세요"); return; }
    const g = pg.grid.slice();
    const prev = g.indexOf(on);
    if (prev >= 0) g[prev] = null;                               // 그 릴레이가 다른 칸에 있었으면 거기서 빠진다
    g[i] = on;                                                   // 이 칸에 있던 릴레이는 빠진다(자리 없음)
    pg.grid = g;
    if (g.every((v) => v != null)) {
      const r = await api("cooler_save", { grid: [g.slice(0, 3), g.slice(3, 6), g.slice(6, 9)] });
      if (r.ok) toast("쿨러 자리 배치를 저장했습니다");
    }
    pg.render();
  }
  function onRelay() {
    const hr = S.cfg.heater_relay;
    for (let n = 2; n <= 10; n++) if (n !== hr && S.relays[n - 1]) return n;
    return null;
  }
  pg.render = () => {
    const sig = JSON.stringify(S.cfg.cooler_grid);
    if (sig !== pg.base || !pg.grid) { pg.base = sig; pg.grid = S.cfg.cooler_grid.flat(); }      // 저장된 배치가 바뀌면 거기서 다시 시작
    const on = onRelay(), why = whyStage(false, false);
    pg.cells.forEach((b, i) => { const n = pg.grid[i]; b.textContent = n == null ? "—" : "R" + n; b.classList.toggle("on", n != null && n === on); b.disabled = !!why; });
    for (let n = 2; n <= 10; n++) { const b = pg.relBtns[n]; b.classList.toggle("on", n === on); b.textContent = "R" + n + (n === on ? " 켜짐" : ""); b.disabled = !!why || n === S.cfg.heater_relay; }
    pg.off.disabled = !!why;
    pg.why.textContent = why;
    const miss = [];
    for (let n = 2; n <= 10; n++) if (n !== S.cfg.heater_relay && pg.grid.indexOf(n) < 0) miss.push("R" + n);
    pg.note.textContent = miss.length ? "자리가 없는 릴레이: " + miss.join(" · ") + " (다 정하면 저장됩니다)" : "저장된 배치입니다";
    pg.note.className = miss.length ? "st-warn" : "small";
  };
  return pg;
}

/* ---------- 왼쪽 목록: 단계마다 끝났는지 + 저장된 값 한 줄 ---------- */
function renderPrepList() {
  const st = P().steps || [];
  prep.items.forEach((x, i) => {
    const s = st[i] || {};
    x.sub.textContent = s.text || "";
    x.sub.title = s.text || "";
    x.done.textContent = s.done ? "✓" : "";
  });
  prep.fakeBox.classList.toggle("hidden", !S.fake);
  if (S.fake) {
    prep.fakeLoad.textContent = P().fake_loaded ? "가짜 샘플 치우기" : "가짜 샘플 올리기";
    prep.fakeFill.disabled = prep.fakeLoad.disabled = running();
  }
}
function renderPrep() {
  renderPrepList();
  const pg = prep.pages[prep.step];
  if (pg && pg.render) pg.render();
}

TABS.prep = { build: buildPrep, render: renderPrep };
