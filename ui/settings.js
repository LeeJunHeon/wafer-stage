/* settings.js - "전체 설정"(준비 탭 왼쪽 목록 맨 아래): settings 전체를 묶음별 표(이름 · 값 · 단위)로 보여 주고 고친다.
   값을 바꾸면 바로 저장한다(서버가 검증 - 못 쓰는 값이면 이유를 알려 주고 되돌린다). "실장비 확인 필요" 항목에는 그 표시를 한다. */
"use strict";

const stg = { items: [], rows: {} };

function buildSettingsPage() {
  stg.body = h("div", { class: "setbody" });
  const el = h("div", { class: "panel prep-panel" }, h("h3", { text: "전체 설정" }),
    h("div", { class: "small", text: "변경 즉시 저장" }), stg.body);
  return { el, render: renderSettingsPage, show: loadSettingsPage };
}
async function loadSettingsPage() {
  const j = await getJson("/api/settings");
  if (!Array.isArray(j)) { setKids(stg.body, small("설정 읽기 실패")); return; }
  stg.items = j; stg.rows = {};
  const groups = [];
  for (const it of j) { let g = groups.find((x) => x.name === it.group); if (!g) { g = { name: it.group, items: [] }; groups.push(g); } g.items.push(it); }
  setKids(stg.body, ...groups.map((g) => h("div", { class: "grp" }, h("h4", { text: g.name || "기타" }),
    h("table", { class: "tb settb" }, h("tbody", {}, ...g.items.map(settingRow))))));
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
  } else if (it.kind === "grid" || it.kind === "xy") {
    input = h("input", { type: "text", class: "settext mono", spellcheck: "false", value: it.value == null ? "" : JSON.stringify(it.value), placeholder: it.kind === "xy" ? "없음 또는 [X, Y]" : "",
      onchange: () => { const t = input.value.trim(); if (t === "" && it.kind === "xy") { save(null); return; } try { save(JSON.parse(t)); } catch (e) { toast("형식 오류 · 예 " + JSON.stringify(it.default)); loadSettingsPage(); } } });
  } else input = h("input", { type: "text", class: "settext", spellcheck: "false", value: it.value == null ? "" : it.value, onchange: () => save(input.value) });
  stg.rows[it.key] = { it, input };
  return h("tr", {}, h("td", { class: "setname" }, it.label, it.verify ? h("span", { class: "st-warn vfy", text: "실장비 확인 필요" }) : null),
    h("td", { class: "setval" }, input), h("td", { class: "setunit small", text: it.unit || "" }));
}
function renderSettingsPage() {
  if (!S || !S.cfg) return;
  for (const [key, r] of Object.entries(stg.rows)) {             // 다른 곳(조건 창 등)에서 바뀐 값을 따라간다. 고치는 중인 칸은 건드리지 않는다
    const v = S.cfg[key];
    if (document.activeElement === r.input || JSON.stringify(v) === JSON.stringify(r.it.value)) continue;
    r.it.value = v;
    if (r.it.kind === "bool") r.input.checked = !!v;
    else if (r.it.kind === "grid" || r.it.kind === "xy") r.input.value = v == null ? "" : JSON.stringify(v);
    else r.input.value = v == null ? "" : v;
  }
  const lock = running();
  for (const r of Object.values(stg.rows)) r.input.disabled = lock;
}
