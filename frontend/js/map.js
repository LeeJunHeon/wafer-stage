/* map.js — 스테이지 내비게이션 맵.

   카메라 사진은 '무엇이 있나' 를, 이 맵은 '기계 어디인가' 를 보여준다. 프로브
   스테이션 소프트웨어의 스테이지 맵과 같은 역할이다.
   좌표계: 위 = X+, 왼쪽 = Y+ (카메라를 정면에서 볼 때의 배치와 맞춘다). */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$;
  const NS = 'http://www.w3.org/2000/svg';

  const M = { ox: 18, oy: 12, w: 264, h: 240 };   // viewBox 300×264 안의 작업영역
  let XMAX = 247.6, YMAX = 247.8;

  const mx = (Y) => M.ox + (YMAX - Y) / YMAX * M.w;
  const my = (X) => M.oy + (XMAX - X) / XMAX * M.h;
  const el = (tag, at) => {
    const e = document.createElementNS(NS, tag);
    for (const k in at) e.setAttribute(k, at[k]);
    return e;
  };
  const txt = (cls, x, y, t) => {
    const e = el('text', { class: cls, x: x, y: y });
    e.textContent = t;
    return e;
  };

  UI.applyMap = function (s) {
    const g = $('map');
    g.textContent = '';
    const lim = s.limits || {};
    XMAX = lim.x_max_mm || XMAX;
    YMAX = lim.y_max_mm || YMAX;

    // 작업영역 · 격자 · 축 라벨
    g.appendChild(el('rect', { class: 'm-frame', x: M.ox, y: M.oy, width: M.w, height: M.h }));
    for (let v = 50; v < Math.max(XMAX, YMAX); v += 50) {
      if (v < YMAX) {
        g.appendChild(el('line', { class: 'm-grid', x1: mx(v), y1: M.oy, x2: mx(v), y2: M.oy + M.h }));
        g.appendChild(txt('m-axis', mx(v) - 6, M.oy + M.h + 9, 'Y' + v));
      }
      if (v < XMAX) {
        g.appendChild(el('line', { class: 'm-grid', x1: M.ox, y1: my(v), x2: M.ox + M.w, y2: my(v) }));
        g.appendChild(txt('m-axis', 2, my(v) + 3, 'X' + v));
      }
    }
    g.appendChild(txt('m-axis', M.ox + M.w - 14, M.oy + M.h + 9, 'Y0'));
    g.appendChild(txt('m-axis', 3, M.oy + M.h - 2, 'X0'));

    // X 레일(좌우 볼스크류)
    g.appendChild(el('line', { class: 'm-rail', x1: M.ox - 6, y1: M.oy + 4,
                               x2: M.ox - 6, y2: M.oy + M.h - 4 }));
    g.appendChild(el('line', { class: 'm-rail', x1: M.ox + M.w + 6, y1: M.oy + 4,
                               x2: M.ox + M.w + 6, y2: M.oy + M.h - 4 }));

    // 마커 + 감지영역(마커 중심 바운딩)
    const mk = (s.settings || {}).marker_mm_xy || {};
    const ids = Object.keys(mk);
    if (ids.length) {
      const xs = ids.map(i => +mk[i][0]), ys = ids.map(i => +mk[i][1]);
      g.appendChild(el('rect', {
        class: 'm-rect', x: mx(Math.max.apply(null, ys)), y: my(Math.max.apply(null, xs)),
        width: mx(Math.min.apply(null, ys)) - mx(Math.max.apply(null, ys)),
        height: my(Math.min.apply(null, xs)) - my(Math.max.apply(null, xs)),
      }));
      const side = 30 / XMAX * M.h;          // 마커 30mm 를 축척대로
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
                                   cy: my(w.center_mm[0]), r: 50 / XMAX * M.h }));
    }

    // 파킹 위치
    const p = (s.settings || {}).park_xy || [0, 90];
    g.appendChild(txt('m-park', mx(p[1]) - 4, my(p[0]) - 4, 'P'));

    // 샘플
    const q = s.sequence || {};
    (s.samples || []).forEach(sm => {
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
    if (st.x_mm != null && st.y_mm != null) {
      const x = mx(st.y_mm), y = my(st.x_mm);
      g.appendChild(el('line', { class: 'm-beam', x1: M.ox - 6, y1: y, x2: M.ox + M.w + 6, y2: y }));
      g.appendChild(el('rect', { class: 'm-carriage', x: x - 9, y: y - 5,
                                 width: 18, height: 10, rx: 1.5 }));
      g.appendChild(el('circle', { class: 'm-pointer-ring', cx: x, cy: y, r: 7 }));
      g.appendChild(el('line', { class: 'm-pointer', x1: x - 11, y1: y, x2: x + 11, y2: y }));
      g.appendChild(el('line', { class: 'm-pointer', x1: x, y1: y - 11, x2: x, y2: y + 11 }));
      $('mapPos').textContent = 'X ' + UI.fmt(st.x_mm) + ' Y ' + UI.fmt(st.y_mm)
        + (st.moving ? ' 이동중' : '');
    } else {
      $('mapPos').textContent = UI.EMPTY;
    }
  };
})();
