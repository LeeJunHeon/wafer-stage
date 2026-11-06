/* viewer.js - 사진 보기: 휠 확대 · 끌어서 이동 · 판 맞춤 / 전체 · SVG 덮개(사진 픽셀 좌표). */
"use strict";
const SVGNS = "http://www.w3.org/2000/svg";
function sv(tag, attrs, ...kids) {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in (attrs || {})) if (attrs[k] != null) e.setAttribute(k, attrs[k]);
  for (const c of kids.flat()) if (c != null) e.append(c.nodeType ? c : document.createTextNode(c));
  return e;
}

class Viewer {
  constructor(opts) {
    this.opts = opts || {};
    this.el = h("div", { class: "vp" });
    this.world = h("div", { class: "world" });
    this.img = h("img", { alt: "" });
    this.svg = sv("svg", {});
    this.world.append(this.img, this.svg);
    this.empty = h("div", { class: "empty", text: "사진 없음" });
    this.msg = h("div", { class: "cap-msg hidden" });
    this.el.append(this.world, this.empty, this.msg);
    this.W = 0; this.H = 0; this.k = 1; this.tx = 0; this.ty = 0; this.mode = "plate"; this.area = null; this.url = null; this.userMoved = false;
    this.drag = null; this.onClick = null; this.onMove = null;
    this.el.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = this.el.getBoundingClientRect(), sc = UI.scale || 1;
      this.zoomAt((e.clientX - r.left) / sc, (e.clientY - r.top) / sc, e.deltaY < 0 ? 1.2 : 1 / 1.2);
    }, { passive: false });
    this.handles = null; this.hd = -1; this.onHandle = null;               // handles: [[x, y] ...] 사진 px - 끌어서 옮기는 점
    this.el.addEventListener("mousedown", (e) => {
      if (this.handles) {
        const p = this.toImg(e.clientX, e.clientY);
        const i = p ? this.handles.findIndex((q) => Math.hypot(q[0] - p[0], q[1] - p[1]) < 16 / this.k) : -1;
        if (i >= 0) { this.hd = i; e.preventDefault(); return; }
      }
      this.drag = { x: e.clientX, y: e.clientY, tx: this.tx, ty: this.ty, moved: false };
    });
    window.addEventListener("mousemove", (e) => {
      if (this.hd >= 0) { const p = this.toImg(e.clientX, e.clientY); if (p) { this.handles[this.hd] = [Math.max(0, Math.min(this.W, p[0])), Math.max(0, Math.min(this.H, p[1]))]; if (this.onHandle) this.onHandle(this.hd, this.handles[this.hd]); } return; }
      if (!this.drag) { if (this.onMove) this.onMove(e); return; }
      const sc = UI.scale || 1, dx = (e.clientX - this.drag.x) / sc, dy = (e.clientY - this.drag.y) / sc;
      if (Math.abs(dx) + Math.abs(dy) > 4) { this.drag.moved = true; this.el.classList.add("drag"); }
      if (this.drag.moved) { this.tx = this.drag.tx + dx; this.ty = this.drag.ty + dy; this.userMoved = true; this.apply(); }
    });
    this.onDblClick = null;
    this.el.addEventListener("dblclick", (e) => { const p = this.toImg(e.clientX, e.clientY); if (p && this.onDblClick) this.onDblClick(p[0], p[1], e); });
    window.addEventListener("mouseup", (e) => {
      if (this.hd >= 0) { this.hd = -1; if (this.onHandleEnd) this.onHandleEnd(); return; }
      const d = this.drag; this.drag = null; this.el.classList.remove("drag");
      if (d && !d.moved && this.onClick && this.el.contains(e.target)) { const p = this.toImg(e.clientX, e.clientY); if (p) this.onClick(p[0], p[1], e); }
    });
  }
  size() { return [this.el.clientWidth || 800, this.el.clientHeight || 600]; }
  toImg(cx, cy) {
    const r = this.el.getBoundingClientRect(), sc = UI.scale || 1;
    if (!this.W) return null;
    return [((cx - r.left) / sc - this.tx) / this.k, ((cy - r.top) / sc - this.ty) / this.k];
  }
  set(url, w, h_, version) {
    this.empty.classList.toggle("hidden", !!url);
    this.world.classList.toggle("hidden", !url);
    if (!url) { this.W = 0; this.url = null; return; }
    const key = url + "|" + version;
    const first = !this.W || w !== this.W || h_ !== this.H;
    this.W = w; this.H = h_;
    this.world.style.width = w + "px"; this.world.style.height = h_ + "px";
    this.svg.setAttribute("viewBox", "0 0 " + w + " " + h_);
    if (this.key !== key) { this.img.src = url + (url.includes("?") ? "&" : "?") + "v=" + encodeURIComponent(version); this.key = key; }
    if (first || !this.userMoved) this.fit(this.mode, this.area);
  }
  fit(mode, area) {
    this.mode = mode; this.area = area || this.area;
    if (!this.W) return;
    const [vw, vh] = this.size();
    let x0 = 0, y0 = 0, x1 = this.W, y1 = this.H;
    if (mode === "plate" && this.area && this.area.length >= 3) {
      x0 = Math.min(...this.area.map((p) => p[0])); x1 = Math.max(...this.area.map((p) => p[0]));
      y0 = Math.min(...this.area.map((p) => p[1])); y1 = Math.max(...this.area.map((p) => p[1]));
      const mx = (x1 - x0) * 0.04, my = (y1 - y0) * 0.04; x0 -= mx; x1 += mx; y0 -= my; y1 += my;
    }
    this.k = Math.min(vw / (x1 - x0), vh / (y1 - y0));
    this.tx = (vw - (x1 - x0) * this.k) / 2 - x0 * this.k;
    this.ty = (vh - (y1 - y0) * this.k) / 2 - y0 * this.k;
    this.userMoved = false;
    this.apply();
  }
  zoomAt(px, py, f) {
    const k2 = Math.max(0.05, Math.min(30, this.k * f));
    this.tx = px - (px - this.tx) * (k2 / this.k); this.ty = py - (py - this.ty) * (k2 / this.k); this.k = k2; this.userMoved = true; this.apply();
  }
  zoomCenter(f) { const [vw, vh] = this.size(); this.zoomAt(vw / 2, vh / 2, f); }
  apply() { this.world.style.transform = "translate(" + this.tx + "px," + this.ty + "px) scale(" + this.k + ")"; if (this.onApply) this.onApply(); }
  px(n) { return n / this.k; }                       // 화면 n px 에 해당하는 사진 px
  message(t) { this.msg.classList.toggle("hidden", !t); this.msg.textContent = t || ""; }
  clear() { this.svg.replaceChildren(); }
}

