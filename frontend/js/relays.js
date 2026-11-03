/* relays.js — 조작 열의 릴레이 패널.

   사용 중인 릴레이만 한 줄씩: 이름 · 상태 램프 · 켜진 시간 · 남은 시간(제한 있을 때) ·
   켜기/끄기. 맨 위 [전체 끄기]. 상태는 서버 state.relays 가 와야 바뀐다(낙관적
   갱신 없음 - 누른 버튼은 응답이 올 때까지 잠깐 잠근다). 미연결·순회 중에는 잠근다
   (전체 끄기는 순회 중에도 된다). */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$, h = UI.h;
  let skew = 0;                    // 서버 시각 - 화면 시각(초). 켜진 시간 계산용
  const pending = {};              // 보낸 뒤 응답을 기다리는 릴레이 번호

  function now() { return Date.now() / 1000 + skew; }

  function fmtS(s) {
    if (s == null || !isFinite(s)) return UI.EMPTY;
    s = Math.max(0, Math.floor(s));
    const m = Math.floor(s / 60), r = s % 60;
    return m ? (m + ':' + String(r).padStart(2, '0')) : (r + ' s');
  }

  function cfg(s) { return (((s && s.settings) || {}).relay || {}).items || []; }

  UI.applyRelays = function (s) {
    if (s && s.ts) skew = s.ts - Date.now() / 1000;
    const box = $('relayList');
    const items = cfg(s);
    const rel = (s && s.relays) || [];
    const used = items.map((it, i) => ({ it: it, n: i + 1, r: rel[i] || {} }))
      .filter(x => x.it.enabled || x.r.on);      // 켜져 있으면 사용 안 함이어도 보인다
    box.textContent = '';
    if (!used.length) {
      box.appendChild(h('div', { cls: 'note empty', text: '사용 릴레이 없음 · 설정에서 지정' }));
      return;
    }
    used.forEach(x => {
      delete pending[x.n + ':' + (!x.r.on)];
      const lamp = h('i', { cls: 'lamp' + (x.r.on ? ' on' : '') });
      const t = h('span', { cls: 'rt mono', 'data-n': x.n });
      const btn = h('button', { cls: 'btn sm rbtn' + (x.r.on ? ' on' : ''), text: x.r.on ? '끄기' : '켜기',
        'data-n': x.n,
        'data-tip': (x.r.on ? '끕니다.' : '켭니다.') + (x.it.enabled ? '' : ' (사용 안 함 - 켤 수 없음)'),
        onclick: () => {
          pending[x.n + ':' + (!x.r.on)] = true;
          btn.disabled = true;
          UI.send({ cmd: 'relay_set', n: x.n, on: !x.r.on });
        } });
      box.appendChild(h('div', { cls: 'rrow' + (x.r.on ? ' on' : '') }, [
        lamp, h('span', { cls: 'rn', text: x.it.name || ('R' + x.n), title: 'R' + x.n }), t, btn]));
    });
    tick();
    UI.lockRelays(s);
  };

  // 켜진 시간 · 남은 시간(1초마다)
  function tick() {
    const s = UI.state;
    if (!s) return;
    const items = cfg(s);
    document.querySelectorAll('#relayList .rt').forEach(el => {
      const n = +el.getAttribute('data-n');
      const r = (s.relays || [])[n - 1] || {};
      const mx = +((items[n - 1] || {}).max_on_s || 0);
      if (!r.on || !r.since) { el.textContent = mx ? '최대 ' + fmtS(mx) : ''; return; }
      const on = now() - r.since;
      el.textContent = fmtS(on) + (mx ? ' · 남은 ' + fmtS(mx - on) : '');
      el.classList.toggle('late', !!mx && mx - on < 10);
    });
  }
  setInterval(tick, 1000);

  UI.lockRelays = function (s) {
    const on = UI.online, st = (s && s.stage) || {};
    const running = UI.isRunning(s);
    const items = cfg(s);
    let reason = '';
    if (!on) reason = '서버 연결 끊김';
    else if (!st.connected) reason = '스테이지 미연결';
    else if (running) reason = '순회 · 보정 중 · 수동 조작 안 함';
    document.querySelectorAll('#relayList .rbtn').forEach(b => {
      const n = +b.getAttribute('data-n');
      const isOn = b.classList.contains('on');
      const dis = !!reason || (!isOn && !(items[n - 1] || {}).enabled) || !!st.moving;
      b.disabled = dis;
      UI.why(b, reason || (st.moving ? '이동 중' : ''));
    });
    const anyOn = ((s && s.relays) || []).some(r => r.on);
    const off = $('btnRelayOff');
    off.disabled = !on || !st.connected || !anyOn;
    UI.why(off, !on ? '서버 연결 끊김' : (!st.connected ? '스테이지 미연결'
      : (!anyOn ? '켜진 릴레이 없음' : '')));
  };

  $('btnRelayOff').onclick = () => UI.send({ cmd: 'relay_all_off' });
})();
