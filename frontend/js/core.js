/* core.js — 고정 캔버스 fit, 헤더(상태 필·위치·칩), 경보 배너, 로그, 모달, 잠금.
   다른 js 보다 먼저 로드된다. 전역은 window.UI 하나로 모은다.

   화면은 한 화면에 다 들어간다(페이지 스크롤 없음). 1920×1040 캔버스를 창 크기에
   맞춰 축소만 하고, 내부에서 스크롤하는 곳은 샘플 표와 로그 둘뿐이다. */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const EMPTY = '—';                  // 값이 없으면 대시. 가짜 숫자를 만들지 않는다.

  const UI = window.UI = {
    state: null, online: false, selected: null, EMPTY: EMPTY, $: $,
  };

  // ---------------- 고정 캔버스 ----------------
  function fit() {
    const s = Math.min(window.innerWidth / 1920, window.innerHeight / 1040);
    $('app').style.transform = 'translate(-50%, -50%) scale(' + s + ')';
  }
  window.addEventListener('resize', fit);
  fit();
  setInterval(() => {
    $('clock').textContent = new Date().toLocaleString('ko-KR', { hour12: false });
  }, 1000);

  // ---------------- 로그 ----------------
  const logEl = $('log');
  function hhmmss() {
    const d = new Date();
    return [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map(x => String(x).padStart(2, '0')).join(':');
  }
  UI.log = function (msg, level) {
    if (!logEl) return;
    const cls = ({ ok: 'rx', warn: 'warn', err: 'warn', info: 'sys' })[level] || 'sys';
    const div = document.createElement('div');
    const t = document.createElement('span'); t.className = 't'; t.textContent = hhmmss();
    const m = document.createElement('span'); m.className = cls; m.textContent = ' ' + msg;
    div.appendChild(t); div.appendChild(m);
    logEl.appendChild(div);
    while (logEl.childElementCount > 500) logEl.removeChild(logEl.firstChild);
    logEl.scrollTop = logEl.scrollHeight;
  };
  $('btnClearLog').onclick = () => { logEl.textContent = ''; };

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
  function ask(msg, title, withCancel) {
    $('askTitle').textContent = title || '확인';
    $('askBody').textContent = msg;
    $('askCancel').hidden = !withCancel;
    dlgAsk.showModal();
    return new Promise(r => { askResolve = r; });
  }
  UI.alert = (msg, title) => ask(msg, title || '알림', false);
  UI.confirm = (msg, title) => ask(msg, title || '확인', true);

  // 창 X → window.py 가 부른다. 확인해야 실제로 닫힌다.
  window.requestExitConfirm = function () {
    UI.confirm('프로그램을 종료합니다.\n파킹 → 위치 저장(save) 후 닫힙니다.', '종료')
      .then(ok => {
        if (!ok) return;
        if (!UI.send({ cmd: 'exit' }) && window.pywebview && window.pywebview.api) {
          window.pywebview.api.force_close();   // 서버가 죽었을 때의 직통 경로
        }
      });
  };
  $('btnExit').onclick = () => window.requestExitConfirm();

  // ---------------- 헤더 ----------------
  // 색만으로 상태를 알리지 않는다(색각 이상·모니터 편차). 글자를 함께 바꾼다.
  const PILL = {
    idle: ['IDLE', ''], ready: ['READY', 'ready'], capturing: ['CAPTURE', 'running'],
    running: ['RUNNING', 'running'], paused: ['PAUSED', 'paused'],
    waiting_confirm: ['CONFIRM', 'waiting'], parking: ['PARKING', 'running'],
    done: ['DONE', 'ready'], stopped: ['STOPPED', 'stopped'], error: ['ERROR', 'error'],
  };

  function chip(el, kind, text) {
    if (!el) return;
    el.classList.remove('ok', 'warn', 'bad');
    if (kind) el.classList.add(kind);
    const m = el.querySelector('.mono');
    if (m && text != null) m.textContent = text;
  }

  function fmt(v) { return (v == null) ? EMPTY : (+v).toFixed(1); }
  UI.fmt = fmt;

  UI.applyHeader = function (s) {
    const st = s.stage || {}, cam = s.camera || {}, q = s.sequence || {};
    let txt = '—', cls = '';
    if (PILL[q.phase]) { txt = PILL[q.phase][0]; cls = PILL[q.phase][1]; }
    if (q.phase === 'stopped' && q.estopped) { txt = 'E-STOP'; cls = 'estop'; }
    $('statePill').className = 'statepill ' + cls;
    $('stateText').textContent = txt;

    // 연결이 없으면 위치는 모르는 값이다 - 마지막 숫자를 남겨 두지 않는다.
    $('posX').textContent = st.connected ? fmt(st.x_mm) : EMPTY;
    $('posY').textContent = st.connected ? fmt(st.y_mm) : EMPTY;

    chip($('chipCam'), cam.ok === false ? 'bad' : (cam.ok ? 'ok' : 'warn'),
      'index ' + cam.index + (cam.capturing ? ' · 촬영 중' : (cam.last_error ? ' · 오류' : '')));
    chip($('chipStage'),
      st.connected ? ((st.homed_x && st.homed_y && !st.needs_home) ? 'ok' : 'warn') : 'bad',
      st.connected
        ? (st.port || '미연결') + ' · '
          + (st.needs_home ? '원점 필요' : (st.homed_x && st.homed_y ? '원점OK' : '원점없음'))
        : '미연결');
    const drv = ((s.settings || {}).measure || {}).driver || 'dummy';
    chip($('chipMeter'), drv === 'dummy' ? null : 'ok',
      drv === 'dummy' ? '미연결 (dummy)' : drv);
    const v = s.version || {};
    $('appVer').textContent = 'v' + (v.version || '?');
    $('barVer').textContent = (v.name || 'Sample Auto Measurement') + ' v' + (v.version || '?');
    $('outDir').textContent = q.out_dir || EMPTY;
  };

  // ---------------- 경보 배너 ----------------
  UI.applyNotice = function (s) {
    const items = [];
    let alarm = false;
    (s.warnings || []).forEach(w => {
      if (w.indexOf('cut off') >= 0) items.push('웨이퍼가 감지영역에 잘렸습니다');
      else if (w.indexOf('glare covers') >= 0) {
        const m = w.match(/glare covers\s*([\d.]+)%/);
        items.push('글레어 ' + (m ? m[1] : '?') + '% — 조명을 확산광으로');
      }
    });
    const c = s.calib;
    if (c && c.missing_ids && c.missing_ids.length) {
      items.push('마커 id ' + c.missing_ids.join(',') + ' 가려짐 — ' + c.corners
                 + '점 보정(오차 약 1mm)');
    }
    const st = s.stage || {}, q = s.sequence || {};
    if (st.needs_home) items.push('원점 필요 — 원점 잡기(fz) 후 이동할 수 있습니다');
    if (st.last_error) items.push('스테이지: ' + st.last_error);
    if (q.estopped) { items.push('비상정지 상태'); alarm = true; }
    if (q.phase === 'error') { items.push(q.message || '오류'); alarm = true; }

    const n = $('notice');
    n.hidden = items.length === 0;
    n.className = 'notice' + (alarm ? ' alarm' : '');
    n.querySelector('.tag').textContent = alarm ? '경보' : '확인 필요';
    const box = $('noticeItems');
    box.textContent = '';
    items.forEach(t => {
      const sp = document.createElement('span');
      sp.textContent = t;
      box.appendChild(sp);
    });
  };

  // ---------------- 오프라인 / 잠금 ----------------
  UI.setOnline = function (on) {
    UI.online = on;
    chip($('chipServer'), on ? 'ok' : 'bad', on ? '연결됨' : '연결 끊김');
    document.body.classList.toggle('offline', !on);
    if (!on) {
      chip($('chipCam'), null, EMPTY);
      chip($('chipStage'), null, EMPTY);
      chip($('chipMeter'), null, EMPTY);
      $('statePill').className = 'statepill';
      $('stateText').textContent = '연결 끊김';
      ['posX', 'posY', 'infoCal', 'infoRect', 'infoWafer', 'infoSamples', 'frameTime',
       'mapPos', 'progText', 'curSample', 'curTarget', 'curStatus', 'countInfo',
       'measureVal', 'outDir'].forEach(id => { if ($(id)) $(id).textContent = EMPTY; });
      const n = $('notice');
      n.hidden = false;
      n.className = 'notice alarm';
      n.querySelector('.tag').textContent = '경보';
      $('noticeItems').textContent = '서버 연결 끊김 — 2초마다 다시 연결합니다';
    }
    UI.lock();
  };

  UI.lock = function () {
    const s = UI.state, on = UI.online;
    const st = (s && s.stage) || {}, q = (s && s.sequence) || {};
    // 이동은 연결 + 원점 + 비상정지 해제 상태에서만.
    const canMove = on && st.connected && st.homed_x && st.homed_y && !st.needs_home;
    const running = ['running', 'paused', 'waiting_confirm', 'parking', 'capturing']
      .indexOf(q.phase) >= 0;
    const dis = (id, v) => { const e = $(id); if (e) e.disabled = !!v; };
    dis('btnCapture', !on || running);
    dis('btnPark', !canMove || running);
    dis('btnHome', !on || !st.connected || running);
    dis('btnConnect', !on || running);
    dis('btnSettings', !on || running);
    dis('btnEstop', !on);                    // 비상정지는 항상 활성
    dis('btnStart', !canMove || running || !(s && s.samples && s.samples.length));
    dis('btnPause', !on || !(q.phase === 'running' || q.phase === 'paused'));
    dis('btnNext', !on || q.phase !== 'waiting_confirm');
    dis('btnStop', !on || !running);
    dis('btnAll', !on);
    dis('btnNone', !on);
    dis('btnGoto', !canMove || running || UI.selected == null);
    dis('btnMeasureHere', !canMove || running);
    dis('btnOpenDir', !on || !q.out_dir);
    dis('btnExport', !on || !q.out_dir);
    $('btnConnect').textContent = st.connected ? '연결 해제' : '연결';
    $('btnPause').textContent = q.phase === 'paused' ? '재개' : '일시정지';

    const lm = $('lockMsg');
    let why = '';
    if (!on) why = '서버 연결 끊김 — 조작할 수 없습니다';
    else if (!st.connected) why = '스테이지 미연결 — [연결] 을 누르세요';
    else if (!(st.homed_x && st.homed_y)) why = '원점 없음 — [원점 잡기] 를 누르세요';
    else if (st.needs_home) why = '비상정지 후 — [원점 잡기] 를 다시 하세요';
    else if (running) why = '순회 중 — 정지 후 조작하세요';
    lm.hidden = !why;
    lm.textContent = why;
  };

  UI.applyFrameNote = function (s) {
    const el = $('frameTime');
    if (!s.frame) { el.textContent = EMPTY; return; }
    const t = new Date((s.frame.ts || 0) * 1000);
    const hh = [t.getHours(), t.getMinutes(), t.getSeconds()]
      .map(x => String(x).padStart(2, '0')).join(':');
    el.textContent = '촬영 ' + hh + ' · ' + s.frame.w + '×' + s.frame.h;
  };
})();
