/* settings.js - 설정 창(고급 설정 · 측정 조건 편집 공용). 기존 프로그램 설정 창 모양: 왼쪽 세로 분류 목록 · 오른쪽 분류 제목 + 설명 + [이 분류 기본값으로],
   줄 = 이름 | 입력 | 단위 | 옅은 설명. 저장 안 한 변경 표시 · 칸 옆 검증 오류 · 닫을 때 확인 · 아래 줄 [내보내기] [가져오기] … [취소] [저장] 고정, 본문만 스크롤.
   스키마(/schema)로 자동 생성한다 - 새 설정은 스키마에 항목만 더하면 화면이 생긴다. */
"use strict";
const SET = { sch: null, vals: null, defs: null, edits: {}, cur: null, errs: {}, timer: null, open: false };
const SET_DESC = {
  camera: "카메라 번호 · 해상도 · 노출 · 화이트밸런스 · 초점", stage: "속도 · 가동범위 · 조그 · 끝단 이동", position: "대기 위치 · 촬영 위치", photohome: "위치 확인(헤드 마커) 기준",
  tool: "핀(공구) 모양 · 크기", relay: "릴레이 사용 · 이름 · 감시", measure: "히트건 · 접촉 높이 · 시뮬레이터", rt: "저항–온도(R–T) 측정 조건: 전압 · 온도 · 속도 · 안전 · 저장 위치",
  condition: "샘플 측정에서 샘플마다 실행할 단계", detect: "샘플 검출 값", advanced: "속도 · 가동범위 등 자주 바꾸지 않는 값",
};
const SET_PLACE = { "rt.emergency_c": "비우면 최고 온도 + 10" };
const SET_GROUP_ALIAS = { save: "저장" };

function setGet(o, k) { return k.split(".").reduce((a, p) => (a == null ? a : a[p]), o); }
function setItems(tab) { return SET.sch.items.filter((it) => it.tab === tab && it.type !== "object" && it.editor == null && !it.hidden); }
function setVal(it) { return SET.edits[it.key] !== undefined ? SET.edits[it.key] : setGet(SET.vals, it.key); }
function setLocked() { const s = UI.state; return (s && typeof lockedBy === "function" && lockedBy(s)) || (s && typeof rtLock === "function" && rtLock(s)) || ""; }
/* [가져오기] 는 운영체제 파일 창을 연다 - 열려 있는 동안 화면 전체가 막혀 비상정지 · 정지도 못 누른다. 이동 · 작업 · 측정이 도는 동안에는 끈다 */
function setImportWhy() {
  const s = UI.state; if (!s) return "";
  const st = s.stage;
  return setLocked() || (st && st.moving ? "이동 중 · 끝난 뒤" : "") || (st && st.move_block ? st.move_block : "") || (s.camera && s.camera.busy ? s.camera.busy + " 중 · 끝난 뒤" : "");
}

