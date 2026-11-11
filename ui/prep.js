/* prep.js - 준비 탭: 왼쪽 단계 목록(260px) + 오른쪽 그 단계의 화면. 순서를 강제하지 않는다. */
"use strict";

const STEPS = ["장비 연결", "원점 맞추기", "카메라 · 판 영역", "빈 판 사진", "좌표 맞추기", "접촉 높이 찾기", "쿨러 구역 확인"];
const DEVS = [
  { key: "stage", name: "스테이지", cfg: "stage_port", list: "dl-ports", ph: "COM 번호", conn: (v) => api("connect_stage", { port: v }), off: () => api("disconnect_stage") },
  { key: "k2110", name: "온도계 K2110", cfg: "k2110_addr", list: "dl-visa", ph: "VISA 주소", conn: (v) => api("connect_meter", { kind: "k2110", addr: v }), off: () => api("disconnect_meter", { kind: "k2110" }) },
  { key: "k2450", name: "저항계 K2450", cfg: "k2450_addr", list: "dl-visa", ph: "VISA 주소", conn: (v) => api("connect_meter", { kind: "k2450", addr: v }), off: () => api("disconnect_meter", { kind: "k2450" }) },
  { key: "camera", name: "카메라", cfg: "camera_index", list: "dl-cams", ph: "장치 번호", conn: (v) => api("connect_camera", { index: parseInt(v, 10) }), off: () => api("disconnect_camera") },
];
const prep = { step: 0, pages: [], items: [] };

function buildPrep() {
  prep.items = STEPS.map((t, i) => {
    const sub = h("div", { class: "sub" }), done = h("span", { class: "done" });
    const el = h("div", { class: "step", onclick: () => setStep(i) }, h("span", { class: "no", text: i + 1 }),
      h("div", { class: "txt" }, h("div", { class: "t", text: t }), sub), done);
    return { el, sub, done };
  });
  const list = h("div", { class: "panel", id: "prep-list" }, h("h3", { text: "처음 한 번 준비" }), ...prep.items.map((x) => x.el));
  prep.list = list;
  prep.main = h("div", { id: "prep-main" });
  prep.pages = [buildStep1()];
  $("#tab-prep").replaceChildren(list, prep.main);
  setStep(0);
}
function setStep(i) {
  if (!prep.pages[i]) { toast("다음 단계에서 만듭니다"); return; }
  prep.step = i;
  prep.items.forEach((x, k) => x.el.classList.toggle("on", k === i));
  prep.main.replaceChildren(prep.pages[i].el);
  if (prep.pages[i].show) prep.pages[i].show();
  if (S) renderPrep();
}

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

/* ---------- 왼쪽 목록: 단계마다 끝났는지 + 저장된 값 한 줄 ---------- */
function renderPrepList() {
  const st = (S.prep && S.prep.steps) || [];
  prep.items.forEach((x, i) => {
    const s = st[i] || {};
    x.sub.textContent = s.text || "";
    x.sub.title = s.text || "";
    x.done.textContent = s.done ? "✓" : "";
  });
}
function renderPrep() {
  renderPrepList();
  const pg = prep.pages[prep.step];
  if (pg && pg.render) pg.render();
}

TABS.prep = { build: buildPrep, render: renderPrep };
