/* app.js — 화면 ↔ 서버(WebSocket) 연동.

   - 사용자 동작은 명령(cmd)으로 보낸다. 화면은 서버 state 가 와야 바뀐다.
   - 서버가 끊기면 "연결 끊김" + 모든 값 '—' + 조작 잠금 + 2초마다 재연결.
     가짜 값을 만들지 않는다(화면만 정상으로 보이는 것이 가장 위험하다).
   - 다른 js 다음에 로드된다. */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$, h = UI.h;

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

  const SETTINGS_ACKS = ['settings_schema', 'settings_validate', 'settings_save',
                         'settings_export', 'settings_import'];
  const CALIB_ACKS = ['head_calib', 'head_calib_apply', 'calib_marker', 'calib_marker_apply',
                      'calib_tool', 'calib_tool_heat', 'calib_tool_mark', 'calib_tool_apply',
                      'plate_shot', 'plate_pos', 'plate_calib_fit', 'plate_calib_apply', 'plate_area_apply'];

  function handleAck(msg) {
    if (msg.of === 'run' && !msg.ok) {
      if (msg.reason === 'needs_confirm') UI.confirmRun(msg.needs_confirm || []);
      else if ((msg.needs_confirm || []).length) UI.alert(msg.needs_confirm.join('\n'), '시작 불가');
    }
    if (msg.of === 'capture' && !msg.ok && msg.reason === 'needs_confirm') {
      // 편집(수동 추가·삭제)은 그 촬영에만 속한다. 새로 찍으면 사라지므로 묻는다.
      UI.confirm('수동 추가·삭제 ' + (msg.edits || 0) + '건이 사라집니다. '
        + '새로 촬영하려면 [확인]을 누르십시오.', '촬영 · 검출').then(ok => {
        if (ok) UI.send({ cmd: 'capture', confirm: true });
      });
    }
    // 추가한 샘플을 바로 선택한다 - [선택 샘플 이동]으로 자리를 확인하는 흐름.
    if (msg.of === 'sample_add' && msg.ok && msg.no != null) UI.selectNo(msg.no);
    if (msg.of === 'list_ports') fillPorts(msg.ports || []);
    if (msg.of === 'jog' && UI.onJogAck) UI.onJogAck(msg);
    if (SETTINGS_ACKS.indexOf(msg.of) >= 0 && UI.onSettingsAck) UI.onSettingsAck(msg);
    if (CALIB_ACKS.indexOf(msg.of) >= 0 && UI.onCalibAck) UI.onCalibAck(msg);
    if (msg.of === 'photo_home') onPhotoHome(msg);
    // 거절된 릴레이 명령은 state 가 오지 않는다 - 잠가 둔 버튼을 다시 그린다.
    if (msg.of === 'relay_set' && !msg.ok && UI.state) { UI.applyRelays(UI.state); UI.lock(); }
    if (msg.of === 'tool_select' && !msg.ok && msg.reason !== 'busy') {
      UI.alert('공구를 바꾸지 못했습니다.\n' + Object.values(msg.errors || {}).join('\n'), '공구');
    }
    if (msg.of === 'exit' && msg.ok === false) {
      // 창 X 로 온 종료 요청이 거절됐다. 창은 그대로 두고 이유를 알린다.
      UI.alert(msg.reason === 'busy'
        ? '순회 중에는 종료할 수 없습니다. 정지 후 종료하십시오.'
        : '이동 중에는 종료할 수 없습니다. 완료 후 종료하십시오.', '종료');
    }
  }

  // 사진 호밍: 확인이 필요하면(차이가 경고보다 크거나 원점 없음) 묻고 적용한다.
  function onPhotoHome(msg) {
    if (msg.ok || msg.reason !== 'needs_confirm') return;
    const m = msg.measured || [], r = msg.recorded;
    const lines = ['헤드 마커로 잰 위치  X ' + (+m[0]).toFixed(2) + '  Y ' + (+m[1]).toFixed(2) + ' mm'];
    if (r) {
      lines.push('기록된 위치        X ' + (+r[0]).toFixed(2) + '  Y ' + (+r[1]).toFixed(2) + ' mm');
      lines.push('차이 ' + (+msg.d_mm).toFixed(2) + ' mm (경고 오차보다 큼)');
    } else {
      lines.push('기록된 원점 없음(원점 복구)');
    }
    lines.push('잰 위치로 스테이지 좌표를 맞추고 저장하시겠습니까?');
    UI.confirm(lines.join('\n'), '사진 호밍', '맞추기').then(ok => {
      if (ok) UI.send({ cmd: 'photo_home_apply' });
    });
  }
  $('btnPhotoHome').onclick = () => UI.send({ cmd: 'photo_home' });

  // 시리얼 포트 목록(설정의 포트 칸). 직접 입력도 되므로 <datalist> 로만 붙인다.
  function fillPorts(ports) {
    const dl = $('portList');
    if (!dl) return;
    dl.textContent = '';
    ports.forEach(p => {
      const o = document.createElement('option');
      o.value = p.device;
      o.label = p.description || '';
      dl.appendChild(o);
    });
  }

  // ---------------- 공구 선택 ----------------
  function openToolPick() {
    const s = UI.state || {};
    if (!UI.online || UI.isRunning(s)) return;
    const box = $('toolPick');
    box.textContent = '';
    const tools = (((s.settings || {}).tool || {}).items) || [];
    const act = (s.tool || {}).name;
    tools.forEach(t => {
      box.appendChild(h('button', { cls: 'btn block toolbtn' + (t.name === act ? ' on' : ''),
        onclick: () => { UI.send({ cmd: 'tool_select', name: t.name }); UI.closeWin($('dlgTool')); } }, [
        h('b', { text: t.name }),
        h('span', { cls: 'note', text: 'dX ' + (+t.dx_mm).toFixed(2) + ' · dY ' + (+t.dy_mm).toFixed(2)
          + ' · 작업 높이 ' + (+t.z_work_mm).toFixed(1) + ' mm' })]));
    });
    UI.openWin($('dlgTool'));
  }
  $('chipTool').onclick = openToolPick;
  $('toolClose').onclick = () => UI.closeWin($('dlgTool'));
  $('toolEdit').onclick = () => { UI.closeWin($('dlgTool')); UI.openSettings('tool'); };

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
    UI.applyRelays(s);
    UI.lock();
    if (UI.applyCalib) UI.applyCalib(s);
  }

  // ---------------- 연결 ----------------
  $('btnConnect').onclick = () => {
    const st = (UI.state && UI.state.stage) || {};
    UI.send({ cmd: st.connected ? 'stage_disconnect' : 'stage_connect' });
  };

  UI.setOnline(false);
  connect();
})();
