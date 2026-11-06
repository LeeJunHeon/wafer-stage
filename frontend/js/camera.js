/* camera.js — 프레임 이미지 + SVG 오버레이(마커·감지영역·웨이퍼·샘플·포인터).
   서버는 좌표만 준다. 그림은 여기서 그린다(annotated.jpg 는 파일 기록용). */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$;
  const NS = 'http://www.w3.org/2000/svg';

  let lastFrameId = null;
  // 미리보기 모드: 카메라가 지금 보는 그림. 촬영본 모드: 마지막 촬영 + 검출 결과.
  // 촬영이 끝나면 자동으로 촬영본으로 넘어간다(검출 결과를 봐야 하므로).
  let live = false;
  let liveTimer = null;
  let lastPhase = null;
  let firstState = true;

  // 추가 모드: 촬영본의 빈 곳 클릭 = 샘플 추가. 켜져 있는 동안은 hit 원을 끄고
  // 윤곽 다각형 · 수동 마름모 안 클릭만 선택으로 받는다 - 붙어 있는 칩 옆 빈 자리가
  // 반지름 16 px 원에 막히지 않게.
  let addMode = false;
  UI.isAddMode = () => addMode;
  UI.setAddMode = function (on) {
    on = !!on;
    if (on && live) setLive(false);        // 추가는 촬영본 위에서만
    if (on === addMode) return;
    addMode = on;
    const b = $('btnAddSample');
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.classList.toggle('on', on);
    $('cam').classList.toggle('addmode', on);
    $('camHint').textContent = on ? '빈 곳 클릭: 추가 · 샘플 클릭: 선택' : '샘플 클릭: 선택';
  };

  function setLive(on) {
    live = !!on;
    if (live) UI.setAddMode(false);
    $(live ? 'viewLive' : 'viewSnap').checked = true;
    $('cam').classList.toggle('livemode', live);
    $('live').hidden = !live;
    if (UI.state) UI.applyCamera(UI.state);
    if (live) {
      if (!liveTimer) liveTimer = setInterval(tick, livePeriod());
      tick();
    } else if (liveTimer) {
      clearInterval(liveTimer);
      liveTimer = null;
    }
    UI.applyCamera(UI.state);
  }

  // 미리보기 주기는 설정(preview_fps)을 따른다. 바뀌면 타이머를 다시 건다.
  let curPeriod = 0;
  function livePeriod() {
    const fps = +((((UI.state || {}).settings) || {}).preview_fps || 4);
    curPeriod = Math.round(1000 / Math.max(0.5, Math.min(15, fps)));
    return curPeriod;
  }
  function retime() {
    if (!liveTimer) return;
    const want = Math.round(1000 / Math.max(0.5, Math.min(15,
      +((((UI.state || {}).settings) || {}).preview_fps || 4))));
    if (want === curPeriod) return;
    clearInterval(liveTimer);
    liveTimer = setInterval(tick, livePeriod());
  }

  function tick() {
    if (!live) return;
    $('live').src = '/preview.jpg?t=' + Date.now();
  }

  // 미리보기가 아직 없으면 서버가 204 를 준다. 그대로 두면 깨진 이미지 아이콘이
  // 뜨므로 받은 장이 있을 때만 보이게 한다.
  $('live').addEventListener('load', () => { $('live').style.visibility = 'visible'; });
  $('live').addEventListener('error', () => { $('live').style.visibility = 'hidden'; });

  UI.isLive = () => live;

  function el(tag, attrs) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  // 촬영 막: 서버가 준 진행 문구(cap_text)를 그대로 보이고 경과 초만 화면이 센다.
  // 실패(cap_error)는 id 가 새로울 때 한 번 2.8초 보인 뒤 사라진다.
  let capT0 = 0, capTimer = null, capErrId = null, capErrTimer = null;
  function capSecs() {
    $('capSec').textContent = capT0 ? (((Date.now() - capT0) / 1000).toFixed(1) + ' 초') : '';
  }
  function renderCapture(cam) {
    const box = $('capturing');
    if (cam.capturing) {
      if (capErrTimer) { clearTimeout(capErrTimer); capErrTimer = null; }
      if (!capT0) { capT0 = Date.now(); capTimer = setInterval(capSecs, 200); }
      box.classList.remove('err');
      $('capMsg').textContent = cam.cap_text || '촬영 중';
      capSecs();
      box.hidden = false;
      return;
    }
    if (capTimer) { clearInterval(capTimer); capTimer = null; }
    capT0 = 0;
    const e = cam.cap_error;
    if (capErrId === null) capErrId = e ? e.id : 0;      // 새로고침으로 들어왔을 때 옛 오류는 보이지 않는다
    if (e && e.id !== capErrId) {
      capErrId = e.id;
      box.classList.add('err');
      $('capMsg').textContent = e.text;
      $('capSec').textContent = '';
      box.hidden = false;
      if (capErrTimer) clearTimeout(capErrTimer);
      capErrTimer = setTimeout(() => { box.hidden = true; box.classList.remove('err'); capErrTimer = null; }, 2800);
    } else if (!capErrTimer) {
      box.hidden = true;
    }
  }

  UI.applyCamera = function (s) {
    if (!s) return;
    retime();
    // ---- 사진 ----
    const img = $('frame');
    if (s.frame && s.frame.url) {
      if (s.frame.id !== lastFrameId) {
        lastFrameId = s.frame.id;
        img.setAttribute('href', s.frame.url);
        $('cam').setAttribute('viewBox', '0 0 ' + s.frame.w + ' ' + s.frame.h);
        // 고해상도 사진을 같은 화면 크기로 줄여 보이므로 선 굵기 · 글자를 비례로 키운다(원본 px 기준).
        $('cam').style.setProperty('--ovs', String(Math.max(1, s.frame.w / 1280)));
        img.setAttribute('width', s.frame.w);
        img.setAttribute('height', s.frame.h);
      }
    } else if (!s.frame) {
      img.removeAttribute('href');
      lastFrameId = null;
    }
    const cam = s.camera || {};
    renderCapture(cam);
    // 보여 줄 그림이 없을 때 검은 화면만 두지 않는다. 미리보기인데 카메라가 안
    // 열렸으면 그 사유를, 촬영본인데 아직 안 찍었으면 '촬영 없음' 을 띄운다.
    const q0 = s.sequence || {};
    let note = '';
    if (live && cam.ok === false) note = cam.last_error || '카메라 열기 실패';
    else if (!live && !s.frame) note = '촬영 없음';
    $('noFrameText').textContent = note;
    $('noFrameText').title = note;
    $('noFrame').hidden = !note;
    $('overlays').style.display = (!s.frame && !live) ? 'none' : '';
    // 촬영이 끝나면 촬영본으로 되돌린다(검출 결과가 보이게). 새로고침으로 들어왔을
    // 때도 이미 찍어 둔 결과가 있으면 그쪽을 먼저 보여 준다.
    if (firstState) {
      firstState = false;
      if (s.frame && live) { setLive(false); return; }
    }
    // 촬영이 끝났고 결과가 실제로 있을 때만 촬영본으로 넘어간다. 실패했으면
    // 미리보기를 유지한다(빈 화면으로 바꿔 봐야 볼 것이 없다).
    if (live && lastPhase === 'capturing' && q0.phase !== 'capturing' && s.frame) {
      setLive(false);
    }
    lastPhase = q0.phase;

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

    // ---- 고정 보정: 감지 영역 사각형(얇은 선) + 라벨은 사각형 밖 ----
    const K = Math.max(1, ((s.frame && s.frame.w) || 1280) / 1280);
    // 미리보기 = 지금 저장된 감지 영역, 촬영본 = 그 촬영에 쓴 영역(옛 촬영이면 지금 저장 영역을 점선으로 겹치고 바뀜을 알린다)
    const saved = ((s.plate && s.plate.data && s.plate.data.area) || null);
    const savedOk = saved && saved.length === 4;
    const capQuad = s.sensing && s.sensing.quad;
    const capRef = s.sensing && s.sensing.area_ref;
    let quad = live ? (savedOk ? saved : null) : capQuad;
    let changed = false;
    if (!live && savedOk && capRef && capRef.length === 4) {
      changed = capRef.some((p, i) => Math.abs(p[0] - saved[i][0]) > 1 || Math.abs(p[1] - saved[i][1]) > 1);
    }
    const pq = $('ovQuad');
    if (quad && quad.length === 4) {
      pq.setAttribute('points', quad.map(p => p.join(',')).join(' '));
      rect.setAttribute('width', 0); rect.setAttribute('height', 0);   // 사각형이 곧 감지 영역
    } else {
      pq.setAttribute('points', '');
    }
    $('ovQuadSaved').setAttribute('points', (changed && savedOk) ? saved.map(p => p.join(',')).join(' ') : '');
    // ---- 헤드 마커 검출 표시 ----
    const gh = $('ovHead');
    gh.textContent = '';
    const hm = s.head_marker;
    if (hm && hm.corners && hm.corners.length === 4) {
      gh.appendChild(el('polygon', { class: 'ov-head', points: hm.corners.map(p => p.join(',')).join(' ') }));
      const top = Math.min.apply(null, hm.corners.map(p => p[1]));
      const lx = Math.max.apply(null, hm.corners.map(p => p[0])) + 8 * K;
      const t = el('text', { class: 'ov-label head', x: lx, y: Math.max(14 * K, top) });
      t.textContent = '헤드 id' + ((s.settings || {}).photohome || {}).marker_id;
      gh.appendChild(t);
    }
    if (quad && quad.length === 4) {
      // 라벨: 가장 위 꼭짓점의 위쪽(선과 겹치지 않게 사각형 밖), 프레임 밖으로 나가면 아래쪽
      const topP = quad.reduce((a, p) => (p[1] < a[1] ? p : a), quad[0]);
      const botP = quad.reduce((a, p) => (p[1] > a[1] ? p : a), quad[0]);
      const fh = (s.frame && s.frame.h) || 720;
      const up = topP[1] - 8 * K > 14 * K;
      const t = el('text', { class: 'ov-label', x: up ? topP[0] : botP[0], y: up ? topP[1] - 8 * K : Math.min(fh - 4 * K, botP[1] + 18 * K) });
      t.textContent = changed ? '감지 영역 바뀜 · 다시 촬영' : '감지 영역';
      gh.appendChild(t);
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
    // 표시는 번호 하나뿐이다(좌표 숫자는 목록 · 툴팁에만). 라벨은 칩 · 윤곽 · 공구 끝 · 다른 라벨과 겹치지 않는
    // 바깥 빈 자리에 두고 짧은 지시선으로 잇는다(core/labels.py 와 같은 방식). 윤곽은 얇게, 색은 상태에만.
    const gs = $('ovSamples');
    gs.textContent = '';
    const D = 7 * K;
    const list0 = s.samples || [];
    const items = list0.map(sm => {
      let bb;
      if (sm.source === 'predicted') bb = [sm.u - 22 * K, sm.v - 22 * K, sm.u + 22 * K, sm.v + 22 * K];
      else if (sm.verts && sm.verts.length >= 3) {
        const xs = sm.verts.map(p => p[0]), ys = sm.verts.map(p => p[1]);
        bb = [Math.min.apply(null, xs), Math.min.apply(null, ys), Math.max.apply(null, xs), Math.max.apply(null, ys)];
      } else bb = [sm.u - D, sm.v - D, sm.u + D, sm.v + D];
      if (tipOn && !live && s.tip && sm.on && sm.mmpx) {            // 공구 끝 모양도 피한다
        tipPoints(s.tip, sm.mmpx, sm.u, sm.v, sm.tip_deg).forEach(p => {
          bb = [Math.min(bb[0], p[0]), Math.min(bb[1], p[1]), Math.max(bb[2], p[0]), Math.max(bb[3], p[1])];
        });
      }
      const text = sm.no + (sm.manual ? 'M' : '') + (sm.edge_completed ? 'E' : '') + (sm.edge_only ? 'G' : '')
        + (sm.source === 'local' ? '+' : '') + ((sm.weak || sm.source === 'predicted') ? '?' : '');
      return { bbox: bb, text: text, w: (text.length * 7.6 + 6) * K, h: 15 * K };
    });
    const FW = (s.frame && s.frame.w) || 1280, FH = (s.frame && s.frame.h) || 720;
    const placed = placeLabels(items, FW, FH, 6 * K);
    list0.forEach((sm, idx) => {
      const cls = ['ov-sample'];
      if (sm.source === 'local') cls.push('local');
      if (sm.status === 'done') cls.push('done');
      if (sm.status === 'skip' || !sm.on) cls.push('skip');
      if (sm.status === 'moving' || sm.status === 'measuring') cls.push('target');
      if (UI.selected === sm.no) cls.push('selected');
      // 수동 샘플은 윤곽이 없다 - 중심에 작은 마름모(포인터 십자와 다른 모양).
      const noOutline = sm.manual && !(sm.verts && sm.verts.length);
      const pts = noOutline
        ? [[sm.u, sm.v - D], [sm.u + D, sm.v], [sm.u, sm.v + D], [sm.u - D, sm.v]].map(p => p.join(',')).join(' ')
        : (sm.verts || []).map(p => p.join(',')).join(' ');
      if (noOutline) cls.push('manual');
      if (sm.fit === false) cls.push('bad');
      const tipTxt = '#' + sm.no + (sm.X == null ? ' · 좌표 없음' : ' · X ' + sm.X.toFixed(1) + ' Y ' + sm.Y.toFixed(1) + ' mm')
        + (sm.area_mm2 ? ' · ' + sm.area_mm2 + ' mm²' : '') + (sm.source ? ' · ' + sm.source : '')
        + (sm.fit === false ? ' · 공구 끝 벗어남' : '');
      const title = () => { const t = el('title', {}); t.textContent = tipTxt; return t; };
      if (sm.source === 'predicted') {
        // 예측: 윤곽이 없다 - 점선 원
        const pc = el('circle', { class: 'ov-sample pred', cx: sm.u, cy: sm.v, r: 22 * K });
        pc.appendChild(title());
        gs.appendChild(pc);
      } else if (pts) {
        const pg = el('polygon', { class: cls.join(' '), points: pts });
        pg.appendChild(title());
        // 추가 모드에서만 선택을 받는다(평소에는 위의 hit 원이 받는다).
        pg.addEventListener('click', (e) => {
          if (!addMode) return;
          e.stopPropagation();
          UI.select(sm.no);
        });
        gs.appendChild(pg);
      }
      const pl = placed[idx], it = items[idx];
      gs.appendChild(el('line', { class: 'ov-lead', x1: pl.from[0], y1: pl.from[1], x2: pl.to[0], y2: pl.to[1] }));
      gs.appendChild(el('rect', { class: 'ov-labbg', x: pl.box[0], y: pl.box[1], width: it.w, height: it.h, rx: 2 * K }));
      const t = el('text', { class: 'ov-num' + (sm.fit === false ? ' bad' : ''), x: pl.box[0] + 3 * K, y: pl.box[1] + it.h - 3.5 * K });
      t.textContent = it.text;
      t.appendChild(title());
      // 추가 모드에서는 번호 라벨도 그 샘플 선택이다(hit 원이 꺼져 있어 라벨 클릭이
      // 사진으로 빠지면 작은 칩 옆에 수동 샘플이 생긴다). 평소에는 CSS 가 막는다.
      t.addEventListener('click', (e) => {
        if (!addMode) return;
        e.stopPropagation();
        UI.select(sm.no);
      });
      gs.appendChild(t);
      // 클릭 판정용(다각형이 얇아도 집히도록 원을 덮는다)
      const hit = el('circle', { class: 'ov-hit', cx: sm.u, cy: sm.v, r: 16 * K, 'data-no': sm.no });
      hit.appendChild(title());
      hit.addEventListener('click', () => UI.select(sm.no));
      gs.appendChild(hit);
    });

    drawZones(s);
    // ---- 공구 끝 모양(순회 대상 샘플마다 · 실제 크기 · 사진 기준 각도) ----
    const gt = $('ovTips');
    gt.textContent = '';
    if (tipOn && !live && s.tip) {
      (s.samples || []).forEach(sm => {
        if (!sm.on || !sm.mmpx) return;
        gt.appendChild(el('polygon', { class: 'ov-tip' + (sm.fit === false ? ' bad' : ''),
                                       points: tipPoints(s.tip, sm.mmpx, sm.u, sm.v, sm.tip_deg).map(p => p.join(',')).join(' ') }));
      });
    }

    // ---- 포인터(스테이지 현재 위치) ----
    // #ovPointer 는 SVG <g> 다. .hidden 프로퍼티는 HTMLElement 전용이라 여기서는
    // 아무 일도 하지 않는다(포인터가 영영 안 보였다) - 속성으로 직접 켜고 끈다.
    const st = s.stage || {};
    const gp = $('ovPointer');
    if (st.connected && st.u != null && st.v != null) {
      gp.removeAttribute('hidden');
      gp.setAttribute('transform', 'translate(' + st.u + ',' + st.v + ') scale(' + K + ')');
      // 미리보기에는 검출 결과가 없다. 십자는 보정으로 계산한 자리이므로 무엇을
      // 기준으로 그렸는지 밝힌다 - 이번 실행에 촬영이 없으면 저장된(이전) 보정을
      // 쓰는데, 그 사이 카메라가 돌아갔다면 십자가 실제 포인터와 수십 mm 떨어진다.
      const stale = !s.calib;
      $('pointerLabel').textContent = 'X ' + UI.fmt(st.x_mm) + ' Y ' + UI.fmt(st.y_mm)
        + (st.moving ? ' 이동중' : '')
        + (live ? (stale ? ' · 이전 보정 기준' : ' · 마지막 촬영 기준') : '');
      gp.classList.toggle('stale', live && stale);
    } else {
      gp.setAttribute('hidden', '');
    }

    // ---- 정보 4칸 ----
    const c = s.calib;
    const fixed = c && c.mode === 'fixed';
    $('infoCal').textContent = c
      ? (fixed && c.area_only ? '검출만 · 판 보정 필요' : fixed ? ('고정 보정 · 오차 RMS ' + c.corner_rms_mm.toFixed(2) + ' · 최대 ' + c.corner_max_mm.toFixed(2) + ' mm')
        : (c.used_ids.length + '마커 ' + c.corners + '점 · 오차 RMS '
         + c.corner_rms_mm.toFixed(2) + ' · 최대 ' + c.corner_max_mm.toFixed(2) + ' mm'
         + (c.missing_ids.length ? ' · id' + c.missing_ids.join(',') + ' 미검출' : '')))
      : UI.EMPTY;
    let rectText = UI.EMPTY;
    if (live && savedOk) {
      const xs = saved.map(p => p[0]), ys = saved.map(p => p[1]);
      rectText = '저장 영역 ' + Math.round(Math.max.apply(null, xs) - Math.min.apply(null, xs)) + '×'
        + Math.round(Math.max.apply(null, ys) - Math.min.apply(null, ys)) + ' px';
    } else if (r) {
      rectText = quad ? ('사각형 ' + (r[2] - r[0]) + '×' + (r[3] - r[1]) + ' px 안')
                      : ('(' + r[0] + ',' + r[1] + ')-(' + r[2] + ',' + r[3] + ') ' + (r[2] - r[0]) + '×' + (r[3] - r[1]) + ' px');
      if (changed) rectText = (quad ? (r[2] - r[0]) + '×' + (r[3] - r[1]) + ' px' : rectText) + ' · 바뀜';
    }
    $('infoRect').textContent = rectText;
    $('infoWafer').textContent = (w && w.found && w.center_mm)
      ? ('중심 X' + w.center_mm[0].toFixed(1) + ' Y' + w.center_mm[1].toFixed(1)
         + ' · r ' + w.r_px.toFixed(0) + ' px')
      : UI.EMPTY;
    const list = s.samples || [];
    const tri = list.filter(x => x.shape === 'triangle').length;
    const on = list.filter(x => x.on).length;
    const man = list.filter(x => x.manual).length;
    $('infoSamples').textContent = list.length
      ? (list.length + ' (삼각 ' + tri + ')' + (man ? ' · 수동 ' + man : '')
         + ' · 대상 ' + on) : UI.EMPTY;
  };

  // 추가 모드의 사진 클릭 → 프레임 px. SVG 는 viewBox(frame.w×frame.h)를 meet 로
  // 그리므로 map.js 와 같은 방식으로 실제 배치된 사각형을 되짚는다.
  // getBoundingClientRect 는 fit() 의 축소까지 반영한 값이라 창 크기와 무관하다.
  $('cam').addEventListener('click', (ev) => {
    if (!addMode || live) return;
    const f = UI.state && UI.state.frame;
    if (!f || !f.w || !f.h) return;
    const r = $('cam').getBoundingClientRect();
    const k = Math.min(r.width / f.w, r.height / f.h);
    const u = (ev.clientX - r.left - (r.width - f.w * k) / 2) / k;
    const v = (ev.clientY - r.top - (r.height - f.h * k) / 2) / k;
    if (!(u >= 0 && v >= 0 && u <= f.w && v <= f.h)) return;   // 레터박스
    UI.send({ cmd: 'sample_add', u: Math.round(u * 10) / 10, v: Math.round(v * 10) / 10 });
  });

  // ---------------- 라벨 배치(core/labels.py place 와 같은 방식) ----------------
  // items[i] = {bbox:[x0,y0,x1,y1], w, h}. 칩 둘레 바깥 8방향 x 거리 3단계 중 칩 · 다른 라벨과 안 겹치는 첫 자리.
  function ovArea(a, b) {
    return Math.max(0, Math.min(a[2], b[2]) - Math.max(a[0], b[0])) * Math.max(0, Math.min(a[3], b[3]) - Math.max(a[1], b[1]));
  }
  function placeLabels(items, W, H, gap) {
    const chips = items.map(i => i.bbox), placed = [], out = [];
    items.forEach((it, i) => {
      const [x0, y0, x1, y1] = chips[i], w = it.w, h = it.h, cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
      const cands = [];
      [1, 2, 3.2].forEach(k => {
        const g = gap * k;
        cands.push([x1 + g, cy - h / 2], [x0 - g - w, cy - h / 2], [cx - w / 2, y0 - g - h], [cx - w / 2, y1 + g],
                   [x1 + g, y0 - g - h], [x0 - g - w, y0 - g - h], [x1 + g, y1 + g], [x0 - g - w, y1 + g]);
      });
      let best = null, bestCost = null;
      for (const c of cands) {
        const lx = Math.min(Math.max(c[0], 2), Math.max(2, W - w - 2)), ly = Math.min(Math.max(c[1], 2), Math.max(2, H - h - 2));
        const box = [lx, ly, lx + w, ly + h];
        let cost = 0;
        chips.forEach((cc, j) => { cost += ovArea(box, [cc[0] - 2, cc[1] - 2, cc[2] + 2, cc[3] + 2]) * (j === i ? 3 : 1); });
        placed.forEach(p => { cost += ovArea(box, p) * 2; });
        if (cost === 0) { best = box; bestCost = 0; break; }
        if (bestCost === null || cost < bestCost) { best = box; bestCost = cost; }
      }
      placed.push(best);
      const lcx = (best[0] + best[2]) / 2, lcy = (best[1] + best[3]) / 2;
      const a = [Math.min(Math.max(lcx, x0), x1), Math.min(Math.max(lcy, y0), y1)];
      const b = [Math.min(Math.max(a[0], best[0]), best[2]), Math.min(Math.max(a[1], best[1]), best[3])];
      out.push({ box: best, from: a, to: b });
    });
    return out;
  }

  // ---------------- 공구 끝 모양: 그리기 · 맞음 검사(core/tip.py 와 같은 식) ----------------
  let tipOn = true;
  try { tipOn = localStorage.getItem('tipOn') !== '0'; } catch (e) { /* 저장 못 해도 동작 */ }
  function syncTipBtn() {
    const b = $('btnTip');
    b.setAttribute('aria-pressed', tipOn ? 'true' : 'false');
    b.classList.toggle('on', tipOn);
  }
  syncTipBtn();
  $('btnZones').onclick = () => {
    zonesOn = !zonesOn;
    $('btnZones').setAttribute('aria-pressed', zonesOn ? 'true' : 'false');
    $('btnZones').classList.toggle('on', zonesOn);
    UI.applyCamera(UI.state);
  };
  $('btnTip').onclick = () => {
    tipOn = !tipOn;
    try { localStorage.setItem('tipOn', tipOn ? '1' : '0'); } catch (e) { /* 무시 */ }
    syncTipBtn();
    UI.applyCamera(UI.state);
  };

  function tipPoints(tip, mmpx, cx, cy, angle) {
    const k = 1 / Math.max(mmpx, 1e-9);
    const out = [];
    if (tip.shape === 'circle') {
      const r = tip.w * k / 2;
      for (let i = 0; i < 24; i++) out.push([cx + r * Math.cos(2 * Math.PI * i / 24), cy + r * Math.sin(2 * Math.PI * i / 24)]);
      return out;
    }
    const ang = angle != null ? angle : (tip.angle_now != null ? tip.angle_now : tip.angle_img || 0);
    const hw = tip.w * k / 2, hh = tip.h * k / 2, th = ang * Math.PI / 180;
    const c = Math.cos(th), sn = Math.sin(th);
    [[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]].forEach(q => out.push([cx + q[0] * c - q[1] * sn, cy + q[0] * sn + q[1] * c]));
    return out;
  }
  // 점에서 다각형까지 부호 있는 거리(안쪽 +, 바깥 -)
  function signedDist(p, poly) {
    let inside = false, best = Infinity;
    for (let i = 0, n = poly.length; i < n; i++) {
      const a = poly[i], b = poly[(i + 1) % n];
      if ((a[1] > p[1]) !== (b[1] > p[1]) && p[0] < (b[0] - a[0]) * (p[1] - a[1]) / (b[1] - a[1]) + a[0]) inside = !inside;
      const dx = b[0] - a[0], dy = b[1] - a[1];
      const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy || 1)));
      best = Math.min(best, Math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy));
    }
    return inside ? best : -best;
  }
  function tipFits(pts, verts, marginPx) {
    if (!verts || verts.length < 3) return null;
    return pts.every(p => signedDist(p, verts) >= -marginPx);
  }
  // 쿨러 구역 격자(옅게, 토글): 서버가 판 사각형 mm 정의(cooler.zones)로 계산한 칸의 사진 px 사각형을 그린다(스테이지 맵과 같은 칸 · 번호).
  let zonesOn = false;
  function drawZones(s) {
    const g = $('ovZones');
    g.textContent = '';
    const z = s.zones;
    if (!zonesOn || live || !z || !z.has_area) return;
    const K = Math.max(1, ((s.frame && s.frame.w) || 1280) / 1280);
    z.cells.forEach(c => {
      const pts = c.quad_px.map(p => p.join(',')).join(' ');
      g.appendChild(el('polygon', { class: 'ov-zone' + (c.on ? ' on' : ''), points: pts }));
      const cx = c.quad_px.reduce((a, p) => a + p[0], 0) / 4, top = Math.min.apply(null, c.quad_px.map(p => p[1]));
      const lx = Math.min.apply(null, c.quad_px.map(p => p[0])) + 6 * K, ly = top + 16 * K;
      const t = el('text', { class: 'ov-zone-lab', x: cx, y: ly });
      t.textContent = c.cooler + ' · R' + c.relay;
      g.appendChild(t);
    });
  }
  function curMmpx(s, u, v) {
    const sm = (s.samples || []).find(x => x.mmpx);
    return (s.sensing && s.sensing.mm_px) || (sm && sm.mmpx) || null;
  }
  // 끌기 · 추가 모드 미리보기용 '유령' 공구 끝(맞음 검사 포함)
  function drawGhost(u, v, verts) {
    const g = $('ovTipHover');
    g.textContent = '';
    const s = UI.state || {};
    const mp = curMmpx(s);
    if (u == null || !s.tip || !mp) return;
    const pts = tipPoints(s.tip, mp, u, v);
    const f = tipFits(pts, verts, (s.tip.margin_mm || 0) / mp);
    g.appendChild(el('polygon', { class: 'ov-tip ghost' + (f === false ? ' bad' : (f ? ' ok' : '')),
                                  points: pts.map(p => p.join(',')).join(' ') }));
  }
  function eventPx(ev) {
    const f = UI.state && UI.state.frame;
    if (!f || !f.w || !f.h) return null;
    const r = $('cam').getBoundingClientRect();
    const k = Math.min(r.width / f.w, r.height / f.h);
    const u = (ev.clientX - r.left - (r.width - f.w * k) / 2) / k;
    const v = (ev.clientY - r.top - (r.height - f.h * k) / 2) / k;
    return (u >= 0 && v >= 0 && u <= f.w && v <= f.h) ? [u, v] : null;
  }
  function sampleAt(u, v) {
    return ((UI.state || {}).samples || []).find(x => x.verts && x.verts.length >= 3 && signedDist([u, v], x.verts) >= 0) || null;
  }
  // 추가 모드: 마우스를 따라 공구 끝 모양
  $('cam').addEventListener('pointermove', (ev) => {
    if (drag) return;
    if (!addMode || live) { if (!drag) drawGhost(null); return; }
    const p = eventPx(ev);
    if (!p) { drawGhost(null); return; }
    const sm = sampleAt(p[0], p[1]);
    drawGhost(p[0], p[1], sm ? sm.verts : null);
  });
  $('cam').addEventListener('pointerleave', () => { if (!drag) drawGhost(null); });

  // 끌어서 샘플 중심 이동(추가 모드가 아닐 때)
  let drag = null;
  $('cam').addEventListener('pointerdown', (ev) => {
    const t = ev.target;
    if (addMode || live || !t.classList || !t.classList.contains('ov-hit') || ev.button !== 0) return;
    const sm = ((UI.state || {}).samples || []).find(x => String(x.no) === t.getAttribute('data-no'));
    if (!sm) return;
    drag = { no: sm.no, sm: sm, moved: false, u: sm.u, v: sm.v };
    $('cam').setPointerCapture(ev.pointerId);
    $('cam').classList.add('dragging');
  });
  window.addEventListener('pointermove', (ev) => {
    if (!drag) return;
    const p = eventPx(ev);
    if (!p) return;
    const K = Math.max(1, ((UI.state && UI.state.frame && UI.state.frame.w) || 1280) / 1280);
    if (Math.hypot(p[0] - drag.sm.u, p[1] - drag.sm.v) > 4 * K) drag.moved = true;
    if (!drag.moved) return;
    drag.u = p[0]; drag.v = p[1];
    drawGhost(p[0], p[1], drag.sm.verts);       // 끄는 동안에도 맞음 검사가 실시간
  });
  window.addEventListener('pointerup', () => {
    if (!drag) return;
    const d = drag;
    drag = null;
    $('cam').classList.remove('dragging');
    drawGhost(null);
    if (d.moved) UI.send({ cmd: 'sample_move', no: d.no, u: Math.round(d.u * 10) / 10, v: Math.round(d.v * 10) / 10 });
  });

  // 카메라 도구 버튼
  $('btnCapture').onclick = () => UI.send({ cmd: 'capture' });
  $('btnPark').onclick = () => UI.send({ cmd: 'park' });
  $('btnAddSample').onclick = () => UI.setAddMode(!addMode);
  $('btnDelSample').onclick = () => {
    if (UI.selected != null) UI.send({ cmd: 'sample_delete', no: UI.selected });
  };
  $('btnUndo').onclick = () => UI.send({ cmd: 'sample_undo' });
  $('viewLive').onchange = () => setLive(true);
  $('viewSnap').onchange = () => setLive(false);
  // 처음에는 미리보기로 시작한다 - 촬영본이 없는 상태에서 검은 화면을 보여 줄
  // 이유가 없다. 촬영이 끝나면 자동으로 촬영본으로 넘어간다.
  setLive(true);
})();
