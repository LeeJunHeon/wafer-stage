/* app.js - 시작: 탭 화면을 만들고 서버에 잇는다. (core.js → jog.js → 탭 파일들 → app.js 순서로 읽는다) */
"use strict";

for (const name of TAB_NAMES) {
  const m = TABS[name];
  if (!m || !m.build) continue;
  try { m.build(); } catch (e) { console.error(e); toast("화면 생성 실패 · " + name); }
}
connectWS();
fetch("/api/state").then((r) => r.json()).then((s) => { if (!S) { S = s; render(); readLists(); } }).catch(() => {});
