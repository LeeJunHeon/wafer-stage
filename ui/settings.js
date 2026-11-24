/* settings.js - "전체 설정"(초기 설정 탭 왼쪽 목록 맨 아래): settings 전체를 고친다.
   배치: 위 줄 = 검색 · [설정 내보내기] · [가져오기] · "PC 값 제외" / 아래 = 왼쪽 묶음 목록(이름 · 항목 수 · "바꿈 n") + 오른쪽 그 묶음 항목(오른쪽만 스크롤).
   맨 위 묶음 "자주 쓰는 설정" = app/config.py Item.common 인 항목만 모은 것(원래 묶음에도 그대로 있다). 고른 묶음은 기억한다(localStorage - 없어도 동작).
   검색: 이름 · 설명 · 키에서(대소문자 · 띄어쓰기 무시) - 검색어가 있으면 모든 묶음의 맞는 항목을 묶음 소제목과 함께 · 지우면 원래 묶음.
   항목 한 줄: 이름(굵게 · 기본값과 다르면 앞에 점) + 회색 요약(설명 첫 줄 · JSON 항목은 고치는 초기 설정 단계) · 값 칸 · [기본값](다를 때만) · 단위.
   행 어디에 마우스를 올려도 전체 설명 말풍선(설명 · 단위 · 기본값 · 범위 - 서버의 설정 정의 app/config.py ITEMS 에서 온다).
   값을 바꾸면 바로 저장한다(서버가 검증 - 못 쓰는 값이면 이유를 알려 주고 되돌린다). 측정 · 순회 · 온도 시험 중에는 고칠 수 없다. */
"use strict";

const stg = { items: [], rows: {}, group: null, q: "" };
const COMMON = "자주 쓰는 설정";
const STG_KEY = "vo2.settings.group";

