/* core.js — 고정 캔버스 fit, 헤더(상태 필·위치·칩), 배너, 로그, 모달, 잠금.
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
  function stamp(d) {
    const p2 = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate())
      + ' ' + p2(d.getHours()) + ':' + p2(d.getMinutes()) + ':' + p2(d.getSeconds());
  }
  setInterval(() => { $('clock').textContent = stamp(new Date()); }, 1000);

  // ---------------- 로그 ----------------
  const logEl = $('log');
  function hhmmss() {
    const d = new Date();
    return [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map(x => String(x).padStart(2, '0')).join(':');
  }
  // 시리얼 원문은 양이 많다. 화면에서 숨겨도 파일(serial.log)에는 전부 남는다.
  UI.log = function (msg, level, meta) {
    if (!logEl) return;
    meta = meta || {};
    if (meta.serial && !$('optSerial').checked) return;
    if (meta.poll && !$('optPoll').checked) return;
    const cls = ({ ok: 'ok', warn: 'warn', err: 'warn', info: 'sys',
                   tx: 'tx', rx: 'rx' })[level] || 'sys';
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
    UI.confirm('파킹 및 위치 저장 후 프로그램을 종료합니다.', '종료')
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
    idle: ['대기', ''], ready: ['준비', 'ready'], capturing: ['촬영', 'running'],
    running: ['순회', 'running'], paused: ['일시정지', 'paused'],
    waiting_confirm: ['확인 대기', 'waiting'], parking: ['파킹', 'running'],
    done: ['완료', 'ready'], stopped: ['정지', 'stopped'], error: ['오류', 'error'],
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
    if (q.phase === 'stopped' && q.estopped) { txt = '비상정지'; cls = 'estop'; }
    $('statePill').className = 'statepill ' + cls;
    $('stateText').textContent = txt;

    // 연결이 없으면 위치는 모르는 값이다 - 마지막 숫자를 남겨 두지 않는다.
    $('posX').textContent = st.connected ? fmt(st.x_mm) : EMPTY;
    $('posY').textContent = st.connected ? fmt(st.y_mm) : EMPTY;

    let camTxt;
    if (cam.capturing) camTxt = '촬영 중';
    else if (cam.ok === false) camTxt = '열기 실패';
    else if (cam.preview) camTxt = '미리보기';
    else camTxt = '대기';
    chip($('chipCam'), cam.ok === false ? 'bad' : (cam.ok ? 'ok' : 'warn'),
      'index ' + cam.index + ' · ' + camTxt);
    chip($('chipStage'),
      st.connected ? ((st.homed_x && st.homed_y && !st.needs_home) ? 'ok' : 'warn') : 'bad',
      st.connected
        ? (st.port || '미연결') + ' · '
          + ((st.homed_x && st.homed_y && !st.needs_home) ? '원점 설정' : '원점 필요')
        : '미연결');
    const drv = ((s.settings || {}).measure || {}).driver || 'dummy';
    chip($('chipMeter'), drv === 'dummy' ? null : 'ok',
      drv === 'dummy' ? '미연결' : drv);
    const v = s.version || {};
    $('appVer').textContent = 'v' + (v.version || '?');
    $('barVer').textContent = (v.name || 'Sample Auto Measurement') + ' v' + (v.version || '?');
    $('outDir').textContent = q.out_dir || EMPTY;
  };

  // ---------------- 배너 ----------------
  // 항목은 [출처, 문구, 오류인가] 로 모은다. 출처를 굵게 앞에 두면 여러 개가
  // 나란히 있어도 어디서 난 것인지 한눈에 보인다. 같은 출처·같은 문구는 한 번만.
  UI.applyNotice = function (s) {
    const items = [];
    const seen = new Set();
    const add = (src, text, isErr) => {
      if (!text) return;
      const key = src + '\u0000' + text;
      if (seen.has(key)) return;
      seen.add(key);
      items.push({ src: src, text: text, err: !!isErr });
    };

    const cam = s.camera || {}, st = s.stage || {}, q = s.sequence || {};
    (s.warnings || []).forEach(w => {
      if (w.indexOf('cut off') >= 0) add('검출', '웨이퍼가 감지영역 밖');
      else if (w.indexOf('glare covers') >= 0) {
        const m = w.match(/glare covers\s*([\d.]+)%/);
        add('검출', '반사광 ' + (m ? m[1] : '?') + '% · 조명 확산 필요');
      }
    });
    const c = s.calib;
    if (c && c.missing_ids && c.missing_ids.length) {
      add('보정', '마커 id' + c.missing_ids.join(',') + ' 미검출 · ' + c.corners
                  + '점 보정(오차 ≈1 mm)');
    }
    if (st.needs_home) add('스테이지', '원점 미설정 · 이동 잠금');
    if (st.last_error) add('스테이지', st.last_error, true);
    if (cam.last_error) add('카메라', cam.last_error, true);
    if (q.estopped) add('스테이지', '비상정지', true);
    // phase=error 의 문구는 대개 위 오류를 되풀이한다. 이미 오류 항목이 있으면 빼서
    // 배너가 같은 말을 두 번 하지 않게 한다.
    if (q.phase === 'error' && !items.some(x => x.err)) add('순회', q.message || '오류', true);

    const n = $('notice');
    const alarm = items.some(x => x.err);
    n.hidden = items.length === 0;
    n.className = 'notice' + (alarm ? ' alarm' : '');
    n.querySelector('.tag').textContent = alarm ? '오류' : '경고';
    fillNotice(items);
  };

  function fillNotice(items) {
    const box = $('noticeItems');
    box.textContent = '';
    items.forEach(it => {
      const sp = document.createElement('span');
      sp.className = 'item';
      sp.title = it.src + ' ' + it.text;
      const b = document.createElement('b');
      b.textContent = it.src;
      sp.appendChild(b);
      sp.appendChild(document.createTextNode(it.text));
      box.appendChild(sp);
    });
  }

  // ---------------- 오프라인 / 잠금 ----------------
  UI.setOnline = function (on) {
    UI.online = on;
    chip($('chipServer'), on ? 'ok' : 'bad', on ? '정상' : '끊김');
    document.body.classList.toggle('offline', !on);
    if (!on) {
      chip($('chipCam'), null, EMPTY);
      chip($('chipStage'), null, EMPTY);
      chip($('chipMeter'), null, EMPTY);
      $('statePill').className = 'statepill error';
      $('stateText').textContent = '연결 끊김';
      ['posX', 'posY', 'infoCal', 'infoRect', 'infoWafer', 'infoSamples',
       'mapPos', 'progText', 'curSample', 'curTarget', 'curStatus', 'countInfo',
       'outDir'].forEach(id => { if ($(id)) $(id).textContent = EMPTY; });
      $('frameTime').textContent = '촬영 없음';
      $('measureVal').textContent = '미연결';
      const n = $('notice');
      n.hidden = false;
      n.className = 'notice alarm';
      n.querySelector('.tag').textContent = '오류';
      fillNotice([{ src: '서버', text: '연결 끊김 · 재연결 중', err: true }]);
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
    dis('btnJog', !on);                      // 팝업은 열리고, 안에서 다시 잠근다
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
    dis('viewLive', !on);
    dis('viewSnap', !on);
    dis('btnOpenDir', !on || !q.out_dir);
    dis('btnExport', !on || !q.out_dir);
    $('btnConnect').textContent = st.connected ? '연결 해제' : '연결';
    $('btnPause').textContent = q.phase === 'paused' ? '재개' : '일시정지';

    const lm = $('lockMsg');
    let why = '';
    if (!on) why = '서버 연결 끊김';
    else if (!st.connected) why = '스테이지 미연결';
    else if (!(st.homed_x && st.homed_y)) why = '원점 미설정';
    else if (st.needs_home) why = '비상정지 · 원점 설정 필요';
    else if (running) why = '순회 중 · 조작 잠금';
    lm.hidden = !why;
    lm.textContent = why;
  };

  UI.applyFrameNote = function (s) {
    const el = $('frameTime');
    if (!s.frame) { el.textContent = '촬영 없음'; return; }
    const t = new Date((s.frame.ts || 0) * 1000);
    const hh = [t.getHours(), t.getMinutes(), t.getSeconds()]
      .map(x => String(x).padStart(2, '0')).join(':');
    el.textContent = '촬영 ' + hh + ' · ' + s.frame.w + '×' + s.frame.h;
  };
})();
