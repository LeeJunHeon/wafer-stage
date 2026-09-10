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

  function setLive(on) {
    live = !!on;
    $(live ? 'viewLive' : 'viewSnap').checked = true;
    $('cam').classList.toggle('livemode', live);
    $('live').hidden = !live;
    if (live) {
      if (!liveTimer) liveTimer = setInterval(tick, 250);
      tick();
    } else if (liveTimer) {
      clearInterval(liveTimer);
      liveTimer = null;
    }
    UI.applyCamera(UI.state);
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

  UI.applyCamera = function (s) {
    if (!s) return;
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
    const cam = s.camera || {};
    $('capturing').hidden = !cam.capturing;
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
    // #ovPointer 는 SVG <g> 다. .hidden 프로퍼티는 HTMLElement 전용이라 여기서는
    // 아무 일도 하지 않는다(포인터가 영영 안 보였다) - 속성으로 직접 켜고 끈다.
    const st = s.stage || {};
    const gp = $('ovPointer');
    if (st.connected && st.u != null && st.v != null) {
      gp.removeAttribute('hidden');
      gp.setAttribute('transform', 'translate(' + st.u + ',' + st.v + ')');
      $('pointerLabel').textContent = 'X ' + UI.fmt(st.x_mm) + ' Y ' + UI.fmt(st.y_mm)
        + (st.moving ? ' 이동중' : '');
    } else {
      gp.setAttribute('hidden', '');
    }

    // ---- 정보 4칸 ----
    const c = s.calib;
    $('infoCal').textContent = c
      ? (c.used_ids.length + '마커 ' + c.corners + '점 · 오차 RMS '
         + c.corner_rms_mm.toFixed(2) + ' · 최대 ' + c.corner_max_mm.toFixed(2) + ' mm'
         + (c.missing_ids.length ? ' · id' + c.missing_ids.join(',') + ' 미검출' : ''))
      : UI.EMPTY;
    $('infoRect').textContent = r
      ? ('(' + r[0] + ',' + r[1] + ')-(' + r[2] + ',' + r[3] + ') '
         + (r[2] - r[0]) + '×' + (r[3] - r[1]) + ' px')
      : UI.EMPTY;
    $('infoWafer').textContent = (w && w.found && w.center_mm)
      ? ('중심 X' + w.center_mm[0].toFixed(1) + ' Y' + w.center_mm[1].toFixed(1)
         + ' · r ' + w.r_px.toFixed(0) + ' px')
      : UI.EMPTY;
    const list = s.samples || [];
    const tri = list.filter(x => x.shape === 'triangle').length;
    const on = list.filter(x => x.on).length;
    $('infoSamples').textContent = list.length
      ? (list.length + ' (삼각 ' + tri + ') · 대상 ' + on) : UI.EMPTY;
  };

  // 카메라 도구 버튼
  $('btnCapture').onclick = () => UI.send({ cmd: 'capture' });
  $('btnPark').onclick = () => UI.send({ cmd: 'park' });
  $('btnHome').onclick = async () => {
    const ok = await UI.confirm(
      '원점 설정: 각 축을 끝단까지 이동합니다(끝에 닿는 소리는 정상).\n'
      + '이동 경로에 프로브·웨이퍼가 없는지 확인 후 진행하십시오.', '원점 설정');
    if (ok) UI.send({ cmd: 'stage_home', axis: 'xy' });
  };
  $('viewLive').onchange = () => setLive(true);
  $('viewSnap').onchange = () => setLive(false);
  // 처음에는 미리보기로 시작한다 - 촬영본이 없는 상태에서 검은 화면을 보여 줄
  // 이유가 없다. 촬영이 끝나면 자동으로 촬영본으로 넘어간다.
  setLive(true);
})();
