/* core.js — 고정 캔버스 fit, 헤더(상태 필·위치·칩), 배너, 로그, 창, 잠금.
   다른 js 보다 먼저 로드된다. 전역은 window.UI 하나로 모은다.

   화면은 한 화면에 다 들어간다(페이지 스크롤 없음). 1920×1040 캔버스를 창 크기에
   맞춰 축소만 하고, 내부에서 스크롤하는 곳은 샘플 표 · 로그 · 단계 목록 · 릴레이뿐이다.

   창(설정·보정·확인 등)은 showModal 을 쓰지 않는다. showModal 은 뒤 화면을 inert 로
   만들어 헤더의 비상정지를 누를 수 없게 한다. 대신 show() + 막(#dimmer)으로 뒤를 막고,
   비상정지 도크(#estopDock)를 막 위에 띄워 어떤 창이 열려 있어도 눌리게 한다. */
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
  $('btnLogDir').onclick = () => UI.send({ cmd: 'open_log_dir' });
  // 표시 중인 줄을 그대로 클립보드로. 창(WebView2)에서 권한이 막히면 textarea 로.
  $('btnCopyLog').onclick = async () => {
    const text = [...logEl.children].map(d => d.textContent).join('\n');
    if (!text) return;
    let ok = false;
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch (e) {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.className = 'clipta';
      document.body.appendChild(ta);
      ta.select();
      try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
      document.body.removeChild(ta);
    }
    UI.log(ok ? ('로그 ' + logEl.childElementCount + '줄 복사') : '복사 실패',
           ok ? 'ok' : 'warn');
  };

  // ---------------- 창 ----------------
  // 열린 창 목록(아래→위). 맨 위 창이 Esc 와 툴팁 범위의 주인이다.
  const wins = [];
  const Z_BASE = 950;

  function syncLayer() {
    const any = wins.length > 0;
    $('dimmer').hidden = !any;
    $('estopDock').hidden = !any;
    wins.forEach((w, i) => { w.dlg.style.zIndex = String(Z_BASE + i * 2); });
  }

  // opts.onCancel: Esc · 닫기 요청 때 부른다(없으면 바로 닫는다).
  UI.openWin = function (dlg, opts) {
    if (!dlg.open) dlg.show();
    const old = wins.findIndex(w => w.dlg === dlg);
    if (old >= 0) wins.splice(old, 1);
    wins.push({ dlg: dlg, opts: opts || {} });
    syncLayer();
  };
  UI.closeWin = function (dlg) {
    const i = wins.findIndex(w => w.dlg === dlg);
    if (i >= 0) wins.splice(i, 1);
    if (dlg.open) dlg.close();
    syncLayer();
  };
  UI.topWin = () => (wins.length ? wins[wins.length - 1].dlg : null);
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !wins.length) return;
    const w = wins[wins.length - 1];
    e.preventDefault();
    if (w.opts.onCancel) w.opts.onCancel(); else UI.closeWin(w.dlg);
  });
  $('estopDock').onclick = () => UI.sendEstop();

  // ---------------- 툴팁 ----------------
  // data-tip 을 단 요소에 마우스를 0.4초 올리면 설명을 띄운다. 비활성 버튼은
  // 마우스 이벤트를 받지 못하므로(CSS 에서 pointer-events:none) 좌표로 찾는다.
  const tipBox = $('tip');
  let tipTimer = null, tipEl = null, tipLast = 0;

  function tipScopes() {
    // 창이 열려 있으면 뒤(막 아래)의 설명이 새어 나오지 않게, 맨 위 창 · 비상정지 도크 ·
    // 수동 이동 팝업(막 위에 뜬다)만 찾는다.
    const top = UI.topWin();
    if (!top) return [document];
    const out = [top, $('estopDock')];
    if ($('dlgJog').open) out.push($('dlgJog'));
    return out;
  }

  function tipTargetAt(x, y) {
    let best = null, bestArea = Infinity;
    tipScopes().forEach(scope => {
      const nodes = scope.querySelectorAll ? [...scope.querySelectorAll('[data-tip]')] : [];
      if (scope.hasAttribute && scope.hasAttribute('data-tip')) nodes.push(scope);
      nodes.forEach(el => {
        if (!el.getAttribute('data-tip')) return;
        const r = el.getBoundingClientRect();
        if (!r.width || !r.height) return;
        if (x < r.left || x > r.right || y < r.top || y > r.bottom) return;
        const area = r.width * r.height;
        if (area < bestArea) { best = el; bestArea = area; }
      });
    });
    return best;
  }

  function showTip(el) {
    tipBox.textContent = el.getAttribute('data-tip');
    tipBox.hidden = false;
    const r = el.getBoundingClientRect();
    const b = tipBox.getBoundingClientRect();
    const left = Math.min(Math.max(4, r.left), window.innerWidth - b.width - 4);
    let top = r.bottom + 6;
    if (top + b.height > window.innerHeight - 4) top = r.top - b.height - 6;
    tipBox.style.left = Math.round(left) + 'px';
    tipBox.style.top = Math.round(Math.max(4, top)) + 'px';
  }

  function hideTip() {
    if (tipTimer) { clearTimeout(tipTimer); tipTimer = null; }
    tipEl = null;
    tipBox.hidden = true;
  }

  document.addEventListener('mousemove', (e) => {
    const now = Date.now();
    if (now - tipLast < 50) return;        // 스로틀 - 매 픽셀마다 훑지 않는다
    tipLast = now;
    const el = tipTargetAt(e.clientX, e.clientY);
    if (el === tipEl) return;
    hideTip();
    if (!el || !el.getAttribute('data-tip')) return;
    tipEl = el;
    tipTimer = setTimeout(() => { if (tipEl === el) showTip(el); }, 400);
  });
  document.addEventListener('mousedown', hideTip);
  document.addEventListener('mouseleave', hideTip);
  window.addEventListener('blur', hideTip);

  // ---------------- 확인 대화상자 ----------------
  const dlgAsk = $('dlgAsk');
  let askResolve = null;
  function closeAsk(v) {
    UI.closeWin(dlgAsk);
    const r = askResolve; askResolve = null;
    if (r) r(v);
  }
  $('askOk').onclick = () => closeAsk(true);
  $('askCancel').onclick = () => closeAsk(false);
  function ask(msg, title, withCancel, okLabel) {
    if (askResolve) closeAsk(false);       // 겹쳐 묻지 않는다
    $('askTitle').textContent = title || '확인';
    $('askBody').textContent = msg;
    $('askCancel').hidden = !withCancel;
    $('askOk').textContent = okLabel || '확인';
    UI.openWin(dlgAsk, { onCancel: () => closeAsk(false) });
    $('askOk').focus();
    return new Promise(r => { askResolve = r; });
  }
  UI.alert = (msg, title) => ask(msg, title || '알림', false);
  UI.confirm = (msg, title, okLabel) => ask(msg, title || '확인', true, okLabel);

  // 창 X → window.py 가 부른다. 확인해야 실제로 닫힌다.
  window.requestExitConfirm = function () {
    UI.confirm('위치를 저장하고 프로그램을 종료합니다. 스테이지는 움직이지 않습니다.', '종료')
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
    waiting_confirm: ['확인 대기', 'waiting'], parking: ['정리', 'running'],
    done: ['완료', 'ready'], stopped: ['정지', 'stopped'], error: ['오류', 'error'],
  };
  const HOME = { unknown: ['미확인', 'warn'], ok: ['정상', 'ok'],
                 corrected: ['보정됨', 'ok'], failed: ['실패', 'bad'] };

  function chip(el, kind, text) {
    if (!el) return;
    el.classList.remove('ok', 'warn', 'bad', 'hot');
    if (kind) el.classList.add(kind);
    const m = el.querySelector('.mono');
    if (m && text != null) m.textContent = text;
  }
  UI.chip = chip;

  // 원점은 축마다 따로다. 한쪽만 없으면 어느 쪽인지 말해 준다.
  function originText(st) {
    if (st.needs_home) return '원점 없음';
    if (st.homed_x && st.homed_y) return '원점 등록됨';
    if (!st.homed_x && !st.homed_y) return '원점 없음';
    return (st.homed_x ? 'Y' : 'X') + ' 원점 없음';
  }
  UI.originText = originText;

  function fmt(v) { return (v == null) ? EMPTY : (+v).toFixed(1); }
  UI.fmt = fmt;

  // 비상정지는 두 경로로 보낸다. WebSocket 이 막혀 있어도(오래 걸리는 명령을
  // 처리하는 중이었다면 그랬다) HTTP 로는 도착한다. 서버 쪽은 두 번 받아도
  // 같은 상태가 되므로 중복이 문제되지 않는다.
  UI.sendEstop = function () {
    UI.send({ cmd: 'estop' });
    try {
      fetch('/estop', { method: 'POST', keepalive: true }).catch(() => {});
    } catch (e) { /* fetch 자체가 막힌 환경 */ }
  };

  // 순회 · 촬영 · 보정 작업 중인가(조작 잠금 기준).
  UI.isRunning = function (s) {
    const q = (s && s.sequence) || {};
    return ['running', 'paused', 'waiting_confirm', 'parking', 'capturing']
      .indexOf(q.phase) >= 0 || !!(s && s.job && s.job.running);
  };

  UI.applyHeader = function (s) {
    const st = s.stage || {}, cam = s.camera || {}, q = s.sequence || {};
    let txt = '—', cls = '';
    if (PILL[q.phase]) { txt = PILL[q.phase][0]; cls = PILL[q.phase][1]; }
    if (q.phase === 'stopped' && q.estopped) { txt = '비상정지'; cls = 'estop'; }
    if (s.job && s.job.running && !(q.phase in { running: 1, paused: 1 })) {
      txt = '보정'; cls = 'running';
    }
    $('statePill').className = 'statepill ' + cls;
    $('stateText').textContent = txt;

    // 연결이 없으면 위치는 모르는 값이다 - 마지막 숫자를 남겨 두지 않는다.
    $('posX').textContent = st.connected ? fmt(st.x_mm) : EMPTY;
    $('posY').textContent = st.connected ? fmt(st.y_mm) : EMPTY;
    $('posZ').textContent = st.connected ? fmt(st.z_mm) : EMPTY;

    let camTxt;
    if (cam.capturing) camTxt = '촬영 중';
    else if (cam.ok === false) camTxt = '열기 실패';
    else if (cam.preview) camTxt = '미리보기';
    else camTxt = '대기';
    chip($('chipCam'), cam.ok === false ? 'bad' : (cam.ok ? 'ok' : 'warn'),
      cam.index + ' · ' + camTxt);
    const xy = st.homed_x && st.homed_y && !st.needs_home;
    chip($('chipStage'),
      st.connected ? (xy ? 'ok' : 'warn') : 'bad',
      st.connected
        ? (st.port || '?') + ' · XY ' + (xy ? '원점' : '없음')
          + ' · Z ' + (st.homed_z ? '원점' : '없음')
          + ' · ' + (st.dirty ? '미저장' : '저장')
        : '미연결');
    // 보정 칩: 고정 보정이면 저장한 판 보정의 RMS, 무효 · 미설정이면 경보 색. 마커 방식은 이번 촬영의 보정.
    const pl = s.plate || {};
    if (pl.mode === 'fixed') {
      const pd = pl.data || {};
      chip($('chipCalib'), pl.valid ? ((pd.res_limit_mm && pd.max_mm > pd.res_limit_mm) ? 'warn' : 'ok') : 'bad',
        pl.valid ? ('고정 보정 · RMS ' + (+pd.rms_mm).toFixed(2) + ' mm') : (pd.n || pd.area ? '무효 · 다시 보정' : '미설정'));
      $('chipCalib').setAttribute('data-tip', pl.valid
        ? ('고정 보정입니다. 판 보정 ' + (pd.created || '') + ' · ' + pd.n + '점 · RMS ' + (+pd.rms_mm).toFixed(3)
           + ' mm · 최대 ' + (+pd.max_mm).toFixed(3) + ' mm 입니다.')
        : ('고정 보정이 무효입니다. ' + (pl.reason || '') + ' · 보정 창의 감지 영역 · 판 보정에서 다시 잡으십시오.'));
    } else {
      const c = s.calib;
      chip($('chipCalib'), c ? (c.missing_ids && c.missing_ids.length ? 'warn' : 'ok') : null,
        c ? ('마커 ' + c.used_ids.length + '개 · RMS ' + c.corner_rms_mm.toFixed(2) + ' mm') : '마커 방식');
      $('chipCalib').setAttribute('data-tip', '판 마커 방식의 보정입니다. 이번 촬영의 마커로 계산한 잔차(RMS)입니다.');
    }
    const ph = s.photohome || {};
    const hk = HOME[ph.status] || HOME.unknown;
    chip($('chipHome'), st.connected ? hk[1] : null,
      st.connected ? (hk[0] + (ph.d_mm != null ? ' ' + (+ph.d_mm).toFixed(2) : '')) : EMPTY);
    $('chipHome').title = ph.msg || '';
    const tool = s.tool || {};
    chip($('chipTool'), (tool.dx_mm || tool.dy_mm || tool.name !== '기준 포인터') ? 'ok' : null,
      tool.name || EMPTY);
    const nOn = (s.relays || []).filter(r => r.on).length;
    chip($('chipRelay'), nOn ? 'hot' : null, st.connected ? (nOn + ' 켜짐') : EMPTY);
    const drv = ((s.settings || {}).measure || {}).driver || 'dummy';
    chip($('chipMeter'), drv === 'dummy' ? null : 'ok',
      drv === 'dummy' ? '미연결' : drv);
    const v = s.version || {};
    $('barVer').textContent = (v.name || 'Sample Auto Measurement') + ' v' + (v.version || '?');
    $('outDir').textContent = q.out_dir || EMPTY;
    $('logFile').textContent = s.log_file || EMPTY;
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
    (s.alarms || []).forEach(a => add(a.src, a.text, true));
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
    if (st.connected && (st.needs_home || !st.homed_x || !st.homed_y)) {
      add('스테이지', originText(st) + ' · 사진 호밍 또는 수동 이동에서 등록');
    }
    const ph = s.photohome || {};
    if (st.connected && ph.status === 'failed') add('위치 확인', ph.msg || '실패');
    else if (st.connected && ph.calib_valid && ph.status === 'unknown') {
      add('위치 확인', '위치 미확인 · 사진 호밍');
    }
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
    n.querySelector('.tag').textContent = alarm ? '경보' : '경고';
    fillNotice(items);
  };

  function fillNotice(items) {
    const box = $('noticeItems');
    box.textContent = '';
    items.forEach(it => {
      const sp = document.createElement('span');
      sp.className = 'item' + (it.err ? ' err' : '');
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
    if (!on && UI.jogReset) UI.jogReset();   // 오지 않을 ack 를 기다리지 않는다
    chip($('chipServer'), on ? 'ok' : 'bad', on ? '정상' : '끊김');
    document.body.classList.toggle('offline', !on);
    if (!on) {
      ['chipCam', 'chipStage', 'chipMeter', 'chipCalib', 'chipHome', 'chipTool', 'chipRelay']
        .forEach(id => chip($(id), null, EMPTY));
      $('statePill').className = 'statepill error';
      $('stateText').textContent = '연결 끊김';
      ['posX', 'posY', 'posZ', 'infoCal', 'infoRect', 'infoWafer', 'infoSamples',
       'mapPos', 'progText', 'curSample', 'curStatus', 'countInfo', 'estText',
       'outDir', 'flowNote'].forEach(id => { if ($(id)) $(id).textContent = EMPTY; });
      $('countInfo').title = '';
      $('frameTime').textContent = '촬영 없음';
      const n = $('notice');
      n.hidden = false;
      n.className = 'notice alarm';
      n.querySelector('.tag').textContent = '경보';
      fillNotice([{ src: '서버', text: '연결 끊김 · 재연결 중', err: true }]);
    }
    UI.lock();
  };

  // 비활성 버튼에는 왜 비활성인지 툴팁으로 알린다. 원래 설명은 data-tip0 에 둔다.
  function why(el, reason) {
    if (!el) return;
    if (!reason && !el.hasAttribute('data-tip0')) return;
    if (!el.hasAttribute('data-tip0')) el.setAttribute('data-tip0', el.getAttribute('data-tip') || '');
    const base = el.getAttribute('data-tip0');
    el.setAttribute('data-tip', reason ? (base ? reason + ' · ' + base : reason) : base);
  }
  UI.why = why;

  UI.lock = function () {
    const s = UI.state, on = UI.online;
    const st = (s && s.stage) || {}, q = (s && s.sequence) || {};
    // 이동은 연결 + 원점 + 비상정지 해제 상태에서만.
    const canMove = on && st.connected && st.homed_x && st.homed_y && !st.needs_home;
    const running = UI.isRunning(s);
    let moveWhy = '';
    if (!on) moveWhy = '서버 연결 끊김';
    else if (!st.connected) moveWhy = '스테이지 미연결';
    else if (st.needs_home) moveWhy = '비상정지 · 원점 필요';
    else if (!(st.homed_x && st.homed_y)) moveWhy = originText(st);
    else if (running) moveWhy = '순회 · 보정 중';
    const dis = (id, v, reason) => {
      const e = $(id);
      if (!e) return;
      e.disabled = !!v;
      why(e, v ? reason : '');
    };
    dis('btnCapture', !on || running, running ? '순회 · 보정 중' : '서버 연결 끊김');
    dis('btnPark', !canMove || running, moveWhy);
    dis('btnJog', !on, '서버 연결 끊김');   // 팝업은 열리고, 안에서 다시 잠근다
    dis('btnPhotoHome', !on || !st.connected || running || !!st.moving,
        !on ? '서버 연결 끊김' : (!st.connected ? '스테이지 미연결'
          : (running ? '순회 · 보정 중' : '이동 중')));
    // 종료는 이동을 일으키지 않지만, 이동 중에 창이 닫히면 비상정지를 누를 데가
    // 없다. 순회 중·이동 중에는 막는다(서버도 같은 조건으로 거절한다).
    dis('btnExit', !on || running || !!st.moving, running ? '순회 · 보정 중' : '이동 중');
    dis('btnConnect', !on || running, '순회 · 보정 중');
    dis('btnSettings', !on || running, '순회 · 보정 중');
    dis('btnCalib', !on, '서버 연결 끊김');
    dis('btnEstop', !on, '서버 연결 끊김');   // 비상정지는 항상 활성
    const hasSamples = !!(s && s.samples && s.samples.length);
    dis('btnStart', !canMove || running || !hasSamples,
        !hasSamples && canMove && !running ? '샘플 없음 · 촬영 · 검출 먼저' : moveWhy);
    dis('btnPause', !on || !(q.phase === 'running' || q.phase === 'paused'), '순회 중 아님');
    dis('btnNext', !on || q.phase !== 'waiting_confirm', '확인 대기 아님');
    dis('btnStop', !on || !running, '진행 중인 작업 없음');
    dis('btnAll', !on, '서버 연결 끊김');
    dis('btnNone', !on, '서버 연결 끊김');
    dis('selRecipe', !on || running, '순회 중에는 바꿀 수 없음');
    dis('btnRecipeEdit', !on || running, '순회 중에는 편집할 수 없음');
    dis('btnGoto', !canMove || running || UI.selected == null,
        UI.selected == null && canMove && !running ? '샘플 선택 필요' : moveWhy);
    dis('btnMeasureHere', !canMove || running, moveWhy);
    // 샘플 편집: 이번 촬영 결과가 있고 순회·촬영 중이 아닐 때만(서버도 같은 조건).
    const list = (s && s.samples) || [];
    const canEdit = on && !running && !!(s && s.calib);
    dis('btnAddSample', !canEdit, running ? '순회 · 보정 중' : '촬영 결과 없음');
    dis('btnDelSample', !canEdit || UI.selected == null
        || !list.some(x => x.no === UI.selected), '샘플 선택 필요');
    dis('btnUndo', !on || running || !((s && s.edit && s.edit.undo) > 0), '되돌릴 편집 없음');
    if (!canEdit && UI.setAddMode) UI.setAddMode(false);   // 잠기면 추가 모드도 끈다
    dis('viewLive', !on);
    dis('viewSnap', !on);
    dis('btnOpenDir', !on || !q.out_dir, '결과 폴더 없음');
    dis('btnExport', !on || !q.out_dir, '결과 없음');
    $('chipTool').classList.toggle('locked', !on || running);
    $('btnConnect').textContent = st.connected ? '연결 해제' : '연결';
    $('btnPause').textContent = q.phase === 'paused' ? '재개' : '일시정지';
    if (UI.lockRelays) UI.lockRelays(s);

    const lm = $('lockMsg');
    let msg = '';
    if (!on) msg = '서버 연결 끊김';
    else if (!st.connected) msg = '스테이지 미연결';
    else if (st.needs_home) msg = '비상정지 · 원점 등록 또는 사진 호밍 필요';
    else if (!(st.homed_x && st.homed_y)) msg = originText(st) + ' · 사진 호밍 또는 수동 이동';
    else if (running) msg = '순회 · 보정 중 · 조작 잠금';
    lm.hidden = !msg;
    lm.textContent = msg;
  };

  UI.applyFrameNote = function (s) {
    const el = $('frameTime');
    if (!s.frame) { el.textContent = '촬영 없음'; return; }
    const t = new Date((s.frame.ts || 0) * 1000);
    const hh = [t.getHours(), t.getMinutes(), t.getSeconds()]
      .map(x => String(x).padStart(2, '0')).join(':');
    el.textContent = '촬영 ' + hh + ' · ' + s.frame.w + '×' + s.frame.h;
  };

  // 초 → "1분 12초" / "45초"
  UI.dur = function (sec) {
    if (sec == null || !isFinite(sec)) return EMPTY;
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return sec + '초';
    const m = Math.floor(sec / 60), r = sec % 60;
    if (m < 60) return m + '분' + (r ? ' ' + r + '초' : '');
    return Math.floor(m / 60) + '시간 ' + (m % 60) + '분';
  };

  // 요소 만들기: h('button', {cls, text, onclick, 'data-tip': ...}, [자식...])
  UI.h = function (tag, attrs, kids) {
    const e = document.createElement(tag);
    for (const k in (attrs || {})) {
      if (k === 'text') e.textContent = attrs[k];
      else if (k === 'cls') e.className = attrs[k];
      else if (k.slice(0, 2) === 'on') e.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] !== undefined && attrs[k] !== null && attrs[k] !== false) e.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach(c => { if (c) e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
    return e;
  };

  // 깊은 복사 · 비교(설정 편집기용)
  UI.clone = (o) => JSON.parse(JSON.stringify(o === undefined ? null : o));
  UI.same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
})();
