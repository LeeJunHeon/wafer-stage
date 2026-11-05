/* plate.js — 보정 창의 "감지 영역 · 판 보정" 탭(고정 보정).

   판 마커 없이 쓰는 방식이다. 촬영본 위에서
     ① 감지 영역: 네 모서리를 차례로 클릭 → 사각형 미리보기 → 꼭짓점 끌기 → [적용]
     ② 판 보정: 점마다 기준 포인터를 판 위 표시점에 맞춰 [위치 기록](스테이지 XY) +
        촬영본에서 같은 점 클릭(u, v) → 4점 이상 → 잔차 · RMS 확인 → [적용]
   좌표는 전부 원본 촬영본 픽셀 기준이다(화면 표시 배율은 클릭 때 환산한다).
   서버가 상태의 주인이다: 이 탭의 초안(P)은 [적용] 전까지 화면에만 있다. */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$, h = UI.h;
  const NS = 'http://www.w3.org/2000/svg';
  const K = () => UI.calibKit;

  const P = { mode: 'area', zoom: 1, quad: [], quadDirty: false, excl: [], exclCur: [], exclDirty: false, pts: [], sel: null, pend: null,
              recFor: null, fit: null, src: 'auto', inited: false, created: null, v: 0, sig: '', hint: '',
              last: null, fitTimer: null };

  function S() { return UI.state || {}; }
  function plateState() { return S().plate || {}; }
  function g(k) { return k.split('.').reduce((o, p) => (o || {})[p], S().settings || {}); }
  function bump() { P.v += 1; UI.renderPlate($('calPlate'), true); }

  function frame() {
    const pl = plateState();
    if (P.src === 'capture') return S().frame || null;
    if (P.src === 'shot') return pl.shot || null;
    return pl.shot || S().frame || null;
  }
  function frameName(fr) {
    const pl = plateState();
    return (pl.shot && fr && fr.id === pl.shot.id) ? '단발 촬영' : '최근 촬영본';
  }

  // 저장된 값으로 초안을 채운다(탭을 처음 열 때와 [적용] 뒤).
  function loadSaved() {
    const d = plateState().data;
    P.quad = d && d.area ? d.area.map(p => p.slice()) : [];
    P.pts = d && d.points ? d.points.map(p => ({ x: p.x, y: p.y, u: p.u, v: p.v })) : [];
    P.excl = d && d.exclude ? d.exclude.map(q => q.map(p => p.slice())) : [];
    P.exclCur = []; P.exclDirty = false;
    P.quadDirty = false; P.sel = null; P.pend = null; P.recFor = null; P.fit = null;
    P.inited = true;
    requestFit();
  }

  function complete() { return P.pts.filter(p => p.x != null && p.y != null && p.u != null); }

  function requestFit() {
    clearTimeout(P.fitTimer);
    const fr = frame(), pts = complete();
    if (pts.length < 4 || !fr) { P.fit = null; return; }
    P.fitTimer = setTimeout(() => UI.send({ cmd: 'plate_calib_fit', points: pts, size: [fr.w, fr.h] }), 120);
  }

  function sizeOf(fr) { return [fr.w, fr.h]; }
  function round1(v) { return Math.round(v * 10) / 10; }

  // ---------------- 화면 ----------------
  UI.renderPlate = function (box, force) {
    const pl = plateState();
    // 저장된 값이 바뀌면(처음 · [적용] 뒤) 초안을 그 값으로 다시 채운다.
    const cr = pl.data ? pl.data.created : null;
    if (pl.mode && (!P.inited || cr !== P.created)) { P.created = cr; loadSaved(); }
    const fr = frame();
    const st = S().stage || {};
    const sig = JSON.stringify([P.v, pl.mode, pl.valid, pl.data && pl.data.created, fr && fr.id,
      st.x_mm, st.y_mm, st.connected, (S().job || {}).running]);
    if (!force && sig === P.sig && box.firstChild) return;
    P.sig = sig;
    const view0 = box.querySelector('.plateview');
    const sx = view0 ? view0.scrollLeft : 0, sy = view0 ? view0.scrollTop : 0;
    box.textContent = '';
    if (pl.mode !== 'fixed') {
      box.appendChild(h('div', { cls: 'note', text: '고정 보정 방식에서만 씁니다 · 설정의 검출 탭 → 보정 방식 → 고정' }));
      return;
    }
    const kit = K();
    const grid = h('div', { cls: 'plategrid' });
    const left = h('div', { cls: 'platecards' });
    const side = h('div', { cls: 'plateside' });
    grid.appendChild(left); grid.appendChild(side);
    box.appendChild(grid);

    const cur = pl.data || {};
    const nq = P.quad.length;
    const areaSaved = !!(cur.area && cur.area.length === 4) && !P.quadDirty;
    const pts = complete();
    const fitOk = !!P.fit && P.fit.ok && pts.length >= 4;
    const sts = kit.states([!!fr, areaSaved, pts.length >= 4, !!pl.valid && !!cur.has_h && fitOk && P.pts.length === cur.n]);

    // 1. 촬영본
    const mism = fr && (fr.w !== g('width') || fr.h !== g('height'));
    left.appendChild(kit.card(1, '촬영본', sts[0], [
      h('div', { cls: 'row' }, [
        kit.btn('단발 촬영', () => UI.send({ cmd: 'plate_shot' }),
          { sm: true, primary: !fr, disabled: kit.busy(), tip: '카메라로 한 장 찍어 이 탭에 띄웁니다(브라케팅 · 검출 없음). 촬영본은 바뀌지 않습니다.' }),
        kit.btn('최근 촬영본', () => { P.src = 'capture'; bump(); },
          { sm: true, disabled: !S().frame, tip: '마지막으로 촬영 · 검출한 사진을 씁니다.' })]),
      h('div', { cls: 'mono', text: fr ? (fr.w + '×' + fr.h + ' px · ' + frameName(fr)) : '촬영본 없음 · 단발 촬영' }),
      mism ? h('div', { cls: 'warnline', text: '설정 해상도 ' + g('width') + '×' + g('height') + ' 와 다름 · 이 사진 기준으로 저장되며 설정과 다르면 무효' }) : null]));

    // 2. 감지 영역
    left.appendChild(kit.card(2, '감지 영역', sts[1], [
      h('div', { cls: 'note', text: '사진에서 판에 새겨진 사각형의 네 모서리를 차례로 클릭합니다. 점은 끌어서 고칩니다.' }),
      h('div', { cls: 'row' }, [
        h('span', { cls: 'mono nowrap', text: nq + '/4' + (P.quadDirty ? ' · 미적용' : (areaSaved ? ' · 저장됨' : '')) }),
        kit.btn('지우기', () => { P.quad = []; P.quadDirty = true; P.mode = 'area'; bump(); },
          { sm: true, disabled: !nq, tip: '모서리를 모두 지우고 처음부터 찍습니다.' }),
        kit.btn('적용', () => UI.send({ cmd: 'plate_area_apply', quad: P.quad, size: sizeOf(fr) }),
          { sm: true, primary: nq === 4 && P.quadDirty, disabled: nq !== 4 || !fr,
            tip: '이 사각형을 감지 영역으로 저장합니다. 검출은 이 사각형 안만 봅니다.' })])]));

    // 제외 영역 · 빈 판 기준
    const ref = pl.ref || {};
    const refTxt = !ref.exists ? '빈 판 기준 없음'
      : (ref.valid ? '빈 판 기준 유효 · ' + (ref.time || '') + (ref.use ? ' · 사용 중' : ' · 설정에서 꺼짐')
        : '빈 판 기준 무효 · ' + (ref.reason || ''));
    left.appendChild(kit.card(5, '제외 영역 · 빈 판 기준', null, [
      h('div', { cls: 'note', text: '헤드 · 기판처럼 감지 영역 안에 걸리는 물체를 가립니다. 사진에서 꼭짓점을 차례로 클릭하고 [영역 닫기].' }),
      h('div', { cls: 'row' }, [
        h('span', { cls: 'mono nowrap', text: '저장 ' + ((cur.exclude || []).length) + '개 · 초안 ' + P.excl.length + '개' + (P.exclCur.length ? ' · 그리는 중 ' + P.exclCur.length + '점' : '') + (P.exclDirty ? ' · 미적용' : '') })]),
      h('div', { cls: 'row' }, [
        kit.btn('영역 닫기', () => { if (P.exclCur.length >= 3) { P.excl.push(P.exclCur); P.exclCur = []; P.exclDirty = true; bump(); } },
          { sm: true, disabled: P.exclCur.length < 3, tip: '그리던 점들을 다각형 하나로 닫아 제외 영역에 넣습니다.' }),
        kit.btn('점 취소', () => { P.exclCur.pop(); bump(); }, { sm: true, disabled: !P.exclCur.length, tip: '마지막으로 찍은 점을 지웁니다.' }),
        kit.btn('마지막 영역 삭제', () => { P.excl.pop(); P.exclDirty = true; bump(); }, { sm: true, disabled: !P.excl.length, tip: '초안의 마지막 제외 영역을 지웁니다.' }),
        kit.btn('적용', () => UI.send({ cmd: 'plate_exclude_apply', polys: P.excl, size: sizeOf(fr) }),
          { sm: true, primary: P.exclDirty, disabled: !fr || !P.exclDirty, tip: '제외 영역을 저장합니다. 다음 촬영부터 검출 전에 가려집니다.' })]),
      h('div', { cls: ref.exists && ref.valid ? 'okline' : 'note', text: refTxt }),
      h('div', { cls: 'row' }, [
        kit.btn('빈 판 기준 촬영', () => UI.send({ cmd: 'plate_ref_shot' }),
          { sm: true, disabled: kit.busy() || !areaSaved, tip: '샘플을 치운 빈 판을 한 장 찍어 기준 사진으로 저장합니다(노출 · 화이트밸런스도 기록). 설정의 빈 판 기준 사용을 켜야 쓰입니다.' })])]));

    // 3. 판 보정점
    const rec = P.sel != null && P.pts[P.sel];
    left.appendChild(kit.card(3, '판 보정점', sts[2], [
      h('div', { cls: 'note', text: '① 기준 포인터를 판 위 표시점(모서리 · 중앙)에 맞춰 [위치 기록] ② 사진에서 같은 점 클릭. 점 4개 이상(권장 5~9).' }),
      h('div', { cls: 'row' }, [kit.btn('수동 이동', () => $('btnJog').click(), { sm: true }),
        h('span', { cls: 'mono', text: '현재 X ' + kit.f2(st.x_mm) + ' · Y ' + kit.f2(st.y_mm) })]),
      h('div', { cls: 'row' }, [
        kit.btn(rec ? '위치 기록 → #' + (P.sel + 1) : '위치 기록', () => {
          P.recFor = P.sel; UI.send({ cmd: 'plate_pos' });
        }, { sm: true, primary: !P.pend && !rec, disabled: !kit.homed() || kit.busy(),
             tip: '지금 스테이지 위치(기준 포인터)를 기록합니다. 표에서 점을 골라 두면 그 점에, 아니면 새 점에 씁니다.' }),
        kit.btn('꼭짓점을 보정점으로', () => {
          if (P.quad.length !== 4) return;
          P.quad.forEach(q => P.pts.push({ x: null, y: null, u: q[0], v: q[1] }));
          P.sel = P.pts.length - 4; P.mode = 'pair'; requestFit(); bump();
        }, { sm: true, disabled: nq !== 4,
             tip: '감지 영역 꼭짓점 네 개를 보정점 목록에 넣습니다. 각 점에 포인터를 맞춰 [위치 기록]만 하면 됩니다.' })]),
      h('div', { cls: 'mono', text: P.pend ? '기록 대기  X ' + kit.f2(P.pend.x) + ' · Y ' + kit.f2(P.pend.y) + ' → 사진에서 클릭' : '' })]));

    // 4. 결과 · 적용
    const lim = g('calib.residual_limit_mm') || 1.0;
    const resOf = {};
    if (fitOk) pts.forEach((p, i) => { resOf[P.pts.indexOf(p)] = P.fit.res[i]; });
    const tb = h('tbody');
    P.pts.forEach((p, i) => {
      const r = resOf[i];
      tb.appendChild(h('tr', { cls: (P.sel === i ? 'sel ' : '') + (r != null && r > lim ? 'bad' : ''),
        onclick: (ev) => { if (ev.target.tagName === 'BUTTON') return; P.sel = (P.sel === i ? null : i); P.mode = 'pair'; bump(); } }, [
        h('td', { text: String(i + 1) }),
        h('td', { text: p.x == null ? UI.EMPTY : (+p.x).toFixed(2) }), h('td', { text: p.y == null ? UI.EMPTY : (+p.y).toFixed(2) }),
        h('td', { text: p.u == null ? UI.EMPTY : (+p.u).toFixed(1) }), h('td', { text: p.v == null ? UI.EMPTY : (+p.v).toFixed(1) }),
        h('td', { text: r == null ? UI.EMPTY : (+r).toFixed(3) }),
        h('td', {}, [kit.btn('삭제', () => { P.pts.splice(i, 1); if (P.sel === i) P.sel = null; else if (P.sel > i) P.sel -= 1; requestFit(); bump(); },
          { sm: true, tip: '이 점을 목록에서 뺍니다.' })])]));
    });
    const kids = [];
    kids.push(h('div', { cls: 'platetbl' }, [h('table', { cls: 'etable' }, [
      h('thead', {}, [h('tr', {}, ['#', 'X mm', 'Y mm', 'u px', 'v px', '잔차', ''].map(t => h('th', { text: t })))]), tb])]));
    if (pts.length < 4) kids.push(h('div', { cls: 'note', text: '완성된 점 ' + pts.length + '개 · 4개 이상 필요' }));
    else if (fitOk) {
      const over = P.fit.over || [];
      kids.push(h('div', { cls: over.length ? 'warnline' : 'okline',
        text: 'RMS ' + (+P.fit.rms_mm).toFixed(3) + ' mm · 최대 ' + (+P.fit.max_mm).toFixed(3) + ' mm'
          + (over.length ? ' · 한계 ' + lim + ' mm 초과 ' + over.length + '점' : '') }));
    } else kids.push(h('div', { cls: 'note', text: '계산 중 · 점이 한 직선 위이면 계산되지 않습니다' }));
    kids.push(h('div', { cls: 'row' }, [
      kit.btn('적용', () => UI.send({ cmd: 'plate_calib_apply', points: pts, size: sizeOf(fr) }),
        { sm: true, primary: fitOk, disabled: pts.length < 4 || !fr,
          tip: '이 판 보정을 저장합니다(카메라 번호 · 해상도도 함께 기록). 4점 미만이면 저장하지 않습니다.' })]));
    left.appendChild(kit.card(4, '결과 · 적용', sts[3], kids));

    // 오른쪽: 도구줄 + 촬영본 뷰어
    side.appendChild(buildBar(fr));
    side.appendChild(buildView(fr, sx, sy));
    side.appendChild(h('div', { cls: 'note', text: P.hint || hintText(fr) }));
  };

  function hintText(fr) {
    if (!fr) return '촬영본이 없습니다 · 단발 촬영 또는 촬영 후 열기';
    if (P.mode === 'excl') return '제외할 물체의 꼭짓점을 차례로 클릭 · 3점 이상이면 [영역 닫기]';
    if (P.mode === 'area') return P.quad.length < 4 ? '모서리 ' + (P.quad.length + 1) + '번째를 클릭' : '모서리 4개 · 끌어서 수정 후 [적용]';
    if (P.sel != null) return '점 #' + (P.sel + 1) + ' 선택 · 사진을 클릭하면 이 점의 위치가 바뀝니다(미세 이동은 화살표)';
    return P.pend ? '같은 점을 사진에서 클릭' : '[위치 기록] 뒤 사진에서 같은 점 클릭';
  }

  function buildBar(fr) {
    const bar = h('div', { cls: 'platebar' });
    const seg = h('div', { cls: 'seg segsm' });
    [['area', '감지 영역 클릭'], ['pair', '판 보정점 클릭'], ['excl', '제외 영역 클릭']].forEach(o => {
      const inp = h('input', { type: 'radio', name: 'platemode', value: o[0] });
      inp.checked = P.mode === o[0];
      inp.addEventListener('change', () => { P.mode = o[0]; P.hint = ''; bump(); });
      seg.appendChild(h('label', {}, [inp, document.createTextNode(o[1])]));
    });
    bar.appendChild(seg);
    const zs = h('div', { cls: 'seg segsm' });
    [1, 2, 4, 8].forEach(z => {
      const inp = h('input', { type: 'radio', name: 'platezoom', value: String(z) });
      inp.checked = P.zoom === z;
      inp.addEventListener('change', () => { P.zoom = z; bump(); centerOnLast(); });
      zs.appendChild(h('label', { 'data-tip': '촬영본을 ' + z + '배로 키웁니다. 점을 정확히 찍을 때 씁니다.' }, [inp, document.createTextNode(z + '×')]));
    });
    bar.appendChild(zs);
    const step = 0.5;
    const nudge = (du, dv) => () => {
      const p = P.pts[P.sel];
      if (!p || p.u == null) return;
      p.u = Math.round((p.u + du) * 10) / 10; p.v = Math.round((p.v + dv) * 10) / 10;
      requestFit(); bump();
    };
    const can = P.sel != null && P.pts[P.sel] && P.pts[P.sel].u != null;
    const pad = h('span', { cls: 'nudge', 'data-tip': '선택한 보정점을 사진에서 0.5 px 씩 옮깁니다.' });
    [['◀', [-step, 0]], ['▲', [0, -step]], ['▼', [0, step]], ['▶', [step, 0]]].forEach(a =>
      pad.appendChild(h('button', { cls: 'btn sm', text: a[0], disabled: can ? null : 'disabled',
                                    onclick: nudge(a[1][0], a[1][1]) })));
    bar.appendChild(pad);
    return bar;
  }

  function el(tag, attrs) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  function buildView(fr, sx, sy) {
    const view = h('div', { cls: 'plateview' });
    if (!fr) return view;
    const inner = h('div', { cls: 'plateinner' });
    inner.style.width = (100 * P.zoom) + '%';
    const img = h('img', { src: fr.url, alt: '촬영본' });
    const svg = el('svg', { viewBox: '0 0 ' + fr.w + ' ' + fr.h, preserveAspectRatio: 'none' });
    inner.appendChild(img); inner.appendChild(svg);
    view.appendChild(inner);
    const draw = () => drawOverlay(svg, fr, inner.clientWidth / fr.w);
    img.addEventListener('load', draw);
    requestAnimationFrame(() => { draw(); view.scrollLeft = sx; view.scrollTop = sy; });
    // 클릭 → 원본 px(표시 배율 환산: 화면 폭 / 원본 폭)
    const toPx = (ev) => {
      const r = svg.getBoundingClientRect();
      return [(ev.clientX - r.left) / r.width * fr.w, (ev.clientY - r.top) / r.height * fr.h];
    };
    let drag = null, moved = false;
    svg.addEventListener('pointerdown', (ev) => {
      const t = ev.target;
      if (t.dataset && t.dataset.q != null) {
        drag = +t.dataset.q; moved = false; svg.setPointerCapture(ev.pointerId); ev.preventDefault();
      }
    });
    svg.addEventListener('pointermove', (ev) => {
      if (drag == null) return;
      moved = true;
      const p = toPx(ev);
      P.quad[drag] = [round1(Math.max(0, Math.min(fr.w - 1, p[0]))), round1(Math.max(0, Math.min(fr.h - 1, p[1])))];
      P.quadDirty = true;
      draw();
    });
    svg.addEventListener('pointerup', () => { if (drag != null) { drag = null; if (moved) bump(); } });
    svg.addEventListener('click', (ev) => {
      if (moved) { moved = false; return; }
      const p = toPx(ev);
      if (!(p[0] >= 0 && p[1] >= 0 && p[0] <= fr.w && p[1] <= fr.h)) return;
      const u = round1(p[0]), v = round1(p[1]);
      P.last = [u, v]; P.hint = '';
      if (P.mode === 'excl') { P.exclCur.push([u, v]); bump(); return; }
      if (P.mode === 'area') {
        if (P.quad.length >= 4) { P.hint = '모서리는 4개 · 점을 끌어 고치거나 [지우기]'; bump(); return; }
        P.quad.push([u, v]); P.quadDirty = true; bump(); return;
      }
      if (P.sel != null && P.pts[P.sel]) { P.pts[P.sel].u = u; P.pts[P.sel].v = v; requestFit(); bump(); return; }
      if (!P.pend) { P.hint = '먼저 포인터를 점에 맞추고 [위치 기록]'; bump(); return; }
      P.pts.push({ x: P.pend.x, y: P.pend.y, u: u, v: v });
      P.pend = null; P.sel = P.pts.length - 1; requestFit(); bump();
    });
    return view;
  }

  function centerOnLast() {
    if (!P.last) return;
    requestAnimationFrame(() => {
      const v = document.querySelector('#calPlate .plateview'), i = document.querySelector('#calPlate .plateinner');
      const fr = frame();
      if (!v || !i || !fr) return;
      v.scrollLeft = P.last[0] / fr.w * i.clientWidth - v.clientWidth / 2;
      v.scrollTop = P.last[1] / fr.h * i.clientHeight - v.clientHeight / 2;
    });
  }

  function drawOverlay(svg, fr, scale) {
    svg.textContent = '';
    const k = 1 / Math.max(scale, 1e-6);            // 화면 1 px = 원본 k px
    const fs = 12 * k;
    if (P.quad.length >= 2) {
      const pts = P.quad.map(q => q.join(',')).join(' ');
      svg.appendChild(el(P.quad.length === 4 ? 'polygon' : 'polyline', { class: 'pq', points: pts, fill: P.quad.length === 4 ? '' : 'none' }));
    }
    P.quad.forEach((q, i) => {
      svg.appendChild(el('circle', { class: 'pv', cx: q[0], cy: q[1], r: 6 * k, 'data-q': i }));
      const t = el('text', { x: q[0] + 9 * k, y: q[1] - 9 * k, 'font-size': fs }); t.textContent = String(i + 1);
      svg.appendChild(t);
    });
    P.excl.forEach(poly => svg.appendChild(el('polygon', { class: 'px', points: poly.map(q => q.join(',')).join(' ') })));
    if (P.exclCur.length >= 2) svg.appendChild(el('polyline', { class: 'pxc', points: P.exclCur.map(q => q.join(',')).join(' ') }));
    P.exclCur.forEach(q => svg.appendChild(el('circle', { class: 'pxv', cx: q[0], cy: q[1], r: 4 * k })));
    const lim = g('calib.residual_limit_mm') || 1.0;
    const comp = complete();
    P.pts.forEach((p, i) => {
      if (p.u == null) return;
      const ci = comp.indexOf(p);
      const bad = P.fit && P.fit.ok && ci >= 0 && P.fit.res[ci] > lim;
      const c = 'pp' + (P.sel === i ? ' sel' : '') + (bad ? ' bad' : '');
      const a = 9 * k;
      svg.appendChild(el('line', { class: c, x1: p.u - a, y1: p.v, x2: p.u + a, y2: p.v }));
      svg.appendChild(el('line', { class: c, x1: p.u, y1: p.v - a, x2: p.u, y2: p.v + a }));
      const t = el('text', { x: p.u + 11 * k, y: p.v + 14 * k, 'font-size': fs }); t.textContent = 'P' + (i + 1);
      svg.appendChild(t);
    });
  }

  // ---------------- 서버 응답 ----------------
  UI.onPlateAck = function (msg) {
    const of = msg.of;
    if (of === 'plate_shot') {
      if (msg.ok) { P.src = 'shot'; P.sig = ''; }
      else UI.alert((msg.needs_confirm || []).join('\n') || '단발 촬영 실패', '단발 촬영');
    } else if (of === 'plate_pos') {
      if (!msg.ok) { UI.alert((msg.needs_confirm || []).join('\n') || '위치를 기록할 수 없습니다', '위치 기록'); }
      else if (P.recFor != null && P.pts[P.recFor]) { P.pts[P.recFor].x = msg.x; P.pts[P.recFor].y = msg.y; P.pend = null; requestFit(); }
      else { P.pend = { x: msg.x, y: msg.y }; P.sel = null; P.mode = 'pair'; }
      P.recFor = null;
    } else if (of === 'plate_calib_fit') {
      P.fit = msg.ok ? msg : null;
    } else if (of === 'plate_calib_apply') {
      if (msg.ok) { P.sig = ''; UI.alert('판 보정 저장 · RMS ' + (+msg.rms_mm).toFixed(3) + ' mm · 최대 ' + (+msg.max_mm).toFixed(3) + ' mm' + ((msg.over || []).length ? '\n한계 초과 ' + msg.over.length + '점' : '') + '\n다시 촬영하면 반영됩니다.', '판 보정'); }
      else UI.alert((msg.needs_confirm || []).join('\n') || '저장하지 않았습니다', '판 보정');
    } else if (of === 'plate_exclude_apply') {
      if (msg.ok) { P.sig = ''; P.exclDirty = false; }
      else UI.alert((msg.needs_confirm || []).join('\n') || '저장하지 않았습니다', '제외 영역');
    } else if (of === 'plate_ref_shot') {
      if (!msg.ok) UI.alert((msg.needs_confirm || []).join('\n') || '촬영하지 못했습니다', '빈 판 기준');
    } else if (of === 'plate_area_apply') {
      if (msg.ok) { P.sig = ''; }
      else UI.alert((msg.needs_confirm || []).join('\n') || '저장하지 않았습니다', '감지 영역');
    }
    P.v += 1;
    UI.renderPlate($('calPlate'), true);
  };
})();
