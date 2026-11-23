/* zoom.js - 사진을 크게 보고 자리를 고르는 부품 하나(zoomView). 초기 설정 3 [크게 선택](판 영역 꼭짓점) · 수동 제어 탭 [크게 보기](클릭 이동)가 같이 쓴다.
   - 확대 · 축소: 마우스 휠(마우스 자리 기준) · [＋] [－] [맞춤]. 창 맞춤 ~ 원본 1:1 의 4배까지.
   - 이동: 가운데 · 오른쪽 버튼 끌기 · 스페이스바를 누른 채 왼쪽 끌기.
   - 원본 해상도: 바탕은 작은 사진(빠르게), 보이는 부분은 서버가 원본에서 잘라 준 조각(GET /api/img/region.jpg)을 위에 덮는다 - 확대해도 흐려지지 않는다.
   - 돋보기: 마우스를 올리기만 해도 커서 근처 원본을 크게(배율 settings plate_pick_mag) + 십자선. 커서와 반대쪽 구석에 둔다.
   - 좌표는 모두 화면 방향(문 기준) 사진의 픽셀이다 - 서버가 그림 · 조각을 화면 방향으로 돌려 준다(geometry.to_view).
   - 덧그림: opts.draw(g, k) 가 사진 픽셀 좌표로 그린다(k = 화면 1 px 의 사진 픽셀 수 - 선 굵기 · 글자 크기에 곱한다).
   - 누르기: opts.onDown(p, ev) → true 면 끌기를 시작해 onDrag(p) · onUp() 을 부른다. p = 사진 픽셀 [x, y](사진 안으로 잘라 줌). */
"use strict";

const ZOOM_MAX = 4;                       // 원본 1:1 의 4배까지
const LOUPE_PX = 168;                     // 돋보기 한 변(화면 px)

