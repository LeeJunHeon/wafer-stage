/* measure.js — 실시간 온도 그래프 · 온도/접촉 칩 · 접촉 확인 버튼.
   서버가 {"type":"temp",...} 를 temp.period_s 마다 보낸다(가상 시간 t 초, 온도 c, 히트건 켜짐, 목표선, 단계 라벨, 남은 시간).
   그래프는 최근 구간만 그린다. 값 · 라벨 · 범례는 그래프 선과 겹치지 않게 축 밖 띠에 두고 필요하면 지시선으로 잇는다. */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$;
  const NS = 'http://www.w3.org/2000/svg';
  const W = 360, H = 150;
  const PL = 34, PR = 58, PT = 8, PB = 34;           // 그래프 여백: 오른쪽 띠(목표 · 현재 라벨), 아래 띠(시간축 · 범례)
  const WIN_S = 180;                                  // 보이는 구간(가상 초)
  let hist = [];                                      // {t, c, h}
  let last = null;

  function el(tag, attrs, text) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }

  UI.onTemp = function (m) {
    last = m;
    if (hist.length && m.t < hist[hist.length - 1].t) hist = [];     // 서버 재시작 · 시간 되감김
    hist.push({ t: m.t, c: m.c, h: !!m.heater });
    const t0 = m.t - WIN_S * 1.2;
    while (hist.length && hist[0].t < t0) hist.shift();
    draw();
    nums(m);
  };

  function fmt(v, d) { return v == null ? UI.EMPTY : (+v).toFixed(d == null ? 1 : d); }

  function nums(m) {
    $('tpNow').textContent = m.c == null ? '센서 오류' : fmt(m.c) + ' °C';
    $('tpStep').textContent = m.label || '—';
    $('tpLeft').textContent = m.left_s == null ? '—' : Math.round(m.left_s) + ' s';
  }

  function draw() {
    const svg = $('tempSvg');
    if (!svg) return;
    svg.textContent = '';
    const pw = W - PL - PR, ph = H - PT - PB;
    const tEnd = last ? last.t : 0, tStart = tEnd - WIN_S;
    const vals = hist.filter(p => p.c != null).map(p => p.c);
    if (last && last.target != null) vals.push(+last.target);
    let lo = vals.length ? Math.min.apply(null, vals) : 20, hi = vals.length ? Math.max.apply(null, vals) : 40;
    if (hi - lo < 10) { const mid = (hi + lo) / 2; lo = mid - 5; hi = mid + 5; }
    lo = Math.floor((lo - 2) / 5) * 5; hi = Math.ceil((hi + 2) / 5) * 5;
    const X = t => PL + (t - tStart) / WIN_S * pw;
    const Y = c => PT + (1 - (c - lo) / (hi - lo)) * ph;
    svg.appendChild(el('rect', { class: 'tp-plot', x: PL, y: PT, width: pw, height: ph }));
    // 눈금(축 밖)
    for (let i = 0; i <= 4; i++) {
      const c = lo + (hi - lo) * i / 4;
      svg.appendChild(el('line', { class: 'tp-grid', x1: PL, x2: PL + pw, y1: Y(c), y2: Y(c) }));
      svg.appendChild(el('text', { class: 'tp-ax', x: PL - 4, y: Y(c) + 3, 'text-anchor': 'end' }, Math.round(c)));
    }
    for (let k = 0; k <= 3; k++) {
      const t = tStart + WIN_S * k / 3;
      svg.appendChild(el('text', { class: 'tp-ax', x: X(t), y: PT + ph + 12, 'text-anchor': 'middle' }, Math.round(t - tEnd) + ' s'));
    }
    // 히트건 켜진 구간 음영
    let from = null;
    hist.forEach((p, i) => {
      if (p.h && from == null) from = p.t;
      const end = !p.h || i === hist.length - 1;
      if (from != null && end) {
        const x0 = Math.max(PL, X(from)), x1 = Math.min(PL + pw, X(p.t));
        if (x1 > x0) svg.appendChild(el('rect', { class: 'tp-heat', x: x0, y: PT, width: x1 - x0, height: ph }));
        from = null;
      }
    });
    // 온도선
    const pts = hist.filter(p => p.c != null && p.t >= tStart).map(p => X(p.t) + ',' + Y(p.c)).join(' ');
    if (pts) svg.appendChild(el('polyline', { class: 'tp-line', points: pts }));
    // 목표선(점선) + 오른쪽 띠 라벨(지시선으로 잇는다)
    if (last && last.target != null) {
      const y = Y(+last.target);
      svg.appendChild(el('line', { class: 'tp-target', x1: PL, x2: PL + pw, y1: y, y2: y }));
      svg.appendChild(el('line', { class: 'tp-lead', x1: PL + pw, x2: PL + pw + 8, y1: y, y2: y }));
      svg.appendChild(el('text', { class: 'tp-lab tgt', x: PL + pw + 10, y: y + 3 }, '목표 ' + Math.round(last.target)));
    }
    // 현재값 라벨(오른쪽 띠, 목표 라벨과 겹치면 위로 밀어낸다)
    const cur = hist.length ? hist[hist.length - 1] : null;
    if (cur && cur.c != null) {
      let y = Y(cur.c);
      if (last && last.target != null && Math.abs(y - Y(+last.target)) < 12) y = Y(+last.target) + (cur.c >= +last.target ? -12 : 12);
      svg.appendChild(el('line', { class: 'tp-lead', x1: X(cur.t), x2: PL + pw + 8, y1: Y(cur.c), y2: y }));
      svg.appendChild(el('text', { class: 'tp-lab cur', x: PL + pw + 10, y: y + 3 }, fmt(cur.c) + ' °C'));
    }
    // 범례(아래 띠)
    const ly = H - 8;
    svg.appendChild(el('line', { class: 'tp-line', x1: PL, x2: PL + 14, y1: ly - 3, y2: ly - 3 }));
    svg.appendChild(el('text', { class: 'tp-lab', x: PL + 18, y: ly }, '온도'));
    svg.appendChild(el('line', { class: 'tp-target', x1: PL + 56, x2: PL + 70, y1: ly - 3, y2: ly - 3 }));
    svg.appendChild(el('text', { class: 'tp-lab', x: PL + 74, y: ly }, '목표'));
    svg.appendChild(el('rect', { class: 'tp-heat', x: PL + 112, y: ly - 9, width: 14, height: 9 }));
    svg.appendChild(el('text', { class: 'tp-lab', x: PL + 130, y: ly }, '히트건 켜짐'));
  }

  // ---------------- 칩 · 접촉 확인 ----------------
  UI.applyMeasure = function (s) {
    const m = (s && s.measure) || {};
    const t = $('chipTemp'), c = $('chipContact');
    if (t) {
      if (m.temp_ok === false) UI.chip(t, 'bad', '센서 오류');
      else UI.chip(t, m.tripped ? 'bad' : (m.heater_on ? 'hot' : null), m.temp == null ? UI.EMPTY : m.temp.toFixed(1) + ' °C');
    }
    if (c) {
      const drv = m.contact_driver;
      if (!drv || drv === 'none') UI.chip(c, null, '미사용');
      else UI.chip(c, m.contact ? 'ok' : null, m.contact == null ? UI.EMPTY : (m.contact ? '접촉' : '비접촉'));
    }
    const b = $('btnContact');
    if (b) {
      b.hidden = m.contact_driver !== 'manual';
      b.classList.toggle('on', !!m.contact);
      b.setAttribute('aria-pressed', m.contact ? 'true' : 'false');
    }
    if (m.temp != null && (!last || last.c !== m.temp) && !hist.length) nums({ c: m.temp, label: m.label, left_s: m.left_s });
  };

  const bc = $('btnContact');
  if (bc) bc.onclick = () => UI.send({ cmd: 'contact_manual', on: !((UI.state && UI.state.measure) || {}).contact });
  draw();
})();