function setInput(it, locked) {
  const v0 = setVal(it);
  const mark = (v) => {
    const orig = setGet(SET.vals, it.key);
    if (JSON.stringify(v) === JSON.stringify(orig)) delete SET.edits[it.key]; else SET.edits[it.key] = v;
    setChanged(); setCheckSoon();
  };
  let el, cls = "sin";
  if (it.type === "bool") { el = h("input", { type: "checkbox", checked: !!v0, onchange: (e) => mark(e.target.checked) }); cls = "sin chk1"; }
  else if (it.type === "enum") { el = h("select", { class: "w220", onchange: (e) => mark(e.target.value) }, (it.options || []).map((o) => h("option", { value: o.value, text: o.label, selected: o.value === v0 }))); }
  else if (it.type === "int" || it.type === "float") {
    el = h("input", { type: "number", class: "w120", value: v0 == null ? "" : v0, step: it.step || "any", min: it.min, max: it.max, placeholder: SET_PLACE[it.key] || (it.nullable ? "비워 둠" : ""),
      onchange: (e) => mark(e.target.value === "" && it.nullable ? null : Number(e.target.value)) });
  } else if (it.type === "xy" || it.type === "list") {
    el = h("input", { type: "text", class: "w220", value: JSON.stringify(v0), onchange: (e) => { try { mark(JSON.parse(e.target.value)); } catch (x) { toast("형식 오류", true); e.target.value = JSON.stringify(setVal(it)); } } });
  } else {
    const wide = /path|addr|port|file/.test(it.key);
    el = h("input", { type: "text", class: wide ? "wfull" : "w220", value: v0 == null ? "" : v0, spellcheck: "false", onchange: (e) => mark(e.target.value) });
  }
  if (locked) el.disabled = true;
  return h("div", { class: cls }, el);
}
function setRows(tab, group) {
  const locked = setLocked();
  const wrap = h("div", { class: "setrows" });
  let grp = null;
  setItems(tab).forEach((it) => {
    if (it.group !== grp) { grp = it.group; wrap.append(h("div", { class: "setg", "data-group": grp, text: grp })); }
    const changed = SET.edits[it.key] !== undefined;
    wrap.append(h("div", { class: "setrow" + (changed ? " chg" : ""), "data-key": it.key },
      h("div", { class: "snm", text: it.label, "data-tip": it.tip || null }), setInput(it, locked), h("div", { class: "sun", text: it.unit || "" }),
      h("div", { class: "sds" }, h("span", { text: it.tip || "" }), h("div", { class: "serr", id: "se-" + it.key.replace(/\./g, "_"), text: SET.errs[it.key] || "" }))));
  });
  return wrap;
}
function setChanged() {
  const n = Object.keys(SET.edits).length;
  const c = $("#set-cnt"); if (c) c.textContent = n ? "변경 " + n + "개 · 저장 전" : "";
  $$(".setrow").forEach((r) => r.classList.toggle("chg", SET.edits[r.dataset.key] !== undefined));
  $$(".setnav .ni").forEach((b) => { const t = b.dataset.tab; b.classList.toggle("chg", setItems(t).some((it) => SET.edits[it.key] !== undefined)); });
  const sv = $("#set-save"); if (sv) sv.classList.toggle("dis", !n || Object.keys(SET.errs).length > 0 || !!setLocked());
}
function setCheckSoon() {
  clearTimeout(SET.timer);
  SET.timer = setTimeout(async () => {
    const r = await send("settings_check", { patch: SET.edits });
    SET.errs = (r.ok && r.res && r.res.errs) || {};
    $$(".serr").forEach((e) => { e.textContent = ""; });
    Object.keys(SET.errs).forEach((k) => { const e = document.getElementById("se-" + k.replace(/\./g, "_")); if (e) e.textContent = SET.errs[k]; });
    setChanged();
  }, 250);
}
function setDraw(group) {
  const main = $("#set-main"); if (!main) return;
  const tabs = SET.sch.tabs, t = tabs.find((x) => x.id === SET.cur) || tabs[0], locked = setLocked();
  const keys = setItems(t.id).map((it) => it.key);
  main.replaceChildren(h("div", { class: "setmh" }, h("div", { class: "grow" }, h("h3", { text: t.label }), h("div", { class: "sub", text: SET_DESC[t.id] || "" })),
    btn("이 분류 기본값으로", "set-defaults", null, "sm", "이 분류의 값을 기본값으로 되돌립니다(저장하기 전에는 바뀌지 않습니다).", locked)),
  (() => { const sc = h("div", { class: "setscroll", id: "set-scroll" }, locked ? h("div", { class: "why", text: locked }) : null, keys.length ? setRows(t.id) : h("div", { class: "sub", text: "이 분류는 전용 화면에서 고칩니다." })); return sc; })());
  $$(".setnav .ni").forEach((b) => b.classList.toggle("on", b.dataset.tab === SET.cur));
  setChanged();
  if (group) { const g = $("#set-scroll .setg[data-group=\"" + (SET_GROUP_ALIAS[group] || group) + "\"]"); if (g) g.scrollIntoView({ block: "start" }); }
}
UI.acts["set-tab"] = (a) => { SET.cur = a.tab; setDraw(); };
UI.acts["set-defaults"] = () => { setItems(SET.cur).forEach((it) => { const d = setGet(SET.defs, it.key); if (JSON.stringify(d) !== JSON.stringify(setGet(SET.vals, it.key))) SET.edits[it.key] = d; else delete SET.edits[it.key]; }); setDraw(); setCheckSoon(); };
UI.acts["set-cancel"] = () => {
  const n = Object.keys(SET.edits).length;
  if (!n) { SET.open = false; closeWin(); return; }
  dialog("저장하지 않고 닫을까요?", "고친 " + n + "개가 사라집니다.", [{ label: "계속 고치기", cls: "primary", fn: () => openSettings(SET.cur, null, true) }, { label: "닫기", cls: "danger", fn: () => { SET.edits = {}; SET.open = false; } }]);
};
UI.acts["set-save"] = async () => {
  if (!Object.keys(SET.edits).length || Object.keys(SET.errs).length || setLocked()) return;
  const r = await send("settings_save", { patch: SET.edits });
  if (r.ok) { SET.edits = {}; SET.open = false; closeWin(); toast("저장"); }
};
UI.acts["set-export"] = () => {
  const blob = new Blob([JSON.stringify(SET.vals, null, 2)], { type: "application/json" });
  const a = h("a", { href: URL.createObjectURL(blob), download: "settings_" + new Date().toISOString().slice(0, 10) + ".json" });
  document.body.append(a); a.click(); a.remove();
  toast("설정을 내보냈습니다");
};
UI.acts["set-import"] = () => {
  const why = setImportWhy();
  if (why) { toast(why, true); return; }                              // 누르는 순간에도 다시 확인한다(화면이 그린 뒤 상태가 바뀔 수 있다)
  const inp = h("input", { type: "file", accept: ".json,application/json", class: "hidden" });
  inp.addEventListener("change", async () => {
    try {
      const obj = JSON.parse(await inp.files[0].text());
      let n = 0;
      SET.sch.items.forEach((it) => { if (it.type === "object" || it.editor != null || it.hidden) return; const v = setGet(obj, it.key); if (v !== undefined && JSON.stringify(v) !== JSON.stringify(setGet(SET.vals, it.key))) { SET.edits[it.key] = v; n++; } });
      setDraw(); setCheckSoon(); toast(n + "개 값을 가져왔습니다 · 저장하면 적용됩니다");
    } catch (e) { toast("가져오기 실패 · 설정 파일 형식 확인", true); }
  });
  document.body.append(inp); inp.click(); setTimeout(() => inp.remove(), 60000);
};