function zoomView(opts) {
  const z = { src: opts.src, W: 1, H: 1, s: 1, tx: 0, ty: 0, fit: 1, pan: null, space: false, hover: null, regT: null, regKey: "", loupeT: 0 };
  z.base = h("img", { class: "zv-base", draggable: "false" });
  z.hi = h("img", { class: "zv-hi hidden", draggable: "false" });
  z.g = svgEl("g", {});
  z.svg = svgEl("svg", { class: "zv-svg" }, z.g);
  z.loupe = h("div", { class: "zv-loupe hidden" }, h("i", { class: "cross-h" }), h("i", { class: "cross-v" }));
  z.note = h("div", { class: "photo-note hidden" });
  z.el = h("div", { class: "zv", tabindex: "0" }, z.base, z.hi, z.svg, z.loupe, z.note);
  z.hi.addEventListener("load", () => z.hi.classList.remove("hidden"));

  /* ---- 좌표 ---- */
  /* 칸의 테두리 안쪽(사진 · 덧그림의 기준). getBoundingClientRect 는 1px 테두리까지 넣어 누른 자리가 화면 1 px 오른쪽 아래로 밀렸다(창에 맞춘 판 사진 0.21 mm) */
  const rect = () => {
    const b = z.el.getBoundingClientRect();
    return { left: b.left + z.el.clientLeft, top: b.top + z.el.clientTop, width: z.el.clientWidth, height: z.el.clientHeight };
  };
  z.toPx = (ev) => { const r = rect(); const x = z.tx + (ev.clientX - r.left) / z.s, y = z.ty + (ev.clientY - r.top) / z.s; return [Math.max(0, Math.min(z.W - 1, x)), Math.max(0, Math.min(z.H - 1, y))]; };
  z.inside = (ev) => { const r = rect(); const x = z.tx + (ev.clientX - r.left) / z.s, y = z.ty + (ev.clientY - r.top) / z.s; return x >= 0 && y >= 0 && x < z.W && y < z.H; };
  function clamp() {                                         // 사진이 칸보다 크면 칸을 벗어나지 않게, 작으면 가운데
    const r = rect(), vw = r.width / z.s, vh = r.height / z.s;
    z.tx = vw >= z.W ? (z.W - vw) / 2 : Math.max(0, Math.min(z.W - vw, z.tx));
    z.ty = vh >= z.H ? (z.H - vh) / 2 : Math.max(0, Math.min(z.H - vh, z.ty));
  }
  z.fitView = () => {
    const r = rect();
    if (r.width < 10 || r.height < 10) return;
    z.fit = Math.min(r.width / z.W, r.height / z.H);
    z.s = z.fit; z.tx = 0; z.ty = 0; clamp(); z.layout();
  };
  z.zoomAt = (f, cx, cy) => {                               // cx, cy = 칸 안의 화면 좌표(그 자리가 그대로 있게)
    const r = rect();
    if (cx == null) { cx = r.width / 2; cy = r.height / 2; }
    const px = z.tx + cx / z.s, py = z.ty + cy / z.s;
    z.s = Math.max(z.fit, Math.min(ZOOM_MAX, z.s * f));
    z.tx = px - cx / z.s; z.ty = py - cy / z.s;
    clamp(); z.layout();
  };

  /* ---- 그리기: 바탕(작은 사진을 사진 크기로 늘림) · 원본 조각 · 덧그림 ---- */
  z.layout = () => {
    const r = rect();
    Object.assign(z.base.style, { width: z.W * z.s + "px", height: z.H * z.s + "px", left: -z.tx * z.s + "px", top: -z.ty * z.s + "px" });
    z.el.classList.toggle("pixel", z.s > 1.01);                // 1:1 보다 크게 보면 화소를 그대로(흐리게 늘이지 않는다)
    z.svg.setAttribute("viewBox", [z.tx, z.ty, r.width / z.s, r.height / z.s].join(" "));
    z.redraw();
    if (z.hiRect) placeHi();
    clearTimeout(z.regT);
    z.regT = setTimeout(loadRegion, 150);                      // 움직임이 멈추면 보이는 부분의 원본 조각을 받는다
  };
  z.redraw = () => { if (opts.draw) { const r = rect(); setKids(z.g); opts.draw(z.g, 1 / z.s, r); } };
  function visible() {
    const r = rect();
    const x0 = Math.max(0, Math.floor(z.tx)), y0 = Math.max(0, Math.floor(z.ty));
    const x1 = Math.min(z.W, Math.ceil(z.tx + r.width / z.s)), y1 = Math.min(z.H, Math.ceil(z.ty + r.height / z.s));
    return x1 > x0 && y1 > y0 ? [x0, y0, x1 - x0, y1 - y0] : null;
  }
  function placeHi() {
    const [x, y, w, hh] = z.hiRect;
    Object.assign(z.hi.style, { left: (x - z.tx) * z.s + "px", top: (y - z.ty) * z.s + "px", width: w * z.s + "px", height: hh * z.s + "px" });
  }
  function loadRegion() {
    if (!z.ready) return;
    const v = visible();
    const dprB = window.devicePixelRatio || 1;
    if (!v || !opts.baseW || z.s * dprB <= opts.baseW / z.W) { z.hi.classList.add("hidden"); z.hiRect = null; z.regKey = ""; return; }   // 바탕 사진으로 충분히 선명하다
    const dpr = window.devicePixelRatio || 1;
    const out = Math.max(16, Math.min(2048, Math.round(Math.min(v[2], v[2] * z.s * dpr))));
    const key = z.ver + "|" + v.join(",") + "|" + out;
    if (key === z.regKey) return;
    z.regKey = key;
    z.hi.classList.add("hidden");
    z.hiRect = v; placeHi();
    z.hi.src = "/api/img/region.jpg?src=" + z.src + "&x=" + v[0] + "&y=" + v[1] + "&w=" + v[2] + "&h=" + v[3] + "&out=" + out + "&v=" + encodeURIComponent(z.ver);
  }

  /* ---- 돋보기: 커서 근처 원본을 (지금 배율 × plate_pick_mag) 로. 커서와 반대쪽 구석 ---- */
  function showLoupe(ev) {
    if (!z.ready || z.pan) { z.loupe.classList.add("hidden"); return; }
    const p = z.toPx(ev), r = rect();
    const mag = Number((S && S.cfg && S.cfg.plate_pick_mag) || 4);
    const sc = Math.min(z.s * mag, ZOOM_MAX * 4);              // 돋보기 안 화면 px / 사진 px
    const span = Math.max(4, Math.round(LOUPE_PX / sc));        // 돋보기가 보이는 사진 픽셀 수
    const x0 = Math.round(Math.max(0, Math.min(z.W - span, p[0] - span / 2))), y0 = Math.round(Math.max(0, Math.min(z.H - span, p[1] - span / 2)));
    const left = ev.clientX - r.left < r.width / 2, top = ev.clientY - r.top < r.height / 2;
    z.loupe.classList.toggle("right", left); z.loupe.classList.toggle("bottom", top);     // 커서와 반대쪽 구석
    z.loupe.classList.remove("hidden");
    const now = performance.now();
    if (now - z.loupeT < 80) return;                           // 1 초에 12번까지
    z.loupeT = now;
    const w = Math.min(span, z.W), hh = Math.min(span, z.H);
    const dpr = window.devicePixelRatio || 1;
    const url = "/api/img/region.jpg?src=" + z.src + "&x=" + x0 + "&y=" + y0 + "&w=" + w + "&h=" + hh + "&out=" + Math.min(512, Math.round(LOUPE_PX * dpr)) + "&v=" + encodeURIComponent(z.ver);
    z.loupe.style.backgroundImage = "url(" + url + ")";
    /* 십자선: 커서가 돋보기 안에서 있는 자리(가장자리에서는 가운데가 아니다) */
    const cx = (p[0] - x0) / w * 100, cy = (p[1] - y0) / hh * 100;
    z.loupe.firstChild.style.top = cy + "%"; z.loupe.lastChild.style.left = cx + "%";
  }

  /* ---- 입력 ---- */
  z.el.addEventListener("wheel", (ev) => { ev.preventDefault(); const r = rect(); z.zoomAt(ev.deltaY < 0 ? 1.25 : 0.8, ev.clientX - r.left, ev.clientY - r.top); showLoupe(ev); }, { passive: false });
  z.el.addEventListener("contextmenu", (ev) => ev.preventDefault());
  z.el.addEventListener("pointerdown", (ev) => {
    if (!z.ready) return;
    z.el.focus({ preventScroll: true });
    if (ev.button === 1 || ev.button === 2 || (ev.button === 0 && z.space)) {          // 이동(끌기)
      ev.preventDefault();
      z.pan = { x: ev.clientX, y: ev.clientY, tx: z.tx, ty: z.ty };
      z.el.setPointerCapture(ev.pointerId); z.el.classList.add("panning");
      z.loupe.classList.add("hidden");
      return;
    }
    if (ev.button !== 0 || !opts.onDown || !z.inside(ev)) return;
    ev.preventDefault();
    if (opts.onDown(z.toPx(ev), ev)) { z.drag = true; z.el.setPointerCapture(ev.pointerId); }
  });
  z.el.addEventListener("pointermove", (ev) => {
    if (z.pan) { z.tx = z.pan.tx - (ev.clientX - z.pan.x) / z.s; z.ty = z.pan.ty - (ev.clientY - z.pan.y) / z.s; clamp(); z.layout(); return; }
    if (z.drag && opts.onDrag) opts.onDrag(z.toPx(ev));
    if (z.inside(ev)) showLoupe(ev); else z.loupe.classList.add("hidden");
  });
  const end = () => { if (z.pan) { z.pan = null; z.el.classList.remove("panning"); } if (z.drag) { z.drag = false; if (opts.onUp) opts.onUp(); } };
  z.el.addEventListener("pointerup", end);
  z.el.addEventListener("pointercancel", end);
  z.el.addEventListener("pointerleave", () => { if (!z.pan && !z.drag) z.loupe.classList.add("hidden"); });
  /* 스페이스바: 누른 동안 왼쪽 끌기 = 이동(창 안의 버튼이 눌리지 않게 막는다). 방향키는 opts.onKey 로 */
  z.onKey = (ev) => {
    if (ev.key === " ") { ev.preventDefault(); z.space = ev.type === "keydown"; z.el.classList.toggle("grab", z.space); return true; }
    if (ev.type === "keydown" && opts.onKey) return opts.onKey(ev);
    return false;
  };

  /* ---- 사진 바꾸기: size = 원본 [가로, 세로] · baseUrl = 바탕(작은 사진) · ver = 사진이 바뀌면 바뀌는 값 ---- */
  z.set = (size, baseUrl, ver, baseW) => {
    if (!size) { z.ready = false; z.base.removeAttribute("src"); z.hi.classList.add("hidden"); z.note.textContent = opts.empty || "사진 없음"; z.note.classList.remove("hidden"); setKids(z.g); return; }
    z.note.classList.add("hidden");
    ver = ver + vq();                                          // 화면 방향이 바뀌어도 새 그림
    const changed = z.ver !== ver;
    z.W = size[0]; z.H = size[1]; z.ver = ver; z.ready = true; opts.baseW = baseW;
    if (changed) { z.base.src = baseUrl + vq(); z.regKey = ""; z.hiRect = null; z.hi.classList.add("hidden"); z.fitView(); }
    else z.layout();
  };
  new ResizeObserver(() => { if (z.ready) { const keep = Math.abs(z.s - z.fit) < 1e-6; const r = rect(); z.fit = Math.min(r.width / z.W, r.height / z.H); if (keep) z.fitView(); else { z.s = Math.max(z.s, z.fit); clamp(); z.layout(); } } }).observe(z.el);
  return z;
}

