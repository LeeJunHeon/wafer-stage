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
    UI.selected = (UI.selected === no) ? null : no;
    UI.applySamples(UI.state);
    UI.applyCamera(UI.state);
    UI.applyMap(UI.state);
    UI.lock();
  };

  UI.applySamples = function (s) {
    const tb = $('tbody');
    tb.textContent = '';
    const list = (s && s.samples) || [];
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
      if (sm.edge_completed) {
        const badge = document.createElement('span');
        badge.className = 'edgebadge';
        badge.textContent = 'E';
        badge.title = '윤곽 보완(엣지)';
        tdn.appendChild(badge);
      }
      tr.appendChild(tdn);

      const cells = [
        SHAPE[sm.shape] || sm.shape || '',
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
    const on = list.filter(x => x.on).length;
    $('countInfo').textContent = list.length
      ? ('검출 ' + list.length + ' · 대상 ' + on)
      : UI.EMPTY;
  };

  $('btnAll').onclick = () => UI.send({ cmd: 'set_all', on: true });
  $('btnNone').onclick = () => UI.send({ cmd: 'set_all', on: false });
  $('btnGoto').onclick = () => {
    if (UI.selected != null) UI.send({ cmd: 'goto', no: UI.selected });
  };
  $('btnMeasureHere').onclick = () => UI.send({ cmd: 'measure_here' });
})();