/* 설정 창 열기. tab = 분류 id, group = 그 분류 안에서 처음 보일 묶음 */
async function openSettings(tab, group, keep) {
  if (!SET.sch || !keep) {
    const j = await (await fetch("/schema")).json();
    SET.sch = j.schema; SET.vals = j.values; SET.defs = j.defaults;
  }
  if (!keep) { SET.edits = {}; SET.errs = {}; }
  SET.cur = tab || SET.cur || SET.sch.tabs[0].id;
  SET.open = true;
  const nav = h("div", { class: "setnav" }, SET.sch.tabs.map((t) => h("button", { class: "ni" + (t.id === SET.cur ? " on" : ""), "data-tab": t.id, "data-act": "set-tab", "data-args": JSON.stringify({ tab: t.id }), text: t.label })));
  const foot = h("div", { class: "setfoot" }, btn("내보내기", "set-export", null, "sm", "지금 저장된 설정을 파일로 내보냅니다."), btn("가져오기", "set-import", null, "sm", "설정 파일을 읽어 값을 채웁니다. 저장하기 전에는 적용되지 않습니다.", setImportWhy()),
    h("span", { class: "sub", id: "set-cnt" }), h("span", { class: "grow" }), btn("취소", "set-cancel", null, ""), btn("저장", "set-save", null, "primary", "고친 값을 검증한 뒤 저장합니다.", ""));
  foot.querySelector("[data-act=\"set-save\"]").id = "set-save";
  openWin(h("div", { class: "setwrap" }, h("div", { class: "seth" }, h("h2", { text: "설정" })), h("div", { class: "setbody" }, nav, h("div", { class: "setmain", id: "set-main" })), foot), "setwin");
  setDraw(group);
  if (keep) setCheckSoon();
}
UI.acts["su-adv"] = () => openSettings(null);
