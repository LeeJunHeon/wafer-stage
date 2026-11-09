/* rtutil.js - R–T 화면의 순수 함수(그래프 기본 범위 · NAS 저장 경로 미리보기). 서버 backend/rt.py 의 nas_target 과 같은 규칙. */
"use strict";
/* 기존 LabVIEW 화면과 같은 기본 범위: 저항 1E+1–1E+6 Ω, 온도 20–100 °C. 값이 넘으면 넓힌다(온도 5 °C · 저항 10배 단위). */
UI.RT_RANGE = { x: [20, 100], y: [1e1, 1e6] };
UI.rtRange = function (temps, rs) {
  let x0 = UI.RT_RANGE.x[0], x1 = UI.RT_RANGE.x[1], y0 = UI.RT_RANGE.y[0], y1 = UI.RT_RANGE.y[1];
  const t = (temps || []).filter((v) => Number.isFinite(v)), r = (rs || []).filter((v) => Number.isFinite(v) && v > 0);
  if (t.length) { x0 = Math.min(x0, Math.floor(Math.min(...t) / 5) * 5); x1 = Math.max(x1, Math.ceil(Math.max(...t) / 5) * 5); }
  if (r.length) { y0 = Math.min(y0, Math.pow(10, Math.floor(Math.log10(Math.min(...r))))); y1 = Math.max(y1, Math.pow(10, Math.ceil(Math.log10(Math.max(...r))))); }
  return { x: [x0, x1], y: [y0, y1] };
};
UI.nasPath = function (base, autoDate, batch, now) {
  base = (base || "").trim();
  if (!base) return "";
  const d = now || new Date(), p2 = (n) => String(n).padStart(2, "0");
  const parts = [];
  if (autoDate) parts.push(String(d.getFullYear()), String(d.getFullYear()) + p2(d.getMonth() + 1) + p2(d.getDate()));
  if ((batch || "").trim()) parts.push(batch.trim());
  return [base.replace(/[\/]+$/, ""), ...parts].join("\\");
};
