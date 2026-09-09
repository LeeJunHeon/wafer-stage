/* camera.js — 프레임 이미지 + SVG 오버레이(마커·감지영역·웨이퍼·샘플·포인터).
   서버는 좌표만 준다. 그림은 여기서 그린다(annotated.jpg 는 파일 기록용). */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$;
  const NS = 'http://www.w3.org/2000/svg';

  let lastFrameId = null;

  function el(tag, attrs) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  UI.applyCamera = function (s) {
    // ---- 사진 ----
    const img = $('frame');
    if (s.frame && s.frame.url) {
      if (s.frame.id !== lastFrameId) {
        lastFrameId = s.frame.id;
        img.setAttribute('href', s.frame.url);
        $('cam').setAttribute('viewBox', '0 0 ' + s.frame.w + ' ' + s.frame.h);
        img.setAttribute('width', s.frame.w);
        img.setAttribute('height', s.frame.h);
      }
    } else if (!s.frame) {
      img.removeAttribute('href');
      lastFrameId = null;
    }
    $('capturing').hidden = !(s.camera && s.camera.capturing);

    // ---- 마커 ----
    const gm = $('ovMarkers');
    gm.textContent = '';
    const mk = s.markers || {};
    Object.keys(mk).forEach(id => {
      const q = mk[id] || [];
      if (q.length < 4) return;
      gm.appendChild(el('polygon', {
        class: 'ov-marker', points: q.map(p => p.join(',')).join(' ')
      }));
      const cx = q.reduce((a, p) => a + p[0], 0) / q.length;
      const cy = q.reduce((a, p) => a + p[1], 0) / q.length;
      const t = el('text', { class: 'ov-marker-label', x: cx + 6, y: cy - 6 });
      t.textContent = 'id' + id;
      gm.appendChild(t);
    });

    // ---- 감지영역 ----
    const r = (s.sensing && s.sensing.rect) || null;
    const rect = $('ovRect');
    if (r) {
      rect.setAttribute('x', r[0]); rect.setAttribute('y', r[1]);
      rect.setAttribute('width', r[2] - r[0]); rect.setAttribute('height', r[3] - r[1]);
    } else {
      rect.setAttribute('width', 0); rect.setAttribute('height', 0);
    }

    // ---- 웨이퍼 ----
    const w = s.wafer;
    const cw = $('ovWafer');
    if (w && w.found) {
      cw.setAttribute('cx', w.cx); cw.setAttribute('cy', w.cy); cw.setAttribute('r', w.r_px);
    } else {
      cw.setAttribute('r', 0);
    }

    // ---- 샘플 ----
    const gs = $('ovSamples');
    gs.textContent = '';
    (s.samples || []).forEach(sm => {
      const cls = ['ov-sample'];
      if (sm.status === 'done') cls.push('done');
      if (sm.status === 'skip' || !sm.on) cls.push('skip');
      if (sm.status === 'moving' || sm.status === 'measuring') cls.push('target');
      if (UI.selected === sm.no) cls.push('selected');
      const pts = (sm.verts || []).map(p => p.join(',')).join(' ');
      if (pts) gs.appendChild(el('polygon', { class: cls.join(' '), points: pts }));
      const t = el('text', { class: 'ov-num', x: sm.u + 8, y: sm.v - 6 });
      t.textContent = sm.no + (sm.edge_completed ? 'E' : '');
      gs.appendChild(t);
      // 클릭 판정용(다각형이 얇아도 집히도록 원을 덮는다)
      const hit = el('circle', { class: 'ov-hit', cx: sm.u, cy: sm.v, r: 16 });
      hit.addEventListener('click', () => UI.select(sm.no));
      gs.appendChild(hit);
    });

    // ---- 포인터(스테이지 현재 위치) ----
    const st = s.stage || {};
    const gp = $('ovPointer');
    if (st.u != null && st.v != null) {
      gp.hidden = false;
      gp.setAttribute('transform', 'translate(' + st.u + ',' + st.v + ')');
      $('pointerLabel').textContent = 'X ' + UI.fmt(st.x_mm) + ' Y ' + UI.fmt(st.y_mm)
        + (st.moving ? ' 이동중' : '');
    } else {
      gp.hidden = true;
    }

    // ---- 정보 4칸 ----
    const c = s.calib;
    $('infoCal').textContent = c
      ? (c.used_ids.length + '개 마커 / ' + c.corners + '점 · ' + c.transform
         + ' · 잔차 ' + c.corner_max_mm.toFixed(2) + ' mm'
         + (c.missing_ids.length ? ' · id ' + c.missing_ids.join(',') + ' 가려짐' : ''))
      : UI.EMPTY;
    $('infoRect').textContent = r
      ? ('(' + r[0] + ',' + r[1] + ')-(' + r[2] + ',' + r[3] + ') · '
         + (r[2] - r[0]) + '×' + (r[3] - r[1]) + ' px')
      : UI.EMPTY;
    $('infoWafer').textContent = (w && w.found && w.center_mm)
      ? ('중심 X' + w.center_mm[0].toFixed(1) + ' Y' + w.center_mm[1].toFixed(1)
         + ' · r ' + w.r_px.toFixed(0) + 'px')
      : UI.EMPTY;
    const list = s.samples || [];
    const tri = list.filter(x => x.shape === 'triangle').length;
    const on = list.filter(x => x.on).length;
    $('infoSamples').textContent = list.length
      ? (list.length + '개 (삼각 ' + tri + ') · 대상 ' + on) : UI.EMPTY;
  };

  // 카메라 도구 버튼
  $('btnCapture').onclick = () => UI.send({ cmd: 'capture' });
  $('btnPark').onclick = () => UI.send({ cmd: 'park' });
  $('btnHome').onclick = async () => {
    const ok = await UI.confirm(
      '원점을 잡습니다. 축을 끝까지 밀어 하드스톱에 닿습니다(드르륵 소리는 정상).\n'
      + '프로브·웨이퍼가 경로에 없는지 확인하세요.', '원점 잡기');
    if (ok) UI.send({ cmd: 'stage_home', axis: 'xy' });
  };
})();
