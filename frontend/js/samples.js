/* samples.js — 샘플 표(측정 대상 토글·선택·이동). */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$;

  const SHAPE = { quad: '사각', triangle: '삼각' };
  const STATUS = {
    wait: '대기', moving: '이동', measuring: '측정',
    done: '완료', skip: '제외', error: '오류',
  };

  UI.select = function (no) {
    UI.selectNo((UI.selected === no) ? null : no);
  };

  // 지정 선택(토글 아님). 수동 추가 직후 새 번호를 선택할 때 쓴다.
  UI.selectNo = function (no) {
    UI.selected = no;
    UI.applySamples(UI.state);
    UI.applyCamera(UI.state);
    UI.applyMap(UI.state);
    UI.lock();
  };

  UI.applySamples = function (s) {
    const tb = $('tbody');
    tb.textContent = '';
    const list = (s && s.samples) || [];
    // 지운 번호를 선택한 채로 두면 [선택 샘플 이동]이 없는 번호를 보낸다.
    if (UI.selected != null && !list.some(x => x.no === UI.selected)) UI.selected = null;
    list.forEach(sm => {
      const tr = document.createElement('tr');
      if (UI.selected === sm.no) tr.classList.add('selected');
      if (sm.status === 'moving' || sm.status === 'measuring') tr.classList.add('target');
      if (!sm.on) tr.classList.add('unchecked');

      const tdc = document.createElement('td');
      const cb = document.createElement('input');
      cb.type = 'checkbox'; cb.checked = !!sm.on;
      cb.disabled = !UI.online;
      cb.onclick = (e) => {
        e.stopPropagation();
        UI.send({ cmd: 'set_on', no: sm.no, on: cb.checked });
      };
      tdc.appendChild(cb);
      tr.appendChild(tdc);

      // No 칸: 번호 + 윤곽 보완 배지. 'E' 를 번호에 붙여 쓰면 번호의 일부로 읽힌다.
      const tdn = document.createElement('td');
      tdn.appendChild(document.createTextNode(String(sm.no)));
      if (sm.manual) {
        const badge = document.createElement('span');
        badge.className = 'edgebadge man';
        badge.textContent = 'M';
        badge.title = '수동 추가';
        tdn.appendChild(badge);
      }
      if (sm.edge_completed) {
        const badge = document.createElement('span');
        badge.className = 'edgebadge';
        badge.textContent = 'E';
        badge.title = '윤곽 보완(엣지)';
        tdn.appendChild(badge);
      }
      // 엣지 제안(면 대비 없이 가장자리로만 찾음) · 신뢰도 하위 - 사람이 확인할 것
      if (sm.edge_only) {
        const badge = document.createElement('span');
        badge.className = 'edgebadge only';
        badge.textContent = 'G';
        badge.title = '가장자리로만 찾음 · 확인 필요';
        tdn.appendChild(badge);
      }
      if (sm.weak) {
        const badge = document.createElement('span');
        badge.className = 'edgebadge weak';
        badge.textContent = '?';
        badge.title = '신뢰도 낮음(' + (sm.strength != null ? sm.strength : '') + ') · 확인 필요';
        tdn.appendChild(badge);
        tr.classList.add('weak');
      }
      tr.appendChild(tdn);

      const cells = [
        sm.manual ? UI.EMPTY : (SHAPE[sm.shape] || sm.shape || ''),
        sm.X.toFixed(1), sm.Y.toFixed(1),
      ];
      cells.forEach((t, i) => {
        const td = document.createElement('td');
        if (i >= 1) td.className = 'r mono';
        td.textContent = t;
        tr.appendChild(td);
      });
      const tds = document.createElement('td');
      const sp = document.createElement('span');
      sp.className = 'st ' + sm.status;
      sp.textContent = STATUS[sm.status] || sm.status;
      tds.appendChild(sp);
      tr.appendChild(tds);

      const tdv = document.createElement('td');
      tdv.className = 'r mono';
      tdv.textContent = (sm.value == null) ? UI.EMPTY
        : (sm.value + (sm.unit ? ' ' + sm.unit : ''));
      tr.appendChild(tdv);

      tr.onclick = () => UI.select(sm.no);
      tb.appendChild(tr);
    });
    if (!list.length) {
      const tr = document.createElement('tr');
      tr.className = 'emptyrow';
      const td = document.createElement('td');
      td.colSpan = 7;
      td.textContent = '검출된 샘플 없음';
      tr.appendChild(td);
      tb.appendChild(tr);
    }
    // 검출 n · 수동 m · 삭제 d · 대상 k. 검출·대상은 늘 보이고 수동·삭제만 0 이면 뺀다
    // (전체 해제한 '대상 0' 이 사라지면 안 된다).
    const ed = (s && s.edit) || {};
    const del = ed.deleted || 0;
    const parts = [
      ['검출', list.filter(x => !x.manual).length, true],
      ['수동', list.filter(x => x.manual).length, false],
      ['삭제', del, false],
      ['대상', list.filter(x => x.on).length, true],
    ].filter(p => p[2] || p[1] > 0).map(p => p[0] + ' ' + p[1]);
    const cnt = (list.length || del) ? parts.join(' · ') : UI.EMPTY;
    $('countInfo').textContent = cnt;
    $('countInfo').title = cnt;
  };

  $('btnAll').onclick = () => UI.send({ cmd: 'set_all', on: true });
  $('btnNone').onclick = () => UI.send({ cmd: 'set_all', on: false });
  $('btnGoto').onclick = () => {
    if (UI.selected != null) UI.send({ cmd: 'goto', no: UI.selected });
  };
  $('btnMeasureHere').onclick = () => UI.send({ cmd: 'measure_here' });
})();
