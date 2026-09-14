/* jog.js — 수동 이동 팝업(조그 패드 · 절대 이동 · 파킹/원점).

   서버가 상태의 주인이다: 화면은 "이 축으로 이만큼" 만 보내고, 목표 좌표와
   가동범위 자르기는 서버가 한다. 위치 숫자도 서버 state 만 그린다.

   누르고 있는 동안 이어서 움직이는 것은 ack{of:"jog"} 를 받은 뒤 다음 스텝을
   보내는 방식이다(한 번에 하나만 보낸다). 타이머로 밀어 넣으면 손을 뗀 뒤에도
   큐에 남은 명령이 계속 실행돼 스테이지가 멋대로 더 간다.

   데드맨: 키보드 조그는 keydown 자동 반복이 계속 와야 유지된다. 창이 포커스를
   잃거나 탭이 가려지면 keyup 이 오지 않을 수 있고, 그러면 손을 뗀 줄 알면서도
   스테이지가 계속 간다. 마지막 keydown 이 400ms 넘게 끊기면 스스로 멈춘다. */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$;

  const dlg = $('dlgJog');
  const KEY_ALIVE_MS = 400;   // 키보드 반복이 이보다 끊기면 손을 뗀 것으로 본다

  let held = null;        // 누르고 있는 방향 {axis, dir, src:'key'|'pointer'}
  let inFlight = false;   // 보낸 조그의 ack 를 기다리는 중인가
  let canMove = false;
  let lastKeyAt = 0;      // 마지막 keydown 시각(데드맨)
  let keySeq = 0;         // keydown 이 올 때마다 증가(자동 반복 포함)
  let sentSeq = -1;       // 마지막으로 보낸 스텝이 어느 keydown 에서 나왔나

  function step() {
    const el = document.querySelector('input[name=jogstep]:checked');
    return el ? +el.value : 1;
  }

  function send(axis, dir) {
    if (!canMove || inFlight) return false;
    inFlight = true;
    sentSeq = keySeq;
    if (!UI.send({ cmd: 'jog', axis: axis, delta_mm: dir * step() })) {
      inFlight = false;   // 못 보냈으면 잠금을 풀어 둔다
      return false;
    }
    return true;
  }

  // 서버가 조그를 끝냈다. 거절이면(끝단·이동 중·잠금·순회 중·오류) 이어 보내지
  // 않는다 - 가동범위 끝에서 누르고 있으면 서버가 거절하는 족족 다시 보내 초당
  // 1300회까지 오갔다(실측). 다시 움직이려면 손을 뗐다 눌러야 한다.
  UI.onJogAck = function (msg) {
    inFlight = false;
    if (msg && msg.ok === false) {
      if (msg.reason === 'at_limit') note('가동범위 끝');
      stop();
      return;
    }
    if (!held) return;
    // 키보드 조그는 keydown 이 계속 와야 이어진다. 새 keydown 없이 다음 스텝을
    // 보내면 키에서 손을 뗀 뒤에도(keyup 을 놓친 경우) 계속 가 버린다.
    // 자동 반복이 올 때마다 keySeq 가 늘고, 그때 다시 start() 가 걸린다.
    if (held.src === 'key'
        && (keySeq === sentSeq || Date.now() - lastKeyAt > KEY_ALIVE_MS)) {
      stop();
      return;
    }
    send(held.axis, held.dir);
  };

  function start(axis, dir, src) {
    if (!canMove) return;
    held = { axis: axis, dir: dir, src: src };
    send(axis, dir);
  }

  function stop() {
    held = null;          // in-flight 인 한 스텝은 끝까지 간다(중간에 못 끊는다)
  }

  // 알림 한 줄(1초 뒤 사라진다). 상태 갱신이 덮지 않게 칸을 따로 쓴다.
  let noteTimer = null;
  function note(text) {
    $('jogNote').textContent = text;
    if (noteTimer) clearTimeout(noteTimer);
    noteTimer = setTimeout(() => { $('jogNote').textContent = ''; }, 1000);
  }

  // 서버가 끊기면 ack 가 영영 오지 않는다. 잠금을 풀어 두어야 재접속 뒤 패드가
  // 바로 산다(안 그러면 inFlight 가 true 로 굳어 아무 것도 안 보내진다).
  UI.jogReset = function () {
    inFlight = false;
    held = null;
  };

  // ---------------- 패드 ----------------
  document.querySelectorAll('#dlgJog .jogb').forEach(b => {
    const axis = b.dataset.axis, dir = +b.dataset.dir;
    b.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      if (b.disabled) return;
      try { b.setPointerCapture(e.pointerId); } catch (err) { /* 합성 이벤트 */ }
      start(axis, dir, 'pointer');
    });
    ['pointerup', 'pointercancel', 'pointerleave', 'blur'].forEach(ev => {
      b.addEventListener(ev, stop);
    });
  });

  // 키보드: ↑ X+ ↓ X− ← Y+ → Y−. 맵·패드와 같은 방향이다.
  // Z 는 PageUp/PageDown - Z 는 0 이 맨 위라 '위로' 가 음수다.
  const KEYS = {
    ArrowUp: ['x', 1], ArrowDown: ['x', -1],
    ArrowLeft: ['y', 1], ArrowRight: ['y', -1],
    PageUp: ['z', -1], PageDown: ['z', 1],
  };
  dlg.addEventListener('keydown', (e) => {
    // 비모달 dialog 는 Esc 로 닫히지 않는다(모달만 기본 동작이 있다).
    if (e.key === 'Escape') {
      e.preventDefault();
      stop();
      dlg.close();
      return;
    }
    const k = KEYS[e.key];
    if (!k) return;
    if (/^(INPUT|SELECT)$/.test((e.target.tagName || '').toUpperCase())) return;
    e.preventDefault();
    lastKeyAt = Date.now();                // 반복 이벤트마다 갱신(데드맨)
    keySeq += 1;
    if (held && held.axis === k[0] && held.dir === k[1]) return;  // 자동 반복
    start(k[0], k[1], 'key');
  });
  dlg.addEventListener('keyup', (e) => { if (KEYS[e.key]) stop(); });
  dlg.addEventListener('close', stop);

  // 창 밖으로 포커스가 나가면 keyup·pointerup 이 오지 않을 수 있다. 그때도 멈춘다.
  window.addEventListener('keyup', (e) => { if (KEYS[e.key]) stop(); });
  window.addEventListener('blur', stop);
  window.addEventListener('pointercancel', stop);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stop();
  });

  // ---------------- 열기/닫기 ----------------
  // show() 다 - showModal() 로 열면 뒤 화면이 inert 가 되어 헤더의 비상정지를
  // 누를 수 없다. 비상정지는 어떤 상황에서도 눌려야 한다.
  $('btnJog').onclick = () => { if (!dlg.open) dlg.show(); UI.applyJog(UI.state); };
  $('jogClose').onclick = () => dlg.close();

  // ---------------- 나머지 버튼 ----------------
  $('jogGo').onclick = () => {
    const x = parseFloat($('jogGx').value), y = parseFloat($('jogGy').value);
    if (isNaN(x) || isNaN(y)) {
      UI.alert('X 와 Y 를 모두 입력하십시오.', '절대 이동');
      return;
    }
    UI.send({ cmd: 'goto', x: x, y: y });
  };
  $('jogHome').onclick = () => UI.send({ cmd: 'goto', x: 0, y: 0 });
  $('jogPark').onclick = () => UI.send({ cmd: 'park' });
  // 지금 자리를 파킹으로 저장한다(설정에 남는다). fc964d4 에서 이 핸들러가
  // 딸려 나가 버튼이 눌리지 않는 채로 있었다.
  $('jogSavePark').onclick = async () => {
    const st = (UI.state && UI.state.stage) || {};
    if (st.x_mm == null || st.y_mm == null) return;
    const ok = await UI.confirm(
      '파킹 위치를 X' + UI.fmt(st.x_mm) + ' Y' + UI.fmt(st.y_mm) + ' 로 저장합니다.',
      '파킹 위치 저장');
    if (ok) UI.send({ cmd: 'park_here' });
  };
  // 끝단 이동과 원점 등록은 따로다. 밀기는 여러 번 반복하는 조작이라 확인을
  // 묻지 않고, 등록은 되돌리기 어려우므로 묻는다.
  // 한 번에 미는 거리. 펌웨어 한도(50 mm)보다 커도 된다 - 드라이버가 나눠 보낸다.
  // 상한은 그 축의 가동범위다(Z 는 훨씬 짧다). 범위는 서버가 state 로 준다.
  const touchMm = (axis) => {
    const lim = (UI.state && UI.state.limits) || {};
    const top = (axis === 'z') ? Math.floor(lim.z_max_mm || 60) : 248;
    const mm = Math.max(1, Math.min(top, Math.round(+$('jogPushMm').value || 10)));
    $('jogPushMm').value = mm;
    return mm;
  };
  $('jogPushX').onclick = () => UI.send({ cmd: 'touch_end', axis: 'x', mm: touchMm('x') });
  $('jogPushY').onclick = () => UI.send({ cmd: 'touch_end', axis: 'y', mm: touchMm('y') });
  $('jogPushZ').onclick = () => UI.send({ cmd: 'touch_end', axis: 'z', mm: touchMm('z') });
  $('jogZTop').onclick = () => UI.send({ cmd: 'z_top' });

  // 물러나는 거리(2 mm)는 서버가 정한다 - 마커 좌표가 그 원점 기준으로 실측되어
  // 있어서 화면에서 고를 수 있는 값이 아니다.
  async function setOrigin(axis) {
    const body = (axis === 'z' ? '위 끝단' : '끝단')
      + '에서 2 mm 물러난 자리를 ' + axis.toUpperCase()
      + ' 의 0' + (axis === 'z' ? '(맨 위)' : '')
      + ' 으로 등록·저장합니다. '
      + (axis === 'z' ? 'Z 가 위 끝단에' : '캐리지가 끝단에')
      + ' 닿아 있는지 확인 후 진행하십시오.';
    if (await UI.confirm(body, '원점 등록')) {
      UI.send({ cmd: 'set_origin', axis: axis });
    }
  }
  $('jogZeroX').onclick = () => setOrigin('x');
  $('jogZeroY').onclick = () => setOrigin('y');
  $('jogZeroZ').onclick = () => setOrigin('z');

  $('jogEstop').onclick = () => UI.sendEstop();   // 확인 없이 즉시 · WS + HTTP

  // 스테이지 맵 클릭 → 절대 이동 칸 채우기(이동은 [이동] 을 눌러야 한다)
  UI.jogPickXY = function (x, y) {
    if (!dlg.open) return false;
    $('jogGx').value = x.toFixed(1);
    $('jogGy').value = y.toFixed(1);
    return true;
  };

  // ---------------- 상태 ----------------
  UI.applyJog = function (s) {
    const on = UI.online;
    if (!on) UI.jogReset();                // 끊긴 동안 오지 않을 ack 를 기다리지 않는다
    if (!dlg.open) return;
    const st = (s && s.stage) || {}, q = (s && s.sequence) || {};
    const running = ['running', 'paused', 'waiting_confirm', 'parking', 'capturing']
      .indexOf(q.phase) >= 0;
    // usable = 절대 좌표를 말할 수 있는 상태(원점 있음). 패드는 그보다 넓다.
    const homed = !!(st.homed_x && st.homed_y && !st.needs_home);
    const usable = !!(on && st.connected && homed && !running);

    $('jogX').textContent = st.connected ? UI.fmt(st.x_mm) : UI.EMPTY;
    $('jogY').textContent = st.connected ? UI.fmt(st.y_mm) : UI.EMPTY;
    $('jogZ').textContent = st.connected ? UI.fmt(st.z_mm) : UI.EMPTY;
    $('jogMoving').textContent = st.moving ? '이동 중' : '';

    // 패드는 원점이 없어도 열려 있다 - 그래야 끝단까지 몰고 갈 수 있다.
    const canStep = !!(on && st.connected && !running);
    const dis = (id, v) => { const e = $(id); if (e) e.disabled = !!v; };
    document.querySelectorAll('#dlgJog .jogb').forEach(b => { b.disabled = !canStep; });
    canMove = canStep;                     // 패드가 쓰는 값
    if (!canStep) stop();

    // 원점 상태는 축마다 따로 보여 준다.
    const relX = (st.jog_mode_x || 'rel') === 'rel';
    const relY = (st.jog_mode_y || 'rel') === 'rel';
    // 표의 '원점' 칸이다. 축 이름은 왼쪽 칸에 이미 있으므로 상태만 쓴다.
    const badge = (el, rel) => {
      el.textContent = st.connected ? (rel ? '없음' : '등록됨') : UI.EMPTY;
      el.className = 'badge' + (st.connected && rel ? ' warn' : ' ok');
    };
    badge($('jogOriginX'), relX);
    badge($('jogOriginY'), relY);
    badge($('jogOriginZ'), (st.jog_mode_z || 'rel') === 'rel');

    // 절대 좌표는 두 축 원점이 있어야 말이 된다.
    ['jogGo', 'jogHome', 'jogPark'].forEach(id => dis(id, !usable));
    dis('jogGx', !usable); dis('jogGy', !usable);
    dis('jogSavePark', !usable);
    // 끝단 이동·원점 등록은 원점 유무와 무관하다(그것을 만드는 절차다).
    ['jogPushX', 'jogPushY', 'jogPushZ', 'jogPushMm',
     'jogZeroX', 'jogZeroY', 'jogZeroZ'].forEach(id => dis(id, !canStep));
    // Z 는 X·Y 원점과 무관하다 - Z 원점만 있으면 맨 위로 올릴 수 있다.
    dis('jogZTop', !(canStep && (st.jog_mode_z || 'rel') === 'abs'));
    dis('jogEstop', !on);                  // 비상정지는 항상 활성

    let why = '';
    if (!on) why = '서버 연결 끊김';
    else if (!st.connected) why = '스테이지 미연결';
    else if (running) why = '순회 중 · 조작 잠금';
    else if (st.needs_home) why = '비상정지 · 원점 등록 필요';
    else if (!st.homed_x && !st.homed_y) why = '원점 없음 · 끝단까지 민 뒤 원점 등록';
    else if (!st.homed_x) why = 'X 원점 없음 · 끝단까지 민 뒤 원점 등록';
    else if (!st.homed_y) why = 'Y 원점 없음 · 끝단까지 민 뒤 원점 등록';
    $('jogLock').hidden = !why;
    $('jogLock').textContent = why;
  };
})();
