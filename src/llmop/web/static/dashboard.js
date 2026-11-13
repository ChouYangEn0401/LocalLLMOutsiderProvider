/* Dashboard: quota cards, 30-day chart, API keys, how-to, playground, recent requests. */
(function () {
  'use strict';

  const { h, $, api, fill } = LL;
  let me = null;
  let models = [];
  let modelsError = null;
  let lastQuota = null;

  LL.boot(async () => {
    me = await LL.loadMe();
    LL.renderTopbar(me, 'dashboard');
    bindKeyForm();
    bindPlayground();
    $('#refresh').addEventListener('click', () => refreshAll(true));
    renderHowTo();
    await refreshAll(false);
  });

  async function refreshAll(withToast) {
    const results = await Promise.allSettled([loadUsage(), loadKeys(), loadModels()]);
    const failed = results.find((r) => r.status === 'rejected');
    if (failed && failed.reason && failed.reason.status !== 401) {
      LL.toast('部分資料載入失敗：' + failed.reason.message, true);
    } else if (withToast) {
      LL.toast('已更新');
    }
  }

  // ================================================================ usage
  async function loadUsage() {
    try {
      const u = await api('GET', '/ui/api/usage?days=30');
      lastQuota = u.quota;
      renderQuota(u.quota);
      renderChart(u.by_day, u.quota);
      renderByModel(u.by_model);
      LL.limitedTable($('#recent'), u.recent, 15, (rows) => LL.requestsTable(rows, false));
      const maxTok = u.quota.limits.max_tokens_per_request;
      $('#pg-max').placeholder = maxTok ? '上限 ' + LL.fmtNum(maxTok) : '預設 (default)';
    } catch (e) {
      LL.showFatal($('#quota-cards'), e);
      throw e;
    }
  }

  function statCard(label, en, used, limit, foot) {
    const unlimited = !limit;
    const value = h('div', { class: 'stat-value' }, LL.fmtNum(used),
      unlimited ? null : h('span', { class: 'of' }, ' / ' + LL.fmtNum(limit)));
    let meterRow = null;
    let note = null;
    if (unlimited) {
      meterRow = h('div', null, h('span', { class: 'pill' }, '不限 (unlimited)'));
    } else {
      const m = LL.meter(used, limit);
      meterRow = m.el;
      note = LL.levelNote(m.level, m.pct);
    }
    return h('div', { class: 'card stat' },
      h('div', { class: 'stat-label' }, label, ' ', h('small', null, en)),
      value, meterRow,
      h('div', { class: 'stat-foot' }, note, note && foot ? ' · ' : '', foot || ''));
  }

  function renderQuota(q) {
    const L = q.limits;
    const left = (rem) => (rem === null || rem === undefined ? '' : '剩 ' + LL.fmtNum(rem));
    const join = (...parts) => parts.filter(Boolean).join(' · ');
    const maxTok = L.max_tokens_per_request;
    fill($('#quota-cards'),
      statCard('今日 Tokens', '(today)', q.tokens_today, L.tokens_per_day,
        join(left(q.remaining.tokens_today), LL.fmtClock(q.resets.day) + ' 重置')),
      statCard('本月 Tokens', '(this month)', q.tokens_month, L.tokens_per_month,
        join(left(q.remaining.tokens_month), LL.fmtDate(q.resets.month) + ' 重置')),
      statCard('每分鐘請求', '(RPM)', q.requests_last_minute, L.requests_per_minute,
        '過去 60 秒'),
      statCard('今日請求數', '(requests)', q.requests_today, L.requests_per_day,
        left(q.remaining.requests_today)),
      statCard('同時請求', '(concurrent)', q.in_flight, L.max_concurrent, '進行中 / 上限'),
      h('div', { class: 'card stat' },
        h('div', { class: 'stat-label' }, '單次上限 ', h('small', null, '(max_tokens)')),
        h('div', { class: 'stat-value' }, maxTok ? LL.fmtNum(maxTok) : '不限'),
        h('div', { class: 'stat-foot' }, maxTok ? '超過會自動夾到這個值' : '(unlimited)')));
  }

  // ================================================================ chart
  let chartState = null;
  let resizeObs = null;

  function dayLabel(iso, withWeekday) {
    const [y, m, d] = iso.split('-').map(Number);
    const base = m + '/' + d;
    if (!withWeekday) return base;
    return base + '（' + new Date(y, m - 1, d).toLocaleDateString('zh-TW', { weekday: 'short' }) + '）';
  }

  function niceStep(raw) {
    const exp = Math.pow(10, Math.floor(Math.log10(raw)));
    const f = raw / exp;
    const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
    return nice * exp;
  }

  function renderChart(days, quota) {
    const wrap = $('#chart');
    const total = days.reduce((s, d) => s + d.total, 0);
    const reqs = days.reduce((s, d) => s + d.requests, 0);
    $('#chart-summary').textContent =
      '合計 ' + LL.fmtNum(total) + ' tokens · ' + LL.fmtNum(reqs) + ' 次請求';

    const rows = days.slice().reverse().filter((d) => d.requests > 0);
    fill($('#chart-table'), LL.table([
      { label: '日期', cell: (d) => dayLabel(d.day, true) },
      { label: '請求', cls: 'num', cell: (d) => LL.fmtNum(d.requests) },
      { label: 'Prompt', cls: 'num', cell: (d) => LL.fmtNum(d.prompt) },
      { label: 'Completion', cls: 'num', cell: (d) => LL.fmtNum(d.completion) },
      { label: '合計 Tokens', cls: 'num', cell: (d) => LL.fmtNum(d.total) },
    ], rows, '這段期間沒有用量'));

    if (!total) {
      chartState = null;
      fill(wrap, h('div', { class: 'chart-empty' }, '最近 30 天還沒有用量 (no usage yet)'));
      return;
    }
    chartState = { days, limit: quota.limits.tokens_per_day || 0, width: 0 };
    drawChart();
    if (!resizeObs && 'ResizeObserver' in window) {
      let raf = 0;
      resizeObs = new ResizeObserver(() => {
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(() => {
          if (chartState && Math.abs(wrap.clientWidth - chartState.width) > 4) drawChart();
        });
      });
      resizeObs.observe(wrap);
    }
  }

  function barPath(x, y, w, hgt, r) {
    // Rounded data-end (top), square at the baseline.
    r = Math.min(r, w / 2, hgt);
    return 'M' + x + ',' + (y + hgt) + 'V' + (y + r) +
      'A' + r + ',' + r + ' 0 0 1 ' + (x + r) + ',' + y +
      'H' + (x + w - r) +
      'A' + r + ',' + r + ' 0 0 1 ' + (x + w) + ',' + (y + r) +
      'V' + (y + hgt) + 'Z';
  }

  function drawChart() {
    const wrap = $('#chart');
    const { days, limit } = chartState;
    const W = Math.max(280, Math.floor(wrap.clientWidth || 600));
    chartState.width = W;
    const H = 220;
    const m = { l: 46, r: 10, t: 14, b: 26 };
    const pw = W - m.l - m.r;
    const ph = H - m.t - m.b;

    let max = Math.max(1, ...days.map((d) => d.total));
    const showRef = limit > 0 && max >= limit * 0.5;
    if (showRef) max = Math.max(max, limit);
    const step = niceStep(max / 4);
    const top = Math.ceil(max / step) * step;
    const y = (v) => m.t + ph - (v / top) * ph;
    const slot = pw / days.length;
    const bw = Math.max(2, Math.min(24, slot - 2));
    const todayIdx = days.length - 1;

    const svg = LL.svg('svg', {
      class: 'chart', viewBox: '0 0 ' + W + ' ' + H, width: W, height: H,
      role: 'img', 'aria-label': '最近 ' + days.length + ' 天每日 token 用量長條圖',
    });

    for (let v = step; v <= top + 1e-9; v += step) {
      svg.appendChild(LL.svg('line', { class: 'grid-line', x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }));
    }
    for (let v = 0; v <= top + 1e-9; v += step) {
      svg.appendChild(LL.svg('text', { class: 'tick', x: m.l - 8, y: y(v) + 4, 'text-anchor': 'end' },
        v < 10000 ? LL.fmtNum(v) : LL.fmtCompact(v)));
    }

    const bars = LL.svg('g');
    const hits = LL.svg('g');
    days.forEach((d, i) => {
      const x0 = m.l + slot * i;
      const cx = x0 + slot / 2;
      if (d.total > 0) {
        const bh = Math.max(2, (d.total / top) * ph);
        bars.appendChild(LL.svg('path', {
          class: 'bar' + (i === todayIdx ? ' today' : ''),
          d: barPath(cx - bw / 2, m.t + ph - bh, bw, bh, 4),
        }));
      }
      const hit = LL.svg('rect', {
        class: 'hit', x: x0, y: m.t, width: slot, height: ph, tabindex: 0,
        'aria-label': dayLabel(d.day, true) + '：' + LL.fmtNum(d.total) + ' tokens，' + LL.fmtNum(d.requests) + ' 次請求',
      });
      const show = () => showTip(d, cx, y(d.total), W);
      hit.addEventListener('pointerenter', show);
      hit.addEventListener('focus', show);
      hit.addEventListener('pointerleave', hideTip);
      hit.addEventListener('blur', hideTip);
      hits.appendChild(hit);
    });
    svg.appendChild(hits);
    svg.appendChild(bars);
    svg.appendChild(LL.svg('line', { class: 'baseline', x1: m.l, x2: W - m.r, y1: m.t + ph, y2: m.t + ph }));

    if (showRef) {
      svg.appendChild(LL.svg('line', { class: 'ref-line', x1: m.l, x2: W - m.r, y1: y(limit), y2: y(limit) }));
      svg.appendChild(LL.svg('text', { class: 'ref-label', x: W - m.r, y: y(limit) - 5, 'text-anchor': 'end' },
        '每日上限 ' + LL.fmtCompact(limit)));
    }

    // X labels: every k-th day counted back from today so "today" is always labelled.
    const k = Math.max(1, Math.ceil(44 / slot));
    days.forEach((d, i) => {
      if ((todayIdx - i) % k !== 0) return;
      svg.appendChild(LL.svg('text', {
        class: 'tick', x: m.l + slot * i + slot / 2, y: H - 8, 'text-anchor': 'middle',
      }, i === todayIdx ? '今天' : dayLabel(d.day)));
    });

    const tip = h('div', { class: 'tooltip hidden', id: 'chart-tip', role: 'presentation' });
    fill(wrap, svg, tip);
  }

  function showTip(d, cx, barTop, W) {
    const tip = $('#chart-tip');
    if (!tip) return;
    fill(tip,
      h('div', { class: 'small muted' }, dayLabel(d.day, true)),
      h('strong', null, LL.fmtNum(d.total) + ' tokens'),
      h('div', { class: 'row' }, h('span', null, '請求'), h('span', { class: 'num' }, LL.fmtNum(d.requests))),
      h('div', { class: 'row' }, h('span', null, 'Prompt'), h('span', { class: 'num' }, LL.fmtNum(d.prompt))),
      h('div', { class: 'row' }, h('span', null, 'Completion'), h('span', { class: 'num' }, LL.fmtNum(d.completion))));
    tip.classList.remove('hidden');
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    const left = Math.min(Math.max(0, cx - tw / 2), W - tw);
    const top = Math.max(0, barTop - th - 10);
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }

  function hideTip() {
    const tip = $('#chart-tip');
    if (tip) tip.classList.add('hidden');
  }

  // ================================================================ by model
  function renderByModel(rows) {
    const total = rows.reduce((s, r) => s + (r.total || 0), 0);
    fill($('#by-model'), LL.table([
      { label: '模型 (model)', cell: (r) => h('span', { class: 'mono' }, r.model) },
      { label: '請求', cls: 'num', cell: (r) => LL.fmtNum(r.requests) },
      { label: 'Tokens', cls: 'num', cell: (r) => LL.fmtNum(r.total) },
      {
        label: '佔比', cell: (r) => {
          const p = LL.pct(r.total || 0, total);
          const mtr = LL.meter(p, 100, true);
          mtr.el.classList.remove('warn', 'crit');
          return h('div', { class: 'share' }, mtr.el, h('span', null, LL.fmtPct(p)));
        },
      },
    ], rows, '還沒有用量 (no usage yet)'));
  }

  // ================================================================ API keys
  async function loadKeys() {
    const r = await api('GET', '/ui/api/keys');
    const max = r.max_api_keys;
    $('#keys-count').textContent = max > 0
      ? '使用中 ' + r.active_count + ' / ' + max + ' 把'
      : '此帳號不能建立 API 金鑰';
    $('#key-create').disabled = max <= 0 || r.active_count >= max;
    $('#key-create').title = r.active_count >= max && max > 0 ? '已達上限，請先撤銷不用的金鑰' : '';
    fill($('#keys-table'), LL.table([
      { label: '名稱 (name)', cell: (k) => h('strong', null, k.name || '—') },
      { label: '金鑰 (key)', cell: (k) => h('span', { class: 'key-display' }, k.display) },
      { label: '建立', cls: 'nowrap', cell: (k) => LL.timeEl(k.created_at) },
      { label: '最後使用', cls: 'nowrap', cell: (k) => LL.timeEl(k.last_used_at, '從未使用') },
      {
        label: '到期', cls: 'nowrap', cell: (k) => k.expired
          ? h('span', { class: 'badge err', title: LL.fmtDateTime(k.expires_at) }, '已過期')
          : LL.timeEl(k.expires_at, '永不過期'),
      },
      {
        label: '', cls: 'right', cell: (k) => {
          const b = h('button', { type: 'button', class: 'btn small danger' }, '撤銷');
          b.addEventListener('click', () => revokeKey(k, b));
          return b;
        },
      },
    ], r.keys, '還沒有 API 金鑰。用上面的表單申請一把。'));
  }

  async function revokeKey(k, btn) {
    if (!confirm('確定要撤銷「' + (k.name || k.display) + '」嗎？\n使用這把金鑰的程式會立刻無法呼叫 API。')) return;
    btn.disabled = true;
    try {
      await api('DELETE', '/ui/api/keys/' + encodeURIComponent(k.id));
      LL.toast('已撤銷金鑰');
      await loadKeys();
    } catch (e) {
      btn.disabled = false;
      LL.toast(e.message, true);
    }
  }

  function bindKeyForm() {
    const form = $('#key-form');
    const err = $('#key-error');
    const btn = $('#key-create');
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      err.textContent = '';
      const body = { name: $('#key-name').value.trim() };
      const exp = $('#key-expires').value;
      if (exp) body.expires_days = Number(exp);
      btn.disabled = true;
      try {
        const r = await api('POST', '/ui/api/keys', body);
        form.reset();
        showNewKey(r.key, r.record);
      } catch (e) {
        err.textContent = e.message;
      }
      try { await loadKeys(); } catch (e) { btn.disabled = false; }
    });
  }

  function showNewKey(key, rec) {
    const box = $('#new-key');
    const input = h('input', { type: 'text', readonly: true, 'aria-label': '新的 API 金鑰', spellcheck: 'false' });
    input.value = key;
    input.addEventListener('focus', () => input.select());
    const done = h('button', { type: 'button', class: 'btn' }, '我已複製，關閉');
    done.addEventListener('click', () => {
      input.value = '';
      fill(box);
    });
    fill(box, h('div', { class: 'secret-box', role: 'alert' },
      h('h3', null, '新的 API 金鑰「' + (rec.name || rec.display) + '」已建立'),
      h('p', { class: 'small' },
        h('strong', null, '這是唯一一次顯示完整金鑰。'),
        '請立即複製並妥善保存（像密碼一樣，不要貼到公開的地方）。關閉後就無法再看到，只能撤銷後重新申請。'),
      h('p', { class: 'small muted' }, 'This is the only time the full key is shown. Copy it now.'),
      h('div', { class: 'copy-line' }, input, LL.copyButton(() => key)),
      done));
    input.focus();
  }

  // ================================================================ models + how-to
  async function loadModels() {
    let r;
    try {
      r = await api('GET', '/ui/api/models');
    } catch (e) {
      r = { data: [], error: e.message };
    }
    models = r.data || [];
    modelsError = r.error;
    const sel = $('#pg-model');
    const prev = sel.value;
    if (models.length) {
      fill(sel, models.map((mdl) => h('option', { value: mdl.id },
        mdl.id + (mdl.context_length ? '  · ' + LL.fmtCompact(mdl.context_length) + ' ctx' : ''))));
      if (prev && models.some((mdl) => mdl.id === prev)) sel.value = prev;
    } else {
      fill(sel, h('option', { value: '' }, '沒有可用的模型 (no models)'));
    }
    const note = $('#pg-models-error');
    if (modelsError || !models.length) {
      note.textContent = modelsError || '目前沒有你可以使用的模型。後端可能還沒啟動，或帳號沒有被授權任何模型。';
      note.classList.remove('hidden');
    } else {
      note.classList.add('hidden');
    }
    renderHowTo();
  }

  function renderHowTo() {
    if (!me) return;
    const base = (me.public_url || location.origin).replace(/\/+$/, '');
    const apiBase = base + '/v1';
    const model = models.length ? models[0].id : 'MODEL_NAME';
    const keyHint = 'sk-llop-你的金鑰';

    const notices = [];
    if (!me.public_url) {
      notices.push(h('p', { class: 'notice warn' },
        '目前還沒有公開網址 (public URL)，下面顯示的是你現在開啟這個頁面的網址。' +
        '如果要從外部連線，請管理員啟用 Cloudflare tunnel 或設定 server.public_url。'));
    } else if (me.url_may_change) {
      notices.push(h('p', { class: 'notice' },
        h('strong', null, '網址可能會改變：'),
        '目前使用 Cloudflare quick tunnel，主機重新啟動後會換一個新網址。' +
        '如果程式突然連不上，請回到這一頁確認最新網址。',
        me.public_url_updated_at ? h('span', { class: 'muted' }, '（網址更新於 ', LL.timeEl(me.public_url_updated_at), '）') : null));
    }

    const curl =
      'curl ' + apiBase + '/chat/completions \\\n' +
      '  -H "Authorization: Bearer ' + keyHint + '" \\\n' +
      '  -H "Content-Type: application/json" \\\n' +
      "  -d '{\"model\": " + JSON.stringify(model) + ', "messages": [{"role": "user", "content": "你好"}]}\'\n\n' +
      '# 查詢自己的額度與今日用量 (quota / usage)\n' +
      'curl ' + apiBase + '/me -H "Authorization: Bearer ' + keyHint + '"';

    const powershell =
      '# Windows PowerShell\n' +
      '$body = @{\n' +
      '  model    = ' + JSON.stringify(model) + '\n' +
      '  messages = @(@{ role = "user"; content = "你好" })\n' +
      '} | ConvertTo-Json -Depth 5\n\n' +
      '$r = Invoke-RestMethod -Method Post -Uri "' + apiBase + '/chat/completions" `\n' +
      '  -Headers @{ Authorization = "Bearer ' + keyHint + '" } `\n' +
      '  -ContentType "application/json; charset=utf-8" `\n' +
      '  -Body ([Text.Encoding]::UTF8.GetBytes($body))\n' +
      '$r.choices[0].message.content';

    const python =
      '# pip install openai\n' +
      'from openai import OpenAI\n\n' +
      'client = OpenAI(\n' +
      '    base_url=' + JSON.stringify(apiBase) + ',\n' +
      '    api_key="' + keyHint + '",\n' +
      ')\n\n' +
      'resp = client.chat.completions.create(\n' +
      '    model=' + JSON.stringify(model) + ',\n' +
      '    messages=[{"role": "user", "content": "你好"}],\n' +
      ')\n' +
      'print(resp.choices[0].message.content)\n' +
      'print(resp.usage)  # 這次用了多少 tokens';

    const other = h('div', null,
      h('p', null, 'Open WebUI、Continue、Cline、LibreChat、Chatbox 等支援「OpenAI 相容 (OpenAI-compatible)」的工具都能直接用：'),
      h('dl', { class: 'kv' },
        h('dt', null, 'Base URL'), h('dd', null, h('code', null, apiBase)),
        h('dt', null, 'API key'), h('dd', null, h('code', null, keyHint)),
        h('dt', null, 'Model'), h('dd', null, h('code', null, model))),
      h('p', { class: 'small muted' },
        '串流 (stream) 可以用，但 quick tunnel 不保證串流穩定；如果回應卡住，請改用非串流。'));

    const panels = {
      curl: LL.codeBlock(curl),
      powershell: LL.codeBlock(powershell),
      python: LL.codeBlock(python),
      other,
    };
    const panelBox = h('div');
    const tabs = h('div', { class: 'tabs', role: 'tablist' });
    const select = (name) => {
      for (const b of tabs.children) b.setAttribute('aria-selected', String(b.dataset.tab === name));
      fill(panelBox, panels[name]);
    };
    [['curl', 'curl'], ['python', 'Python (openai SDK)'], ['powershell', 'PowerShell'], ['other', '其他工具']].forEach(([name, label]) => {
      const b = h('button', { type: 'button', role: 'tab', 'data-tab': name }, label);
      b.addEventListener('click', () => select(name));
      tabs.appendChild(b);
    });
    select('curl');

    const urlInput = h('input', { type: 'text', readonly: true, 'aria-label': 'API base URL' });
    urlInput.value = apiBase;
    urlInput.addEventListener('focus', () => urlInput.select());

    fill($('#howto'),
      h('p', null, '1. 在上面「API 金鑰」申請一把金鑰。2. 把下面的 Base URL 和金鑰填進你的程式或工具。'),
      h('label', { for: 'howto-url' }, 'API Base URL'),
      h('div', { class: 'copy-line' }, setId(urlInput, 'howto-url'), LL.copyButton(() => apiBase)),
      notices,
      h('p', { class: 'small muted' },
        '可用的模型：', models.length ? models.map((mdl) => mdl.id).join('、') : '（載入中或沒有）'),
      tabs, panelBox);
  }

  function setId(el, id) { el.id = id; return el; }

  // ================================================================ playground
  const history = [];

  function bindPlayground() {
    const input = $('#pg-input');
    const send = $('#pg-send');
    send.addEventListener('click', sendMessage);
    $('#pg-clear').addEventListener('click', () => {
      history.length = 0;
      fill($('#pg-log'));
      $('#pg-status').textContent = '';
    });
    input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
        ev.preventDefault();
        sendMessage();
      }
    });
  }

  function bubble(role, text, meta) {
    const el = h('div', { class: 'msg ' + role }, text);
    if (meta) el.appendChild(h('span', { class: 'msg-meta' }, meta));
    $('#pg-log').appendChild(el);
    el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    return el;
  }

  let busy = false;
  async function sendMessage() {
    if (busy) return;
    const input = $('#pg-input');
    const text = input.value.trim();
    const model = $('#pg-model').value;
    const status = $('#pg-status');
    if (!model) { status.textContent = '請先選擇模型。'; return; }
    if (!text) { input.focus(); return; }
    const maxRaw = $('#pg-max').value.trim();
    const body = { model, messages: history.concat([{ role: 'user', content: text }]) };
    if (maxRaw) body.max_tokens = Number(maxRaw);

    busy = true;
    $('#pg-send').disabled = true;
    input.value = '';
    bubble('user', text);
    const started = performance.now();
    const timer = setInterval(() => {
      status.textContent = '等待回應… ' + ((performance.now() - started) / 1000).toFixed(0) + ' 秒';
    }, 500);
    status.textContent = '等待回應…';
    try {
      const r = await api('POST', '/ui/api/playground/chat', body);
      const choice = (r.choices && r.choices[0]) || {};
      const msg = choice.message || {};
      const content = typeof msg.content === 'string' ? msg.content : '';
      const u = r.usage || {};
      const secs = ((performance.now() - started) / 1000).toFixed(1);
      const meta = [
        r.model || model,
        'prompt ' + LL.fmtNum(u.prompt_tokens) + ' · completion ' + LL.fmtNum(u.completion_tokens) +
          ' · 共 ' + LL.fmtNum(u.total_tokens) + ' tokens',
        secs + ' 秒',
        choice.finish_reason === 'length' ? '已達 max_tokens 上限，回覆被截斷' : null,
      ].filter(Boolean).join(' · ');
      history.push({ role: 'user', content: text }, { role: 'assistant', content });
      bubble('assistant', content || (msg.reasoning_content ? '（只有思考內容）\n' + msg.reasoning_content : '（空白回應）'), meta);
      status.textContent = '';
      loadUsage().catch(() => {});
    } catch (e) {
      bubble('error', e.message);
      input.value = text;
      status.textContent = '';
    } finally {
      clearInterval(timer);
      busy = false;
      $('#pg-send').disabled = false;
    }
  }
})();
