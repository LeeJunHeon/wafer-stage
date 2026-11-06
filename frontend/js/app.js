/* app.js - 시작 연결 · 탭 바뀔 때 실시간 미리보기 켜고 끄기. */
"use strict";
(function () {
  let last = "";
  setInterval(() => {
    const want = (UI.tab === "measure" && typeof M !== "undefined" && M.mode === "live") || (UI.tab === "setup" && typeof S !== "undefined" && S.view === "live");
    const k = String(want);
    if (k !== last && UI.ws && UI.ws.readyState === 1) { last = k; UI.ws.send(JSON.stringify({ cmd: "preview", on: want })); }
  }, 400);
})();
