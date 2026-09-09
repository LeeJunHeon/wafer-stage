/* samples.js — 샘플 표(측정 대상 토글·선택·이동). */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$;

  const SHAPE = { quad: '사각', triangle: '삼각' };
  const STATUS = {
    wait: '대기', moving: '이동중', measuring: '측정중',
    done: '완료', skip: '제외', error: '오류',
  };

  UI.select = function (no) {
    UI.selected = (UI.selected === no) ? null : no;
    UI.applySamples(UI.state);
    UI.applyCamera(UI.state);
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

      const cells = [
        String(sm.no) + (sm.edge_completed ? ' E' : ''),
        SHAPE[sm.shape] || sm.shape || '',
        sm.X.toFixed(1), sm.Y.toFixed(1),
      ];
      cells.forEach((t, i) => {
        const td = document.createElement('td');
        if (i >= 2) td.className = 'r mono';
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
    const on = list.filter(x => x.on).length;
    $('countInfo').textContent = list.length
      ? (list.length + '개 검출 · ' + on + '개 측정 대상')
      : UI.EMPTY;
  };

  $('btnAll').onclick = () => UI.send({ cmd: 'set_all', on: true });
  $('btnNone').onclick = () => UI.send({ cmd: 'set_all', on: false });
  $('btnGoto').onclick = () => {
    if (UI.selected != null) UI.send({ cmd: 'goto', no: UI.selected });
  };
  $('btnMeasureHere').onclick = () => UI.send({ cmd: 'measure_here' });
})();
