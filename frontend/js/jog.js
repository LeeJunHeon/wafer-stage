/* jog.js — 수동 이동 팝업(조그 패드 · 절대 이동 · 파킹/원점).

   서버가 상태의 주인이다: 화면은 "이 축으로 이만큼" 만 보내고, 목표 좌표와
   가동범위 자르기는 서버가 한다. 위치 숫자도 서버 state 만 그린다.

   누르고 있는 동안 이어서 움직이는 것은 ack{of:"jog"} 를 받은 뒤 다음 스텝을
   보내는 방식이다(한 번에 하나만 보낸다). 타이머로 밀어 넣으면 손을 뗀 뒤에도
   큐에 남은 명령이 계속 실행돼 스테이지가 멋대로 더 간다. */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$;

  const dlg = $('dlgJog');
  let held = null;        // 누르고 있는 방향 {axis, dir}
  let inFlight = false;   // 보낸 조그의 ack 를 기다리는 중인가
  let canMove = false;

  function step() {
    const el = document.querySelector('input[name=jogstep]:checked');
    return el ? +el.value : 1;
  }

  function send(axis, dir) {
    if (!canMove || inFlight) return false;
    inFlight = true;
    if (!UI.send({ cmd: 'jog', axis: axis, delta_mm: dir * step() })) {
      inFlight = false;   // 못 보냈으면 잠금을 풀어 둔다
      return false;
    }
    return true;
  }

  // 서버가 조그를 끝냈다(성공·거절 모두). 아직 누르고 있으면 다음 스텝.
  UI.onJogAck = function () {
    inFlight = false;
    if (held) send(held.axis, held.dir);
  };

  function start(axis, dir) {
    if (!canMove) return;
    held = { axis: axis, dir: dir };
    send(axis, dir);
  }

  function stop() {
    held = null;          // in-flight 인 한 스텝은 끝까지 간다(중간에 못 끊는다)
  }

  // ---------------- 패드 ----------------
  document.querySelectorAll('#dlgJog .jogb').forEach(b => {
    const axis = b.dataset.axis, dir = +b.dataset.dir;
    b.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      if (b.disabled) return;
      try { b.setPointerCapture(e.pointerId); } catch (err) { /* 합성 이벤트 */ }
      start(axis, dir);
    });
    ['pointerup', 'pointercancel', 'pointerleave', 'blur'].forEach(ev => {
      b.addEventListener(ev, stop);
    });
  });

  // 키보드: ↑ X+ ↓ X− ← Y+ → Y−. 맵·패드와 같은 방향이다.
  const KEYS = {
    ArrowUp: ['x', 1], ArrowDown: ['x', -1],
    ArrowLeft: ['y', 1], ArrowRight: ['y', -1],
  };
  dlg.addEventListener('keydown', (e) => {
    const k = KEYS[e.key];
    if (!k) return;
    if (/^(INPUT|SELECT)$/.test((e.target.tagName || '').toUpperCase())) return;
    e.preventDefault();
    if (held && held.axis === k[0] && held.dir === k[1]) return;  // 자동 반복
    start(k[0], k[1]);
  });
  dlg.addEventListener('keyup', (e) => { if (KEYS[e.key]) stop(); });
  dlg.addEventListener('close', stop);
  // Esc 는 dialog 기본 동작으로 닫힌다. 닫히면 누르고 있던 것도 푼다.

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
  $('jogSetHome').onclick = async () => {
    const ok = await UI.confirm(
      '원점 설정: 각 축을 끝단까지 이동합니다(끝에 닿는 소리는 정상).\n'
      + '이동 경로에 프로브·웨이퍼가 없는지 확인 후 진행하십시오.', '원점 설정');
    if (ok) UI.send({ cmd: 'stage_home', axis: 'xy' });
  };
  $('jogSavePark').onclick = async () => {
    const st = (UI.state && UI.state.stage) || {};
    if (st.x_mm == null || st.y_mm == null) return;
    const ok = await UI.confirm(
      '파킹 위치를 X' + UI.fmt(st.x_mm) + ' Y' + UI.fmt(st.y_mm) + ' 로 저장합니다.',
      '파킹 위치 저장');
    if (ok) UI.send({ cmd: 'park_here' });
  };
  $('jogEstop').onclick = () => UI.send({ cmd: 'estop' });   // 확인 없이 즉시

  // 스테이지 맵 클릭 → 절대 이동 칸 채우기(이동은 [이동] 을 눌러야 한다)
  UI.jogPickXY = function (x, y) {
    if (!dlg.open) return false;
    $('jogGx').value = x.toFixed(1);
    $('jogGy').value = y.toFixed(1);
    return true;
  };

  // ---------------- 상태 ----------------
  UI.applyJog = function (s) {
    if (!dlg.open) return;
    const st = (s && s.stage) || {}, q = (s && s.sequence) || {};
    const on = UI.online;
    canMove = !!(on && st.connected && st.homed_x && st.homed_y && !st.needs_home);
    const running = ['running', 'paused', 'waiting_confirm', 'parking', 'capturing']
      .indexOf(q.phase) >= 0;
    const usable = canMove && !running;
    canMove = usable;
    if (!usable) stop();

    $('jogX').textContent = st.connected ? UI.fmt(st.x_mm) : UI.EMPTY;
    $('jogY').textContent = st.connected ? UI.fmt(st.y_mm) : UI.EMPTY;
    $('jogMoving').textContent = st.moving ? '이동 중' : '';

    const dis = (id, v) => { const e = $(id); if (e) e.disabled = !!v; };
    document.querySelectorAll('#dlgJog .jogb').forEach(b => { b.disabled = !usable; });
    ['jogGo', 'jogHome', 'jogPark', 'jogSavePark'].forEach(id => dis(id, !usable));
    dis('jogSetHome', !on || !st.connected || running);
    dis('jogEstop', !on);                  // 비상정지는 항상 활성
    dis('jogGx', !usable); dis('jogGy', !usable);

    let why = '';
    if (!on) why = '서버 연결 끊김';
    else if (!st.connected) why = '스테이지 미연결';
    else if (!(st.homed_x && st.homed_y)) why = '원점 미설정';
    else if (st.needs_home) why = '비상정지 · 원점 설정 필요';
    else if (running) why = '순회 중 · 조작 잠금';
    $('jogLock').hidden = !why;
    $('jogLock').textContent = why;
  };
})();
