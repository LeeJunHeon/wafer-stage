/* settings.js - "전체 설정"(초기 설정 탭 왼쪽 목록 맨 아래): settings 전체를 묶음별 표(이름 · 값 · 단위)로 보여 주고 고친다.
   값을 바꾸면 바로 저장한다(서버가 검증 - 못 쓰는 값이면 이유를 알려 주고 되돌린다). "실장비 확인 필요" 항목에는 그 표시를 한다.
   항목 이름(ⓘ)에 마우스를 올리면 설명 말풍선(뜻 · 언제 바꾸는지 · 단위 · 기본값)이 뜬다 - 설명은 서버의 설정 정의(app/config.py ITEMS)에서 온다.
   폴더 항목(데이터 폴더 · NAS 폴더)에는 [찾아보기] 가 붙는다(창으로 실행했을 때만 보인다). */
"use strict";

const stg = { items: [], rows: {} };

function buildSettingsPage() {
  stg.body = h("div", { class: "setbody" });
  /* 설정 내보내기(바꾼 값만) · 가져오기(파일 선택 → 바뀔 항목 확인 → 검사 통과해야 적용 · "PC 값 제외" 기본 켬) */
  stg.exp = btn("설정 내보내기", async () => { const r = await api("settings_export"); if (r.ok && !r.result.cancelled) toast("내보내기 완료 · " + r.result.n + "개 · " + r.result.path); });
  stg.file = h("input", { type: "file", accept: ".json,application/json", class: "hidden", onchange: () => importFile() });
  stg.imp = btn("가져오기", () => { stg.file.value = ""; stg.file.click(); });
  stg.skipPc = h("input", { type: "checkbox", checked: true });
  const el = h("div", { class: "panel prep-panel" }, h("h3", { text: "전체 설정" }),
    h("div", { class: "row" }, small("변경 즉시 저장 · 기본값과 다른 값만 저장"), stg.exp, stg.imp, h("label", { class: "row" }, stg.skipPc, small("PC 값 제외")), stg.file),
    stg.body);
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
  const go = btn("적용", async () => { const a = await api("settings_import_apply", { text, skip_pc: skip }); if (a.ok) { closeDialog(); toast("가져오기 완료 · " + a.result.n + "개"); loadSettingsPage(); } }, "on");
  go.disabled = !!bad.length || !p.changes.length || running();
  openDialog("설정 가져오기", body, [noEnter(btn("취소", closeDialog)), noEnter(go)]);
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
  } else if (it.kind === "grid" || it.kind === "ints" || it.kind === "xy" || it.kind === "list") {     // JSON 글로 고친다(쿨러 배치 · 쿨러 릴레이 · 촬영 위치 · 사용 안 할 포트)
    input = h("input", { type: "text", class: "settext mono", spellcheck: "false", value: it.value == null ? "" : JSON.stringify(it.value), placeholder: it.kind === "xy" ? "없음 또는 [X, Y]" : "",
      onchange: () => { const t = input.value.trim(); if (t === "" && it.kind === "xy") { save(null); return; } try { save(JSON.parse(t)); } catch (e) { toast("형식 오류 · 예 " + JSON.stringify(it.default)); loadSettingsPage(); } } });
  } else input = h("input", { type: "text", class: "settext", spellcheck: "false", value: it.value == null ? "" : it.value, onchange: () => save(input.value) });
  const browse = it.folder ? browseBtn(input) : null;            // 고르면 입력 칸에 넣고 바로 저장한다(change)
  stg.rows[it.key] = { it, input, browse };
  const label = h("span", {}, it.label, h("span", { class: "info", text: "ⓘ" }));
  tip(label, { title: it.label, text: it.desc || "", hint: it.hint || "" });
  return h("tr", {}, h("td", { class: "setname" }, label, it.verify ? h("span", { class: "st-warn vfy", text: "실장비 확인 필요" }) : null,
      it.pc ? h("span", { class: "vfy pc", text: "PC 값" }) : null),
    h("td", { class: "setval" }, browse ? h("div", { class: "row" }, input, browse) : input), h("td", { class: "setunit small", text: it.unit || "" }));
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
  const lock = running() || !!(S.temptest && S.temptest.active);  // 측정 · 온도 시험 중에는 설정을 바꾸지 않는다(서버도 거절)
  if (stg.imp) stg.imp.disabled = lock;
  for (const r of Object.values(stg.rows)) {
    r.input.disabled = lock;
    if (r.browse) {                                              // [찾아보기] 는 창으로 실행했을 때만 보인다(그때만 입력 칸을 줄여 자리를 낸다)
      r.browse.disabled = lock;
      r.browse.classList.toggle("hidden", !S.can_pick_folder);
      r.input.classList.toggle("withbtn", !!S.can_pick_folder);
    }
  }
}