/* 확대 · 이동 버튼 줄(원격 데스크톱에서 휠이 불편할 때) */
function zoomButtons(z) {
  return h("div", { class: "row" }, btn("＋", () => z.zoomAt(1.5)), btn("－", () => z.zoomAt(1 / 1.5)), btn("맞춤", () => z.fitView()),
    small("휠 확대 · 오른쪽 끌기 이동"));
}

/* 큰 창: 위 띠(비상정지)만 남기고 나머지 전부. 창 안 키(스페이스 · 방향키)는 zoomView 가 받는다(Esc 는 core.js 가 언제나 비상정지) */
let zoomKeys = null;
function openZoomDialog(title, z, side, buttons, render) {
  openDialog(title, h("div", { class: "zv-wrap" }, z.el, h("div", { class: "zv-side" }, zoomButtons(z), side)), buttons, { cls: "full-box", render });
  zoomKeys = (ev) => {
    if ($("#dlg").classList.contains("hidden")) { document.removeEventListener("keydown", zoomKeys); document.removeEventListener("keyup", zoomKeys); zoomKeys = null; return; }
    if (ev.target && ["INPUT", "SELECT", "TEXTAREA"].includes(ev.target.tagName)) return;
    if (z.onKey(ev)) ev.preventDefault();
  };
  document.addEventListener("keydown", zoomKeys);
  document.addEventListener("keyup", zoomKeys);
  requestAnimationFrame(() => z.fitView());
}
