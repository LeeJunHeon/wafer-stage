/* sequence.js — 순회 영역(레시피 선택 · 예상 시간 · 모드 · 시작/일시정지/다음/정지 ·
   진행 · 단계 목록). 레시피 편집은 설정의 측정 순서 탭이다. */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$, h = UI.h;

  const PHASE = {
    idle: '촬영 · 검출 필요',
    capturing: '촬영',
    ready: '대기',
    running: '순회',
    paused: '일시정지',
    waiting_confirm: '확인 대기',
    parking: '정리',
    done: '완료',
    stopped: '정지',
    error: '오류',
  };

  function mode() {
    const el = document.querySelector('input[name=mode]:checked');
    return el ? el.value : 'auto';
  }

  // 화면에 보일 단계 목록: 순회 중이면 서버가 준 목록, 아니면 활성 레시피에서 만든다.
  function stepTexts(s) {
    const q = s.sequence || {};
    if (q.steps && q.steps.length && UI.isRunning(s)) return q.steps;
    const rc = ((s.settings || {}).recipe || {});
    const r = (rc.items || []).find(x => x.name === rc.active) || (rc.items || [])[0];
    if (!r) return [];
    const relays = ((s.settings || {}).relay || {}).items || [];
    const tool = s.tool || {};
    return (r.steps || []).map(st => {
      if (st.type === 'move_sample') return '샘플 위로 이동';
      if (st.type === 'z') return st.target === 'top' ? 'Z 맨 위'
        : (st.target === 'work' ? 'Z 작업 높이 ' + (tool.z_work_mm || 0) + ' mm' : 'Z ' + st.mm + ' mm');
      if (st.type === 'relay') return ((relays[st.n - 1] || {}).name || ('R' + st.n)) + (st.on ? ' 켜기' : ' 끄기');
      if (st.type === 'wait') return '대기 ' + st.s + ' s';
      if (st.type === 'measure') return '측정';
      if (st.type === 'confirm') return '확인 대기';
      return st.type;
    }).concat(mode() === 'confirm' ? ['확인 대기'] : []);
  }

  UI.applySequence = function (s) {
    const q = (s && s.sequence) || {};
    const running = UI.isRunning(s);
    // 레시피 선택(서버 값이 주인 - 사용자가 펼쳐 둔 동안은 덮지 않는다)
    const sel = $('selRecipe');
    const rc = ((s.settings || {}).recipe || {});
    if (document.activeElement !== sel) {
      const names = (rc.items || []).map(r => r.name);
      const cur = [...sel.options].map(o => o.value);
      if (!UI.same(names, cur)) {
        sel.textContent = '';
        names.forEach(n => sel.appendChild(h('option', { value: n, text: n })));
      }
      sel.value = rc.active || '';
    }
    const est = s.estimate || {};
    $('estText').textContent = (est.n
      ? '샘플당 ' + UI.dur(est.sample_s) + ' · 전체 ' + UI.dur(est.total_s) + ' (' + est.n + '개)'
      : '샘플당 ' + UI.dur(est.sample_s)) + (est.confirm ? ' + 확인' : '')
      + ((est.relays || []).length ? ' · 릴레이 ' + est.relays.map(n => 'R' + n).join(',') : '');

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
    $('curSample').textContent = cur
      ? ('#' + cur.no + '  X ' + cur.X.toFixed(1) + '  Y ' + cur.Y.toFixed(1)) : UI.EMPTY;

    // 단계 목록 - 지금 줄 강조, 대기는 남은 초
    const ol = $('stepList');
    ol.textContent = '';
    stepTexts(s).forEach((t, i) => {
      const li = h('li', { text: t });
      if (running && q.step_index === i) {
        li.className = 'cur';
        if (q.step_left_s != null) li.appendChild(h('span', { cls: 'left', text: ' · 남은 ' + Math.ceil(q.step_left_s) + ' s' }));
      }
      ol.appendChild(li);
    });
    const curLi = ol.querySelector('li.cur');
    if (curLi) curLi.scrollIntoView({ block: 'nearest' });

    // 촬영 흐름 안내(설정이 조합한다)
    const g = (k) => k.split('.').reduce((o, p) => (o || {})[p], s.settings || {});
    const flow = [];
    if (g('flow.capture_lift_z')) flow.push('Z↑');
    if (g('flow.capture_goto')) flow.push('촬영 위치');
    if (g('photohome.before_capture')) flow.push('위치 확인');
    flow.push('촬영 · 검출');
    $('flowNote').textContent = flow.join(' → ');
  };

  function fmtSec(n) {
    n = Math.max(0, Math.round(n));
    const m = Math.floor(n / 60), sec = n % 60;
    return String(m).padStart(2, '0') + ':' + String(sec).padStart(2, '0');
  }

  $('selRecipe').addEventListener('change', () => {
    UI.send({ cmd: 'recipe_select', name: $('selRecipe').value });
  });
  document.querySelectorAll('input[name=mode]').forEach(r => r.addEventListener('change', () => {
    if (UI.state) UI.applySequence(UI.state);
  }));

  $('btnStart').onclick = () => {
    const m = mode();
    if (m === 'pick') {
      UI.alert('번호 선택 모드: 목록에서 샘플을 선택한 뒤 [선택 샘플 이동]을 사용하십시오.',
               '번호 선택');
      return;
    }
    UI.send({ cmd: 'run', mode: m });
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

  // 서버가 needs_confirm 을 돌려주면 확인 대화상자를 띄우고 confirm:true 로 다시 보낸다.
  UI.confirmRun = function (reasons) {
    UI.confirm('촬영 상태 확인:\n· ' + reasons.join('\n· ')
      + '\n순회를 시작하시겠습니까?', '확인', '시작').then(ok => {
        if (ok) UI.send({ cmd: 'run', mode: mode(), confirm: true });
      });
  };
})();
