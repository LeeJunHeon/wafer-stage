/* editors.js — 설정 스키마의 전용 편집기(editor 필드).

   설정 창(settings.js)이 항목의 editor 이름으로 여기의 render(el, item, ctx) 를 부른다.
   ctx: get(key) · set(key, value) · def(key) · errs(prefix) → [[key, 문구]] ·
        rerender() · state(서버 state) · send(obj)
   값은 ctx.set 으로만 바꾼다(설정 창이 변경 표시 · 검증을 맡는다). 구조가 바뀌는
   조작(추가 · 삭제 · 순서)은 rerender 로 다시 그리고, 칸 입력은 다시 그리지 않는다
   (입력 중 포커스를 잃지 않게). */
(function () {
  'use strict';
  const UI = window.UI;
  const E = UI.editors = {};

  const h = UI.h;

  function num(v) {
    if (v === '' || v == null) return v;
    const f = Number(v);
    return isFinite(f) ? f : v;            // 숫자가 아니면 그대로 - 서버가 사유를 돌려준다
  }

  function errCell(ctx, key) {
    const list = ctx.errs(key);
    return list.length ? list[0][1] : '';
  }

  function markErr(el, msg) {
    el.classList.toggle('bad', !!msg);
    if (msg) el.setAttribute('title', msg); else el.removeAttribute('title');
  }

  // ---------------- 시리얼 포트 ----------------
  E.port = {
    render(el, item, ctx) {
      const inp = h('input', { type: 'text', cls: 'in', list: 'portList', placeholder: '자동',
                               value: ctx.get(item.key) || '' });
      inp.addEventListener('input', () => ctx.set(item.key, inp.value.trim() || null));
      el.appendChild(inp);
      if (!document.getElementById('portList')) el.appendChild(h('datalist', { id: 'portList' }));
      ctx.send({ cmd: 'list_ports' });     // 열 때마다 최신 목록으로
    },
  };

  // ---------------- 릴레이 10개 ----------------
  const WIRE = ['노', '주', '보', '갈', '파', '녹', '노', '주', '빨', '검'];
  E.relays = {
    render(el, item, ctx) {
      const rows = ctx.get(item.key) || [];
      const tb = h('tbody');
      rows.forEach((r, i) => {
        const p = item.key + '[' + i + ']';
        const cb = h('input', { type: 'checkbox' });
        cb.checked = !!r.enabled;
        cb.addEventListener('change', () => { r.enabled = cb.checked; ctx.set(item.key, rows); });
        const nm = h('input', { type: 'text', cls: 'in', value: r.name || '', maxlength: 20 });
        nm.addEventListener('input', () => { r.name = nm.value; ctx.set(item.key, rows); });
        const mx = h('input', { type: 'number', cls: 'in num', min: 0, max: 86400, step: 1,
                                value: r.max_on_s });
        mx.addEventListener('input', () => { r.max_on_s = num(mx.value); ctx.set(item.key, rows); });
        markErr(nm, errCell(ctx, p + '.name'));
        markErr(mx, errCell(ctx, p + '.max_on_s'));
        tb.appendChild(h('tr', {}, [
          h('td', { cls: 'mono', text: 'R' + (i + 1) }),
          h('td', {}, [cb]),
          h('td', {}, [nm]),
          h('td', {}, [mx, h('span', { cls: 'unit', text: 's' })]),
          h('td', { cls: 'note', text: 'D' + (22 + 2 * i) + ' ' + WIRE[i] }),
        ]));
      });
      el.appendChild(h('table', { cls: 'etable' }, [
        h('thead', {}, [h('tr', {}, [h('th', { text: '번호' }), h('th', { text: '사용' }),
          h('th', { text: '이름' }), h('th', { text: '최대 켜짐 (0 = 제한 없음)' }),
          h('th', { text: '배선' })])]),
        tb]));
    },
  };

  // ---------------- 공구 목록 ----------------
  const POINTER = '기준 포인터';
  // 끝 모양(tip_*)이 없는 예전 공구는 이름별 기본값을 보여 준다(서버 core/tip.py 와 같은 값)
  function tipDefaults(t) {
    return t.name === POINTER ? { tip_shape: 'rect', tip_w_mm: 10, tip_h_mm: 2, tip_angle_deg: 90 }
                              : { tip_shape: 'circle', tip_w_mm: 5, tip_h_mm: 5, tip_angle_deg: 0 };
  }
  function tipShape(t, item, ctx, rows) {
    const d = tipDefaults(t);
    const sel = h('select', { cls: 'in' });
    [['rect', '사각'], ['circle', '원']].forEach(o => sel.appendChild(h('option', { value: o[0], text: o[1] })));
    sel.value = t.tip_shape || d.tip_shape;
    sel.addEventListener('change', () => {
      Object.keys(d).forEach(k => { if (t[k] == null) t[k] = d[k]; });
      t.tip_shape = sel.value;
      if (sel.value === 'circle') t.tip_h_mm = t.tip_w_mm;
      ctx.set(item.key, rows); ctx.rerender();
    });
    return sel;
  }
  // 스테이지 기준 각도: 0 = 긴 변이 스테이지 X 축, 90 = Y 축. 비우면 판 보정이 생길 때 사진 예비값에서 한 번 채워진다.
  function angCell(f, t, item, ctx, rows, p, off) {
    const inp = h('input', { type: 'number', cls: 'in num', step: 1, value: t[f] != null ? t[f] : '',
                             placeholder: '자동', disabled: off ? 'disabled' : null,
                             'data-tip': off ? '원은 방향이 없습니다.' : '스테이지 기준 각도입니다(0 = 긴 변이 스테이지 X 축, 90 = Y 축). 카메라 방향이 바뀌어도 실물 방향이 그대로입니다. 비우면 판 보정이 생길 때 사진 예비값에서 한 번 채웁니다.' });
    inp.addEventListener('input', () => { t[f] = inp.value === '' ? null : num(inp.value); ctx.set(item.key, rows); });
    markErr(inp, errCell(ctx, p + '.' + f));
    return inp;
  }
  // 지금 사진에서의 방향(서버가 판 보정으로 변환한 값) + 작은 미리보기
  function nowDir(t) {
    const tn = ((UI.state && UI.state.tip) || {}).tips_now || {};
    const deg = tn[t.name];
    const box = h('span', { cls: 'nowdir' });
    if (deg == null || t.tip_shape === 'circle') { box.appendChild(document.createTextNode(UI.EMPTY)); return box; }
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '-12 -12 24 24'); svg.setAttribute('class', 'nowsvg');
    const r = document.createElementNS(NS, 'rect');
    const w = Math.min(20, Math.max(2, (+t.tip_w_mm || 10) * 1.2)), hh = Math.min(20, Math.max(2, (+t.tip_h_mm || 2) * 1.2));
    r.setAttribute('x', -w / 2); r.setAttribute('y', -hh / 2); r.setAttribute('width', w); r.setAttribute('height', hh);
    r.setAttribute('transform', 'rotate(' + deg + ')');
    svg.appendChild(r);
    box.appendChild(svg);
    box.appendChild(document.createTextNode(' ' + (+deg).toFixed(0) + '°'));
    return box;
  }
  function tcell(f, step, t, item, ctx, rows, p, off) {
    const d = tipDefaults(t);
    const inp = h('input', { type: 'number', cls: 'in num', step: step, value: t[f] != null ? t[f] : d[f],
                             disabled: off ? 'disabled' : null,
                             'data-tip': off ? '원은 지름(가로) 하나만 씁니다.' : null });
    inp.addEventListener('input', () => {
      Object.keys(d).forEach(k => { if (t[k] == null) t[k] = d[k]; });
      t[f] = num(inp.value); ctx.set(item.key, rows);
    });
    markErr(inp, errCell(ctx, p + '.' + f));
    return inp;
  }
  E.tools = {
    render(el, item, ctx) {
      const rows = ctx.get(item.key) || [];
      const tb = h('tbody');
      const active = ctx.get('tool.active');
      rows.forEach((t, i) => {
        const p = item.key + '[' + i + ']';
        const isPtr = t.name === POINTER && i === rows.findIndex(x => x.name === POINTER);
        const nm = h('input', { type: 'text', cls: 'in', value: t.name || '', maxlength: 20,
                                readonly: isPtr ? 'readonly' : null });
        nm.addEventListener('input', () => {
          const old = t.name;
          t.name = nm.value;
          ctx.set(item.key, rows);
          if (ctx.get('tool.active') === old) ctx.set('tool.active', t.name);
        });
        const cell = (f, step) => {
          const inp = h('input', { type: 'number', cls: 'in num', step: step, value: t[f] });
          inp.addEventListener('input', () => { t[f] = num(inp.value); ctx.set(item.key, rows); });
          markErr(inp, errCell(ctx, p + '.' + f));
          return inp;
        };
        const note = h('input', { type: 'text', cls: 'in', value: t.note || '', maxlength: 80 });
        note.addEventListener('input', () => { t.note = note.value; ctx.set(item.key, rows); });
        markErr(nm, errCell(ctx, p + '.name'));
        const dup = h('button', { cls: 'btn sm', text: '복제', 'data-tip': '이 공구를 복사해 새 공구를 만듭니다.',
          onclick: () => {
            let n = 2, name = t.name + ' ' + n;
            while (rows.some(x => x.name === name)) { n += 1; name = t.name + ' ' + n; }
            rows.splice(i + 1, 0, Object.assign(UI.clone(t), { name: name }));
            ctx.set(item.key, rows); ctx.rerender();
          } });
        const del = h('button', { cls: 'btn sm', text: '삭제', disabled: isPtr ? 'disabled' : null,
          'data-tip': isPtr ? '기준 포인터는 지울 수 없습니다.' : '이 공구를 목록에서 뺍니다.',
          onclick: () => {
            rows.splice(i, 1);
            ctx.set(item.key, rows);
            if (ctx.get('tool.active') === t.name) ctx.set('tool.active', POINTER);
            ctx.rerender();
          } });
        tb.appendChild(h('tr', { cls: t.name === active ? 'act' : '' }, [
          h('td', {}, [nm]),
          h('td', {}, [cell('dx_mm', 0.01)]), h('td', {}, [cell('dy_mm', 0.01)]),
          h('td', {}, [cell('z_work_mm', 0.1)]),
          h('td', {}, [tipShape(t, item, ctx, rows)]),
          h('td', {}, [tcell('tip_w_mm', 0.1, t, item, ctx, rows, p)]),
          h('td', {}, [tcell('tip_h_mm', 0.1, t, item, ctx, rows, p, t.tip_shape === 'circle')]),
          h('td', {}, [angCell('tip_angle_deg', t, item, ctx, rows, p, t.tip_shape === 'circle')]),
          h('td', {}, [tcell('tip_angle_img_deg', 1, t, item, ctx, rows, p, t.tip_shape === 'circle')]),
          h('td', {}, [nowDir(t)]),
          h('td', {}, [note]),
          h('td', {}, [h('div', { cls: 'cellrow' }, [dup, del])]),
        ]));
      });
      el.appendChild(h('table', { cls: 'etable' }, [
        h('thead', {}, [h('tr', {}, [h('th', { text: '이름' }), h('th', { text: 'dX mm' }),
          h('th', { text: 'dY mm' }), h('th', { text: '작업 높이 mm' }), h('th', { text: '끝 모양' }), h('th', { text: '끝 가로 mm' }),
          h('th', { text: '끝 세로 mm' }), h('th', { text: '스테이지 각도 °' }), h('th', { text: '사진 예비 °' }),
          h('th', { text: '지금 사진 방향' }), h('th', { text: '메모' }),
          h('th', { text: '' })])]),
        tb]));
      const add = h('button', { cls: 'btn sm', text: '공구 추가', 'data-tip': '새 공구를 목록 끝에 더합니다.',
        onclick: () => {
          let n = rows.length + 1, name = '공구 ' + n;
          while (rows.some(x => x.name === name)) { n += 1; name = '공구 ' + n; }
          rows.push({ name: name, dx_mm: 0, dy_mm: 0, z_work_mm: 0, tip_shape: 'circle', tip_w_mm: 5, tip_h_mm: 5,
                      tip_angle_deg: null, tip_angle_img_deg: 0, note: '' });
          ctx.set(item.key, rows); ctx.rerender();
        } });
      el.appendChild(h('div', { cls: 'erow' }, [add,
        h('span', { cls: 'note', text: '공구 끝 = 스테이지 좌표 + (dX, dY) · 샘플을 겨냥할 때 스테이지는 샘플 − (dX, dY)' })]));
    },
  };

  function nameSelect(el, item, ctx, listKey) {
    const names = (ctx.get(listKey) || []).map(x => x && x.name).filter(Boolean);
    const sel = h('select', { cls: 'in' });
    names.forEach(n => sel.appendChild(h('option', { value: n, text: n })));
    sel.value = ctx.get(item.key);
    sel.addEventListener('change', () => ctx.set(item.key, sel.value));
    el.appendChild(sel);
  }
  E.tool_active = { render(el, item, ctx) { nameSelect(el, item, ctx, 'tool.items'); } };
  E.recipe_active = { render(el, item, ctx) { nameSelect(el, item, ctx, 'recipe.items'); } };

  // ---------------- 쿨러 구역(판 사각형 mm 기준 3x3 칸) ----------------
  let zSel = 0;                                  // 선택한 칸(행 우선 번호)
  function zNorm(z) {                            // 예전 형식({rows, cols, relays})이면 새 형식으로(서버 core/zones.py migrate 와 같다)
    z = z || {};
    z.rows = z.rows || 3; z.cols = z.cols || 3;
    const n = z.rows * z.cols;
    if (!z.cells) {
      const rl = z.relays || [];
      z.cells = [];
      for (let i = 0; i < n; i++) z.cells.push({ cooler: i + 1, relay: rl[i] || i + 1, name: '쿨러 ' + (i + 1) });
      delete z.relays;
    }
    if (z.row_edges === undefined) z.row_edges = null;
    if (z.col_edges === undefined) z.col_edges = null;
    return z;
  }
  function zEdges(z, W, H) {
    const eq = (n, T) => Array.from({ length: n + 1 }, (_, i) => T * i / n);
    return [z.row_edges || eq(z.rows, H), z.col_edges || eq(z.cols, W)];
  }
  // 4점 호모그래피(JS): src → dst
  function homog(src, dst) {
    const A = [], b = [];
    for (let i = 0; i < 4; i++) {
      const [x, y] = src[i], [u, v] = dst[i];
      A.push([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.push(u);
      A.push([0, 0, 0, x, y, 1, -v * x, -v * y]); b.push(v);
    }
    for (let c = 0; c < 8; c++) {                // 가우스 소거
      let p = c;
      for (let r = c + 1; r < 8; r++) if (Math.abs(A[r][c]) > Math.abs(A[p][c])) p = r;
      [A[c], A[p]] = [A[p], A[c]]; [b[c], b[p]] = [b[p], b[c]];
      for (let r = c + 1; r < 8; r++) {
        const f = A[r][c] / A[c][c];
        for (let k = c; k < 8; k++) A[r][k] -= f * A[c][k];
        b[r] -= f * b[c];
      }
    }
    const h = new Array(8);
    for (let r = 7; r >= 0; r--) {
      let t = b[r];
      for (let k = r + 1; k < 8; k++) t -= A[r][k] * h[k];
      h[r] = t / A[r][r];
    }
    return [[h[0], h[1], h[2]], [h[3], h[4], h[5]], [h[6], h[7], 1]];
  }
  function hApply(H, x, y) {
    const w = H[2][0] * x + H[2][1] * y + H[2][2];
    return [(H[0][0] * x + H[0][1] * y + H[0][2]) / w, (H[1][0] * x + H[1][1] * y + H[1][2]) / w];
  }
  function orderQuad(q) {                        // 사진 기준 TL · TR · BR · BL (서버 plate.order_quad 와 같다)
    const cx = q.reduce((a, p) => a + p[0], 0) / 4, cy = q.reduce((a, p) => a + p[1], 0) / 4;
    return q.slice().sort((a, b) => Math.atan2(a[1] - cy, a[0] - cx) - Math.atan2(b[1] - cy, b[0] - cx));
  }

  E.zones = {
    render(el, item, ctx) {
      const z = zNorm(ctx.get(item.key));
      const sz = ctx.get('calib.area_size_mm') || [154, 154];
      const W = +sz[0] || 154, H = +sz[1] || 154;
      const n = z.rows * z.cols;
      if (zSel >= n) zSel = 0;
      const commit = () => ctx.set(item.key, z);
      const resize = () => {
        while (z.cells.length < n) { const i = z.cells.length; z.cells.push({ cooler: Math.min(9, i + 1), relay: Math.min(10, i + 1), name: '쿨러 ' + Math.min(9, i + 1) }); }
        z.cells.length = n;
      };
      const dim = (f, label) => {
        const inp = h('input', { type: 'number', cls: 'in num', min: 1, max: 6, step: 1, value: z[f] });
        inp.addEventListener('change', () => {
          z[f] = Math.max(1, Math.min(6, Math.round(num(inp.value) || 1)));
          z.row_edges = z.col_edges = null;      // 칸 수가 바뀌면 등분으로
          resize(); commit(); ctx.rerender();
        });
        return [h('span', { cls: 'note', text: label }), inp];
      };
      const bEq = h('button', { cls: 'btn sm', text: '등분으로', 'data-tip': '행 · 열 경계를 판 크기(' + W + ' × ' + H + ' mm)의 등분으로 되돌립니다.',
        onclick: () => { z.row_edges = z.col_edges = null; commit(); ctx.rerender(); } });
      el.appendChild(h('div', { cls: 'erow' }, dim('rows', '행').concat(dim('cols', '열')).concat([bEq,
        h('span', { cls: 'note', text: '판 ' + W + ' × ' + H + ' mm (감지 영역 실제 크기)' })])));

      // ---- 미리보기: 현재 촬영본(있으면) 위에 칸을 겹쳐 실제 쿨러 위치와 맞춰 본다 ----
      const S = UI.state || {};
      const area = ((S.plate || {}).data || {}).area;
      const fr = S.frame || ((S.plate || {}).shot) || null;
      const photo = !!(area && area.length === 4 && fr);
      const NS = 'http://www.w3.org/2000/svg';
      const svg = document.createElementNS(NS, 'svg');
      svg.setAttribute('class', 'zoneprev');
      let toView, fromView;
      if (photo) {
        const q = orderQuad(area);
        const Hp = homog(q, [[0, 0], [W, 0], [W, H], [0, H]]);          // px → 판 mm
        const Hi = homog([[0, 0], [W, 0], [W, H], [0, H]], q);          // 판 mm → px
        toView = (x, y) => hApply(Hi, x, y);
        fromView = (u, v) => hApply(Hp, u, v);
        svg.setAttribute('viewBox', '0 0 ' + fr.w + ' ' + fr.h);
        const im = document.createElementNS(NS, 'image');
        im.setAttribute('href', fr.url); im.setAttribute('width', fr.w); im.setAttribute('height', fr.h);
        svg.appendChild(im);
      } else {
        toView = (x, y) => [x + 10, y + 10];
        fromView = (u, v) => [u - 10, v - 10];
        svg.setAttribute('viewBox', '0 0 ' + (W + 20) + ' ' + (H + 20));
      }
      const K = photo ? Math.max(1, fr.w / 1280) : W / 154;
      const SE = (tag, at) => { const e = document.createElementNS(NS, tag); for (const k in at) e.setAttribute(k, at[k]); return e; };
      const dragging = { kind: null, i: 0 };
      const draw = () => {
        Array.from(svg.querySelectorAll('.zd')).forEach(e => e.remove());
        const [re, ce] = zEdges(z, W, H);
        for (let r = 0; r < z.rows; r++) for (let c = 0; c < z.cols; c++) {
          const idx = r * z.cols + c, cell = z.cells[idx] || {};
          const ps = [[ce[c], re[r]], [ce[c + 1], re[r]], [ce[c + 1], re[r + 1]], [ce[c], re[r + 1]]].map(p => toView(p[0], p[1]));
          const poly = SE('polygon', { class: 'zd zc' + (idx === zSel ? ' sel' : ''), points: ps.map(p => p.join(',')).join(' ') });
          poly.addEventListener('click', () => { zSel = idx; ctx.rerender(); });
          svg.appendChild(poly);
          const cx = ps.reduce((a, p) => a + p[0], 0) / 4, cy = ps.reduce((a, p) => a + p[1], 0) / 4;
          const t = SE('text', { class: 'zd zt', x: cx, y: cy, 'font-size': 14 * K }); t.textContent = cell.cooler || '';
          svg.appendChild(t);
          const t2 = SE('text', { class: 'zd zs', x: cx, y: cy + 14 * K, 'font-size': 10 * K }); t2.textContent = 'R' + (cell.relay || '');
          svg.appendChild(t2);
        }
        // 끌 수 있는 경계선(바깥 경계는 고정)
        const line = (kind, i, a, b) => {
          const p0 = toView(a[0], a[1]), p1 = toView(b[0], b[1]);
          const ln = SE('line', { class: 'zd zl drag', x1: p0[0], y1: p0[1], x2: p1[0], y2: p1[1] });
          ln.addEventListener('pointerdown', (ev) => { dragging.kind = kind; dragging.i = i; svg.setPointerCapture(ev.pointerId); ev.preventDefault(); });
          svg.appendChild(ln);
        };
        for (let c = 1; c < z.cols; c++) line('col', c, [ce[c], 0], [ce[c], H]);
        for (let r = 1; r < z.rows; r++) line('row', r, [0, re[r]], [W, re[r]]);
      };
      svg.addEventListener('pointermove', (ev) => {
        if (!dragging.kind) return;
        const rc = svg.getBoundingClientRect(), vb = svg.viewBox.baseVal;
        const vx = vb.x + (ev.clientX - rc.left) / rc.width * vb.width, vy = vb.y + (ev.clientY - rc.top) / rc.height * vb.height;
        const [px, py] = fromView(vx, vy);
        const [re, ce] = zEdges(z, W, H);
        if (dragging.kind === 'col') { const a = ce.slice(); a[dragging.i] = Math.round(Math.max(a[dragging.i - 1] + 1, Math.min(a[dragging.i + 1] - 1, px)) * 10) / 10; z.col_edges = a; }
        else { const a = re.slice(); a[dragging.i] = Math.round(Math.max(a[dragging.i - 1] + 1, Math.min(a[dragging.i + 1] - 1, py)) * 10) / 10; z.row_edges = a; }
        commit(); draw();
        const inp = el.querySelector('[data-edge="' + dragging.kind + dragging.i + '"]');
        if (inp) inp.value = (dragging.kind === 'col' ? z.col_edges : z.row_edges)[dragging.i];
      });
      svg.addEventListener('pointerup', () => { dragging.kind = null; });
      draw();
      el.appendChild(svg);
      el.appendChild(h('div', { cls: 'note', text: photo ? '현재 촬영본 위에 칸을 겹쳐 보입니다. 노란 경계선을 끌어 실제 쿨러 위치에 맞춥니다.' : '촬영본 · 감지 영역이 없어 판 사각형(mm)만 보입니다. 보정 창에서 감지 영역을 지정하고 촬영하면 사진 위에 겹쳐 보입니다.' }));

      // ---- 경계(mm) 입력 ----
      const [re0, ce0] = zEdges(z, W, H);
      const edgeIn = (kind, i, val) => {
        const inp = h('input', { type: 'number', cls: 'in num', step: 0.1, value: Math.round(val * 100) / 100, 'data-edge': kind + i });
        inp.addEventListener('change', () => {
          const [re, ce] = zEdges(z, W, H);
          const a = (kind === 'col' ? ce : re).slice();
          a[i] = Math.max(a[i - 1] + 1, Math.min(a[i + 1] - 1, num(inp.value)));
          if (kind === 'col') z.col_edges = a; else z.row_edges = a;
          commit(); ctx.rerender();
        });
        return inp;
      };
      const ce = [h('span', { cls: 'note', text: '열 경계 mm' })];
      for (let c = 1; c < z.cols; c++) ce.push(edgeIn('col', c, ce0[c]));
      const rr = [h('span', { cls: 'note', text: '행 경계 mm' })];
      for (let r = 1; r < z.rows; r++) rr.push(edgeIn('row', r, re0[r]));
      el.appendChild(h('div', { cls: 'erow' }, ce));
      el.appendChild(h('div', { cls: 'erow' }, rr));

      // ---- 선택한 칸: 쿨러 번호 · 릴레이 번호 · 이름 ----
      const cell = z.cells[zSel] || z.cells[0];
      const relays = ctx.get('relay.items') || [];
      const selCo = h('select', { cls: 'in' });
      for (let k = 1; k <= 9; k++) selCo.appendChild(h('option', { value: k, text: String(k) }));
      selCo.value = String(cell.cooler);
      selCo.addEventListener('change', () => { cell.cooler = +selCo.value; commit(); ctx.rerender(); });
      const selRe = h('select', { cls: 'in' });
      for (let k = 1; k <= 10; k++) selRe.appendChild(h('option', { value: k, text: 'R' + k + ' ' + ((relays[k - 1] || {}).name || '') + ((relays[k - 1] || {}).enabled ? '' : ' (사용 안 함)') }));
      selRe.value = String(cell.relay);
      const nmIn = h('input', { type: 'text', cls: 'in', value: cell.name || '', maxlength: 20 });
      nmIn.addEventListener('change', () => { cell.name = nmIn.value; commit(); ctx.rerender(); });
      const dup = z.cells.findIndex((c, i) => i !== zSel && c.relay === cell.relay);
      selRe.addEventListener('change', () => {
        const r = +selRe.value;
        cell.relay = r; commit();
        // 릴레이 탭 연동: 구역에 지정된 릴레이를 '사용'으로 표시(확인) · 이름을 구역 이름으로 제안
        const it = relays[r - 1];
        if (it && (!it.enabled || it.name === 'R' + r)) {
          UI.confirm('릴레이 R' + r + ' 을 쿨러 칸 "' + (cell.name || '쿨러 ' + cell.cooler) + '" 용으로 사용 지정하고, 이름을 "' + (cell.name || '쿨러 ' + cell.cooler) + '" 로 합니다.', '릴레이 연동').then(ok => {
            if (ok) { it.enabled = true; if (it.name === 'R' + r) it.name = cell.name || ('쿨러 ' + cell.cooler); ctx.set('relay.items', relays); }
            ctx.rerender();
          });
        } else ctx.rerender();
      });
      el.appendChild(h('div', { cls: 'erow' }, [h('span', { cls: 'note', text: '선택한 칸 ' + (Math.floor(zSel / z.cols) + 1) + '행 ' + (zSel % z.cols + 1) + '열' }),
        h('span', { cls: 'note', text: '쿨러' }), selCo, h('span', { cls: 'note', text: '릴레이' }), selRe, h('span', { cls: 'note', text: '이름' }), nmIn]));
      if (dup >= 0) el.appendChild(h('div', { cls: 'warnline', text: '릴레이 R' + cell.relay + ' 가 칸 ' + (dup + 1) + ' 과 중복 · 저장이 거절됩니다' }));
      markErr(el, errCell(ctx, item.key));
    },
  };

  // ---------------- 판 마커 좌표 ----------------
  E.markers = {
    render(el, item, ctx) {
      const mk = ctx.get(item.key) || {};
      ['0', '1', '2', '3'].forEach(id => { if (!mk[id]) mk[id] = [0, 0]; });
      const tb = h('tbody');
      Object.keys(mk).sort().forEach(id => {
        const xy = mk[id];
        const cell = (k) => {
          const inp = h('input', { type: 'number', cls: 'in num', step: 0.1, value: xy[k] });
          inp.addEventListener('input', () => { xy[k] = num(inp.value); ctx.set(item.key, mk); });
          markErr(inp, errCell(ctx, item.key + '.' + id));
          return inp;
        };
        tb.appendChild(h('tr', {}, [h('td', { cls: 'mono', text: 'id' + id }),
          h('td', {}, [cell(0)]), h('td', {}, [cell(1)])]));
      });
      el.appendChild(h('table', { cls: 'etable narrow' }, [
        h('thead', {}, [h('tr', {}, [h('th', { text: '마커' }), h('th', { text: 'X mm' }),
          h('th', { text: 'Y mm' })])]), tb]));
      el.appendChild(h('div', { cls: 'note', text: '포인터로 다시 잡으려면 [보정] → 판 마커 탭' }));
    },
  };

  // ---------------- 레시피 ----------------
  const STEP = { move_sample: '샘플 위로 이동', z: 'Z 이동', relay: '릴레이', wait: '대기',
                 measure: '측정', confirm: '확인 대기', cooler: '쿨러', contact: '접촉 탐색',
                 wait_temp: '온도 대기', heat: '가열', cool: '냉각 대기', repeat: '반복', log: '기록' };
  const NEW_STEP = {
    z: () => ({ type: 'z', target: 'work' }),
    relay: () => ({ type: 'relay', n: 1, on: true }),
    wait: () => ({ type: 'wait', s: 5 }),
    measure: () => ({ type: 'measure' }),
    confirm: () => ({ type: 'confirm' }),
    cooler: () => ({ type: 'cooler', action: 'on', target: 'sample' }),
    contact: () => ({ type: 'contact', start_ref: 'work', start_mm: -2, step_mm: 0.1, max_ref: 'work', max_mm: 1,
                     press_mm: 0.1, settle_s: 0.2, on_fail: 'stop' }),
    wait_temp: () => ({ type: 'wait_temp', cond: '<=', target_c: 30, hold_s: 5, max_s: 300, on_timeout: 'confirm' }),
    heat: () => ({ type: 'heat', end: 'either', target_c: 150, time_s: 60, max_s: 120 }),
    cool: () => ({ type: 'cool', cond: '<=', target_c: 40, hold_s: 0, max_s: 300, on_timeout: 'continue' }),
    repeat: () => ({ type: 'repeat', from: 2, to: 2, count: 2 }),
    log: () => ({ type: 'log', action: 'start' }),
  };
  let recSel = null;                       // 편집 중인 레시피 이름

  // 반복 블록 안의 단계는 들여쓴다(from~to 안 = 깊이 1 이상)
  function indentOf(steps, j) {
    let d = 0;
    steps.forEach((s, k) => {
      if (s.type === 'repeat' && k > j && +s.from - 1 <= j && j < k) d += 1;
    });
    return Math.min(d, 3);
  }
  function numIn(st, f, step, commit, min, w) {
    const inp = h('input', { type: 'number', cls: 'in num' + (w ? ' ' + w : ''), step: step, min: min, value: st[f] });
    inp.addEventListener('input', () => { st[f] = num(inp.value); commit(); });
    return inp;
  }
  function sel(st, f, opts, commit, rerender, ctx) {
    const s = h('select', { cls: 'in' });
    opts.forEach(o => s.appendChild(h('option', { value: o[0], text: o[1] })));
    s.value = st[f];
    s.addEventListener('change', () => { st[f] = s.value; commit(); if (rerender) ctx.rerender(); });
    return s;
  }
  const TIMEOUT_OPTS = [['continue', '계속'], ['skip', '이 샘플 건너뜀'], ['stop', '정지'], ['confirm', '확인 요청']];
  const ref = (txt) => h('span', { cls: 'unit', text: txt });
  // 쿨러 · 접촉 · 온도 대기 · 가열 · 냉각 · 반복 · 기록 단계의 값 입력
  function measureStep(val, st, ctx, commit, sp) {
    const T = (t) => ['data-tip', t];
    if (st.type === 'cooler') {
      val.appendChild(sel(st, 'action', [['on', '켜기'], ['off', '끄기']], commit));
      val.appendChild(sel(st, 'target', [['sample', '이 샘플 구역'], ['all', '전부'], ['n', '번호 지정']], commit, true, ctx));
      if (st.target === 'n') {
        const n = h('input', { type: 'number', cls: 'in num', min: 1, max: 10, step: 1, value: st.n || 1 });
        n.addEventListener('input', () => { st.n = num(n.value); commit(); });
        val.appendChild(ref('R')); val.appendChild(n);
      }
    } else if (st.type === 'contact') {
      val.appendChild(ref('시작'));
      val.appendChild(sel(st, 'start_ref', [['work', '작업 높이 +'], ['mm', 'Z 값']], commit));
      val.appendChild(numIn(st, 'start_mm', 0.1, commit, null));
      val.appendChild(ref('스텝')); val.appendChild(numIn(st, 'step_mm', 0.05, commit, 0.01));
      val.appendChild(ref('최대')); val.appendChild(sel(st, 'max_ref', [['work', '작업 높이 +'], ['mm', 'Z 값']], commit));
      val.appendChild(numIn(st, 'max_mm', 0.1, commit, null));
      val.appendChild(ref('누름')); val.appendChild(numIn(st, 'press_mm', 0.05, commit, 0));
      val.appendChild(ref('대기 s')); val.appendChild(numIn(st, 'settle_s', 0.1, commit, 0));
      val.appendChild(ref('못 찾으면'));
      val.appendChild(sel(st, 'on_fail', [['stop', '정지'], ['skip', '이 샘플 건너뜀'], ['confirm', '확인 요청']], commit));
    } else if (st.type === 'wait_temp' || st.type === 'cool') {
      val.appendChild(sel(st, 'cond', [['<=', '≤'], ['>=', '≥']], commit));
      val.appendChild(numIn(st, 'target_c', 1, commit, null)); val.appendChild(ref('°C'));
      val.appendChild(ref('유지')); val.appendChild(numIn(st, 'hold_s', 1, commit, 0)); val.appendChild(ref('s'));
      val.appendChild(ref('최대')); val.appendChild(numIn(st, 'max_s', 5, commit, 1)); val.appendChild(ref('s'));
      val.appendChild(ref('초과 시')); val.appendChild(sel(st, 'on_timeout', TIMEOUT_OPTS, commit));
      val.appendChild(h('span', { cls: 'note', text: st.type === 'cool' ? '히트건을 끄고 대기 · 값은 임시, 장비에서 측정 후 조정' : '값은 임시, 장비에서 측정 후 조정' }));
    } else if (st.type === 'heat') {
      val.appendChild(sel(st, 'end', [['either', '온도 또는 시간 먼저'], ['temp', '목표 온도'], ['time', '시간']], commit, true, ctx));
      if (st.end !== 'time') { val.appendChild(numIn(st, 'target_c', 1, commit, null)); val.appendChild(ref('°C')); }
      if (st.end !== 'temp') { val.appendChild(numIn(st, 'time_s', 1, commit, 1)); val.appendChild(ref('s')); }
      val.appendChild(ref('최대(필수)')); val.appendChild(numIn(st, 'max_s', 5, commit, 1)); val.appendChild(ref('s'));
      val.appendChild(h('span', { cls: 'note', text: '히트건 켬 → 끝 조건 → 끔 · 값은 임시, 장비에서 측정 후 조정' }));
    } else if (st.type === 'repeat') {
      val.appendChild(numIn(st, 'from', 1, commit, 2)); val.appendChild(ref('번 ~'));
      val.appendChild(numIn(st, 'to', 1, commit, 2)); val.appendChild(ref('번 단계를'));
      val.appendChild(numIn(st, 'count', 1, commit, 1)); val.appendChild(ref('번 실행'));
    } else if (st.type === 'log') {
      val.appendChild(sel(st, 'action', [['start', '시작'], ['end', '끝']], commit));
      val.appendChild(h('span', { cls: 'note', text: '온도 · 릴레이 · Z · 측정값을 샘플별 CSV 로 기록' }));
    }
  }

  function zOf(step, ctx) {
    if (step.target === 'top') return 0;
    if (step.target === 'work') {
      const tools = ctx.get('tool.items') || [];
      const t = tools.find(x => x.name === ctx.get('tool.active')) || tools[0] || {};
      return +t.z_work_mm || 0;
    }
    return +step.mm || 0;
  }

  function estimate(r, ctx) {
    const speed = (+ctx.get('motion.speed_pps') || 6000) / 160;
    let s = 0, confirm = false;
    // repeat 를 풀어 실행 순서대로(서버 recipe.expand 와 같은 규칙)
    const flat = [], steps0 = r.steps || [], cnt = {};
    for (let i = 0, g = 0; i < steps0.length && g < 2000; g++) {
      const st = steps0[i];
      if (st.type === 'repeat') {
        cnt[i] = (cnt[i] || 0) + 1;
        if (cnt[i] < (+st.count || 1) && +st.from >= 1 && +st.from <= i) {
          for (let k = +st.from - 1; k < i; k++) delete cnt[k];
          i = +st.from - 1; continue;
        }
        delete cnt[i]; i += 1; continue;
      }
      flat.push(st); i += 1;
    }
    flat.forEach(st => {
      if (st.type === 'wait_temp' || st.type === 'cool' || st.type === 'heat') s += +st.max_s || 0;   // 온도 조건은 최대 대기 기준
      else if (st.type === 'cooler') s += 0.3;
      else if (st.type === 'contact') {
        const w = (tools => (tools.find(x => x.name === ctx.get('tool.active')) || tools[0] || {}).z_work_mm)(ctx.get('tool.items') || []) || 0;
        const a = st.start_ref === 'work' ? w + (+st.start_mm || 0) : +st.start_mm || 0;
        const b = st.max_ref === 'work' ? w + (+st.max_mm || 0) : +st.max_mm || 0;
        s += Math.max(0, (b - a) / Math.max(+st.step_mm || 0.1, 0.01)) * ((+st.settle_s || 0) + 0.15) + 0.5;
      }
      else if (st.type === 'move_sample') s += 2.0;
      else if (st.type === 'z') s += 0.3 + Math.abs(zOf(st, ctx)) / speed;
      else if (st.type === 'wait') s += +st.s || 0;
      else if (st.type === 'relay') s += 0.1;
      else if (st.type === 'measure') s += 0.5;
      else if (st.type === 'confirm') confirm = true;
    });
    return { s: s, confirm: confirm };
  }

  E.recipes = {
    render(el, item, ctx) {
      const list = ctx.get(item.key) || [];
      if (!list.length) list.push({ name: '기본', steps: [{ type: 'move_sample' }] });
      if (!recSel || !list.some(r => r.name === recSel)) recSel = ctx.get('recipe.active') || list[0].name;
      let ri = list.findIndex(r => r.name === recSel);
      if (ri < 0) { ri = 0; recSel = list[0].name; }
      const r = list[ri];
      const p = item.key + '[' + ri + ']';
      const commit = () => ctx.set(item.key, list);

      // 위: 선택 · 새로 · 복제 · 삭제 · 이름 · 활성
      const sel = h('select', { cls: 'in' });
      list.forEach((x, i) => sel.appendChild(h('option', {
        value: x.name, text: x.name + (x.name === ctx.get('recipe.active') ? ' (활성)' : '')
          + (ctx.errs(item.key + '[' + i + ']').length ? ' ⚠' : '') })));
      sel.value = r.name;
      sel.addEventListener('change', () => { recSel = sel.value; ctx.rerender(); });
      const uniq = (base) => {
        let n = 2, name = base;
        while (list.some(x => x.name === name)) { name = base + ' ' + n; n += 1; }
        return name;
      };
      const bNew = h('button', { cls: 'btn sm', text: '새로 만들기', 'data-tip': '샘플 이동 한 단계만 있는 새 레시피를 만듭니다.',
        onclick: () => { const nm = uniq('레시피'); list.push({ name: nm, steps: [{ type: 'move_sample' }] });
                         recSel = nm; commit(); ctx.rerender(); } });
      const bDup = h('button', { cls: 'btn sm', text: '복제', 'data-tip': '이 레시피를 복사합니다.',
        onclick: () => { const nm = uniq(r.name + ' 사본'); list.splice(ri + 1, 0, Object.assign(UI.clone(r), { name: nm }));
                         recSel = nm; commit(); ctx.rerender(); } });
      const bDel = h('button', { cls: 'btn sm', text: '삭제', disabled: list.length <= 1 ? 'disabled' : null,
        'data-tip': '이 레시피를 지웁니다. 마지막 하나는 지울 수 없습니다.',
        onclick: async () => {
          if (!await UI.confirm("레시피 '" + r.name + "' 을 지웁니다. 저장을 눌러야 적용됩니다.", '레시피 삭제')) return;
          list.splice(ri, 1);
          if (ctx.get('recipe.active') === r.name) ctx.set('recipe.active', list[0].name);
          recSel = list[0].name; commit(); ctx.rerender();
        } });
      const nm = h('input', { type: 'text', cls: 'in', value: r.name, maxlength: 30,
                              'data-tip': '레시피 이름입니다(30자 이내, 겹치면 안 됩니다).' });
      nm.addEventListener('change', () => {
        const old = r.name;
        r.name = nm.value.trim();
        if (ctx.get('recipe.active') === old) ctx.set('recipe.active', r.name);
        recSel = r.name; commit(); ctx.rerender();
      });
      markErr(nm, errCell(ctx, p + '.name'));
      const isActive = ctx.get('recipe.active') === r.name;
      const bAct = h('button', { cls: 'btn sm', text: isActive ? '활성 레시피' : '활성으로',
        disabled: isActive ? 'disabled' : null, 'data-tip': '순회가 이 레시피를 쓰게 합니다.',
        onclick: () => { ctx.set('recipe.active', r.name); ctx.rerender(); } });
      el.appendChild(h('div', { cls: 'erow' }, [h('span', { cls: 'note', text: '레시피' }), sel, bNew, bDup, bDel,
        h('span', { cls: 'note', text: '이름' }), nm, bAct]));

      // 아래: 단계 표 + 요약
      const tb = h('tbody');
      const steps = r.steps || (r.steps = []);
      const relays = ctx.get('relay.items') || [];
      steps.forEach((st, j) => {
        const sp = p + '.steps[' + j + ']';
        const fixed = j === 0 && st.type === 'move_sample';
        const val = h('div', { cls: 'cellrow' });
        if (st.type === 'move_sample') {
          val.appendChild(h('span', { cls: 'note', text: '샘플 위치 − 활성 공구 오프셋 · Z 먼저 맨 위' }));
        } else if (st.type === 'z') {
          const tg = h('select', { cls: 'in' });
          [['work', '작업 높이'], ['top', '맨 위'], ['mm', '숫자']].forEach(o =>
            tg.appendChild(h('option', { value: o[0], text: o[1] })));
          tg.value = st.target || 'work';
          const mm = h('input', { type: 'number', cls: 'in num', step: 0.1, min: 0,
                                  value: st.mm != null ? st.mm : 0, hidden: st.target === 'mm' ? null : 'hidden' });
          const info = h('span', { cls: 'note', text: st.target === 'mm' ? 'mm' : '= ' + zOf(st, ctx) + ' mm' });
          tg.addEventListener('change', () => {
            st.target = tg.value;
            if (st.target === 'mm' && st.mm == null) st.mm = 0;
            commit(); ctx.rerender();
          });
          mm.addEventListener('input', () => { st.mm = num(mm.value); commit(); });
          val.appendChild(tg); val.appendChild(mm); val.appendChild(info);
        } else if (st.type === 'relay') {
          const n = h('select', { cls: 'in' });
          relays.forEach((x, k) => n.appendChild(h('option', { value: k + 1,
            text: 'R' + (k + 1) + ' ' + (x.name || '') + (x.enabled ? '' : ' (사용 안 함)') })));
          n.value = String(st.n || 1);
          const on = h('select', { cls: 'in' });
          on.appendChild(h('option', { value: '1', text: '켜기' }));
          on.appendChild(h('option', { value: '0', text: '끄기' }));
          on.value = st.on ? '1' : '0';
          n.addEventListener('change', () => { st.n = +n.value; commit(); });
          on.addEventListener('change', () => { st.on = on.value === '1'; commit(); });
          val.appendChild(n); val.appendChild(on);
        } else if (st.type === 'wait') {
          const s = h('input', { type: 'number', cls: 'in num', step: 0.5, min: 0, value: st.s });
          s.addEventListener('input', () => { st.s = num(s.value); commit(); });
          val.appendChild(s); val.appendChild(h('span', { cls: 'unit', text: 's' }));
        } else if (st.type === 'measure') {
          val.appendChild(h('span', { cls: 'note', text: '계측기 호출 (지금은 더미)' }));
        } else if (st.type === 'confirm') {
          val.appendChild(h('span', { cls: 'note', text: '[다음] 을 누를 때까지 대기' }));
        } else {
          measureStep(val, st, ctx, commit, sp);
        }
        const move = (d) => () => {
          const k = j + d;
          if (k < 1 || k >= steps.length) return;
          const t = steps[j]; steps[j] = steps[k]; steps[k] = t;
          commit(); ctx.rerender();
        };
        const up = h('button', { cls: 'btn sm', text: '↑', 'aria-label': '위로',
          disabled: (fixed || j <= 1) ? 'disabled' : null, onclick: move(-1) });
        const dn = h('button', { cls: 'btn sm', text: '↓', 'aria-label': '아래로',
          disabled: (fixed || j >= steps.length - 1) ? 'disabled' : null, onclick: move(1) });
        const del = h('button', { cls: 'btn sm', text: '✕', 'aria-label': '삭제',
          disabled: fixed ? 'disabled' : null,
          onclick: () => { steps.splice(j, 1); commit(); ctx.rerender(); } });
        const e = errCell(ctx, sp);
        tb.appendChild(h('tr', { cls: e ? 'bad' : '' }, [
          h('td', { cls: 'mono', text: String(j + 1) }),
          h('td', { cls: 'ind' + indentOf(steps, j), text: STEP[st.type] || st.type }),
          h('td', {}, [val]),
          h('td', {}, [h('div', { cls: 'cellrow' }, [up, dn, del])]),
          h('td', { cls: 'err', text: e }),
        ]));
      });
      const add = h('select', { cls: 'in', 'data-tip': '고른 종류의 단계를 맨 끝에 더합니다.' });
      add.appendChild(h('option', { value: '', text: '단계 추가 ▾' }));
      Object.keys(NEW_STEP).forEach(k => add.appendChild(h('option', { value: k, text: STEP[k] })));
      add.addEventListener('change', () => {
        if (!add.value) return;
        steps.push(NEW_STEP[add.value]());
        commit(); ctx.rerender();
      });
      const table = h('div', { cls: 'recmain' }, [
        h('table', { cls: 'etable steps' }, [
          h('thead', {}, [h('tr', {}, [h('th', { text: '#' }), h('th', { text: '종류' }),
            h('th', { text: '값' }), h('th', { text: '순서' }), h('th', { text: '' })])]), tb]),
        h('div', { cls: 'erow' }, [add])]);

      const est = estimate(r, ctx);
      const used = [];
      steps.forEach(st => { if (st.type === 'relay' && used.indexOf(st.n) < 0) used.push(st.n); });
      const hasTemp = steps.some(x => x.type === 'heat' || x.type === 'wait_temp' || x.type === 'cool');
      const warns = (ctx.warnings() || []).filter(w => w.indexOf("'" + r.name + "'") >= 0);
      const sum = h('div', { cls: 'recsum' }, [
        h('div', { cls: 'k', text: '요약' }),
        h('div', { text: '단계 ' + steps.length + '개' }),
        h('div', { text: '샘플당 최대 약 ' + UI.dur(est.s) + (est.confirm ? ' + 확인' : '') }),
        h('div', { cls: 'note', text: hasTemp ? '온도 조건은 최대 대기 기준' : '' }),
        h('div', { text: '릴레이 ' + (used.length ? used.map(n => 'R' + n).join(', ') : '없음') }),
        h('div', { text: 'Z 단계 ' + (steps.some(x => x.type === 'z') ? '있음 (Z 원점 필요)' : '없음') }),
      ].concat(warns.map(w => h('div', { cls: 'warnline', text: w }))));
      el.appendChild(h('div', { cls: 'recwrap' }, [table, sum]));
    },
  };
})();
