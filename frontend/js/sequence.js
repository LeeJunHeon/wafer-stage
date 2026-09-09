/* sequence.js — 순회 패널(모드·대기·시작/일시정지/다음/정지·진행·측정·요약). */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$;

  const PHASE = {
    idle: '대기 — 촬영·검출부터 하세요',
    capturing: '촬영·검출 중',
    ready: '검토 후 시작',
    running: '순회 중',
    paused: '일시정지',
    waiting_confirm: '확인 대기 — 다음을 누르세요',
    parking: '파킹 중',
    done: '완료',
    stopped: '정지됨',
    error: '오류',
  };

  function mode() {
    const el = document.querySelector('input[name=mode]:checked');
    return el ? el.value : 'auto';
  }

  UI.applySequence = function (s) {
    const q = (s && s.sequence) || {};
    $('runState').textContent = PHASE[q.phase] || q.phase || UI.EMPTY;
    $('curStatus').textContent = q.message || PHASE[q.phase] || UI.EMPTY;
    const total = q.total || 0, done = q.done || 0;
    $('progText').textContent = total ? (done + ' / ' + total) : UI.EMPTY;
    $('bar').style.width = total ? (100 * done / total) + '%' : '0%';
    $('elapsed').textContent = fmtSec(q.elapsed_s || 0);

    const cur = q.cur_no != null ? (s.samples || []).find(x => x.no === q.cur_no) : null;
    $('curSample').textContent = cur ? ('#' + cur.no) : UI.EMPTY;
    $('curTarget').textContent = cur
      ? ('X ' + cur.X.toFixed(1) + '  Y ' + cur.Y.toFixed(1) + ' mm') : UI.EMPTY;
    const vals = (s.samples || []).filter(x => x.value != null);
    $('measureVal').textContent = vals.length
      ? vals.map(x => '#' + x.no + ' ' + x.value + (x.unit || '')).slice(-3).join('  ')
      : '계측기 미연결 — 측정값 없음';

    // 모드/대기는 서버 값이 주인이지만, 사용자가 조작 중일 때는 덮지 않는다.
    if (document.activeElement !== $('dwell') && q.dwell_s != null
        && $('dwell').value !== String(q.dwell_s)) {
      if (q.phase !== 'running') $('dwell').value = q.dwell_s;
    }

    const sum = $('summary'), stp = $('stopped');
    sum.classList.toggle('show', q.phase === 'done');
    if (q.phase === 'done') {
      sum.textContent = '완료: ' + done + '개 · ' + fmtSec(q.elapsed_s || 0)
        + ' · 결과 ' + (q.out_dir || '');
    }
    stp.classList.toggle('show', q.phase === 'stopped' || q.phase === 'error');
    if (q.phase === 'stopped' || q.phase === 'error') {
      stp.textContent = (q.phase === 'error' ? '오류: ' : '정지: ') + (q.message || '');
    }
    if (q.out_dir) $('outDir').textContent = '결과 폴더: ' + q.out_dir;
  };

  function fmtSec(n) {
    n = Math.max(0, Math.round(n));
    const m = Math.floor(n / 60), s = n % 60;
    return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
  }

  $('btnStart').onclick = () => {
    const m = mode();
    if (m === 'pick') {
      UI.alert('번호 선택 모드입니다. 표에서 샘플을 고르고 "선택 샘플로 이동" 을 쓰세요.',
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
  $('btnEstop').onclick = () => UI.send({ cmd: 'estop' });   // 확인 없이 즉시

  // 서버가 needs_confirm 을 돌려주면 확인 모달을 띄우고 confirm:true 로 다시 보낸다.
  UI.confirmRun = function (reasons) {
    UI.confirm('사진에 문제가 있습니다:\n\n· ' + reasons.join('\n· ')
      + '\n\n그래도 순회를 시작할까요?', '확인 필요').then(ok => {
        if (ok) UI.send({ cmd: 'run', mode: mode(), dwell_s: +$('dwell').value || 0,
                          confirm: true });
      });
  };
})();