/* 배지 배치: 칩 · 다른 배지와 겹치지 않는 바깥 빈 자리 + 지시선 */
function placeBadges(items, W, H, bw, bh) {
  const boxes = items.map((it) => it.bbox);
  const placed = [];
  const hit = (a, b) => !(a[2] < b[0] || b[2] < a[0] || a[3] < b[1] || b[3] < a[1]);
  const out = [];
  for (let i = 0; i < items.length; i++) {
    const bb = items[i].bbox, cx = (bb[0] + bb[2]) / 2, cy = (bb[1] + bb[3]) / 2;
    let best = null;
    for (let r = 0; r < 14 && !best; r++) {
      const d = (Math.max(bb[2] - bb[0], bb[3] - bb[1]) / 2 + bh * 0.3) + r * bh * 0.7;
      for (let a = 0; a < 16; a++) {
        const th = (a / 16) * Math.PI * 2 - Math.PI / 4;
        const x = cx + Math.cos(th) * (d + bw / 2) - bw / 2, y = cy + Math.sin(th) * (d + bh / 2) - bh / 2;
        const box = [x, y, x + bw, y + bh];
        if (x < 0 || y < 0 || x + bw > W || y + bh > H) continue;
        if (boxes.some((b, j) => hit(box, [b[0] - 2, b[1] - 2, b[2] + 2, b[3] + 2]) && j !== -1)) continue;
        if (placed.some((p) => hit(box, p))) continue;
        best = box; break;
      }
    }
    if (!best) best = [cx - bw / 2, bb[1] - bh - 2, cx + bw / 2, bb[1] - 2];
    placed.push(best);
    out.push({ box: best, from: [cx, cy] });
  }
  return out;
}
function pointInPoly(p, poly) {
  let c = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const a = poly[i], b = poly[j];
    if ((a[1] > p[1]) !== (b[1] > p[1]) && p[0] < ((b[0] - a[0]) * (p[1] - a[1])) / (b[1] - a[1]) + a[0]) c = !c;
  }
  return c;
}