function buildSettingsPage() {
  /* 설정 내보내기(바꾼 값만) · 가져오기(파일 선택 → 바뀔 항목 확인 → 검사 통과해야 적용 · "PC 값 제외" 기본 켬) */
  stg.exp = btn("설정 내보내기", async () => { const r = await api("settings_export"); if (r.ok && !r.result.cancelled) toast("내보내기 완료 · " + r.result.n + "개 · " + r.result.path); });
  stg.file = h("input", { type: "file", accept: ".json,application/json", class: "hidden", onchange: () => importFile() });
  stg.imp = btn("가져오기", () => { stg.file.value = ""; stg.file.click(); });
  stg.skipPc = tk(h("input", { type: "checkbox", checked: true }), "stg.skipPc");
  stg.search = tk(h("input", { type: "search", class: "set-search", spellcheck: "false", placeholder: "설정 검색 · 이름 · 설명 · 키",
    oninput: () => { stg.q = stg.search.value; showGroup(); } }), "stg.search");
  stg.list = h("div", { class: "set-groups" });
  stg.body = h("div", { class: "set-items" });
  try { stg.group = localStorage.getItem(STG_KEY); } catch (e) { stg.group = null; }
  const el = h("div", { class: "panel prep-panel set-panel" },
    h("div", { class: "row" }, h("h3", { text: "전체 설정" }), stg.search, h("span", { class: "sp" }), stg.exp, stg.imp,
      h("label", { class: "row" }, stg.skipPc, small("PC 값 제외")), stg.file),
    small("변경 즉시 저장 · 기본값과 다른 값만 저장"),
    h("div", { class: "set-main" }, stg.list, stg.body));
  return { el, render: renderSettingsPage, show: loadSettingsPage };
}
const fmtVal = (v) => (v == null ? "없음" : typeof v === "object" ? JSON.stringify(v) : String(v));
async function importFile() {
  const f = stg.file.files && stg.file.files[0];
  if (!f) return;
  let text = "";
  try { text = await f.text(); } catch (e) { toast("파일 읽기 실패"); return; }
  const skip = stg.skipPc.checked;
  const r = await api("settings_import_preview", { text, skip_pc: skip });
  if (!r.ok) return;
  const p = r.result, bad = [...p.errors, ...p.problems];
  const rows = p.changes.map((c) => h("tr", {}, h("td", { text: c.label + (c.pc ? " (PC 값)" : "") }), h("td", { class: "mono", text: fmtVal(c.old) }),
    h("td", { text: "→" }), h("td", { class: "mono", text: fmtVal(c.new) })));
  const body = h("div", { class: "col" },
    small(f.name + " · 바뀔 항목 " + p.changes.length + "개"),
    p.changes.length ? h("table", { class: "tb" }, h("tbody", {}, ...rows)) : small("바뀔 항목 없음"),
    p.skipped.length ? small("PC 값 제외: " + p.skipped.join(" · ")) : null,
    p.ignored.length ? small("모르는 설정 무시: " + p.ignored.join(" · ")) : null,
    ...bad.map((t) => h("div", { class: "st-err", text: t })));
  const go = tk(btn("적용", async () => { const a = await api("settings_import_apply", { text, skip_pc: skip }); if (a.ok) { closeDialog(); toast("가져오기 완료 · " + a.result.n + "개"); loadSettingsPage(); } }, "on"), "stg.importApply");
  setOff(go, bad.length ? "값 오류 · 조합 문제 있음" : !p.changes.length ? "바뀔 항목 없음" : running() ? "측정 중 사용 불가" : "");
  openDialog("설정 가져오기", body, [noEnter(btn("취소", closeDialog)), noEnter(go)]);
}
async function loadSettingsPage() {
  const j = await getJson("/api/settings");
  if (!Array.isArray(j)) { setKids(stg.body, small("설정 읽기 실패")); return; }
  stg.items = j;
  tipsSetCfg(j);                                                 // 말풍선의 설정 설명도 같은 표
  showGroup();
}
/* 묶음 목록(가상 묶음 "자주 쓰는 설정" 이 맨 위) */
function stgGroups() {
  const groups = [{ name: COMMON, items: stg.items.filter((it) => it.common) }];
  for (const it of stg.items) { let g = groups.find((x) => x.name === (it.group || "기타")); if (!g) { g = { name: it.group || "기타", items: [] }; groups.push(g); } g.items.push(it); }
  return groups;
}
const stgVal = (it) => (S && S.cfg && it.key in S.cfg ? S.cfg[it.key] : it.value);
const stgChanged = (it) => JSON.stringify(stgVal(it)) !== JSON.stringify(it.default);
const stgNorm = (t) => String(t || "").toLowerCase().replace(/\s+/g, "");
function stgPick(name) {
  stg.group = name;
  try { localStorage.setItem(STG_KEY, name); } catch (e) { /* 기억 못 해도 동작 */ }
  if (stg.q) { stg.q = ""; stg.search.value = ""; }
  showGroup();
}
function renderGroupList(groups) {
  setKids(stg.list, ...groups.map((g) => {
    const n = g.items.filter(stgChanged).length;
    const b = tk(h("button", { type: "button", class: "set-g" + (!stg.q && g.name === stg.group ? " on" : ""), onclick: () => stgPick(g.name) },
      h("span", { class: "grow", text: g.name }), h("span", { class: "small", text: String(g.items.length) }),
      n ? h("span", { class: "set-chg", text: "바꿈 " + n }) : null), "stg.group");
    b._title = g.name;
    return b;
  }));
}
function showGroup() {
  if (!stg.items.length) return;
  const groups = stgGroups();
  if (!groups.some((g) => g.name === stg.group)) stg.group = groups[0].name;
  renderGroupList(groups);
  stg.rows = {};
  const q = stgNorm(stg.q);
  if (q) {
    const hit = (it) => [it.label, it.desc, it.key].some((t) => stgNorm(t).includes(q));
    const parts = groups.slice(1).map((g) => ({ name: g.name, items: g.items.filter(hit) })).filter((g) => g.items.length);
    setKids(stg.body, ...(parts.length ? parts.map((g) => h("div", { class: "set-sec" }, h("h4", { text: g.name + " · " + g.items.length }), ...g.items.map(settingRow)))
      : [small("맞는 설정 없음")]));
  } else {
    const g = groups.find((x) => x.name === stg.group);
    setKids(stg.body, h("div", { class: "set-sec" }, h("h4", { text: g.name + " · " + g.items.length }),
      g.name === COMMON ? small("측정마다 바꿀 수 있는 값 · 원래 묶음에도 있음") : null, ...g.items.map(settingRow)));
  }
  stg.body.scrollTop = 0;
  if (S) renderSettingsPage();
}
function settingRow(it) {
  const save = async (value) => {
    const ok = await putSettings({ [it.key]: value });
    if (!ok) loadSettingsPage();                                 // 못 쓰는 값이면 서버 값으로 되돌린다
    else { it.value = value; toast(it.label + " 저장 완료"); }
  };
  let input;
  if (it.kind === "bool") input = h("input", { type: "checkbox", checked: !!it.value, onchange: () => save(input.checked) });
  else if (it.kind === "choice") input = h("select", { onchange: () => save(input.value) }, ...it.choices.map(([v, t]) => h("option", { value: v, text: t, selected: it.value === v })));
  else if (it.kind === "int" || it.kind === "float" || it.kind === "optfloat") {
    input = h("input", { type: "number", class: "setnum", step: "any", value: it.value == null ? "" : it.value, placeholder: it.kind === "optfloat" ? "없음" : "",
      onchange: () => { const t = input.value.trim(); if (t === "") { if (it.kind === "optfloat") save(null); else loadSettingsPage(); return; } const v = Number(t); if (!isFinite(v)) { toast("숫자 입력 필요"); loadSettingsPage(); return; } save(v); } });
  } else if (it.kind === "grid" || it.kind === "ints" || it.kind === "xy" || it.kind === "list") {     // JSON 글로 고친다(쿨러 배치 · 쿨러 릴레이 · 촬영 위치 · 사용 안 할 포트)
    input = h("input", { type: "text", class: "settext mono", spellcheck: "false", value: it.value == null ? "" : JSON.stringify(it.value), placeholder: it.kind === "xy" ? "없음 또는 [X, Y]" : "",
      onchange: () => { const t = input.value.trim(); if (t === "" && it.kind === "xy") { save(null); return; } try { save(JSON.parse(t)); } catch (e) { toast("형식 오류 · 예 " + JSON.stringify(it.default)); loadSettingsPage(); } } });
  } else input = h("input", { type: "text", class: "settext", spellcheck: "false", value: it.value == null ? "" : it.value, onchange: () => save(input.value) });
  const browse = it.folder ? browseBtn(input) : null;            // 고르면 입력 칸에 넣고 바로 저장한다(change)
  const reset = tk(btn("기본값", () => save(it.default), "sm"), "stg.default");
  reset._title = "기본값 · " + it.label;
  const dot = h("i", { class: "set-dot" });
  const sum = (it.desc || "").split("\n")[0] + (it.step ? " · " + it.step : "");
  const row = h("div", { class: "set-row" },
    h("div", { class: "set-name" },
      h("div", { class: "row" }, dot, h("b", { text: it.label }), it.verify ? h("span", { class: "st-warn vfy", text: "실장비 확인 필요" }) : null,
        it.pc ? h("span", { class: "vfy pc", text: "PC 값" }) : null, it.device ? h("span", { class: "vfy pc", text: "장비 값" }) : null),
      h("div", { class: "set-sum", text: sum })),
    h("div", { class: "set-val row" }, input, browse, reset),
    h("div", { class: "set-unit small", text: it.unit || "" }));
  tk(input, "cfg:" + it.key);                                    // 값 칸 말풍선도 같은 설명(행 말풍선과 같은 글)
  tip(row, () => ({ title: it.label, text: it.desc + (it.step ? "\n" + it.step : ""), hint: it.hint }));
  stg.rows[it.key] = { it, input, browse, reset, dot, row };
  return row;
}
function renderSettingsPage() {
  if (!S || !S.cfg) return;
  for (const [key, r] of Object.entries(stg.rows)) {             // 다른 곳(조건 창 등)에서 바뀐 값을 따라간다. 고치는 중인 칸은 건드리지 않는다
    const v = S.cfg[key];
    if (document.activeElement === r.input || JSON.stringify(v) === JSON.stringify(r.it.value)) continue;
    r.it.value = v;
    if (r.it.kind === "bool") r.input.checked = !!v;
    else if (r.it.kind === "grid" || r.it.kind === "ints" || r.it.kind === "xy" || r.it.kind === "list") r.input.value = v == null ? "" : JSON.stringify(v);
    else r.input.value = v == null ? "" : v;
  }
  const lock = running() || touring() || !!(S.temptest && S.temptest.active);  // 측정 · 순회 · 온도 시험 중에는 설정을 바꾸지 않는다(서버도 거절)
  const why = running() ? "측정 중 사용 불가" : touring() ? "순회 중 사용 불가" : lock ? "온도 시험 중 사용 불가" : "";
  if (stg.imp) setOff(stg.imp, why);
  for (const r of Object.values(stg.rows)) {
    setOff(r.input, why);
    const chg = stgChanged(r.it);
    r.dot.classList.toggle("on", chg);
    r.reset.classList.toggle("hidden", !chg);
    setOff(r.reset, why);
    if (r.browse) {                                              // [찾아보기] 는 창으로 실행했을 때만 보인다(그때만 입력 칸을 줄여 자리를 낸다)
      setOff(r.browse, why);
      r.browse.classList.toggle("hidden", !S.can_pick_folder);
      r.input.classList.toggle("withbtn", !!S.can_pick_folder);
    }
  }
  const sig = stg.items.map((it) => (stgChanged(it) ? 1 : 0)).join("");    // 바꾼 항목 수가 달라졌으면 묶음 목록을 다시
  if (sig !== stg.chgSig) { stg.chgSig = sig; renderGroupList(stgGroups()); }
}
