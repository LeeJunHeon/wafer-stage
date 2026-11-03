/* calib.js — 보정 창: 헤드 마커 · 판 마커 · 공구 오프셋.

   탭마다 위에서 아래로 번호 매긴 단계 카드(지금 단계 강조, 끝난 단계 체크). 결과는
   표로 보이고, [적용]을 눌러야 저장된다(확인 없이 저장하지 않는다). 진행 중에도
   비상정지(도크)·정지가 된다. 수동 이동 팝업은 이 창 위에 함께 뜬다. */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$, h = UI.h;

  const C = { tab: 'head', marker: '0', method: '1', tool: null, open: false,
              head: null, mk: {}, tl: {}, heat: { n: null, s: 10, z: null } };

  UI.openCalib = function (tab) {
    if (tab) C.tab = tab;
    C.open = true;
    document.querySelector('input[name=caltab][value="' + C.tab + '"]').checked = true;
    UI.openWin($('dlgCal'), { onCancel: tryClose });
    render();
  };

  async function tryClose() {
    const left = (C.head ? 1 : 0) + Object.keys(C.mk).length
      + ((C.tl.d1 || C.tl.d2) ? 1 : 0);
    if (left && !await UI.confirm('적용하지 않은 보정 결과가 있습니다. 닫으면 버립니다.', '보정 닫기', '버리기')) return;
    C.open = false; C.head = null; C.mk = {};
    UI.closeWin($('dlgCal'));
  }
  $('calX').onclick = tryClose;
  $('calClose').onclick = tryClose;
  $('calStop').onclick = () => UI.send({ cmd: 'stop' });
  $('calJog').onclick = () => { $('btnJog').click(); };
  $('btnCalib').onclick = () => UI.openCalib();
  document.querySelectorAll('input[name=caltab]').forEach(r => r.addEventListener('change', () => {
    C.tab = r.value; render();
  }));

  function S() { return UI.state || {}; }
  function st() { return S().stage || {}; }
  function g(k) { return k.split('.').reduce((o, p) => (o || {})[p], S().settings || {}); }
  function homed() { const s = st(); return s.connected && s.homed_x && s.homed_y && !s.needs_home; }
  function busy() { return UI.isRunning(S()); }
  function f2(v) { return v == null ? UI.EMPTY : (+v).toFixed(2); }

  function card(n, title, state, kids) {
    return h('div', { cls: 'card' + (state ? ' ' + state : '') }, [
      h('div', { cls: 'cnum', text: state === 'done' ? '✓' : String(n) }),
      h('div', { cls: 'cbody' }, [h('div', { cls: 'ct', text: title })].concat(kids || []))]);
  }
  function check(ok, text) {
    return h('div', { cls: 'chk' + (ok ? ' ok' : ' no') }, [h('i'), document.createTextNode(text)]);
  }
  function btn(text, onclick, opts) {
    opts = opts || {};
    return h('button', { cls: 'btn' + (opts.primary ? ' primary' : '') + (opts.sm ? ' sm' : ''),
      text: text, disabled: opts.disabled ? 'disabled' : null, 'data-tip': opts.tip || null,
      onclick: onclick });
  }
  // 단계 상태: 앞 단계가 끝났으면 다음이 cur
  function states(doneFlags) {
    let curSet = false;
    return doneFlags.map(d => {
      if (d) return 'done';
      if (!curSet) { curSet = true; return 'cur'; }
      return '';
    });
  }

  // ---------------- 그리기 ----------------
  function render() {
    if (!C.open) return;
    ['head', 'marker', 'tool'].forEach(t => {
      $('cal' + t[0].toUpperCase() + t.slice(1)).hidden = t !== C.tab;
    });
    if (C.tab === 'head') renderHead();
    else if (C.tab === 'marker') renderMarker();
    else renderTool();
    footer();
  }

  function footer() {
    const job = S().job || {};
    $('calStatus').textContent = job.running ? (job.message || '진행 중')
      : (job.message || (homed() ? '준비' : '스테이지 연결 · 원점이 필요합니다(원점이 없으면 사진 호밍 또는 수동 이동)'));
    $('calStop').disabled = !job.running;
  }

  function renderHead() {
    const box = $('calHead');
    box.textContent = '';
    const ph = S().photohome || {};
    const cal = ph.calib;
    box.appendChild(h('div', { cls: 'calnow' }, [
      h('b', { text: '현재 보정 ' }),
      document.createTextNode(cal
        ? (cal.created + ' · RMS ' + f2(cal.rms_mm) + ' mm · ' + cal.n + '점 · '
           + (ph.calib_valid ? '유효' : '무효(' + ph.calib_reason + ')'))
        : '없음 · 사진 호밍을 쓰려면 먼저 보정')]));
    const job = S().job || {};
    const running = job.running && job.kind === 'head_calib';
    const ready = homed() && !busy();
    const n = g('photohome.grid_n') || 3;
    const res = C.head || (job.kind === 'head_calib' && job.result) || null;
    const sts = states([ready || running || !!res, !!res && !running, false]);
    box.appendChild(card(1, '준비', sts[0], [
      check(!!st().connected, '스테이지 연결'),
      check(homed(), 'X · Y 원점 (없으면 사진 호밍 불가 - 수동 원점으로 먼저)'),
      h('div', { cls: 'note', text: '헤드 마커 id' + g('photohome.marker_id') + ' · ' + g('photohome.marker_mm')
        + ' mm 를 Z 모터 윗면(높이가 변하지 않는 곳)에 붙였는지 확인 · 격자 ' + n + '×' + n
        + ' · 여백 ' + g('photohome.grid_margin_mm') + ' mm · 점마다 Z 맨 위' }),
    ]));
    const bar = h('div', { cls: 'progress' }, [h('i')]);
    if (job.kind === 'head_calib' && job.n) bar.firstChild.style.width = (100 * (job.i || 0) / job.n) + '%';
    box.appendChild(card(2, '격자 촬영', sts[1], [
      h('div', { cls: 'row' }, [
        btn(running ? '진행 중' : '보정 시작', () => { C.head = null; UI.send({ cmd: 'head_calib_start' }); },
            { primary: !res, disabled: !ready || running,
              tip: '격자 점을 차례로 돌며 헤드 마커를 찍습니다. 정지 · 비상정지로 멈출 수 있습니다.' }),
        h('span', { cls: 'note grow1', text: job.kind === 'head_calib' ? (job.message || '') : '' })]),
      bar]));
    const kids = [];
    if (res) {
      const tb = h('tbody');
      (res.points || []).forEach((p, i) => tb.appendChild(h('tr', { cls: p.res_mm > (res.rms_limit_mm || 0.5) ? 'bad' : '' }, [
        h('td', { text: String(i + 1) }), h('td', { text: f2(p.x) }), h('td', { text: f2(p.y) }),
        h('td', { text: (+p.u).toFixed(1) }), h('td', { text: (+p.v).toFixed(1) }),
        h('td', { text: (+p.res_mm).toFixed(3) })])));
      kids.push(h('div', { cls: 'restbl' }, [h('table', { cls: 'etable' }, [
        h('thead', {}, [h('tr', {}, ['#', 'X mm', 'Y mm', 'u px', 'v px', '잔차 mm'].map(t => h('th', { text: t })))]), tb])]));
      kids.push(h('div', { cls: res.ok ? 'okline' : 'warnline',
        text: 'RMS ' + (+res.rms_mm).toFixed(3) + ' mm · 최대 ' + (+res.max_mm).toFixed(3) + ' mm'
          + (res.ok ? '' : ' · 한계 ' + res.rms_limit_mm + ' mm 초과 · 다시 권장') }));
      kids.push(h('div', { cls: 'row' }, [
        btn('적용', () => UI.send({ cmd: 'head_calib_apply' }), { primary: res.ok,
          tip: '이 보정을 저장합니다(카메라 번호 · 해상도도 함께 기록).' }),
        btn('다시', () => { C.head = null; UI.send({ cmd: 'head_calib_start' }); },
            { primary: !res.ok, disabled: !ready, tip: '격자 촬영을 처음부터 다시 합니다.' })]));
    } else {
      kids.push(h('div', { cls: 'note', text: '촬영이 끝나면 점별 잔차와 RMS 가 여기에 나옵니다.' }));
    }
    box.appendChild(card(3, '결과 확인 · 적용', res ? 'cur' : '', kids));
  }

  function renderMarker() {
    const box = $('calMarker');
    box.textContent = '';
    const mk = g('marker_mm_xy') || {};
    const seg = h('div', { cls: 'seg segsm' });
    ['0', '1', '2', '3'].forEach(id => {
      const inp = h('input', { type: 'radio', name: 'calmk', value: id });
      inp.checked = C.marker === id;
      inp.addEventListener('change', () => { C.marker = id; render(); });
      seg.appendChild(h('label', {}, [inp, document.createTextNode('id' + id)]));
    });
    const cur = mk[C.marker];
    const r = C.mk[C.marker];
    const sts = states([true, homed() && !!r, !!r, false]);
    box.appendChild(card(1, '마커 선택', sts[0], [seg,
      h('div', { cls: 'note', text: '지금 값 id' + C.marker + ' · X ' + f2(cur && cur[0]) + ' · Y ' + f2(cur && cur[1])
        + ' · 카메라 위치가 바뀌면 네 개 모두 다시 잡습니다.' })]));
    box.appendChild(card(2, '기준 포인터를 마커 중심에', sts[1], [
      h('div', { cls: 'note', text: '수동 이동으로 기준 포인터 끝을 마커 id' + C.marker + ' 의 중심에 맞춥니다. 공구와 무관하게 스테이지 좌표(기준 포인터)를 기록합니다.' }),
      h('div', { cls: 'row' }, [btn('수동 이동', () => $('btnJog').click(), { sm: true }),
        h('span', { cls: 'mono', text: '현재 X ' + f2(st().x_mm) + ' · Y ' + f2(st().y_mm) })])]));
    box.appendChild(card(3, '기록', sts[2], [
      h('div', { cls: 'row' }, [btn('기록', () => UI.send({ cmd: 'calib_marker_record', id: C.marker }),
        { primary: !r, disabled: !homed() || busy(), tip: '지금 위치를 이 마커의 새 좌표 후보로 기록합니다(아직 저장 안 함).' })]),
      r ? h('table', { cls: 'etable narrow' }, [h('tbody', {}, [
        h('tr', {}, [h('td', { text: '이전' }), h('td', { text: f2(r.old && r.old[0]) }), h('td', { text: f2(r.old && r.old[1]) })]),
        h('tr', {}, [h('td', { text: '새 값' }), h('td', { text: f2(r.new[0]) }), h('td', { text: f2(r.new[1]) })]),
        h('tr', {}, [h('td', { text: '차이' }), h('td', { text: r.diff_mm == null ? UI.EMPTY : f2(r.diff_mm) + ' mm' }), h('td')])])]) : null]));
    box.appendChild(card(4, '적용', r ? 'cur' : '', [
      h('div', { cls: 'row' }, [btn('적용', () => UI.send({ cmd: 'calib_marker_apply', id: C.marker }),
        { primary: true, disabled: !r, tip: '새 값을 판 마커 좌표로 저장합니다. 다음 촬영부터 씁니다.' })])]));
  }

  function renderTool() {
    const box = $('calTool');
    box.textContent = '';
    const tools = (g('tool.items') || []).filter(t => t.name !== '기준 포인터');
    if (!C.tool || !tools.some(t => t.name === C.tool)) {
      const act = (S().tool || {}).name;
      C.tool = tools.some(t => t.name === act) ? act : (tools[0] || {}).name;
    }
    const sel = h('select', { cls: 'in' });
    tools.forEach(t => sel.appendChild(h('option', { value: t.name, text: t.name })));
    sel.value = C.tool || '';
    sel.addEventListener('change', () => { C.tool = sel.value; render(); });
    const seg = h('div', { cls: 'seg segsm' });
    [['1', '방법 1 · 맞추기'], ['2', '방법 2 · 자국']].forEach(o => {
      const inp = h('input', { type: 'radio', name: 'calmeth', value: o[0] });
      inp.checked = C.method === o[0];
      inp.addEventListener('change', () => { C.method = o[0]; render(); });
      seg.appendChild(h('label', {}, [inp, document.createTextNode(o[1])]));
    });
    const t = tools.find(x => x.name === C.tool) || {};
    box.appendChild(h('div', { cls: 'erow' }, [h('span', { cls: 'note', text: '공구' }), sel, seg,
      h('span', { cls: 'note', text: '지금 오프셋 dX ' + f2(t.dx_mm) + ' · dY ' + f2(t.dy_mm) })]));
    if (!tools.length) {
      box.appendChild(h('div', { cls: 'note', text: '기준 포인터 말고 공구가 없습니다 · 설정의 공구 탭에서 추가' }));
      return;
    }
    const r = C.tl;
    if (C.method === '1') {
      const sts = states([!!r.A, !!r.B, false]);
      box.appendChild(card(1, '기준 포인터를 기준점 위에', sts[0], [
        h('div', { cls: 'note', text: '판 위 표시점(십자 등) 위에 기준 포인터 끝을 수동 이동으로 맞춘 뒤 기록합니다.' }),
        h('div', { cls: 'row' }, [btn('기록 A', () => UI.send({ cmd: 'calib_tool_record', which: 'A' }),
          { primary: !r.A, disabled: !homed() || busy() }),
          h('span', { cls: 'mono', text: r.A ? 'A  X ' + f2(r.A[0]) + ' · Y ' + f2(r.A[1]) : '' })])]));
      box.appendChild(card(2, '공구 끝을 같은 점에', sts[1], [
        h('div', { cls: 'note', text: '공구를 달고, 공구 끝이 같은 표시점을 가리키도록 수동 이동으로 맞춘 뒤 기록합니다(필요하면 Z 를 내려 맞춤).' }),
        h('div', { cls: 'row' }, [btn('기록 B', () => UI.send({ cmd: 'calib_tool_record', which: 'B' }),
          { primary: !!r.A && !r.B, disabled: !homed() || busy() }),
          h('span', { cls: 'mono', text: r.B ? 'B  X ' + f2(r.B[0]) + ' · Y ' + f2(r.B[1]) : '' })])]));
      box.appendChild(card(3, '결과 · 적용', r.d1 ? 'cur' : '', [
        h('div', { cls: 'mono big', text: r.d1 ? 'd = A − B = dX ' + f2(r.d1[0]) + ' · dY ' + f2(r.d1[1]) + ' mm' : UI.EMPTY }),
        h('div', { cls: 'row' }, [btn('적용', () => UI.send({ cmd: 'calib_tool_apply', method: '1', tool: C.tool }),
          { primary: true, disabled: !r.d1, tip: "이 오프셋을 '" + C.tool + "' 에 저장합니다." })])]));
    } else {
      const relays = (g('relay.items') || []).map((x, i) => ({ n: i + 1, it: x })).filter(x => x.it.enabled);
      if (C.heat.n == null || !relays.some(x => x.n === C.heat.n)) C.heat.n = relays.length ? relays[0].n : null;
      if (C.heat.z == null) C.heat.z = t.z_work_mm || 0;
      const rs = h('select', { cls: 'in' });
      relays.forEach(x => rs.appendChild(h('option', { value: x.n, text: 'R' + x.n + ' ' + (x.it.name || '') })));
      if (C.heat.n) rs.value = String(C.heat.n);
      rs.addEventListener('change', () => { C.heat.n = +rs.value; });
      const sec = h('input', { type: 'number', cls: 'in num', min: 1, max: 600, step: 1, value: C.heat.s });
      sec.addEventListener('input', () => { C.heat.s = +sec.value; });
      const zz = h('input', { type: 'number', cls: 'in num', min: 0, step: 0.1, value: C.heat.z });
      zz.addEventListener('input', () => { C.heat.z = +zz.value; });
      const job = S().job || {};
      const heating = job.running && job.kind === 'tool_heat';
      const sts = states([!!r.P0, !!r.P0 && !!(S().frame) && !!r.shot, !!r.M, false]);
      box.appendChild(card(1, '단독 가열', sts[0], [
        h('div', { cls: 'note', text: '감열지 등을 놓고 지금 자리(P0)에서 레시피 없이 가열합니다: Z 내림 → 릴레이 켜기 → 대기 → 끄기 → Z 맨 위.' }),
        h('div', { cls: 'row' }, [h('span', { cls: 'note', text: '릴레이' }), rs,
          h('span', { cls: 'note', text: '시간' }), sec, h('span', { cls: 'unit', text: 's' }),
          h('span', { cls: 'note', text: 'Z' }), zz, h('span', { cls: 'unit', text: 'mm' }),
          btn(heating ? '가열 중' : '가열', () => UI.send({ cmd: 'calib_tool_heat', n: C.heat.n, s: C.heat.s, z_mm: C.heat.z }),
            { primary: !r.P0, disabled: !homed() || busy() || !C.heat.n,
              tip: relays.length ? '지금 위치를 P0 로 기록하고 가열합니다.' : '사용 릴레이가 없습니다(설정 → 릴레이).' })]),
        h('div', { cls: 'mono', text: r.P0 ? 'P0  X ' + f2(r.P0[0]) + ' · Y ' + f2(r.P0[1]) : (heating ? job.message : '') })]));
      box.appendChild(card(2, '공구를 치우고 촬영', sts[1], [
        h('div', { cls: 'note', text: '촬영은 설정된 흐름대로 촬영 위치로 가서 찍습니다(공구가 시야에서 비켜납니다).' }),
        h('div', { cls: 'row' }, [btn('촬영 · 검출', () => { C.tl.shot = true; UI.send({ cmd: 'capture', confirm: true }); },
          { disabled: !r.P0 || busy() })])]));
      const fr = S().frame;
      const imgBox = h('div', { cls: 'markimg' });
      if (fr && r.P0) {
        const img = h('img', { src: fr.url, alt: '촬영본' });
        img.addEventListener('click', (ev) => {
          const b = img.getBoundingClientRect();
          const u = (ev.clientX - b.left) / b.width * fr.w;
          const v = (ev.clientY - b.top) / b.height * fr.h;
          C.tl.MpxPending = [u, v];        // 서버 ack 에는 mm 만 온다 - 점은 여기서 기억
          UI.send({ cmd: 'calib_tool_mark', u: Math.round(u * 10) / 10, v: Math.round(v * 10) / 10 });
        });
        imgBox.appendChild(img);
        if (r.Mpx) {
          imgBox.appendChild(h('i', { cls: 'markdot' }));
          const dot = imgBox.lastChild;
          dot.style.left = (100 * r.Mpx[0] / fr.w) + '%';
          dot.style.top = (100 * r.Mpx[1] / fr.h) + '%';
        }
      } else {
        imgBox.appendChild(h('div', { cls: 'note', text: '가열과 촬영 뒤 여기에 촬영본이 나옵니다.' }));
      }
      box.appendChild(card(3, '자국 중심 클릭', sts[2], [imgBox,
        h('div', { cls: 'mono', text: r.M ? 'M  X ' + f2(r.M[0]) + ' · Y ' + f2(r.M[1]) : '' })]));
      box.appendChild(card(4, '결과 · 적용', r.d2 ? 'cur' : '', [
        h('div', { cls: 'mono big', text: r.d2 ? 'd = M − P0 = dX ' + f2(r.d2[0]) + ' · dY ' + f2(r.d2[1]) + ' mm' : UI.EMPTY }),
        h('div', { cls: 'row' }, [btn('적용', () => UI.send({ cmd: 'calib_tool_apply', method: '2', tool: C.tool }),
          { primary: true, disabled: !r.d2, tip: "이 오프셋을 '" + C.tool + "' 에 저장합니다." })])]));
    }
  }

  // ---------------- 서버 응답 ----------------
  UI.onCalibAck = function (msg) {
    const of = msg.of;
    if (of === 'head_calib' && msg.ok) C.head = msg.result;
    if (of === 'head_calib' && !msg.ok && msg.reason !== 'busy' && (msg.needs_confirm || []).length) {
      UI.alert(msg.needs_confirm.join('\n'), '헤드 보정');
    }
    if (of === 'head_calib_apply' && msg.ok) C.head = null;
    if (of === 'calib_marker' && msg.ok) C.mk[msg.id] = { new: msg.new, old: msg.old, diff_mm: msg.diff_mm };
    if (of === 'calib_marker_apply' && msg.ok) delete C.mk[msg.id];
    if ((of === 'calib_tool' || of === 'calib_tool_heat' || of === 'calib_tool_mark') && msg.ok && msg.result) {
      const shot = C.tl.shot;
      const mpx = of === 'calib_tool_mark' ? C.tl.MpxPending : (of === 'calib_tool_heat' ? null : C.tl.Mpx);
      C.tl = Object.assign({}, msg.result, { shot: of === 'calib_tool_heat' ? false : shot, Mpx: mpx });
    }
    if (of === 'calib_tool_apply' && msg.ok) {
      UI.alert("공구 '" + msg.tool + "' 오프셋 dX " + f2(msg.d[0]) + ' · dY ' + f2(msg.d[1]) + ' mm 저장', '공구 오프셋');
    }
    if (!msg.ok && msg.reason && ['locked', 'invalid', 'no_heat', 'no_capture', 'error'].indexOf(msg.reason) >= 0
        && of !== 'head_calib') {
      const why = (msg.needs_confirm || []).join('\n') || Object.values(msg.errors || {}).join('\n') || msg.reason;
      UI.alert(why, '보정');
    }
    render();
  };

  // 입력 중(칸에 포커스)에는 다시 그리지 않는다 - 상태가 올 때마다 그리면 입력이 끊긴다.
  UI.applyCalib = function () {
    if (!C.open) return;
    const a = document.activeElement;
    if (a && $('dlgCal').contains(a) && /^(INPUT|SELECT)$/.test(a.tagName)) { footer(); return; }
    render();
  };
})();
