/* editors.js — 설정 스키마의 전용 편집기(editor 필드).

   설정 창(settings.js)이 항목의 editor 이름으로 여기의 render(el, item, ctx) 를 부른다.
   ctx: get(key) · set(key, value) · def(key) · errs(prefix) → [[key, 문구]] ·
        rerender() · state(서버 state) · send(obj)
   값은 ctx.set 으로만 바꾼다(설정 창이 변경 표시 · 검증을 맡는다). 구조가 바뀌는
   조작(추가 · 삭제 · 순서)은 rerender 로 다시 그리고, 칸 입력은 다시 그리지 않는다
   (입력 중 포커스를 잃지 않게). */
(function () {
  'use strict';
  const UI = window.UI;
  const E = UI.editors = {};

  const h = UI.h;

  function num(v) {
    if (v === '' || v == null) return v;
    const f = Number(v);
    return isFinite(f) ? f : v;            // 숫자가 아니면 그대로 - 서버가 사유를 돌려준다
  }

  function errCell(ctx, key) {
    const list = ctx.errs(key);
    return list.length ? list[0][1] : '';
  }

  function markErr(el, msg) {
    el.classList.toggle('bad', !!msg);
    if (msg) el.setAttribute('title', msg); else el.removeAttribute('title');
  }

  // ---------------- 시리얼 포트 ----------------
  E.port = {
    render(el, item, ctx) {
      const inp = h('input', { type: 'text', cls: 'in', list: 'portList', placeholder: '자동',
                               value: ctx.get(item.key) || '' });
      inp.addEventListener('input', () => ctx.set(item.key, inp.value.trim() || null));
      el.appendChild(inp);
      if (!document.getElementById('portList')) el.appendChild(h('datalist', { id: 'portList' }));
      ctx.send({ cmd: 'list_ports' });     // 열 때마다 최신 목록으로
    },
  };

  // ---------------- 릴레이 10개 ----------------
  const WIRE = ['노', '주', '보', '갈', '파', '녹', '노', '주', '빨', '검'];
  E.relays = {
    render(el, item, ctx) {
      const rows = ctx.get(item.key) || [];
      const tb = h('tbody');
      rows.forEach((r, i) => {
        const p = item.key + '[' + i + ']';
        const cb = h('input', { type: 'checkbox' });
        cb.checked = !!r.enabled;
        cb.addEventListener('change', () => { r.enabled = cb.checked; ctx.set(item.key, rows); });
        const nm = h('input', { type: 'text', cls: 'in', value: r.name || '', maxlength: 20 });
        nm.addEventListener('input', () => { r.name = nm.value; ctx.set(item.key, rows); });
        const mx = h('input', { type: 'number', cls: 'in num', min: 0, max: 86400, step: 1,
                                value: r.max_on_s });
        mx.addEventListener('input', () => { r.max_on_s = num(mx.value); ctx.set(item.key, rows); });
        markErr(nm, errCell(ctx, p + '.name'));
        markErr(mx, errCell(ctx, p + '.max_on_s'));
        tb.appendChild(h('tr', {}, [
          h('td', { cls: 'mono', text: 'R' + (i + 1) }),
          h('td', {}, [cb]),
          h('td', {}, [nm]),
          h('td', {}, [mx, h('span', { cls: 'unit', text: 's' })]),
          h('td', { cls: 'note', text: 'D' + (22 + 2 * i) + ' ' + WIRE[i] }),
        ]));
      });
      el.appendChild(h('table', { cls: 'etable' }, [
        h('thead', {}, [h('tr', {}, [h('th', { text: '번호' }), h('th', { text: '사용' }),
          h('th', { text: '이름' }), h('th', { text: '최대 켜짐 (0 = 제한 없음)' }),
          h('th', { text: '배선' })])]),
        tb]));
    },
  };

  // ---------------- 공구 목록 ----------------
  const POINTER = '기준 포인터';
  // 끝 모양(tip_*)이 없는 예전 공구는 이름별 기본값을 보여 준다(서버 core/tip.py 와 같은 값)
  function tipDefaults(t) {
    return t.name === POINTER ? { tip_shape: 'rect', tip_w_mm: 10, tip_h_mm: 2, tip_angle_deg: 90 }
                              : { tip_shape: 'circle', tip_w_mm: 5, tip_h_mm: 5, tip_angle_deg: 0 };
  }
  function tipShape(t, item, ctx, rows) {
    const d = tipDefaults(t);
    const sel = h('select', { cls: 'in' });
    [['rect', '사각'], ['circle', '원']].forEach(o => sel.appendChild(h('option', { value: o[0], text: o[1] })));
    sel.value = t.tip_shape || d.tip_shape;
    sel.addEventListener('change', () => {
      Object.keys(d).forEach(k => { if (t[k] == null) t[k] = d[k]; });
      t.tip_shape = sel.value;
      if (sel.value === 'circle') t.tip_h_mm = t.tip_w_mm;
      ctx.set(item.key, rows); ctx.rerender();
    });
    return sel;
  }
  function tcell(f, step, t, item, ctx, rows, p, off) {
    const d = tipDefaults(t);
    const inp = h('input', { type: 'number', cls: 'in num', step: step, value: t[f] != null ? t[f] : d[f],
                             disabled: off ? 'disabled' : null,
                             'data-tip': off ? '원은 지름(가로) 하나만 씁니다.' : null });
    inp.addEventListener('input', () => {
      Object.keys(d).forEach(k => { if (t[k] == null) t[k] = d[k]; });
      t[f] = num(inp.value); ctx.set(item.key, rows);
    });
    markErr(inp, errCell(ctx, p + '.' + f));
    return inp;
  }
  E.tools = {
    render(el, item, ctx) {
      const rows = ctx.get(item.key) || [];
      const tb = h('tbody');
      const active = ctx.get('tool.active');
      rows.forEach((t, i) => {
        const p = item.key + '[' + i + ']';
        const isPtr = t.name === POINTER && i === rows.findIndex(x => x.name === POINTER);
        const nm = h('input', { type: 'text', cls: 'in', value: t.name || '', maxlength: 20,
                                readonly: isPtr ? 'readonly' : null });
        nm.addEventListener('input', () => {
          const old = t.name;
          t.name = nm.value;
          ctx.set(item.key, rows);
          if (ctx.get('tool.active') === old) ctx.set('tool.active', t.name);
        });
        const cell = (f, step) => {
          const inp = h('input', { type: 'number', cls: 'in num', step: step, value: t[f] });
          inp.addEventListener('input', () => { t[f] = num(inp.value); ctx.set(item.key, rows); });
          markErr(inp, errCell(ctx, p + '.' + f));
          return inp;
        };
        const note = h('input', { type: 'text', cls: 'in', value: t.note || '', maxlength: 80 });
        note.addEventListener('input', () => { t.note = note.value; ctx.set(item.key, rows); });
        markErr(nm, errCell(ctx, p + '.name'));
        const dup = h('button', { cls: 'btn sm', text: '복제', 'data-tip': '이 공구를 복사해 새 공구를 만듭니다.',
          onclick: () => {
            let n = 2, name = t.name + ' ' + n;
            while (rows.some(x => x.name === name)) { n += 1; name = t.name + ' ' + n; }
            rows.splice(i + 1, 0, Object.assign(UI.clone(t), { name: name }));
            ctx.set(item.key, rows); ctx.rerender();
          } });
        const del = h('button', { cls: 'btn sm', text: '삭제', disabled: isPtr ? 'disabled' : null,
          'data-tip': isPtr ? '기준 포인터는 지울 수 없습니다.' : '이 공구를 목록에서 뺍니다.',
          onclick: () => {
            rows.splice(i, 1);
            ctx.set(item.key, rows);
            if (ctx.get('tool.active') === t.name) ctx.set('tool.active', POINTER);
            ctx.rerender();
          } });
        tb.appendChild(h('tr', { cls: t.name === active ? 'act' : '' }, [
          h('td', {}, [nm]),
          h('td', {}, [cell('dx_mm', 0.01)]), h('td', {}, [cell('dy_mm', 0.01)]),
          h('td', {}, [cell('z_work_mm', 0.1)]),
          h('td', {}, [tipShape(t, item, ctx, rows)]),
          h('td', {}, [tcell('tip_w_mm', 0.1, t, item, ctx, rows, p)]),
          h('td', {}, [tcell('tip_h_mm', 0.1, t, item, ctx, rows, p, t.tip_shape === 'circle')]),
          h('td', {}, [tcell('tip_angle_deg', 1, t, item, ctx, rows, p, t.tip_shape === 'circle')]),
          h('td', {}, [note]),
          h('td', {}, [h('div', { cls: 'cellrow' }, [dup, del])]),
        ]));
      });
      el.appendChild(h('table', { cls: 'etable' }, [
        h('thead', {}, [h('tr', {}, [h('th', { text: '이름' }), h('th', { text: 'dX mm' }),
          h('th', { text: 'dY mm' }), h('th', { text: '작업 높이 mm' }), h('th', { text: '끝 모양' }), h('th', { text: '끝 가로 mm' }),
          h('th', { text: '끝 세로 mm' }), h('th', { text: '각도 °' }), h('th', { text: '메모' }),
          h('th', { text: '' })])]),
        tb]));
      const add = h('button', { cls: 'btn sm', text: '공구 추가', 'data-tip': '새 공구를 목록 끝에 더합니다.',
        onclick: () => {
          let n = rows.length + 1, name = '공구 ' + n;
          while (rows.some(x => x.name === name)) { n += 1; name = '공구 ' + n; }
          rows.push({ name: name, dx_mm: 0, dy_mm: 0, z_work_mm: 0, tip_shape: 'circle', tip_w_mm: 5, tip_h_mm: 5,
                      tip_angle_deg: 0, note: '' });
          ctx.set(item.key, rows); ctx.rerender();
        } });
      el.appendChild(h('div', { cls: 'erow' }, [add,
        h('span', { cls: 'note', text: '공구 끝 = 스테이지 좌표 + (dX, dY) · 샘플을 겨냥할 때 스테이지는 샘플 − (dX, dY)' })]));
    },
  };

  function nameSelect(el, item, ctx, listKey) {
    const names = (ctx.get(listKey) || []).map(x => x && x.name).filter(Boolean);
    const sel = h('select', { cls: 'in' });
    names.forEach(n => sel.appendChild(h('option', { value: n, text: n })));
    sel.value = ctx.get(item.key);
    sel.addEventListener('change', () => ctx.set(item.key, sel.value));
    el.appendChild(sel);
  }
  E.tool_active = { render(el, item, ctx) { nameSelect(el, item, ctx, 'tool.items'); } };
  E.recipe_active = { render(el, item, ctx) { nameSelect(el, item, ctx, 'recipe.items'); } };

  // ---------------- 판 마커 좌표 ----------------
  E.markers = {
    render(el, item, ctx) {
      const mk = ctx.get(item.key) || {};
      ['0', '1', '2', '3'].forEach(id => { if (!mk[id]) mk[id] = [0, 0]; });
      const tb = h('tbody');
      Object.keys(mk).sort().forEach(id => {
        const xy = mk[id];
        const cell = (k) => {
          const inp = h('input', { type: 'number', cls: 'in num', step: 0.1, value: xy[k] });
          inp.addEventListener('input', () => { xy[k] = num(inp.value); ctx.set(item.key, mk); });
          markErr(inp, errCell(ctx, item.key + '.' + id));
          return inp;
        };
        tb.appendChild(h('tr', {}, [h('td', { cls: 'mono', text: 'id' + id }),
          h('td', {}, [cell(0)]), h('td', {}, [cell(1)])]));
      });
      el.appendChild(h('table', { cls: 'etable narrow' }, [
        h('thead', {}, [h('tr', {}, [h('th', { text: '마커' }), h('th', { text: 'X mm' }),
          h('th', { text: 'Y mm' })])]), tb]));
      el.appendChild(h('div', { cls: 'note', text: '포인터로 다시 잡으려면 [보정] → 판 마커 탭' }));
    },
  };

  // ---------------- 레시피 ----------------
  const STEP = { move_sample: '샘플 위로 이동', z: 'Z 이동', relay: '릴레이', wait: '대기',
                 measure: '측정', confirm: '확인 대기' };
  const NEW_STEP = {
    z: () => ({ type: 'z', target: 'work' }),
    relay: () => ({ type: 'relay', n: 1, on: true }),
    wait: () => ({ type: 'wait', s: 5 }),
    measure: () => ({ type: 'measure' }),
    confirm: () => ({ type: 'confirm' }),
  };
  let recSel = null;                       // 편집 중인 레시피 이름

  function zOf(step, ctx) {
    if (step.target === 'top') return 0;
    if (step.target === 'work') {
      const tools = ctx.get('tool.items') || [];
      const t = tools.find(x => x.name === ctx.get('tool.active')) || tools[0] || {};
      return +t.z_work_mm || 0;
    }
    return +step.mm || 0;
  }

  function estimate(r, ctx) {
    const speed = (+ctx.get('motion.speed_pps') || 6000) / 160;
    let s = 0, confirm = false;
    (r.steps || []).forEach(st => {
      if (st.type === 'move_sample') s += 2.0;
      else if (st.type === 'z') s += 0.3 + Math.abs(zOf(st, ctx)) / speed;
      else if (st.type === 'wait') s += +st.s || 0;
      else if (st.type === 'relay') s += 0.1;
      else if (st.type === 'measure') s += 0.5;
      else if (st.type === 'confirm') confirm = true;
    });
    return { s: s, confirm: confirm };
  }

  E.recipes = {
    render(el, item, ctx) {
      const list = ctx.get(item.key) || [];
      if (!list.length) list.push({ name: '기본', steps: [{ type: 'move_sample' }] });
      if (!recSel || !list.some(r => r.name === recSel)) recSel = ctx.get('recipe.active') || list[0].name;
      let ri = list.findIndex(r => r.name === recSel);
      if (ri < 0) { ri = 0; recSel = list[0].name; }
      const r = list[ri];
      const p = item.key + '[' + ri + ']';
      const commit = () => ctx.set(item.key, list);

      // 위: 선택 · 새로 · 복제 · 삭제 · 이름 · 활성
      const sel = h('select', { cls: 'in' });
      list.forEach((x, i) => sel.appendChild(h('option', {
        value: x.name, text: x.name + (x.name === ctx.get('recipe.active') ? ' (활성)' : '')
          + (ctx.errs(item.key + '[' + i + ']').length ? ' ⚠' : '') })));
      sel.value = r.name;
      sel.addEventListener('change', () => { recSel = sel.value; ctx.rerender(); });
      const uniq = (base) => {
        let n = 2, name = base;
        while (list.some(x => x.name === name)) { name = base + ' ' + n; n += 1; }
        return name;
      };
      const bNew = h('button', { cls: 'btn sm', text: '새로 만들기', 'data-tip': '샘플 이동 한 단계만 있는 새 레시피를 만듭니다.',
        onclick: () => { const nm = uniq('레시피'); list.push({ name: nm, steps: [{ type: 'move_sample' }] });
                         recSel = nm; commit(); ctx.rerender(); } });
      const bDup = h('button', { cls: 'btn sm', text: '복제', 'data-tip': '이 레시피를 복사합니다.',
        onclick: () => { const nm = uniq(r.name + ' 사본'); list.splice(ri + 1, 0, Object.assign(UI.clone(r), { name: nm }));
                         recSel = nm; commit(); ctx.rerender(); } });
      const bDel = h('button', { cls: 'btn sm', text: '삭제', disabled: list.length <= 1 ? 'disabled' : null,
        'data-tip': '이 레시피를 지웁니다. 마지막 하나는 지울 수 없습니다.',
        onclick: async () => {
          if (!await UI.confirm("레시피 '" + r.name + "' 을 지웁니다. 저장을 눌러야 적용됩니다.", '레시피 삭제')) return;
          list.splice(ri, 1);
          if (ctx.get('recipe.active') === r.name) ctx.set('recipe.active', list[0].name);
          recSel = list[0].name; commit(); ctx.rerender();
        } });
      const nm = h('input', { type: 'text', cls: 'in', value: r.name, maxlength: 30,
                              'data-tip': '레시피 이름입니다(30자 이내, 겹치면 안 됩니다).' });
      nm.addEventListener('change', () => {
        const old = r.name;
        r.name = nm.value.trim();
        if (ctx.get('recipe.active') === old) ctx.set('recipe.active', r.name);
        recSel = r.name; commit(); ctx.rerender();
      });
      markErr(nm, errCell(ctx, p + '.name'));
      const isActive = ctx.get('recipe.active') === r.name;
      const bAct = h('button', { cls: 'btn sm', text: isActive ? '활성 레시피' : '활성으로',
        disabled: isActive ? 'disabled' : null, 'data-tip': '순회가 이 레시피를 쓰게 합니다.',
        onclick: () => { ctx.set('recipe.active', r.name); ctx.rerender(); } });
      el.appendChild(h('div', { cls: 'erow' }, [h('span', { cls: 'note', text: '레시피' }), sel, bNew, bDup, bDel,
        h('span', { cls: 'note', text: '이름' }), nm, bAct]));

      // 아래: 단계 표 + 요약
      const tb = h('tbody');
      const steps = r.steps || (r.steps = []);
      const relays = ctx.get('relay.items') || [];
      steps.forEach((st, j) => {
        const sp = p + '.steps[' + j + ']';
        const fixed = j === 0 && st.type === 'move_sample';
        const val = h('div', { cls: 'cellrow' });
        if (st.type === 'move_sample') {
          val.appendChild(h('span', { cls: 'note', text: '샘플 위치 − 활성 공구 오프셋 · Z 먼저 맨 위' }));
        } else if (st.type === 'z') {
          const tg = h('select', { cls: 'in' });
          [['work', '작업 높이'], ['top', '맨 위'], ['mm', '숫자']].forEach(o =>
            tg.appendChild(h('option', { value: o[0], text: o[1] })));
          tg.value = st.target || 'work';
          const mm = h('input', { type: 'number', cls: 'in num', step: 0.1, min: 0,
                                  value: st.mm != null ? st.mm : 0, hidden: st.target === 'mm' ? null : 'hidden' });
          const info = h('span', { cls: 'note', text: st.target === 'mm' ? 'mm' : '= ' + zOf(st, ctx) + ' mm' });
          tg.addEventListener('change', () => {
            st.target = tg.value;
            if (st.target === 'mm' && st.mm == null) st.mm = 0;
            commit(); ctx.rerender();
          });
          mm.addEventListener('input', () => { st.mm = num(mm.value); commit(); });
          val.appendChild(tg); val.appendChild(mm); val.appendChild(info);
        } else if (st.type === 'relay') {
          const n = h('select', { cls: 'in' });
          relays.forEach((x, k) => n.appendChild(h('option', { value: k + 1,
            text: 'R' + (k + 1) + ' ' + (x.name || '') + (x.enabled ? '' : ' (사용 안 함)') })));
          n.value = String(st.n || 1);
          const on = h('select', { cls: 'in' });
          on.appendChild(h('option', { value: '1', text: '켜기' }));
          on.appendChild(h('option', { value: '0', text: '끄기' }));
          on.value = st.on ? '1' : '0';
          n.addEventListener('change', () => { st.n = +n.value; commit(); });
          on.addEventListener('change', () => { st.on = on.value === '1'; commit(); });
          val.appendChild(n); val.appendChild(on);
        } else if (st.type === 'wait') {
          const s = h('input', { type: 'number', cls: 'in num', step: 0.5, min: 0, value: st.s });
          s.addEventListener('input', () => { st.s = num(s.value); commit(); });
          val.appendChild(s); val.appendChild(h('span', { cls: 'unit', text: 's' }));
        } else if (st.type === 'measure') {
          val.appendChild(h('span', { cls: 'note', text: '계측기 호출 (지금은 더미)' }));
        } else if (st.type === 'confirm') {
          val.appendChild(h('span', { cls: 'note', text: '[다음] 을 누를 때까지 대기' }));
        }
        const move = (d) => () => {
          const k = j + d;
          if (k < 1 || k >= steps.length) return;
          const t = steps[j]; steps[j] = steps[k]; steps[k] = t;
          commit(); ctx.rerender();
        };
        const up = h('button', { cls: 'btn sm', text: '↑', 'aria-label': '위로',
          disabled: (fixed || j <= 1) ? 'disabled' : null, onclick: move(-1) });
        const dn = h('button', { cls: 'btn sm', text: '↓', 'aria-label': '아래로',
          disabled: (fixed || j >= steps.length - 1) ? 'disabled' : null, onclick: move(1) });
        const del = h('button', { cls: 'btn sm', text: '✕', 'aria-label': '삭제',
          disabled: fixed ? 'disabled' : null,
          onclick: () => { steps.splice(j, 1); commit(); ctx.rerender(); } });
        const e = errCell(ctx, sp);
        tb.appendChild(h('tr', { cls: e ? 'bad' : '' }, [
          h('td', { cls: 'mono', text: String(j + 1) }),
          h('td', { text: STEP[st.type] || st.type }),
          h('td', {}, [val]),
          h('td', {}, [h('div', { cls: 'cellrow' }, [up, dn, del])]),
          h('td', { cls: 'err', text: e }),
        ]));
      });
      const add = h('select', { cls: 'in', 'data-tip': '고른 종류의 단계를 맨 끝에 더합니다.' });
      add.appendChild(h('option', { value: '', text: '단계 추가 ▾' }));
      Object.keys(NEW_STEP).forEach(k => add.appendChild(h('option', { value: k, text: STEP[k] })));
      add.addEventListener('change', () => {
        if (!add.value) return;
        steps.push(NEW_STEP[add.value]());
        commit(); ctx.rerender();
      });
      const table = h('div', { cls: 'recmain' }, [
        h('table', { cls: 'etable steps' }, [
          h('thead', {}, [h('tr', {}, [h('th', { text: '#' }), h('th', { text: '종류' }),
            h('th', { text: '값' }), h('th', { text: '순서' }), h('th', { text: '' })])]), tb]),
        h('div', { cls: 'erow' }, [add])]);

      const est = estimate(r, ctx);
      const used = [];
      steps.forEach(st => { if (st.type === 'relay' && used.indexOf(st.n) < 0) used.push(st.n); });
      const warns = (ctx.warnings() || []).filter(w => w.indexOf("'" + r.name + "'") >= 0);
      const sum = h('div', { cls: 'recsum' }, [
        h('div', { cls: 'k', text: '요약' }),
        h('div', { text: '단계 ' + steps.length + '개' }),
        h('div', { text: '샘플당 약 ' + UI.dur(est.s) + (est.confirm ? ' + 확인' : '') }),
        h('div', { text: '릴레이 ' + (used.length ? used.map(n => 'R' + n).join(', ') : '없음') }),
        h('div', { text: 'Z 단계 ' + (steps.some(x => x.type === 'z') ? '있음 (Z 원점 필요)' : '없음') }),
      ].concat(warns.map(w => h('div', { cls: 'warnline', text: w }))));
      el.appendChild(h('div', { cls: 'recwrap' }, [table, sum]));
    },
  };
})();
