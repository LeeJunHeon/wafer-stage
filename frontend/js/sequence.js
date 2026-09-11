/* sequence.js — 순회 패널(모드·대기·시작/일시정지/다음/정지·진행·측정·요약). */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$;

  const PHASE = {
    idle: '촬영·검출 필요',
    capturing: '촬영',
    ready: '대기',
    running: '순회',
    paused: '일시정지',
    waiting_confirm: '확인 대기',
    parking: '파킹',
    done: '완료',
    stopped: '정지',
    error: '오류',
  };

  function mode() {
    const el = document.querySelector('input[name=mode]:checked');
    return el ? el.value : 'auto';
  }

  UI.applySequence = function (s) {
    const q = (s && s.sequence) || {};
    // 값 칸은 한 줄로 잘린다(CSS). 전체 문구는 title 로 남긴다.
    const statusTxt = (q.phase === 'idle')
      ? PHASE.idle : (q.message || PHASE[q.phase] || UI.EMPTY);
    $('curStatus').textContent = statusTxt;
    $('curStatus').title = statusTxt;
    const total = q.total || 0, done = q.done || 0;
    $('progText').textContent = total ? (done + ' / ' + total) : UI.EMPTY;
    $('bar').style.width = total ? (100 * done / total) + '%' : '0%';
    $('elapsed').textContent = fmtSec(q.elapsed_s || 0);

    const cur = q.cur_no != null ? (s.samples || []).find(x => x.no === q.cur_no) : null;
    $('curSample').textContent = cur ? ('#' + cur.no) : UI.EMPTY;
    $('curTarget').textContent = cur
      ? ('X ' + cur.X.toFixed(1) + '  Y ' + cur.Y.toFixed(1) + ' mm') : UI.EMPTY;
    const vals = (s.samples || []).filter(x => x.value != null);
    const mv = vals.length
      ? vals.map(x => '#' + x.no + ' ' + x.value + (x.unit || '')).slice(-3).join('  ')
      : '미연결';
    $('measureVal').textContent = mv;
    $('measureVal').title = mv;

    // 모드/대기는 서버 값이 주인이지만, 사용자가 조작 중일 때는 덮지 않는다.
    if (document.activeElement !== $('dwell') && q.dwell_s != null
        && $('dwell').value !== String(q.dwell_s)) {
      if (q.phase !== 'running') $('dwell').value = q.dwell_s;
    }

  };

  function fmtSec(n) {
    n = Math.max(0, Math.round(n));
    const m = Math.floor(n / 60), s = n % 60;
    return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
  }

  $('btnStart').onclick = () => {
    const m = mode();
    if (m === 'pick') {
      UI.alert('번호 선택 모드: 목록에서 샘플을 선택한 뒤 [선택 위치 이동]을 사용하십시오.',
               '번호 선택');
      return;
    }
    UI.send({ cmd: 'run', mode: m, dwell_s: +$('dwell').value || 0 });
  };
  $('btnPause').onclick = () => {
    const q = (UI.state && UI.state.sequence) || {};
    UI.send({ cmd: q.phase === 'paused' ? 'resume' : 'pause' });
  };
  $('btnNext').onclick = () => UI.send({ cmd: 'next' });
  $('btnStop').onclick = () => UI.send({ cmd: 'stop' });
  $('btnEstop').onclick = () => UI.sendEstop();   // 확인 없이 즉시 · WS + HTTP
  $('btnOpenDir').onclick = () => UI.send({ cmd: 'open_out_dir' });
  $('btnExport').onclick = () => UI.send({ cmd: 'open_results' });

  // 서버가 needs_confirm 을 돌려주면 확인 모달을 띄우고 confirm:true 로 다시 보낸다.
  UI.confirmRun = function (reasons) {
    UI.confirm('촬영 상태 확인:\n· ' + reasons.join('\n· ')
      + '\n순회를 시작하시겠습니까?', '확인').then(ok => {
        if (ok) UI.send({ cmd: 'run', mode: mode(), dwell_s: +$('dwell').value || 0,
                          confirm: true });
      });
  };
})();
