/* app.js — 화면 ↔ 서버(WebSocket) 연동.

   - 사용자 동작은 명령(cmd)으로 보낸다. 화면은 서버 state 가 와야 바뀐다.
   - 서버가 끊기면 "연결 끊김" + 모든 값 '—' + 조작 잠금 + 2초마다 재연결.
     가짜 값을 만들지 않는다(화면만 정상으로 보이는 것이 가장 위험하다).
   - 다른 js 다음에 로드된다. */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$;

  let ws = null, connected = false, reconnectTimer = null;

  function connect() {
    try {
      ws = new WebSocket('ws://' + location.host + '/ws');
    } catch (e) { scheduleReconnect(); return; }
    ws.onopen = () => { connected = true; UI.setOnline(true); UI.log('서버 연결됨', 'ok'); };
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      handle(msg);
    };
    ws.onclose = () => { connected = false; UI.setOnline(false); scheduleReconnect(); };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, 2000);
  }

  UI.send = function (obj) {
    if (ws && connected) {
      try { ws.send(JSON.stringify(obj)); return true; } catch (e) { return false; }
    }
    UI.log('서버에 연결되지 않아 명령을 보내지 못했습니다', 'warn');
    return false;
  };

  function handle(msg) {
    switch (msg && msg.type) {
      case 'state': applyState(msg); break;
      case 'log': UI.log(msg.msg, msg.level, msg); break;
      case 'ack': handleAck(msg); break;
    }
  }

  function handleAck(msg) {
    if (msg.of === 'run' && !msg.ok) {
      if (msg.reason === 'needs_confirm') UI.confirmRun(msg.needs_confirm || []);
      else if ((msg.needs_confirm || []).length) UI.alert(msg.needs_confirm.join('\n'), '시작 불가');
    }
    if (msg.of === 'list_ports') fillPorts(msg.ports || []);
    if (msg.of === 'jog' && UI.onJogAck) UI.onJogAck(msg);
    if (msg.of === 'exit' && msg.ok === false) {
      // 창 X 로 온 종료 요청이 거절됐다. 창은 그대로 두고 이유를 알린다.
      UI.alert(msg.reason === 'busy'
        ? '순회 중에는 종료할 수 없습니다. 정지 후 종료하십시오.'
        : '이동 중에는 종료할 수 없습니다. 완료 후 종료하십시오.', '종료');
    }
  }

  // 설정창의 시리얼 포트 목록. 직접 입력도 되므로 <datalist> 로만 붙인다.
  function fillPorts(ports) {
    const dl = $('portList');
    dl.textContent = '';
    ports.forEach(p => {
      const o = document.createElement('option');
      o.value = p.device;
      o.label = p.description || '';
      dl.appendChild(o);
    });
  }

  function applyState(s) {
    UI.state = s;
    UI.applyHeader(s);
    UI.applyNotice(s);
    UI.applyFrameNote(s);
    UI.applyCamera(s);
    UI.applyMap(s);
    UI.applySamples(s);
    UI.applySequence(s);
    UI.applyJog(s);
    UI.lock();
    fillSettings(s.settings || {}, s.data_dir);
  }

  // ---------------- 연결 / 설정 ----------------
  $('btnConnect').onclick = () => {
    const st = (UI.state && UI.state.stage) || {};
    UI.send({ cmd: st.connected ? 'stage_disconnect' : 'stage_connect' });
  };

  const dlg = $('dlgSettings');
  let settingsOpen = false;
  function fillSettings(cfg, dataDir) {
    if (settingsOpen) return;             // 편집 중에는 덮어쓰지 않는다
    $('setData').textContent = dataDir || UI.EMPTY;
    $('setPort').value = cfg.serial_port || '';
    $('setCam').value = cfg.camera_index != null ? cfg.camera_index : 1;
    const p = cfg.park_xy || [0, 90];
    $('setParkX').value = p[0]; $('setParkY').value = p[1];
    $('setDwell').value = cfg.dwell_s != null ? cfg.dwell_s : 5;
    $('setDriver').value = (cfg.measure || {}).driver || 'dummy';
    const tb = $('setMarkers');
    tb.textContent = '';
    const mk = cfg.marker_mm_xy || {};
    Object.keys(mk).sort().forEach(id => {
      const tr = document.createElement('tr');
      const td0 = document.createElement('td'); td0.textContent = id; tr.appendChild(td0);
      ['0', '1'].forEach(i => {
        const td = document.createElement('td'); td.className = 'r';
        const inp = document.createElement('input');
        inp.type = 'number'; inp.step = '0.1'; inp.value = mk[id][+i];
        inp.dataset.id = id; inp.dataset.axis = i;
        inp.className = 'mkin';
        td.appendChild(inp); tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
  }
  $('btnSettings').onclick = () => {
    settingsOpen = true;
    UI.send({ cmd: 'list_ports' });      // 열 때마다 최신 목록으로
    dlg.showModal();
  };
  $('btnCloseSettings').onclick = () => { settingsOpen = false; dlg.close(); fillSettings((UI.state || {}).settings || {}, (UI.state || {}).data_dir); };
  $('btnSaveSettings').onclick = () => {
    const mk = {};
    document.querySelectorAll('.mkin').forEach(inp => {
      const id = inp.dataset.id;
      mk[id] = mk[id] || [0, 0];
      mk[id][+inp.dataset.axis] = +inp.value;
    });
    UI.send({
      cmd: 'settings_save',
      serial_port: $('setPort').value.trim() || null,
      camera_index: +$('setCam').value,
      park_xy: [+$('setParkX').value, +$('setParkY').value],
      dwell_s: +$('setDwell').value,
      marker_mm_xy: mk,
      measure: { driver: $('setDriver').value.trim() || 'dummy' },
    });
    settingsOpen = false;
    dlg.close();
  };

  UI.setOnline(false);
  connect();
})();
