/* core.js — 헤더 상태 칩·진행 단계·로그·모달(alert/confirm/종료).
   다른 js 보다 먼저 로드된다. 전역 UI 는 window.UI 하나로 모은다. */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const EMPTY = '—';                 // 값이 없을 때 쓰는 대시. 가짜 숫자를 만들지 않는다.

  const UI = window.UI = {
    state: null,                          // 마지막으로 받은 서버 state
    online: false,
    selected: null,                       // 표/오버레이에서 고른 샘플 번호
    EMPTY: EMPTY,
    $: $,
  };

  // ---------------- 로그 ----------------
  const logEl = $('log');
  function now() {
    const d = new Date();
    return [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map(x => String(x).padStart(2, '0')).join(':');
  }
  UI.log = function (msg, level) {
    if (!logEl) return;
    const cls = ({ ok: 'rx', warn: 'warn', err: 'warn', info: 'sys' })[level] || 'sys';
    const div = document.createElement('div');
    const t = document.createElement('span'); t.className = 't'; t.textContent = now();
    const m = document.createElement('span'); m.className = cls; m.textContent = ' ' + msg;
    div.appendChild(t); div.appendChild(m);
    logEl.appendChild(div);
    while (logEl.childElementCount > 500) logEl.removeChild(logEl.firstChild);
    logEl.scrollTop = logEl.scrollHeight;
  };
  if ($('btnClearLog')) $('btnClearLog').onclick = () => { logEl.textContent = ''; };

  // ---------------- 모달 ----------------
  const dlgAsk = $('dlgAsk');
  let askResolve = null;
  function closeAsk(v) {
    if (dlgAsk.open) dlgAsk.close();
    const r = askResolve; askResolve = null;
    if (r) r(v);
  }
  $('askOk').onclick = () => closeAsk(true);
  $('askCancel').onclick = () => closeAsk(false);
  dlgAsk.addEventListener('cancel', (e) => { e.preventDefault(); closeAsk(false); });

  UI.alert = function (msg, title) {
    $('askTitle').textContent = title || '알림';
    $('askBody').textContent = msg;
    $('askCancel').hidden = true;
    dlgAsk.showModal();
    return new Promise(r => { askResolve = r; });
  };
  UI.confirm = function (msg, title) {
    $('askTitle').textContent = title || '확인';
    $('askBody').textContent = msg;
    $('askCancel').hidden = false;
    dlgAsk.showModal();
    return new Promise(r => { askResolve = r; });
  };

  // 창 X → window.py 가 이 함수를 부른다. 확인해야 실제로 닫힌다.
  window.requestExitConfirm = function () {
    UI.confirm('프로그램을 종료합니다.\n파킹 → 위치 저장(save) 후 닫힙니다.', '종료')
      .then(ok => {
        if (!ok) return;
        if (!UI.send({ cmd: 'exit' })) {
          // 서버가 끊겼으면 pywebview 직통 경로로 닫는다.
          if (window.pywebview && window.pywebview.api) window.pywebview.api.force_close();
        }
      });
  };
  if ($('btnExit')) $('btnExit').onclick = () => window.requestExitConfirm();

  // ---------------- 헤더 칩 / 단계 ----------------
  function chip(el, kind, text) {
    if (!el) return;
    el.classList.remove('ok', 'warn', 'bad');
    if (kind) el.classList.add(kind);
    if (text != null) {
      const m = el.querySelector('.mono');
      if (m) m.textContent = text;
    }
  }

  UI.setOnline = function (on) {
    UI.online = on;
    chip($('chipServer'), on ? 'ok' : 'bad', on ? '연결됨' : '연결 끊김');
    document.body.classList.toggle('offline', !on);
    if (!on) {
      chip($('chipCam'), null, EMPTY);
      chip($('chipStage'), null, EMPTY);
      ['infoCal', 'infoRect', 'infoSamples', 'infoPos', 'frameTime', 'runState',
       'progText', 'curSample', 'curTarget', 'curStatus', 'countInfo', 'measureVal']
        .forEach(id => { if ($(id)) $(id).textContent = EMPTY; });
    }
    UI.lock();
  };

  // 오프라인이거나 스테이지가 준비되지 않았으면 조작을 잠근다.
  // 화면만 바뀌고 장비는 그대로인 상태를 만들지 않기 위해서다.
  UI.lock = function () {
    const s = UI.state;
    const on = UI.online;
    const stage = (s && s.stage) || {};
    const canMove = on && stage.connected && stage.homed_x && stage.homed_y;
    const seq = (s && s.sequence) || {};
    const running = ['running', 'paused', 'waiting_confirm', 'parking', 'capturing']
      .indexOf(seq.phase) >= 0;
    const dis = (id, v) => { const e = $(id); if (e) e.disabled = !!v; };
    dis('btnCapture', !on || running);
    dis('btnPark', !canMove || running);
    dis('btnHome', !on || !stage.connected || running);
    dis('btnConnect', !on);
    dis('btnSettings', !on);
    dis('btnEstop', !on);                 // 비상정지는 서버만 있으면 항상 가능
    dis('btnStart', !canMove || running || !(s && s.samples && s.samples.length));
    dis('btnPause', !(seq.phase === 'running' || seq.phase === 'paused'));
    dis('btnNext', seq.phase !== 'waiting_confirm');
    dis('btnStop', !running);
    dis('btnGoto', !canMove || running || UI.selected == null);
    dis('btnMeasureHere', !canMove || running);
    const bc = $('btnConnect');
    if (bc) bc.textContent = stage.connected ? '연결 해제' : '연결';
  };

  UI.steps = function (s) {
    const seq = (s && s.sequence) || {};
    const stage = (s && s.stage) || {};
    let cur = 1;
    if (stage.connected && stage.homed_x && stage.homed_y) cur = 2;
    if (s && s.samples && s.samples.length) cur = 3;
    if (['running', 'paused', 'waiting_confirm', 'parking'].indexOf(seq.phase) >= 0) cur = 4;
    if (['done', 'stopped'].indexOf(seq.phase) >= 0) cur = 5;
    document.querySelectorAll('.step').forEach(el => {
      const n = +el.dataset.step;
      el.classList.toggle('active', n === cur);
      el.classList.toggle('done', n < cur);
    });
  };

  UI.applyHeader = function (s) {
    const st = s.stage || {}, cam = s.camera || {};
    chip($('chipStage'), st.connected ? (st.homed_x && st.homed_y ? 'ok' : 'warn') : 'bad',
      st.connected
        ? (st.port || '?') + ' · ' + (st.homed_x && st.homed_y ? '원점OK' : '원점없음')
          + ' · X ' + fmt(st.x_mm) + ' Y ' + fmt(st.y_mm)
        : '미연결');
    chip($('chipCam'), cam.ok === false ? 'bad' : (cam.ok ? 'ok' : 'warn'),
      'index ' + cam.index + (cam.capturing ? ' · 촬영 중' : (cam.last_error ? ' · 오류' : '')));
    const drv = ((s.settings || {}).measure || {}).driver || 'dummy';
    chip($('chipMeter'), drv === 'dummy' ? null : 'ok',
      drv === 'dummy' ? '미연결 (dummy)' : drv);
    const v = s.version || {};
    $('brandSub').textContent = 'wafer_stage 통합 콘솔 · v' + (v.version || '?');
    $('infoPos').textContent = (st.x_mm == null) ? EMPTY
      : ('X ' + fmt(st.x_mm) + '  Y ' + fmt(st.y_mm) + ' mm' + (st.moving ? ' (이동중)' : ''));
  };

  function fmt(v) { return (v == null) ? EMPTY : (+v).toFixed(1); }
  UI.fmt = fmt;

  UI.warn = function (list) {
    // 검출 경고는 로그로만 흘리면 놓치기 쉬워 카메라 패널 아래 note 로도 보여준다.
    const el = $('frameTime');
    if (!el || !UI.state || !UI.state.frame) return;
    const t = new Date((UI.state.frame.ts || 0) * 1000);
    const hhmm = [t.getHours(), t.getMinutes(), t.getSeconds()]
      .map(x => String(x).padStart(2, '0')).join(':');
    el.textContent = '촬영 ' + hhmm + (list && list.length ? ' · 경고 ' + list.length + '건' : '');
  };
})();
