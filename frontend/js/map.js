/* map.js — 스테이지 내비게이션 맵.

   카메라 사진은 '무엇이 있나' 를, 이 맵은 '기계 어디인가' 를 보여준다. 프로브
   스테이션 소프트웨어의 스테이지 맵과 같은 역할이다.
   좌표계: 위 = X+, 왼쪽 = Y+ (카메라를 정면에서 볼 때의 배치와 맞춘다). */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$;
  const NS = 'http://www.w3.org/2000/svg';

  // viewBox 300×264 안의 작업영역. 두 축을 같은 축척(k)으로 그린다 - 축척이
  // 다르면 원형 웨이퍼가 타원으로 보이고 거리 감이 틀어진다.
  //   ox 34 = 왼쪽 X 라벨 자리, oy 6 = 아래 Y 라벨(oy+h+14)까지 264 안에 들어오는 값
  const M = { ox: 34, oy: 6, w: 240, h: 240, k: 1 };
  let XMAX = 247.6, YMAX = 247.8;

  const mx = (Y) => M.ox + (YMAX - Y) / YMAX * M.w;
  const my = (X) => M.oy + (XMAX - X) / XMAX * M.h;
  const el = (tag, at) => {
    const e = document.createElementNS(NS, tag);
    for (const k in at) e.setAttribute(k, at[k]);
    return e;
  };
  const anchorEnd = (e) => { e.setAttribute('text-anchor', 'end'); return e; };
  const txt = (cls, x, y, t) => {
    const e = el('text', { class: cls, x: x, y: y });
    e.textContent = t;
    return e;
  };

  // ---- 쿨러 구역: 판 사각형 위의 3x3 칸(서버가 판 보정으로 스테이지 mm 로 옮긴 사각형을 준다) ----
  let zpop = null;                              // 열려 있는 칸 팝업 {i, vx, vy}
  let zcache = [];                              // 화면 좌표 다각형(클릭 판정용)
  function inPoly(x, y, poly) {
    let inside = false;
    for (let i = 0, n = poly.length; i < n; i++) {
      const a = poly[i], b = poly[(i + 1) % n];
      if ((a[1] > y) !== (b[1] > y) && x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]) inside = !inside;
    }
    return inside;
  }
  function drawZones(g, s) {
    zcache = [];
    const z = s.zones;
    if (!z || !z.has_area) return;
    if (!z.has_cal) {                           // 판 보정이 없으면 맵에는 그리지 않고 안내만(촬영본 오버레이는 감지 영역만으로 그린다)
      g.appendChild(txt('m-note', M.ox + M.w / 2 - 54, M.oy + 14, '쿨러 구역 · 판 보정 필요'));
      return;
    }
    const pts = [];                             // 샘플 · 포인터의 화면 좌표(라벨 회피용)
    (s.samples || []).forEach(sm => { if (sm.X != null) pts.push([mx(sm.Y), my(sm.X)]); });
    const st = s.stage || {};
    if (st.connected && st.x_mm != null) pts.push([mx(st.y_mm), my(st.x_mm)]);
    const act = z.active;
    z.cells.forEach((c, i) => {
      if (!c.quad_stage) return;
      const poly = c.quad_stage.map(p => [mx(p[1]), my(p[0])]);
      zcache[i] = poly;
      const isAct = act && act[0] === c.row && act[1] === c.col;
      g.appendChild(el('polygon', { class: 'm-zone' + (c.on ? ' on' : '') + (isAct ? ' active' : ''),
                                    points: poly.map(p => p.join(',')).join(' ') }));
      const xs = poly.map(p => p[0]), ys = poly.map(p => p[1]);
      const x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs), y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
      // 라벨: 칸 안쪽 귀퉁이 고정 위치 - 쿨러 번호(크게)는 왼쪽 위, 릴레이 · 샘플 수(작게)는 오른쪽 아래.
      // 샘플 점(+번호) · 포인터와 겹치면 반대 귀퉁이로 옮긴다.
      const hit = (p, w, h) => pts.some(q => q[0] + 16 > p[0] - 2 && q[0] - 7 < p[0] + w + 2 && q[1] + 7 > p[1] - 2 && q[1] - 13 < p[1] + h + 2);
      const corners = (w, h) => ({ tl: [x0 + 4, y0 + 3], tr: [x1 - w - 4, y0 + 3], bl: [x0 + 4, y1 - h - 3], br: [x1 - w - 4, y1 - h - 3] });
      const OPP = { tl: 'br', br: 'tl', tr: 'bl', bl: 'tr' };
      const nw = 16, nh = 16, sw = 38, sh = 11;
      let nk = 'tl';
      const cn = corners(nw, nh);
      for (const k of ['tl', 'br', 'tr', 'bl']) { if (!hit(cn[k], nw, nh)) { nk = k; break; } }
      let sk = OPP[nk];
      const cs = corners(sw, sh);
      for (const k of [OPP[nk], nk === 'tl' ? 'bl' : 'tl', 'br', 'bl', 'tr', 'tl']) { if (k !== nk && !hit(cs[k], sw, sh)) { sk = k; break; } }
      const tt = txt('m-zone-no', cn[nk][0], cn[nk][1] + 13, String(c.cooler));
      const t1 = el('title', {}); t1.textContent = c.name + ' · R' + c.relay + (c.on ? ' 켜짐' : ' 꺼짐') + ' · 샘플 ' + c.count + '개';
      tt.appendChild(t1);
      g.appendChild(tt);
      g.appendChild(txt('m-zone-sub', cs[sk][0], cs[sk][1] + 9, 'R' + c.relay + ' · ' + c.count));
    });
  }
  function drawZonePop(g, s) {
    if (!zpop || !s.zones || !s.zones.cells[zpop.i]) return;
    const c = s.zones.cells[zpop.i];
    const run = UI.isRunning(s);
    const st = s.stage || {};
    const can = !run && st.connected;
    const w = 112, rows = can ? 3 : 2, h = 14 + rows * 18;
    let x = Math.min(zpop.vx, 300 - w - 2), y = Math.min(zpop.vy, 264 - h - 2);
    const box = el('g', { class: 'm-zpop' });
    box.appendChild(el('rect', { class: 'm-zpop-bg', x: x, y: y, width: w, height: h, rx: 4 }));
    box.appendChild(txt('m-zpop-t', x + 6, y + 12, c.name + ' · R' + c.relay + (c.on ? ' 켜짐' : ' 꺼짐')));
    const btn = (label, yy, fn) => {
      const b = el('g', { class: 'm-zpop-b' });
      b.appendChild(el('rect', { x: x + 6, y: yy, width: w - 12, height: 15, rx: 3 }));
      b.appendChild(txt('m-zpop-bt', x + 10, yy + 11, label));
      b.addEventListener('click', (ev) => { ev.stopPropagation(); fn(); });
      box.appendChild(b);
    };
    if (can) {
      btn(c.on ? '끄기' : '켜기', y + 18, () => { UI.send({ cmd: 'relay_set', n: c.relay, on: !c.on }); zpop = null; UI.applyMap(UI.state); });
      btn('설정 열기', y + 36, () => { zpop = null; UI.applyMap(UI.state); UI.openSettings('measure'); });
    } else {
      box.appendChild(txt('m-zpop-t', x + 6, y + 30, run ? '순회 중 · 정보만' : '스테이지 미연결'));
      btn('닫기', y + 36 - 18 + 18, () => { zpop = null; UI.applyMap(UI.state); });
    }
    g.appendChild(box);
  }
  window.addEventListener('keydown', (e) => { if (e.key === 'Escape' && zpop) { zpop = null; if (UI.state) UI.applyMap(UI.state); } });

  // 맵 클릭: 칸이면 작은 팝업(켜기/끄기 · 설정 열기, 순회 중이면 정보만) + 수동 이동 팝업의 절대 이동 칸을 채운다(이동은 그쪽에서 누른다).
  $('map').addEventListener('click', (ev) => {
    {
      const r0 = ev.currentTarget.getBoundingClientRect();
      const k0 = Math.min(r0.width / 300, r0.height / 264);
      const vx0 = (ev.clientX - r0.left - (r0.width - 300 * k0) / 2) / k0;
      const vy0 = (ev.clientY - r0.top - (r0.height - 264 * k0) / 2) / k0;
      const hit = zcache.findIndex(p => p && inPoly(vx0, vy0, p));
      if (hit >= 0 && !(ev.target.classList && ev.target.classList.contains('m-s'))) {
        zpop = (zpop && zpop.i === hit) ? null : { i: hit, vx: vx0, vy: vy0 };
        if (UI.state) UI.applyMap(UI.state);
      } else if (zpop) { zpop = null; if (UI.state) UI.applyMap(UI.state); }
    }
    if (!UI.jogPickXY) return;
    const r = ev.currentTarget.getBoundingClientRect();
    // viewBox 300×264 를 preserveAspectRatio="meet" 로 그린다 - 실제 배치된
    // 사각형을 되짚어야 화면 좌표가 맞는다.
    const k = Math.min(r.width / 300, r.height / 264);
    const vx = (ev.clientX - r.left - (r.width - 300 * k) / 2) / k;
    const vy = (ev.clientY - r.top - (r.height - 264 * k) / 2) / k;
    const Y = YMAX - (vx - M.ox) / M.w * YMAX;
    const X = XMAX - (vy - M.oy) / M.h * XMAX;
    if (X < 0 || Y < 0 || X > XMAX || Y > YMAX) return;   // 작업영역 밖
    UI.jogPickXY(X, Y);
  });

  UI.applyMap = function (s) {
    const g = $('map');
    g.textContent = '';
    const lim = s.limits || {};
    XMAX = lim.x_max_mm || XMAX;
    YMAX = lim.y_max_mm || YMAX;
    M.k = Math.min(264 / YMAX, 240 / XMAX);
    M.w = YMAX * M.k;
    M.h = XMAX * M.k;
    const sz = $('mapSize');
    if (sz) sz.textContent = Math.round(XMAX) + '×' + Math.round(YMAX) + ' mm';

    // 작업영역 · 격자 · 축 라벨
    g.appendChild(el('rect', { class: 'm-frame', x: M.ox, y: M.oy, width: M.w, height: M.h }));
    for (let v = 50; v < Math.max(XMAX, YMAX); v += 50) {
      if (v < YMAX) {
        g.appendChild(el('line', { class: 'm-grid', x1: mx(v), y1: M.oy, x2: mx(v), y2: M.oy + M.h }));
        g.appendChild(txt('m-axis', mx(v) - 6, M.oy + M.h + 14, 'Y' + v));
      }
      if (v < XMAX) {
        g.appendChild(el('line', { class: 'm-grid', x1: M.ox, y1: my(v), x2: M.ox + M.w, y2: my(v) }));
        g.appendChild(anchorEnd(txt('m-axis', M.ox - 10, my(v) + 3, 'X' + v)));
      }
    }
    g.appendChild(txt('m-axis', M.ox + M.w - 14, M.oy + M.h + 14, 'Y0'));
    g.appendChild(anchorEnd(txt('m-axis', M.ox - 10, M.oy + M.h + 3, 'X0')));

    // X 레일(좌우 볼스크류)
    g.appendChild(el('line', { class: 'm-rail', x1: M.ox - 6, y1: M.oy + 4,
                               x2: M.ox - 6, y2: M.oy + M.h - 4 }));
    g.appendChild(el('line', { class: 'm-rail', x1: M.ox + M.w + 6, y1: M.oy + 4,
                               x2: M.ox + M.w + 6, y2: M.oy + M.h - 4 }));

    // 마커 + 감지영역(마커 중심 바운딩)
    const mk = (s.settings || {}).marker_mm_xy || {};
    const fixedMode = ((s.plate || {}).mode === 'fixed');
    const ids = fixedMode ? [] : Object.keys(mk);   // 고정 보정에서는 판 마커 자리를 그리지 않는다
    if (fixedMode) {
      // 감지 영역이 판 보정으로 mm 변환되면 그 사각형을 점선으로
      const am = ((s.plate || {}).data || {}).area_mm;
      if (am && am.length === 4) {
        g.appendChild(el('polygon', { class: 'm-rect', points: am.map(p => mx(p[1]) + ',' + my(p[0])).join(' ') }));
      }
    }
    if (ids.length) {
      const xs = ids.map(i => +mk[i][0]), ys = ids.map(i => +mk[i][1]);
      g.appendChild(el('rect', {
        class: 'm-rect', x: mx(Math.max.apply(null, ys)), y: my(Math.max.apply(null, xs)),
        width: mx(Math.min.apply(null, ys)) - mx(Math.max.apply(null, ys)),
        height: my(Math.min.apply(null, xs)) - my(Math.max.apply(null, xs)),
      }));
      const side = 30 * M.k;                 // 마커 30mm 를 축척대로
      ids.forEach(id => {
        const X = +mk[id][0], Y = +mk[id][1];
        g.appendChild(el('rect', { class: 'm-marker', x: mx(Y) - side / 2,
                                   y: my(X) - side / 2, width: side, height: side }));
        g.appendChild(txt('m-marker-t', mx(Y) - side / 2, my(X) - side / 2 - 2, 'id' + id));
      });
    }

    // 웨이퍼(반지름 50mm)
    const w = s.wafer;
    if (w && w.found && w.center_mm) {
      g.appendChild(el('circle', { class: 'm-wafer', cx: mx(w.center_mm[1]),
                                   cy: my(w.center_mm[0]), r: 50 * M.k }));
    }

    // 파킹(P) · 촬영(C) 위치
    const p = (s.settings || {}).park_xy || [0, 90];
    g.appendChild(txt('m-park', mx(p[1]) - 4, my(p[0]) - 4, 'P'));
    const cxy = (s.settings || {}).capture_xy;
    if (cxy) g.appendChild(txt('m-park', mx(cxy[1]) + 2, my(cxy[0]) + 9, 'C'));

    drawZones(g, s);

    // 샘플
    const q = s.sequence || {};
    (s.samples || []).forEach(sm => {
      if (sm.X == null) return;            // 좌표 없는 샘플(검출만 모드)은 맵에 없다
      const cls = ['m-s'];
      if (!sm.on || sm.status === 'skip') cls.push('skip');
      if (sm.status === 'done') cls.push('done');
      if (q.cur_no === sm.no || sm.status === 'moving' || sm.status === 'measuring') cls.push('target');
      if (UI.selected === sm.no) cls.push('selected');
      const c = el('circle', { class: cls.join(' '), cx: mx(sm.Y), cy: my(sm.X), r: 4.2 });
      c.style.cursor = 'pointer';
      c.addEventListener('click', () => UI.select(sm.no));
      g.appendChild(c);
      g.appendChild(txt('m-s-num', mx(sm.Y) + 5, my(sm.X) - 4, sm.no));
    });

    // 빔(현재 X) + 캐리지(현재 Y) + 포인터
    const st = s.stage || {};
    if (st.connected && st.x_mm != null && st.y_mm != null) {
      const x = mx(st.y_mm), y = my(st.x_mm);
      g.appendChild(el('line', { class: 'm-beam', x1: M.ox - 6, y1: y, x2: M.ox + M.w + 6, y2: y }));
      g.appendChild(el('rect', { class: 'm-carriage', x: x - 9, y: y - 5,
                                 width: 18, height: 10, rx: 1.5 }));
      g.appendChild(el('circle', { class: 'm-pointer-ring', cx: x, cy: y, r: 7 }));
      g.appendChild(el('line', { class: 'm-pointer', x1: x - 11, y1: y, x2: x + 11, y2: y }));
      g.appendChild(el('line', { class: 'm-pointer', x1: x, y1: y - 11, x2: x, y2: y + 11 }));
      // 공구 지점 = 스테이지 좌표(기준 포인터) + 오프셋. 오프셋이 있을 때만 따로 그린다.
      const t = s.tool || {};
      const dx = +t.dx_mm || 0, dy = +t.dy_mm || 0;
      if (dx || dy) {
        const tx = mx(st.y_mm + dy), ty = my(st.x_mm + dx);
        g.appendChild(el('line', { class: 'm-tool-link', x1: x, y1: y, x2: tx, y2: ty }));
        g.appendChild(el('circle', { class: 'm-tool', cx: tx, cy: ty, r: 4.5 }));
        g.appendChild(el('circle', { class: 'm-tool-c', cx: tx, cy: ty, r: 1.4 }));
      }
      $('mapPos').textContent = 'X ' + UI.fmt(st.x_mm) + ' Y ' + UI.fmt(st.y_mm)
        + ((dx || dy) ? ' · ' + (t.name || '공구') + ' X ' + UI.fmt(st.x_mm + dx) + ' Y ' + UI.fmt(st.y_mm + dy) : '')
        + (st.moving ? ' 이동중' : '');
    } else {
      $('mapPos').textContent = UI.EMPTY;
    }
    drawZonePop(g, s);
  };
})();
