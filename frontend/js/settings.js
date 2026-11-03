/* settings.js — 설정 창. 서버 스키마(settings_schema)로 자동 생성한다.

   - 왼쪽 탭, 오른쪽 그룹 제목 + 2열 필드. 필드는 라벨 · 입력 + 단위 · 기본값과 다르면
     점(마우스 오버에 기본값) · 적용 시점 배지 · 오류 한 줄.
   - 검증은 서버가 한다(settings_validate). 입력이 멈추면 보내고, 오류가 있으면 [저장]을
     막고 사유를 툴팁으로 보인다. 저장도 서버가 다시 전부 검증한다(부분 저장 없음).
   - 창은 show() 로 연다(core.js UI.openWin) - 열려 있어도 비상정지는 눌린다. */
(function () {
  'use strict';
  const UI = window.UI, $ = UI.$, h = UI.h;

  const S = { schema: null, values: null, defaults: null, form: null, errors: {},
              warnings: [], tab: 'camera', seq: 0, open: false, dataDir: '', path: '' };
  const APPLY_CLS = { '즉시': 'now', '다음 촬영': 'shot', '재연결': 'reconn', '다음 순회': 'run' };
  let vTimer = null;

  // ---------------- 경로 ----------------
  function getP(o, key) {
    let cur = o;
    for (const part of key.split('.')) {
      if (cur == null || typeof cur !== 'object' || !(part in cur)) return undefined;
      cur = cur[part];
    }
    return cur;
  }
  function setP(o, key, v) {
    const parts = key.split('.');
    let cur = o;
    parts.slice(0, -1).forEach(p => {
      if (cur[p] == null || typeof cur[p] !== 'object') cur[p] = {};
      cur = cur[p];
    });
    cur[parts[parts.length - 1]] = v;
  }

  function items() { return (S.schema && S.schema.items) || []; }
  function tabItems(tab) {
    return items().filter(it => !it.hidden
      && (tab === 'advanced' ? it.advanced : (it.tab === tab && !it.advanced)));
  }
  function errsFor(prefix) {
    return Object.keys(S.errors).filter(k => k === prefix || k.indexOf(prefix + '.') === 0
      || k.indexOf(prefix + '[') === 0).map(k => [k, S.errors[k]]);
  }
  function tabOf(key) {
    const it = items().find(x => key === x.key || key.indexOf(x.key + '.') === 0
                                 || key.indexOf(x.key + '[') === 0);
    if (!it) return null;
    return it.advanced ? 'advanced' : it.tab;
  }
  function dirtyCount() {
    return items().filter(it => !it.hidden
      && !UI.same(getP(S.form, it.key), getP(S.values, it.key))).length;
  }

  // ---------------- 열기 / 닫기 ----------------
  UI.openSettings = function (tab) {
    if (tab) S.tab = tab;
    S.open = true;
    UI.send({ cmd: 'settings_schema' });
    UI.openWin($('dlgSet'), { onCancel: tryClose });
  };

  async function tryClose() {
    const n = S.form ? dirtyCount() : 0;
    if (n && !await UI.confirm('저장하지 않은 변경 ' + n + '건을 버립니다.', '설정 닫기', '버리기')) return;
    S.open = false;
    UI.closeWin($('dlgSet'));
  }
  $('setX').onclick = tryClose;
  $('setCancel').onclick = tryClose;

  // ---------------- 서버 응답 ----------------
  UI.onSettingsAck = function (msg) {
    if (msg.of === 'settings_schema') {
      S.schema = msg.schema; S.values = msg.values; S.defaults = msg.defaults;
      S.form = UI.clone(msg.values); S.errors = {}; S.warnings = [];
      S.dataDir = msg.data_dir || ''; S.path = msg.settings_path || '';
      if (!S.schema.tabs.some(t => t.id === S.tab)) S.tab = 'camera';
      render();
      validateSoon(0);
    } else if (msg.of === 'settings_validate') {
      if (msg.seq !== S.seq) return;       // 늦게 온 옛 결과
      S.errors = msg.errors || {}; S.warnings = msg.warnings || [];
      showErrors();
    } else if (msg.of === 'settings_save') {
      if (msg.ok) {
        S.values = UI.clone(S.form);
        S.open = false;
        UI.closeWin($('dlgSet'));
      } else {
        S.errors = msg.errors || {};
        showErrors();
        const first = Object.keys(S.errors)[0];
        if (first && tabOf(first) && tabOf(first) !== S.tab) { S.tab = tabOf(first); render(); }
      }
    } else if (msg.of === 'settings_export') {
      download(msg.data);
      UI.alert('설정을 내보냈습니다.' + (msg.path ? '\n' + msg.path : ''), '내보내기');
    } else if (msg.of === 'settings_import') {
      if (msg.ok) {
        UI.send({ cmd: 'settings_schema' });   // 저장된 값으로 다시 그린다
        UI.alert('설정을 가져와 저장했습니다.', '가져오기');
      } else {
        const lines = Object.keys(msg.errors || {}).slice(0, 6)
          .map(k => '· ' + (k || '파일') + ' ' + msg.errors[k]);
        UI.alert('가져오지 않았습니다(검증 오류).\n' + lines.join('\n'), '가져오기');
      }
    }
  };

  function download(data) {
    try {
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      const d = new Date(), p2 = (n) => String(n).padStart(2, '0');
      a.href = URL.createObjectURL(blob);
      a.download = 'settings_' + d.getFullYear() + p2(d.getMonth() + 1) + p2(d.getDate())
        + '_' + p2(d.getHours()) + p2(d.getMinutes()) + '.json';
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    } catch (e) { /* 창(WebView2)에서 내려받기가 막히면 서버가 남긴 파일을 쓴다 */ }
  }

  // ---------------- 그리기 ----------------
  function render() {
    if (!S.schema) return;
    items().filter(it => it.editor).forEach(it => { errSig[it.key] = JSON.stringify(errsFor(it.key)); });
    const nav = $('setTabs');
    nav.textContent = '';
    S.schema.tabs.forEach(t => {
      const b = h('button', { cls: 'settab' + (t.id === S.tab ? ' on' : ''), text: t.label,
                              'data-tab': t.id, onclick: () => { S.tab = t.id; render(); } });
      b.appendChild(h('i', { cls: 'tdot' }));
      nav.appendChild(b);
    });
    const tab = S.schema.tabs.find(t => t.id === S.tab);
    $('setTabTitle').textContent = tab ? tab.label : '—';
    $('setTabNote').textContent = S.tab === 'advanced' ? '바꾸기 전에 뜻을 확인' : '';
    const box = $('setGroups');
    const scroll = box.scrollTop;
    box.textContent = '';
    const groups = [];
    tabItems(S.tab).forEach(it => {
      const g = (S.tab === 'advanced' ? labelOfTab(it.tab) + ' · ' : '') + it.group;
      let grp = groups.find(x => x.name === g);
      if (!grp) { grp = { name: g, items: [] }; groups.push(grp); }
      grp.items.push(it);
    });
    groups.forEach(g => {
      const fields = h('div', { cls: 'sfields' });
      g.items.forEach(it => fields.appendChild(field(it)));
      box.appendChild(h('section', { cls: 'sgroup' }, [h('h4', { text: g.name }), fields]));
    });
    if (S.tab === 'advanced') {
      box.appendChild(h('section', { cls: 'sgroup' }, [h('h4', { text: '파일' }),
        h('div', { cls: 'sfields' }, [
          info('데이터 폴더', S.dataDir), info('설정 파일', S.path),
          info('검출 파라미터', 'settings.json 에서 직접 편집(이 화면에 없는 키는 보존)')])]));
    }
    box.scrollTop = scroll;
    showErrors();
  }

  function labelOfTab(id) {
    const t = (S.schema.tabs || []).find(x => x.id === id);
    return t ? t.label : id;
  }

  function info(label, text) {
    return h('div', { cls: 'fld full' }, [h('div', { cls: 'fl' }, [h('span', { cls: 'lab', text: label })]),
      h('div', { cls: 'fc' }, [h('span', { cls: 'mono ro', text: text || UI.EMPTY })])]);
  }

  function fmtVal(v) {
    if (v == null) return '없음';
    if (typeof v === 'boolean') return v ? '켬' : '끔';
    if (Array.isArray(v)) return v.length > 4 ? '목록 ' + v.length + '개' : JSON.stringify(v);
    if (typeof v === 'object') return '기본 목록';
    return String(v);
  }

  function field(it) {
    const wide = it.editor && ['port', 'tool_active', 'recipe_active'].indexOf(it.editor) < 0;
    const f = h('div', { cls: 'fld' + (wide || it.type === 'xy' ? ' full' : ''), 'data-key': it.key });
    const lab = h('span', { cls: 'lab', text: it.label, 'data-tip': it.tip || null });
    const mod = h('i', { cls: 'mod' });
    const ap = h('span', { cls: 'ap ' + (APPLY_CLS[it.apply] || ''), text: it.apply });
    f.appendChild(h('div', { cls: 'fl' }, [lab, mod, h('span', { cls: 'sp' }), ap]));
    const fc = h('div', { cls: 'fc' });
    control(fc, it);
    if (it.unit && it.type !== 'xy' && !it.editor) fc.appendChild(h('span', { cls: 'unit', text: it.unit }));
    f.appendChild(fc);
    f.appendChild(h('div', { cls: 'fe' }));
    markMod(f, it);
    return f;
  }

  function markMod(f, it) {
    const mod = f.querySelector('.mod');
    const d = getP(S.defaults, it.key), v = getP(S.form, it.key);
    const diff = !UI.same(d, v);
    mod.classList.toggle('on', diff);
    mod.setAttribute('data-tip', diff ? '기본값 ' + fmtVal(d) : '');
  }

  function changed(it) {
    const f = document.querySelector('#setGroups .fld[data-key="' + it.key + '"]');
    if (f) markMod(f, it);
    updateDirty();
    validateSoon();
  }

  function num(v) {
    if (v === '') return '';
    const n = Number(v);
    return isFinite(n) ? n : v;
  }

  function control(fc, it) {
    const val = getP(S.form, it.key);
    const ctx = {
      get: (k) => getP(S.form, k),
      set: (k, v) => {
        setP(S.form, k, v);
        const owner = items().find(x => x.key === k);
        changed(owner || it);
      },
      def: (k) => getP(S.defaults, k),
      errs: errsFor,
      warnings: () => S.warnings,
      rerender: render,
      state: UI.state,
      send: UI.send,
    };
    if (it.editor && UI.editors[it.editor]) {
      UI.editors[it.editor].render(fc, it, ctx);
      return;
    }
    const set = (v) => { setP(S.form, it.key, v); changed(it); };
    if (it.type === 'int' || it.type === 'float') {
      const inp = h('input', { type: 'number', cls: 'in num', value: val == null ? '' : val,
                               step: it.step || 'any', min: it.min, max: it.max });
      inp.addEventListener('input', () => set(num(inp.value)));
      fc.appendChild(inp);
    } else if (it.type === 'bool') {
      const cb = h('input', { type: 'checkbox' });
      cb.checked = !!val;
      const txt = h('span', { text: val ? '켬' : '끔' });
      cb.addEventListener('change', () => { txt.textContent = cb.checked ? '켬' : '끔'; set(cb.checked); });
      fc.appendChild(h('label', { cls: 'switch' }, [cb, txt]));
    } else if (it.type === 'enum') {
      const sel = h('select', { cls: 'in' });
      (it.options || []).forEach(o => sel.appendChild(h('option', { value: o.value, text: o.label })));
      sel.value = val;
      sel.addEventListener('change', () => set(sel.value));
      fc.appendChild(sel);
    } else if (it.type === 'xy') {
      const x = h('input', { type: 'number', cls: 'in num', step: 0.1, value: val ? val[0] : '' });
      const y = h('input', { type: 'number', cls: 'in num', step: 0.1, value: val ? val[1] : '' });
      const upd = () => set([num(x.value), num(y.value)]);
      x.addEventListener('input', upd);
      y.addEventListener('input', upd);
      const st = (UI.state && UI.state.stage) || {};
      const can = st.connected && st.homed_x && st.homed_y && !st.needs_home && st.x_mm != null;
      const pick = h('button', { cls: 'btn sm', text: '현재 위치 넣기', disabled: can ? null : 'disabled',
        'data-tip': can ? '지금 스테이지 좌표를 넣습니다. 저장을 눌러야 적용됩니다.'
                        : '스테이지 연결 · 원점이 있어야 합니다.',
        onclick: () => { x.value = (+st.x_mm).toFixed(1); y.value = (+st.y_mm).toFixed(1); upd(); } });
      fc.appendChild(h('span', { cls: 'note', text: 'X' })); fc.appendChild(x);
      fc.appendChild(h('span', { cls: 'note', text: 'Y' })); fc.appendChild(y);
      fc.appendChild(h('span', { cls: 'unit', text: it.unit || 'mm' }));
      fc.appendChild(pick);
    } else if (it.type === 'list') {
      const inp = h('input', { type: 'text', cls: 'in wide', value: (val || []).join(', '),
                               placeholder: '비움 = 자동' });
      inp.addEventListener('input', () => {
        const parts = inp.value.split(',').map(t => t.trim()).filter(t => t !== '');
        set(parts.map(num));
      });
      fc.appendChild(inp);
    } else {
      const inp = h('input', { type: 'text', cls: 'in wide', value: val == null ? '' : val });
      inp.addEventListener('input', () => set(it.nullable && !inp.value.trim() ? null : inp.value));
      fc.appendChild(inp);
    }
  }

  // ---------------- 검증 · 오류 ----------------
  function validateSoon(ms) {
    if (vTimer) clearTimeout(vTimer);
    vTimer = setTimeout(() => {
      vTimer = null;
      S.seq += 1;
      UI.send({ cmd: 'settings_validate', values: S.form, seq: S.seq });
    }, ms == null ? 250 : ms);
  }

  // 전용 편집기의 칸별 오류 표시는 그릴 때 정해진다. 오류가 바뀌면 그 편집기만 다시
  // 그린다 - 단, 사용자가 그 안에서 입력 중이면 포커스가 빠질 때까지 미룬다.
  const errSig = {};
  function refreshEditor(f, it) {
    const sig = JSON.stringify(errsFor(it.key));
    if (errSig[it.key] === sig) return;
    if (f.contains(document.activeElement)) {
      f.addEventListener('focusout', () => setTimeout(() => {
        if (!f.contains(document.activeElement) && f.isConnected) refreshEditor(f, it);
      }, 0), { once: true });
      return;
    }
    errSig[it.key] = sig;
    const nf = field(it);
    f.replaceWith(nf);
  }

  function showErrors() {
    document.querySelectorAll('#setGroups .fld[data-key]').forEach(f0 => {
      let f = f0;
      const key = f.getAttribute('data-key');
      const it = items().find(x => x.key === key) || {};
      if (it.editor && ['port', 'tool_active', 'recipe_active'].indexOf(it.editor) < 0) {
        refreshEditor(f, it);
        f = document.querySelector('#setGroups .fld[data-key="' + key + '"]') || f;
      }
      const list = errsFor(key);
      const fe = f.querySelector('.fe');
      // 전용 편집기는 칸마다 표시한다. 칸에 걸리지 않는 오류(목록 전체)만 아래 줄에.
      const shown = it.editor ? list.filter(e => e[0] === key) : list;
      fe.textContent = shown.length ? shown[0][1] : (it.editor && list.length
        ? '오류 ' + list.length + '건 · ' + list[0][1] : '');
      f.classList.toggle('bad', list.length > 0);
    });
    document.querySelectorAll('#setTabs .settab').forEach(b => {
      const t = b.getAttribute('data-tab');
      b.classList.toggle('bad', Object.keys(S.errors).some(k => tabOf(k) === t));
    });
    const keys = Object.keys(S.errors);
    const btn = $('setSave');
    btn.disabled = keys.length > 0;
    const first = keys.length ? (labelFor(keys[0]) + ' · ' + S.errors[keys[0]]) : '';
    $('setSaveWrap').setAttribute('data-tip', keys.length ? '저장 불가 · ' + first : '');
    $('setErrSum').textContent = keys.length ? '오류 ' + keys.length + '건 · ' + first : '';
    updateDirty();
  }

  function labelFor(key) {
    const it = items().find(x => key === x.key || key.indexOf(x.key + '.') === 0
                                 || key.indexOf(x.key + '[') === 0);
    return it ? it.label : key;
  }

  function updateDirty() {
    const n = S.form ? dirtyCount() : 0;
    $('setDirty').textContent = n ? '변경 ' + n + '건' : '';
  }

  // ---------------- 버튼 ----------------
  $('setDefaults').onclick = () => {
    if (!S.schema) return;
    tabItems(S.tab).forEach(it => setP(S.form, it.key, UI.clone(getP(S.defaults, it.key))));
    render();
    updateDirty();
    validateSoon(0);
  };

  $('setSave').onclick = async () => {
    if (!S.form || Object.keys(S.errors).length) return;
    if (!UI.same(S.form.origin_gap_mm, S.values.origin_gap_mm)) {
      const ok = await UI.confirm('원점 이격을 ' + S.values.origin_gap_mm + ' → ' + S.form.origin_gap_mm
        + ' mm 로 바꿉니다. 판 마커 좌표와 헤드 마커 보정이 이 원점 기준이라 다시 해야 합니다.',
        '원점 이격 변경', '바꾸기');
      if (!ok) return;
    }
    UI.send({ cmd: 'settings_save', values: S.form });
  };

  $('setExport').onclick = () => UI.send({ cmd: 'settings_export' });
  $('setImport').onclick = () => { $('setFile').value = ''; $('setFile').click(); };
  $('setFile').addEventListener('change', () => {
    const f = $('setFile').files && $('setFile').files[0];
    if (!f) return;
    const rd = new FileReader();
    rd.onload = async () => {
      let data;
      try { data = JSON.parse(String(rd.result)); } catch (e) {
        UI.alert('JSON 파일이 아닙니다.', '가져오기');
        return;
      }
      if (!await UI.confirm("'" + f.name + "' 의 설정으로 바꾸고 저장합니다. 지금 설정은 백업됩니다.",
                            '가져오기', '가져오기')) return;
      UI.send({ cmd: 'settings_import', data: data });
    };
    rd.readAsText(f, 'utf-8');
  });

  $('btnSettings').onclick = () => UI.openSettings();
  $('btnRecipeEdit').onclick = () => UI.openSettings('recipe');
})();
